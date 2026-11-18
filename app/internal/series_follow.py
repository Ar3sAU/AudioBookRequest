"""Series pages and followed series.

- `load_series_view`: every book in a series with what the user owns, has
  requested, and what is still to come, for the series page.
- `request_missing`: request every released book the user doesn't have.
- `check_followed_series`: request books in followed series that were
  released after the user followed (run on a schedule).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from aiohttp import ClientSession
from fastapi import BackgroundTasks, HTTPException
from sqlmodel import Session, select

from app.internal.audible.search import upsert_books
from app.internal.audible.series import (
    ResolvedSeries,
    SeriesEntry,
    owned_series,
    series_by_asin,
    series_key,
)
from app.internal.audible.types import audible_region_type, get_region_from_settings
from app.internal.audiobookshelf.library import get_library_index
from app.internal.auth.authentication import DetailedUser
from app.internal.auth.config import auth_config
from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    AudiobookWithRequests,
    FollowedSeries,
    User,
)
from app.util.log import logger

# how fresh the series listing must be when checking follows
FOLLOW_CHECK_MAX_AGE = 60 * 60 * 3
# Audible release dates are calendar days; allow a day either side of following
FOLLOW_GRACE = timedelta(days=1)


@dataclass
class SeriesItem:
    entry: SeriesEntry
    book: Audiobook
    owned: bool
    requested: bool
    released: bool

    @property
    def position(self) -> float:
        return self.entry.position


@dataclass
class SeriesView:
    series: ResolvedSeries
    items: list[SeriesItem]
    authors: list[str] = field(default_factory=list)
    followed: FollowedSeries | None = None

    @property
    def released(self) -> list[SeriesItem]:
        return [i for i in self.items if i.released]

    @property
    def owned_count(self) -> int:
        return sum(1 for i in self.items if i.owned)

    @property
    def missing(self) -> list[SeriesItem]:
        return [i for i in self.items if i.released and not i.owned and not i.requested]

    @property
    def upcoming(self) -> list[SeriesItem]:
        return [i for i in self.items if not i.released]

    @property
    def cover(self) -> str | None:
        for i in self.items:
            if i.book.cover_image:
                return i.book.cover_image
        return None

    def with_requests(self, item: SeriesItem, username: str) -> AudiobookWithRequests:
        return AudiobookWithRequests(
            book=item.book, requests=item.book.requests, username=username
        )


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def load_series_view(
    session: Session,
    client_session: ClientSession,
    series_asin: str,
    username: str,
    region: audible_region_type | None = None,
    max_age: float | None = None,
) -> SeriesView | None:
    region = region or get_region_from_settings()
    kwargs = {} if max_age is None else {"max_age": max_age}
    resolved = await series_by_asin(client_session, series_asin, None, region, **kwargs)
    if resolved is None or not resolved.entries:
        return None

    stored = {
        b.asin: b for b in upsert_books(session, [e.book for e in resolved.entries])
    }
    index = get_library_index(session)
    owned_positions: set[float] = set()
    key = series_key(resolved.name)
    for o in owned_series(session):
        if o.key == key:
            owned_positions = o.positions
            break
    requested = set(
        session.exec(
            select(AudiobookRequest.asin).where(
                AudiobookRequest.user_username == username
            )
        ).all()
    )

    now = _now()
    items: list[SeriesItem] = []
    authors: list[str] = []
    for entry in resolved.entries:
        book = stored.get(entry.book.asin)
        if book is None:
            continue
        owned = book.fulfilled or index.owns(book) or entry.position in owned_positions
        if owned and not book.fulfilled:
            book.fulfilled = True
        items.append(
            SeriesItem(
                entry=entry,
                book=book,
                owned=owned,
                requested=book.asin in requested,
                released=_aware(book.release_date) <= now,
            )
        )
        for a in book.authors:
            if a not in authors:
                authors.append(a)
    session.commit()
    for item in items:
        session.refresh(item.book)

    followed = session.get(FollowedSeries, (username, series_asin))
    return SeriesView(
        series=resolved, items=items, authors=authors[:3], followed=followed
    )


async def _request(
    session: Session,
    client_session: ClientSession,
    user: DetailedUser,
    background_tasks: BackgroundTasks,
    asin: str,
    region: audible_region_type,
) -> bool:
    # local import: routers import this module's callers
    from app.routers.api.requests import create_request

    try:
        await create_request(
            session=session,
            client_session=client_session,
            user=user,
            background_task=background_tasks,
            asin_or_uuid=asin,
            region=region,
        )
        return True
    except HTTPException as e:
        if e.status_code != 409:  # 409 = already requested
            logger.warning("Series: request failed", asin=asin, detail=e.detail)
        return False


async def request_missing(
    session: Session,
    client_session: ClientSession,
    user: DetailedUser,
    background_tasks: BackgroundTasks,
    series_asin: str,
    region: audible_region_type | None = None,
) -> int:
    """Request every released book in the series the user neither owns nor requested."""
    region = region or get_region_from_settings()
    view = await load_series_view(
        session, client_session, series_asin, user.username, region
    )
    if view is None:
        return 0
    count = 0
    for item in view.missing:
        if await _request(
            session, client_session, user, background_tasks, item.book.asin, region
        ):
            count += 1
    return count


def follow_series(
    session: Session, username: str, series_asin: str, name: str, region: str
) -> FollowedSeries:
    existing = session.get(FollowedSeries, (username, series_asin))
    if existing:
        return existing
    follow = FollowedSeries(
        user_username=username, series_asin=series_asin, name=name, region=region
    )
    session.add(follow)
    session.commit()
    session.refresh(follow)
    return follow


def unfollow_series(session: Session, username: str, series_asin: str) -> None:
    existing = session.get(FollowedSeries, (username, series_asin))
    if existing:
        session.delete(existing)
        session.commit()


def new_releases_to_request(
    view: SeriesView, followed_at: datetime
) -> list[SeriesItem]:
    """Released books after the furthest one the user has, released since following."""
    have = [i.position for i in view.items if i.owned or i.requested]
    furthest = max(have) if have else 0.0
    since = _aware(followed_at) - FOLLOW_GRACE
    return [
        i
        for i in view.items
        if i.released
        and not i.owned
        and not i.requested
        and i.position > furthest
        and _aware(i.book.release_date) >= since
    ]


async def check_followed_series(session: Session, client_session: ClientSession) -> int:
    """Request new releases in every followed series. Returns how many were requested."""
    background_tasks = BackgroundTasks()
    requested = 0
    login_type = auth_config.get_login_type(session)
    for follow in session.exec(select(FollowedSeries)).all():
        user = session.get(User, follow.user_username)
        if user is None:
            continue
        detailed = DetailedUser.model_validate(user, update={"login_type": login_type})
        region: audible_region_type = (
            follow.region if follow.region else get_region_from_settings()  # pyright: ignore[reportAssignmentType]
        )
        try:
            view = await load_series_view(
                session,
                client_session,
                follow.series_asin,
                user.username,
                region,
                max_age=FOLLOW_CHECK_MAX_AGE,
            )
        except Exception as e:
            logger.warning(
                "Series: follow check failed", series=follow.name, error=str(e)
            )
            continue
        if view is not None:
            for item in new_releases_to_request(view, follow.created_at):
                if await _request(
                    session,
                    client_session,
                    detailed,
                    background_tasks,
                    item.book.asin,
                    region,
                ):
                    requested += 1
                    logger.info(
                        "Series: requested new release",
                        series=follow.name,
                        book=item.book.title,
                        user=user.username,
                    )
        follow.last_checked_at = _now()
        session.add(follow)
        session.commit()
    # notifications and auto-downloads queued by create_request
    await background_tasks()
    return requested
