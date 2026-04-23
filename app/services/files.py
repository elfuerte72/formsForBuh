"""File download + normalisation to PNG (SDK boundary: httpx + pymupdf)."""

from __future__ import annotations

import mimetypes
from pathlib import PurePosixPath
from urllib.parse import urlparse

import httpx
import pymupdf

from app.core.errors import FileDownloadError, UnsupportedFileTypeError
from app.core.logging import get_logger
from app.models import DownloadedFile

log = get_logger("files")

_PDF_TYPES = {"application/pdf", "application/x-pdf"}
_IMAGE_PREFIX = "image/"


class FilesService:
    """Downloads a remote file and rasterises PDFs to a single PNG page.

    Services stay thin — one narrow async API surface per class.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        max_bytes: int,
        dpi: int,
    ) -> None:
        self._client = client
        self._max_bytes = max_bytes
        self._dpi = dpi

    # --- public API ----------------------------------------------------------

    async def download(self, url: str) -> tuple[bytes, str]:
        """Return ``(raw_bytes, media_type)``.

        Enforces ``max_bytes``. Media type is taken from the ``Content-Type``
        response header; falls back to ``mimetypes`` on the URL path if the
        server did not set one.
        """
        log.debug("files.download.start", url=url, max_bytes=self._max_bytes)
        try:
            async with self._client.stream("GET", url) as resp:
                resp.raise_for_status()
                content_length = resp.headers.get("content-length")
                if content_length and int(content_length) > self._max_bytes:
                    raise FileDownloadError(
                        f"File too large: {content_length} > {self._max_bytes}"
                    )

                chunks: list[bytes] = []
                received = 0
                async for chunk in resp.aiter_bytes():
                    received += len(chunk)
                    if received > self._max_bytes:
                        raise FileDownloadError(
                            f"File exceeded size cap while streaming: {received} > {self._max_bytes}"
                        )
                    chunks.append(chunk)

                raw = b"".join(chunks)
                media_type = (
                    resp.headers.get("content-type", "").split(";")[0].strip()
                    or _guess_media_type(url)
                )
                log.debug(
                    "files.download.done",
                    url=url,
                    bytes=len(raw),
                    media_type=media_type,
                )
                return raw, media_type

        except FileDownloadError:
            raise
        except httpx.HTTPError as exc:
            log.warning("files.download.http_error", url=url, error=str(exc))
            raise FileDownloadError(f"HTTP error downloading {url}: {exc}") from exc

    async def to_png(
        self, data: bytes, *, filename: str, media_type: str
    ) -> DownloadedFile:
        """Normalise ``data`` to a single-page PNG.

        - ``image/*``: passthrough (returned media_type preserved).
        - PDF: rasterise page 1 with PyMuPDF at ``self._dpi``.
        - anything else: :class:`UnsupportedFileTypeError`.
        """
        log.debug(
            "files.to_png.start",
            filename=filename,
            media_type=media_type,
            bytes=len(data),
        )

        if media_type in _PDF_TYPES or filename.lower().endswith(".pdf"):
            png_bytes, pages = _pdf_first_page_png(data, dpi=self._dpi)
            log.debug(
                "files.to_png.pdf_done",
                filename=filename,
                pages=pages,
                png_bytes=len(png_bytes),
                dpi=self._dpi,
            )
            return DownloadedFile(
                data=png_bytes,
                media_type="image/png",
                original_name=filename,
                page_count=pages,
            )

        if media_type.startswith(_IMAGE_PREFIX):
            log.debug(
                "files.to_png.image_passthrough",
                filename=filename,
                media_type=media_type,
                bytes=len(data),
            )
            return DownloadedFile(
                data=data,
                media_type=media_type,
                original_name=filename,
                page_count=None,
            )

        raise UnsupportedFileTypeError(
            f"Unsupported media type '{media_type}' for file '{filename}'"
        )


# --- helpers ----------------------------------------------------------------


def _guess_media_type(url: str) -> str:
    path = PurePosixPath(urlparse(url).path)
    guess, _ = mimetypes.guess_type(path.name)
    return guess or "application/octet-stream"


def _pdf_first_page_png(pdf_bytes: bytes, *, dpi: int) -> tuple[bytes, int]:
    """Rasterise page 1 of a PDF to PNG.

    Returns ``(png_bytes, page_count)``. PyMuPDF is the single place that owns
    the dependency — keep it behind this function.
    """
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # pragma: no cover - pymupdf error types vary
        raise UnsupportedFileTypeError(f"Cannot open PDF: {exc}") from exc

    try:
        if doc.page_count == 0:
            raise UnsupportedFileTypeError("PDF has zero pages")
        page = doc[0]
        pix = page.get_pixmap(dpi=dpi)
        return pix.tobytes("png"), doc.page_count
    finally:
        doc.close()
