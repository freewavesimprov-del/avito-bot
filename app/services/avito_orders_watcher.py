"""Background-поллер: тикает раз в N секунд, читает Avito Messenger API,
ловит системные сообщения (заказы Авито-Доставки, прочие триггеры), реагирует.

Логика:
  1. Берём последние N чатов (limit из конфига).
  2. Для каждого чата читаем недавние сообщения, фильтруем type=='system'.
  3. Если msg_id уже в `avito_system_messages` — пропускаем (идемпотентность).
  4. Иначе:
     a) Сохраняем в БД с initial classification='unknown'.
     b) Классифицируем по flow_id / тексту → 'order' / 'preorder' / 'ignore'.
     c) Side-effects по классу:
        - order:    добавить комментарий в существующий Avito-лид + (опц) ack в Avito
        - preorder: ничего (просто фиксируем)
        - ignore:   ничего
        - unknown:  оставить как есть, в DB; ждём ручной классификации

КЛЮЧЕВОЕ ПРАВИЛО: мы НЕ создаём новых сущностей в B24 CRM (сделок/лидов).
B24 Open Lines уже автоматически создаёт лид при первом обращении клиента
и назначает ответственного. Наша задача — обогащать существующий лид
комментариями. Это исключает дубли, ложных ответственных и автоматизации
(«задачи на нового ответственного» и пр.).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from app.config import get_settings
from app.services.avito_client import AvitoAPIError, AvitoClient
from app.services.bitrix import BitrixError, client_from_install
from app.services.logger import (
    avito_sys_msg_exists,
    avito_sys_msg_mark_processed,
    avito_sys_msg_record,
)

logger = logging.getLogger("avito_bot.avito_watcher")


# Сюда дописывать новые flow_id по мере обнаружения.
FLOW_CLASSIFICATION: dict[str, str] = {
    "sbc_seller_notification": "preorder",      # покупатель смотрит со скидкой
    "target_contacts_expt": "ignore",           # пытался дозвониться, не наша задача
    "flower_440007": "ignore",                  # экспериментальный UI Авито
    "seller_audience_discount": "ignore",       # уведомление о скидке для продавца
    "smart-assistant": "ignore",                # Avito Smart Assistant подсказки
    "marketplace_lead_away": "ignore",          # покупатель отвалился без заказа
    "messenger_email_warning": "ignore",        # системные предупреждения
    "messenger_email_warning_with_actions": "ignore",
}

# Текстовые маркеры для эвристики (если flow_id неизвестен)
ORDER_TEXT_PATTERNS = [
    re.compile(r"оформил(?:а)?\s+заказ", re.IGNORECASE),
    re.compile(r"купил(?:а)?\s+с\s+доставк", re.IGNORECASE),
    re.compile(r"авито\s+доставк(?:а|ой|и)\s*[—–:-]?\s*заказ", re.IGNORECASE),
    re.compile(r"заказ\s+№\s*\d+", re.IGNORECASE),
    re.compile(r"новый\s+заказ", re.IGNORECASE),
]

# Регексы для извлечения order_id из текста системного сообщения.
ORDER_ID_PATTERNS = [
    re.compile(r"заказ\s+№\s*(\d{5,})", re.IGNORECASE),
    re.compile(r"заказ\s+#\s*(\d{5,})", re.IGNORECASE),
    re.compile(r"order[_\s]+id[:\s]+(\d{5,})", re.IGNORECASE),
    re.compile(r"\b(\d{8,12})\b"),  # fallback: длинное число в тексте
]

AVITO_PRO_ORDERS_URL = "https://www.avito.ru/profile/orders"
AVITO_PRO_MESSENGER_URL = "https://www.avito.ru/profile/messenger"

ACK_MESSAGE_TEMPLATE = (
    "Здравствуйте! Спасибо за заказ — он принят. "
    "Менеджер свяжется с вами в этом чате с 9:00 до 19:00 МСК — "
    "подтвердит детали доставки и сроки."
)


def _extract_order_id(text: str) -> str | None:
    for pat in ORDER_ID_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


def _classify(flow_id: str | None, text: str) -> str:
    """Вернуть один из: order | preorder | ignore | unknown."""
    if flow_id and flow_id in FLOW_CLASSIFICATION:
        return FLOW_CLASSIFICATION[flow_id]

    if flow_id:
        fid = flow_id.lower()
        if "order" in fid and any(k in fid for k in ("create", "new", "paid", "confirm")):
            return "order"

    for pat in ORDER_TEXT_PATTERNS:
        if pat.search(text):
            return "order"

    return "unknown"


class AvitoWatcher:
    """Запускается через start()/stop() из FastAPI lifespan."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._client: AvitoClient | None = None

    async def start(self) -> None:
        settings = get_settings()
        if not settings.AVITO_WATCHER_ENABLE:
            logger.info("avito watcher disabled (AVITO_WATCHER_ENABLE=false)")
            return
        if not settings.AVITO_CLIENT_ID or not settings.AVITO_CLIENT_SECRET:
            logger.warning("avito watcher: креды Avito не заданы, skip")
            return
        self._client = AvitoClient.from_settings()
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="avito_watcher")
        logger.info(
            "avito watcher started: interval=%ds, chats=%d, ack=%s",
            settings.AVITO_WATCHER_INTERVAL_SEC,
            settings.AVITO_WATCHER_CHATS_LIMIT,
            settings.AVITO_WATCHER_SEND_ACK,
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
            if self._client is not None:
                await self._client.close()
            self._task = None
            logger.info("avito watcher stopped")

    async def _run(self) -> None:
        settings = get_settings()
        interval = settings.AVITO_WATCHER_INTERVAL_SEC
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("avito watcher: tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def _tick(self) -> None:
        assert self._client is not None
        settings = get_settings()
        chats = await self._client.list_chats(limit=settings.AVITO_WATCHER_CHATS_LIMIT)
        for chat in chats:
            cid = chat.get("id")
            if not cid:
                continue
            try:
                msgs = await self._client.get_messages(cid, limit=20)
            except AvitoAPIError as e:
                logger.warning("avito watcher: get_messages %s: %s", cid, e)
                continue
            for m in msgs:
                if m.get("type") != "system":
                    continue
                msg_id = m.get("id")
                if not msg_id:
                    continue
                if await avito_sys_msg_exists(msg_id):
                    continue
                content = m.get("content") or {}
                flow_id = content.get("flow_id")
                text = content.get("text") or ""
                created_ts = int(m.get("created") or 0)
                classification = _classify(flow_id, text)

                await avito_sys_msg_record(
                    msg_id=msg_id,
                    chat_id=cid,
                    flow_id=flow_id,
                    created_ts=created_ts,
                    text=text,
                    classification=classification,
                )
                logger.info(
                    "avito watcher: new system msg chat=%s flow_id=%s class=%s",
                    cid, flow_id, classification,
                )
                await self._react(
                    chat=chat, msg_id=msg_id, flow_id=flow_id,
                    text=text, classification=classification,
                )

    async def _react(
        self, *, chat: dict[str, Any], msg_id: str, flow_id: str | None,
        text: str, classification: str,
    ) -> None:
        """Side-effects по классу. Любая ошибка логируется и не валит поллер."""
        settings = get_settings()
        chat_id = chat["id"]

        if classification == "order":
            # 1. Комментарий в существующий лид (НЕ новая сделка/лид)
            lead_id: int | None = None
            try:
                lead_id = await self._comment_on_lead_for_chat(
                    chat=chat, text=text, flow_id=flow_id,
                )
            except Exception:
                logger.exception("avito watcher: lead comment failed for chat=%s", chat_id)

            # 2. Ack клиенту в Avito
            ack_sent = False
            if settings.AVITO_WATCHER_SEND_ACK:
                try:
                    assert self._client is not None
                    await self._client.send_message(chat_id, ACK_MESSAGE_TEMPLATE)
                    ack_sent = True
                except Exception:
                    logger.exception("avito watcher: ack send failed for chat=%s", chat_id)

            await avito_sys_msg_mark_processed(
                msg_id, classification="order",
                b24_deal_id=lead_id, ack_sent=ack_sent,
            )
            return

        if classification == "preorder":
            await avito_sys_msg_mark_processed(msg_id, classification="preorder")
            return

        if classification == "ignore":
            await avito_sys_msg_mark_processed(msg_id, classification="ignore")
            return

        # unknown — фиксируем в БД, в B24 ничего не пишем (не плодим мусор).
        logger.warning(
            "avito watcher: UNKNOWN flow_id=%r — recorded only, no B24 action. "
            "Add to FLOW_CLASSIFICATION if it's an order pattern.",
            flow_id,
        )

    def _user_id_or_zero(self) -> int:
        s = get_settings()
        return int(s.AVITO_USER_ID or 0)

    async def _find_lead_for_chat(self, chat: dict[str, Any]) -> int | None:
        """Найти B24-лид, созданный коннектором OL для этого Avito-чата.

        B24 Open Lines создаёт лид с SOURCE_ID='4|AVITO' и NAME=имя покупателя
        при первом обращении. Эту связь используем чтобы добавлять комментарии в
        правильный лид, а не плодить новые сущности.

        Если лид не найден — возвращает None (watcher молча пропустит CRM-обогащение).
        """
        buyer_name = ""
        uid_self = self._user_id_or_zero()
        for u in chat.get("users") or []:
            if u.get("id") and int(u["id"]) != uid_self:
                buyer_name = (u.get("name") or "").strip()
                break
        if not buyer_name:
            return None

        try:
            client = await client_from_install()
        except BitrixError as e:
            logger.error("avito watcher: cannot build B24 client: %s", e)
            return None
        try:
            leads = await client.call("crm.lead.list", {
                "filter": {"SOURCE_ID": "4|AVITO", "NAME": buyer_name},
                "select": ["ID", "DATE_CREATE"],
                "order": {"ID": "DESC"},
            })
        except BitrixError as e:
            logger.warning("avito watcher: crm.lead.list failed: %s", e)
            return None

        items = leads if isinstance(leads, list) else (leads or {}).get("result", leads) or []
        if not items:
            logger.warning(
                "avito watcher: no Lead found for chat=%s buyer=%r — skipping CRM enrich",
                chat.get("id"), buyer_name,
            )
            return None
        return int(items[0].get("ID") or 0) or None

    async def _comment_on_lead_for_chat(
        self, *, chat: dict[str, Any], text: str, flow_id: str | None,
    ) -> int | None:
        """Найти лид и добавить комментарий с инфо для менеджера. Возвращает lead_id."""
        ctx = chat.get("context") or {}
        item = (ctx.get("value") or {}) if ctx.get("type") == "item" else {}
        item_title = item.get("title") or "Авито-заказ"
        item_url = item.get("url") or ""
        price_str = item.get("price_string") or ""
        order_id = _extract_order_id(text)

        lead_id = await self._find_lead_for_chat(chat)
        if lead_id is None:
            return None

        comment_lines = [
            "[B]Avito Доставка — новый заказ[/B]",
            "",
            "[B]Действие менеджера:[/B]",
            f"  1) Открыть Avito Pro: {AVITO_PRO_ORDERS_URL}",
            "  2) Подтвердить заказ и распечатать наклейку",
            "  3) Передать на отгрузку",
            "",
            "Бот уже отправил клиенту автоматическое подтверждение в чат Avito.",
            "",
            "[B]Детали:[/B]",
            f"  Товар: {item_title}",
            f"  Цена: {price_str or '—'}",
            f"  Номер заказа: {order_id or 'не извлечён — см. исходный текст ниже'}",
            f"  Объявление: {item_url or '—'}",
            f"  Переписка: {AVITO_PRO_MESSENGER_URL}",
            "",
            f"Источник: Avito Messenger API (flow_id={flow_id or '—'})",
            "",
            "[I]Системное сообщение от Avito:[/I]",
            text[:1500],
        ]
        comment = "\n".join(comment_lines)

        try:
            client = await client_from_install()
            await client.crm_timeline_comment_add(
                entity_type="lead", entity_id=lead_id, comment=comment,
            )
        except BitrixError:
            logger.exception("avito watcher: crm.timeline.comment.add failed for lead=%s", lead_id)
            return None
        logger.info(
            "avito watcher: commented on lead=%s for chat=%s order_id=%s",
            lead_id, chat["id"], order_id,
        )
        return lead_id


# Глобальный singleton (импортируется из main.py lifespan)
watcher = AvitoWatcher()
