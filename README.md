# formsForBuh

Сервис для клиента «Выборг». На странице `/` бухгалтер выбирает прораба
(Юра / Гриша / Боря) и загружает УПД (PDF или фото). Бэкенд отдаёт документ
Claude Vision, извлекает четыре поля (`organization`, `date`, `amount`,
`upd_number`) и добавляет строку в Google-таблицу. Сразу после отправки
форма показывает результат: ссылку на таблицу при успехе, предупреждение
при частичном распознавании или ошибку с `correlation_id` для саппорта.

Вторая вкладка «Сводка» — еженедельная сверка с реестром 1С. Бухгалтер
выгружает «Реестр документов "Поступление (акт, накладная, УПД)"»
(`.xls` / `.xlsx` / `.csv`), бэкенд парсит реестр, читает таблицу прорабов
из того же Google Sheet и возвращает три списка (не загружено прорабами /
дубликаты в таблице / лишние записи) плюс сводную статистику. Каждый
непустой список имеет кнопку «Скопировать список» в формате
`№X от dd.mm.yyyy на 12 345 ₽`.

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
├── api/
│   ├── upd_upload.py        — POST /api/upload (multipart)
│   └── reconciliation.py    — POST /api/reconciliation (multipart)
├── pipelines/
│   ├── upd_upload.py        — оркестрация: to_png → vision → sheets
│   └── reconciliation.py    — onec.parse → sheets.read → diff
├── services/
│   ├── files.py             — PyMuPDF PDF→PNG (httpx остался для будущего)
│   ├── vision.py            — Claude Vision tool-use
│   ├── sheets.py            — gspread + google-auth (append + read)
│   └── onec.py              — xlrd / openpyxl / csv — парсер выгрузки 1С
├── core/
│   ├── logging.py           — structlog (JSON / pretty) + correlation-id
│   └── errors.py            — доменные ошибки
├── static/index.html        — две вкладки: «Загрузка УПД» и «Сводка»
├── config.py                — pydantic-settings
├── models.py                — DTO загрузки + DTO сверки (OneCRecord, MissingUPD, …)
├── deps.py                  — фабрики сервисов
└── main.py                  — FastAPI + lifespan + StaticFiles
tests/
├── unit/                    — сервисы и pipeline под моками
├── integration/             — endpoints через httpx ASGITransport
└── fixtures/
    ├── upd/                 — реальные скриншоты УПД
    └── onec/sample.xls      — реальная выгрузка 1С (для регрессии парсера)
```

Подробнее: `.ai-factory/ARCHITECTURE.md`.

## Эндпоинты

- `GET /` — HTML-форма с двумя вкладками.
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

- `POST /api/reconciliation` — multipart `file` (`.xls` / `.xlsx` / `.csv`).
  Возвращает `ReconciliationResult`:

  ```json
  {
    "ok": true,
    "correlation_id": "abc...",
    "missing": [{"upd_number": "...", "date": "2026-02-05", "amount": 21324.0, "organization": "...", "source_row": 7}],
    "duplicates": [{"upd_number": "...", "count": 2, "foremen": ["Юра", "Гриша"], "dates": ["2026-02-05", "2026-02-05"]}],
    "extras": [{"upd_number": "...", "foreman": "Боря", "date": "2026-04-15"}],
    "stats": {"onec_total": 45, "foreman_total": 38, "matched": 30, "missing": 15, "duplicates": 1, "extras": 8, "coverage_percent": 66.7},
    "error": null
  }
  ```

- `GET /health` — `{"status":"ok"}`.

### Сверка с 1С

Ожидаемая колоночная структура выгрузки (1С формирует её автоматически):

| `№ п/п` | `Дата` | `Документ` | `Номер` | `Дата вх.` | `Номер вх.` | `Сумма` | `Информация` |

В сравнении используется **`Номер вх.`** (исходный номер УПД от поставщика —
именно его Claude Vision вытягивает из шапки документа). Внутренний
номер 1С (`Номер`) игнорируется. Нормализация перед сравнением: lowercase,
обрезка пробелов, удаление пробелов внутри, обрезка ведущих нулей.

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

- Хранение истории сверок (БД/файл).
- Группировка missing по контрагентам/прорабам — пока показываем плоский список (организация — колонка таблицы).
- Авто-уведомления прорабам (Telegram/email): план копируется бухгалтером вручную.
- Загрузка оригиналов файлов в Drive: пока только Sheets-строка (`services/drive.py` запланирован отдельным PR).
