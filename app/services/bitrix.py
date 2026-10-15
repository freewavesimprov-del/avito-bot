"""Async REST-клиент Bitrix24 для локального приложения.

Все методы работают через `https://{domain}/rest/{method}.json` с access_token,
переданным в auth-параметре. На вебхуках используем свежий `auth.access_token`
из payload события — рефреш не делаем (для бота это не нужно).

Для одноразовых скриптов (`register_bot.py`, `b24_check.py`) используется
`B24_ACCESS_TOKEN` из `.env` — это тестовый токен, видимый в кабинете
локального приложения после установки.

Документация B24 REST: https://apidocs.bitrix24.ru/api-reference/
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger("avito_bot.bitrix")


class BitrixError(Exception):
    """Любая ошибка обращения к Bitrix REST."""


@dataclass
class BitrixCreds:
    """Тонкая обёртка над парой (домен, access_token).

    `domain` — `your.bitrix24.ru` (без схемы).
    `access_token` — берём из event.auth (вебхук) или из .env (одноразовые скрипты).
    """

    domain: str
    access_token: str

    @classmethod
    def from_env(cls) -> BitrixCreds:
        s = get_settings()
        if not s.B24_DOMAIN or not s.B24_ACCESS_TOKEN:
            raise BitrixError("B24_DOMAIN / B24_ACCESS_TOKEN не заданы в .env")
        return cls(domain=s.B24_DOMAIN, access_token=s.B24_ACCESS_TOKEN)

    @property
    def base_url(self) -> str:
        return f"https://{self.domain}/rest"


class BitrixClient:
    """Async-клиент B24 REST. Не держит долгоживущих соединений —
    создаёт httpx.AsyncClient на каждый вызов (для бота на ~1 RPS норм).
    """

    def __init__(self, creds: BitrixCreds, *, timeout_sec: float = 10.0):
        self._creds = creds
        self._timeout = httpx.Timeout(timeout_sec)

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, max_retries: int = 2
    ) -> dict[str, Any]:
        """Вызов любого REST-метода. Возвращает поле `result` из ответа B24.

        Ретраи на 5xx и сетевые ошибки. На 4xx (включая `error` в JSON) — поднимаем BitrixError.

        Вложенные dict/list плющим в PHP-style ключи (`PROPERTIES[NAME]=X`) — иначе
        B24 их не понимает и кидает `NAME_ERROR` или подобное.
        """
        url = f"{self._creds.base_url}/{method}.json"
        # Плющим вложенные dict в PHP-style ключи и собираем dict (дубликатов у нас нет).
        body = dict(_flatten_php({**(params or {}), "auth": self._creds.access_token}))

        attempt = 0
        last_exc: Exception | None = None
        while attempt <= max_retries:
            attempt += 1
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(url, data=body)
            except httpx.HTTPError as e:
                last_exc = e
                logger.warning("B24 %s network error (attempt %d): %s", method, attempt, e)
            else:
                # B24 даже на 200 может вернуть {"error": "...", "error_description": "..."}
                if resp.status_code >= 500:
                    last_exc = BitrixError(f"{method}: 5xx {resp.status_code}")
                    logger.warning("B24 %s 5xx (attempt %d): %d", method, attempt, resp.status_code)
                else:
                    try:
                        data = resp.json()
                    except ValueError as e:
                        raise BitrixError(f"{method}: invalid JSON: {resp.text[:200]}") from e
                    if "error" in data:
                        # Ошибки уровня REST: invalid_token, NO_AUTH_FOUND, etc.
                        raise BitrixError(
                            f"{method}: {data.get('error')} :: {data.get('error_description', '')}"
                        )
                    return data.get("result", data)

            if attempt <= max_retries:
                await asyncio.sleep(0.5 * attempt)

        raise BitrixError(f"{method}: failed after {attempt} attempts: {last_exc}")

    # --- imbot ----------------------------------------------------------

    async def imbot_message_add(self, *, bot_id: int, dialog_id: str, message: str) -> Any:
        """Отправить сообщение клиенту от имени бота."""
        return await self.call(
            "imbot.message.add",
            {"BOT_ID": bot_id, "DIALOG_ID": dialog_id, "MESSAGE": message},
        )

    async def imbot_chat_leave(self, *, bot_id: int, dialog_id: str) -> Any:
        """Бот выходит из чата — Открытая линия передаст диалог следующему оператору."""
        return await self.call(
            "imbot.chat.leave",
            {"BOT_ID": bot_id, "DIALOG_ID": dialog_id},
        )

    async def imbot_register(
        self,
        *,
        code: str,
        type_: str,
        event_handler: str,
        name: str,
        color: str = "GREEN",
        properties: dict[str, str] | None = None,
    ) -> int:
        """Регистрация бота на портале. Возвращает BOT_ID. Одноразовая операция."""
        params = {
            "CODE": code,
            "TYPE": type_,  # 'O' = бот для Открытых линий
            "EVENT_HANDLER": event_handler,
            "PROPERTIES": {
                "NAME": name,
                "LAST_NAME": "",
                "COLOR": color,
                **(properties or {}),
            },
        }
        return int(await self.call("imbot.register", params))

    async def imbot_unregister(self, *, bot_id: int) -> Any:
        return await self.call("imbot.unregister", {"BOT_ID": bot_id})

    # --- im (общая истории чата) ---------------------------------------

    async def im_dialog_messages_get(
        self, *, dialog_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """История сообщений чата (включая до подключения бота).

        Используется handler'ом, чтобы понять — не вёл ли уже оператор
        переписку в этом чате до того, как бот туда попал. Если да —
        бот не должен лезть.
        """
        result = await self.call(
            "im.dialog.messages.get",
            {"DIALOG_ID": dialog_id, "LIMIT": limit, "CONVERT_TEXT": "Y"},
        )
        if isinstance(result, dict):
            return result.get("messages") or []
        return []

    # --- crm ------------------------------------------------------------

    async def crm_timeline_comment_add(self, *, entity_type: str, entity_id: int, comment: str) -> Any:
        """Комментарий в таймлайн карточки CRM (lead/deal/contact и т.д.).
        entity_type: 'lead', 'deal', 'contact', 'company'.
        """
        return await self.call(
            "crm.timeline.comment.add",
            {"fields": {"ENTITY_ID": entity_id, "ENTITY_TYPE": entity_type, "COMMENT": comment}},
        )

    async def crm_deal_add(self, *, title: str, comments: str = "", opportunity: float | None = None,
                           source_id: str = "AVITO", extra_fields: dict[str, Any] | None = None) -> int:
        """Создать сделку (Deal) в B24 CRM. Возвращает ID сделки."""
        fields: dict[str, Any] = {
            "TITLE": title,
            "SOURCE_ID": source_id,
            "COMMENTS": comments,
        }
        if opportunity is not None:
            fields["OPPORTUNITY"] = opportunity
            fields["CURRENCY_ID"] = "RUB"
        if extra_fields:
            fields.update(extra_fields)
        result = await self.call("crm.deal.add", {"fields": fields})
        return int(result) if not isinstance(result, dict) else int(result.get("result") or 0)


# --- Удобные helpers для использования в хэндлерах --------------------------


def _flatten_php(obj: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Превратить вложенный dict в PHP-style form-данные.

    Пример:
        {"PROPERTIES": {"NAME": "Авито", "COLOR": "GREEN"}, "auth": "abc"}
      → [("PROPERTIES[NAME]", "Авито"), ("PROPERTIES[COLOR]", "GREEN"), ("auth", "abc")]

    Для list — индексируем: {"items": ["a", "b"]} → [("items[0]", "a"), ("items[1]", "b")].
    Bool сериализуем как 'Y'/'N' (B24-конвенция), None — как пустую строку.
    """
    out: list[tuple[str, str]] = []
    for k, v in obj.items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, dict):
            out.extend(_flatten_php(v, key))
        elif isinstance(v, (list, tuple)):
            for i, item in enumerate(v):
                child_key = f"{key}[{i}]"
                if isinstance(item, dict):
                    out.extend(_flatten_php(item, child_key))
                elif isinstance(item, bool):
                    out.append((child_key, "Y" if item else "N"))
                elif item is None:
                    out.append((child_key, ""))
                else:
                    out.append((child_key, str(item)))
        elif isinstance(v, bool):
            out.append((key, "Y" if v else "N"))
        elif v is None:
            out.append((key, ""))
        else:
            out.append((key, str(v)))
    return out


def client_from_event_auth(domain: str | None, access_token: str | None) -> BitrixClient:
    """Сделать клиент из (domain, access_token), которые приходят в event.auth.

    Если значения пустые — поднимем ошибку немедленно.
    """
    if not domain or not access_token:
        raise BitrixError("event.auth.domain / access_token отсутствуют")
    return BitrixClient(BitrixCreds(domain=domain, access_token=access_token))


def client_from_env() -> BitrixClient:
    """Клиент с токеном из .env (для одноразовых скриптов)."""
    return BitrixClient(BitrixCreds.from_env())


# --- OAuth refresh для долгоживущих скриптов (register_bot, b24_check, экспорты) ---

OAUTH_REFRESH_URL = "https://oauth.bitrix.info/oauth/token/"


async def refresh_access_token(
    refresh_token: str, client_id: str, client_secret: str
) -> dict[str, Any]:
    """Получить свежий access_token по refresh_token. Возвращает dict с полями
    `access_token`, `expires_in`, `refresh_token` (B24 ротирует и его).
    """
    if not (refresh_token and client_id and client_secret):
        raise BitrixError("refresh_access_token: пустые client_id/secret/refresh_token")
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as cli:
        resp = await cli.get(
            OAUTH_REFRESH_URL,
            params={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
        )
    if resp.status_code != 200:
        raise BitrixError(f"OAuth refresh failed: {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    if "access_token" not in data:
        raise BitrixError(f"OAuth refresh: no access_token in response: {data}")
    return data


async def client_from_install(client_id: str = "", client_secret: str = "") -> BitrixClient:
    """Клиент B24 на основе токенов, сохранённых в data/b24_install.json.

    Если access_token истёк — автоматически рефрешит через refresh_token (для этого
    нужны client_id/client_secret; передай их явно или положи в .env как
    `B24_CLIENT_ID` / `B24_CLIENT_SECRET`).

    Использовать в скриптах:
        client = await client_from_install()
        await client.imbot_register(...)
    """
    from app.services.b24_install import load_install, update_access_token

    inst = load_install()
    if not inst or not inst.access_token or not inst.domain:
        raise BitrixError(
            "data/b24_install.json не найден или пуст. "
            "Сначала установи приложение в B24 (нажми 'Переустановить' в карточке)."
        )

    token = inst.access_token
    if inst.access_token_expired and inst.refresh_token:
        s = get_settings()
        cid = client_id or s.B24_CLIENT_ID
        csec = client_secret or s.B24_CLIENT_SECRET
        if not cid or not csec:
            raise BitrixError(
                "access_token истёк, нужен refresh; задай B24_CLIENT_ID / B24_CLIENT_SECRET в .env"
            )
        logger.info("access_token expired, refreshing via OAuth")
        fresh = await refresh_access_token(inst.refresh_token, cid, csec)
        token = fresh["access_token"]
        update_access_token(
            access_token=token,
            expires_in=int(fresh.get("expires_in") or 3600),
            refresh_token=fresh.get("refresh_token"),
        )

    return BitrixClient(BitrixCreds(domain=inst.domain, access_token=token))
