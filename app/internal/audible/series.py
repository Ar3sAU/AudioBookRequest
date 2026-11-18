"""Series awareness: "next in your series" and "missing from your series".

Series come from the synced Audiobookshelf library (`LibraryItem.series`).
Each is resolved in the configured Audible region by *name*, not ASIN,
because ASINs differ per marketplace (an ABS library matched against
audible.com has US ASINs; an AU instance searches audible.com.au).

Resolution per series (results cached for SERIES_TTL):
1. Catalog search "<series> <author>" with the `series` response group and
   pick the series whose normalised title matches.
2. Fetch the series product's `relationships` to list every book in it.
3. Batch-fetch those books' metadata, drop Audible's placeholder entries
   (release year 2200, no runtime) and keep one edition per position.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from aiohttp import ClientSession
from pydantic import BaseModel
from sqlmodel import Session, select

from app.internal.audible.types import (
    AudibleProduct,
    audible_region_type,
    audible_regions,
    get_region_from_settings,
)
from app.internal.audiobookshelf.config import abs_config
from app.internal.audiobookshelf.library import (
    author_key,
    get_library_index,
    normalize,
)
from app.internal.models import Audiobook, LibraryItem
from app.util.log import logger

SERIES_TTL = 60 * 60 * 24
MAX_SERIES = 40
CONCURRENCY = 4
BATCH = 50
PLACEHOLDER_YEAR = 2100

# ---------------------------------------------------------------------------
# Owned series (from the local library copy)
# ---------------------------------------------------------------------------

_SERIES_NOISE = re.compile(r"^(?:the )|(?: series| saga| books| novels)$")


def series_key(name: str) -> str:
    """'The Solo Leveling Series' and 'Solo Leveling' -> 'solo leveling'."""
    key = normalize(name)
    while True:
        stripped = _SERIES_NOISE.sub("", key).strip()
        if stripped == key:
            return key
        key = stripped


def parse_position(sequence: str | None) -> float | None:
    if not sequence:
        return None
    m = re.match(r"^\s*(\d+(?:\.\d+)?)", sequence)
    return float(m.group(1)) if m else None


_BUNDLE = re.compile(r"\bbooks?\s+(\d+(?:\s*(?:,|&|and|-|to)\s*\d+)+)", re.IGNORECASE)


def bundle_positions(text: str) -> set[float]:
    """'Book 1, 2' / 'Books 1-3' / 'Books 4 & 5' -> the positions covered."""
    m = _BUNDLE.search(text)
    if not m:
        return set()
    spec = m.group(1)
    positions: set[float] = set()
    for part in re.split(r"\s*(?:,|&|and)\s*", spec):
        bounds = re.split(r"\s*(?:-|to)\s*", part)
        nums = [int(b) for b in bounds if b.isdigit()]
        if len(nums) == 2 and 0 < nums[1] - nums[0] <= 20:
            positions.update(float(n) for n in range(nums[0], nums[1] + 1))
        elif nums:
            positions.update(float(n) for n in nums)
    return positions


@dataclass
class OwnedSeries:
    key: str
    name: str
    authors: list[str] = field(default_factory=list)
    positions: set[float] = field(default_factory=set)
    last_added: datetime | None = None

    @property
    def max_position(self) -> float:
        return max(self.positions) if self.positions else 0.0


def owned_series(session: Session) -> list[OwnedSeries]:
    """Series in the configured library, most recently added first."""
    library_id = abs_config.get_library_id(session)
    if not library_id:
        return []
    by_key: dict[str, OwnedSeries] = {}
    for item in session.exec(
        select(LibraryItem).where(LibraryItem.library_id == library_id)
    ).all():
        for s in item.series:
            name = s.get("name")
            if not name:
                continue
            key = series_key(name)
            if not key:
                continue
            owned = by_key.setdefault(key, OwnedSeries(key=key, name=name))
            for a in item.authors:
                if a not in owned.authors:
                    owned.authors.append(a)
            if (pos := parse_position(s.get("sequence"))) is not None:
                owned.positions.add(pos)
            # bundles: "Publisher's Pack 2" with subtitle "Book 3, 4"
            owned.positions.update(
                bundle_positions(f"{item.title} {item.subtitle or ''}")
            )
            added = item.added_at
            if added is not None:
                if added.tzinfo is None:
                    added = added.replace(tzinfo=UTC)
                if owned.last_added is None or added > owned.last_added:
                    owned.last_added = added
    epoch = datetime.min.replace(tzinfo=UTC)
    return sorted(by_key.values(), key=lambda s: s.last_added or epoch, reverse=True)


# ---------------------------------------------------------------------------
# Audible resolution
# ---------------------------------------------------------------------------


class _SeriesRef(BaseModel):
    asin: str
    title: str
    sequence: str | None = None


class _ProductWithSeries(AudibleProduct):
    series: list[_SeriesRef] | None = None


class _SearchResponse(BaseModel):
    products: list[_ProductWithSeries] = []


class _Relationship(BaseModel):
    asin: str
    relationship_to_product: str | None = None
    relationship_type: str | None = None
    sequence: str | None = None
    sort: str | None = None


class _SeriesProduct(BaseModel):
    class _Inner(BaseModel):
        relationships: list[_Relationship] = []

    product: _Inner


class _BatchResponse(BaseModel):
    products: list[_ProductWithSeries] = []


@dataclass
class SeriesEntry:
    position: float
    sequence: str
    book: Audiobook


@dataclass
class ResolvedSeries:
    key: str
    name: str
    asin: str
    entries: list[SeriesEntry]


@dataclass
class _CacheEntry:
    value: ResolvedSeries | None
    timestamp: float


_series_cache: dict[tuple[str, str], _CacheEntry] = {}
_build_lock = asyncio.Lock()


def clear_series_cache() -> None:
    _series_cache.clear()


def _api(region: audible_region_type) -> str:
    return f"https://api.audible{audible_regions[region]}/1.0/catalog/products"


def _is_placeholder(p: AudibleProduct) -> bool:
    try:
        year = datetime.fromisoformat(p.release_date).year
    except ValueError:
        return True
    return year >= PLACEHOLDER_YEAR


async def _find_series_asin(
    client_session: ClientSession, region: audible_region_type, owned: OwnedSeries
) -> tuple[str, str] | None:
    author = owned.authors[0] if owned.authors else ""
    params = {
        "keywords": f"{owned.name} {author}".strip(),
        "num_results": "25",
        "products_sort_by": "Relevance",
        "response_groups": "series,media",
    }
    async with client_session.get(_api(region), params=params) as resp:
        resp.raise_for_status()
        data = _SearchResponse.model_validate(await resp.json())
    counts: dict[str, int] = {}
    titles: dict[str, str] = {}
    for p in data.products:
        if not author_overlap(p.to_audiobook(), owned):
            continue
        for s in p.series or []:
            if series_key(s.title) == owned.key:
                counts[s.asin] = counts.get(s.asin, 0) + 1
                titles[s.asin] = s.title
    if not counts:
        return None
    best = max(counts, key=lambda a: counts[a])
    return best, titles[best]


async def _series_children(
    client_session: ClientSession, region: audible_region_type, series_asin: str
) -> list[_Relationship]:
    params = {"response_groups": "relationships"}
    async with client_session.get(
        f"{_api(region)}/{series_asin}", params=params
    ) as resp:
        resp.raise_for_status()
        data = _SeriesProduct.model_validate(await resp.json())
    return [
        r
        for r in data.product.relationships
        if r.relationship_to_product == "child" and r.relationship_type == "series"
    ]


async def _fetch_products(
    client_session: ClientSession, region: audible_region_type, asins: list[str]
) -> dict[str, _ProductWithSeries]:
    found: dict[str, _ProductWithSeries] = {}
    for i in range(0, len(asins), BATCH):
        params = {
            "asins": ",".join(asins[i : i + BATCH]),
            "response_groups": "media,series",
        }
        async with client_session.get(_api(region), params=params) as resp:
            resp.raise_for_status()
            data = _BatchResponse.model_validate(await resp.json())
        for p in data.products:
            if p.title:
                found[p.asin] = p
    return found


async def resolve_series(
    client_session: ClientSession,
    owned: OwnedSeries,
    region: audible_region_type | None = None,
) -> ResolvedSeries | None:
    region = region or get_region_from_settings()
    cache_key = (region, owned.key)
    cached = _series_cache.get(cache_key)
    if cached and time.time() - cached.timestamp < SERIES_TTL:
        return cached.value

    result: ResolvedSeries | None = None
    try:
        found = await _find_series_asin(client_session, region, owned)
        if found is not None:
            series_asin, title = found
            children = await _series_children(client_session, region, series_asin)
            products = await _fetch_products(
                client_session, region, [c.asin for c in children]
            )
            best: dict[float, tuple[_Relationship, _ProductWithSeries]] = {}
            for rel in children:
                product = products.get(rel.asin)
                pos = parse_position(rel.sequence) or parse_position(rel.sort)
                if product is None or pos is None or _is_placeholder(product):
                    continue
                current = best.get(pos)
                # several editions can share a position; prefer the one with audio
                if current is None or (
                    product.runtime_length_min > current[1].runtime_length_min
                ):
                    best[pos] = (rel, product)
            entries = [
                SeriesEntry(
                    position=pos,
                    sequence=rel.sequence or rel.sort or "",
                    book=product.to_audiobook(),
                )
                for pos, (rel, product) in sorted(best.items())
            ]
            result = ResolvedSeries(
                key=owned.key, name=title, asin=series_asin, entries=entries
            )
    except Exception as e:
        logger.warning(
            "Audible: failed to resolve series", series=owned.name, error=str(e)
        )
        # do not cache failures for the full TTL; retry in an hour
        _series_cache[cache_key] = _CacheEntry(None, time.time() - SERIES_TTL + 3600)
        return None

    _series_cache[cache_key] = _CacheEntry(result, time.time())
    return result


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


@dataclass
class SeriesSuggestion:
    book: Audiobook
    series_name: str
    sequence: str
    reason: str


@dataclass
class SeriesRows:
    next_up: list[SeriesSuggestion] = field(default_factory=list)
    missing: list[SeriesSuggestion] = field(default_factory=list)


def _owns(index_owns: bool, entry: SeriesEntry, owned: OwnedSeries) -> bool:
    return index_owns or entry.position in owned.positions


async def build_series_rows(
    session: Session,
    client_session: ClientSession,
    region: audible_region_type | None = None,
    limit: int = 20,
) -> SeriesRows:
    """Next unowned book after the furthest owned one, and unowned gaps before it.

    Books returned are not yet persisted; callers upsert them so requests work.
    """
    async with _build_lock:
        return await _build_series_rows(session, client_session, region, limit)


async def _build_series_rows(
    session: Session,
    client_session: ClientSession,
    region: audible_region_type | None,
    limit: int,
) -> SeriesRows:
    series = owned_series(session)[:MAX_SERIES]
    if not series:
        return SeriesRows()
    index = get_library_index(session)
    semaphore = asyncio.Semaphore(CONCURRENCY)
    now = datetime.now(UTC)

    async def _resolve(o: OwnedSeries) -> tuple[OwnedSeries, ResolvedSeries | None]:
        async with semaphore:
            return o, await resolve_series(client_session, o, region)

    resolved = await asyncio.gather(*[_resolve(o) for o in series])

    rows = SeriesRows()
    for owned, res in resolved:
        if res is None:
            continue
        furthest = owned.max_position
        next_added = False
        for entry in res.entries:
            if _owns(index.owns(entry.book), entry, owned):
                continue
            released = entry.book.release_date <= now
            label = f"{res.name} · Book {entry.sequence}"
            if entry.position > furthest:
                if not next_added and released:
                    rows.next_up.append(
                        SeriesSuggestion(
                            book=entry.book,
                            series_name=res.name,
                            sequence=entry.sequence,
                            reason=f"Next in {label}",
                        )
                    )
                    next_added = True
            elif released and entry.position.is_integer():
                rows.missing.append(
                    SeriesSuggestion(
                        book=entry.book,
                        series_name=res.name,
                        sequence=entry.sequence,
                        reason=f"Missing from {label}",
                    )
                )
    rows.next_up = rows.next_up[:limit]
    rows.missing = rows.missing[:limit]
    return rows


def author_overlap(book: Audiobook, owned: OwnedSeries) -> bool:
    """Guard for name collisions between unrelated series."""
    mine = {author_key(a) for a in owned.authors}
    return not mine or any(author_key(a) in mine for a in book.authors)
