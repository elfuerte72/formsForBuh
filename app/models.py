"""Pydantic DTOs crossing layer boundaries."""

from __future__ import annotations

from datetime import date as Date
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, computed_field, model_validator


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class WebhookPayload(_Base):
    """Payload posted by Yandex Forms to POST /webhook/yandex-form.

    Contract is defined by us — user copies the JSON body template into the
    form's HTTP-integration settings (see README).
    """

    foreman: str = Field(..., min_length=1, description="Selected foreman name")
    file_url: HttpUrl = Field(..., description="Public URL of the uploaded UPD")
    file_name: str = Field(..., min_length=1, description="Original filename from the form")
    submitted_at: datetime | None = Field(None, description="Form submission timestamp")
    form_id: str | None = Field(None, description="Yandex Form id")


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

    organization: str | None = Field(None, description="Full legal name of the seller org")
    date: Date | None = Field(None, description="Document issue date")
    amount: float | None = Field(None, ge=0, description="Total amount payable (VAT included)")
    upd_number: str | None = Field(None, description="UPD / invoice number as printed")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def needs_review(self) -> bool:
        """True when at least one required field could not be extracted."""
        return any(
            v is None for v in (self.organization, self.date, self.amount, self.upd_number)
        )

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

    ``bytes`` is the PNG payload (passthrough for image/*, rasterised page 1
    for PDFs). ``page_count`` is None for non-PDF inputs.
    """

    data: bytes
    media_type: str = Field("image/png")
    original_name: str
    page_count: int | None = None
