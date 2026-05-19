"""Integration tests for POST /api/upload.

We skip the FastAPI lifespan (it touches structlog globals and cached clients)
and drive the ASGI app directly through ``httpx.ASGITransport`` with
dependency overrides for the three services.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.core.errors import SheetsAppendError, VisionExtractionError
from app.deps import get_files_service, get_sheets_service, get_vision_service
from app.main import app as real_app
from app.models import DownloadedFile, UPDRecord


@asynccontextmanager
async def _noop_lifespan(_):
    yield


@pytest.fixture
def app():
    """App with lifespan disabled — tests don't want structlog reconfig."""
    real_app.router.lifespan_context = _noop_lifespan  # type: ignore[attr-defined]
    yield real_app
    real_app.dependency_overrides.clear()


@pytest.fixture
def fake_files():
    svc = MagicMock()
    svc.to_png = AsyncMock(
        return_value=DownloadedFile(
            data=b"\x89PNG-fake",
            media_type="image/png",
            original_name="upd.png",
            page_count=None,
        )
    )
    return svc


@pytest.fixture
def fake_vision():
    svc = MagicMock()
    svc.extract = AsyncMock(
        return_value=UPDRecord(
            organization="Гринлайн",
            counterparty='ООО "Озеленский"',
            date=date(2026, 4, 22),
            amount=10500.0,
            upd_number="6022635717",
        )
    )
    return svc


@pytest.fixture
def fake_sheets():
    svc = MagicMock()
    svc.append_row = AsyncMock(return_value=None)
    return svc


@pytest.fixture
def client(app, fake_files, fake_vision, fake_sheets):
    app.dependency_overrides[get_files_service] = lambda: fake_files
    app.dependency_overrides[get_vision_service] = lambda: fake_vision
    app.dependency_overrides[get_sheets_service] = lambda: fake_sheets
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _png(name: str = "upd.png", data: bytes = b"\x89PNG\r\n\x1a\nfake-image-bytes"):
    """A single multipart file tuple under the ``files`` field name."""
    return ("files", (name, data, "image/png"))


def _pdf(name: str = "upd.pdf", data: bytes = b"%PDF-1.4\nfake"):
    return ("files", (name, data, "application/pdf"))


@pytest.mark.asyncio
async def test_happy_path_image(client, fake_files, fake_vision, fake_sheets):
    async with client as c:
        resp = await c.post(
            "/api/upload",
            files=[_png()],
            data={"foreman": "Юра"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["ok"] is True
    assert item["needs_review"] is False
    assert item["filename"] == "upd.png"
    assert item["sheet_url"].startswith("https://docs.google.com/spreadsheets/")
    assert item["record"]["organization"] == "Гринлайн"
    assert item["record"]["counterparty"] == 'ООО "Озеленский"'

    fake_files.to_png.assert_awaited_once()
    fake_vision.extract.assert_awaited_once()
    fake_sheets.append_row.assert_awaited_once()
    call = fake_sheets.append_row.await_args
    assert call.kwargs["foreman"] == "Юра"
    # Per-item correlation_id is "<batch>-<index>"
    assert call.kwargs["correlation_id"] == item["correlation_id"]
    assert item["correlation_id"].startswith(body["correlation_id"])


@pytest.mark.asyncio
async def test_happy_path_pdf(client, fake_sheets):
    async with client as c:
        resp = await c.post(
            "/api/upload",
            files=[_pdf()],
            data={"foreman": "Гриша"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["items"][0]["ok"] is True
    fake_sheets.append_row.assert_awaited_once()


@pytest.mark.asyncio
async def test_needs_review_skips_sheets(client, fake_vision, fake_sheets):
    fake_vision.extract = AsyncMock(
        return_value=UPDRecord(
            organization="Гринлайн",
            counterparty='ООО "Test"',
            date=date(2026, 4, 22),
            amount=None,  # missing → needs_review
            upd_number="123",
        )
    )
    async with client as c:
        resp = await c.post(
            "/api/upload",
            files=[_png()],
            data={"foreman": "Боря"},
        )
    assert resp.status_code == 200
    body = resp.json()
    # Batch-level ok is False when any item needs review.
    assert body["ok"] is False
    item = body["items"][0]
    assert item["ok"] is True
    assert item["needs_review"] is True
    assert "amount" in item["missing_fields"]
    assert item["sheet_url"] is None
    fake_sheets.append_row.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_foreman(client):
    async with client as c:
        resp = await c.post(
            "/api/upload",
            files=[_png()],
            data={"foreman": "   "},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_custom_foreman_name_accepted(client, fake_sheets):
    async with client as c:
        resp = await c.post(
            "/api/upload",
            files=[_png()],
            data={"foreman": "Вася"},
        )
    assert resp.status_code == 200
    assert resp.json()["items"][0]["ok"] is True
    call = fake_sheets.append_row.await_args
    assert call.kwargs["foreman"] == "Вася"


@pytest.mark.asyncio
async def test_missing_file(client):
    async with client as c:
        resp = await c.post("/api/upload", data={"foreman": "Юра"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_unsupported_content_type_per_item(client, fake_sheets):
    async with client as c:
        resp = await c.post(
            "/api/upload",
            files=[("files", ("note.txt", b"hello", "text/plain"))],
            data={"foreman": "Юра"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    item = body["items"][0]
    assert item["ok"] is False
    assert item["error"] == "unsupported_file_type"
    assert item["filename"] == "note.txt"
    fake_sheets.append_row.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_file_per_item(client, fake_sheets):
    async with client as c:
        resp = await c.post(
            "/api/upload",
            files=[("files", ("empty.png", b"", "image/png"))],
            data={"foreman": "Юра"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    item = body["items"][0]
    assert item["error"] == "empty_file"
    fake_sheets.append_row.assert_not_awaited()


@pytest.mark.asyncio
async def test_too_large_file_per_item(client, monkeypatch, fake_sheets):
    from app.config import get_settings as _gs

    settings = _gs()
    monkeypatch.setattr(settings, "max_upload_bytes", 16, raising=False)

    async with client as c:
        resp = await c.post(
            "/api/upload",
            files=[("files", ("upd.png", b"x" * 64, "image/png"))],
            data={"foreman": "Юра"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    item = body["items"][0]
    assert item["error"] == "file_too_large"
    fake_sheets.append_row.assert_not_awaited()


@pytest.mark.asyncio
async def test_too_many_files_returns_413(client, monkeypatch):
    from app.config import get_settings as _gs

    settings = _gs()
    monkeypatch.setattr(settings, "max_batch_files", 2, raising=False)

    async with client as c:
        resp = await c.post(
            "/api/upload",
            files=[_png("a.png"), _png("b.png"), _png("c.png")],
            data={"foreman": "Юра"},
        )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_vision_failure(client, fake_vision, fake_sheets):
    fake_vision.extract = AsyncMock(side_effect=VisionExtractionError("boom"))
    async with client as c:
        resp = await c.post(
            "/api/upload",
            files=[_png()],
            data={"foreman": "Юра"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    item = body["items"][0]
    assert item["ok"] is False
    assert item["error"] == "vision_extraction_error"
    fake_sheets.append_row.assert_not_awaited()


@pytest.mark.asyncio
async def test_sheets_failure(client, fake_sheets):
    fake_sheets.append_row = AsyncMock(side_effect=SheetsAppendError("api 503"))
    async with client as c:
        resp = await c.post(
            "/api/upload",
            files=[_png()],
            data={"foreman": "Юра"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    item = body["items"][0]
    assert item["ok"] is False
    assert item["error"] == "sheets_append_error"


# --- Batch-specific scenarios ----------------------------------------------


@pytest.mark.asyncio
async def test_batch_two_files_both_succeed(client, fake_files, fake_vision, fake_sheets):
    async with client as c:
        resp = await c.post(
            "/api/upload",
            files=[_png("a.png"), _pdf("b.pdf")],
            data={"foreman": "Юра"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert len(body["items"]) == 2
    assert [it["filename"] for it in body["items"]] == ["a.png", "b.pdf"]
    assert all(it["ok"] and not it["needs_review"] for it in body["items"])
    # Sequential pipeline: one append per valid file.
    assert fake_files.to_png.await_count == 2
    assert fake_vision.extract.await_count == 2
    assert fake_sheets.append_row.await_count == 2
    # Per-item correlation ids preserve batch order.
    assert body["items"][0]["correlation_id"].endswith("-0")
    assert body["items"][1]["correlation_id"].endswith("-1")


@pytest.mark.asyncio
async def test_batch_mixed_valid_and_invalid(client, fake_sheets):
    async with client as c:
        resp = await c.post(
            "/api/upload",
            files=[
                _png("good.png"),
                ("files", ("note.txt", b"hello", "text/plain")),
            ],
            data={"foreman": "Гриша"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert len(body["items"]) == 2
    assert body["items"][0]["ok"] is True
    assert body["items"][0]["filename"] == "good.png"
    assert body["items"][1]["ok"] is False
    assert body["items"][1]["error"] == "unsupported_file_type"
    assert body["items"][1]["filename"] == "note.txt"
    # The invalid file does NOT block the valid one from being appended.
    fake_sheets.append_row.assert_awaited_once()
