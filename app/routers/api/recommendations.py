from collections.abc import Sequence
from typing import Annotated

from aiohttp import ClientSession
from fastapi import APIRouter, Depends, Query, Security
from pydantic import BaseModel
from sqlmodel import Session

from app.internal.audible.category import (
    list_category_audible_books,
    list_combined_audible_books,
)
from app.internal.audible.search import upsert_books
from app.internal.audible.series import SeriesSuggestion, build_series_rows
from app.internal.audible.types import audible_region_type, get_region_from_settings
from app.internal.auth.authentication import AnyAuth, DetailedUser
from app.internal.models import Audiobook, AudiobookWithRequests
from app.internal.recommendations.local import (
    AudiobookPopularity,
    get_most_popular_authors,
    get_popular_books,
    get_recently_requested_books,
)
from app.internal.recommendations.user_recommendations import (
    AudiobookRecommendation,
    UserSimsRecommendation,
    get_user_sims_recommendations,
)
from app.util.connection import get_connection
from app.util.db import get_session

router = APIRouter(prefix="/recommendations", tags=["Recommendations"])


def _resolve_exclude_fulfilled(
    exclude_fulfilled: bool, exclude_downloaded: bool | None
) -> bool:
    return exclude_fulfilled if exclude_downloaded is None else exclude_downloaded


def parse_seed_asins(seed_asins: list[str] | None) -> list[str] | None:
    """Accept both repeated `seed_asins` params and one comma-separated value."""
    if seed_asins is None:
        return None
    return [
        asin.strip()
        for value in seed_asins
        for asin in value.split(",")
        if asin.strip()
    ]


@router.get("/user", response_model=UserSimsRecommendation)
async def get_user_recommendations(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(AnyAuth())],
    seed_asins: Annotated[
        list[str] | None,
        Query(
            alias="seed_asins",
            description="ASINs to seed recommendations with. Repeat the parameter or pass a comma-separated list.",
        ),
    ] = None,
    limit: Annotated[
        int, Query(description="Maximum number of recommendations to return.")
    ] = 20,
    offset: Annotated[int, Query(description="Number of recommendations to skip.")] = 0,
) -> UserSimsRecommendation:
    recommendations = await get_user_sims_recommendations(
        session=session,
        client_session=client_session,
        user=user,
        seed_asins=parse_seed_asins(seed_asins),
        limit=limit,
        offset=offset,
    )
    return recommendations


@router.get("/popular", response_model=list[AudiobookPopularity])
async def get_popular_recommendations(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(AnyAuth())],
    min_requests: int = 1,
    limit: int = 10,
    exclude_fulfilled: Annotated[
        bool, Query(description="Exclude requests that are already fulfilled.")
    ] = True,
    exclude_downloaded: Annotated[
        bool | None,
        Query(
            description="Deprecated: use `exclude_fulfilled`.",
            deprecated=True,
        ),
    ] = None,
) -> list[AudiobookPopularity]:
    return get_popular_books(
        session=session,
        limit=limit,
        min_requests=min_requests,
        exclude_fulfilled=_resolve_exclude_fulfilled(
            exclude_fulfilled, exclude_downloaded
        ),
        exclude_requested_username=user.username,
    )


@router.get("/recent", response_model=Sequence[AudiobookWithRequests])
async def get_recently_requested_recommendations(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(AnyAuth())],
    limit: int = 10,
    days_back: int = 30,
    exclude_fulfilled: Annotated[
        bool, Query(description="Exclude requests that are already fulfilled.")
    ] = True,
    exclude_downloaded: Annotated[
        bool | None,
        Query(
            description="Deprecated: use `exclude_fulfilled`.",
            deprecated=True,
        ),
    ] = None,
) -> Sequence[AudiobookWithRequests]:
    return get_recently_requested_books(
        session=session,
        limit=limit,
        days_back=days_back,
        exclude_fulfilled=_resolve_exclude_fulfilled(
            exclude_fulfilled, exclude_downloaded
        ),
        exclude_requested_username=user.username,
    )


@router.get("/fallback", response_model=list[AudiobookWithRequests])
async def get_fallback_recommendations(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(AnyAuth())],
    limit: int = 10,
    audible_region: audible_region_type | None = None,
) -> list[AudiobookWithRequests]:
    """Get fallback popular recommendations from Audible search. Does not take into account user history or preference."""

    if audible_region is None:
        audible_region = get_region_from_settings()

    popular_search_terms = [
        "bestseller",
        "james clear",
        "atomic habits",
        "stephen king",
        "psychology",
        "biography",
        "business",
        "self help",
    ]
    return await list_combined_audible_books(
        session=session,
        client_session=client_session,
        search_terms=popular_search_terms,
        num_results=limit,
        audible_region=audible_region,
        exclude_requested_username=user.username,
    )


@router.get("/categories", response_model=dict[str, list[AudiobookWithRequests]])
async def get_category_recommendations(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(AnyAuth())],
    audible_region: audible_region_type | None = None,
) -> dict[str, list[AudiobookWithRequests]]:
    """Get recommendations by popular categories from Audible search."""

    if audible_region is None:
        audible_region = get_region_from_settings()

    return await list_category_audible_books(
        session=session,
        client_session=client_session,
        audible_region=audible_region,
        excluded_requested_username=user.username,
    )


@router.get("/authors", response_model=list[AudiobookWithRequests])
async def get_popular_authors_recommendations(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(AnyAuth())],
    limit: int = 10,
    exclude_fulfilled: Annotated[
        bool, Query(description="Exclude requests that are already fulfilled.")
    ] = True,
    exclude_downloaded: Annotated[
        bool | None,
        Query(
            description="Deprecated: use `exclude_fulfilled`.",
            deprecated=True,
        ),
    ] = None,
    audible_region: audible_region_type | None = None,
    personal_favorites: bool = True,
) -> list[AudiobookWithRequests]:
    if audible_region is None:
        audible_region = get_region_from_settings()

    author_narrators = get_most_popular_authors(
        session=session,
        limit=limit,
        exclude_fulfilled=_resolve_exclude_fulfilled(
            exclude_fulfilled, exclude_downloaded
        ),
        username=user.username if personal_favorites else None,
    )
    return await list_combined_audible_books(
        session=session,
        client_session=client_session,
        search_terms=author_narrators.authors,
        num_results=limit,
        audible_region=audible_region,
    )


@router.get("/narrators", response_model=list[AudiobookWithRequests])
async def get_popular_narrators_recommendations(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(AnyAuth())],
    limit: int = 10,
    exclude_fulfilled: Annotated[
        bool, Query(description="Exclude requests that are already fulfilled.")
    ] = True,
    exclude_downloaded: Annotated[
        bool | None,
        Query(
            description="Deprecated: use `exclude_fulfilled`.",
            deprecated=True,
        ),
    ] = None,
    audible_region: audible_region_type | None = None,
    personal_favorites: bool = True,
) -> list[AudiobookWithRequests]:
    if audible_region is None:
        audible_region = get_region_from_settings()

    author_narrators = get_most_popular_authors(
        session=session,
        limit=limit,
        exclude_fulfilled=_resolve_exclude_fulfilled(
            exclude_fulfilled, exclude_downloaded
        ),
        username=user.username if personal_favorites else None,
    )
    return await list_combined_audible_books(
        session=session,
        client_session=client_session,
        search_terms=author_narrators.narrators,
        num_results=limit,
        audible_region=audible_region,
    )


class SeriesRecommendations(BaseModel):
    next_up: list[AudiobookRecommendation]
    missing: list[AudiobookRecommendation]


def _series_to_recommendations(
    suggestions: list[SeriesSuggestion],
    stored: dict[str, Audiobook],
    username: str,
) -> list[AudiobookRecommendation]:
    result: list[AudiobookRecommendation] = []
    for s in suggestions:
        book = stored.get(s.book.asin)
        # upsert re-checks the library, and a download may have landed since
        if book is None or book.fulfilled:
            continue
        result.append(
            AudiobookRecommendation(
                book=AudiobookWithRequests(
                    book=book, requests=book.requests, username=username
                ),
                reason=s.reason,
            )
        )
    return result


@router.get("/series", response_model=SeriesRecommendations)
async def get_series_recommendations(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(AnyAuth())],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    audible_region: audible_region_type | None = None,
) -> SeriesRecommendations:
    """Next book and missing books for series in the Audiobookshelf library.

    Requires the Audiobookshelf library sync; empty otherwise.
    """
    rows = await build_series_rows(
        session,
        client_session,
        region=audible_region or get_region_from_settings(),
        limit=limit,
    )
    # one upsert (one commit) for both rows: a second commit would expire the
    # first row's objects before they are serialised
    stored = {
        b.asin: b
        for b in upsert_books(session, [s.book for s in rows.next_up + rows.missing])
    }
    return SeriesRecommendations(
        next_up=_series_to_recommendations(rows.next_up, stored, user.username),
        missing=_series_to_recommendations(rows.missing, stored, user.username),
    )
