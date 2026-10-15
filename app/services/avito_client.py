"""Async Avito API client.

Используется фоновым поллером `avito_orders_watcher`. Делает OAuth client_credentials
с кэшем токена на 24h, GET/POST с экспоненциальным ретраем на 429/5xx.

Точки входа:
  - get_token() — OAuth, синглтон с авто-рефрешем
  - list_chats(limit, offset) — список чатов
  - get_messages(chat_id, limit, offset) — сообщения чата
  - send_message(chat_id, text) — отправить текст в чат от имени продавца
  - get_account_self() — узнать свой user_id
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger("avito_bot.avito_client")

API_BASE = "https://api.avito.ru"


class AvitoAPIError(Exception):
    """Ошибка обращения к Avito API."""


class AvitoClient:
    """Singleton-friendly async client. Создавать один раз на процесс."""

    def __init__(self, *, client_id: str, client_secret: str, user_id: int | None = None):
        self._cid = client_id
        self._csec = client_secret
        self._user_id = user_id
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))

    @classmethod
    def from_settings(cls) -> AvitoClient:
        s = get_settings()
        if not s.AVITO_CLIENT_ID or not s.AVITO_CLIENT_SECRET:
            raise AvitoAPIError("AVITO_CLIENT_ID / AVITO_CLIENT_SECRET не заданы в .env")
        return cls(
            client_id=s.AVITO_CLIENT_ID,
            client_secret=s.AVITO_CLIENT_SECRET,
            user_id=s.AVITO_USER_ID,
        )

    async def close(self) -> None:
        await self._http.aclose()

    # --- OAuth ----------------------------------------------------------

    async def _get_token(self) -> str:
        async with self._lock:
            if self._token and time.time() < self._token_expires_at - 300:
                return self._token
            resp = await self._http.post(
                f"{API_BASE}/token/",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._cid,
                    "client_secret": self._csec,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code != 200:
                raise AvitoAPIError(f"OAuth {resp.status_code}: {resp.text[:300]}")
            payload = resp.json()
            tok = payload.get("access_token")
            if not tok:
                raise AvitoAPIError(f"OAuth: token missing: {payload}")
            self._token = tok
            self._token_expires_at = time.time() + int(payload.get("expires_in") or 3600)
            logger.info("avito oauth ok, expires_in=%s", payload.get("expires_in"))
            return tok

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._get_token()}", "Accept": "application/json"}

    # --- HTTP with retry ------------------------------------------------

    async def _request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        for attempt in range(3):
            try:
                resp = await self._http.request(
                    method,
                    f"{API_BASE}{path}",
                    headers=await self._headers(),
                    params=params,
                    json=json_body,
                )
            except httpx.HTTPError as e:
                if attempt == 2:
                    raise AvitoAPIError(f"{method} {path} network: {e}") from e
                await asyncio.sleep(0.5 * (attempt + 1))
                continue

            if resp.status_code == 200 or resp.status_code == 201:
                try:
                    return resp.json()
                except ValueError:
                    return {}
            if resp.status_code == 401:
                # Токен мог истечь раньше нашего предсказания — сбрасываем кеш и ретраим
                self._token = None
                self._token_expires_at = 0.0
                if attempt < 2:
                    continue
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = float(resp.headers.get("Retry-After", 1.0)) * (attempt + 1)
                logger.warning("%s %s -> %d, retry in %.1fs", method, path, resp.status_code, wait)
                await asyncio.sleep(wait)
                continue
            raise AvitoAPIError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        raise AvitoAPIError(f"{method} {path}: out of retries")

    # --- Endpoints ------------------------------------------------------

    async def get_account_self(self) -> dict[str, Any]:
        return await self._request("GET", "/core/v1/accounts/self")

    async def ensure_user_id(self) -> int:
        if self._user_id:
            return self._user_id
        info = await self.get_account_self()
        uid = int(info.get("id") or 0)
        if not uid:
            raise AvitoAPIError(f"cannot resolve user_id: {info}")
        self._user_id = uid
        return uid

    async def list_chats(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        uid = await self.ensure_user_id()
        data = await self._request(
            "GET", f"/messenger/v2/accounts/{uid}/chats",
            params={"limit": limit, "offset": offset, "unread_only": "false"},
        )
        return data.get("chats") or []

    async def get_messages(self, chat_id: str, *, limit: int = 30, offset: int = 0) -> list[dict[str, Any]]:
        uid = await self.ensure_user_id()
        data = await self._request(
            "GET", f"/messenger/v3/accounts/{uid}/chats/{chat_id}/messages/",
            params={"limit": limit, "offset": offset},
        )
        return data.get("messages") or []

    async def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        """Отправить текстовое сообщение в чат от имени продавца.

        Используется только если AVITO_WATCHER_SEND_ACK=true.
        """
        uid = await self.ensure_user_id()
        return await self._request(
            "POST", f"/messenger/v1/accounts/{uid}/chats/{chat_id}/messages",
            json_body={"message": {"text": text}, "type": "text"},
        )
