from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.internal.audible.details import BookDetails, BookSeries
from app.internal.audible.series import ResolvedSeries, SeriesEntry
from app.internal.audible.types import AudibleProduct
from app.internal.models import Audiobook
from app.routers.pages import book as book_page
from tests.constants import ASIN


def _book(asin: str, title: str) -> Audiobook:
    return Audiobook(
        asin=asin,
        title=title,
        subtitle=None,
        authors=["Mock Author"],
        narrators=["Mock Narrator"],
        cover_image="https://example.invalid/cover.jpg",
        release_date=datetime(2020, 1, 1, tzinfo=UTC),
        runtime_length_min=600,
    )


@pytest.fixture
def audible(monkeypatch: pytest.MonkeyPatch) -> None:
    async def details(*_: object) -> BookDetails:
        return BookDetails(
            product=AudibleProduct(
                asin="B0NEWBOOK",
                title="Brand New Book",
                release_date="2024-05-01",
                runtime_length_min=480,
                authors=[AudibleProduct._Author(name="Mock Author")],  # pyright: ignore[reportPrivateUsage]
            ),
            summary_html="<p>An <b>exciting</b> story.</p><script>alert(1)</script>",
            publisher="Mock Press",
            language="english",
            format_type="unabridged",
            series=[BookSeries(asin="SERIES1", title="Mock Saga", sequence="2")],
        )

    async def series(*_: object) -> ResolvedSeries:
        return ResolvedSeries(
            key="mock saga",
            name="Mock Saga",
            asin="SERIES1",
            entries=[
                SeriesEntry(
                    position=1.0, sequence="1", book=_book("B0SAGA1", "Saga One")
                ),
                SeriesEntry(position=2.0, sequence="2", book=_book(ASIN, "Test Book")),
            ],
        )

    async def similar(*args: object, **kwargs: object) -> list[Audiobook]:
        del args, kwargs
        return []

    monkeypatch.setattr(book_page, "get_book_details", details)
    monkeypatch.setattr(book_page, "series_by_asin", series)
    monkeypatch.setattr(book_page, "list_similar_audible_books", similar)


def test_book_page_renders(admin_client: TestClient, audible: None) -> None:
    _ = audible
    page = admin_client.get(f"/book/{ASIN}")
    assert page.status_code == 200, page.text
    assert "Test Book - AudioBookRequest" in page.text
    assert "Mock Saga" in page.text
    assert "More in Mock Saga" in page.text
    assert "Saga One" in page.text
    assert "This book" in page.text
    # summary HTML from Audible is stripped, never rendered
    assert "exciting" in page.text
    assert "<script>alert(1)</script>" not in page.text
    assert "<b>exciting</b>" not in page.text


def test_book_page_fetches_unknown_book(
    admin_client: TestClient, audible: None, db: Session
) -> None:
    _ = audible
    page = admin_client.get("/book/B0NEWBOOK")
    assert page.status_code == 200, page.text
    assert "Brand New Book" in page.text
    assert db.get(Audiobook, "B0NEWBOOK") is not None


def test_book_page_404_when_audible_has_nothing(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def empty(*_: object) -> BookDetails:
        return BookDetails()

    monkeypatch.setattr(book_page, "get_book_details", empty)
    assert admin_client.get("/book/B0MISSING").status_code == 404


def test_detail_request_button_variant(admin_client: TestClient, db: Session) -> None:
    db.add(_book("B0REQ", "Request Me"))
    db.commit()
    response = admin_client.post(
        "/request/hx-add/B0REQ", data={"region": "us", "variant": "detail"}
    )
    assert response.status_code == 200, response.text
    assert "book-request" in response.text
    assert "Requested" in response.text
    assert "book-card" not in response.text

    card = admin_client.post("/request/hx-add/" + ASIN, data={"region": "us"})
    assert card.status_code == 200, card.text
    assert "book-card" in card.text
