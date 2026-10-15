"""Конфиг приложения. Читает переменные из .env через pydantic-settings."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Все параметры — в .env (см. .env.example)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # --- Environment ---
    APP_ENV: Literal["prod", "dev"] = Field(
        default="prod",
        description="'dev' включает /dev/chat эндпоинты и HTML-эмулятор чата",
    )
    MOCK_LLM: bool = Field(
        default=False,
        description="Если True — LLM-клиент возвращает stub без похода в YandexGPT",
    )

    # --- Bitrix24 ---
    # В dev-режиме эти поля становятся опциональными (не нужны для /dev/chat)
    B24_DOMAIN: str = Field(default="", description="Поддомен B24, напр. company.bitrix24.ru")
    B24_ACCESS_TOKEN: str = Field(default="", description="access_token из payload / OAuth")
    B24_BOT_ID: int | None = Field(default=None, description="ID зарегистрированного бота")
    B24_APPLICATION_TOKEN: str = Field(
        default="",
        description="Сохранённый при установке application_token; сверяется с auth.application_token",
    )
    # client_id/secret видны в карточке локального приложения B24 — нужны для рефреша access_token
    # (одноразовые скрипты register_bot.py / b24_check.py / экспорт чатов).
    B24_CLIENT_ID: str = Field(default="")
    B24_CLIENT_SECRET: str = Field(default="")

    # --- Avito API (поллер заказов + сообщения клиенту) ---
    AVITO_CLIENT_ID: str = Field(default="", description="client_id Avito API-приложения")
    AVITO_CLIENT_SECRET: str = Field(default="", description="client_secret Avito API-приложения")
    AVITO_USER_ID: int | None = Field(
        default=None,
        description="user_id продавца. Если не задан — будет получен через /core/v1/accounts/self",
    )
    AVITO_WATCHER_ENABLE: bool = Field(
        default=False,
        description="Включить background-поллер Avito заказов / системных сообщений",
    )
    AVITO_WATCHER_INTERVAL_SEC: int = Field(
        default=60, ge=15, le=600,
        description="Период поллинга Avito API в секундах",
    )
    AVITO_WATCHER_CHATS_LIMIT: int = Field(
        default=50, ge=10, le=200,
        description="Сколько последних чатов опрашивать за один тик",
    )
    AVITO_WATCHER_SEND_ACK: bool = Field(
        default=False,
        description="Отправлять автоматическое сообщение клиенту в Avito при детекте заказа",
    )

    # --- Сторож зависших диалогов в Открытой линии ---
    # Telegram-креды не дублируем: используются TG_BOT_TOKEN / TG_CHAT_ID /
    # TG_NOTIFY_ENABLE, объявленные ниже в секции уведомлений.
    OL_WATCHDOG_ENABLE: bool = Field(
        default=False,
        description="Следить за неотвеченными диалогами Открытой линии и слать алерт в TG",
    )
    OL_WATCHDOG_LINE_ID: int = Field(
        default=166, description="CONFIG_ID Открытой линии, за которой следим"
    )
    OL_WATCHDOG_INTERVAL_SEC: int = Field(default=300, ge=60, le=3600)
    OL_WATCHDOG_UNANSWERED_MIN: int = Field(
        default=15, ge=5, le=240,
        description="Через сколько минут без ответа оператора слать алерт",
    )

    # --- Yandex Cloud (YandexGPT) ---
    # В dev-режиме с MOCK_LLM=True — тоже опциональны
    YC_API_KEY: str = Field(default="", description="API key Yandex Cloud")
    YC_FOLDER_ID: str = Field(default="", description="ID каталога Yandex Cloud")
    YC_MODEL: str = Field(default="yandexgpt-lite")
    YC_TIMEOUT_SEC: int = Field(default=8, ge=1, le=60)

    # --- App ---
    APP_HOST: str = Field(default="0.0.0.0")
    APP_PORT: int = Field(default=8000, ge=1, le=65535)
    WEBHOOK_PATH: str = Field(default="/b24/webhook")
    DB_PATH: str = Field(default="./data/bot.db")
    LOG_LEVEL: str = Field(default="INFO")
    # 0 = бот отвечает сразу (режим "бот = основной канал, Татьяна — по запросу").
    # >0 — бот ждёт N сек, давая оператору шанс ответить первым (режим резерва за оператором).
    DEBOUNCE_SEC: int = Field(default=0, ge=0, le=60)

    # --- Telegram-уведомления (обращения клиентов и ответы бота) ---
    TG_BOT_TOKEN: str = Field(default="", description="Token Telegram-бота для уведомлений")
    TG_CHAT_ID: str = Field(default="", description="Chat ID получателя уведомлений")
    TG_NOTIFY_ENABLE: bool = Field(
        default=False,
        description="Слать в Telegram уведомления о новых обращениях и ответах бота",
    )

    # --- Расписание работы бота (московское время) ---
    BOT_SCHEDULE_ENABLE: bool = Field(
        default=True,
        description="Ограничивать работу бота нерабочими часами",
    )
    BOT_ACTIVE_FROM_HOUR: int = Field(
        default=17, ge=0, le=23,
        description="С какого часа МСК бот начинает отвечать",
    )
    BOT_ACTIVE_TO_HOUR: int = Field(
        default=9, ge=0, le=23,
        description="До какого часа МСК бот отвечает (окно через полночь)",
    )
    BOT_WEEKEND_ALWAYS_ACTIVE: bool = Field(
        default=True,
        description="В субботу и воскресенье бот отвечает круглосуточно (выходные целиком нерабочее время)",
    )
    B24_FORWARD_WINDOW_ENABLE: bool = Field(
        default=True,
        description="Ограничивать передачу диалогов менеджерам окном выходных",
    )
    B24_FORWARD_FROM_HOUR: int = Field(
        default=17, ge=0, le=23,
        description="С какого часа пятницы МСК разрешена передача в B24",
    )
    B24_FORWARD_TO_HOUR: int = Field(
        default=19, ge=0, le=23,
        description="До какого часа последнего дня окна МСК разрешена передача в B24",
    )
    B24_FORWARD_TO_WEEKDAY: int = Field(
        default=6, ge=0, le=6,
        description=(
            "Каким днём недели заканчивается окно передачи (0=пн … 6=вс). "
            "По умолчанию воскресенье: в будни с клиентами работает магазин "
            "прямо в кабинете Авито"
        ),
    )

    # --- Очередь Открытой линии Avito по расписанию ---
    OL_QUEUE_ENABLE: bool = Field(
        default=True,
        description="Держать состав очереди линии по расписанию будни/выходные",
    )
    OL_QUEUE_LINE_ID: int = Field(default=166, description="ID Открытой линии Avito")
    OL_QUEUE_WEEKDAY: str = Field(
        default="2964",
        description="Кто в очереди в будни (ID через запятую) — обычно один человек",
    )
    OL_QUEUE_WEEKEND: str = Field(
        default="2964,838,21004,15790,30596,27414,3902,32452",
        description="Полный состав очереди на выходные",
    )
    OL_QUEUE_INTERVAL_SEC: int = Field(
        default=300, ge=60, description="Как часто сверять состав очереди",
    )

    # --- Ответственный за лиды Авито по расписанию ---
    LEAD_ROUTER_ENABLE: bool = Field(
        default=False,
        description="В будни переназначать лиды Авито на магазин",
    )
    LEAD_ROUTER_WEEKDAY_USER: int = Field(
        default=9020,
        description="Кому принадлежат лиды Авито в будни (ID пользователя B24)",
    )
    LEAD_ROUTER_SOURCES: str = Field(
        default="4|AVITO",
        description="Источники лидов, которые считаем авитовскими (через запятую)",
    )
    LEAD_ROUTER_LOOKBACK_MIN: int = Field(
        default=180, ge=5,
        description="Насколько назад смотреть свежие лиды при каждом проходе",
    )
    LEAD_ROUTER_SINCE: str = Field(
        default="",
        description="Не трогать лиды, созданные раньше этого момента (ISO, МСК)",
    )
    LEAD_ROUTER_INTERVAL_SEC: int = Field(
        default=60, ge=30, description="Как часто проверять новые лиды",
    )

    # --- Admin (веб-форма обновления каталога) ---
    ADMIN_USER: str = Field(default="", description="Логин для /admin/* (Basic Auth)")
    ADMIN_PASSWORD: str = Field(default="", description="Пароль для /admin/*")
    # Путь к файлу каталога. Пусто → app/data/pilot_skus.json относительно репо.
    # Можно переопределить (например в тестах или для нескольких окружений).
    CATALOG_PATH: str = Field(default="")

    @field_validator("B24_BOT_ID", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        """Пустая строка из .env (B24_BOT_ID=) должна стать None, а не падать."""
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    def require_prod_secrets(self) -> list[str]:
        """В prod — обязательны B24_APPLICATION_TOKEN и YC-ключи (если не MOCK_LLM).
        Возвращает список пустых обязательных полей.
        """
        missing: list[str] = []
        if self.APP_ENV == "prod":
            if not self.B24_APPLICATION_TOKEN:
                missing.append("B24_APPLICATION_TOKEN")
            if not self.B24_DOMAIN:
                missing.append("B24_DOMAIN")
            if not self.MOCK_LLM:
                if not self.YC_API_KEY:
                    missing.append("YC_API_KEY")
                if not self.YC_FOLDER_ID:
                    missing.append("YC_FOLDER_ID")
        return missing


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Синглтон конфига. В тестах сбрасывается через get_settings.cache_clear()."""
    return Settings()  # type: ignore[call-arg]
