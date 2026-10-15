"""Привязать все наши event-handlers к webhook."""

from __future__ import annotations
import asyncio
from app.services.bitrix import client_from_install

WEBHOOK = "https://avito-bot.n8nrgimprovise.space/b24/webhook"
EVENTS = [
    "ONAPPINSTALL", "ONAPPUPDATE",
    "ONIMBOTMESSAGEADD", "ONIMBOTJOINCHAT", "ONIMBOTDELETE",
    "ONIMBOTMESSAGEUPDATE", "ONIMBOTMESSAGEDELETE", "ONIMBOTCONTEXTGET",
]

async def main():
    c = await client_from_install()
    for ev in EVENTS:
        try:
            r = await c.call("event.bind", {"event": ev, "handler": WEBHOOK})
            print(f"[ok] bind {ev} → {r}")
        except Exception as e:
            print(f"[err] {ev}: {e}")

asyncio.run(main())
