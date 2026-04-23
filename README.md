# formsForBuh

Webhook-сервис для клиента Выборг. Принимает submission из Яндекс Формы (выбор прораба +
загруженный файл УПД), извлекает через Claude Vision четыре поля
(`organization`, `date`, `amount`, `upd_number`) и пишет результат в лог. Google Drive и
Sheets подключаются отдельным планом (Stage 1.5).

## Быстрый старт

```bash
cp .env.example .env          # заполнить ANTHROPIC_API_KEY и WEBHOOK_SECRET
uv sync
uv run uvicorn app.main:app --host :: --port 8000    # IPv6 обязателен для Яндекс Форм
```

Проверить, что сервер поднялся:

```bash
curl -s http://[::1]:8000/health
# {"status":"ok"}
```

## Структура

```
app/
├── api/upd_upload.py       — POST /webhook/yandex-form
├── pipelines/upd_upload.py — оркестрация: download → to_png → extract → log
├── services/
│   ├── files.py            — httpx download + PyMuPDF PDF→PNG
│   └── vision.py           — Claude Vision (tool-use) для извлечения полей
├── core/
│   ├── logging.py          — structlog (JSON prod / pretty dev) + correlation-id
│   └── errors.py           — доменная таксономия ошибок
├── config.py               — pydantic-settings
├── models.py               — WebhookPayload / UPDRecord / DownloadedFile
├── deps.py                 — DI (секрет, фабрики сервисов)
└── main.py                 — FastAPI + lifespan
tests/
├── unit/                   — сервисы под моками (respx, mock)
├── integration/            — webhook через httpx ASGITransport
└── fixtures/upd/           — реальные скриншоты УПД
```

Подробнее: `.ai-factory/ARCHITECTURE.md`.

## Тесты

```bash
uv run pytest
```

## Настройка webhook в Яндекс Формах (бизнес)

Интеграция "Отправить HTTP-запрос" в Я.Формах передаёт произвольный JSON на указанный URL.
Контракт payload определяем мы — ниже готовый шаблон, который пользователь копирует в UI.

### 1. Зарегистрировать публичный URL

Наш сервис должен слушать **IPv6** (Яндекс Формы шлют запросы только из сети
`2a02:6b8:c00::/40`). На Railway это из коробки: `uvicorn --host ::` достаточно.

Финальный URL: `https://<ваш-домен-railway>/webhook/yandex-form`.

### 2. В форме → Настройки → Интеграции → "Отправить HTTP-запрос"

- **Метод:** `POST`
- **URL:** `https://<ваш-домен-railway>/webhook/yandex-form`
- **Headers:**
  - `Content-Type: application/json`
  - `X-Webhook-Secret: <значение WEBHOOK_SECRET из .env>`
- **Body (JSON):**

```json
{
  "foreman":      "{{переменная поля 'Выбор прораба'}}",
  "file_url":     "{{переменная поля 'Файл'}}",
  "file_name":    "{{имя файла}}",
  "submitted_at": "{{$system.submission_timestamp}}",
  "form_id":      "{{$system.form_id}}"
}
```

Плейсхолдеры `{{…}}` — UI Я.Форм предложит автокомплит по существующим переменным формы.
Название переменных у каждой формы своё, подставьте точные идентификаторы.

### 3. Проверка перед продакшеном

1. Поставить в поле URL временный адрес с `https://webhook.site`.
2. Отправить тестовую заявку — убедиться, что прилетает JSON нужной формы
   (все 4 пользовательских поля + 2 системных).
3. Если ок, переключить URL на прод-домен сервиса.

### 4. Поведение при сбоях

Я.Формы делают до **7 ретраев за 30 минут** на `timeout / 5XX / 404`. Успешный ответ — коды
`200/201/202`. Наш webhook всегда отвечает быстро (валидация + постановка в фон), поэтому
ретраев быть не должно при штатной работе.

### 5. Долгосрочное хранение файлов

Файлы в Я.Формах живут только **3 месяца**. Для бизнес-форм рекомендуется подключить
**Yandex Object Storage** (S3-совместимое) — тогда `file_url` в payload указывает на
долговременный бакет, а не на временный URL Я.Форм. Делается в той же вкладке "Интеграции".

## Логи

`LOG_FORMAT=pretty` — читаемый цветной вывод для локальной разработки.
`LOG_FORMAT=json` — одна JSON-строка на событие, подходит для Railway и аггрегаторов.

Каждое событие пайплайна содержит `correlation_id`, он же возвращается в ответе webhook —
удобно грепать по одной заявке.

## Out of scope (следующие планы)

- Google Drive: папки `dd/mm/yyyy`, загрузка файла, возврат ссылки.
- Google Sheets: append + duplicate check по `upd_number`.
- Docker / Railway deploy (`/aif-dockerize`).
- CI (`/aif-ci`).
- Stage 2 — еженедельная сверка.
