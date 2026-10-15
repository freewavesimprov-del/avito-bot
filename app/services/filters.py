"""Post-filter и парсер маркера ESCALATE для ответа LLM.

Применение в pipeline (chat_engine):
  1. raw_reply = LLM.generate(...)
  2. cleaned, reason = parse_escalate_marker(raw_reply)
  3. ok, block_reason = post_filter(cleaned)
  4. if not ok → отправляем фиксированный текст эскалации,
                cleaned попадает в events как 'blocked'.
  5. elif reason → отправляем cleaned клиенту + эскалация.
  6. else → отправляем cleaned, продолжаем диалог.

Паттерны и логика — по SPEC §4.
"""

from __future__ import annotations

import re

# Фиксированный текст, который шлём клиенту при срабатывании post-filter,
# при невозможности дозвониться до LLM, или при hard-trigger без LLM.
ESCALATION_CLIENT_MESSAGE = (
    "Секунду, передаю вопрос менеджеру — ответит в этом же чате"
)

# --- Парсер маркера ESCALATE ----------------------------------------------

# Маркер где угодно в тексте. Формы которые встречаются в проде:
#   "ESCALATE: запрос скидки"     ← каноническая форма
#   "ESCALATE — уточнение"         ← LLM ставит тире вместо двоеточия
#   "ESCALATE"                     ← LLM просто пишет слово в конце без причины
#   "ESCALATE.<eol>"               ← с точкой
#   "(ESCALATE: ...)"              ← в скобках
# Группа причины опциональна: если её нет — записываем "unspecified".
_ESCALATE_RE = re.compile(
    r"(?i)\s*\(?ESCALATE\b\)?\s*[:\-—––]?\s*([^\n)]*?)\s*\)?\s*(?=\n|$)"
)


# Модель иногда рассуждает вслух про сам маркер: «ESCALATE не требуется»,
# «ESCALATE не нужен». Это НЕ эскалация — наоборот, явный отказ от неё.
# Без этой проверки бот уходил из чата на ровном месте, бросая клиента.
_ESCALATE_NEGATION_RE = re.compile(
    r"^(?:не\s+(?:требуется|нужен|нужно|нужна|надо)|нет|not\s+needed)\b",
    re.IGNORECASE,
)


def parse_escalate_marker(text: str) -> tuple[str, str | None]:
    """Найти маркер `ESCALATE: <причина>` и удалить его из текста.

    Возвращает: (cleaned_text, reason).
    Если маркер отсутствует — reason=None, текст возвращается как есть (но обрезанный).
    Если маркеров несколько — берём последний (как самый вероятный итоговый), все вырезаем.
    Если вместо причины стоит отрицание («ESCALATE не требуется») — маркер вырезаем,
    но эскалацией это не считаем.
    """
    matches = list(_ESCALATE_RE.finditer(text or ""))
    if not matches:
        return (text or "").strip(), None
    raw_reason = matches[-1].group(1).strip().strip(".,;:")
    cleaned = _ESCALATE_RE.sub("", text).strip()
    # Иногда LLM перед маркером ставит лишний перевод строки или двойные пустые
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if raw_reason and _ESCALATE_NEGATION_RE.match(raw_reason):
        return cleaned, None
    reason = raw_reason or "unspecified"
    return cleaned, reason


# --- Post-filter ----------------------------------------------------------

# Список (regex, причина). Порядок важен — более специфичные сверху.
# messenger_link и external_link идут ДО phone, чтобы не ловить цифры внутри URL
# (например, wa.me/79991234567 — это messenger_link, не phone).
_BLOCKED_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Мессенджеры и короткие домены
    (re.compile(r"(whatsapp|ватсап|telegram|телеграм|вайбер|viber|tg\.me|wa\.me|t\.me)", re.IGNORECASE), "messenger_link"),
    # Внешние ссылки (тоже до phone, чтобы цифры из URL-параметров не ловились)
    (re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE), "external_link"),
    # Email
    (re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", re.IGNORECASE), "email"),
    # Телефоны: +7(495)123-45-67, 8 495 123 45 67, 84951234567 и пр.
    (re.compile(r"(?<!\d)\+?[78][\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)"), "phone"),
    # Конкретные скидки (10%, скидка 5, скину 200, уступлю 1000)
    (re.compile(r"(?:\d+\s?(?:%|процент)|скидк[аяуе]\s+\d+|скину\s+\d+|уступлю\s+\d+)", re.IGNORECASE), "specific_discount"),
    # Конкретные близкие даты
    (re.compile(r"(?:\bзавтра\b|\bпослезавтра\b|\bчерез\s+\d+\s+дн)", re.IGNORECASE), "specific_date"),
    # Утёкший маркер ESCALATE — не должно дойти до клиента
    (re.compile(r"\bESCALATE\b", re.IGNORECASE), "leaked_marker"),
]


def post_filter(text: str) -> tuple[bool, str | None]:
    """Проверить ответ LLM перед отправкой клиенту.

    Возвращает (True, None) — текст безопасен,
    или (False, reason) — нашли запрещённый паттерн.
    """
    if not text:
        return True, None
    for pattern, reason in _BLOCKED_PATTERNS:
        if pattern.search(text):
            return False, reason
    return True, None
