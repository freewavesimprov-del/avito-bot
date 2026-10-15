"""Окна работы бота и передачи диалогов в Битрикс (московское время)."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.config import get_settings
from app.services.schedule import (
    MSK,
    b24_forward_allowed_at,
    bot_active_at,
    manager_reply_phrase,
)

# Опорная неделя: пн 7 сентября — вс 13 сентября 2026
MON, TUE, WED, THU, FRI, SAT, SUN = 7, 8, 9, 10, 11, 12, 13


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=MSK)


@pytest.fixture(autouse=True)
def _schedule_on(monkeypatch: pytest.MonkeyPatch):
    """conftest глушит расписание для остальных тестов — здесь включаем обратно."""
    monkeypatch.setenv("BOT_SCHEDULE_ENABLE", "true")
    monkeypatch.setenv("B24_FORWARD_WINDOW_ENABLE", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize(
    "moment, active",
    [
        (at(MON, 8, 59), True),    # ночь ещё не кончилась
        (at(MON, 9, 0), False),    # начался рабочий день
        (at(MON, 13, 0), False),
        (at(MON, 16, 59), False),
        (at(MON, 17, 0), True),    # менеджеры ушли
        (at(WED, 3, 0), True),
        (at(FRI, 16, 59), False),
        (at(FRI, 17, 0), True),
        (at(SAT, 12, 0), True),    # выходной — бот работает и днём
        (at(SUN, 12, 0), True),
    ],
)
def test_bot_window(moment, active):
    assert bot_active_at(moment) is active


@pytest.mark.parametrize(
    "moment, allowed",
    [
        (at(MON, 10, 0), False),
        (at(THU, 23, 0), False),   # ночь буднего дня — менеджеров не дёргаем
        (at(FRI, 16, 59), False),
        (at(FRI, 17, 0), True),    # окно выходных открылось
        (at(SAT, 3, 0), True),
        (at(SUN, 23, 59), True),
        (at(MON, 8, 59), True),    # ещё выходное окно
        (at(MON, 9, 0), False),    # закрылось
    ],
)
def test_b24_forward_window(moment, allowed):
    assert b24_forward_allowed_at(moment) is allowed


def test_windows_are_independent():
    """Ночью в будни бот отвечает, но менеджеров не беспокоит."""
    night = at(WED, 2, 0)
    assert bot_active_at(night) is True
    assert b24_forward_allowed_at(night) is False


def test_manager_phrase_mentions_morning_outside_window():
    assert "после 9:00" in manager_reply_phrase(at(WED, 2, 0))
    assert "после" not in manager_reply_phrase(at(SAT, 12, 0))
