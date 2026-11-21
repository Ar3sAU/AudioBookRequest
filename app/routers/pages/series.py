from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Annotated

from aiohttp import ClientSession
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Cookie,
    Depends,
    HTTPException,
    Security,
)
from sqlmodel import Session

from app.internal.audible.types import get_region_from_settings
from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.ranking.quality import quality_config
from app.internal.series_follow import (
    check_followed_series,
    follow_series,
    load_series_view,
    request_missing,
    unfollow_series,
)
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.log import logger
from app.util.templates import catalog_response
from app.util.toast import ToastException

FOLLOW_CHECK_HOURS = 6


async def scheduled_follow_check() -> None:
    with next(get_session()) as session:
        async with ClientSession() as client_session:
            try:
                count = await check_followed_series(session, client_session)
                logger.info("Series: follow check complete", requested=count)
            except Exception as e:
                logger.warning("Series: follow check failed", error=str(e))


@asynccontextmanager
async def lifespan(app: object) -> AsyncIterator[None]:
    _ = app
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scheduled_follow_check,
        "interval",
        hours=FOLLOW_CHECK_HOURS,
        next_run_time=datetime.now() + timedelta(minutes=3),
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    yield
    scheduler.shutdown()


router = APIRouter(prefix="/series", lifespan=lifespan)


@router.get("/{series_asin}")
async def read_series(
    series_asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
):
    region = get_region_from_settings()
    view = await load_series_view(
        session, client_session, series_asin, user.username, region
    )
    if view is None:
        raise HTTPException(status_code=404, detail="Series not found")
    return catalog_response(
        "Series.Index",
        user=user,
        view=view,
        region=region,
        auto_start_download=quality_config.get_auto_download(session),
    )


def _follow_button(user: DetailedUser, series_asin: str, followed: bool):
    return catalog_response(
        "Series.FollowButton",
        user=user,
        series_asin=series_asin,
        followed=followed,
    )


@router.post("/{series_asin}/hx-follow")
async def follow(
    series_asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
):
    region = get_region_from_settings()
    view = await load_series_view(
        session, client_session, series_asin, user.username, region
    )
    if view is None:
        raise ToastException("Series not found")
    follow_series(session, user.username, series_asin, view.series.name, region)
    return _follow_button(user, series_asin, True)


@router.delete("/{series_asin}/hx-follow")
async def unfollow(
    series_asin: str,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
):
    unfollow_series(session, user.username, series_asin)
    return _follow_button(user, series_asin, False)


@router.post("/{series_asin}/hx-request-missing")
async def request_all_missing(
    series_asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    background_tasks: BackgroundTasks,
    user: Annotated[DetailedUser, Security(ABRAuth())],
    abr_library: Annotated[str | None, Cookie()] = None,
):
    count = await request_missing(
        session,
        client_session,
        user,
        background_tasks,
        series_asin,
        library_id=abr_library,
    )
    if count == 0:
        raise ToastException("Nothing left to request", "info")
    # Returned, not raised: FastAPI attaches the queued background tasks
    # (notifications, auto-downloads) only to a returned response.
    return catalog_response(
        "ToastBlock",
        toast_success=f"Requested {count} {'book' if count == 1 else 'books'}",
        headers={"HX-Retarget": "#toast-block", "HX-Refresh": "true"},
    )
