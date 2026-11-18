from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.internal.audiobookshelf.library import (
    LibraryIndex,
    author_key,
    core_title,
    parse_series,
    split_names,
)
from app.internal.models import Audiobook, LibraryItem
from tests.constants import ASIN
from tests.http import json_object
from tests.mock_services import MockServer


def _book(
    title: str,
    authors: list[str],
    asin: str = "B0NOTOWNED",
    subtitle: str | None = None,
) -> Audiobook:
    return Audiobook(
        asin=asin,
        title=title,
        subtitle=subtitle,
        authors=authors,
        narrators=[],
        cover_image=None,
        release_date=datetime(2020, 1, 1, tzinfo=UTC),
        runtime_length_min=60,
    )


def _item(
    abs_id: str,
    title: str,
    authors: list[str],
    asin: str | None = None,
    subtitle: str | None = None,
) -> LibraryItem:
    return LibraryItem(
        abs_id=abs_id,
        library_id="lib-1",
        asin=asin,
        title=title,
        subtitle=subtitle,
        norm_title=core_title(title),
        authors=authors,
    )


def _abs_item(
    abs_id: str,
    title: str,
    author: str,
    asin: str | None = None,
    series: str | None = None,
) -> dict[str, object]:
    return {
        "id": abs_id,
        "mediaType": "book",
        "addedAt": 1_700_000_000_000,
        "media": {
            "duration": 7200,
            "metadata": {
                "title": title,
                "subtitle": None,
                "authorName": author,
                "narratorName": "Some Narrator",
                "seriesName": series,
                "asin": asin,
            },
        },
    }


# -- normalisation -----------------------------------------------------------
def test_core_title_strips_series_noise() -> None:
    assert core_title("The Way of Kings (Stormlight Archive, Book 1)") == "way of kings"
    assert (
        core_title("Mistborn: The Final Empire, Book 1") == "mistborn the final empire"
    )
    assert core_title("Project Hail Mary [Unabridged]") == "project hail mary"
    assert core_title("Café Society & Friends") == "cafe society and friends"


def test_author_key_handles_name_order() -> None:
    assert author_key("Brandon Sanderson") == "sanderson"
    assert author_key("Sanderson, Brandon") == "sanderson"
    assert author_key("") == ""


def test_split_names_and_series() -> None:
    assert split_names("Terry Pratchett, Neil Gaiman") == [
        "Terry Pratchett",
        "Neil Gaiman",
    ]
    assert split_names("A & B") == ["A", "B"]
    assert split_names(None) == []
    assert parse_series("Stormlight Archive #1, Cosmere #4.5") == [
        {"name": "Stormlight Archive", "sequence": "1"},
        {"name": "Cosmere", "sequence": "4.5"},
    ]
    assert parse_series("Standalone Series") == [
        {"name": "Standalone Series", "sequence": None}
    ]


# -- index matching ----------------------------------------------------------
def _index(*items: LibraryItem) -> LibraryIndex:
    index = LibraryIndex()
    for item in items:
        index.add(item)
    return index


def test_index_matches_by_asin() -> None:
    index = _index(_item("1", "Totally Different", ["Nobody"], asin="B0OWNED"))
    assert index.owns(_book("Anything", ["Anyone"], asin="b0owned"))


def test_index_matches_title_with_series_suffix() -> None:
    index = _index(_item("1", "The Way of Kings", ["Brandon Sanderson"]))
    assert index.owns(
        _book("The Way of Kings (Stormlight Archive, Book 1)", ["Brandon Sanderson"])
    )


def test_index_matches_title_plus_subtitle() -> None:
    index = _index(_item("1", "Mistborn: The Final Empire", ["Brandon Sanderson"]))
    assert index.owns(
        _book("Mistborn", ["Brandon Sanderson"], subtitle="The Final Empire")
    )


def test_index_fuzzy_match_requires_shared_author() -> None:
    index = _index(
        _item("1", "Harry Potter and the Philosophers Stone", ["J.K. Rowling"])
    )
    assert index.owns(
        _book("Harry Potter and the Philosopher's Stone", ["J. K. Rowling"])
    )
    assert not index.owns(
        _book("Harry Potter and the Philosopher's Stone", ["Someone Else"])
    )


def test_index_rejects_same_title_different_author() -> None:
    index = _index(_item("1", "Legacy", ["Author One"]))
    assert not index.owns(_book("Legacy", ["Author Two"]))


def test_empty_index_owns_nothing() -> None:
    assert not LibraryIndex().owns(_book("Anything", ["Anyone"]))


# -- sync --------------------------------------------------------------------
def test_sync_requires_configuration(admin_client: TestClient) -> None:
    response = admin_client.post("/api/settings/audiobookshelf/sync")
    assert response.status_code == 400, response.text


def test_sync_copies_library_and_marks_owned(
    admin_client: TestClient, abs_service: MockServer, db: Session
) -> None:
    db.add(_book("Words of Radiance", ["Brandon Sanderson"], asin="B0WOR"))
    db.commit()
    abs_service.state["abs_items"] = [
        _abs_item("i1", "Test Book", "Mock Author", asin=ASIN),
        _abs_item(
            "i2",
            "Words of Radiance",
            "Brandon Sanderson",
            series="The Stormlight Archive #2",
        ),
        _abs_item("i3", "No Title Match Here", "Nobody"),
    ]

    response = admin_client.post("/api/settings/audiobookshelf/sync")
    assert response.status_code == 200, response.text
    body = json_object(response)
    assert body["total"] == 3
    assert body["added"] == 3
    assert body["newly_owned"] == 2

    db.expire_all()
    owned = {
        b.asin for b in db.exec(select(Audiobook).where(Audiobook.fulfilled)).all()
    }
    assert {ASIN, "B0WOR"} <= owned

    stored = db.get(LibraryItem, "i2")
    assert stored is not None
    assert stored.series == [{"name": "The Stormlight Archive", "sequence": "2"}]
    assert stored.duration_min == 120

    status = json_object(admin_client.get("/api/settings/audiobookshelf/sync"))
    assert status["item_count"] == 3
    assert status["last_synced"] is not None


def test_resync_removes_deleted_items(
    admin_client: TestClient, abs_service: MockServer, db: Session
) -> None:
    abs_service.state["abs_items"] = [
        _abs_item("i1", "Book One", "A Author"),
        _abs_item("i2", "Book Two", "A Author"),
    ]
    assert admin_client.post("/api/settings/audiobookshelf/sync").status_code == 200

    abs_service.state["abs_items"] = [_abs_item("i2", "Book Two Renamed", "A Author")]
    body = json_object(admin_client.post("/api/settings/audiobookshelf/sync"))
    assert body == {"total": 1, "added": 0, "removed": 1, "newly_owned": 0}

    db.expire_all()
    remaining = db.exec(select(LibraryItem)).all()
    assert [(li.abs_id, li.title) for li in remaining] == [("i2", "Book Two Renamed")]


def test_settings_page_sync_button(
    admin_client: TestClient, abs_service: MockServer
) -> None:
    abs_service.state["abs_items"] = [_abs_item("i1", "Book One", "A Author")]
    page = admin_client.get("/settings/audiobookshelf")
    assert page.status_code == 200
    assert "Not synced yet." in page.text

    response = admin_client.post("/settings/audiobookshelf/hx-sync")
    assert response.status_code == 200, response.text
    assert "Synced 1 books" in response.text

    page = admin_client.get("/settings/audiobookshelf")
    assert "1 books synced" in page.text
