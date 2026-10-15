"""Тесты dev-режима (локальный чат-эмулятор)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def dev_client() -> AsyncIterator[AsyncClient]:
    """Клиент с включенным APP_ENV=dev и MOCK_LLM=true."""
    os.environ["APP_ENV"] = "dev"
    os.environ["MOCK_LLM"] = "true"

    from app.config import get_settings

    get_settings.cache_clear()

    from app import main as app_module

    # Идемпотентная регистрация dev-роутов (после смены env)
    app_module.maybe_register_dev_routes()

    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await app_module.init_db()
        try:
            yield ac
        finally:
            await app_module.dispose_db()
    # Сброс для остальных тестов
    os.environ.pop("APP_ENV", None)
    os.environ.pop("MOCK_LLM", None)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_dev_chat_html_served(dev_client):
    r = await dev_client.get("/dev/chat")
    assert r.status_code == 200
    assert "Avito-bot" in r.text
    assert "dev-chat" in r.text


@pytest.mark.asyncio
async def test_dev_chat_send_uses_mock(dev_client):
    r = await dev_client.post(
        "/dev/chat/send",
        json={"dialog_id": "t1", "user_id": "u1", "message": "Здравствуйте!"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "sent"
    # MOCK_LLM=true — ответ на "здравствуйте" детерминистичен
    assert body["text"] is not None
    assert "Здравствуйте" in body["text"] or "помочь" in body["text"].lower()


@pytest.mark.asyncio
async def test_dev_chat_empty_message_rejected(dev_client):
    r = await dev_client.post(
        "/dev/chat/send", json={"dialog_id": "t1", "user_id": "u1", "message": "   "}
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_dev_chat_history_returns_both_in_and_out(dev_client):
    await dev_client.post(
        "/dev/chat/send",
        json={"dialog_id": "t2", "user_id": "u1", "message": "Доставка по МО есть?"},
    )
    r = await dev_client.get("/dev/chat/history?dialog_id=t2")
    assert r.status_code == 200
    items = r.json()["items"]
    dirs = [it["direction"] for it in items]
    assert "in" in dirs
    assert "out" in dirs


@pytest.mark.asyncio
async def test_dev_chat_operator_takeover_silences_bot(dev_client):
    # До takeover — бот отвечает
    r1 = await dev_client.post(
        "/dev/chat/send", json={"dialog_id": "t3", "user_id": "u1", "message": "Привет"}
    )
    assert r1.json()["action"] == "sent"

    # Симулируем, что оператор зашёл
    r2 = await dev_client.post("/dev/chat/operator_takeover?dialog_id=t3")
    assert r2.status_code == 200

    # Следующее сообщение — бот молчит
    r3 = await dev_client.post(
        "/dev/chat/send",
        json={"dialog_id": "t3", "user_id": "u1", "message": "Ещё вопрос"},
    )
    body = r3.json()
    assert body["action"] == "silent"
    assert body["reason"] == "operator_led"


@pytest.mark.asyncio
async def test_dev_chat_reset_clears_dialog(dev_client):
    await dev_client.post(
        "/dev/chat/send", json={"dialog_id": "t4", "user_id": "u1", "message": "Тест"}
    )
    hist = (await dev_client.get("/dev/chat/history?dialog_id=t4")).json()
    assert len(hist["items"]) > 0

    await dev_client.post("/dev/chat/reset?dialog_id=t4")
    hist2 = (await dev_client.get("/dev/chat/history?dialog_id=t4")).json()
    assert hist2["items"] == []
