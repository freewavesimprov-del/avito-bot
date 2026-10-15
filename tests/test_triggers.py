"""Тесты hard-triggers (SPEC §2)."""

from __future__ import annotations

import pytest

from app.services.triggers import check_hard_triggers


@pytest.mark.parametrize(
    "text,category,priority",
    [
        # Жалобы / брак
        ("Жалоба на ваш диван", "complaint", "urgent"),
        ("Прислали с браком", "complaint", "urgent"),
        ("Сломалась ножка после первого дня", "complaint", "urgent"),
        ("Хочу подать рекламацию", "complaint", "urgent"),
        ("Дефект на боковой панели", "complaint", "urgent"),
        ("Не работает механизм", "complaint", "urgent"),
        ("Шкаф развалился через неделю", "complaint", "urgent"),
        # Возвраты
        ("Хочу вернуть деньги", "return", "urgent"),
        ("Оформите возврат, пожалуйста", "return", "urgent"),
        ("Хочу вернуть товар", "return", "urgent"),
        ("Отказ от покупки, не подошло", "return", "urgent"),
        # Запрос менеджера / контакта
        ("Позовите менеджера", "manager", "normal"),
        ("Позовите оператора", "manager", "normal"),
        ("Дайте телефон", "manager", "normal"),
        ("Дайте номер для связи", "manager", "normal"),
        ("Давайте созвонимся", "manager", "normal"),
        ("Перезвоните мне", "manager", "normal"),
        # Номер заказа (6+ цифр)
        ("Где мой заказ 458219?", "order_number", "normal"),
        ("Заказ №1234567", "order_number", "normal"),
        # Юридические темы
        ("Хочу составить претензию", "legal", "urgent"),
        ("Обращусь в Роспотребнадзор", "legal", "urgent"),
        # Документы / счета (order_paperwork) — Татьяна выставляет в 1С
        ("Выставите счёт", "order_paperwork", "normal"),
        ("Дайте реквизиты компании", "order_paperwork", "normal"),
        ("Какой у вас ИНН?", "order_paperwork", "normal"),
        ("Можно по безналу?", "order_paperwork", "normal"),
        ("Нам нужен договор поставки", "order_paperwork", "normal"),
        ("Оплата по счёту возможна?", "order_paperwork", "normal"),
    ],
)
def test_trigger_hits(text: str, category: str, priority: str):
    hit = check_hard_triggers(text)
    assert hit is not None, f"должен был сработать на: {text!r}"
    assert hit.category == category
    assert hit.priority == priority
    assert hit.reason  # непустая


@pytest.mark.parametrize(
    "text",
    [
        "Здравствуйте, комод Ангара есть?",
        "Какие шкафы у вас?",
        "Доставка по МО есть?",
        "Какой материал у Мадрида?",
        "Сколько стоит самовывоз?",
        "Гарантия 12 месяцев — это на что распространяется?",  # «гарантия» без жалобы
        "Хочу диван за 20000",  # цифры до 6 знаков
    ],
)
def test_trigger_does_not_fire_on_normal(text: str):
    assert check_hard_triggers(text) is None


def test_trigger_complaint_takes_priority_over_manager():
    """Если в одном сообщении и жалоба, и просьба позвать менеджера — категория должна быть complaint."""
    text = "У меня жалоба на брак, позовите менеджера"
    hit = check_hard_triggers(text)
    assert hit is not None
    assert hit.category == "complaint"
    assert hit.priority == "urgent"


def test_trigger_empty_string():
    assert check_hard_triggers("") is None
    assert check_hard_triggers(None) is None  # type: ignore[arg-type]
