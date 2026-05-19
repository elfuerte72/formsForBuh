"""Pydantic DTOs crossing layer boundaries."""

from __future__ import annotations

from datetime import date as Date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


# Foreman is a free-form string entered in the upload form. Kept as an alias so
# call sites stay self-documenting; validation lives in the API handler.
Foreman = str


# Values Claude emits when it can't confidently read a field (see vision SYSTEM prompt).
_UNKNOWN_SENTINELS = frozenset(
    {"<unknown>", "unknown", "n/a", "na", "?", "-", "—", "none", "null"}
)


class UPDRecord(_Base):
    """Structured result of Claude Vision extraction.

    Fields are nullable: if Claude can't read a field confidently it emits the
    sentinel ``<UNKNOWN>`` (see SYSTEM prompt in ``app/services/vision.py``) and
    :meth:`_sanitize_unknowns` converts it to ``None``. The ``needs_review``
    computed field tells downstream consumers (logs, Sheets row) that a human
    must look at this record.

    Field semantics defined in .claude/skills/upd-vision-extraction/SKILL.md.
    """

    organization: str | None = Field(
        None, description="Buyer side: на кого выписан УПД (Гринлайн / Исмаилов)"
    )
    counterparty: str | None = Field(
        None, description="Seller side: от кого пришёл УПД (Продавец/Грузоотправитель)"
    )
    date: Date | None = Field(None, description="Document issue date")
    amount: float | None = Field(None, ge=0, description="Total amount payable (VAT included)")
    upd_number: str | None = Field(None, description="UPD / invoice number as printed")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def needs_review(self) -> bool:
        """True when at least one required field could not be extracted."""
        return any(
            v is None
            for v in (
                self.organization,
                self.counterparty,
                self.date,
                self.amount,
                self.upd_number,
            )
        )

    def missing_fields(self) -> list[str]:
        """Names of fields the model failed to extract."""
        return [
            name
            for name, value in (
                ("organization", self.organization),
                ("counterparty", self.counterparty),
                ("date", self.date),
                ("amount", self.amount),
                ("upd_number", self.upd_number),
            )
            if value is None
        ]

    @model_validator(mode="before")
    @classmethod
    def _sanitize_unknowns(cls, data: Any) -> Any:
        """Convert Claude's fallback sentinels into real ``None`` values.

        Accepts Russian-formatted amounts ("12 345,67") as a string and converts
        to float. Anything non-parseable in a numeric field becomes ``None``
        rather than a validation error — partial records are useful.
        """
        if not isinstance(data, dict):
            return data

        cleaned: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, str):
                stripped = value.strip()
                if not stripped or stripped.lower() in _UNKNOWN_SENTINELS:
                    cleaned[key] = None
                    continue
                cleaned[key] = stripped
            else:
                cleaned[key] = value

        # Numeric fields may arrive as Russian-formatted strings — normalise.
        amount = cleaned.get("amount")
        if isinstance(amount, str):
            candidate = amount.replace("\u00a0", "").replace(" ", "").replace(",", ".")
            try:
                cleaned["amount"] = float(candidate)
            except ValueError:
                cleaned["amount"] = None

        return cleaned


class DownloadedFile(_Base):
    """Result of files.to_png(): normalised PNG bytes + metadata.

    ``data`` is the PNG payload (passthrough for image/*, rasterised page 1
    for PDFs). ``page_count`` is None for non-PDF inputs.
    """

    data: bytes
    media_type: str = Field("image/png")
    original_name: str
    page_count: int | None = None


class UploadResult(_Base):
    """Per-file result inside :class:`BatchUploadResult`.

    Per-file validation failures (unsupported type, empty / too large) surface
    here as ``ok=False`` with a stable ``error`` code — request-level problems
    (no foreman, no files, too many files) still raise HTTP 413/422 from the
    handler. The frontend branches on ``ok`` and ``needs_review`` to render the
    right banner. ``filename`` is populated by the handler / batch wrapper so
    the UI can pair each result with the file the user selected.
    """

    ok: bool
    correlation_id: str
    filename: str | None = None
    record: UPDRecord | None = None
    sheet_url: str | None = None
    needs_review: bool = False
    missing_fields: list[str] | None = None
    error: str | None = None


class BatchUploadResult(_Base):
    """Response DTO returned by ``POST /api/upload``.

    Always wraps results in ``items`` — even when the user submitted a single
    file.

    Invariant: ``ok`` is ``True`` iff **every** item has ``ok=True`` and
    ``needs_review=False``. A single partial-recognition (``needs_review=True``)
    or per-file error flips the batch ``ok`` to ``False``. The frontend uses
    this as a single signal to decide whether to reset the form.
    """

    ok: bool
    correlation_id: str
    items: list[UploadResult] = Field(default_factory=list)


# --- Stage 2: reconciliation ------------------------------------------------


class OneCRecord(_Base):
    """One row parsed from a 1С export (xls/xlsx/csv).

    ``upd_number`` is the *incoming* document number (column «Номер вх.» in
    1С) — the same value Claude Vision extracts from the УПД header. The
    internal 1С document number is intentionally ignored.
    """

    upd_number: str = Field(description="Incoming UPD number from supplier (raw)")
    date: Date | None = Field(None, description="Document date (Дата or Дата вх.)")
    amount: float | None = Field(None, ge=0, description="Total amount with VAT")
    organization: str | None = Field(None, description="Counterparty name (column «Информация»)")
    source_row: int = Field(description="Original 1-based row index in the spreadsheet")


class SheetUPDRow(_Base):
    """One row read from the foreman's Google Sheet."""

    upd_number: str = Field(description="UPD number as written by Claude Vision")
    organization: str | None = None
    counterparty: str | None = None
    date: Date | None = None
    amount: float | None = Field(None, ge=0)
    foreman: str | None = None
    uploaded_at: str | None = None
    status: str | None = None
    source_row: int = Field(description="1-based row index inside the spreadsheet")


class MissingUPD(_Base):
    """A UPD listed in 1С but never uploaded by foremen."""

    upd_number: str
    date: Date | None = None
    amount: float | None = None
    organization: str | None = None
    source_row: int


class DuplicateUPD(_Base):
    """A UPD uploaded more than once into the foreman sheet."""

    upd_number: str
    count: int = Field(ge=2)
    foremen: list[str] = Field(default_factory=list)
    dates: list[Date] = Field(default_factory=list)


class ExtraUPD(_Base):
    """A UPD uploaded by a foreman that has no match in the 1С export."""

    upd_number: str
    foreman: str | None = None
    date: Date | None = None


class ReconciliationStats(_Base):
    """High-level counts shown above the three lists."""

    onec_total: int = Field(ge=0)
    foreman_total: int = Field(ge=0)
    matched: int = Field(ge=0)
    missing: int = Field(ge=0)
    duplicates: int = Field(ge=0)
    extras: int = Field(ge=0)
    coverage_percent: float = Field(ge=0, le=100)


class ReconciliationResult(_Base):
    """Response DTO returned by ``POST /api/reconciliation``.

    Mirrors :class:`UploadResult`: HTTP 200 unless the request is malformed;
    pipeline errors land here as ``ok=False`` with a stable ``error`` code.
    """

    ok: bool
    correlation_id: str
    missing: list[MissingUPD] = Field(default_factory=list)
    duplicates: list[DuplicateUPD] = Field(default_factory=list)
    extras: list[ExtraUPD] = Field(default_factory=list)
    stats: ReconciliationStats | None = None
    error: str | None = None
