from typing import Annotated

from aiohttp import ClientSession
from fastapi import APIRouter, Depends, HTTPException, Security
from sqlmodel import Session

from app.internal.audible.details import get_book_details
from app.internal.audible.search import upsert_books
from app.internal.audible.series import series_by_asin
from app.internal.audible.similar import list_similar_audible_books
from app.internal.audible.types import get_region_from_settings
from app.internal.audiobookshelf.library import get_library_index
from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.models import Audiobook, AudiobookWithRequests
from app.internal.ranking.quality import quality_config
from app.internal.recommendations.user_recommendations import AudiobookRecommendation
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.log import logger
from app.util.templates import catalog_response

router = APIRouter(prefix="/book")


def _wrap(book: Audiobook, username: str, reason: str | None = None):
    return AudiobookRecommendation(
        book=AudiobookWithRequests(
            book=book, requests=book.requests, username=username
        ),
        reason=reason,
    )


@router.get("/{asin}")
async def read_book(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
):
    region = get_region_from_settings()
    details = await get_book_details(client_session, asin, region)

    book = session.get(Audiobook, asin)
    if book is None:
        if details.product is None:
            raise HTTPException(status_code=404, detail="Book not found")
        book = upsert_books(session, [details.product.to_audiobook()])[0]

    if not book.fulfilled and get_library_index(session).owns(book):
        book.fulfilled = True
        session.add(book)
        session.commit()

    # More in this series
    series_name: str | None = None
    series_position: str | None = None
    series_asin: str | None = None
    series_books: list[AudiobookRecommendation] = []
    if details.series:
        first = details.series[0]
        series_name, series_position = first.title, first.sequence
        series_asin = first.asin
        resolved = await series_by_asin(client_session, first.asin, first.title, region)
        if resolved and resolved.entries:
            stored = {
                b.asin: b
                for b in upsert_books(session, [e.book for e in resolved.entries])
            }
            index = get_library_index(session)
            for entry in resolved.entries:
                b = stored.get(entry.book.asin)
                if b is None:
                    continue
                if not b.fulfilled and index.owns(b):
                    b.fulfilled = True
                label = "This book" if b.asin == asin else f"Book {entry.sequence}"
                series_books.append(_wrap(b, user.username, label))
            session.commit()

    # Listeners also enjoyed
    similar: list[AudiobookRecommendation] = []
    try:
        sims = await list_similar_audible_books(
            session, client_session, asin, num_results=10, audible_region=region
        )
        similar = [_wrap(b, user.username) for b in sims if b.asin != asin]
    except Exception as e:
        logger.debug("Audible: similar books unavailable", asin=asin, error=str(e))

    session.refresh(book)
    return catalog_response(
        "Book.Index",
        user=user,
        book_with_requests=AudiobookWithRequests(
            book=book, requests=book.requests, username=user.username
        ),
        details=details,
        series_name=series_name,
        series_position=series_position,
        series_asin=series_asin,
        series_books=series_books,
        similar=similar,
        region=region,
        auto_start_download=quality_config.get_auto_download(session),
    )
