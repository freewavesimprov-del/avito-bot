"""Ответственный за лиды Авито: в будни — магазин, в выходные — очередь.

Зачем. С понедельника по пятницу клиентов ведёт Татьяна прямо в кабинете
Авито, и карточка в Битриксе не должна расходиться по всем менеджерам.
Очередь Открытой линии для этого не годится: её состав Битрикс не даёт
менять извне, а расписания у неё нет. Зато готовый лид можно переназначить:
линия создала карточку и раздала по очереди — мы тут же возвращаем её тому,
кто в этот день и правда работает с клиентом.

Окно то же, что у передачи диалогов (`schedule.b24_forward_allowed_now`):
с 17:00 пятницы до 19:00 воскресенья лиды остаются у того менеджера,
которому их отдала линия. В остальное время ответственным становится
магазин.

Трогаем только лиды источника Avito и только свежие: чужие карточки и
старую историю не переписываем.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from app.config import get_settings
from app.services import bitrix as B
from app.services.b24_install import load_install
from app.services.schedule import (
    MSK, b24_forward_allowed_at, b24_forward_allowed_now, describe_now,
)

logger = logging.getLogger("avito_bot.lead_router")


def _client() -> B.BitrixClient:
    inst = load_install()
    return B.BitrixClient(B.BitrixCreds(domain=inst.domain,
                                        access_token=inst.access_token))


async def route_once() -> dict:
    """Один проход: свежие лиды Авито, созданные в будни, — на магазин."""
    s = get_settings()
    if not s.LEAD_ROUTER_ENABLE:
        return {"skipped": "выключено"}
    if b24_forward_allowed_now():
        # Выходные: линия раздала лиды менеджерам, так и задумано.
        return {"changed": 0, "why": "окно выходных", "when": describe_now()}

    owner = str(s.LEAD_ROUTER_WEEKDAY_USER)
    sources = [x.strip() for x in str(s.LEAD_ROUTER_SOURCES).split(",") if x.strip()]
    since = (datetime.now(MSK) - timedelta(minutes=int(s.LEAD_ROUTER_LOOKBACK_MIN)))
    floor = str(s.LEAD_ROUTER_SINCE or "").strip()
    if floor:
        try:
            floor_dt = datetime.fromisoformat(floor)
            if floor_dt.tzinfo is None:
                floor_dt = floor_dt.replace(tzinfo=MSK)
            since = max(since, floor_dt)
        except ValueError:
            logger.warning("LEAD_ROUTER_SINCE не разобран: %s", floor)
    client = _client()
    got = await client.call("crm.lead.list", {
        "order": {"ID": "DESC"},
        "filter": {"SOURCE_ID": sources, ">DATE_CREATE": since.strftime("%Y-%m-%dT%H:%M:%S")},
        "select": ["ID", "TITLE", "SOURCE_ID", "ASSIGNED_BY_ID", "DATE_CREATE",
                   "STATUS_SEMANTIC_ID"],
    })
    rows = got if isinstance(got, list) else got.get("result", [])
    moved = []
    for row in rows:
        if str(row.get("ASSIGNED_BY_ID")) == owner:
            continue
        # Окно поиска теперь дни, а не часы: менеджер открывает ночной диалог
        # утром, и Битрикс снова делает его ответственным (30.09.2026). Поэтому
        # смотрим, когда лид создан: выходные остаются за менеджером; закрытые
        # лиды не трогаем.
        if str(row.get("STATUS_SEMANTIC_ID") or "P") != "P":
            continue
        try:
            born = datetime.fromisoformat(str(row.get("DATE_CREATE"))).astimezone(MSK)
        except ValueError:
            continue
        if b24_forward_allowed_at(born):
            continue
        try:
            await client.call("crm.lead.update", {
                "ID": row["ID"],
                "fields": {"ASSIGNED_BY_ID": owner},
                "params": {"REGISTER_SONET_EVENT": "N"},
            })
        except Exception as exc:                          # noqa: BLE001
            logger.warning("лид %s не переназначен: %s", row.get("ID"), str(exc)[:120])
            continue
        moved.append(str(row["ID"]))
        logger.info("лид %s (%s) с %s → %s — %s", row.get("ID"),
                    str(row.get("TITLE"))[:40], row.get("ASSIGNED_BY_ID"), owner,
                    describe_now())
    return {"changed": len(moved), "ids": moved, "looked": len(rows)}


class LeadRouter:
    """Фоновая служба: раз в минуту проверяет свежие лиды Авито."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        s = get_settings()
        if not s.LEAD_ROUTER_ENABLE:
            logger.info("lead router disabled (LEAD_ROUTER_ENABLE=false)")
            return
        self._task = asyncio.create_task(self._run(), name="lead_router")
        logger.info("lead router: источники %s, в будни ответственный %s",
                    s.LEAD_ROUTER_SOURCES, s.LEAD_ROUTER_WEEKDAY_USER)

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
        interval = int(get_settings().LEAD_ROUTER_INTERVAL_SEC)
        while True:
            try:
                await route_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("lead router: тик не удался")
            await asyncio.sleep(interval)


router = LeadRouter()
