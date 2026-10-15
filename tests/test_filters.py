"""Тесты post-filter и парсера маркера ESCALATE."""

from __future__ import annotations

import pytest

from app.services.filters import parse_escalate_marker, post_filter

# --- post_filter ----------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected_reason",
    [
        # Телефоны
        ("Звоните +7 (495) 123-45-67", "phone"),
        ("Тел. 8 495 1234567", "phone"),
        ("Звонок: 84951234567", "phone"),
        ("+7-985-123-45-67 наш номер", "phone"),
        # Email
        ("Пишите на manager@example.com", "email"),
        ("Order.123@some-shop.ru — наш", "email"),
        # Мессенджеры
        ("Напишите в WhatsApp", "messenger_link"),
        ("Я в ватсапе всё расскажу", "messenger_link"),
        ("https://t.me/managername", "messenger_link"),
        ("https://wa.me/79991234567", "messenger_link"),
        ("Telegram: @ourshop", "messenger_link"),
        # Скидки
        ("Дам скидку 10%", "specific_discount"),
        ("Скидка 1500 рублей", "specific_discount"),
        ("Скину 500", "specific_discount"),
        ("Уступлю 2000", "specific_discount"),
        # Конкретные даты
        ("Привезём завтра", "specific_date"),
        ("Через 3 дня будет у вас", "specific_date"),
        # Внешние ссылки
        ("Смотрите сайт https://example.com/catalog", "external_link"),
        ("www.our-shop.ru — каталог", "external_link"),
        # Утёкший маркер
        ("Передаю менеджеру ESCALATE: причина", "leaked_marker"),
    ],
)
def test_post_filter_blocks(text: str, expected_reason: str):
    ok, reason = post_filter(text)
    assert ok is False
    assert reason == expected_reason


@pytest.mark.parametrize(
    "text",
    [
        "Здравствуйте! Чем могу помочь?",
        "По данным на 22.04 в наличии 5 шт, цена 14 000 ₽.",
        "Доставка по МО 2 000 ₽, срок 1-3 дня.",
        "Самовывоз бесплатно со склада в МО.",
        "Гарантия 12 месяцев, передам менеджеру для деталей.",
        "Скидку согласует менеджер, передам запрос.",  # без конкретной суммы — ок
        "Срок 1-3 дня по МО, точную дату согласует менеджер.",  # без «завтра»
    ],
)
def test_post_filter_allows_clean_text(text: str):
    ok, reason = post_filter(text)
    assert ok is True
    assert reason is None


def test_post_filter_empty_string_is_ok():
    assert post_filter("") == (True, None)


# --- parse_escalate_marker ------------------------------------------------


def test_parse_marker_at_end():
    text = "Скидки согласует менеджер.\nESCALATE: запрос скидки"
    cleaned, reason = parse_escalate_marker(text)
    assert reason == "запрос скидки"
    assert "ESCALATE" not in cleaned
    assert "Скидки согласует менеджер" in cleaned


def test_parse_marker_inline_separate_line():
    text = "Хорошо, передаю менеджеру.\n\nESCALATE: оформление заказа\n"
    cleaned, reason = parse_escalate_marker(text)
    assert reason == "оформление заказа"
    assert cleaned == "Хорошо, передаю менеджеру."


def test_parse_marker_case_insensitive():
    text = "Передаю.\nescalate: причина"
    cleaned, reason = parse_escalate_marker(text)
    assert reason == "причина"
    assert "escalate" not in cleaned.lower()


def test_parse_no_marker():
    text = "Здравствуйте! Что вас интересует?"
    cleaned, reason = parse_escalate_marker(text)
    assert reason is None
    assert cleaned == text


def test_parse_multiple_markers_takes_last():
    text = "ESCALATE: первая\nрабочий ответ\nESCALATE: финальная"
    cleaned, reason = parse_escalate_marker(text)
    assert reason == "финальная"
    assert "ESCALATE" not in cleaned
    assert "рабочий ответ" in cleaned


def test_parse_marker_strips_extra_blanks():
    text = "Текст\n\n\n\nESCALATE: x\n"
    cleaned, reason = parse_escalate_marker(text)
    assert reason == "x"
    assert "\n\n\n" not in cleaned


# Реальные форматы которые LLM генерирует в проде (yandexgpt-lite не всегда
# следует «ESCALATE: <причина>» строго).


def test_parse_marker_without_colon():
    """LLM иногда пишет ESCALATE без двоеточия — должны словить."""
    text = "Передаю запрос. ESCALATE"
    cleaned, reason = parse_escalate_marker(text)
    assert reason == "unspecified"
    assert "ESCALATE" not in cleaned
    assert "Передаю запрос" in cleaned


def test_parse_marker_with_dash():
    """ESCALATE — уточнение модели"""
    text = "В наличии есть. ESCALATE — уточнение модели"
    cleaned, reason = parse_escalate_marker(text)
    assert reason == "уточнение модели"
    assert "ESCALATE" not in cleaned


def test_parse_marker_with_emdash():
    """ESCALATE – уточнение (em-dash без пробелов вокруг)"""
    text = "Вопрос непростой. ESCALATE – нужны детали"
    cleaned, reason = parse_escalate_marker(text)
    assert reason == "нужны детали"
    assert "ESCALATE" not in cleaned


def test_parse_marker_in_parentheses():
    """LLM иногда заворачивает в скобки: (ESCALATE: причина)"""
    text = "Передам менеджеру. (ESCALATE: оформление заказа)"
    cleaned, reason = parse_escalate_marker(text)
    assert reason == "оформление заказа"
    assert "ESCALATE" not in cleaned


def test_parse_marker_with_trailing_punctuation():
    """ESCALATE: причина. — уберём концевую точку из reason."""
    text = "Передаю.\nESCALATE: запрос скидки."
    _cleaned, reason = parse_escalate_marker(text)
    assert reason == "запрос скидки"  # без точки
