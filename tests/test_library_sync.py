from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.internal.audiobookshelf.config import abs_config
from app.internal.audiobookshelf.library import (
    LibraryIndex,
    author_key,
    core_title,
    library_choices,
    parse_series,
    split_names,
)
from app.internal.models import Audiobook, AudiobookRequest, LibraryItem
from tests.constants import ASIN, FULFILLED_ASIN
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
        core_title("Mistborn: The Final Empire, Book 1")
        == "mistborn the final empire 1"
    )
    assert core_title("Project Hail Mary [Unabridged]") == "project hail mary"
    assert core_title("Café Society & Friends") == "cafe society and friends"


def test_core_title_keeps_book_numbers() -> None:
    assert core_title("Defiance of the Fall, Book 10 (Unabridged)") == core_title(
        "Defiance of the Fall 10"
    )
    assert core_title("Defiance of the Fall, Book 10") != core_title(
        "Defiance of the Fall"
    )
    assert core_title("The Primal Hunter 3 (Unabridge") == "primal hunter 3"


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


def test_index_fuzzy_match_respects_book_numbers() -> None:
    index = _index(
        _item("1", "The Primal Hunter 2", ["Zogarth"]),
        _item("2", "Defiance of the Fall, Book 10", ["JF Brink"]),
    )
    assert not index.owns(_book("The Primal Hunter", ["Zogarth"]))
    assert not index.owns(_book("The Primal Hunter 3", ["Zogarth"]))
    assert index.owns(_book("Primal Hunter 2", ["Zogarth"]))
    assert index.owns(_book("Defiance of the Fall 10", ["JF Brink"]))
    assert not index.owns(_book("Defiance of the Fall", ["JF Brink"]))


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
    assert body == {
        "total": 1,
        "added": 0,
        "removed": 1,
        "newly_owned": 0,
        "unflagged": 0,
    }

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


def test_resync_unflags_unrequested_books_no_longer_in_library(
    admin_client: TestClient, abs_service: MockServer, db: Session
) -> None:
    db.add(_book("Gone Book", ["A Author"], asin="B0GONE"))
    db.commit()
    abs_service.state["abs_items"] = [_abs_item("i1", "Gone Book", "A Author")]
    first = json_object(admin_client.post("/api/settings/audiobookshelf/sync"))
    assert first["newly_owned"] == 1

    abs_service.state["abs_items"] = []
    second = json_object(admin_client.post("/api/settings/audiobookshelf/sync"))
    assert second["unflagged"] == 1
    db.expire_all()
    gone = db.get(Audiobook, "B0GONE")
    assert gone is not None and not gone.fulfilled
    # a requested + fulfilled book (a real download) is left alone
    kept = db.get(Audiobook, FULFILLED_ASIN)
    assert kept is not None and kept.fulfilled


# -- per-library requests (21 Nov 2026) -------------------------------------------------------------


def test_index_find_returns_the_matching_item() -> None:
    index = LibraryIndex()
    index.add(_item("a1", "Words of Radiance", ["Brandon Sanderson"], asin="B0WOR"))
    index.add(_item("a2", "The Tenant", ["Freida McFadden"]))
    assert index.find(_book("Anything", ["Nobody"], asin="B0WOR")) == "a1"
    assert index.find(_book("The Tenant (Unabridged)", ["Freida McFadden"])) == "a2"
    assert index.find(_book("The Tenant", ["Someone Else"])) is None
    assert index.find(_book("Not In Library", ["Freida McFadden"])) is None
    assert LibraryIndex().find(_book("The Tenant", ["Freida McFadden"])) is None


def test_sync_flags_and_clears_a_book_for_another_library(
    admin_client: TestClient, abs_service: MockServer, db: Session
) -> None:
    abs_service.state["abs_libraries"] = [
        {"id": "lib-1", "name": "Audiobooks", "mediaType": "book"},
        {"id": "lib-2", "name": "Main", "mediaType": "book"},
        {"id": "pod", "name": "Podcasts", "mediaType": "podcast"},
    ]
    db.add(_book("Nancy Book", ["Nancy Author"], asin="B0NANCY"))
    db.add(AudiobookRequest(asin="B0NANCY", user_username="root", library_id="lib-2"))
    db.commit()
    abs_service.state["abs_items"] = [_abs_item("n1", "Nancy Book", "Nancy Author")]

    assert admin_client.post("/api/settings/audiobookshelf/sync").status_code == 200
    db.expire_all()
    req = db.get(AudiobookRequest, ("B0NANCY", "root"))
    assert req is not None and req.move_abs_id == "n1"
    with Session(db.get_bind()) as s:
        assert abs_config.get_libraries(s) == [
            {"id": "lib-1", "name": "Audiobooks"},
            {"id": "lib-2", "name": "Main"},
        ]
        assert library_choices(s) == [("lib-1", "Audiobooks"), ("lib-2", "Main")]

    # once the media host has moved it, the item is gone from this library: flag cleared
    abs_service.state["abs_items"] = []
    assert admin_client.post("/api/settings/audiobookshelf/sync").status_code == 200
    db.expire_all()
    req = db.get(AudiobookRequest, ("B0NANCY", "root"))
    assert req is not None and req.move_abs_id is None


def test_request_uses_the_library_chosen_in_the_navbar(
    admin_client: TestClient, abs_service: MockServer, db: Session
) -> None:
    _ = abs_service
    abs_config.set_libraries(
        db, [{"id": "lib-1", "name": "Audiobooks"}, {"id": "lib-2", "name": "Main"}]
    )
    db.add(_book("For Main", ["A Author"], asin="B0FORMAIN"))
    db.add(_book("For Me", ["A Author"], asin="B0FORME"))
    db.add(_book("Bad Cookie", ["A Author"], asin="B0BADCOOKIE"))
    db.commit()

    page = admin_client.get("/wishlist")
    assert 'id="abr-library"' in page.text and ">Main</option>" in page.text

    admin_client.cookies.set("abr_library", "lib-2")
    assert (
        admin_client.post(
            "/request/hx-add/B0FORMAIN", data={"region": "us"}
        ).status_code
        == 200
    )
    admin_client.cookies.set("abr_library", "lib-1")
    assert (
        admin_client.post("/request/hx-add/B0FORME", data={"region": "us"}).status_code
        == 200
    )
    admin_client.cookies.set("abr_library", "not-a-library")
    assert (
        admin_client.post(
            "/request/hx-add/B0BADCOOKIE", data={"region": "us"}
        ).status_code
        == 200
    )
    admin_client.cookies.clear()

    db.expire_all()
    got = {
        r.asin: r.library_id
        for r in db.exec(
            select(AudiobookRequest).where(AudiobookRequest.user_username == "root")
        ).all()
        if r.asin in {"B0FORMAIN", "B0FORME", "B0BADCOOKIE"}
    }
    assert got == {"B0FORMAIN": "lib-2", "B0FORME": None, "B0BADCOOKIE": None}


def test_no_library_switch_with_one_library(
    admin_client: TestClient, abs_service: MockServer, db: Session
) -> None:
    _ = abs_service
    abs_config.set_libraries(db, [{"id": "lib-1", "name": "Audiobooks"}])
    assert 'id="abr-library"' not in admin_client.get("/wishlist").text
