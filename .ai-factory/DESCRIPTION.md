# Project: formsForBuh — Bookkeeping Automation Bot

## Overview

Automation service for a construction company's bookkeeping workflow. Field foremen submit UPD
(universal transfer document — Russian tax document combining invoice and delivery note) via a
Yandex Form; the service receives the webhook, uses Claude Vision to extract key fields from the
document image/PDF, archives the file into a date-organized Google Drive folder structure, and
appends a row to a Google Sheet used by two accountants.

Client: Выборг. Delivered in two stages (20k each = 40k total). This repo covers **Stage 1**
(UPD upload flow). Stage 2 (weekly reconciliation) will be added later.

## Core Features (Stage 1)

- **Webhook receiver** — accepts submissions from Yandex Forms (foreman choice + uploaded file).
- **File download** — pulls UPD file (PDF or photo) from the URL supplied by the form.
- **Vision extraction** — Claude Sonnet 4.5 (vision) with tool use returns structured JSON:
  - `organization` — counterparty name (ООО / ИП)
  - `date` — document date
  - `amount` — total amount payable
  - `upd_number` — UPD / invoice number
- **Google Drive archival** — files are stored in folders named `dd/mm/yyyy` derived from the
  extracted date. If the folder exists, reuse it; otherwise create it. Target parent folder:
  <https://drive.google.com/drive/folders/1Iwlt-TalUu_l2jz0IvgjK-pFZBK7jHXa>.
- **Google Sheets append** — one row per UPD with the four extracted fields plus metadata
  (foreman name, submission timestamp, Drive file link).
- **Duplicate check** — before appending, look up `upd_number` in the sheet; skip if already
  present (log the event, no user notification on Stage 1).

## Tech Stack

- **Language:** Python 3.12
- **Package manager:** uv
- **Web framework:** FastAPI (async)
- **LLM / Vision:** Anthropic SDK (`anthropic`), model `claude-sonnet-4-6`, vision + tool use
- **PDF handling:** PyMuPDF (`pymupdf`) — pure-Python, no system deps
- **HTTP client:** httpx (async)
- **Google Sheets:** `gspread` (service account auth)
- **Google Drive:** `google-api-python-client` + `google-auth` (same service account)
- **Config:** `pydantic-settings` + `.env`
- **Logging:** `structlog` (JSON output in prod, pretty in dev)
- **Background tasks:** FastAPI `BackgroundTasks` (no Redis — low volume, two accountants)
- **Tests:** `pytest` + `pytest-asyncio`, fixtures using real UPD screenshots from `tests/fixtures/`
- **Container:** Docker (single-stage Python 3.12 slim)
- **Deployment:** Railway (user already has a paid plan)

## Architecture

See `.ai-factory/ARCHITECTURE.md` for detailed architecture guidelines.
Pattern: **Layered** (api → pipelines → services → external SDKs).

## Architecture Notes

- **Sync-respond, async-process:** webhook endpoint validates payload, enqueues a
  `BackgroundTasks` job, and returns 200 immediately — Yandex Forms must see a fast response to
  avoid retries.
- **Pipeline is a single function** (`app/pipeline.py::process_upd`) that orchestrates
  download → vision → drive → sheets. Kept linear for readability; no premature abstractions.
- **Services are thin adapters** around external SDKs (vision, sheets, drive, files). Each has a
  narrow public API so they can be swapped or mocked in tests.
- **Idempotency:** duplicate detection on `upd_number` before `append_row`. Note: this is a race
  window for concurrent submissions — acceptable given two foremen submitting occasionally.
- **Structured extraction:** Claude is called with a tool-use schema that mirrors a Pydantic
  model `UPDRecord`. This forces valid JSON and lets Pydantic catch parse errors at the boundary.
- **Auth:** webhook endpoint protected by a shared secret passed via HTTP header (configured in
  Yandex Form webhook settings and in `.env`).

## Non-Functional Requirements

- **Logging:** structured, configurable via `LOG_LEVEL`. Every pipeline run gets a correlation id.
- **Error handling:** failures in the background pipeline are logged but do not crash the webhook
  (the response was already sent). A failed extraction still archives the file and appends a row
  with empty fields + `error` note, so accountants can see the submission and fix manually.
- **Security:** secrets via env vars only; service-account JSON kept out of the repo; webhook
  secret required on every POST.
- **Observability:** Railway logs + optional structured-log forwarding later.
- **Cost ceiling:** Claude Sonnet vision ≈ $0.003 per page. Expected volume < 100 UPDs/month,
  so inference cost is negligible.

## Out of Scope (Stage 1)

- Telegram/email notifications to foremen (accepted / duplicate).
- Stage 2 reconciliation form (separate accountant-facing form that diffs a 1С export against
  the sheet).
- Multi-tenant support, auth UI, admin dashboards.
