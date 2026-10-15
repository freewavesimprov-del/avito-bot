"""Одноразовая регистрация бота на портале Bitrix24.

Берёт токены из data/b24_install.json (сохраняются автоматически при
первой установке приложения в B24, см. RUNBOOK §1-7).

Запуск:
    .venv/bin/python -m scripts.register_bot register \
        --webhook-url https://avito-bot.example.ru/b24/webhook

После успеха:
    1. Скрипт выведет BOT_ID.
    2. Положи его в .env: B24_BOT_ID=<id>.
    3. Перезапусти сервис (systemctl restart avito-bot).

Если бот уже зарегистрирован и хочется перерегистрировать — сначала
`unregister BOT_ID`. После регистрации бот появится в списке Открытых линий.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.services.bitrix import BitrixError, client_from_install


async def register(webhook_url: str, name: str, code: str) -> int:
    client = await client_from_install()
    print(f"[..] регистрирую бота '{name}' (code='{code}', handler={webhook_url})")
    bot_id = await client.imbot_register(
        code=code,
        type_="O",  # Open Lines
        event_handler=webhook_url,
        name=name,
        color="GREEN",
    )
    print(f"[ok] BOT_ID = {bot_id}")
    print()
    print("Положи в .env:")
    print(f"  B24_BOT_ID={bot_id}")
    return bot_id


async def unregister(bot_id: int) -> None:
    client = await client_from_install()
    print(f"[..] разрегистрирую бота {bot_id}")
    await client.imbot_unregister(bot_id=bot_id)
    print("[ok] разрегистрирован")


def cli() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_reg = sub.add_parser("register", help="Зарегистрировать бота (по умолчанию)")
    p_reg.add_argument("--webhook-url", required=True, help="HTTPS-URL обработчика, напр. https://avito-bot.example.ru/b24/webhook")
    p_reg.add_argument("--name", default="Авито-ассистент")
    p_reg.add_argument("--code", default="avito_assistant", help="Уникальный код бота на портале")

    p_un = sub.add_parser("unregister", help="Разрегистрировать существующего")
    p_un.add_argument("bot_id", type=int)

    args = ap.parse_args()

    try:
        if args.cmd == "register":
            asyncio.run(register(args.webhook_url, args.name, args.code))
        elif args.cmd == "unregister":
            asyncio.run(unregister(args.bot_id))
    except BitrixError as e:
        print(f"[err] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()
