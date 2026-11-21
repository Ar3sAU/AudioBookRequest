"""Local copy of the Audiobookshelf library and the "do I already own this?" index.

The upstream check (`abs_book_exists`) runs a live ABS search for each book,
is capped at 25 books per search and only matches exact titles. Here the
whole library is synced into the `libraryitem` table on a schedule, and an
in-memory index answers ownership for any number of books with no ABS calls.

Matching, in order:
1. ASIN, the reliable key when ABS has matched the book against Audible.
2. Normalised title (with and without subtitle; series suffixes and
   parentheticals removed) plus at least one shared author surname.
3. Fuzzy title match (>= FUZZY_THRESHOLD) among books by a shared author.
"""

from __future__ import annotations

import posixpath
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime

from aiohttp import ClientSession
from pydantic import BaseModel
from rapidfuzz import fuzz
from sqlmodel import Session, col, delete, select

from app.internal.audiobookshelf.config import abs_config
from app.internal.models import Audiobook, AudiobookRequest, LibraryItem
from app.util.connection import USER_AGENT
from app.util.log import logger

PAGE_SIZE = 500
FUZZY_THRESHOLD = 92

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

# also unclosed ones: ABS titles get cut off, e.g. "The Primal Hunter 3 (Unabridge"
_BRACKETS = re.compile(r"\([^)]*(?:\)|$)|\[[^\]]*(?:\]|$)")
# "Book 10", "Vol. 2", "Part 3" -> "10", "2", "3": keeps the number, so
# "Defiance of the Fall, Book 10" and "Defiance of the Fall 10" agree while
# still differing from book 9.
_NUMBER_WORD = re.compile(r"\b(?:book|volume|vol|part|novel|no)\s+(\d)")
_UNABRIDGED = re.compile(r"\b(?:un)?abridged\b", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_LEADING_ARTICLE = re.compile(r"^(?:the|a|an) ")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ")
    return _NON_ALNUM.sub(" ", text).strip()


def core_title(title: str) -> str:
    """Title reduced to what identifies the work across Audible and ABS."""
    title = _BRACKETS.sub(" ", title)
    title = _UNABRIDGED.sub(" ", title)
    title = _NUMBER_WORD.sub(r"\1", normalize(title))
    return _LEADING_ARTICLE.sub("", title)


def title_variants(title: str, subtitle: str | None) -> set[str]:
    variants = {core_title(title)}
    if subtitle:
        variants.add(core_title(f"{title} {subtitle}"))
    return {v for v in variants if v}


def author_key(name: str) -> str:
    """Surname, so 'Brandon Sanderson' and 'Sanderson, Brandon' agree."""
    if "," in name:
        name = name.split(",", 1)[0]
        parts = normalize(name).split()
        return parts[0] if parts else ""
    parts = normalize(name).split()
    return parts[-1] if parts else ""


def split_names(joined: str | None) -> list[str]:
    """ABS minified items join multiple people as 'A, B & C'."""
    if not joined:
        return []
    parts = re.split(r",\s*|\s+&\s+|\s+and\s+", joined)
    return [p.strip() for p in parts if p.strip()]


_SERIES_PART = re.compile(r"^(?P<name>.*?)(?:\s+#(?P<seq>[\w.\-]+))?$")


def parse_series(series_name: str | None) -> list[dict[str, str | None]]:
    """'Stormlight Archive #1, Cosmere #4' -> [{name, sequence}, ...]."""
    if not series_name:
        return []
    result: list[dict[str, str | None]] = []
    for part in series_name.split(", "):
        m = _SERIES_PART.match(part.strip())
        if m and m.group("name"):
            result.append({"name": m.group("name").strip(), "sequence": m.group("seq")})
    return result


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


@dataclass
class LibraryIndex:
    asins: set[str] = field(default_factory=set)
    # core title -> author surnames that have a book with that title
    by_title: dict[str, set[str]] = field(default_factory=dict)
    # author surname -> core titles by that author
    by_author: dict[str, set[str]] = field(default_factory=dict)
    size: int = 0
    # per item, for find(): asin -> abs id, and (title variants, author keys, abs id)
    ids_by_asin: dict[str, str] = field(default_factory=dict)
    entries: list[tuple[set[str], set[str], str]] = field(default_factory=list)

    def add(self, item: LibraryItem) -> None:
        self.size += 1
        if item.asin:
            self.asins.add(item.asin.upper())
            self.ids_by_asin.setdefault(item.asin.upper(), item.abs_id)
        authors = {k for a in item.authors if (k := author_key(a))}
        variants = title_variants(item.title, item.subtitle)
        for variant in variants:
            self.by_title.setdefault(variant, set()).update(authors)
            for a in authors:
                self.by_author.setdefault(a, set()).add(variant)
        self.entries.append((variants, authors, item.abs_id))

    def find(self, book: Audiobook) -> str | None:
        """The Audiobookshelf item id that owns() matched, by the same rules."""
        if not self.size:
            return None
        if book.asin and (found := self.ids_by_asin.get(book.asin.upper())):
            return found
        variants = title_variants(book.title, book.subtitle)
        authors = {k for a in book.authors if (k := author_key(a))}
        for item_variants, item_authors, abs_id in self.entries:
            if variants & item_variants and (
                not authors or not item_authors or item_authors & authors
            ):
                return abs_id
        for item_variants, item_authors, abs_id in self.entries:
            if item_authors & authors and any(
                _fuzzy_same(v, c) for v in variants for c in item_variants
            ):
                return abs_id
        return None

    def owns(self, book: Audiobook) -> bool:
        if not self.size:
            return False
        if book.asin and book.asin.upper() in self.asins:
            return True
        variants = title_variants(book.title, book.subtitle)
        authors = {k for a in book.authors if (k := author_key(a))}
        for v in variants:
            owners = self.by_title.get(v)
            if owners is not None and (not authors or not owners or owners & authors):
                return True
        for a in authors:
            for candidate in self.by_author.get(a, ()):
                if any(_fuzzy_same(v, candidate) for v in variants):
                    return True
        return False


_DIGITS = re.compile(r"\d+")


def _fuzzy_same(a: str, b: str) -> bool:
    """Near-identical titles with the same numbers.

    Without the number check "primal hunter" and "primal hunter 2" score 93
    and book 1 of every series would count as owned once book 2 is.
    """
    if _DIGITS.findall(a) != _DIGITS.findall(b):
        return False
    return fuzz.ratio(a, b) >= FUZZY_THRESHOLD


_index: LibraryIndex | None = None
_index_library_id: str | None = None


def invalidate_library_index() -> None:
    global _index
    _index = None


def get_library_index(session: Session) -> LibraryIndex:
    """Index of the configured library; empty when ABS is not configured."""
    global _index, _index_library_id
    library_id = abs_config.get_library_id(session)
    if not library_id or not abs_config.get_check_downloaded(session):
        return LibraryIndex()
    if _index is not None and _index_library_id == library_id:
        return _index
    index = LibraryIndex()
    for item in session.exec(
        select(LibraryItem).where(LibraryItem.library_id == library_id)
    ).all():
        index.add(item)
    _index, _index_library_id = index, library_id
    return index


def mark_owned(session: Session, books: list[Audiobook]) -> list[Audiobook]:
    """Set `fulfilled` on books found in the library. Returns the owned ones.

    Does not commit; callers decide whether the books are persisted.
    """
    index = get_library_index(session)
    owned: list[Audiobook] = []
    for book in books:
        if index.owns(book):
            book.fulfilled = True
            owned.append(book)
    return owned


# ---------------------------------------------------------------------------
# Libraries a request can be for
# ---------------------------------------------------------------------------


def library_choices(session: Session) -> list[tuple[str, str]]:
    """(id, name) of the book libraries, the configured one first."""
    default = abs_config.get_library_id(session)
    if not default:
        return []
    libraries = abs_config.get_libraries(session)
    names = {lib["id"]: lib["name"] for lib in libraries}
    choices = [(default, names.get(default, "Default"))]
    choices += [(lib["id"], lib["name"]) for lib in libraries if lib["id"] != default]
    return choices


def resolve_request_library(session: Session, library_id: str | None) -> str | None:
    """A request's library: None for the configured one or anything unknown."""
    if not library_id or library_id == abs_config.get_library_id(session):
        return None
    if library_id in {lib_id for lib_id, _ in library_choices(session)}:
        return library_id
    return None


def update_library_moves(session: Session) -> int:
    """Flag requests whose book arrived in the configured library but is for another.

    Sets `move_abs_id` to the matching item, or clears it once the item is gone
    (moved). Returns how many requests are flagged.
    """
    index = get_library_index(session)
    flagged = 0
    for req in session.exec(
        select(AudiobookRequest).where(col(AudiobookRequest.library_id).is_not(None))
    ).all():
        abs_id = index.find(req.audiobook) if req.audiobook else None
        if req.move_abs_id != abs_id:
            req.move_abs_id = abs_id
            session.add(req)
        if abs_id:
            flagged += 1
    session.commit()
    return flagged


class _MinLibrary(BaseModel):
    id: str
    name: str | None = None
    mediaType: str = "book"


class _LibrariesPage(BaseModel):
    libraries: list[_MinLibrary] = []


async def _fetch_libraries(
    session: Session, client_session: ClientSession, base_url: str
) -> list[dict[str, str]]:
    token = abs_config.get_api_token(session)
    headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
    async with client_session.get(
        posixpath.join(base_url, "api/libraries"), headers=headers
    ) as resp:
        if not resp.ok:
            raise LibrarySyncError(
                f"Audiobookshelf returned {resp.status} listing libraries"
            )
        data = _LibrariesPage.model_validate(await resp.json())
    return [
        {"id": lib.id, "name": lib.name or lib.id}
        for lib in data.libraries
        if lib.mediaType == "book"
    ]


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


class _MinMetadata(BaseModel):
    title: str | None = None
    subtitle: str | None = None
    authorName: str | None = None
    narratorName: str | None = None
    seriesName: str | None = None
    asin: str | None = None


class _MinMedia(BaseModel):
    duration: float | None = None
    metadata: _MinMetadata


class _MinItem(BaseModel):
    id: str
    addedAt: int | None = None
    mediaType: str = "book"
    media: _MinMedia


class _ItemsPage(BaseModel):
    results: list[_MinItem] = []
    total: int | None = None
    mediaType: str | None = None


@dataclass
class SyncResult:
    total: int = 0
    added: int = 0
    removed: int = 0
    newly_owned: int = 0
    unflagged: int = 0


class LibrarySyncError(RuntimeError):
    pass


def _to_library_item(
    item: _MinItem, library_id: str, now: datetime
) -> LibraryItem | None:
    meta = item.media.metadata
    if not meta.title:
        return None
    return LibraryItem(
        abs_id=item.id,
        library_id=library_id,
        asin=meta.asin.strip().upper() if meta.asin and meta.asin.strip() else None,
        title=meta.title,
        subtitle=meta.subtitle,
        norm_title=core_title(meta.title),
        authors=split_names(meta.authorName),
        narrators=split_names(meta.narratorName),
        series=parse_series(meta.seriesName),
        duration_min=round((item.media.duration or 0) / 60),
        added_at=(
            datetime.fromtimestamp(item.addedAt / 1000, UTC) if item.addedAt else None
        ),
        synced_at=now,
    )


async def _fetch_all_items(
    session: Session, client_session: ClientSession, base_url: str, library_id: str
) -> list[_MinItem]:
    token = abs_config.get_api_token(session)
    headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
    url = posixpath.join(base_url, f"api/libraries/{library_id}/items")
    items: list[_MinItem] = []
    page = 0
    while True:
        params = {"limit": str(PAGE_SIZE), "page": str(page), "minified": "1"}
        async with client_session.get(url, headers=headers, params=params) as resp:
            if not resp.ok:
                raise LibrarySyncError(
                    f"Audiobookshelf returned {resp.status} listing library items"
                )
            data = _ItemsPage.model_validate(await resp.json())
        if data.mediaType == "podcast":
            raise LibrarySyncError(
                "Selected Audiobookshelf library is a podcast library"
            )
        items.extend(i for i in data.results if i.mediaType == "book")
        page += 1
        if len(data.results) < PAGE_SIZE:
            break
        if data.total is not None and page * PAGE_SIZE >= data.total:
            break
    return items


async def sync_library(session: Session, client_session: ClientSession) -> SyncResult:
    """Mirror the configured ABS library into `libraryitem` and flag owned books.

    The table is only modified after every page was fetched successfully, so a
    failed or partial fetch never wipes the local copy.
    """
    base_url = abs_config.get_base_url(session)
    library_id = abs_config.get_library_id(session)
    if not base_url or not library_id or not abs_config.get_api_token(session):
        raise LibrarySyncError("Audiobookshelf is not fully configured")

    fetched = await _fetch_all_items(session, client_session, base_url, library_id)
    try:
        abs_config.set_libraries(
            session, await _fetch_libraries(session, client_session, base_url)
        )
    except Exception as e:  # the library list is a convenience; never fail the sync
        logger.warning("ABS: could not list libraries", error=str(e))
    now = datetime.now(UTC)
    incoming = {
        li.abs_id: li
        for item in fetched
        if (li := _to_library_item(item, library_id, now)) is not None
    }

    existing = {
        li.abs_id: li
        for li in session.exec(
            select(LibraryItem).where(LibraryItem.library_id == library_id)
        ).all()
    }
    result = SyncResult(total=len(incoming))
    for abs_id, li in incoming.items():
        current = existing.get(abs_id)
        if current is None:
            session.add(li)
            result.added += 1
            continue
        for attr in (
            "asin",
            "title",
            "subtitle",
            "norm_title",
            "authors",
            "narrators",
            "series",
            "duration_min",
            "added_at",
            "synced_at",
        ):
            setattr(current, attr, getattr(li, attr))

    stale = [abs_id for abs_id in existing if abs_id not in incoming]
    if stale:
        session.exec(delete(LibraryItem).where(col(LibraryItem.abs_id).in_(stale)))
        result.removed = len(stale)
    # rows from a previously selected library are no longer relevant
    session.exec(delete(LibraryItem).where(col(LibraryItem.library_id) != library_id))
    session.commit()
    invalidate_library_index()

    unflagged = list(
        session.exec(select(Audiobook).where(~col(Audiobook.fulfilled))).all()
    )
    result.newly_owned = len(mark_owned(session, unflagged))

    # A fulfilled book nobody requested can only have been flagged by library
    # matching, so re-evaluate it: clears books removed from Audiobookshelf and
    # any earlier false match. Requested books keep their download state.
    index = get_library_index(session)
    requested = select(col(AudiobookRequest.asin)).distinct()
    for book in session.exec(
        select(Audiobook).where(
            col(Audiobook.fulfilled), col(Audiobook.asin).not_in(requested)
        )
    ).all():
        if not index.owns(book):
            book.fulfilled = False
            result.unflagged += 1
    session.commit()
    to_move = update_library_moves(session)

    logger.info(
        "ABS: library sync complete",
        total=result.total,
        added=result.added,
        removed=result.removed,
        newly_owned=result.newly_owned,
        unflagged=result.unflagged,
        to_move=to_move,
    )
    return result


def library_sync_status(session: Session) -> tuple[int, datetime | None]:
    library_id = abs_config.get_library_id(session)
    if not library_id:
        return 0, None
    rows = session.exec(
        select(LibraryItem.synced_at).where(LibraryItem.library_id == library_id)
    ).all()
    return len(rows), max(rows) if rows else None
