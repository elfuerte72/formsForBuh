# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Service for the client "Выборг" (Russian bookkeeping). Serves a custom HTML upload form at `/`: bookkeeper picks a foreman (Юра / Гриша / Боря) and uploads the УПД file (PDF or image). The backend rasterises PDFs to PNG, calls Claude Vision (tool-use) to extract four fields (`organization`, `date`, `amount`, `upd_number`), appends a row to a configured Google Sheet, and returns the result synchronously so the page can show a success / warning / error banner.

Yandex Forms integration was removed in favour of this self-hosted form. Stage 2 (weekly reconciliation) is planned but not built. See `.ai-factory/DESCRIPTION.md`, `.ai-factory/ARCHITECTURE.md`, `.ai-factory/PLAN.md`, `AGENTS.md`, `README.md`.

## Common commands

Dependencies and environment are managed with **uv** (Python 3.12). Do not use pip directly.

```bash
uv sync                                                # install deps from uv.lock
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 # run locally; open http://localhost:8000/
uv run pytest                                          # full test suite
uv run pytest tests/unit/test_vision.py                # one file
uv run pytest tests/unit/test_vision.py::test_name     # one test
uv run pytest -k pattern                               # filter by name
```

Required env vars: `ANTHROPIC_API_KEY`, `SHEET_ID`, `GOOGLE_CREDENTIALS_JSON` (Service Account JSON as a single line). Optional vars live in `app/config.py` / `.env.example`. Tests auto-set dummy values via `tests/conftest.py`.

Docker / Railway: `Dockerfile` is multi-stage (uv builder → slim runtime, non-root). Railway uses `railway.toml` with healthcheck on `/health`; the container binds `0.0.0.0:$PORT`.

## Architecture

Strict **layered architecture** with enforced downward-only dependencies (see `.ai-factory/ARCHITECTURE.md` for the full rationale and examples). Violations are treated as bugs.

```
api/         → pipelines/ → services/ → external SDKs
                              ↓
                            core/ (logging, errors) — stdlib + structlog only
models, config cross all layers unchanged
static/      → served via fastapi.staticfiles.StaticFiles (no templating)
```

**Request flow (`POST /api/upload`):**

1. `app/api/upd_upload.py` — thin handler. Validates `foreman: Foreman` (Literal) and `file: UploadFile` (content type whitelist + size cap). No auth — the form is public. Generates a `correlation_id`, awaits the pipeline synchronously, returns `UploadResult` as JSON.
2. `app/pipelines/upd_upload.py::process_upd` — orchestrates `files.to_png` → `vision.extract` → (when all fields present) `sheets.append_row`. Catches `UnsupportedFileTypeError` / `VisionExtractionError` / `SheetsAppendError` / `AppError` and returns `UploadResult(ok=False, error=<machine_code>)` instead of raising.
3. `app/services/files.py` — only place that imports `httpx` and `pymupdf`. `to_png(data, filename, media_type)` rasterises PDF page 1 at configurable DPI; passthrough for `image/*`. The legacy `download(url)` method is kept (no callers in Stage 1) for future use.
4. `app/services/vision.py` — only place that imports `anthropic`. Calls Claude with `tool_choice` forced to the `record_upd` tool (schema defined inline; semantics in `.claude/skills/upd-vision-extraction/SKILL.md`). Retries `APIConnectionError`/`RateLimitError` with `1s/2s/4s` backoff; other SDK errors raise `VisionExtractionError` immediately. Claude emits the sentinel `<UNKNOWN>` for unreadable fields — `UPDRecord._sanitize_unknowns` in `app/models.py` converts sentinels to `None` and parses Russian-formatted amounts (`"12 345,67"` → `12345.67`). `record.needs_review` is `True` whenever any required field is `None`; in that case the pipeline does NOT write to Sheets and the form shows a warning banner.
5. `app/services/sheets.py` — only place that imports `gspread` and `google.oauth2`. `append_row` is async (wraps the synchronous gspread call via `asyncio.to_thread`). Column order is fixed in `COLUMNS`; the user creates the matching header row in the spreadsheet manually. SDK errors are translated into `SheetsAppendError`.

**Frontend:** a single `app/static/index.html` (inline CSS + JS, no bundler, no Jinja). `app/main.py` mounts `StaticFiles(directory="app/static", html=True)` at `/` AFTER `include_router(...)`, so `/api/upload` and `/health` win the dispatch and `/` serves the form. The script POSTs `multipart/form-data` to `/api/upload` and renders one of three banners (success / needs_review / error) based on the JSON response.

**DI and singletons (`app/deps.py`):** `AsyncAnthropic`, `httpx.AsyncClient`, and `SheetsService` are `@lru_cache` singletons; the first two are warmed in the FastAPI lifespan. `FilesService` and `VisionService` are constructed per-request via `Depends`. Tests override the three service factories via `app.dependency_overrides`.

**Settings (`app/config.py`):** All env access goes through `Settings` (pydantic-settings, `.env` loading). `get_settings()` is `@lru_cache`d — tests clear that cache between cases via the autouse fixture in `tests/conftest.py`. `sheet_url` is a `cached_property` derived from `sheet_id`. `redact_settings()` masks `anthropic_api_key` and `google_credentials_json` before logging at startup.

**Logging (`app/core/logging.py`):** `structlog` with JSON (prod) or pretty (dev) renderer, driven by `LOG_FORMAT`. A `correlation_id` contextvar (`bind_correlation_id`) threads through the pipeline and is returned in the upload response so one submission can be grepped end-to-end.

## Key conventions (read before editing)

- **SDK boundaries are the only abstraction.** A `services/*` module is the **only** place allowed to import `anthropic`, `httpx`, `pymupdf`, `gspread`, `google.oauth2`. Pipelines never import external SDKs. Handlers never call services directly — always through a pipeline.
- **Translate SDK exceptions at the service boundary** into `app/core/errors.py` types (`UnsupportedFileTypeError`, `VisionExtractionError`, `SheetsAppendError`, base `AppError`, plus the legacy `FileDownloadError`). Pipelines handle one taxonomy.
- **Pydantic at every edge.** `Foreman` literal + `UploadFile` for inbound, `UPDRecord` for vision output, `DownloadedFile` for files, `UploadResult` for outbound. Inside the app, pass validated instances, never dicts.
- **Synchronous handler.** The form must show the result on submit, so `process_upd` is awaited inline. There are no `BackgroundTasks`. If processing exceeds the proxy timeout, surface that as a backend issue — don't move work back to the background without revisiting the UX.
- **Vision extraction rules live in `app/services/vision.py::SYSTEM`** and `.claude/skills/upd-vision-extraction/SKILL.md`. When changing the tool schema or the sentinel behaviour, update **both** — `UPDRecord._sanitize_unknowns` in `app/models.py` depends on the sentinel contract.
- **No database, no Clean Architecture.** Stated preference: minimal abstractions, modern Python tooling. Don't add repository patterns, ABCs, or "domain services" around already thin SDKs. If you feel a layer is missing, re-read `.ai-factory/ARCHITECTURE.md` — the anti-pattern list is explicit.
- **Stage 2 additions** (`app/services/drive.py`, `app/api/reconciliation.py`, `app/pipelines/reconciliation.py`) are planned but not yet created. Add them in place without restructuring.

## Tests

`pytest-asyncio` in `auto` mode; `respx` mocks `httpx` in unit tests; integration tests call the FastAPI app through `httpx.ASGITransport` with dependency overrides on `get_files_service` / `get_vision_service` / `get_sheets_service`. Real UPD screenshots in `tests/fixtures/upd/` act as regression samples for vision — tests reading them expect to be able to stub out the Anthropic client, not hit the real API. `tests/unit/test_sheets.py` patches `gspread.authorize` and `Credentials.from_service_account_info` so SheetsService never hits Google.
