"""Выгрузка истории Открытых линий из Bitrix24 для replay через chat_engine.

Тащит:
  - все сессии указанной Открытой линии (или всех ОЛ — если --all-lines)
  - сообщения каждой сессии через im.dialog.messages.get
  - метаданные (lead/deal id если есть, статусы, время начала/закрытия)

Сохраняет в data/b24_chats_dataset.json в формате, совместимом с avito_replay.py
(те же поля messages[].text/.direction/.created), плюс расширенный meta-блок
для разметки и сравнения «бот vs живой менеджер».

Запуск:
    .venv/bin/python -m scripts.b24_export_chats --line-id 5
    .venv/bin/python -m scripts.b24_export_chats --all-lines --max-sessions 200
    .venv/bin/python -m scripts.b24_export_chats --since 2026-04-25

Авторизация — через data/b24_install.json (тот же install, что и для бота).
Refresh access_token делается автоматически при необходимости.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from app.services.bitrix import BitrixError, client_from_install

logger = logging.getLogger("b24_export_chats")

REPLAY_DATASET_PATH = Path("data/b24_chats_dataset.json")


# --- helpers --------------------------------------------------------------


async def _list_open_lines(client) -> list[dict[str, Any]]:
    """Список Открытых линий портала."""
    try:
        return await client.call("imopenlines.config.list.get") or []
    except BitrixError as e:
        logger.warning("imopenlines.config.list.get недоступен: %s", e)
        return []


async def _list_sessions(
    client,
    line_id: int | None,
    since_ts: int | None,
    max_sessions: int,
) -> list[dict[str, Any]]:
    """Сессии Открытой линии. Постранично через imopenlines.session.list."""
    out: list[dict[str, Any]] = []
    start = 0
    page_limit = 50
    while len(out) < max_sessions:
        params: dict[str, Any] = {"start": start, "ORDER": {"DATE_CREATE": "DESC"}}
        if line_id is not None:
            params["FILTER"] = {"CONFIG_ID": line_id}
        if since_ts is not None:
            params.setdefault("FILTER", {})["DATE_CREATE_GREATER"] = (
                dt.datetime.fromtimestamp(since_ts).isoformat()
            )

        try:
            data = await client.call("imopenlines.session.list", params)
        except BitrixError as e:
            logger.warning("imopenlines.session.list упал: %s", e)
            break
        items = data.get("sessions") if isinstance(data, dict) else data
        if not items:
            break
        out.extend(items)
        if len(items) < page_limit:
            break
        start += len(items)
    return out[:max_sessions]


async def _get_dialog_messages(
    client, dialog_id: str, limit: int = 200
) -> list[dict[str, Any]]:
    """Сообщения чата через im.dialog.messages.get."""
    try:
        data = await client.call(
            "im.dialog.messages.get",
            {"DIALOG_ID": dialog_id, "LIMIT": limit, "CONVERT_TEXT": "Y"},
        )
    except BitrixError as e:
        logger.warning("im.dialog.messages.get(%s) упал: %s", dialog_id, e)
        return []
    if isinstance(data, dict):
        return data.get("messages") or []
    return []


def _normalize_message(msg: dict[str, Any], operator_user_ids: set[int]) -> dict[str, Any]:
    """Нормализация сообщения B24 к формату replay (text + direction)."""
    text = msg.get("text") or ""
    if not text and msg.get("params"):
        # вложенные attach-блоки (картинки, файлы) — заменяем на маркер
        text = "[вложение]"
    author_id = msg.get("author_id")
    # Эвристика: если author в списке операторов портала — это менеджер (out),
    # если нет — клиент (in). При выгрузке мы передаём seller_user_ids явно.
    is_seller = author_id in operator_user_ids
    return {
        "id": msg.get("id"),
        "author_id": author_id,
        "direction": "out" if is_seller else "in",
        "type": "text",
        "text": text,
        "created": _to_unix(msg.get("date")),
    }


def _to_unix(s: Any) -> int | None:
    if isinstance(s, int):
        return s
    if not s:
        return None
    try:
        return int(dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


# --- main -----------------------------------------------------------------


async def main(args: argparse.Namespace) -> int:
    try:
        client = await client_from_install()
    except BitrixError as e:
        print(f"[err] {e}", file=sys.stderr)
        return 2

    # Кого считать продавцом-оператором
    operator_user_ids: set[int] = set()
    if args.operator_ids:
        operator_user_ids = {int(x) for x in args.operator_ids.split(",")}

    # Линии
    if args.line_id:
        line_filter: int | None = int(args.line_id)
    elif args.all_lines:
        line_filter = None
    else:
        # по умолчанию — выводим список линий и просим выбрать
        lines = await _list_open_lines(client)
        if not lines:
            print(
                "[err] не удалось получить список ОЛ — укажи --line-id <id> или --all-lines",
                file=sys.stderr,
            )
            return 2
        print("[info] доступные Открытые линии:")
        for ln in lines:
            print(f"  ID={ln.get('ID')} :: {ln.get('LINE_NAME') or ln.get('NAME') or '—'}")
        print("\nЗапусти с --line-id <id> или --all-lines")
        return 0

    since_ts: int | None = None
    if args.since:
        since_ts = int(dt.datetime.fromisoformat(args.since).timestamp())

    print(f"[..] выгружаю сессии (line_id={line_filter}, since={args.since})")
    sessions = await _list_sessions(client, line_filter, since_ts, args.max_sessions)
    print(f"[ok] сессий получено: {len(sessions)}")

    bundles: list[dict[str, Any]] = []
    msg_total = 0
    for i, sess in enumerate(sessions, 1):
        chat_id = sess.get("CHAT_ID") or sess.get("chat_id")
        if not chat_id:
            continue
        dialog_id = f"chat{chat_id}"
        msgs_raw = await _get_dialog_messages(client, dialog_id, limit=args.max_messages_per_chat)
        msgs = [_normalize_message(m, operator_user_ids) for m in msgs_raw]
        # Сортируем по времени (старое → новое)
        msgs.sort(key=lambda m: m.get("created") or 0)
        msg_total += len(msgs)

        bundles.append(
            {
                "id": dialog_id,
                "session_id": sess.get("ID") or sess.get("SESSION_ID"),
                "line_id": sess.get("CONFIG_ID"),
                "created": _to_unix(sess.get("DATE_CREATE")),
                "closed": _to_unix(sess.get("DATE_CLOSE")),
                "status": sess.get("STATUS"),
                # Поля для разметки и сравнения «бот vs живой менеджер»:
                "lead_id": sess.get("CRM_CREATE_LEAD") or sess.get("LEAD_ID"),
                "deal_id": sess.get("DEAL_ID"),
                "operator_id": sess.get("OPERATOR_ID"),
                "context": {"type": "open_line", "value": {}},
                "messages": msgs,
            }
        )
        if i % 10 == 0:
            print(f"  ... обработано {i}/{len(sessions)}, сообщений всего: {msg_total}")

    REPLAY_DATASET_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPLAY_DATASET_PATH.write_text(
        json.dumps(
            {
                "meta": {
                    "fetched_at": int(time.time()),
                    "source": "bitrix24_open_lines",
                    "line_id": line_filter,
                    "operator_user_ids": sorted(operator_user_ids),
                    "chats_total": len(bundles),
                    "messages_total": msg_total,
                },
                "chats": bundles,
                "orders": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n[done] сохранено в {REPLAY_DATASET_PATH}")
    print(f"       чатов: {len(bundles)}, сообщений: {msg_total}")
    print("\nДальше можно прогнать через бота:")
    print(f"  .venv/bin/python -m scripts.avito_replay --dataset {REPLAY_DATASET_PATH}")
    return 0


def cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--line-id", type=int, default=None, help="ID конкретной Открытой линии (Авито)")
    ap.add_argument("--all-lines", action="store_true", help="все ОЛ портала")
    ap.add_argument("--since", default=None, help="ISO-дата 'YYYY-MM-DD', выгружать с этого момента")
    ap.add_argument("--max-sessions", type=int, default=500)
    ap.add_argument("--max-messages-per-chat", type=int, default=200)
    ap.add_argument(
        "--operator-ids",
        default="",
        help="ID операторов через запятую — их сообщения будут direction=out (менеджер)",
    )
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    sys.exit(asyncio.run(main(args)))


if __name__ == "__main__":
    cli()
