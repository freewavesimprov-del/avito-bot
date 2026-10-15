"""E2E-тесты обработчика ONIMBOTMESSAGEADD: webhook → chat_engine → bitrix.

Подменяем bitrix.client_from_event_auth, чтобы поймать вызовы send/leave.
LLM мокаем через MOCK_LLM=true (детерминистичные ответы).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


class _BitrixSpy:
    """Двойник BitrixClient — пишет, что от него вызывали."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.left: list[dict[str, Any]] = []
        # Что вернёт im.dialog.messages.get — задаётся в тесте при необходимости.
        self.preexisting_messages: list[dict[str, Any]] = []

    async def imbot_message_add(self, *, bot_id: int, dialog_id: str, message: str) -> bool:
        self.sent.append({"bot_id": bot_id, "dialog_id": dialog_id, "message": message})
        return True

    async def imbot_chat_leave(self, *, bot_id: int, dialog_id: str) -> bool:
        self.left.append({"bot_id": bot_id, "dialog_id": dialog_id})
        return True

    async def im_dialog_messages_get(
        self, *, dialog_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        return list(self.preexisting_messages)


@pytest_asyncio.fixture
async def webhook_client(monkeypatch) -> AsyncIterator[tuple[AsyncClient, _BitrixSpy]]:
    """FastAPI client с замоканным B24-клиентом и MOCK_LLM=true."""
    os.environ["B24_BOT_ID"] = "100"
    os.environ["MOCK_LLM"] = "true"
    os.environ.setdefault("B24_DOMAIN", "test.bitrix24.ru")
    os.environ.setdefault("B24_ACCESS_TOKEN", "x")
    os.environ.setdefault("B24_APPLICATION_TOKEN", "test-app-token-abc")

    from app.config import get_settings

    get_settings.cache_clear()

    spy = _BitrixSpy()
    import app.handlers.message as handler_mod

    monkeypatch.setattr(handler_mod, "client_from_event_auth", lambda d, t: spy)

    from app import main as app_module

    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await app_module.init_db()
        try:
            yield ac, spy
        finally:
            await app_module.dispose_db()
    os.environ.pop("MOCK_LLM", None)
    os.environ.pop("B24_BOT_ID", None)
    get_settings.cache_clear()


async def _post_and_drain(client, payload):
    """POST на webhook + ждём фоновую обработку handler'а."""
    from app import main as app_module

    r = await client.post("/b24/webhook", json=payload)
    await app_module.wait_for_background_tasks(timeout=5)
    return r


def _msg_event(text: str, dialog_id: str = "chat-x", from_id: int = 555):
    return {
        "event": "ONIMBOTMESSAGEADD",
        "data": {
            "PARAMS": {
                "MESSAGE_ID": 1,
                "MESSAGE": text,
                "DIALOG_ID": dialog_id,
                "FROM_USER_ID": from_id,
                "BOT_ID": 100,
            },
            "USER": {"ID": from_id, "NAME": "Иван", "IS_EXTRANET": True},
        },
        "auth": {
            "access_token": "fresh",
            "domain": "test.bitrix24.ru",
            "application_token": "test-app-token-abc",
        },
    }


@pytest.mark.asyncio
async def test_normal_message_is_delivered(webhook_client):
    client, spy = webhook_client
    r = await _post_and_drain(client, _msg_event("Здравствуйте!"))
    assert r.status_code == 200
    assert len(spy.sent) == 1
    sent = spy.sent[0]
    assert sent["bot_id"] == 100
    assert sent["dialog_id"] == "chat-x"
    # mock_reply на «здравствуйте» отдаёт приветствие
    assert "Здравствуйте" in sent["message"] or "помочь" in sent["message"].lower()
    assert spy.left == []  # обычный ответ — не выходим


@pytest.mark.asyncio
async def test_hard_trigger_escalates_with_leave(webhook_client):
    client, spy = webhook_client
    r = await _post_and_drain(
        client, _msg_event("Прислали с браком, ужасное качество")
    )
    assert r.status_code == 200
    # Должны отправить фикс-фразу + покинуть чат
    assert len(spy.sent) == 1
    assert "менеджер" in spy.sent[0]["message"].lower()
    assert len(spy.left) == 1
    assert spy.left[0]["dialog_id"] == "chat-x"


@pytest.mark.asyncio
async def test_operator_message_marks_dialog_operator_led(webhook_client):
    """IS_EXTRANET=False — пишет оператор. Stage 6.5: бот не отвечает И помечает operator_led,
    чтобы в этом диалоге больше не лезть никогда."""
    from app.services.logger import is_operator_led

    client, spy = webhook_client
    evt = _msg_event("любое сообщение от Татьяны через коннектор")
    evt["data"]["USER"]["IS_EXTRANET"] = False
    r = await _post_and_drain(client, evt)
    assert r.status_code == 200
    assert spy.sent == []
    assert spy.left == []
    # secondary detection — диалог помечен operator_led
    assert await is_operator_led("chat-x") is True

    # Следующее обычное сообщение клиента — silent
    r = await _post_and_drain(client, _msg_event("привет, бот ты тут?"))
    assert spy.sent == []


@pytest.mark.asyncio
async def test_self_echo_is_skipped(webhook_client):
    """Если FROM_USER_ID == BOT_ID, это эхо самого бота."""
    client, spy = webhook_client
    evt = _msg_event("эхо", from_id=100)  # совпадает с BOT_ID=100
    r = await _post_and_drain(client, evt)
    assert r.status_code == 200
    assert spy.sent == []


@pytest.mark.asyncio
async def test_operator_led_silences_subsequent_messages(webhook_client):
    """Если до этого пришёл ONIMBOTDELETE (operator_led=True), бот игнорит входящие."""
    client, spy = webhook_client

    # B24 шлёт ONIMBOTDELETE → main.py выставляет operator_led=True
    delete_evt = {
        "event": "ONIMBOTDELETE",
        "data": {"BOT_ID": 100, "DIALOG_ID": "chat-x"},
        "auth": {"application_token": "test-app-token-abc", "domain": "test.bitrix24.ru"},
    }
    r = await client.post("/b24/webhook", json=delete_evt)
    assert r.status_code == 200

    # Теперь обычное сообщение клиента — должно быть silent (никаких send/leave)
    r = await _post_and_drain(client, _msg_event("Здравствуйте"))
    assert r.status_code == 200
    assert spy.sent == []
    assert spy.left == []


@pytest.mark.asyncio
async def test_preexisting_operator_chat_bot_stays_silent(webhook_client):
    """Если в B24 уже была переписка с оператором ДО подключения бота —
    бот не должен входить в этот чат на первое же сообщение клиента."""
    client, spy = webhook_client
    # В B24 уже есть сообщения: одно от клиента, одно от менеджера-оператора (id 1)
    spy.preexisting_messages = [
        {"id": 100, "author_id": 555, "text": "Здравствуйте, цена?"},
        {"id": 101, "author_id": 1, "text": "9 000 ₽, доставим в среду"},
        {"id": 102, "author_id": 555, "text": "Хорошо"},
    ]
    r = await _post_and_drain(client, _msg_event("Передумал, можно вернуть?"))
    assert r.status_code == 200
    assert spy.sent == []  # бот не отвечает
    assert spy.left == []  # и не выходит, потому что он там и не был

    from app.services.logger import is_operator_led

    assert await is_operator_led("chat-x") is True


@pytest.mark.asyncio
async def test_truly_new_chat_bot_enters_normally(webhook_client):
    """Если в B24 нет никакой истории по dialog_id — это новый чат, бот отвечает."""
    client, spy = webhook_client
    spy.preexisting_messages = []  # пусто — новый чат
    r = await _post_and_drain(client, _msg_event("Здравствуйте, есть Ангара?"))
    assert r.status_code == 200
    assert len(spy.sent) == 1


@pytest.mark.asyncio
async def test_existing_chat_only_with_bot_history_is_normal(webhook_client):
    """Если до этого в чате были только сообщения от клиента и от нашего бота
    (мы уже его вели раньше) — это не «оператор работал», обрабатываем дальше."""
    client, spy = webhook_client
    spy.preexisting_messages = [
        {"id": 100, "author_id": 555, "text": "Привет"},
        {"id": 101, "author_id": 100, "text": "Здравствуйте, чем могу помочь?"},  # author=BOT_ID=100
    ]
    r = await _post_and_drain(client, _msg_event("Есть Ангара?"))
    assert r.status_code == 200
    # Бот отвечает (наша же история не считается «оператор»)
    assert len(spy.sent) == 1


@pytest.mark.asyncio
async def test_bitrix_failure_does_not_break_webhook(webhook_client, monkeypatch):
    """Если B24 REST упал — webhook всё равно возвращает 200 (B24 нельзя долго мариновать)."""
    client, spy = webhook_client

    async def _boom(**kwargs):
        from app.services.bitrix import BitrixError

        raise BitrixError("simulated outage")

    spy.imbot_message_add = _boom  # type: ignore[method-assign]

    r = await _post_and_drain(client, _msg_event("Здравствуйте"))
    # Webhook всё равно 200 — B24 повторит, если что
    assert r.status_code == 200
