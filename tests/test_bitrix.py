"""Тесты Bitrix REST-клиента — мокаем httpx через MockTransport."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from app.services.bitrix import BitrixClient, BitrixCreds, BitrixError


def _mocked_client(handler: Callable[[httpx.Request], httpx.Response]) -> BitrixClient:
    """Подменяем httpx.AsyncClient внутри BitrixClient через monkeypatching на уровне теста."""
    return BitrixClient(BitrixCreds(domain="test.bitrix24.ru", access_token="tok"))


@pytest.fixture(autouse=True)
def _patch_httpx(monkeypatch):
    """Каждый тест вешает свой handler через переменную в conftest."""
    yield


@pytest.mark.asyncio
async def test_send_message_ok(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"result": True})

    import app.services.bitrix as bx

    class _PatchedClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(bx.httpx, "AsyncClient", _PatchedClient)

    client = BitrixClient(BitrixCreds(domain="test.bitrix24.ru", access_token="TOK"))
    res = await client.imbot_message_add(bot_id=42, dialog_id="d1", message="hi")
    assert res is True
    assert "imbot.message.add.json" in captured["url"]
    # form body содержит auth + параметры
    assert "auth=TOK" in captured["body"]
    assert "BOT_ID=42" in captured["body"]
    assert "DIALOG_ID=d1" in captured["body"]


@pytest.mark.asyncio
async def test_b24_error_on_invalid_token(monkeypatch):
    def handler(_):
        return httpx.Response(
            200,
            json={"error": "invalid_token", "error_description": "The access token is invalid"},
        )

    import app.services.bitrix as bx

    class _PatchedClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(bx.httpx, "AsyncClient", _PatchedClient)

    client = BitrixClient(BitrixCreds(domain="test.bitrix24.ru", access_token="X"))
    with pytest.raises(BitrixError) as exc:
        await client.call("imbot.message.add", {})
    assert "invalid_token" in str(exc.value)


@pytest.mark.asyncio
async def test_retries_on_5xx_then_succeeds(monkeypatch):
    attempts = {"n": 0}

    def handler(_):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, text="upstream down")
        return httpx.Response(200, json={"result": {"ID": 99}})

    import app.services.bitrix as bx

    class _PatchedClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(bx.httpx, "AsyncClient", _PatchedClient)
    monkeypatch.setattr(bx.asyncio, "sleep", lambda *_: _noop())

    client = BitrixClient(BitrixCreds(domain="d", access_token="t"))
    res = await client.call("any.method", max_retries=3)
    assert res == {"ID": 99}
    assert attempts["n"] == 3


async def _noop():
    return None


@pytest.mark.asyncio
async def test_imbot_register_returns_int(monkeypatch):
    def handler(_):
        return httpx.Response(200, json={"result": 123})

    import app.services.bitrix as bx

    class _PatchedClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(bx.httpx, "AsyncClient", _PatchedClient)

    client = BitrixClient(BitrixCreds(domain="d", access_token="t"))
    bot_id = await client.imbot_register(
        code="avito_assistant",
        type_="O",
        event_handler="https://x/y",
        name="Авито-ассистент",
    )
    assert bot_id == 123
    assert isinstance(bot_id, int)


def test_creds_from_env_missing(monkeypatch):
    monkeypatch.setenv("B24_DOMAIN", "")
    monkeypatch.setenv("B24_ACCESS_TOKEN", "")
    from app.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(BitrixError):
        BitrixCreds.from_env()


def test_client_from_event_auth_missing():
    from app.services.bitrix import client_from_event_auth

    with pytest.raises(BitrixError):
        client_from_event_auth(None, None)
    with pytest.raises(BitrixError):
        client_from_event_auth("d", "")


# Убираем неиспользуемый импорт _mocked_client — ruff бы поругался
_ = _mocked_client
