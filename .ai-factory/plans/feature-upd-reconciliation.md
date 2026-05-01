# Plan: Сверка УПД с выгрузкой 1С

**Mode:** Full
**Branch:** `feature/upd-reconciliation`
**Created:** 2026-04-29
**Feature:** Раз в неделю бухгалтер выгружает реестр УПД из 1С (xls/xlsx/csv), загружает файл во вкладку «Сводка» нового UI; backend парсит реестр, читает таблицу прорабов из Google Sheets, нормализует номера и возвращает три списка (не загружено прорабами / дубликаты / лишние) + сводную статистику. У бухгалтера есть кнопка «Скопировать список» для отправки прорабам.

## Settings

- **Testing:** yes — unit (парсер 1С + diff-логика + sheets.read) + integration (`/api/reconciliation`).
- **Logging:** verbose — DEBUG на нормализацию каждого ключа, INFO на старт/итог, WARNING на пропуск строк, exception() в catch-блоках; structlog с `bind_correlation_id`.
- **Docs:** README.md (раздел «Сверка с 1С») + CLAUDE.md (Request flow, Key conventions — добавить SDK-границы для openpyxl/xlrd).
- **Roadmap linkage:** none (`.ai-factory/ROADMAP.md` не существует).

## Research Context

### Формат 1С-выгрузки (`Реестр документов "Поступление (акт, накладная, УПД)"`)

Изучен реальный пример — `Реестр документов  Поступление (акт_ накладная_ УПД) Исмаилов.xls` (51×8, .xls):

- Строки 0–4 — meta: организация (ИП Исмаилов К. Н.), период, фильтр по контрагенту.
- Строка 5 — header: `№ п/п | Дата | Документ | Номер | Дата вх. | Номер вх. | Сумма | Информация`.
- Строки 6+ — записи. Колонки для нашей задачи: **`Номер вх.`** (исходный номер УПД от поставщика — поле сравнения), `Дата` или `Дата вх.`, `Сумма`, `Информация` (контрагент).
- Конец данных — пустая строка или строка «Итого».

`Номер` в 1С — это внутренний номер документа в учётной системе, **не используется** в сравнении. Сравнение ведётся по `Номер вх.` ↔ `upd_number` из таблицы прорабов (то, что Claude Vision вытащил с самого УПД).

### Текущая архитектура (см. `CLAUDE.md`, `.ai-factory/ARCHITECTURE.md`)

- Layered: `api/ → pipelines/ → services/ → external SDKs`. `services/sheets.py` — единственное место где импортируется gspread.
- `app/main.py` mount-ит StaticFiles на `/` ПОСЛЕ `include_router(...)`, поэтому новые `/api/*` маршруты автоматически выигрывают диспатч.
- Pydantic на каждой границе. Сервисы инжектятся через `Depends`. Тесты переопределяют через `app.dependency_overrides`.
- Stage 2 (reconciliation) изначально предусмотрен как `app/api/reconciliation.py` + `app/pipelines/reconciliation.py` — план аккуратно ложится в существующий каркас.

### Архитектурные решения

- Новый сервис **`app/services/onec.py`** — единственное место в проекте, импортирующее `openpyxl`, `xlrd` и stdlib `csv`. xlrd>=2.0 поддерживает только `.xls` (что нам и нужно), для `.xlsx` идёт через openpyxl.
- Расширить **`app/services/sheets.py`** методом `read_all_records()` (асинхронная обёртка над `worksheet.get_all_values`), не трогать append-логику.
- Новый pipeline **`app/pipelines/reconciliation.py::reconcile`** работает синхронно (как `process_upd`): UI ждёт ответ — кнопка показывает spinner.
- Нормализация номера УПД: `lower → strip → remove_spaces → strip_leading_zeros`. Этого достаточно для одного контрагента (а пример выгрузки именно такой). Если впоследствии захочется (организация, номер) или fuzzy match — это отдельная итерация.
- Новый endpoint **POST `/api/reconciliation`** принимает `multipart/form-data` с одним полем `file`. Без авторизации (форма публичная, как и `/api/upload`).
- Frontend: вкладки в одном `index.html` (минимум кода, общий CSS). Без бандлеров, vanilla JS, как сейчас.

### Контракт ответа `POST /api/reconciliation`

```json
{
  "ok": true,
  "correlation_id": "abc123…",
  "missing": [
    {"upd_number": "6022461056", "date": "2026-02-05", "amount": 21324.0,
     "organization": "СТРОИТЕЛЬНЫЙ ДВОР ООО", "source_row": 6}
  ],
  "duplicates": [
    {"upd_number": "6022461056", "count": 2, "foremen": ["Юра", "Гриша"], "dates": ["2026-02-05", "2026-02-05"]}
  ],
  "extras": [
    {"upd_number": "999", "foreman": "Боря", "date": "2026-04-15"}
  ],
  "stats": {"onec_total": 45, "foreman_total": 38, "matched": 30, "missing": 15, "duplicates": 1, "extras": 8, "coverage_percent": 66.7}
}
```

При ошибке: `{"ok": false, "correlation_id": "...", "error": "onec_parse_error" | "sheets_read_error" | "app_error" | "unexpected_error"}`.

## Tasks

### Phase 1 — Фундамент (T1 → T3)

**T1. Зависимости и фикстура** *(no deps)* ✅
- [x] В `pyproject.toml` (project.dependencies): `openpyxl>=3.1.0`, `xlrd==2.0.1`. `uv sync`.
- [x] Скопировать `Реестр документов  Поступление (акт_ накладная_ УПД) Исмаилов.xls` в `tests/fixtures/onec/sample.xls` (создать директорию).

**T2. Расширить error taxonomy** *(blocked by T1)* ✅
- [x] В `app/core/errors.py`: `OneCParseError(AppError)`, `SheetsReadError(AppError)` с однострочным docstring.

**T3. Pydantic-модели для сверки** *(blocked by T2)* ✅
- [x] В `app/models.py` добавить: `OneCRecord`, `SheetUPDRow`, `MissingUPD`, `DuplicateUPD`, `ExtraUPD`, `ReconciliationStats`, `ReconciliationResult` (всё от `_Base`, с осмысленными `Field(description=...)`).
- [x] `ReconciliationResult.error` — `str | None`, такой же стиль как у `UploadResult`.

### Phase 2 — Сервисы (T4 → T5)

**T4. `app/services/onec.py` — парсер 1С** *(blocked by T1, T2, T3)* ✅
- [x] Класс `OneCParserService` (stateless). Метод `parse(data: bytes, filename: str) -> list[OneCRecord]`.
- [x] Ветка по расширению: `.xls` → xlrd, `.xlsx` → openpyxl, `.csv` → stdlib csv c sniffer.
- [x] Auto-header detection + column mapping (`Номер вх.` / `Дата (вх.)` / `Сумма` / `Информация`).
- [x] Skip rows without upd_number, stop on `Итого` / signature block / blank.
- [x] xlrd-даты через `xlrd.xldate.xldate_as_datetime`; verbose structlog логи (start / header_found / row / skip_row / done).

**T5. `SheetsService.read_all_records`** *(blocked by T2, T3)* ✅
- [x] В `app/services/sheets.py`: `async def read_all_records(self) -> list[SheetUPDRow]` + sync impl через `worksheet.get_all_values()`.
- [x] Фильтрация: пропускать header (строка 0) и строки с пустым upd_number. gspread.APIError → `SheetsReadError`.
- [x] Verbose-логи (start/row/skip_row/done) + парсинг даты/суммы из строк ячеек.

### Commit checkpoint #1

```
git add app/core/errors.py app/models.py app/services/onec.py app/services/sheets.py pyproject.toml uv.lock tests/fixtures/onec/
git commit -m "feat(reconciliation): models, error types, 1C parser service, sheets read method"
```

### Phase 3 — Оркестрация (T6 → T8)

**T6. `app/pipelines/reconciliation.py`** *(blocked by T4, T5)* ✅
- [x] `async def reconcile(...) -> ReconciliationResult` с try/except (parse/read/app/unexpected).
- [x] `_normalize` (lower / strip / drop spaces / strip leading zeros) + verbose структурное логирование.
- [x] Diff: missing / duplicates / extras + `ReconciliationStats` (matched / coverage_percent).

**T7. DI: `get_onec_parser_service`** *(blocked by T4)* ✅
- [x] В `app/deps.py`: per-request фабрика `def get_onec_parser_service() -> OneCParserService` (без lru_cache).

**T8. API endpoint POST `/api/reconciliation`** *(blocked by T6, T7)* ✅
- [x] `app/api/reconciliation.py` + router зарегистрирован в `app/main.py` до static mount.
- [x] Валидация filename / расширения / content-type / size / empty.
- [x] correlation_id, `result.model_dump(mode="json")`, structured logs.

### Commit checkpoint #2

```
git add app/pipelines/reconciliation.py app/api/reconciliation.py app/deps.py app/main.py
git commit -m "feat(reconciliation): pipeline + endpoint POST /api/reconciliation"
```

### Phase 4 — UI (T9)

**T9. Вкладки + UI сводки в `index.html`** *(blocked by T8)* ✅
- [x] Tab-bar (`.tab-button`) + два `<section>` (`#tab-upload`, `#tab-reconciliation`).
- [x] Reconciliation form (.xls/.xlsx/.csv) + result area со `.stats-grid` и тремя секциями `.section-list` (missing/duplicates/extras).
- [x] `.copy-btn` для каждой непустой секции, формат «№X от dd.mm.yyyy на 12 345 ₽».
- [x] `copyToClipboard` с fallback на `<textarea>` + `execCommand("copy")`.
- [x] `ERROR_MESSAGES` расширен ключами `onec_parse_error`, `sheets_read_error`.
- [x] Success banner «Все УПД на месте» при пустых трёх списках.
- [x] max-width 640px и медиа-брейкпоинт под таблицы.

### Commit checkpoint #3

```
git add app/static/index.html
git commit -m "feat(reconciliation): tabbed UI with summary view + copy-to-clipboard"
```

### Phase 5 — Тесты (T10 → T12)

**T10. Unit-тесты парсера 1С** *(blocked by T1, T4)* ✅
- [x] `tests/unit/test_onec.py`: real fixture .xls, synthetic .xlsx (Итого), csv `;`, csv с BOM, missing columns → OneCParseError, empty → OneCParseError, .pdf → OneCParseError, skip rows без upd_number.

**T11. Unit-тесты diff и Sheets.read** *(blocked by T5, T6)* ✅
- [x] `tests/unit/test_reconciliation_pipeline.py`: perfect_match / missing / duplicates / extras / normalization / parse-error / read-error.
- [x] `tests/unit/test_sheets.py` дополнен `read_all_records` happy + skip empty + APIError → SheetsReadError.

**T12. Integration-тест endpoint** *(blocked by T8)* ✅
- [x] `tests/integration/test_reconciliation_api.py`: accept .xls (real fixture), 415, 413, 422, parse_error, read_error.

### Commit checkpoint #4

```
git add tests/
git commit -m "test(reconciliation): unit + integration coverage for parser, pipeline, endpoint"
```

### Phase 6 — Docs + QA (T13 → T14)

**T13. Обновить README.md и CLAUDE.md** *(blocked by T9, T12)* ✅
- [x] README: «Сверка с 1С» (структура колонок 1С + контракт ответа), endpoint `/api/reconciliation`, обновлены «Структура» и «Что вне scope».
- [x] CLAUDE.md: добавлен Request flow для reconciliation, расширены Key conventions (xlrd/openpyxl boundaries, 1С comparison key), Stage 2 status обновлён.

**T14. Manual QA в браузере** *(blocked by T9, T12)* ✅ (smoke)
- [x] Smoke с тестовыми env: `/health` 200, `/` отдаёт HTML с обеими вкладками, `POST /api/reconciliation` → 422 (empty), 415 (.pdf), реальный .xls парсится и pipeline возвращает `ok=false` (потому что фейковые Google-credentials).
- [ ] **Полная проверка в браузере с настоящими секретами остаётся за пользователем** — переключение вкладок, загрузка sample.xls с реальной таблицей, кнопка «Скопировать список», smoke оригинальной формы. Для этого: `cp .env.example .env`, заполнить `ANTHROPIC_API_KEY` / `SHEET_ID` / `GOOGLE_CREDENTIALS_JSON`, `uv run uvicorn app.main:app --port 8000`, открыть <http://localhost:8000/>.

### Commit checkpoint #5 (финальный)

```
git add README.md CLAUDE.md
git commit -m "docs(reconciliation): README section + CLAUDE.md updates"
```

## Out of Scope

- Хранение истории сверок (БД/файлы).
- Группировка missing по контрагентам/прорабам — пока показываем плоский список (организация — колонка таблицы).
- Авто-уведомления прорабам (Telegram, email) — план вручную копируется бухгалтером.
- Drive (`services/drive.py`, `dd/mm/yyyy` папки) — отдельный план.
- Авторизация на новый эндпоинт — публичный, как `/api/upload`. Добавить можно отдельным PR с одним декоратором deps.
