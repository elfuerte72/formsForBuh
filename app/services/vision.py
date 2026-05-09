"""Claude Vision extractor for Russian UPD documents.

This service is the ONLY place that imports ``anthropic``. It translates
provider errors into :class:`VisionExtractionError` so the pipeline handles
one taxonomy.
"""

from __future__ import annotations

import asyncio
import base64

from anthropic import APIConnectionError, AsyncAnthropic, RateLimitError
from pydantic import ValidationError

from app.core.errors import VisionExtractionError
from app.core.logging import get_logger
from app.models import UPDRecord

log = get_logger("vision")

# --- Prompt / tool schema (source of truth lives in the skill) --------------
# See .claude/skills/upd-vision-extraction/SKILL.md

SYSTEM = """You extract structured data from Russian UPD (универсальный передаточный документ) images.
A UPD combines an invoice (счёт-фактура) and a delivery note (товарная накладная / транспортная накладная).

Rules:
1. Two parties to extract — do NOT confuse them:
   • `organization` = the BUYER side (Покупатель / Грузополучатель). This is the company on whose behalf the document was issued — typically "Гринлайн" or "Исмаилов" (also seen as "Кемран"). Include the legal form (ООО / ИП / АО) if printed.
   • `counterparty` = the SELLER side (Продавец / Грузоотправитель) — the supplier that issued the УПД. Include the legal form too.
2. Document date: look for "от DD.MM.YYYY" adjacent to "СЧЁТ-ФАКТУРА №". Not the delivery date.
3. Amount: "Всего к оплате" or "Всего с учётом НДС" — NOT a line-item price. Convert "12 345,67" → 12345.67.
4. UPD number: take the exact string after "СЧЁТ-ФАКТУРА №", preserving all characters.
5. If a field is obscured by a stamp/signature but you can reconstruct it from context, do so. If the field is genuinely unreadable, absent from the page, or you are not confident — output the string "<UNKNOWN>" for that field. This applies to every field including numeric ones (the downstream system expects this sentinel). NEVER invent, guess, or fabricate a value.
6. Always call the `record_upd` tool. Never respond with plain text.
"""

EXTRACT_TOOL = {
    "name": "record_upd",
    "description": "Record the extracted fields from the UPD document.",
    "input_schema": {
        "type": "object",
        "properties": {
            "organization": {
                "type": "string",
                "description": "BUYER side: на кого выписан УПД (Покупатель / Грузополучатель). Typically 'Гринлайн' or 'Исмаилов' (also written as 'Кемран'). Include legal form if printed.",
            },
            "counterparty": {
                "type": "string",
                "description": "SELLER side: от кого пришёл УПД (Продавец / Грузоотправитель). Full legal name including ООО/ИП/АО prefix.",
            },
            "date": {
                "type": "string",
                "description": "Document date in ISO format YYYY-MM-DD. Look for 'от DD.MM.YYYY' near the document number",
            },
            "amount": {
                "type": ["number", "string"],
                "description": "Total amount payable in rubles, VAT included ('Всего к оплате'). Russian decimal comma converted to dot. Emit the literal string '<UNKNOWN>' if unreadable.",
            },
            "upd_number": {
                "type": "string",
                "description": "UPD number exactly as printed ('СЧЁТ-ФАКТУРА №' value), preserve slashes/letters",
            },
        },
        "required": ["organization", "counterparty", "date", "amount", "upd_number"],
    },
}

_MAX_TOKENS = 1024
_RETRY_DELAYS = (1.0, 2.0, 4.0)


class VisionService:
    def __init__(
        self,
        *,
        client: AsyncAnthropic,
        model: str,
    ) -> None:
        self._client = client
        self._model = model

    async def extract(
        self, image_bytes: bytes, *, media_type: str = "image/png"
    ) -> UPDRecord:
        """Call Claude Vision with tool-use forcing and parse the result.

        Retries (1s/2s/4s) only on ``APIConnectionError`` / ``RateLimitError``.
        All other failures raise :class:`VisionExtractionError` immediately.
        """
        b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        attempt = 0
        last_exc: Exception | None = None

        while attempt <= len(_RETRY_DELAYS):
            try:
                log.debug(
                    "vision.extract.call",
                    attempt=attempt + 1,
                    model=self._model,
                    max_tokens=_MAX_TOKENS,
                    media_type=media_type,
                    bytes=len(image_bytes),
                )
                resp = await self._client.messages.create(
                    model=self._model,
                    max_tokens=_MAX_TOKENS,
                    system=SYSTEM,
                    tools=[EXTRACT_TOOL],
                    tool_choice={"type": "tool", "name": "record_upd"},
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": media_type,
                                        "data": b64,
                                    },
                                },
                                {
                                    "type": "text",
                                    "text": "Extract the UPD fields and call record_upd.",
                                },
                            ],
                        }
                    ],
                )
                break
            except (APIConnectionError, RateLimitError) as exc:
                last_exc = exc
                if attempt >= len(_RETRY_DELAYS):
                    log.warning(
                        "vision.extract.retry_exhausted",
                        attempt=attempt + 1,
                        error=str(exc),
                    )
                    raise VisionExtractionError(
                        f"Vision call failed after retries: {exc}"
                    ) from exc
                delay = _RETRY_DELAYS[attempt]
                log.warning(
                    "vision.extract.retry",
                    attempt=attempt + 1,
                    delay_seconds=delay,
                    error=str(exc),
                )
                await asyncio.sleep(delay)
                attempt += 1
            except Exception as exc:
                log.exception("vision.extract.unexpected_sdk_error")
                raise VisionExtractionError(f"Vision SDK error: {exc}") from exc
        else:  # pragma: no cover - loop always breaks/raises
            raise VisionExtractionError(str(last_exc))

        tool_use = next((b for b in resp.content if b.type == "tool_use"), None)
        if tool_use is None:
            log.warning(
                "vision.extract.no_tool_use",
                stop_reason=getattr(resp, "stop_reason", None),
                content_types=[b.type for b in resp.content],
            )
            raise VisionExtractionError("Claude response contained no tool_use block")

        try:
            record = UPDRecord.model_validate(tool_use.input)
        except ValidationError as exc:
            log.warning(
                "vision.extract.validation_error",
                raw=tool_use.input,
                errors=exc.errors(),
            )
            raise VisionExtractionError(f"Tool output failed validation: {exc}") from exc

        usage = getattr(resp, "usage", None)
        log.info(
            "vision.extract.ok",
            record=record.model_dump(mode="json"),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )
        return record
