from datetime import UTC, datetime

import pytest
from aiohttp import ClientSession
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.internal.audible import series as series_mod
from app.internal.audible.series import (
    OwnedSeries,
    ResolvedSeries,
    SeriesEntry,
    bundle_positions,
    owned_series,
    parse_position,
    resolve_series,
    series_key,
)
from app.internal.audiobookshelf.library import core_title
from app.internal.models import Audiobook, LibraryItem
from tests.http import as_list, json_object
from tests.mock_services import MockServer

REGION = "us"


def _book(asin: str, title: str, year: int = 2020, runtime: int = 600) -> Audiobook:
    return Audiobook(
        asin=asin,
        title=title,
        subtitle=None,
        authors=["Zogarth"],
        narrators=["Travis Baldree"],
        cover_image=None,
        release_date=datetime(year, 1, 1, tzinfo=UTC),
        runtime_length_min=runtime,
    )


def _library(db: Session, *books: tuple[str, str, str]) -> None:
    for abs_id, title, seq in books:
        db.add(
            LibraryItem(
                abs_id=abs_id,
                library_id="lib-1",
                title=title,
                norm_title=core_title(title),
                authors=["Zogarth"],
                series=[{"name": "The Primal Hunter", "sequence": seq}],
                added_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
    db.commit()


# -- parsing -----------------------------------------------------------------
def test_series_key_ignores_noise() -> None:
    assert series_key("The Solo Leveling Series") == series_key("Solo Leveling")
    assert series_key("The Primal Hunter") == "primal hunter"


def test_parse_position() -> None:
    assert parse_position("4") == 4.0
    assert parse_position("4.5") == 4.5
    assert parse_position("1-3") == 1.0
    assert parse_position(None) is None
    assert parse_position("prequel") is None


def test_bundle_positions() -> None:
    assert bundle_positions("Publisher's Pack 2: Book 3, 4") == {3.0, 4.0}
    assert bundle_positions("Books 1-3") == {1.0, 2.0, 3.0}
    assert bundle_positions("Books 4 & 5") == {4.0, 5.0}
    assert bundle_positions("Book 7") == set()


def test_owned_series_groups_library(abs_service: MockServer, db: Session) -> None:
    _ = abs_service
    _library(db, ("a", "The Primal Hunter 2", "2"), ("b", "The Primal Hunter 4", "4"))
    [owned] = owned_series(db)
    assert owned.key == "primal hunter"
    assert owned.positions == {2.0, 4.0}
    assert owned.max_position == 4.0


# -- resolution --------------------------------------------------------------
@pytest.mark.anyio
async def test_resolve_series_drops_placeholders_and_dupes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    series_mod.clear_series_cache()

    async def find(*_: object) -> tuple[str, str]:
        return "SERIES1", "The Primal Hunter"

    async def children(*_: object) -> list[series_mod._Relationship]:  # pyright: ignore[reportPrivateUsage]
        rel = series_mod._Relationship  # pyright: ignore[reportPrivateUsage]
        return [
            rel(
                asin="B1",
                relationship_to_product="child",
                relationship_type="series",
                sequence="1",
            ),
            rel(
                asin="B12a",
                relationship_to_product="child",
                relationship_type="series",
                sequence="12",
            ),
            rel(
                asin="B12b",
                relationship_to_product="child",
                relationship_type="series",
                sequence="12",
            ),
            rel(
                asin="B15",
                relationship_to_product="child",
                relationship_type="series",
                sequence="15",
            ),
        ]

    async def products(*_: object) -> dict[str, series_mod._ProductWithSeries]:  # pyright: ignore[reportPrivateUsage]
        def p(asin: str, date: str, runtime: int) -> series_mod._ProductWithSeries:  # pyright: ignore[reportPrivateUsage]
            return series_mod._ProductWithSeries(  # pyright: ignore[reportPrivateUsage]
                asin=asin,
                title=f"Book {asin}",
                release_date=date,
                runtime_length_min=runtime,
            )

        return {
            "B1": p("B1", "2022-03-08", 1209),
            "B12a": p("B12a", "2200-01-01", 0),  # placeholder
            "B12b": p("B12b", "2025-06-11", 1207),
            "B15": p("B15", "2200-01-01", 0),  # placeholder only
        }

    monkeypatch.setattr(series_mod, "_find_series_asin", find)
    monkeypatch.setattr(series_mod, "_series_children", children)
    monkeypatch.setattr(series_mod, "_fetch_products", products)

    owned = OwnedSeries(
        key="primal hunter", name="The Primal Hunter", authors=["Zogarth"]
    )
    async with ClientSession() as cs:
        resolved = await resolve_series(cs, owned, REGION)
    assert resolved is not None
    assert [(e.position, e.book.asin) for e in resolved.entries] == [
        (1.0, "B1"),
        (12.0, "B12b"),
    ]


# -- rows --------------------------------------------------------------------
def _seed_resolved() -> None:
    entries = [
        SeriesEntry(
            position=float(n),
            sequence=str(n),
            book=_book(f"BPH{n}", f"The Primal Hunter {n}"),
        )
        for n in range(1, 7)
    ]
    entries.append(
        SeriesEntry(
            position=7.0,
            sequence="7",
            book=_book("BPH7", "The Primal Hunter 7", year=2999),
        )
    )
    series_mod._series_cache[(REGION, "primal hunter")] = series_mod._CacheEntry(  # pyright: ignore[reportPrivateUsage]
        ResolvedSeries(
            key="primal hunter", name="The Primal Hunter", asin="S", entries=entries
        ),
        datetime.now(UTC).timestamp(),
    )


def test_series_rows_next_and_missing(
    admin_client: TestClient, abs_service: MockServer, db: Session
) -> None:
    _ = abs_service
    series_mod.clear_series_cache()
    # owns 2, 4 and 5 -> missing 1 and 3, next is 6 (7 is unreleased)
    _library(
        db,
        ("a", "The Primal Hunter 2", "2"),
        ("b", "The Primal Hunter 4", "4"),
        ("c", "The Primal Hunter 5", "5"),
    )
    _seed_resolved()

    body = json_object(
        admin_client.get(f"/api/recommendations/series?audible_region={REGION}")
    )
    next_up = [json_object_book(r) for r in as_list(body["next_up"])]
    missing = [json_object_book(r) for r in as_list(body["missing"])]
    assert next_up == ["BPH6"]
    assert missing == ["BPH1", "BPH3"]

    # suggested books are stored so they can be requested
    assert db.get(Audiobook, "BPH6") is not None


def json_object_book(row: object) -> str:
    assert isinstance(row, dict)
    book = row["book"]  # pyright: ignore[reportUnknownVariableType]
    assert isinstance(book, dict)
    inner = book["book"]  # pyright: ignore[reportUnknownVariableType]
    assert isinstance(inner, dict)
    asin = inner["asin"]  # pyright: ignore[reportUnknownVariableType]
    assert isinstance(asin, str)
    return asin


def test_series_rows_on_home_page(
    admin_client: TestClient, abs_service: MockServer, db: Session
) -> None:
    _ = abs_service
    home = admin_client.get("/")
    assert "/hx-series/next" not in home.text  # no series in library yet

    _library(db, ("a", "The Primal Hunter 2", "2"))
    series_mod.clear_series_cache()
    _seed_resolved()
    home = admin_client.get("/")
    assert "/hx-series/next" in home.text

    row = admin_client.get("/hx-series/missing")
    assert row.status_code == 200, row.text
    assert "Missing from your series" in row.text
    assert "The Primal Hunter 1" in row.text


def test_series_rows_empty_without_library(admin_client: TestClient) -> None:
    body = json_object(admin_client.get("/api/recommendations/series"))
    assert body == {"next_up": [], "missing": []}
