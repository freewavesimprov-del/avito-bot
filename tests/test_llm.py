"""Тесты YandexGPT-клиента. httpx мокаем через MockTransport."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.services.llm import ChatMessage, LLMError, _build_payload, _extract_text, generate


@pytest.fixture(autouse=True)
def _yc_env(monkeypatch):
    os.environ["YC_API_KEY"] = "fake-api-key"
    os.environ["YC_FOLDER_ID"] = "fake-folder"
    os.environ.setdefault("B24_APPLICATION_TOKEN", "t")
    os.environ.setdefault("B24_DOMAIN", "test.bitrix24.ru")
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _ok_response(text: str) -> dict[str, Any]:
    return {
        "result": {
            "alternatives": [
                {"message": {"role": "assistant", "text": text}, "status": "ALTERNATIVE_STATUS_FINAL"}
            ],
            "usage": {"inputTextTokens": "10", "completionTokens": "5", "totalTokens": "15"},
            "modelVersion": "test",
        }
    }


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_generate_happy_path(monkeypatch):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        import json as _json

        captured["json"] = _json.loads(request.content.decode())
        return httpx.Response(200, json=_ok_response("Здравствуйте!"))

    # Подменяем httpx.AsyncClient целиком
    import app.services.llm as llm_mod

    class _PatchedClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _PatchedClient)

    text = await generate(
        system_prompt="ты бот магазина",
        user_messages=[ChatMessage(role="user", text="Привет")],
    )
    assert text == "Здравствуйте!"
    assert captured["url"].endswith("/foundationModels/v1/completion")
    assert captured["headers"]["authorization"] == "Api-Key fake-api-key"
    assert captured["headers"]["x-folder-id"] == "fake-folder"
    assert captured["json"]["modelUri"] == "gpt://fake-folder/yandexgpt-lite/latest"
    assert captured["json"]["messages"][0]["role"] == "system"
    assert captured["json"]["messages"][1]["text"] == "Привет"


@pytest.mark.asyncio
async def test_generate_retries_on_5xx(monkeypatch):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json=_ok_response("Ok"))

    import app.services.llm as llm_mod

    class _PatchedClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _PatchedClient)
    # Чтобы тест был быстрый — зануляем sleep
    monkeypatch.setattr(llm_mod.asyncio, "sleep", lambda *_: _noop())

    text = await generate(
        system_prompt="sys", user_messages=[ChatMessage(role="user", text="hi")], max_retries=3
    )
    assert text == "Ok"
    assert attempts["n"] == 3


async def _noop():
    return None


@pytest.mark.asyncio
async def test_generate_raises_on_4xx_no_retry(monkeypatch):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(401, text="unauthorized")

    import app.services.llm as llm_mod

    class _PatchedClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _PatchedClient)

    with pytest.raises(LLMError):
        await generate(system_prompt="s", user_messages=[ChatMessage(role="user", text="h")])
    assert attempts["n"] == 1  # 4xx не ретраится


def test_extract_text_ok():
    assert _extract_text(_ok_response("hi")) == "hi"


def test_extract_text_malformed_raises():
    with pytest.raises(LLMError):
        _extract_text({"result": {"alternatives": []}})
    with pytest.raises(LLMError):
        _extract_text({"wrong": "shape"})


def test_build_payload_shape():
    p = _build_payload(
        system_prompt="S",
        user_messages=[ChatMessage(role="user", text="U")],
        folder_id="F",
        model="yandexgpt-lite",
        temperature=0.3,
        max_tokens=200,
    )
    assert p["modelUri"] == "gpt://F/yandexgpt-lite/latest"
    assert p["completionOptions"]["maxTokens"] == "200"  # YC ждёт строку
    assert p["completionOptions"]["temperature"] == 0.3
    assert p["messages"][0] == {"role": "system", "text": "S"}
    assert p["messages"][1] == {"role": "user", "text": "U"}
