from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Annotated

from aiohttp import ClientSession
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import APIRouter, Depends, HTTPException, Request, Response, Security
from pydantic import BaseModel
from sqlmodel import Session

from app.internal.audiobookshelf.client import (
    abs_get_libraries,
    abs_list_library_items,
    abs_trigger_scan,
)
from app.internal.audiobookshelf.config import AudiobookshelfMisconfigured, abs_config
from app.internal.audiobookshelf.library import (
    LibrarySyncError,
    library_sync_status,
    sync_library,
)
from app.internal.audiobookshelf.types import ABSLibrary
from app.internal.auth.authentication import AnyAuth, DetailedUser
from app.internal.models import GroupEnum
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.log import logger
from app.util.request import form_or_json_openapi, model_from_request

SYNC_INTERVAL_MINUTES = 30


async def scheduled_library_sync() -> None:
    with next(get_session()) as session:
        if not abs_config.is_valid(session):
            return
        async with ClientSession() as client_session:
            try:
                await sync_library(session, client_session)
            except Exception as e:
                logger.warning("ABS: scheduled library sync failed", error=str(e))


@asynccontextmanager
async def lifespan(app: object) -> AsyncIterator[None]:
    _ = app
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scheduled_library_sync,
        "interval",
        minutes=SYNC_INTERVAL_MINUTES,
        next_run_time=datetime.now() + timedelta(seconds=30),
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    yield
    scheduler.shutdown()


router = APIRouter(prefix="/audiobookshelf", lifespan=lifespan)


class ABSResponse(BaseModel):
    abs_base_url: str
    abs_api_token: str
    abs_library_id: str
    abs_check_downloaded: bool
    abs_libraries: list[ABSLibrary]


@router.get("", response_model=ABSResponse)
async def read_abs(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    admin_user: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    _ = admin_user
    base_url = abs_config.get_base_url(session) or ""
    api_token = abs_config.get_api_token(session) or ""
    library_id = abs_config.get_library_id(session) or ""
    check_downloaded = abs_config.get_check_downloaded(session)
    libraries = []
    if base_url and api_token:
        libraries = await abs_get_libraries(session, client_session)

    return ABSResponse(
        abs_base_url=base_url,
        abs_api_token=api_token,
        abs_library_id=library_id,
        abs_check_downloaded=check_downloaded,
        abs_libraries=libraries,
    )


class UpdateABSBaseUrl(BaseModel):
    base_url: str


@router.put(
    "/base-url",
    status_code=204,
    openapi_extra=form_or_json_openapi(UpdateABSBaseUrl),
)
async def update_abs_base_url(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    _ = admin_user
    body = await model_from_request(request, UpdateABSBaseUrl)
    abs_config.set_base_url(session, body.base_url)
    return Response(status_code=204)


class UpdateABSApiToken(BaseModel):
    api_token: str


@router.put(
    "/api-token",
    status_code=204,
    openapi_extra=form_or_json_openapi(UpdateABSApiToken),
)
async def update_abs_api_token(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    _ = admin_user
    body = await model_from_request(request, UpdateABSApiToken)
    abs_config.set_api_token(session, body.api_token)
    return Response(status_code=204)


class UpdateABSLibrary(BaseModel):
    library_id: str


@router.put(
    "/library",
    status_code=204,
    openapi_extra=form_or_json_openapi(UpdateABSLibrary),
)
async def update_abs_library(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    _ = admin_user
    body = await model_from_request(request, UpdateABSLibrary)
    abs_config.set_library_id(session, body.library_id)
    return Response(status_code=204)


class UpdateABSCheckDownloaded(BaseModel):
    check_downloaded: bool = False


@router.put(
    "/check-downloaded",
    status_code=204,
    openapi_extra=form_or_json_openapi(UpdateABSCheckDownloaded),
)
async def update_abs_check_downloaded(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    _ = admin_user
    body = await model_from_request(request, UpdateABSCheckDownloaded)
    abs_config.set_check_downloaded(session, body.check_downloaded)
    return Response(status_code=204)


class TestResponse(BaseModel):
    library_count: int
    triggered_scan: bool
    sample_items: list[str]


@router.get("/test-connection", response_model=TestResponse)
async def test_abs_connection(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    try:
        abs_config.raise_if_invalid(session)
    except AudiobookshelfMisconfigured as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    libraries = await abs_get_libraries(session, client_session)
    if not libraries:
        raise HTTPException(
            status_code=400, detail="Failed to connect to Audiobookshelf"
        )
    logger.info(
        "Successfully connected to Audiobookshelf", library_count=len(libraries)
    )
    success = await abs_trigger_scan(session, client_session)
    if not success:
        raise HTTPException(
            status_code=400, detail="Failed to trigger scan on Audiobookshelf"
        )
    logger.info("Successfully triggered scan on Audiobookshelf")
    list_library_items = await abs_list_library_items(session, client_session)
    logger.info(
        "Fetched items from Audiobookshelf library",
        item_count=len(list_library_items),
    )
    return TestResponse(
        library_count=len(libraries),
        triggered_scan=success,
        sample_items=[item.title for item in list_library_items[:5]],
    )


class LibrarySyncResponse(BaseModel):
    total: int
    added: int
    removed: int
    newly_owned: int
    unflagged: int


@router.post("/sync", response_model=LibrarySyncResponse)
async def run_library_sync(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    """Copy the configured Audiobookshelf library into ABR now."""
    try:
        abs_config.raise_if_invalid(session)
        result = await sync_library(session, client_session)
    except (AudiobookshelfMisconfigured, LibrarySyncError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return LibrarySyncResponse(
        total=result.total,
        added=result.added,
        removed=result.removed,
        newly_owned=result.newly_owned,
        unflagged=result.unflagged,
    )


class LibrarySyncStatusResponse(BaseModel):
    item_count: int
    last_synced: datetime | None


@router.get("/sync", response_model=LibrarySyncStatusResponse)
async def read_library_sync_status(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    count, last = library_sync_status(session)
    return LibrarySyncStatusResponse(item_count=count, last_synced=last)
