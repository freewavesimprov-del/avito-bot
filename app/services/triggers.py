"""Hard-triggers: сообщения клиента, на которые отвечать LLM не нужно — сразу эскалация.

По SPEC §2. Применяется ДО вызова LLM. Если сработал — бот не зовёт YandexGPT,
шлёт фиксированную фразу про менеджера и выходит из чата.

Возвращает причину (для логов / комментария в лиде) или None.
Дополнительно — категорию приоритета: 'urgent' для жалоб/брака, чтобы
в Stage 5 в `crm.timeline.comment.add` пометить как «⚠ требует приоритета».
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TriggerHit:
    reason: str
    category: str  # 'complaint' | 'return' | 'manager' | 'order_number' | 'legal'
    priority: str  # 'urgent' | 'normal'


# Каждое правило: (pattern, reason, category, priority)
_RULES: list[tuple[re.Pattern[str], str, str, str]] = [
    # Жалобы / брак — приоритет.
    # Без правой границы у корней — нужны падежи (браком, развалился, сломалась).
    (
        re.compile(
            r"(?iux)"
            r"(жалоб|рекламац|\bбрак|сломал|сломан|не\s+работае|"
            r"дефект|плохое\s+качество|развалил|трещин)",
        ),
        "жалоба/брак",
        "complaint",
        "urgent",
    ),
    # Возвраты
    (
        re.compile(
            r"(?ix)"
            r"\b(верн[уи]те\s+деньги|возврат|вернуть\s+товар|отказ\s+от\s+покупк|"
            r"хочу\s+вернуть)",
        ),
        "запрос возврата",
        "return",
        "urgent",
    ),
    # Прямой запрос менеджера / контактов / звонка
    (
        re.compile(
            r"(?ix)"
            r"(позов[иу]те?\s+(менеджер|оператор|человек)|"
            r"дайте\s+(телефон|номер|контакт)|"
            r"давайте\s+созвон|перезвон)",
        ),
        "запрос менеджера/контакта",
        "manager",
        "normal",
    ),
    # Номер заказа: 6 и более цифр подряд (заказ обычно длинный номер)
    (
        re.compile(r"(?<!\d)\d{6,}(?!\d)"),
        "упомянут номер заказа",
        "order_number",
        "normal",
    ),
    # Юридические темы
    (
        re.compile(r"(?ix)\b(судебн|законн|претензи|роспотребнадзор)"),
        "юридический вопрос",
        "legal",
        "urgent",
    ),
    # Документы / счета — Татьяна выставляет в 1С вручную, бот не должен влезать.
    # Включает: «выставите счёт», «реквизиты», «ИНН», «КПП», «р/с», «расчётный счёт»,
    # «договор поставки», «оплата по счёту», «безнал», «безналичный расчёт», «платёжное поручение»
    (
        re.compile(
            r"(?ixu)"
            r"(выставит[ье]\s+счёт|счёт\s+на\s+оплату|"
            r"\bреквизит|\bИНН\b|\bКПП\b|"
            r"\bр/?с\b|расчётн\w*\s+счёт|"
            r"договор(\s+поставки|\s+купли)?|"
            r"безнал\w*|платёжн\w+\s+поручен|"
            r"оплат\w*\s+по\s+счёту)",
        ),
        "документы / счёт / реквизиты",
        "order_paperwork",
        "normal",
    ),
]


def check_hard_triggers(text: str) -> TriggerHit | None:
    """Найти хард-триггер в сообщении клиента.

    Возвращает первое совпадение или None. Порядок важен —
    жалобы и возвраты идут раньше «менеджера», чтобы корректно проставлять priority.
    """
    if not text:
        return None
    low = text  # regex'ы сами с IGNORECASE/UNICODE
    for pattern, reason, category, priority in _RULES:
        if pattern.search(low):
            return TriggerHit(reason=reason, category=category, priority=priority)
    return None
