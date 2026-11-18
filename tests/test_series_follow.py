from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import ClientSession
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.internal import series_follow
from app.internal.audible.series import ResolvedSeries, SeriesEntry
from app.internal.models import Audiobook, AudiobookRequest, FollowedSeries
from app.internal.series_follow import (
    SeriesItem,
    SeriesView,
    check_followed_series,
    new_releases_to_request,
)

SERIES = "SERIES1"
NOW = datetime.now(UTC)


def _book(n: int, released: datetime) -> Audiobook:
    return Audiobook(
        asin=f"BSAGA{n}",
        title=f"Saga {n}",
        subtitle=None,
        authors=["Saga Author"],
        narrators=["Narrator"],
        cover_image=None,
        release_date=released,
        runtime_length_min=600,
    )


def _resolved(*books: tuple[int, datetime]) -> ResolvedSeries:
    return ResolvedSeries(
        key="the saga",
        name="The Saga",
        asin=SERIES,
        entries=[
            SeriesEntry(position=float(n), sequence=str(n), book=_book(n, when))
            for n, when in books
        ],
    )


@pytest.fixture
def saga(monkeypatch: pytest.MonkeyPatch) -> dict[str, ResolvedSeries]:
    """Books 1-3 long released, 4 released yesterday, 5 next month."""
    holder = {
        "series": _resolved(
            (1, NOW - timedelta(days=900)),
            (2, NOW - timedelta(days=600)),
            (3, NOW - timedelta(days=300)),
            (4, NOW - timedelta(days=1)),
            (5, NOW + timedelta(days=30)),
        )
    }

    async def by_asin(*_: object, **kwargs: object) -> ResolvedSeries:
        del kwargs
        return holder["series"]

    monkeypatch.setattr(series_follow, "series_by_asin", by_asin)
    return holder


# -- rule --------------------------------------------------------------------
def _item(n: int, released_days_ago: int, owned: bool = False) -> SeriesItem:
    when = NOW - timedelta(days=released_days_ago)
    return SeriesItem(
        entry=SeriesEntry(position=float(n), sequence=str(n), book=_book(n, when)),
        book=_book(n, when),
        owned=owned,
        requested=False,
        released=released_days_ago >= 0,
    )


def test_follow_only_requests_books_released_since_following() -> None:
    view = SeriesView(
        series=_resolved(),
        items=[
            _item(1, 900, owned=True),
            _item(2, 600),  # older gap: never auto-requested
            _item(3, 300, owned=True),
            _item(4, 1),  # released after following
            _item(5, -30),  # not out yet
        ],
    )
    picked = new_releases_to_request(view, followed_at=NOW - timedelta(days=10))
    assert [i.book.asin for i in picked] == ["BSAGA4"]

    # following today: book 4 (yesterday) still counts thanks to the grace day
    assert [i.book.asin for i in new_releases_to_request(view, NOW)] == ["BSAGA4"]
    # following after a week: book 4 predates the follow
    assert new_releases_to_request(view, NOW + timedelta(days=7)) == []


# -- pages -------------------------------------------------------------------
def test_series_page(admin_client: TestClient, saga: object) -> None:
    _ = saga
    page = admin_client.get(f"/series/{SERIES}")
    assert page.status_code == 200, page.text
    assert "The Saga - AudioBookRequest" in page.text
    assert "of 4 released books in your library" in page.text
    assert "Request 4 missing" in page.text
    assert "Coming " in page.text  # book 5
    assert "Follow series" in page.text


def test_series_page_404(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def none(*_: object, **kwargs: object) -> None:
        del kwargs

    monkeypatch.setattr(series_follow, "series_by_asin", none)
    assert admin_client.get("/series/NOPE").status_code == 404


def test_follow_and_unfollow(
    admin_client: TestClient, saga: object, db: Session
) -> None:
    _ = saga
    followed = admin_client.post(f"/series/{SERIES}/hx-follow")
    assert followed.status_code == 200, followed.text
    assert "Following" in followed.text
    row = db.get(FollowedSeries, ("root", SERIES))
    assert row is not None and row.name == "The Saga"

    unfollowed = admin_client.delete(f"/series/{SERIES}/hx-follow")
    assert "Follow series" in unfollowed.text
    db.expire_all()
    assert db.get(FollowedSeries, ("root", SERIES)) is None


def test_request_missing(admin_client: TestClient, saga: object, db: Session) -> None:
    _ = saga
    response = admin_client.post(f"/series/{SERIES}/hx-request-missing")
    assert response.status_code == 200, response.text
    assert "Requested 4 books" in response.text
    asins = set(
        db.exec(
            select(AudiobookRequest.asin).where(
                AudiobookRequest.user_username == "root"
            )
        ).all()
    )
    assert {"BSAGA1", "BSAGA2", "BSAGA3", "BSAGA4"} <= asins
    assert "BSAGA5" not in asins  # unreleased

    again = admin_client.post(f"/series/{SERIES}/hx-request-missing")
    assert "Nothing left to request" in again.text


@pytest.mark.anyio
async def test_follow_check_requests_new_release(
    admin_client: TestClient, saga: dict[str, ResolvedSeries], db: Session
) -> None:
    _ = (admin_client, saga)  # seeds users; stubs the series lookup
    db.add(
        FollowedSeries(
            user_username="root",
            series_asin=SERIES,
            name="The Saga",
            region="us",
            created_at=NOW - timedelta(days=10),
        )
    )
    db.commit()

    async with ClientSession() as cs:
        assert await check_followed_series(db, cs) == 1
        # nothing new on the next run
        assert await check_followed_series(db, cs) == 0

    requested = set(
        db.exec(
            select(AudiobookRequest.asin).where(
                AudiobookRequest.user_username == "root"
            )
        ).all()
    )
    assert "BSAGA4" in requested
    assert not {"BSAGA1", "BSAGA2", "BSAGA3", "BSAGA5"} & requested
    follow = db.get(FollowedSeries, ("root", SERIES))
    assert follow is not None and follow.last_checked_at is not None


def test_upsert_never_binds_cached_objects(db: Session) -> None:
    """Regression: cached books passed to upsert must stay usable afterwards."""
    from app.internal.audible.search import upsert_books

    cached = [_book(7, NOW), _book(7, NOW), _book(8, NOW)]  # duplicate ASIN too
    stored = upsert_books(db, cached)
    assert len({b.asin for b in stored}) == 2
    db.close()
    # the cached originals stay readable after the storing session is gone
    assert [b.title for b in cached] == ["Saga 7", "Saga 7", "Saga 8"]
