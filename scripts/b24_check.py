"""Sanity-check связи с Bitrix24.

Запуск:
    .venv/bin/python -m scripts.b24_check                     # дёрнет profile
    .venv/bin/python -m scripts.b24_check --send DIALOG_ID    # отправит test-сообщение

Используется для проверки, что .env заполнен корректно ДО регистрации бота.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import get_settings
from app.services.b24_install import load_install
from app.services.bitrix import BitrixError, client_from_install


async def check_profile() -> None:
    inst = load_install()
    if inst is None:
        print(
            "[err] data/b24_install.json не найден.\n"
            "       Сначала установи (или переустанови) приложение в B24 — токены сохранятся автоматически.",
            file=sys.stderr,
        )
        sys.exit(2)
    print(
        f"[ok] install найден: domain={inst.domain}, "
        f"app_token={'set' if inst.application_token else 'EMPTY'}, "
        f"access_expired={inst.access_token_expired}"
    )
    client = await client_from_install()
    print("[..] вызываю profile (данные текущего пользователя)…")
    res = await client.call("profile")
    print(f"[ok] profile: ID={res.get('ID')}, NAME={res.get('NAME')} {res.get('LAST_NAME', '')}")
    print(f"     ADMIN={res.get('ADMIN')}, EMAIL={res.get('EMAIL')}")


async def send_test(dialog_id: str, text: str) -> None:
    settings = get_settings()
    if not settings.B24_BOT_ID:
        print("[err] B24_BOT_ID не задан в .env — сначала запусти register_bot.py", file=sys.stderr)
        sys.exit(2)
    client = await client_from_install()
    print(f"[..] шлю тестовое сообщение в DIALOG_ID={dialog_id}")
    await client.imbot_message_add(
        bot_id=settings.B24_BOT_ID, dialog_id=dialog_id, message=text
    )
    print("[ok] отправлено")


def cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", metavar="DIALOG_ID", help="отправить тестовое сообщение")
    ap.add_argument(
        "--text",
        default="Тестовое сообщение от бота — связь работает.",
        help="текст для --send",
    )
    args = ap.parse_args()

    try:
        if args.send:
            asyncio.run(send_test(args.send, args.text))
        else:
            asyncio.run(check_profile())
    except BitrixError as e:
        print(f"[err] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()
