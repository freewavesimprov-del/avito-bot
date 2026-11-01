"""Async-клиент YandexGPT Foundation Models API.

Endpoint: POST https://llm.api.cloud.yandex.net/foundationModels/v1/completion
Auth:     Authorization: Api-Key <YC_API_KEY>
Model URI: gpt://<folder_id>/<model>/latest

Отвечает dict-ом вида:
  { "result": { "alternatives": [{"message": {"role": "assistant", "text": "..."}}], ... } }

См. https://yandex.cloud/ru/docs/foundation-models/text-generation/api-ref/TextGeneration/completion
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.services.yandex_connection import Connection, connection

logger = logging.getLogger("avito_bot.llm")

YC_ENDPOINT = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"


class LLMError(Exception):
    """Общая ошибка обращения к LLM (таймаут, 5xx, невалидный ответ)."""


@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    text: str


def _model_uri(folder_id: str, model: str) -> str:
    # Формат: gpt://<folder_id>/<model>/latest
    return f"gpt://{folder_id}/{model}/latest"


def _build_payload(
    system_prompt: str,
    user_messages: list[ChatMessage],
    folder_id: str,
    model: str,
    temperature: float,
    max_tokens: int,
) -> dict[str, Any]:
    messages: list[dict[str, str]] = [{"role": "system", "text": system_prompt}]
    for m in user_messages:
        messages.append({"role": m.role, "text": m.text})
    return {
        "modelUri": _model_uri(folder_id, model),
        "completionOptions": {
            "stream": False,
            "temperature": temperature,
            "maxTokens": str(max_tokens),  # YC ждёт строку
        },
        "messages": messages,
    }


def _extract_text(response_json: dict[str, Any]) -> str:
    """Достать текст ответа из ответа YandexGPT.

    Бросает LLMError, если структура не та.
    """
    try:
        alternatives = response_json["result"]["alternatives"]
        if not alternatives:
            raise KeyError("empty alternatives")
        text = alternatives[0]["message"]["text"]
        if not isinstance(text, str):
            raise KeyError("text is not a string")
        return text.strip()
    except (KeyError, TypeError, IndexError) as e:
        raise LLMError(f"unexpected LLM response shape: {e}; keys={list(response_json)}") from e


async def generate(
    system_prompt: str,
    user_messages: list[ChatMessage],
    *,
    temperature: float = 0.3,
    max_tokens: int = 200,  # ~400 символов по-русски — хватает на 3-4 строки списка
    max_retries: int = 2,
    connection_override: Connection | None = None,
) -> str:
    """Вызов YandexGPT. Возвращает текст ответа или бросает LLMError.

    Ретраи: на таймаут и 5xx, до max_retries попыток (итого 1 + max_retries запросов).
    """
    settings = connection_override or connection()
    if not settings.api_key.get_secret_value() or not settings.folder_id:
        raise LLMError("YC_API_KEY / YC_FOLDER_ID не заданы в конфиге")

    headers = {
        "Authorization": f"Api-Key {settings.api_key.get_secret_value()}",
        "Content-Type": "application/json",
        "x-folder-id": settings.folder_id,
    }
    payload = _build_payload(
        system_prompt=system_prompt,
        user_messages=user_messages,
        folder_id=settings.folder_id,
        model=settings.model,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    timeout = httpx.Timeout(settings.timeout)
    attempt = 0
    last_exc: Exception | None = None
    while attempt <= max_retries:
        attempt += 1
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(YC_ENDPOINT, json=payload, headers=headers)
            if resp.status_code >= 500:
                last_exc = LLMError(f"LLM 5xx: {resp.status_code}")
                logger.warning(
                    "YC 5xx (attempt %d/%d): %d", attempt, max_retries + 1, resp.status_code
                )
            elif resp.status_code >= 400:
                # 4xx — не ретраим, это ошибка запроса / прав / лимитов
                raise LLMError(f"LLM 4xx: {resp.status_code}")
            else:
                return _extract_text(resp.json())
        except httpx.TimeoutException as e:
            last_exc = e
            logger.warning("YC timeout (attempt %d/%d)", attempt, max_retries + 1)
        except httpx.HTTPError as e:
            last_exc = e
            logger.warning("YC HTTP error (attempt %d/%d): %s", attempt, max_retries + 1, type(e).__name__)

        if attempt <= max_retries:
            await asyncio.sleep(0.5 * attempt)  # линейный backoff

    raise LLMError(f"YC failed after {attempt} attempts ({type(last_exc).__name__})")
