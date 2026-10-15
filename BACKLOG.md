# Backlog — задачи для разработки бота

Бэклог для Claude Code. Задачи упорядочены по приоритету и зависимостям. Рекомендуется брать сверху вниз.

## Этап 1 — Инфраструктура (~2 дня)

- [ ] **T-001** · Инициализировать Python-проект: venv, `requirements.txt`, `.env.example`, `.gitignore`. Проверить что `pip install -r requirements.txt` проходит без ошибок.
- [ ] **T-002** · `app/config.py` — pydantic-settings, загрузка `.env`. Обязательные поля: `B24_DOMAIN`, `B24_ACCESS_TOKEN`, `B24_BOT_ID`, `YC_API_KEY`, `YC_FOLDER_ID`, `WEBHOOK_PATH`, `WEBHOOK_SECRET`, `DB_PATH`.
- [ ] **T-003** · `app/main.py` — FastAPI, endpoint `GET /health` → `{"status":"ok"}`, endpoint `POST /b24/webhook` → пока просто принимает JSON и логирует.
- [ ] **T-004** · `app/models.py` — pydantic-модели для B24-событий: `B24Event`, `MessageAddPayload`, `BotJoinPayload`. Модели должны парсить реальный JSON из документации Bitrix24.
- [ ] **T-005** · `app/services/logger.py` + SQLAlchemy async — создание таблицы `events`, функция `log_event(dialog_id, direction, message, meta)`. Выполнять автоинициализацию БД при старте.

## Этап 2 — Интеграция с Bitrix24 (~2 дня)

- [ ] **T-006** · `app/services/bitrix.py` — async REST-клиент B24. Методы: `send_message(dialog_id, text)`, `leave_chat(dialog_id)`, `register_bot(payload)`, `get_chat_history(dialog_id, limit=10)`. Все через httpx с retry 2-3 раз.
- [ ] **T-007** · `scripts/register_bot.py` — одноразовая регистрация бота в B24. Читает из `.env`, вызывает `imbot.register` с параметрами: `CODE=avito_assistant`, `TYPE=O`, `EVENT_HANDLER=<APP_HOST>/b24/webhook`, имя «Авито-ассистент». Выводит полученный `BOT_ID` — пользователь сохраняет в `.env`.
- [ ] **T-008** · `app/main.py` — валидация подписи события (заголовок `X-Bitrix24-Signature` с HMAC от тела через `WEBHOOK_SECRET`). Если подпись невалидна — 401.
- [ ] **T-009** · Первый end-to-end тест: бот получает `ONIMBOTMESSAGEADD`, отвечает эхом «Получил: <текст>» через `imbot.message.add`. Тестируется на реальной открытой линии в Битриксе.

## Этап 3 — LLM и system prompt (~2 дня)

- [ ] **T-010** · `app/services/llm.py` — async YandexGPT-клиент. Метод `generate(system_prompt: str, messages: list[dict]) -> str`. Параметры: `temperature=0.3`, `maxTokens=200`, `stream=false`. Обработка ошибок (таймаут, 429, 5xx) с retry.
- [ ] **T-011** · `app/services/prompt_builder.py` — читает `app/prompts/system_prompt.md`, `app/data/pilot_skus.json`, `app/data/faq_extras.md`. Подставляет переменные (`{{ SKUS_TABLE }}`, `{{ UPDATED_DATE }}`). Кэш в памяти, reload по SIGHUP.
- [ ] **T-012** · `app/handlers/message.py` — главный обработчик `ONIMBOTMESSAGEADD`. Логика: проверить что клиент (не оператор), собрать историю диалога, вызвать LLM с system prompt, распарсить ответ на `ESCALATE:` маркер, вернуть готовый ответ или эскалацию.
- [ ] **T-013** · Тест с реальным клиентом: задать 5 типовых вопросов (наличие, доставка, самовывоз, скидка, жалоба), проверить что LLM отвечает адекватно и с маркером ESCALATE где надо.

## Этап 4 — Фильтры и триггеры (~1.5 дня)

- [ ] **T-014** · `app/services/filters.py` — модуль `post_filter(text: str) -> tuple[bool, str | None]`. Regex-паттерны из SPEC.md §4. Возвращает `(True, None)` если ок или `(False, "reason")` если заблокировано.
- [ ] **T-015** · `app/services/triggers.py` — модуль `check_hard_triggers(text: str) -> str | None`. Паттерны из SPEC.md §2. Возвращает причину эскалации или None.
- [ ] **T-016** · Интеграция filters + triggers в `handlers/message.py`. Приоритет: hard_triggers → LLM → post_filter. Все блокировки логировать.
- [ ] **T-017** · `tests/test_filters.py` — 15+ тестов на post_filter: телефоны, email, WhatsApp-ссылки, скидки в %, конкретные даты, внешние ссылки. Оба: ложные срабатывания и пропуски.
- [ ] **T-018** · `tests/test_triggers.py` — 10+ тестов на hard-triggers: жалобы, возвраты, запросы менеджера.

## Этап 5 — Эскалация и контроль менеджера (~1 день)

- [ ] **T-019** · Логика эскалации в `handlers/message.py`: при триггере или `ESCALATE:` маркере — отправить клиенту «Передаю менеджеру», вызвать `leave_chat`, записать в events с `direction=escalate` и причиной.
- [ ] **T-020** · Обработка события `ONIMBOTDELETE` — бот помечает диалог как «operator_led» в БД, больше не отвечает.
- [ ] **T-021** · Обработка случая, когда оператор пишет в чат одновременно с ботом: в `handlers/message.py` проверять последние N сообщений в истории — если есть ответ не-бота за последние 60 сек, бот молчит.

## Этап 6 — Тесты и деплой (~2 дня)

- [ ] **T-022** · `tests/test_e2e.py` — эмуляция вебхуков B24 через httpx.AsyncClient. Проверить полный сценарий: входящее → LLM (mock) → ответ. Сценарий эскалации.
- [ ] **T-023** · Dockerfile (multi-stage: builder + runtime на python:3.11-slim). Entry point: `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
- [ ] **T-024** · systemd unit `deploy/avito-bot.service` для запуска вне Docker (если не нужен контейнер).
- [ ] **T-025** · nginx-конфиг `deploy/nginx.conf.example` — reverse proxy на 8000, HTTPS через certbot.
- [ ] **T-026** · Регистрация бота на боевом домене, первые 5-10 реальных диалогов с тестовым аккаунтом, калибровка system prompt.

## Этап 7 — Операционные скрипты (~0.5 дня)

- [ ] **T-027** · `scripts/reload_skus.py` — читает актуальный Excel (путь из аргумента), парсит через openpyxl, сохраняет в `app/data/pilot_skus.json`. Валидация: 18 строк, все обязательные поля.
- [ ] **T-028** · `scripts/report_weekly.py` — SQL-выборка из events за 7 дней: сколько сообщений, сколько эскалаций, топ причин эскалации, топ заблокированных ответов. Вывод в markdown.

## После MVP

- Co-pilot режим для сложных диалогов (показывать менеджеру рекомендацию, а не отправлять)
- A/B-тест формулировок system prompt
- Интеграция с 1С через HTTP-сервис (замена ручной выгрузки)
- Расширение на другие SKU (выход за пилотные 18)
- Расширение на другие каналы Битрикса (Telegram, WhatsApp)

## Definition of Done для каждой задачи

- Код проходит `ruff check .` без ошибок
- Тесты зелёные (`pytest`)
- `requirements.txt` не сломан
- Ничего не хардкодится кроме значений из `app/data/*` и `app/prompts/*`
- Новое поведение отражено в `README.md` или `SPEC.md` (что именно поменялось)
