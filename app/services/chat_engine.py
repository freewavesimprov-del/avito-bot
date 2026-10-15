"""Единая точка обработки входящего сообщения клиента.

Пайплайн (Stage 6.5):
  1. Лог входящего (direction='in').
  2. Если operator_led → silent, выходим.
  3. **Debounce** (DEBOUNCE_SEC, по умолчанию 30с) — даём оператору в Авито шанс
     ответить первым. Если за это время в этом диалоге появилось любое сообщение
     не от бота (через коннектор от Татьяны, или ONIMBOTMESSAGE от другого юзера) —
     ставим operator_led=true и выходим silent.
  4. Hard-triggers — без LLM сразу эскалация.
  5. LLM.generate() с system prompt + историей.
  6. Парсинг маркера ESCALATE → cleaned_text + reason.
  7. Post-filter cleaned_text → если запрещённое — block.
  8. Решение:
       - block_reason  → action='blocked' + фикс. фраза эскалации клиенту.
       - escalate_reason → action='escalated', клиенту cleaned_text.
       - иначе → action='sent', клиенту cleaned_text.

Используется и продовым B24-вебхуком, и dev-эндпоинтом /dev/chat — поведение
идентично. Вебхук обрабатывает событие в фоне (asyncio.create_task), потому что
debounce + LLM могут занять >10с (таймаут B24 для вебхуков).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Literal

from app.config import get_settings
from app.services.filters import (
    ESCALATION_CLIENT_MESSAGE,
    parse_escalate_marker,
    post_filter,
)
from app.services.llm import ChatMessage, LLMError, generate
from app.services.logger import (
    get_recent_history,
    is_operator_led,
    log_event,
    mark_operator_led,
)
from app.services.prompt_builder import build_system_prompt
from app.services.schedule import (
    b24_forward_allowed_now,
    bot_active_now,
    describe_now,
    manager_reply_phrase,
)
from app.services.triggers import check_hard_triggers

logger = logging.getLogger("avito_bot.chat_engine")

Action = Literal["sent", "silent", "escalated", "blocked"]

# --- Repeat-loop detection -------------------------------------------------

# Сколько раз подряд клиент должен повторить похожий вопрос, чтобы бот сдался и эскалировал.
REPEAT_THRESHOLD = 3
# Сколько последних входящих сообщений учитываем при поиске повторов.
REPEAT_WINDOW = 10
# Если после нормализации первые N символов совпадают — считаем сообщения "тем же вопросом".
REPEAT_PREFIX_LEN = 50

# Сколько последних сообщений диалога подаём в LLM для контекста (in/out, в порядке времени).
LLM_HISTORY_LIMIT = 15


def _normalize_for_repeat(s: str) -> str:
    """Нижний регистр + схлопывание не-буквенных символов в один пробел + первые N символов."""
    return re.sub(r"\W+", " ", (s or "").lower()).strip()[:REPEAT_PREFIX_LEN]


_DENIAL_RE = re.compile(
    r"(нет|отсутству\w+)\s+(в\s+)?(нашем\s+)?(каталог|ассортимент|наличии в каталоге)"
    r"|(такого|такой|таких)\s+(\w+\s+){0,2}(в\s+(нашем\s+)?каталоге\s+)?(нет|не\s+найден)"
    r"|в\s+каталоге\s+(такого\s+|такой\s+)?(\w+\s+)?нет",
    re.IGNORECASE)


def _count_repeats(history: list[dict], current_text: str) -> int:
    """Сколько раз похожий вопрос встречается в истории (включая текущее сообщение,
    которое уже было залогировано через log_event 'in' до вызова этой функции)."""
    cur = _normalize_for_repeat(current_text)
    if not cur:
        return 0
    n = 0
    for item in history[-REPEAT_WINDOW:]:
        if item.get("direction") != "in":
            continue
        if _normalize_for_repeat(item.get("message", "")) == cur:
            n += 1
    return n


@dataclass
class BotReply:
    """Результат обработки одного входящего сообщения.

    action='sent'      — клиенту ушёл text, диалог продолжается.
    action='escalated' — клиенту ушёл text (вежливая фраза от LLM), бот выходит из чата.
    action='blocked'   — LLM хотел отправить blocked_text, post-filter не пустил;
                         клиенту ушёл фиксированный ESCALATION_CLIENT_MESSAGE,
                         бот выходит из чата.
    action='silent'    — бот не отвечает (operator_led / пустой ввод).
    """

    action: Action
    text: str | None = None
    reason: str | None = None
    blocked_text: str | None = None  # что LLM хотела сказать (только для action='blocked')
    stage: str = "stage4"


async def _escalate_or_hold(
    *,
    dialog_id: str,
    client_text: str,
    reason: str,
    source: str,
    meta_extra: dict | None = None,
    action_if_forwarded: Action = "escalated",
) -> BotReply:
    """Передать диалог менеджерам — или придержать до рабочего окна.

    Внутри окна передачи ведём себя как раньше: пишем клиенту, помечаем диалог
    operator_led и выходим из чата (это делает handler по action).

    Вне окна менеджеров не дёргаем: клиенту всё равно отвечаем, но бот остаётся
    в чате и operator_led не ставим — иначе диалог замолчал бы совсем, а
    менеджер о нём так и не узнал. Утром переписку видно в Avito Pro.
    """
    meta = {"source": source, "reason": reason, **(meta_extra or {})}

    if b24_forward_allowed_now():
        await log_event(dialog_id, "escalate", client_text, meta=meta)
        await mark_operator_led(dialog_id, True)
        return BotReply(action=action_if_forwarded, text=client_text, reason=reason)

    # Дежурную фразу «сейчас передам менеджеру» заменяем на честную: передавать
    # мы не будем. Содержательный ответ самой модели оставляем как есть —
    # в нём уже сказано, что менеджер ответит в рабочие часы.
    if client_text.strip() == ESCALATION_CLIENT_MESSAGE.strip():
        held_text = f"Спасибо за вопрос, {manager_reply_phrase()}."
    else:
        held_text = client_text

    await log_event(
        dialog_id,
        "escalate",
        held_text,
        meta={**meta, "forward_suppressed": True, "schedule": describe_now()},
    )
    logger.info(
        "dialog %s: эскалация придержана вне окна передачи (%s) — %s",
        dialog_id,
        reason,
        describe_now(),
    )
    return BotReply(action="sent", text=held_text, reason=f"held:{reason}")


async def process_message(
    dialog_id: str,
    user_id: str | None,
    text: str,
    ad_note: str = "",
) -> BotReply:
    """Главная точка входа."""
    text = (text or "").strip()
    if not text:
        return BotReply(action="silent", reason="empty_input")

    settings = get_settings()

    # 1. Лог входящего
    await log_event(dialog_id, "in", text, user_id=user_id, meta={"source": "chat_engine"})

    # 2. Оператор уже ведёт диалог — молчим
    if await is_operator_led(dialog_id):
        return BotReply(action="silent", reason="operator_led")

    # 2.1. Рабочее время — в чате работают живые менеджеры через Avito Pro,
    # бот не вмешивается. Входящее уже записано выше, история не теряется.
    if not bot_active_now():
        logger.info("dialog %s: бот вне своего окна — %s", dialog_id, describe_now())
        return BotReply(action="silent", reason="outside_bot_hours")

    # 3. Debounce — даём оператору в Авито шанс ответить первым.
    #    Если за это время выставился operator_led (по сообщению через коннектор
    #    от Татьяны, либо явное ONIMBOTDELETE) — выходим silent.
    debounce = max(0, settings.DEBOUNCE_SEC)
    if debounce > 0:
        await asyncio.sleep(debounce)
        if await is_operator_led(dialog_id):
            logger.info(
                "dialog %s: operator intervened during debounce (%ds), bot silent",
                dialog_id,
                debounce,
            )
            return BotReply(action="silent", reason="operator_intervened_during_debounce")

    # 4. Получаем историю — для детекта повторов используем хвост, для LLM — больше.
    history = await get_recent_history(dialog_id, limit=LLM_HISTORY_LIMIT)

    # 5. Детект повторов: если клиент задаёт ОДИН И ТОТ ЖЕ вопрос >= REPEAT_THRESHOLD раз —
    # значит наши ответы ему не помогают, передаём менеджеру.
    repeats = _count_repeats(history, text)
    if repeats >= REPEAT_THRESHOLD:
        return await _escalate_or_hold(
            dialog_id=dialog_id,
            client_text=ESCALATION_CLIENT_MESSAGE,
            reason=f"repeat_loop:{repeats}x",
            source="repeat_loop",
            meta_extra={"client_text": text},
        )

    # 6. Hard-triggers — эскалация без LLM
    hit = check_hard_triggers(text)
    if hit is not None:
        return await _escalate_or_hold(
            dialog_id=dialog_id,
            client_text=ESCALATION_CLIENT_MESSAGE,
            reason=f"hard_trigger:{hit.category}:{hit.reason}",
            source="hard_trigger",
            meta_extra={
                "category": hit.category,
                "priority": hit.priority,
                "client_text": text,
            },
        )

    # 7. LLM
    raw_reply = await _generate(text, history, ad_note)

    # 5. Парсим ESCALATE из ответа LLM
    cleaned, escalate_reason = parse_escalate_marker(raw_reply)
    # Страховка: «такого нет в каталоге» клиенту не уходит никогда. Каталог
    # бота — не весь ассортимент, и так бот «потерял» Басю (01.10.2026).
    if cleaned and _DENIAL_RE.search(cleaned):
        logger.info("dialog %s: отказ «нет в каталоге» заменён передачей менеджеру", dialog_id)
        cleaned = f"Уточню по этой модели у менеджера — {manager_reply_phrase()}."
        escalate_reason = escalate_reason or "товара нет в таблице"

    # 6. Post-filter
    if cleaned:
        ok, block_reason = post_filter(cleaned)
    else:
        # LLM ничего внятного не вернула — эскалируем
        ok, block_reason = True, None
        if not escalate_reason:
            cleaned = ESCALATION_CLIENT_MESSAGE
            escalate_reason = "empty_llm_reply"

    # 7. Решение
    if not ok:
        # Блокируем: то, что хотел сказать LLM — в events как 'blocked',
        # клиенту шлём фиксированную фразу, помечаем 'escalate'.
        await log_event(
            dialog_id,
            "blocked",
            cleaned,
            meta={"source": "post_filter", "block_reason": block_reason},
        )
        reply = await _escalate_or_hold(
            dialog_id=dialog_id,
            client_text=ESCALATION_CLIENT_MESSAGE,
            reason=block_reason or "post_filter",
            source="post_filter",
            meta_extra={"blocked_text": cleaned},
            action_if_forwarded="blocked",
        )
        reply.blocked_text = cleaned
        return reply

    if escalate_reason:
        # LLM сама пометила маркером — клиенту шлём её вежливый текст,
        # затем бот выходит (если окно передачи открыто). cleaned — обычное 'out'.
        await log_event(dialog_id, "out", cleaned, meta={"source": "llm"})
        reply = await _escalate_or_hold(
            dialog_id=dialog_id,
            client_text=cleaned,
            reason=escalate_reason,
            source="llm_marker",
        )
        return reply

    # Обычный ответ
    await log_event(
        dialog_id,
        "out",
        cleaned,
        meta={"source": "llm", "mock_llm": get_settings().MOCK_LLM},
    )
    return BotReply(action="sent", text=cleaned)


# --- Внутренности генерации ------------------------------------------------


async def _generate(user_text: str, history: list[dict], ad_note: str = "") -> str:
    """Mock или LLM. Внутренний — внешний интерфейс — process_message()."""
    settings = get_settings()
    if settings.MOCK_LLM:
        return _mock_reply(user_text)

    system_prompt = build_system_prompt().system
    if ad_note:
        # Объявление клиента — последним блоком, чтобы перевешивало каталог.
        system_prompt = f"{system_prompt}\n\n---\n\n{ad_note}"
    messages = _history_to_chat_messages(history, current_user_text=user_text)

    try:
        return await generate(system_prompt=system_prompt, user_messages=messages)
    except LLMError as e:
        logger.warning("LLM failed, fallback to escalation: %s", e)
        return "Уточню у менеджера, ответит в этом же чате. ESCALATE: llm_unavailable"


def _history_to_chat_messages(
    history: list[dict], current_user_text: str
) -> list[ChatMessage]:
    mapped: list[ChatMessage] = []
    for item in history:
        role = "user" if item.get("direction") == "in" else "assistant"
        text = (item.get("message") or "").strip()
        if text:
            mapped.append(ChatMessage(role=role, text=text))
    if not mapped or mapped[-1].role != "user" or mapped[-1].text != current_user_text.strip():
        mapped.append(ChatMessage(role="user", text=current_user_text.strip()))
    return mapped


def _mock_reply(user_text: str) -> str:
    """Детерминистичный ответ для локальной отладки без YC-ключа."""
    low = user_text.lower()
    if any(w in low for w in ("привет", "здравствуйте", "добрый")):
        return "Здравствуйте! Чем могу помочь? Вас интересует конкретный товар?"
    if "наличи" in low or ("есть" in low and any(w in low for w in ("комод", "шкаф", "диван", "кровать"))):
        return "По данным на 22.04 позиция в наличии. Уточнить цену и цвета?"
    if any(w in low for w in ("скидк", "дешевле", "уступите")):
        return "Скидки согласует менеджер, передам запрос. ESCALATE: запрос скидки"
    if any(w in low for w in ("доставк", "привезёт", "привезете")):
        return "Доставка по МО 2 000 ₽, срок 1-3 дня. Укажите адрес — посчитаю точнее."
    if any(w in low for w in ("самовывоз", "забрать", "склад")):
        return "Самовывоз бесплатный: склад в МО, офлайн-точки в Шатуре, Егорьевске, Орехово-Зуево."
    return f"[MOCK] Принял: «{user_text[:160]}»."
