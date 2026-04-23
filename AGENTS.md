# AGENTS.md

> Project map for AI agents. Keep this file up-to-date as the project evolves.

## Project Overview

Automation service that receives UPD document submissions from a Yandex Form webhook,
extracts four key fields (organization, date, amount, UPD number) via Claude Vision,
archives the source file into a date-named Google Drive folder, and appends a row to a
Google Sheet used by the company's two accountants.

See `.ai-factory/DESCRIPTION.md` for full specification.

## Tech Stack

- **Language:** Python 3.12
- **Package manager:** uv
- **Web framework:** FastAPI (async)
- **LLM / Vision:** Anthropic SDK — Claude Sonnet 4.5 (`claude-sonnet-4-6`)
- **PDF:** PyMuPDF
- **Google APIs:** gspread (Sheets), google-api-python-client (Drive)
- **Deployment:** Railway + Docker

## Project Structure

```
formsForBuh/
├── app/                        # Application package (layered: api → pipelines → services → SDK)
│   ├── main.py                 # FastAPI app + lifespan + /health
│   ├── config.py               # pydantic-settings (Settings, get_settings)
│   ├── models.py               # Pydantic: WebhookPayload, UPDRecord, DownloadedFile
│   ├── deps.py                 # DI: verify_webhook_secret, service factories
│   ├── api/
│   │   └── upd_upload.py       # POST /webhook/yandex-form
│   ├── pipelines/
│   │   └── upd_upload.py       # process_upd(): download → vision → log
│   ├── services/               # Thin adapters around external SDKs
│   │   ├── files.py            # httpx download + PyMuPDF PDF → PNG
│   │   └── vision.py           # Claude Vision + tool-use → UPDRecord
│   └── core/
│       ├── logging.py          # structlog config + correlation-id contextvar
│       └── errors.py           # AppError taxonomy
├── tests/
│   ├── fixtures/upd/           # Real UPD screenshots as regression fixtures
│   ├── unit/                   # test_files.py, test_vision.py
│   └── integration/            # test_webhook.py (ASGITransport + dep overrides)
├── .ai-factory/                # Agent context (DESCRIPTION, ARCHITECTURE, PLAN)
├── .claude/skills/             # Installed + custom skills
├── .env.example
├── pyproject.toml              # uv
└── README.md
```

_Not yet created (future planned slices):_ `app/services/{sheets,drive}.py`, `app/api/reconciliation.py`, `Dockerfile`, `docker-compose.yml`.

## Key Entry Points

| File | Purpose |
|------|---------|
| `app/main.py` | FastAPI app + lifespan + `/health` |
| `app/api/upd_upload.py` | `POST /webhook/yandex-form` (thin handler) |
| `app/pipelines/upd_upload.py` | `process_upd()` — download → vision → log |
| `app/services/vision.py` | Claude Vision call with tool-use schema |
| `app/services/files.py` | httpx download + PyMuPDF PDF → PNG |
| `app/config.py` | Env-driven settings (ANTHROPIC_API_KEY, WEBHOOK_SECRET, LOG_*, ANTHROPIC_MODEL) |
| `.env.example` | Required env vars (no secrets) |

## Documentation

| Document | Path | Description |
|----------|------|-------------|
| Specification | .ai-factory/DESCRIPTION.md | Full project description, stack, scope |
| Architecture | .ai-factory/ARCHITECTURE.md | Patterns, folder rules, code examples (to be generated) |
| Reference — UPD sample 1 | Screenshot 2026-04-23 at 11.45.41 AM.png | Real UPD, счёт-фактура page |
| Reference — UPD sample 2 | Screenshot 2026-04-23 at 11.45.54 AM.png | Real UPD, транспортная накладная page |
| Reference — Original brief | тз бот.pdf | Client's original ТЗ (Russian) |

## AI Context Files

| File | Purpose |
|------|---------|
| AGENTS.md | This file — project structure map |
| .ai-factory/DESCRIPTION.md | Project specification and tech stack |
| .ai-factory/ARCHITECTURE.md | Architecture decisions and guidelines |

## Installed Skills

External (from skills.sh):
- `fastapi` — official FastAPI patterns
- `uv` — official astral guide for uv
- `gws-sheets` — Google Sheets read/write
- `gws-drive` — Google Drive file/folder management

Custom:
- `upd-vision-extraction` — Claude Vision + tool-use schema for UPD field extraction

AI Factory (general):
- `aif-*` — full suite (plan, implement, verify, dockerize, ci, etc.)

## MCP Servers

- `filesystem` — local file operations
