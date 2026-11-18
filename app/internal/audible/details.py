"""Extra Audible metadata for a book's page: summary and series membership."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from aiohttp import ClientSession
from pydantic import BaseModel

from app.internal.audible.types import (
    AudibleProduct,
    audible_region_type,
    audible_regions,
    get_region_from_settings,
)
from app.util.log import logger

DETAILS_TTL = 60 * 60 * 24


class _SeriesRef(BaseModel):
    asin: str
    title: str
    sequence: str | None = None


class _Product(AudibleProduct):
    series: list[_SeriesRef] | None = None
    publisher_summary: str | None = None
    merchandising_summary: str | None = None
    publisher_name: str | None = None
    language: str | None = None
    format_type: str | None = None


class _Response(BaseModel):
    product: _Product


@dataclass
class BookSeries:
    asin: str
    title: str
    sequence: str | None


@dataclass
class BookDetails:
    product: AudibleProduct | None = None
    summary_html: str | None = None
    publisher: str | None = None
    language: str | None = None
    format_type: str | None = None
    series: list[BookSeries] = field(default_factory=list)


_cache: dict[tuple[str, str], tuple[float, BookDetails]] = {}


def clear_details_cache() -> None:
    _cache.clear()


async def get_book_details(
    client_session: ClientSession,
    asin: str,
    region: audible_region_type | None = None,
) -> BookDetails:
    """Empty details (not an error) when Audible doesn't know the ASIN in this region."""
    region = region or get_region_from_settings()
    key = (region, asin)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < DETAILS_TTL:
        return hit[1]

    url = f"https://api.audible{audible_regions[region]}/1.0/catalog/products/{asin}"
    params = {"response_groups": "media,series,product_desc,product_attrs"}
    details = BookDetails()
    try:
        async with client_session.get(url, params=params) as resp:
            resp.raise_for_status()
            product = _Response.model_validate(await resp.json()).product
        details = BookDetails(
            product=product,
            summary_html=product.publisher_summary or product.merchandising_summary,
            publisher=product.publisher_name,
            language=product.language,
            format_type=product.format_type,
            series=[
                BookSeries(asin=s.asin, title=s.title, sequence=s.sequence)
                for s in product.series or []
            ],
        )
    except Exception as e:
        # region without this ASIN returns a product with no fields
        logger.debug("Audible: no details for book", asin=asin, error=str(e))
    _cache[key] = (time.time(), details)
    return details
