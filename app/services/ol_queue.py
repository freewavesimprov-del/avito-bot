"""Состав очереди Открытой линии Avito по расписанию.

Зачем. С понедельника по пятницу клиентов ведёт магазин прямо в кабинете
Авито, и диалоги не должны падать на всех менеджеров: карточка в CRM
появляется на каждое обращение и распределяется по очереди линии. Само
расписание Открытой линии этого не решает — оно лишь выбирает, чем
ответить в нерабочее время, а очередь и создание лида работают всегда.

Поэтому очередь держим мы: в будни в ней остаётся один человек (тот, кто
ведёт переписку в кабинете), а с вечера пятницы до вечера воскресенья
возвращается полный состав — выходные менеджеры разбирают в Битриксе, как
раньше. Момент переключения тот же, что у окна передачи диалогов
(`schedule.b24_forward_allowed_now`), чтобы две половины не разъезжались.

Состав выходных берём из настроек, а не из текущего состояния линии: если
кто-то уйдёт из очереди руками, в субботу он вернётся.
"""

from __future__ import annotations

import asyncio
import logging

from app.services import bitrix as B
from app.services.b24_install import load_install
from app.services.schedule import b24_forward_allowed_now, describe_now
from app.config import get_settings

logger = logging.getLogger("avito_bot.ol_queue")


def _client() -> B.BitrixClient:
    inst = load_install()
    return B.BitrixClient(B.BitrixCreds(domain=inst.domain,
                                        access_token=inst.access_token))


def _wanted() -> list[str]:
    s = get_settings()
    weekend = [x.strip() for x in str(s.OL_QUEUE_WEEKEND).split(",") if x.strip()]
    weekday = [x.strip() for x in str(s.OL_QUEUE_WEEKDAY).split(",") if x.strip()]
    return weekend if b24_forward_allowed_now() else weekday


async def current_queue(client: B.BitrixClient, line_id: int) -> list[str]:
    got = await client.call("imopenlines.config.get",
                            {"CONFIG_ID": line_id, "WITH_QUEUE": "Y"})
    cfg = got.get("result", got) if isinstance(got, dict) else got
    seen, out = set(), []
    for uid in (cfg.get("QUEUE") or []):
        uid = str(uid)
        if uid not in seen:
            seen.add(uid)
            out.append(uid)
    return out


async def apply_now(dry: bool = False) -> dict:
    """Привести очередь линии к тому составу, который положен сейчас."""
    s = get_settings()
    line = int(s.OL_QUEUE_LINE_ID)
    client = _client()
    want = _wanted()
    have = await current_queue(client, line)
    if set(have) == set(want):
        return {"changed": False, "queue": have, "when": describe_now()}
    if dry:
        return {"changed": False, "would_be": want, "queue": have, "dry": True}
    await client.call("imopenlines.config.update", {
        "CONFIG_ID": line,
        "PARAMS": {"QUEUE": want},
    })
    logger.info("очередь линии %s: было %s, стало %s — %s",
                line, ",".join(have), ",".join(want), describe_now())
    return {"changed": True, "was": have, "queue": want, "when": describe_now()}


class QueueKeeper:
    """Фоновая служба: раз в несколько минут сверяет состав очереди."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        s = get_settings()
        if not s.OL_QUEUE_ENABLE:
            logger.info("ol queue keeper disabled (OL_QUEUE_ENABLE=false)")
            return
        self._task = asyncio.create_task(self._run(), name="ol_queue")
        logger.info("ol queue keeper: линия %s, будни %s, выходные %s",
                    s.OL_QUEUE_LINE_ID, s.OL_QUEUE_WEEKDAY, s.OL_QUEUE_WEEKEND)

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        interval = int(get_settings().OL_QUEUE_INTERVAL_SEC)
        while True:
            try:
                await apply_now()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ol queue keeper: тик не удался")
            await asyncio.sleep(interval)


keeper = QueueKeeper()
