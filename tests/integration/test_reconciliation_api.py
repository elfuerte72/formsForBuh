"""Integration tests for POST /api/reconciliation.

Drives the FastAPI app through ``httpx.ASGITransport`` with dependency
overrides for the parser + sheets services. The lifespan is replaced
because tests don't want structlog reconfiguration / network warmup.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.core.errors import OneCParseError, SheetsReadError
from app.deps import get_onec_parser_service, get_sheets_service
from app.main import app as real_app
from app.models import OneCRecord, SheetUPDRow


FIXTURE_XLS = Path(__file__).resolve().parents[1] / "fixtures" / "onec" / "sample.xls"


@asynccontextmanager
async def _noop_lifespan(_):
    yield


@pytest.fixture
def app():
    real_app.router.lifespan_context = _noop_lifespan  # type: ignore[attr-defined]
    yield real_app
    real_app.dependency_overrides.clear()


@pytest.fixture
def fake_onec():
    svc = MagicMock()
    svc.parse = MagicMock(
        return_value=[
            OneCRecord(
                upd_number="6022461056",
                date=date(2026, 2, 5),
                amount=21324.0,
                organization="СТРОИТЕЛЬНЫЙ ДВОР ООО",
                source_row=7,
            ),
            OneCRecord(
                upd_number="6022503412",
                date=date(2026, 2, 26),
                amount=5315.0,
                organization="СТРОИТЕЛЬНЫЙ ДВОР ООО",
                source_row=8,
            ),
        ]
    )
    return svc


@pytest.fixture
def fake_sheets():
    svc = MagicMock()
    svc.read_all_records = AsyncMock(
        return_value=[
            SheetUPDRow(
                upd_number="6022461056",
                organization="СТРОИТЕЛЬНЫЙ ДВОР ООО",
                date=date(2026, 2, 5),
                amount=21324.0,
                foreman="Юра",
                source_row=2,
            ),
        ]
    )
    return svc


@pytest.fixture
def client(app, fake_onec, fake_sheets):
    app.dependency_overrides[get_onec_parser_service] = lambda: fake_onec
    app.dependency_overrides[get_sheets_service] = lambda: fake_sheets
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _xls_payload() -> dict:
    return {
        "file": (
            "sample.xls",
            FIXTURE_XLS.read_bytes(),
            "application/vnd.ms-excel",
        )
    }


@pytest.mark.asyncio
async def test_accept_xls_returns_summary(client, fake_onec, fake_sheets):
    async with client as c:
        resp = await c.post("/api/reconciliation", files=_xls_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["stats"]["onec_total"] == 2
    assert body["stats"]["foreman_total"] == 1
    assert [m["upd_number"] for m in body["missing"]] == ["6022503412"]
    assert body["duplicates"] == []
    assert body["extras"] == []
    fake_onec.parse.assert_called_once()
    fake_sheets.read_all_records.assert_awaited_once()


@pytest.mark.asyncio
async def test_unsupported_extension_rejected_with_415(client):
    async with client as c:
        resp = await c.post(
            "/api/reconciliation",
            files={"file": ("note.pdf", b"%PDF-1.4", "application/pdf")},
        )
    assert resp.status_code == 415


@pytest.mark.asyncio
async def test_too_large_returns_413(client, monkeypatch):
    from app.config import get_settings as _gs

    settings = _gs()
    monkeypatch.setattr(settings, "max_upload_bytes", 16, raising=False)

    async with client as c:
        resp = await c.post(
            "/api/reconciliation",
            files={"file": ("big.csv", b"x" * 64, "text/csv")},
        )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_empty_file_returns_422(client):
    async with client as c:
        resp = await c.post(
            "/api/reconciliation",
            files={"file": ("empty.csv", b"", "text/csv")},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_parser_error_returns_ok_false(client, fake_onec):
    fake_onec.parse = MagicMock(side_effect=OneCParseError("broken header"))
    async with client as c:
        resp = await c.post("/api/reconciliation", files=_xls_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "onec_parse_error"


@pytest.mark.asyncio
async def test_sheets_error_returns_ok_false(client, fake_sheets):
    fake_sheets.read_all_records = AsyncMock(side_effect=SheetsReadError("api 503"))
    async with client as c:
        resp = await c.post("/api/reconciliation", files=_xls_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "sheets_read_error"
