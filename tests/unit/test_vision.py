"""Unit tests for VisionService — no real Anthropic calls."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from anthropic import APIConnectionError

from app.core.errors import VisionExtractionError
from app.services.vision import EXTRACT_TOOL, VisionService


def _fake_response(tool_input: dict | None, *, include_tool_use: bool = True) -> MagicMock:
    content = []
    if include_tool_use:
        tu = MagicMock()
        tu.type = "tool_use"
        tu.input = tool_input or {}
        content.append(tu)
    else:
        tx = MagicMock()
        tx.type = "text"
        tx.text = "refusing"
        content.append(tx)
    resp = MagicMock()
    resp.content = content
    resp.stop_reason = "tool_use" if include_tool_use else "end_turn"
    resp.usage = SimpleNamespace(input_tokens=1500, output_tokens=80)
    return resp


def _make_service(messages_create: AsyncMock, *, model: str = "claude-sonnet-4-6") -> VisionService:
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = messages_create
    return VisionService(client=client, model=model)


@pytest.mark.asyncio
async def test_extract_sends_tool_use_request() -> None:
    create = AsyncMock(
        return_value=_fake_response(
            {
                "organization": 'ООО "Озеленский"',
                "date": "2026-04-22",
                "amount": 12345.67,
                "upd_number": "6022635717",
            }
        )
    )
    svc = _make_service(create)
    rec = await svc.extract(b"PNGBYTES", media_type="image/png")

    assert rec.upd_number == "6022635717"
    assert rec.amount == pytest.approx(12345.67)

    kwargs = create.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["tool_choice"] == {"type": "tool", "name": "record_upd"}
    assert kwargs["tools"] == [EXTRACT_TOOL]
    content = kwargs["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[0]["source"]["type"] == "base64"
    assert content[1]["type"] == "text"


@pytest.mark.asyncio
async def test_extract_parses_record() -> None:
    create = AsyncMock(
        return_value=_fake_response(
            {
                "organization": "ИП Петров И.И.",
                "date": "2026-01-03",
                "amount": 42.5,
                "upd_number": "A-1/2026",
            }
        )
    )
    svc = _make_service(create)
    rec = await svc.extract(b"PNG", media_type="image/png")
    assert rec.organization == "ИП Петров И.И."
    assert rec.date.isoformat() == "2026-01-03"
    assert rec.upd_number == "A-1/2026"


@pytest.mark.asyncio
async def test_extract_raises_when_no_tool_use() -> None:
    create = AsyncMock(return_value=_fake_response(None, include_tool_use=False))
    svc = _make_service(create)
    with pytest.raises(VisionExtractionError):
        await svc.extract(b"PNG")


@pytest.mark.asyncio
async def test_extract_unknown_amount_yields_needs_review() -> None:
    """Sonnet returns '<UNKNOWN>' for unreadable fields — must not crash."""
    create = AsyncMock(
        return_value=_fake_response(
            {
                "organization": 'ООО "Строительный Двор"',
                "date": "2026-04-15",
                "amount": "<UNKNOWN>",
                "upd_number": "0084537945/2116132546",
            }
        )
    )
    svc = _make_service(create)
    rec = await svc.extract(b"PNG")
    assert rec.amount is None
    assert rec.organization == 'ООО "Строительный Двор"'
    assert rec.upd_number == "0084537945/2116132546"
    assert rec.needs_review is True


@pytest.mark.asyncio
async def test_extract_russian_decimal_amount_string() -> None:
    """Amount arriving as '12 345,67' must be parsed to a float."""
    create = AsyncMock(
        return_value=_fake_response(
            {
                "organization": "ИП Петров И.И.",
                "date": "2026-01-03",
                "amount": "12 345,67",
                "upd_number": "A-1/2026",
            }
        )
    )
    svc = _make_service(create)
    rec = await svc.extract(b"PNG")
    assert rec.amount == pytest.approx(12345.67)
    assert rec.needs_review is False


@pytest.mark.asyncio
async def test_extract_all_unknown_all_nullable() -> None:
    """Completely unreadable doc — every field None, needs_review True."""
    create = AsyncMock(
        return_value=_fake_response(
            {
                "organization": "<UNKNOWN>",
                "date": "<UNKNOWN>",
                "amount": "<UNKNOWN>",
                "upd_number": "<UNKNOWN>",
            }
        )
    )
    svc = _make_service(create)
    rec = await svc.extract(b"PNG")
    assert rec.organization is None
    assert rec.date is None
    assert rec.amount is None
    assert rec.upd_number is None
    assert rec.needs_review is True


@pytest.mark.asyncio
async def test_extract_retries_on_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Zero out backoff delays to keep the test fast.
    import app.services.vision as vision_mod

    monkeypatch.setattr(vision_mod, "_RETRY_DELAYS", (0, 0, 0))

    ok_response = _fake_response(
        {
            "organization": "ООО Ромашка",
            "date": "2026-02-01",
            "amount": 100.0,
            "upd_number": "R-7",
        }
    )

    # APIConnectionError requires a request kwarg.
    import httpx

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = APIConnectionError(request=req)

    create = AsyncMock(side_effect=[err, err, ok_response])
    svc = _make_service(create)
    rec = await svc.extract(b"PNG")
    assert rec.upd_number == "R-7"
    assert create.await_count == 3
