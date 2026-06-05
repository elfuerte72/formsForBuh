# formsForBuh

Сервис для клиента «Выборг». На странице `/` бухгалтер выбирает прораба
(Юра / Гриша / Боря) и загружает УПД (PDF или фото). Бэкенд отдаёт документ
Claude Vision, извлекает четыре поля (`organization`, `date`, `amount`,
`upd_number`) и добавляет строку в Google-таблицу. Сразу после отправки
форма показывает результат: ссылку на таблицу при успехе, предупреждение
при частичном распознавании или ошибку с `correlation_id` для саппорта.

Оригинал каждого УПД может архивироваться в Google Drive (по дате,
`<папка>/<dd.mm.yyyy>/`), а кликабельная ссылка на скан пишется в колонку
«Файл» — чтобы спорный номер можно было проверить глазами. Архивация за
фича-флагом `DRIVE_ENABLED` (по умолчанию выключена).

Вторая вкладка «Сводка» — еженедельная сверка с реестром 1С. Бухгалтер
выгружает «Реестр документов "Поступление (акт, накладная, УПД)"»
(`.xls` / `.xlsx` / `.csv`), бэкенд парсит реестр, читает зелёные строки
прорабов из того же Google Sheet и **аннотирует таблицу на месте, ничего
не затирая**: рядом с каждой зелёной строкой дописывает жёлтый блок из 1С
и статус, недостающие записи 1С добавляет снизу. Side-by-side формат: слева
жёлтый блок из 1С, справа зелёный блок из загрузок бригадиров, статус
`OK / СУММА? / NO / ЛИШНЕЕ`. **История и ручные статусы сохраняются** —
авто-статусы помечаются суффиксом `·авто` и обновляются каждую сверку,
а статус без суффикса считается ручным и не перетирается. На самой
странице остаётся короткий баннер «Готово» и числа: `Совпало` /
`Не хватает` / `Лишние` / `Сумма ≠`. Подробности расхождений — в таблице.

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
   В первой строке листа 1 пропишите 13 заголовков в этом порядке:

   | Дата (1С) | Контрагент (1С) | Сумма (1С) | № УПД (1С) | № УПД | Дата | Сумма | Контрагент | Организация | Прораб | Дата загрузки | Статус | Файл |

   Левые четыре (A–D) — жёлтый блок (заполняется из реестра 1С на сводке).
   Колонки E–K — зелёный блок (заполняется бригадирами через форму).
   Колонка L — статус: `OK` (есть в обоих), `СУММА?` (номер совпал, сумма
   нет), `NO` (есть в 1С, не загружено) или `ЛИШНЕЕ` (загружено, но в
   реестре нет); авто-статусы помечаются суффиксом `·авто`, ручные — без него.
   Колонка M — «Файл»: ссылка на скан в Drive (если включён `DRIVE_ENABLED`).
   Цвет фона колонок проставляет бэкенд автоматически после первой сводки.

   > **Миграция схемы 12 → 13.** Если таблица уже использовалась со старыми
   > 12 заголовками — допишите 13-й заголовок «Файл» в колонку M. Данные
   > трогать не нужно: сверка теперь аннотирует строки на месте, ничего не
   > затирая, а новые загрузки сразу пишут ссылку в M.

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
| `RECONCILIATION_ENABLED` | Вкладка «Сводка» + эндпоинты сверки (default `false`) |
| `DRIVE_ENABLED` | Архивация скана в Google Drive (default `false`) |
| `DRIVE_FOLDER_ID` | ID папки в вашем Drive, куда складывать сканы |
| `DRIVE_OAUTH_CLIENT_ID` / `DRIVE_OAUTH_CLIENT_SECRET` / `DRIVE_OAUTH_REFRESH_TOKEN` | OAuth-доступ для заливки (см. ниже) |

### Архивация сканов в Google Drive

**Почему OAuth, а не сервис-аккаунт.** У сервис-аккаунта **нет собственной
дисковой квоты**: любой файл, который он *создаёт*, отклоняется с ошибкой
`storageQuotaExceeded` — расшаривание папки не помогает, потому что новый
файл принадлежит сервис-аккаунту, а не владельцу папки. Поэтому заливка идёт
**от вашего личного Google-аккаунта** по OAuth refresh-токену: файлы
оказываются в вашем Drive, на вашей квоте. Сервис-аккаунт по-прежнему пишет
в Sheets (он только *редактирует* вашу таблицу — новый файл не создаётся).

**Разовая настройка:**

1. В Google Cloud Console (тот же проект, что и сервис-аккаунт): включите
   **Google Drive API**; создайте **OAuth client ID** типа **Desktop app**,
   скачайте JSON; на экране согласия добавьте свой Google-аккаунт в
   **Test users**.
2. Создайте/выберите папку в своём Drive, скопируйте её id из URL в
   `DRIVE_FOLDER_ID`.
3. Получите refresh-токен (нужен браузер на машине):

   ```bash
   uv run python scripts/drive_authorize.py path/to/client_secret.json
   ```

   Скрипт откроет браузер, попросит доступ к вашему аккаунту и напечатает
   `DRIVE_OAUTH_CLIENT_ID` / `DRIVE_OAUTH_CLIENT_SECRET` /
   `DRIVE_OAUTH_REFRESH_TOKEN` — впишите их в `.env`.
4. Поставьте `DRIVE_ENABLED=true`.

Внутри `DRIVE_FOLDER_ID` создаются подпапки по дате `dd.mm.yyyy`, ссылка
(`webViewLink`) пишется в колонку «Файл» (M). Используется scope
`https://www.googleapis.com/auth/drive` (нужен, чтобы писать в уже
существующую вашу папку). Сбой Drive **мягкий**: строка в таблицу всё равно
пишется (без ссылки), форма показывает предупреждение, аплоад не падает.

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
│   ├── sheets.py            — gspread + google-auth (append + неразрушающая аннотация)
│   ├── drive.py             — google-api-python-client — архивация скана + ссылка
│   └── onec.py              — xlrd / openpyxl / csv — парсер выгрузки 1С
├── core/
│   ├── logging.py           — structlog (JSON / pretty) + correlation-id
│   └── errors.py            — доменные ошибки
├── static/index.html        — две вкладки: «Загрузка УПД» и «Сводка»
├── config.py                — pydantic-settings
├── models.py                — DTO загрузки + DTO сверки (OneCRecord, ReconRow, …)
├── deps.py                  — фабрики сервисов
└── main.py                  — FastAPI + lifespan + StaticFiles
tests/
├── unit/                    — сервисы и pipeline под моками
├── integration/             — endpoints через httpx ASGITransport
└── fixtures/
    ├── upd/                 — реальные скриншоты УПД
    └── onec/sample.xls      — реальная выгрузка 1С (для регрессии парсера)
```

Подробнее об архитектуре и соглашениях: `CLAUDE.md`.

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
  Полная сводка пишется в Google Sheet; в ответе только короткая
  статистика:

  ```json
  {
    "ok": true,
    "correlation_id": "abc...",
    "stats": {"matched": 30, "missing": 15, "extras": 8, "amount_mismatch": 2},
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

- Отдельная БД: система записи — Google-таблица; сканы — Google Drive.
- Авто-уведомления прорабам (Telegram/email): сводка лежит в Google Sheet,
  ссылка рассылается бухгалтером вручную.
- Мультиорганизационная сверка: ключ сравнения сейчас — только
  `_normalize(№ УПД)` (один контрагент). Ключ `(организация, номер)` —
  отдельная итерация.
