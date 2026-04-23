# Plan: Webhook + UPD Vision Extraction (MVP iteration)

**Mode:** Fast
**Created:** 2026-04-23
**Feature:** Принять webhook от Яндекс Формы → скачать файл УПД → извлечь 4 поля через Claude
Vision → записать результат в лог (JSON). Drive и Sheets подключаются отдельным планом.

## Settings

- **Testing:** yes (pytest + pytest-asyncio, фикстуры из реальных скриншотов УПД)
- **Logging:** verbose — structlog, JSON в prod, pretty в dev, correlation_id на весь pipeline
- **Docs:** README section с шаблоном настройки Я.Форм
- **Roadmap linkage:** none (ROADMAP.md не существует)

## Research Context

### Yandex Forms Business — webhook (краткая выжимка)

- **Интеграция "Отправить HTTP-запрос"** (Settings → Интеграции → Custom method). JSON body
  формирует пользователь, подставляя переменные формы — **контракт определяем мы**.
- **IPv6-only.** Я.Формы шлют запросы только из сети `2a02:6b8:c00::/40`. Сервер должен слушать
  IPv6. Railway поддерживает IPv6 из коробки (uvicorn `--host ::`).
- **Ретраи:** до 7 попыток за 30 минут на timeout/5XX/404. Успех — 200/201/202. Наш webhook
  обязан отвечать быстро и идемпотентно — обработка уходит в `BackgroundTasks`.
- **Файлы:** в Я.Формах хранятся 3 месяца. Для бизнес-форм рекомендуется подключить
  Object Storage (S3-совместимое Yandex Cloud) для долгосрочного хранения. В webhook передаём
  URL из переменной поля-файла.

### Контракт payload (наш дизайн, пользователь копирует в UI Я.Форм)

```json
{
  "foreman":      "{{переменная поля 'Выбор прораба'}}",
  "file_url":     "{{переменная поля 'Файл'}}",
  "file_name":    "{{имя файла}}",
  "submitted_at": "{{$system.submission_timestamp}}",
  "form_id":      "{{$system.form_id}}"
}
```

Headers: `Content-Type: application/json`, `X-Webhook-Secret: <value>` (общий секрет из `.env`).

### Архитектурные решения (из .ai-factory/ARCHITECTURE.md)

- Layered: `api → pipelines → services → SDK`. На этой итерации `services/{files,vision}.py`
  есть, `services/{sheets,drive}.py` — не трогаем.
- Сервисы инжектятся в pipeline параметрами (FastAPI Depends в handler → передаём в `bg.add_task`).
- Pydantic на каждой границе (payload, LLM tool-use, config).

## Tasks

### Phase 1 — Foundation (T1 → T3)

**T1. Инициализировать Python проект с uv** ✅
- `uv init --package`, Python 3.12, добавить deps (fastapi, uvicorn[standard], anthropic, httpx,
  pymupdf, pydantic, pydantic-settings, structlog, python-multipart); dev-deps (pytest,
  pytest-asyncio, pytest-mock, respx)
- Структура директорий по ARCHITECTURE.md: `app/{api,pipelines,services,core}/`,
  `tests/{unit,integration,fixtures/upd}/`
- Скопировать скриншоты УПД из корня в `tests/fixtures/upd/`

**T2. Настроить конфиг и логирование** *(blocked by T1)* ✅
- `app/config.py` — `Settings(anthropic_api_key, webhook_secret, log_level, anthropic_model,
  max_image_dpi)`, `get_settings()` с lru_cache
- `.env.example`
- `app/core/logging.py` — structlog config, JSON в prod, pretty в dev, `bind_correlation_id()`
- `app/core/errors.py` — `VisionExtractionError`, `FileDownloadError`, `UnsupportedFileTypeError`
- Verbose логи: старт приложения (настройки без секретов), каждый запрос

**T3. Определить Pydantic модели** *(blocked by T2)* ✅
- `app/models.py` — `WebhookPayload`, `UPDRecord`, `DownloadedFile`
- `model_config = ConfigDict(extra='ignore', str_strip_whitespace=True)`
- Validation error → WARN через structlog с path/msg

### Phase 2 — Services (T4 → T5)

**T4. Service для работы с файлами** *(blocked by T2, T3)* ✅
- `app/services/files.py`, класс `FilesService`
- `async download(url, max_bytes=25MB) -> bytes` через httpx.AsyncClient
- `async to_png(data, filename) -> DownloadedFile` — PDF → PyMuPDF page[0] → PNG при DPI из
  settings; passthrough для image/*
- Verbose: DEBUG url + content-length, кол-во страниц PDF, размер итогового PNG

**T5. Claude Vision extraction** *(blocked by T2, T3)* ✅
- `app/services/vision.py`, класс `VisionService`
- SYSTEM / EXTRACT_TOOL / tool_choice forcing — копируем из `.claude/skills/upd-vision-extraction/SKILL.md`
- `async extract(image_bytes, media_type) -> UPDRecord`
- Retry 3× (1s/2s/4s) только на `APIConnectionError` и `RateLimitError`
- Verbose: model/max_tokens, полный `UPDRecord` одной строкой, usage tokens

### Commit checkpoint #1

```
git commit -m "feat: project scaffolding, config, services for file download + Claude Vision extraction"
```

### Phase 3 — Orchestration (T6 → T8)

**T6. Pipeline** *(blocked by T4, T5)* ✅
- `app/pipelines/upd_upload.py::process_upd(payload, files, vision)`
- Последовательность: download → to_png → extract → `log.info("upd.extracted", **record.model_dump())`
- На exception: `log.exception` с таксономией из `core.errors`, graceful return
- Нет глобальных синглтонов — зависимости параметрами

**T7. Dependencies + FastAPI app** *(blocked by T2, T4, T5)* ✅
- `app/deps.py` — `verify_webhook_secret` (secrets.compare_digest), lru_cache-фабрики
  `get_anthropic_client`, `get_files_service`, `get_vision_service`
- `app/main.py` — FastAPI app + lifespan, регистрация router, `/health`
- Запуск `uvicorn app.main:app --host :: --port 8000` (IPv6)
- INFO на старте: routes + log_level

**T8. Webhook handler** *(blocked by T6, T7)* ✅
- `app/api/upd_upload.py` — `POST /webhook/yandex-form`
- Генерация correlation_id, `bg.add_task(process_upd, payload, files, vision)`, 200 с
  `{ok, correlation_id}`
- Никакой работы в handler — только валидация + постановка в фон
- Неверный secret → 401 (в dep), bad payload → 422 (автоматом)

### Commit checkpoint #2

```
git commit -m "feat: webhook endpoint, pipeline orchestration, DI wiring"
```

### Phase 4 — Tests (T9 → T10)

**T9. Unit-тесты services** *(blocked by T4, T5)* ✅
- `tests/conftest.py` — фикстуры upd_image_bytes, settings_override
- `tests/unit/test_files.py` — 5 тестов (download success/size-limit, to_png passthrough/pdf/unsupported)
- `tests/unit/test_vision.py` — 4 теста (tool-use message shape, parse response, missing tool_use, retry)
- Без реальных API-вызовов

**T10. Integration-тест webhook** *(blocked by T8)* ✅
- `tests/integration/test_webhook.py` — httpx.AsyncClient + ASGITransport
- Моки через `app.dependency_overrides`
- 5 сценариев: accept, missing secret, invalid secret, bad payload, background pipeline called

### Phase 5 — Docs (T11)

**T11. Yandex Forms integration notes (README)** ✅
- Пошаговая настройка webhook в UI Я.Форм
- JSON body template (плейсхолдеры переменных — пользователь подставит точные идентификаторы)
- Headers, URL, IPv6-требование
- Рекомендация подключить Object Storage для бизнес-форм
- Тест через webhook.site перед продакшн-подключением

### Commit checkpoint #3

```
git commit -m "test: unit + integration coverage, docs: Yandex Forms webhook setup"
```

## Manual QA (после T10)

1. Экспортировать `ANTHROPIC_API_KEY`, `WEBHOOK_SECRET`, запустить `uv run uvicorn app.main:app --host :: --port 8000`
2. Прогнать вручную через `curl` с `tests/fixtures/upd/*.png` как `file_url` (локальный HTTP-сервер через `python -m http.server`) — убедиться, что в логах появляется правильный `UPDRecord`
3. Если всё ок — зарегистрировать домен Railway, накатить `.env`, подключить реальную форму через webhook.site для проверки формата payload от Я.Форм, потом переключить URL на прод

## Out of Scope (следующие планы)

- Google Drive (папки `dd/mm/yyyy`, загрузка файла, возврат ссылки)
- Google Sheets (append + duplicate check по `upd_number`)
- Docker / Railway deployment (через `/aif-dockerize`)
- CI (через `/aif-ci`)
- Stage 2 — еженедельная сверка
