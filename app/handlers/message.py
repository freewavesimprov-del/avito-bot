"""Обработчик события `ONIMBOTMESSAGEADD` от Bitrix24.

Вызывается из webhook-роутера (`app/main.py`) **в фоновом таске** (asyncio.create_task),
потому что debounce 30с + LLM 2-3с превышают B24-таймаут вебхука (~10с).

Что делает:
  1. Решает, наш ли это event (правильный BOT_ID, не от оператора, не от себя).
  2. Если событие от оператора (Татьяна через коннектор Авито) — помечает диалог
     `operator_led=true`, чтобы бот в нём больше не отвечал.
  3. Дёргает `chat_engine.process_message()` — debounce, триггеры, LLM, фильтры.
  4. Доставляет результат через `bitrix.imbot.message.add` / `imbot.chat.leave`.

CRM-комментарии при эскалации (`crm.timeline.comment.add`) пока **не делаем**:
у Татьяны нет сейчас рабочих лидов в B24 (она ведёт всё в 1С вручную). Включим
позже, когда появится связка B24-лиды ↔ Авито-чат — потенциально через scenario
с Татьяной, заполняющей карточки клиентов вручную в B24 (история диалога уже там).

Каждый event обслуживается с собственным B24-клиентом, построенным из
event.auth (domain + access_token) — токен свежий, рефреш не нужен.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.models import B24EventBase, MessageAddPayload
from app.services.bitrix import BitrixClient, BitrixError, client_from_event_auth
from app.services.chat_engine import BotReply, process_message
from app.services.notifier import notify_dialog
from app.services.logger import (
    get_recent_history,
    is_operator_led,
    log_event,
    mark_operator_led,
)

logger = logging.getLogger("avito_bot.handler")


async def _detect_existing_operator_chat(
    *,
    client: BitrixClient,
    dialog_id: str,
    bot_id: int | None,
    client_user_id: str,
) -> bool:
    """Был ли в этом B24-чате обмен с участием оператора до того, как мы туда попали?

    Логика:
      Запрашиваем историю чата через im.dialog.messages.get.
      Если хоть одно сообщение **не от клиента** и **не от нашего бота** —
      это оператор уже работал. Сообщения системы / коннектора (technical user_id)
      тоже считаем «уже не пустой чат» — лучше перестраховаться и не лезть.

    Возвращает True если бот должен остаться в стороне.
    """
    try:
        messages = await client.im_dialog_messages_get(dialog_id=dialog_id, limit=50)
    except BitrixError as e:
        # Если не удалось проверить — НЕ блокируем ответ бота.
        # Раньше был "fail-closed" (return True = не отвечать), но это привело к
        # тому, что любая ошибка REST (DIALOG_ID_EMPTY у Avito-чатов с особым
        # форматом, expired_token, scope, network) убивала бот в тишину.
        # Лучше один раз ошибиться (бот ответит в существующий чат с оператором,
        # клиент увидит дубль) чем хроническая тишина — её не видно.
        logger.warning(
            "handler: im.dialog.messages.get failed for %s: %s — proceeding as new chat",
            dialog_id,
            e,
        )
        return False

    if not messages:
        return False  # пусто — это новый чат, всё ок

    # Считаем сообщения от не-клиента и не-нашего-бота. Текущее сообщение клиента
    # (тот же author_id что в payload) тоже учтём — но оно из категории "клиент".
    bot_id_str = str(bot_id) if bot_id is not None else None
    other_authors = 0
    for m in messages:
        author = str(m.get("author_id") or "")
        if not author:
            continue
        if author == client_user_id:
            continue  # это сам клиент — норма
        if bot_id_str and author == bot_id_str:
            continue  # это наш бот — игнорируем (хотя его не должно тут быть до первого ответа)
        other_authors += 1

    if other_authors > 0:
        logger.info(
            "handler: %s has %d prior message(s) from non-client/non-bot — operator was active",
            dialog_id,
            other_authors,
        )
        return True
    return False


def _is_seller_message(payload: MessageAddPayload, expected_bot_id: int | None) -> tuple[bool, str]:
    """Возвращает (skip, reason). skip=True → не обрабатываем."""
    user = payload.USER
    params = payload.PARAMS

    # 1. IS_EXTRANET == false (или 'N') — это оператор/сотрудник, не клиент.
    is_extranet = user.IS_EXTRANET
    is_extranet_truthy = is_extranet is True or is_extranet == "Y"
    if not is_extranet_truthy:
        return True, "operator_message"

    # 2. Сообщение от самого бота (FROM_USER_ID == BOT_ID) — иногда B24 шлёт эхо
    from_id = params.FROM_USER_ID
    bot_id_in_event = params.BOT_ID
    if from_id is not None and bot_id_in_event is not None and str(from_id) == str(bot_id_in_event):
        return True, "self_echo"

    # 3. Сообщение адресовано не нашему боту (если в .env прописан конкретный BOT_ID)
    if (
        expected_bot_id is not None
        and bot_id_in_event is not None
        and str(bot_id_in_event) != str(expected_bot_id)
    ):
        return True, "wrong_bot_id"

    # 4. Avito-системные сообщения (заказы Авито-Доставки, уведомления о скидках,
    # автоматические рассылки от Avito) приходят как обычный текст с маркером
    # "[Системное сообщение]" в начале. LLM не должен на них отвечать — это не вопрос
    # клиента. Обрабатываем их отдельно в Avito-поллере (см. avito_orders_watcher).
    text = (params.MESSAGE or "").lstrip()
    if text.startswith("[Системное сообщение]") or "[Системное сообщение]" in text[:200]:
        return True, "avito_system_message"

    return False, ""


async def handle_message_add(event: B24EventBase) -> dict[str, Any]:
    """Главный обработчик ONIMBOTMESSAGEADD.

    Возвращает dict с результатом для логов / ответа вебхуку:
      {"status": "processed"|"skipped", "action": "sent"|..., "reason": "..."}
    """
    settings = get_settings()

    # Парсим вложенный payload (data → MessageAddPayload)
    try:
        payload = MessageAddPayload.model_validate(event.data or {})
    except Exception as e:  # pragma: no cover - модели мягкие, должны парсить всё
        logger.warning("handler: cannot parse MessageAddPayload: %s", e)
        return {"status": "skipped", "reason": "invalid_payload"}

    skip, skip_reason = _is_seller_message(payload, settings.B24_BOT_ID)
    if skip:
        # Если сообщение пришло от оператора (Татьяны через коннектор Авито,
        # или другого менеджера в B24) — помечаем диалог как operator_led,
        # чтобы бот замолчал в этом диалоге до новой сессии.
        # Эхо нашего бота и сообщения чужому боту — не считаем.
        if skip_reason == "operator_message":
            dlg = (payload.PARAMS.DIALOG_ID or "").strip()
            if dlg:
                await mark_operator_led(dlg, True)
                await log_event(
                    dlg,
                    "system",
                    "operator_message_detected",
                    meta={"source": "secondary_detect", "from_user_id": str(payload.USER.ID or "")},
                )
                logger.info("handler: operator_led=true for %s (operator message)", dlg)
        logger.info("handler: skip event (%s)", skip_reason)
        return {"status": "skipped", "reason": skip_reason}

    dialog_id = (payload.PARAMS.DIALOG_ID or "").strip()
    text = (payload.PARAMS.MESSAGE or "").strip()
    user_id = str(payload.USER.ID or payload.PARAMS.FROM_USER_ID or "")

    if not dialog_id or not text:
        return {"status": "skipped", "reason": "empty_dialog_or_text"}

    # --- Защита от вторжения в существующий активный чат -------------------
    # Если этот dialog_id у нас в БД ещё не имеет ни одного 'in'-сообщения
    # (мы видим его впервые), но в B24 в чате уже есть переписка с участием
    # оператора — значит Татьяна уже работала там до подключения бота.
    # Не лезем: помечаем operator_led=true и выходим silent.
    try:
        client = client_from_event_auth(event.auth.domain, event.auth.access_token)
    except BitrixError as e:
        logger.error("handler: cannot build B24 client: %s", e)
        return {"status": "error", "reason": str(e)}

    if not await is_operator_led(dialog_id):
        existing_history = await get_recent_history(dialog_id, limit=1)
        is_first_for_us = not any(h["direction"] == "in" for h in existing_history)
        if is_first_for_us:
            had_operator = await _detect_existing_operator_chat(
                client=client,
                dialog_id=dialog_id,
                bot_id=settings.B24_BOT_ID,
                client_user_id=user_id,
            )
            if had_operator:
                await mark_operator_led(dialog_id, True)
                await log_event(
                    dialog_id,
                    "system",
                    "preexisting_operator_chat",
                    meta={"source": "first_seen", "client_text": text},
                )
                logger.info(
                    "handler: %s — pre-existing operator chat, bot stays out",
                    dialog_id,
                )
                return {"status": "skipped", "reason": "preexisting_operator_chat"}

    # 1. Pipeline (внутри process_message — лог 'in', triggers, LLM, фильтры, лог 'out'/'escalate'/'blocked')
    # Объявление, из которого клиент открыл чат (01.10.2026): без него бот
    # переспрашивал «какой товар вас интересует?» на «есть в других цветах?».
    try:
        from app.services import ad_context

        ad_note = await ad_context.note_for(client, dialog_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("handler: объявление клиента не определилось: %s", e)
        ad_note = ""
    reply: BotReply = await process_message(dialog_id=dialog_id, user_id=user_id, text=text,
                                            ad_note=ad_note)

    if reply.action == "silent":
        # Уведомляем даже про молчание: важно видеть, что обращение было,
        # но бот в него не вмешался (оператор уже ведёт диалог и т.п.).
        notify_dialog(
            dialog_id=dialog_id, client_text=text, bot_text=None,
            action="silent", reason=reply.reason,
        )
        return {"status": "processed", "action": "silent", "reason": reply.reason}

    # 2. Доставка ответа через Bitrix REST
    bot_id = settings.B24_BOT_ID
    if bot_id is None:
        # На всякий случай — без BOT_ID отправить мы не можем; помечаем диалог проблемным
        logger.error("B24_BOT_ID не задан в .env — невозможно отправить ответ. Запусти register_bot.py")
        await log_event(
            dialog_id, "system", "delivery_failed: no B24_BOT_ID", meta={"reply_text": reply.text}
        )
        return {"status": "error", "reason": "no_bot_id"}

    try:
        if reply.text:
            await client.imbot_message_add(bot_id=bot_id, dialog_id=dialog_id, message=reply.text)

        if reply.action in ("escalated", "blocked"):
            # Бот выходит из чата — оператор очереди примет диалог
            await client.imbot_chat_leave(bot_id=bot_id, dialog_id=dialog_id)
            await log_event(
                dialog_id, "system", "bot_left_chat", meta={"reason": reply.reason or "—"}
            )
    except BitrixError as e:
        logger.error("handler: B24 delivery failed: %s", e)
        await log_event(
            dialog_id, "system", f"delivery_failed: {e}", meta={"reply_text": reply.text}
        )
        notify_dialog(
            dialog_id=dialog_id, client_text=text, bot_text=reply.text,
            action="blocked", reason=f"не доставлено в B24: {e}",
        )
        return {"status": "error", "reason": str(e)}

    notify_dialog(
        dialog_id=dialog_id, client_text=text, bot_text=reply.text,
        action=reply.action, reason=reply.reason,
    )

    return {
        "status": "processed",
        "action": reply.action,
        "reason": reply.reason,
        "delivered": bool(reply.text),
    }
