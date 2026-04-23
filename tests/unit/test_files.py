"""Unit tests for FilesService."""

from __future__ import annotations

from pathlib import Path

import httpx
import pymupdf
import pytest
import respx

from app.core.errors import FileDownloadError, UnsupportedFileTypeError
from app.services.files import FilesService


FIXTURES = Path(__file__).parent.parent / "fixtures" / "upd"


async def _make_service(
    client: httpx.AsyncClient, *, max_bytes: int = 25 * 1024 * 1024, dpi: int = 150
) -> FilesService:
    return FilesService(client=client, max_bytes=max_bytes, dpi=dpi)


@pytest.mark.asyncio
@respx.mock
async def test_download_success(upd_image_bytes: bytes) -> None:
    url = "https://forms.example/file.png"
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            content=upd_image_bytes,
            headers={"content-type": "image/png"},
        )
    )
    async with httpx.AsyncClient() as c:
        svc = await _make_service(c)
        raw, media_type = await svc.download(url)
    assert media_type == "image/png"
    assert raw == upd_image_bytes


@pytest.mark.asyncio
@respx.mock
async def test_download_size_limit_via_header() -> None:
    url = "https://forms.example/big.pdf"
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            content=b"x" * 10,
            headers={"content-type": "application/pdf", "content-length": "99999999"},
        )
    )
    async with httpx.AsyncClient() as c:
        svc = await _make_service(c, max_bytes=1024)
        with pytest.raises(FileDownloadError):
            await svc.download(url)


@pytest.mark.asyncio
async def test_to_png_image_passthrough(upd_image_bytes: bytes) -> None:
    async with httpx.AsyncClient() as c:
        svc = await _make_service(c)
        out = await svc.to_png(
            upd_image_bytes, filename="upd.png", media_type="image/png"
        )
    assert out.media_type == "image/png"
    assert out.data == upd_image_bytes
    assert out.page_count is None
    assert out.original_name == "upd.png"


@pytest.mark.asyncio
async def test_to_png_pdf_first_page(upd_image_bytes: bytes) -> None:
    # Build a 2-page PDF in memory by embedding the sample image twice.
    doc = pymupdf.open()
    for _ in range(2):
        page = doc.new_page(width=600, height=800)
        rect = pymupdf.Rect(0, 0, 600, 800)
        page.insert_image(rect, stream=upd_image_bytes)
    pdf_bytes = doc.tobytes()
    doc.close()

    async with httpx.AsyncClient() as c:
        svc = await _make_service(c, dpi=100)
        out = await svc.to_png(
            pdf_bytes, filename="upd.pdf", media_type="application/pdf"
        )
    assert out.media_type == "image/png"
    assert out.page_count == 2
    assert out.data.startswith(b"\x89PNG")  # PNG magic header


@pytest.mark.asyncio
async def test_to_png_unsupported_type() -> None:
    async with httpx.AsyncClient() as c:
        svc = await _make_service(c)
        with pytest.raises(UnsupportedFileTypeError):
            await svc.to_png(b"hello", filename="notes.txt", media_type="text/plain")
