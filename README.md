# formsForBuh

Сервис для клиента «Выборг». На странице `/` бухгалтер выбирает прораба
(Юра / Гриша / Боря) и загружает УПД (PDF или фото). Бэкенд отдаёт документ
Claude Vision, извлекает четыре поля (`organization`, `date`, `amount`,
`upd_number`) и добавляет строку в Google-таблицу. Сразу после отправки
форма показывает результат: ссылку на таблицу при успехе, предупреждение
при частичном распознавании или ошибку с `correlation_id` для саппорта.

## Быстрый старт

```bash
cp .env.example .env          # заполнить ANTHROPIC_API_KEY, SHEET_ID, GOOGLE_CREDENTIALS_JSON
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- Форма: <http://localhost:8000/>
- Healthcheck: `curl http://localhost:8000/health` → `{"status":"ok"}`

## Подключение Google Sheets

1. **Создать таблицу.** Откройте Google Sheets, создайте новую таблицу.
   В первой строке листа 1 пропишите заголовки в этом порядке:

   | Организация | Дата | Сумма | Номер УПД | Прораб | Загружено | correlation_id |

2. **Скопировать `SHEET_ID`** из URL таблицы:
   `https://docs.google.com/spreadsheets/d/`**`<SHEET_ID>`**`/edit`.

3. **Создать Service Account** в Google Cloud Console:
   - IAM & Admin → Service Accounts → Create.
   - Keys → Add Key → Create new key → JSON → скачать файл.
   - Включить Google Sheets API в проекте.

4. **Расшарить таблицу** на email сервисного аккаунта (вид
   `name@project.iam.gserviceaccount.com`) с правами Editor.

5. **Положить JSON в `.env`** одной строкой (с экранированными `\n`
   внутри `private_key`). Пример формата — в `.env.example`.

## Переменные окружения

| Переменная | Описание |
|---|---|
| `ANTHROPIC_API_KEY` | Ключ Claude API |
| `SHEET_ID` | ID Google-таблицы |
| `GOOGLE_CREDENTIALS_JSON` | Service Account JSON (одной строкой) |
| `LOG_LEVEL` | `INFO` / `DEBUG` (default `INFO`) |
| `LOG_FORMAT` | `json` (prod) или `pretty` (dev) |
| `ANTHROPIC_MODEL` | По умолчанию `claude-sonnet-4-6` |
| `MAX_IMAGE_DPI` | DPI растеризации PDF (default 200) |
| `MAX_UPLOAD_BYTES` | Лимит размера файла (default 25 МБ) |
| `HTTP_TIMEOUT_SECONDS` | Таймаут httpx (default 30) |

## Структура

```
app/
├── api/upd_upload.py       — POST /api/upload (multipart)
├── pipelines/upd_upload.py — оркестрация: to_png → vision → sheets
├── services/
│   ├── files.py            — PyMuPDF PDF→PNG (httpx остался для будущего)
│   ├── vision.py           — Claude Vision tool-use
│   └── sheets.py           — gspread + google-auth (append-only)
├── core/
│   ├── logging.py          — structlog (JSON / pretty) + correlation-id
│   └── errors.py           — доменные ошибки
├── static/index.html       — кастомная HTML-форма (CSS/JS инлайн)
├── config.py               — pydantic-settings
├── models.py               — Foreman / UPDRecord / DownloadedFile / UploadResult
├── deps.py                 — фабрики сервисов
└── main.py                 — FastAPI + lifespan + StaticFiles
tests/
├── unit/                   — сервисы под моками
├── integration/            — endpoint через httpx ASGITransport
└── fixtures/upd/           — реальные скриншоты УПД
```

Подробнее: `.ai-factory/ARCHITECTURE.md`.

## Эндпоинты

- `GET /` — HTML-форма.
- `POST /api/upload` — multipart `foreman` (Юра/Гриша/Боря) + `file`
  (PDF / image). Возвращает `UploadResult`:

  ```json
  {
    "ok": true,
    "correlation_id": "abc...",
    "record": {"organization": "...", "date": "2026-04-22", "amount": 12345.67, "upd_number": "..."},
    "sheet_url": "https://docs.google.com/spreadsheets/d/.../edit",
    "needs_review": false,
    "missing_fields": null,
    "error": null
  }
  ```

- `GET /health` — `{"status":"ok"}`.

Ошибки валидации запроса (415 / 413 / 422) возвращают стандартный
FastAPI-ответ. Ошибки распознавания/записи приходят как HTTP 200
с `ok: false` и машиночитаемым `error` — фронт показывает баннер
с пояснением и `correlation_id`.

## Тесты

```bash
uv run pytest
uv run pytest tests/unit/test_sheets.py
uv run pytest -k upload
```

## Логи

`LOG_FORMAT=pretty` — читаемый цветной вывод локально.
`LOG_FORMAT=json` — одна JSON-строка на событие, подходит для Railway.
Каждое событие содержит `correlation_id` — удобно грепать по одной заявке.

## Что вне scope

- Stage 2 — еженедельная сверка (`app/api/reconciliation.py`,
  `app/pipelines/reconciliation.py`) — запланировано в `.ai-factory/PLAN.md`.
- Загрузка оригиналов файлов в Drive: пока только Sheets-строка.
