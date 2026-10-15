"""Откат: отвязать events нашего приложения."""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.services.bitrix import BitrixError, client_from_install

EVENTS = [
    "ONAPPINSTALL", "ONAPPUPDATE", "ONIMBOTMESSAGEADD", "ONIMBOTJOINCHAT",
    "ONIMBOTDELETE", "ONIMBOTMESSAGEUPDATE", "ONIMBOTMESSAGEDELETE", "ONIMBOTCONTEXTGET",
]


async def status() -> None:
    client = await client_from_install()
    res = await client.call("event.get", {})
    items = res if isinstance(res, list) else (res or {}).get("result", res)
    print("[ok] bindings:")
    for it in items or []:
        print(f"  - {it}")


async def unbind_all(bot_id: int | None) -> None:
    client = await client_from_install()
    res = await client.call("event.get", {})
    items = res if isinstance(res, list) else (res or {}).get("result", res)
    for it in items or []:
        ev = it.get("event")
        handler = it.get("handler")
        try:
            r = await client.call("event.unbind", {"event": ev, "handler": handler})
            print(f"[ok] unbind {ev} ({handler}) → {r}")
        except BitrixError as e:
            print(f"[err] unbind {ev}: {e}")
    if bot_id is not None:
        try:
            await client.imbot_unregister(bot_id=bot_id)
            print(f"[ok] imbot.unregister BOT_ID={bot_id}")
        except BitrixError as e:
            print(f"[err] imbot.unregister: {e}")


def cli() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_un = sub.add_parser("unbind")
    p_un.add_argument("--bot-id", type=int, default=None)
    sub.add_parser("status")
    args = ap.parse_args()
    try:
        if args.cmd == "unbind":
            asyncio.run(unbind_all(args.bot_id))
        elif args.cmd == "status":
            asyncio.run(status())
    except BitrixError as e:
        print(f"[err] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()
