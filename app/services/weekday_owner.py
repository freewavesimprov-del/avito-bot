"""Будни: новый диалог линии Avito сразу адресуется магазину.

Зачем. Бот стоит в линии первым, и Битрикс держит диалог на нём десять минут,
после чего сам рассылает обращение всей очереди менеджеров. В будни клиентов
ведёт Татьяна из кабинета Авито, и в очередь диалог попадать не должен.
Поэтому бот при входе в чат переводит сессию на неё адресно и остаётся в
чате сам: ночью он отвечает клиенту как раньше.

В окне выходных (с 17:00 пятницы до 19:00 воскресенья) ничего не делаем —
диалог идёт менеджерам обычным путём.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from app.config import get_settings
from app.services.bitrix import client_from_install
from app.services.logger import log_event
from app.services.schedule import b24_forward_allowed_now, bot_active_now, describe_now

logger = logging.getLogger("avito_bot.weekday_owner")

# Линия держит диалог на боте 600 секунд (WELCOME_BOT_TIME), и через API
# это время не меняется. Передаём с запасом в минуту.
HOLD_SEC = 540

# Отложенные передачи пишем на диск: перезапуск бота обрывал ожидание, и
# диалог через 10 минут уходил всей очереди (01.10.2026, chat166254).
_PENDING = Path(__file__).resolve().parents[2] / "data" / "pending_handover.json"


def _pending() -> dict[str, float]:
    try:
        return json.loads(_PENDING.read_text(encoding="utf-8"))
    except Exception:                                     # noqa: BLE001
        return {}


def _remember(dialog_id: str, due: float | None) -> None:
    data = _pending()
    if due is None:
        data.pop(dialog_id, None)
    else:
        data[dialog_id] = due
    now = time.time()
    data = {k: v for k, v in data.items() if v > now - 3600}
    try:
        _PENDING.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


async def resume_pending() -> None:
    """После перезапуска досрочно передать то, что ждало передачи."""
    now = time.time()
    for dialog_id, due in list(_pending().items()):
        if due < now - 120:
            _remember(dialog_id, None)        # уже ушло очереди, поздно
            continue
        asyncio.create_task(hand_over(dialog_id, wait=max(0.0, due - now)))


async def hand_over(dialog_id: str, wait: float | None = None) -> dict:
    s = get_settings()
    if not s.LEAD_ROUTER_ENABLE or b24_forward_allowed_now():
        return {"skipped": True}
    if not str(dialog_id).startswith("chat"):
        return {"skipped": True, "why": "не чат линии"}
    chat_id = str(dialog_id)[4:]
    # Вечером и ночью бот отвечает сам, а после передачи писать в чат уже не
    # может: Битрикс отвечает «Вы не можете отправлять сообщения в указанный
    # чат» (30.09.2026, 18:49). Поэтому в часы бота передаём не сразу, а перед
    # тем, как линия через 10 минут сама разослала бы диалог очереди. Раньше
    # бот после этих 10 минут тоже уже не вёл диалог — меняется только адресат.
    delay = wait if wait is not None else (HOLD_SEC if bot_active_now() else 0)
    if delay:
        _remember(dialog_id, time.time() + delay)
        await asyncio.sleep(delay)
        if b24_forward_allowed_now():
            _remember(dialog_id, None)
            return {"skipped": True, "why": "началось окно выходных"}
    owner = int(s.LEAD_ROUTER_WEEKDAY_USER)
    try:
        client = await client_from_install()
        await client.call("imopenlines.bot.session.transfer", {
            "CHAT_ID": chat_id, "USER_ID": owner, "LEAVE": "N",
        })
    except Exception as exc:                              # noqa: BLE001
        logger.warning("диалог %s не передан магазину: %s", dialog_id, str(exc)[:200])
        _remember(dialog_id, None)
        await log_event(dialog_id, "system", f"weekday_handover_failed: {str(exc)[:200]}")
        return {"ok": False, "error": str(exc)[:200]}
    _remember(dialog_id, None)
    logger.info("диалог %s передан на %s — %s", dialog_id, owner, describe_now())
    await log_event(dialog_id, "system", f"weekday_handover: user {owner}")
    return {"ok": True}
