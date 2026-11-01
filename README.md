# Avito Bot — чат-бот для магазина мебели на Авито

LLM-бот на FastAPI + YandexGPT, интегрированный с Авито через Открытые линии Битрикс24.

## Что читать первым

1. **CLAUDE.md** — контекст проекта для агента Claude Code
2. **SPEC.md** — детальное ТЗ: потоки данных, триггеры, system prompt, post-filter

## Локальная отладка без Авито и Битрикса (dev-режим)

Для разработки и калибровки промпта/фильтров можно запустить бота полностью локально — без регистрации в B24, без YandexGPT-ключа, без ngrok. Поднимается HTML-чат в браузере, ответы бота детерминистичны (mock).

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# .env для dev (минимум)
cat > .env <<'EOF'
APP_ENV=dev
MOCK_LLM=true
EOF

uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
# Открыть http://127.0.0.1:8000/dev/chat
```

В чате:
- слева-вверху поле `Dialog` / `User` — меняй чтобы открыть новую сессию,
- `Reset диалога` — очистить events и флаги,
- `Оператор зашёл` — ставит `operator_led=true`, следующее сообщение бот должен проигнорировать.

Когда в Stage 3 подключим YandexGPT — та же страница начнёт показывать живые ответы (достаточно `MOCK_LLM=false` и валидных YC-ключей в `.env`).

## Быстрый старт (локально)

```bash
# 1. Создать venv и поставить зависимости
python -m venv .venv
source .venv/bin/activate   # Linux/Mac
pip install -r requirements.txt

# 2. Скопировать и заполнить .env
cp .env.example .env
nano .env   # указать B24_DOMAIN, B24_ACCESS_TOKEN, YC_API_KEY, YC_FOLDER_ID

# 3. Подготовить данные о товарах
# Положить актуальную выгрузку 1С как data/pilot_skus_raw.xlsx
python scripts/reload_skus.py

# 4. Зарегистрировать бота в Bitrix24 (разовая операция)
python scripts/register_bot.py

# 5. Запустить
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## Деплой на VPS

1. Python 3.11, venv, Nginx, certbot (Let's Encrypt)
2. systemd unit `avito-bot.service` в `/etc/systemd/system/`
3. Nginx proxy на `127.0.0.1:8000`, webhook path `/b24/webhook`
4. В Bitrix24 указать URL `https://ваш-домен/b24/webhook` при регистрации бота

## Структура

```
app/
  main.py              # FastAPI, регистрация endpoint'ов
  config.py            # pydantic-settings, читает .env
  models.py            # pydantic-модели для событий Bitrix24
  handlers/
    message.py         # обработчик ONIMBOTMESSAGEADD
  services/
    bitrix.py          # REST-клиент Битрикса
    llm.py             # YandexGPT-клиент
    filters.py         # pre-/post-filter (regex на phone/email/links)
    triggers.py        # хард-триггеры (жалобы, «позовите менеджера»)
    logger.py          # запись в SQLite
  prompts/
    system_prompt.md   # текст system prompt (редактируется без релиза)
  data/
    pilot_skus.json    # 18 SKU с ценами и остатками
    faq_extras.md      # доп. контекст (доставка, гарантия)
scripts/
  register_bot.py      # одноразовая регистрация
  reload_skus.py       # обновление pilot_skus.json из Excel
tests/
  test_filters.py
  test_triggers.py
  test_e2e.py
```

## Окружение

Необходимые переменные в `.env` (см. `.env.example`):

- `B24_DOMAIN` — поддомен Битрикса (`your-company.bitrix24.ru`)
- `B24_ACCESS_TOKEN` — токен приложения
- `B24_BOT_ID` — ID зарегистрированного бота
- `YC_API_KEY` — API-ключ Yandex Cloud
- `YC_FOLDER_ID` — ID каталога
- `WEBHOOK_SECRET` — для валидации подписи от B24

## Управление каталогом и сценариями

Страница `/admin/catalog` содержит импорт выгрузок Авито/1С с предпросмотром,
редактор шаблонов и сценариев, а также изолированный playground. Сохранение
черновика и публикация для бота — отдельные действия. Доступ защищён настройками
`ADMIN_USER` и `ADMIN_PASSWORD`; для проверки ответов модели нужны настроенные
`YC_API_KEY` и `YC_FOLDER_ID`. [Подробная инструкция](docs/admin-studio.md).

## Проверки

```bash
pytest                              # все
pytest tests/test_filters.py        # только фильтры
pytest tests/test_e2e.py -v         # эмуляция B24-событий
```

## Логи и отладка

- App-логи: stdout → `journalctl -u avito-bot -f` (в проде)
- Диалоги и события: SQLite `data/bot.db` — таблица `events`
- Быстрая выборка: `sqlite3 data/bot.db "SELECT * FROM events ORDER BY ts DESC LIMIT 20;"`

## Что делать с недостающими ответами

Раз в неделю:

```bash
sqlite3 data/bot.db "SELECT message, COUNT(*) FROM events WHERE direction='escalate' GROUP BY message ORDER BY 2 DESC LIMIT 20;"
```

→ топ-20 вопросов, которые ушли к менеджеру. Добавляем новые кейсы в `app/prompts/system_prompt.md` или расширяем `faq_extras.md`.
