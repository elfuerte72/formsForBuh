# Architecture: Layered

## Overview

The service is a thin, linear ETL pipeline between a Yandex Form webhook and Google
Sheets/Drive, with Claude Vision as the transformation step. There is one primary use case
(Stage 1 — UPD upload) and one small additional use case planned for Stage 2 (weekly
reconciliation). The domain contains no meaningful business rules beyond field extraction and
duplicate detection.

For this shape and scale, a **layered architecture** is the right fit: it keeps the request
path obvious (controller → orchestrator → service → SDK), avoids speculative abstractions, and
is cheap to extend with a second endpoint for Stage 2.

## Decision Rationale

- **Project type:** Single-purpose automation webhook.
- **Tech stack:** Python 3.12, FastAPI, async I/O throughout.
- **Team:** 1 developer.
- **Scale:** < 100 UPDs/month (two accountants, a handful of foremen).
- **Key factor:** Domain is trivial (ETL). Abstraction layers pay off only when business rules
  proliferate. Premature Clean Architecture would triple the code for no functional benefit.
- **Stage 2 consideration:** A second use case is planned, but it is structurally similar (form
  webhook → file parse → sheet diff). It fits as a second router + pipeline file within the same
  layered structure; no need to pre-modularize.

## Folder Structure

```
app/
├── main.py                 # FastAPI app + lifespan + router registration
├── config.py               # Settings (pydantic-settings) — single source of truth for env
├── models.py               # Pydantic DTOs: WebhookPayload, UPDRecord, SheetRow
├── deps.py                 # FastAPI dependencies (auth, service factories)
├── api/                    # Presentation layer — HTTP handlers only
│   ├── __init__.py
│   ├── upd_upload.py       # POST /webhook/yandex-form (Stage 1)
│   └── reconciliation.py   # POST /webhook/reconciliation (Stage 2, later)
├── pipelines/              # Application layer — orchestration of services
│   ├── __init__.py
│   ├── upd_upload.py       # process_upd(payload) → downloads, extracts, archives, appends
│   └── reconciliation.py   # (Stage 2)
├── services/               # Integration layer — thin SDK adapters
│   ├── __init__.py
│   ├── vision.py           # Claude Vision + tool use — returns UPDRecord
│   ├── sheets.py           # gspread wrapper: append_row, find_by_upd_number
│   ├── drive.py            # Google Drive: get_or_create_folder(dd/mm/yyyy), upload_file
│   └── files.py            # HTTP download, PDF → PNG (PyMuPDF)
├── core/                   # Cross-cutting: no business logic here
│   ├── logging.py          # structlog configuration, correlation-id context var
│   └── errors.py           # VisionExtractionError, DuplicateUPDError, etc.
└── __init__.py

tests/
├── fixtures/
│   └── upd/                # Real UPD screenshots as regression samples
├── unit/
│   ├── test_vision.py      # Uses fixtures; hits real Claude or recorded cassettes
│   ├── test_sheets.py      # Mocked gspread
│   └── test_drive.py       # Mocked Drive API
└── integration/
    └── test_webhook.py     # End-to-end: webhook call → mocked externals → assertions

pyproject.toml              # uv
Dockerfile
docker-compose.yml
.env.example
README.md
```

## Dependency Rules

Each layer depends **only downward**. Upward imports are forbidden.

```
api/         ← imports pipelines/, models, deps, core
pipelines/   ← imports services/, models, core
services/    ← imports external SDKs (anthropic, gspread, googleapiclient, httpx, pymupdf), models, core
core/        ← stdlib only (+ structlog, pydantic)
models       ← pydantic only
config       ← pydantic-settings only
```

- ✅ `api/upd_upload.py` → `pipelines/upd_upload.py` → `services/vision.py`
- ✅ `services/drive.py` imports `googleapiclient`
- ❌ `services/vision.py` imports from `pipelines/` (would be a circular/backward dep)
- ❌ `api/upd_upload.py` imports `services/sheets.py` directly (skip pipeline → forbidden)
- ❌ Any module imports from `main.py`

## Layer/Module Communication

- **Handlers stay thin.** An `api/*` handler does: (1) validate payload via FastAPI + Pydantic,
  (2) check webhook secret via `deps.verify_webhook_secret`, (3) schedule the pipeline via
  `BackgroundTasks`, (4) return a 200 immediately. No business logic.
- **Pipelines own orchestration.** A single top-level function per use case (e.g.,
  `process_upd(payload, services)`) calls services in sequence and handles pipeline-level errors.
  Pipelines take service instances via parameters (dependency injection) — easy to swap in tests.
- **Services are stateless wrappers.** Each exposes a narrow async interface
  (`await vision.extract(image_bytes)`), owns SDK client lifecycle, and translates SDK-specific
  exceptions into our `core.errors` types.
- **Models cross layers unchanged.** A `UPDRecord` produced by `vision.py` is the same object the
  pipeline passes to `sheets.py`. No layer-specific DTOs — the project is too small to justify
  mapping layers.
- **Config flows via FastAPI Depends.** `get_settings()` returns a cached `Settings` instance;
  services pick up what they need through a factory dependency (`get_vision_service`, etc.).

## Key Principles

1. **Linear pipelines over abstractions.** `process_upd` reads top-to-bottom like a recipe. If a
   step is reused across pipelines, extract a helper — otherwise leave inline.
2. **Fail loud in the background, fast in the foreground.** The webhook returns 200 within
   ~500ms. Pipeline failures are logged with correlation-id and persisted to the sheet as a row
   with an `error` column set, so accountants still see the submission.
3. **SDK boundaries are the only abstraction.** A `services/*` module is the ONLY place that
   imports `anthropic`, `gspread`, `googleapiclient`, or `pymupdf`. Swapping a provider is a
   single-file change.
4. **Pydantic everywhere at the edges.** Every inbound payload, LLM tool-use result, and config
   value is parsed by Pydantic. Inside the app we pass validated model instances, not dicts.
5. **No speculative layers.** Don't add a repository pattern around gspread — it's already a
   repository-shaped API. Don't add a "domain service" for calling Claude — `services/vision.py`
   is already the right abstraction level.
6. **Idempotency at the sheet boundary.** Duplicate check happens in `services/sheets.py::find_by_upd_number`
   before `append_row`. No database; the sheet is the source of truth.

## Code Examples

### Handler — thin, delegates to pipeline

```python
# app/api/upd_upload.py
from fastapi import APIRouter, BackgroundTasks, Depends
from app.deps import verify_webhook_secret, get_pipeline
from app.models import WebhookPayload
from app.pipelines.upd_upload import process_upd

router = APIRouter()

@router.post("/webhook/yandex-form", status_code=200)
async def yandex_form_webhook(
    payload: WebhookPayload,
    bg: BackgroundTasks,
    _: None = Depends(verify_webhook_secret),
    pipeline = Depends(get_pipeline),
):
    bg.add_task(process_upd, payload, pipeline)
    return {"ok": True}
```

### Pipeline — orchestration, no SDK calls

```python
# app/pipelines/upd_upload.py
from app.models import WebhookPayload
from app.core.logging import bind_correlation_id, log
from app.core.errors import DuplicateUPDError

async def process_upd(payload: WebhookPayload, svc) -> None:
    with bind_correlation_id():
        log.info("upd.received", foreman=payload.foreman, file=str(payload.file_url))

        raw = await svc.files.download(payload.file_url)
        image = await svc.files.to_png(raw, filename=payload.file_name)

        try:
            record = await svc.vision.extract(image.bytes, media_type=image.media_type)
        except Exception as e:
            await svc.sheets.append_error_row(payload, reason=str(e))
            log.exception("upd.extract_failed")
            return

        if await svc.sheets.find_by_upd_number(record.upd_number):
            log.info("upd.duplicate", upd_number=record.upd_number)
            return  # Stage 1: silent skip, no notification

        folder = await svc.drive.get_or_create_folder(record.date.strftime("%d/%m/%Y"))
        link = await svc.drive.upload(folder_id=folder.id, name=payload.file_name, content=raw)

        await svc.sheets.append_row(record, foreman=payload.foreman, drive_link=link)
        log.info("upd.saved", upd_number=record.upd_number)
```

### Service — SDK boundary

```python
# app/services/sheets.py
import gspread
from app.models import UPDRecord

class SheetsService:
    def __init__(self, worksheet: gspread.Worksheet):
        self._ws = worksheet

    async def find_by_upd_number(self, upd_number: str) -> bool:
        # gspread is sync; run in threadpool from callers that need true async
        cell = self._ws.find(upd_number, in_column=COL_UPD_NUMBER)
        return cell is not None

    async def append_row(self, r: UPDRecord, foreman: str, drive_link: str) -> None:
        self._ws.append_row([
            r.upd_number, r.date.isoformat(), r.organization, r.amount,
            foreman, drive_link, "",  # last col reserved for error notes
        ], value_input_option="USER_ENTERED")
```

Note: the handler `api/*` file never imports `gspread`. The pipeline `pipelines/*` file never
imports `gspread`. Only `services/sheets.py` does.

## Anti-Patterns

- ❌ **Don't put HTTP or SDK logic in `pipelines/*`.** Pipelines orchestrate; services integrate.
- ❌ **Don't reach through layers.** A handler calling `services/sheets.py` directly skips the
  pipeline and hides the business sequence. If you feel the urge, the pipeline is probably too
  thick — move logic, don't skip.
- ❌ **Don't create interfaces/protocols just because you can.** `SheetsService` doesn't need a
  `SheetsRepository` ABC. Pass the concrete class; mock it in tests via `monkeypatch` or by
  substituting at the `deps.get_pipeline` boundary.
- ❌ **Don't scatter `os.getenv` calls.** All env access goes through `app/config.py::Settings`.
- ❌ **Don't let SDK exceptions leak past `services/*`.** Translate `gspread.WorksheetNotFound`,
  `anthropic.APIStatusError`, etc. into `core.errors` types so pipelines handle one taxonomy.
- ❌ **Don't build async wrappers around sync SDKs prematurely.** gspread and googleapiclient are
  sync — call them from pipelines with `asyncio.to_thread(...)` if/when latency becomes an issue.
  For this volume it won't.
- ❌ **Don't add a database.** The sheet is the system of record. Adding Postgres for "later"
  doubles the ops burden for zero current value.
