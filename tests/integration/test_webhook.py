"""Integration tests for POST /webhook/yandex-form.

We skip FastAPI lifespan (it touches structlog globals and cached clients)
and drive the ASGI app directly with dependency overrides.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.deps import get_files_service, get_vision_service
from app.main import app as real_app
from app.models import DownloadedFile, UPDRecord
from app.pipelines import upd_upload as pipeline_module


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
    svc.download = AsyncMock(return_value=(b"rawpng", "image/png"))
    svc.to_png = AsyncMock(
        return_value=DownloadedFile(
            data=b"rawpng",
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
            organization='ООО "Озеленский"',
            date=date(2026, 4, 22),
            amount=10500.0,
            upd_number="6022635717",
        )
    )
    return svc


@pytest.fixture
def client(app, fake_files, fake_vision):
    app.dependency_overrides[get_files_service] = lambda: fake_files
    app.dependency_overrides[get_vision_service] = lambda: fake_vision
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


VALID_PAYLOAD = {
    "foreman": "Иванов",
    "file_url": "https://forms.example/file.png",
    "file_name": "upd.png",
    "submitted_at": "2026-04-23T10:00:00Z",
    "form_id": "form-123",
}


@pytest.mark.asyncio
async def test_webhook_accepts_valid_payload(client):
    async with client as c:
        resp = await c.post(
            "/webhook/yandex-form",
            json=VALID_PAYLOAD,
            headers={"X-Webhook-Secret": "test-webhook-secret"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert isinstance(body["correlation_id"], str) and len(body["correlation_id"]) >= 16


@pytest.mark.asyncio
async def test_webhook_rejects_missing_secret(client):
    async with client as c:
        resp = await c.post("/webhook/yandex-form", json=VALID_PAYLOAD)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_webhook_rejects_invalid_secret(client):
    async with client as c:
        resp = await c.post(
            "/webhook/yandex-form",
            json=VALID_PAYLOAD,
            headers={"X-Webhook-Secret": "nope"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_webhook_rejects_bad_payload(client):
    bad = {"file_name": "upd.png"}  # missing foreman, file_url
    async with client as c:
        resp = await c.post(
            "/webhook/yandex-form",
            json=bad,
            headers={"X-Webhook-Secret": "test-webhook-secret"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_webhook_schedules_pipeline(client, fake_files, fake_vision, monkeypatch):
    captured: dict = {}

    original = pipeline_module.process_upd

    async def spy(payload, files, vision, *, correlation_id=None):
        captured["payload"] = payload
        captured["files"] = files
        captured["vision"] = vision
        captured["cid"] = correlation_id
        await original(payload, files, vision, correlation_id=correlation_id)

    # The handler imports process_upd directly — patch the symbol there.
    from app.api import upd_upload as api_module

    monkeypatch.setattr(api_module, "process_upd", spy)

    async with client as c:
        resp = await c.post(
            "/webhook/yandex-form",
            json=VALID_PAYLOAD,
            headers={"X-Webhook-Secret": "test-webhook-secret"},
        )

    assert resp.status_code == 200
    assert captured["payload"].foreman == "Иванов"
    assert captured["files"] is fake_files
    assert captured["vision"] is fake_vision
    assert captured["cid"] == resp.json()["correlation_id"]

    fake_files.download.assert_awaited_once()
    fake_files.to_png.assert_awaited_once()
    fake_vision.extract.assert_awaited_once()
