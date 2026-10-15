import pytest

from tests.conftest import TEST_APP_TOKEN


def _sample_message_event(token: str | None):
    return {
        "event": "ONIMBOTMESSAGEADD",
        "ts": 1700000000,
        "data": {
            "PARAMS": {
                "MESSAGE_ID": 1,
                "MESSAGE": "Здравствуйте, Ангара в наличии?",
                "DIALOG_ID": "chat42",
                "FROM_USER_ID": 555,
                "BOT_ID": 100,
            },
            "USER": {"ID": 555, "NAME": "Иван", "IS_EXTRANET": True},
        },
        "auth": {
            "access_token": "acc",
            "domain": "test.bitrix24.ru",
            **({"application_token": token} if token is not None else {}),
        },
    }


@pytest.mark.asyncio
async def test_webhook_rejects_missing_token(client):
    r = await client.post("/b24/webhook", json=_sample_message_event(None))
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_webhook_rejects_wrong_token(client):
    r = await client.post("/b24/webhook", json=_sample_message_event("wrong-token"))
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_webhook_accepts_valid_token_and_logs(client):
    from app import main as app_module

    r = await client.post("/b24/webhook", json=_sample_message_event(TEST_APP_TOKEN))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["event"] == "ONIMBOTMESSAGEADD"

    # Stage 6.5: ONIMBOTMESSAGEADD обрабатывается в фоне — ждём.
    await app_module.wait_for_background_tasks(timeout=5)

    # Проверяем что входящее сообщение легло в events.
    # Бот может ответить (action='sent') — тогда будет вторая запись 'out'.
    # Здесь смысл теста — auth и сам факт записи входящего, не количество.
    from app.services.logger import get_recent_history

    hist = await get_recent_history("chat42", limit=5)
    incoming = [h for h in hist if h["direction"] == "in"]
    assert len(incoming) == 1
    assert "Ангара" in incoming[0]["message"]


@pytest.mark.asyncio
async def test_webhook_bot_delete_marks_operator_led(client):
    evt = {
        "event": "ONIMBOTDELETE",
        "data": {"BOT_ID": 100, "DIALOG_ID": "chat99"},
        "auth": {"application_token": TEST_APP_TOKEN, "domain": "test.bitrix24.ru"},
    }
    r = await client.post("/b24/webhook", json=evt)
    assert r.status_code == 200

    from app.services.logger import is_operator_led

    assert await is_operator_led("chat99") is True
    assert await is_operator_led("chat42") is False


@pytest.mark.asyncio
async def test_webhook_rejects_malformed_json(client):
    r = await client.post(
        "/b24/webhook",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_webhook_accepts_form_encoded(client):
    """B24 часто шлёт application/x-www-form-urlencoded с PHP-style ключами."""
    form = {
        "event": "ONIMBOTMESSAGEADD",
        "ts": "1700000000",
        "data[PARAMS][MESSAGE_ID]": "7",
        "data[PARAMS][MESSAGE]": "Привет",
        "data[PARAMS][DIALOG_ID]": "chat7",
        "data[PARAMS][FROM_USER_ID]": "333",
        "data[USER][ID]": "333",
        "data[USER][IS_EXTRANET]": "Y",
        "auth[access_token]": "acc",
        "auth[domain]": "test.bitrix24.ru",
        "auth[application_token]": TEST_APP_TOKEN,
    }
    r = await client.post("/b24/webhook", data=form)
    assert r.status_code == 200, r.text

    from app import main as app_module
    from app.services.logger import get_recent_history

    await app_module.wait_for_background_tasks(timeout=5)

    hist = await get_recent_history("chat7", limit=5)
    incoming = [h for h in hist if h["direction"] == "in"]
    assert len(incoming) == 1
    assert incoming[0]["message"] == "Привет"
