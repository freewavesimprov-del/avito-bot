"""Сторож зависших диалогов Открытой линии → алерт в Telegram.

Зачем: 3 августа диалог с клиентом провисел в очереди 3,5 часа — бот отработал и
передал операторам, но никто не отреагировал, а B24 не был настроен об этом
сообщить. KPI-оповещения B24 умеют писать только внутрь портала, поэтому шлём
в Telegram сами.

Как работает (тик раз в OL_WATCHDOG_INTERVAL_SEC):
  1. crm.activity.list → открытые (COMPLETED=N) активности провайдера
     IMOPENLINES_SESSION нужной линии. Это и есть незакрытые сессии ОЛ.
  2. Для каждой сессии старше OL_WATCHDOG_UNANSWERED_MIN минут поднимаем чат
     (imopenlines.session.history.get → chatId) и смотрим сообщения.
  3. Считаем диалог «зависшим», если последнее содержательное сообщение —
     от клиента (или от нашего бота), и после него никто из людей не ответил.
  4. Шлём алерт в Telegram, отмечаем сессию в таблице ol_alerts (один алерт на
     сессию, повторно не дёргаем).

Все ошибки логируются и не валят цикл: сторож не должен ронять сервис.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import get_settings
from app.services.bitrix import BitrixError, client_from_install
from app.services.logger import ol_alert_exists, ol_alert_record
from app.services.notifier import notify_stale_dialog

logger = logging.getLogger("avito_bot.ol_watchdog")

# Авторы, которые НЕ считаются ответом оператора
SYSTEM_AUTHOR_ID = 0  # системные сообщения B24


def _parse_b24_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class OLWatchdog:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        s = get_settings()
        if not s.OL_WATCHDOG_ENABLE:
            logger.info("ol watchdog disabled (OL_WATCHDOG_ENABLE=false)")
            return
        if not s.TG_BOT_TOKEN or not s.TG_CHAT_ID:
            logger.warning("ol watchdog: нет TG-кредов (TG_BOT_TOKEN/TG_CHAT_ID), не запускаю")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="ol_watchdog")
        logger.info(
            "ol watchdog started: line=%s, interval=%ds, threshold=%d min",
            s.OL_WATCHDOG_LINE_ID, s.OL_WATCHDOG_INTERVAL_SEC, s.OL_WATCHDOG_UNANSWERED_MIN,
        )

    async def stop(self) -> None:
        if not self._task:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except (TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        finally:
            self._task = None
            logger.info("ol watchdog stopped")

    async def _run(self) -> None:
        interval = get_settings().OL_WATCHDOG_INTERVAL_SEC
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("ol watchdog: tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def _tick(self) -> None:
        s = get_settings()
        threshold = timedelta(minutes=s.OL_WATCHDOG_UNANSWERED_MIN)
        now = datetime.now(timezone.utc)

        try:
            client = await client_from_install()
        except BitrixError as e:
            logger.error("ol watchdog: cannot build B24 client: %s", e)
            return

        try:
            res = await client.call("crm.activity.list", {
                "filter": {
                    "PROVIDER_ID": "IMOPENLINES_SESSION",
                    "PROVIDER_TYPE_ID": str(s.OL_WATCHDOG_LINE_ID),
                    "COMPLETED": "N",
                },
                "select": ["ID", "SUBJECT", "CREATED", "RESPONSIBLE_ID", "ORIGIN_ID"],
                "order": {"ID": "DESC"},
            })
        except BitrixError as e:
            logger.warning("ol watchdog: crm.activity.list failed: %s", e)
            return

        items = res if isinstance(res, list) else (res or {}).get("result", res) or []
        for act in items:
            created = _parse_b24_dt(act.get("CREATED"))
            if created is None:
                continue
            if now - created < threshold:
                continue  # ещё рано тревожиться

            origin = str(act.get("ORIGIN_ID") or "")  # 'IMOL_44794'
            session_id = 0
            if origin.startswith("IMOL_"):
                try:
                    session_id = int(origin.split("_", 1)[1])
                except ValueError:
                    session_id = 0
            if not session_id:
                continue
            if await ol_alert_exists(session_id):
                continue

            info = await self._analyze_session(client, session_id)
            if info is None or not info["stale"]:
                continue

            await self._alert(act=act, session_id=session_id, info=info)
            await ol_alert_record(session_id, info.get("chat_id"))

    async def _analyze_session(
        self, client: Any, session_id: int
    ) -> dict[str, Any] | None:
        """Понять, отвечал ли живой оператор. None если не удалось разобрать."""
        settings = get_settings()
        bot_id = str(settings.B24_BOT_ID or "")
        try:
            hist = await client.call(
                "imopenlines.session.history.get", {"SESSION_ID": session_id}
            )
        except BitrixError as e:
            logger.warning("ol watchdog: session.history.get %s: %s", session_id, e)
            return None
        data = hist if isinstance(hist, dict) else (hist or {}).get("result", hist) or {}
        chat_id = data.get("chatId")
        if not chat_id:
            return None

        try:
            msgs_res = await client.call(
                "im.dialog.messages.get", {"DIALOG_ID": f"chat{chat_id}", "LIMIT": 50}
            )
        except BitrixError as e:
            logger.warning("ol watchdog: messages.get chat%s: %s", chat_id, e)
            return None
        md = msgs_res if isinstance(msgs_res, dict) else (msgs_res or {}).get("result", {}) or {}
        messages = md.get("messages") or []
        if not messages:
            return None
        messages.sort(key=lambda m: m.get("id") or 0)

        # Отбрасываем системные сообщения — остаются реплики людей и бота
        real = [
            m for m in messages
            if str(m.get("author_id") or "") not in ("", str(SYSTEM_AUTHOR_ID))
        ]
        if not real:
            return None

        # Диалог в ОЛ всегда открывает клиент → автор первой реплики и есть клиент
        client_id = str(real[0].get("author_id") or "")
        last = real[-1]
        last_author = str(last.get("author_id") or "")

        # Зависший = последним писал клиент (никто не ответил) либо наш бот
        # (бот отработал и передал в очередь, но живой оператор не подключился).
        stale = last_author in (client_id, bot_id)

        return {
            "chat_id": chat_id,
            "stale": stale,
            "last_text": (last.get("text") or "")[:300],
            "last_author": last_author,
            "client_id": client_id,
        }

    async def _alert(self, *, act: dict, session_id: int, info: dict) -> None:
        s = get_settings()
        portal = ""
        try:
            from app.services.b24_install import load_install
            portal = load_install().domain
        except Exception:
            portal = ""

        ok = await notify_stale_dialog(
            session_id=session_id,
            subject=act.get("SUBJECT") or "Диалог открытой линии",
            created=act.get("CREATED") or "—",
            last_text=info.get("last_text") or "—",
            minutes=s.OL_WATCHDOG_UNANSWERED_MIN,
            portal=portal,
        )
        logger.warning(
            "ol watchdog: STALE session=%s chat=%s alert_sent=%s",
            session_id, info.get("chat_id"), ok,
        )


watchdog = OLWatchdog()
