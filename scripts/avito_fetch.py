"""Разовая выгрузка чатов / сообщений / заказов через Avito API.

Использование:
    .venv/bin/python -m scripts.avito_fetch                         # все чаты, до 200 сообщений в каждом
    .venv/bin/python -m scripts.avito_fetch --max-chats 50          # только последние 50
    .venv/bin/python -m scripts.avito_fetch --no-orders             # без заказов
    .venv/bin/python -m scripts.avito_fetch --out custom_path.json  # свой путь

Креды берутся из .env: AVITO_CLIENT_ID, AVITO_CLIENT_SECRET (+ опц. AVITO_USER_ID).
Результат сохраняется в data/avito_dataset.json:

{
  "meta": {"fetched_at": "...", "user_id": 123, "chats_total": 42, "messages_total": 350},
  "chats": [
    {
      "id": "chat-id",
      "created": 1700000000,
      "users": [{"id": 123, "name": "ШопимВместе"}, {"id": 999, "name": "Иван"}],
      "context": {"type": "item", "value": {"id": 12345, "title": "Комод..."}},
      "messages": [
        {"id": "...", "author_id": 999, "direction": "in", "type": "text",
         "text": "Здравствуйте, есть в наличии?", "created": 1700000010}
      ]
    }
  ],
  "orders": [...]
}

API:
  POST /token/                                              — OAuth client_credentials
  GET  /core/v1/accounts/self                              — узнать свой user_id
  GET  /messenger/v2/accounts/{uid}/chats                  — список чатов
  GET  /messenger/v3/accounts/{uid}/chats/{cid}/messages/  — сообщения чата
  GET  /core/v1/accounts/{uid}/operations_history          — заказы и операции
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

# Запускаем как модуль (`python -m scripts.avito_fetch`), поэтому пакет app виден
from app.config import get_settings

logger = logging.getLogger("avito_fetch")

API_BASE = "https://api.avito.ru"


class AvitoAPIError(Exception):
    """Ошибка обращения к Avito API."""


# --- Auth -----------------------------------------------------------------


async def get_access_token(client: httpx.AsyncClient, client_id: str, client_secret: str) -> str:
    """OAuth client_credentials. TTL ~24h, для одной выгрузки достаточно."""
    resp = await client.post(
        f"{API_BASE}/token/",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code != 200:
        raise AvitoAPIError(f"OAuth failed: {resp.status_code} {resp.text[:300]}")
    payload = resp.json()
    token = payload.get("access_token")
    if not token:
        raise AvitoAPIError(f"OAuth: token missing in response: {payload}")
    logger.info("got access_token, expires_in=%s, scope=%s", payload.get("expires_in"), payload.get("scope"))
    return token


# --- Helpers --------------------------------------------------------------


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


async def _get_json(
    client: httpx.AsyncClient, token: str, path: str, *, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    """GET с экспоненциальным ретраем на 429/5xx."""
    for attempt in range(3):
        try:
            resp = await client.get(f"{API_BASE}{path}", headers=_auth_headers(token), params=params)
        except httpx.HTTPError as e:
            if attempt == 2:
                raise AvitoAPIError(f"GET {path} network: {e}") from e
            await asyncio.sleep(0.5 * (attempt + 1))
            continue

        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 500, 502, 503, 504):
            wait = float(resp.headers.get("Retry-After", 1.0)) * (attempt + 1)
            logger.warning("GET %s -> %d, retry in %.1fs", path, resp.status_code, wait)
            await asyncio.sleep(wait)
            continue
        # 4xx, кроме 429 — без ретраев
        raise AvitoAPIError(f"GET {path} -> {resp.status_code}: {resp.text[:300]}")
    raise AvitoAPIError(f"GET {path}: out of retries")


# --- Endpoints ------------------------------------------------------------


async def fetch_self(client: httpx.AsyncClient, token: str) -> dict[str, Any]:
    return await _get_json(client, token, "/core/v1/accounts/self")


async def fetch_all_chats(
    client: httpx.AsyncClient, token: str, user_id: int, *, max_chats: int | None = None
) -> AsyncIterator[dict[str, Any]]:
    """Итерируем все чаты постранично (limit=100)."""
    limit = 100
    offset = 0
    yielded = 0
    while True:
        data = await _get_json(
            client,
            token,
            f"/messenger/v2/accounts/{user_id}/chats",
            params={"limit": limit, "offset": offset, "unread_only": "false"},
        )
        chats = data.get("chats") or data.get("resources") or []
        if not chats:
            return
        for c in chats:
            yield c
            yielded += 1
            if max_chats is not None and yielded >= max_chats:
                return
        if len(chats) < limit:
            return
        offset += limit


async def fetch_chat_messages(
    client: httpx.AsyncClient,
    token: str,
    user_id: int,
    chat_id: str,
    *,
    max_messages: int = 200,
) -> list[dict[str, Any]]:
    """Возвращает все сообщения чата, отсортированные по времени (старое → новое)."""
    out: list[dict[str, Any]] = []
    limit = 100
    offset = 0
    while len(out) < max_messages:
        data = await _get_json(
            client,
            token,
            f"/messenger/v3/accounts/{user_id}/chats/{chat_id}/messages/",
            params={"limit": min(limit, max_messages - len(out)), "offset": offset},
        )
        items = data.get("messages") or data.get("resources") or []
        if not items:
            break
        out.extend(items)
        if len(items) < limit:
            break
        offset += limit
    out.sort(key=lambda m: m.get("created") or 0)
    return out


async def fetch_orders(
    client: httpx.AsyncClient, token: str, user_id: int, *, max_items: int = 500
) -> list[dict[str, Any]]:
    """История операций по аккаунту. У Авито нет прямого 'orders' endpoint — ближайшее это
    operations_history (включает доставку Авито). Если scope не разрешает — отдаст 403,
    мы это перехватим и вернём пустой список.
    """
    out: list[dict[str, Any]] = []
    limit = 100
    offset = 0
    try:
        while len(out) < max_items:
            data = await _get_json(
                client,
                token,
                f"/core/v1/accounts/{user_id}/operations_history",
                params={"limit": min(limit, max_items - len(out)), "offset": offset},
            )
            ops = data.get("operations") or data.get("result", {}).get("operations") or []
            if not ops:
                break
            out.extend(ops)
            if len(ops) < limit:
                break
            offset += limit
    except AvitoAPIError as e:
        logger.warning("orders/operations_history недоступны: %s", e)
        return []
    return out


# --- Main -----------------------------------------------------------------


def _normalize_message(msg: dict[str, Any], seller_id: int) -> dict[str, Any]:
    """Извлекаем плоское представление сообщения для replay/анализа."""
    content = msg.get("content") or {}
    text = content.get("text") or ""
    if not text and "call" in content:
        text = "[звонок]"
    if not text and "image" in content:
        text = "[изображение]"
    if not text and "voice" in content:
        text = "[голосовое]"
    if not text and "location" in content:
        text = "[геолокация]"
    if not text and "item" in content:
        text = f"[карточка товара: {content.get('item', {}).get('title', '?')}]"
    if not text and "link" in content:
        text = f"[ссылка: {content.get('link', {}).get('url', '?')}]"

    author_id = msg.get("author_id")
    direction = "out" if author_id == seller_id else "in"
    return {
        "id": msg.get("id"),
        "author_id": author_id,
        "direction": direction,
        "type": msg.get("type") or "text",
        "text": text,
        "created": msg.get("created"),
        "isRead": msg.get("isRead"),
    }


async def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.AVITO_CLIENT_ID or not settings.AVITO_CLIENT_SECRET:
        print("ERROR: AVITO_CLIENT_ID / AVITO_CLIENT_SECRET не заданы в .env", file=sys.stderr)
        return 2

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        token = await get_access_token(
            client, settings.AVITO_CLIENT_ID, settings.AVITO_CLIENT_SECRET
        )

        # user_id: из .env или через self
        if settings.AVITO_USER_ID:
            user_id = settings.AVITO_USER_ID
            self_info: dict[str, Any] = {"id": user_id, "source": "env"}
        else:
            self_info = await fetch_self(client, token)
            user_id = int(self_info.get("id") or 0)
            if not user_id:
                print(f"ERROR: не удалось получить user_id, ответ self: {self_info}", file=sys.stderr)
                return 2
        print(f"[ok] user_id={user_id}")

        # Чаты
        chats: list[dict[str, Any]] = []
        async for c in fetch_all_chats(client, token, user_id, max_chats=args.max_chats):
            chats.append(c)
        print(f"[ok] чатов получено: {len(chats)}")

        # Сообщения по каждому чату
        bundles: list[dict[str, Any]] = []
        msg_total = 0
        for i, chat in enumerate(chats, 1):
            cid = chat.get("id")
            if not cid:
                continue
            try:
                msgs_raw = await fetch_chat_messages(
                    client, token, user_id, cid, max_messages=args.max_messages_per_chat
                )
            except AvitoAPIError as e:
                logger.warning("chat %s: skip messages (%s)", cid, e)
                msgs_raw = []
            msgs = [_normalize_message(m, user_id) for m in msgs_raw]
            msg_total += len(msgs)

            ctx = chat.get("context") or {}
            bundles.append(
                {
                    "id": cid,
                    "created": chat.get("created"),
                    "users": chat.get("users", []),
                    "context": ctx,
                    "last_message_created": (chat.get("last_message") or {}).get("created"),
                    "messages": msgs,
                }
            )
            if i % 10 == 0:
                print(f"  ... обработано {i}/{len(chats)} чатов, сообщений всего: {msg_total}")

        # Заказы / операции
        orders: list[dict[str, Any]] = []
        if not args.no_orders:
            print("[..] выгружаю операции / заказы")
            orders = await fetch_orders(client, token, user_id, max_items=args.max_orders)
            print(f"[ok] операций / заказов: {len(orders)}")

        result = {
            "meta": {
                "fetched_at": int(time.time()),
                "user_id": user_id,
                "self": self_info,
                "chats_total": len(bundles),
                "messages_total": msg_total,
                "orders_total": len(orders),
            },
            "chats": bundles,
            "orders": orders,
        }

    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[done] сохранено в {out_path}")
    print(f"       чатов: {len(bundles)}, сообщений: {msg_total}, операций: {len(orders)}")
    return 0


def cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/avito_dataset.json", help="куда сохранить JSON")
    ap.add_argument("--max-chats", type=int, default=None, help="ограничить число чатов")
    ap.add_argument(
        "--max-messages-per-chat", type=int, default=200, help="лимит сообщений на чат"
    )
    ap.add_argument("--max-orders", type=int, default=500)
    ap.add_argument("--no-orders", action="store_true", help="не тащить операции/заказы")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    sys.exit(asyncio.run(main(args)))


if __name__ == "__main__":
    cli()
