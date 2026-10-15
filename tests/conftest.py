"""Общие фикстуры тестов."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

TEST_APP_TOKEN = "test-app-token-abc"


def _set_test_env(db_path: str) -> None:
    os.environ.setdefault("B24_DOMAIN", "test.bitrix24.ru")
    os.environ.setdefault("B24_ACCESS_TOKEN", "test-access")
    os.environ["B24_APPLICATION_TOKEN"] = TEST_APP_TOKEN
    os.environ.setdefault("YC_API_KEY", "test-yc-key")
    # По дефолту тесты не должны ходить в реальный YandexGPT.
    # Тесты, которым нужен другой режим, переопределяют MOCK_LLM локально.
    os.environ.setdefault("MOCK_LLM", "true")
    # Отключаем debounce в тестах — иначе каждый прогон chat_engine
    # будет спать DEBOUNCE_SEC секунд.
    os.environ.setdefault("DEBOUNCE_SEC", "0")
    os.environ.setdefault("YC_FOLDER_ID", "test-folder")
    os.environ["DB_PATH"] = db_path
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    # Расписание в тестах выключено: иначе результат прогона зависел бы от того,
    # в какой час суток его запустили. Окна проверяются отдельно в
    # tests/test_schedule.py, там настройки включаются явно.
    os.environ.setdefault("BOT_SCHEDULE_ENABLE", "false")
    os.environ.setdefault("B24_FORWARD_WINDOW_ENABLE", "false")


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Изолированная SQLite на каждый тест + сброс кэша get_settings."""
    db_path = tmp_path / "bot.db"
    _set_test_env(str(db_path))

    from app.config import get_settings

    get_settings.cache_clear()
    yield db_path
    get_settings.cache_clear()


class _DefaultBitrixStub:
    """Минимальный двойник BitrixClient для тестов webhook-auth: ничего не пишет,
    возвращает пустую историю. Тесты handler-pipeline подменяют своим spy."""

    async def imbot_message_add(self, **kw):
        return True

    async def imbot_chat_leave(self, **kw):
        return True

    async def im_dialog_messages_get(self, **kw):
        return []


@pytest_asyncio.fixture
async def client(monkeypatch) -> AsyncIterator[AsyncClient]:
    """Httpx AsyncClient, привязанный к FastAPI-приложению через ASGI-транспорт.

    Импорт app делаем ВНУТРИ фикстуры, чтобы он подхватил test env.
    Сразу подменяем B24-клиент на stub — тесты по дефолту не должны лезть в сеть.
    """
    import app.handlers.message as handler_mod

    monkeypatch.setattr(handler_mod, "client_from_event_auth", lambda d, t: _DefaultBitrixStub())

    from app import main as app_module

    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await app_module.init_db()
        try:
            yield ac
        finally:
            await app_module.dispose_db()
