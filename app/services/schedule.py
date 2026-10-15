"""Расписание работы бота по московскому времени.

Две независимые вещи, у каждой своё окно:

1. Окно бота — когда бот вообще отвечает клиенту. По умолчанию нерабочее
   время, с 17:00 до 09:00. Днём в чате работают живые менеджеры через
   Avito Pro, и бот им не мешает.

2. Окно передачи в Битрикс — когда боту разрешено дёргать менеджеров
   (эскалация выводит бота из чата, и диалог падает в очередь операторов).
   По умолчанию выходные: с 17:00 пятницы до 09:00 понедельника. В будни
   менеджеры и так видят переписку в Avito Pro, дублировать не нужно.

Зона считается явно через zoneinfo, а не по локальному времени машины:
сервер могут перенастроить, а расписание должно остаться московским.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import get_settings

MSK = ZoneInfo("Europe/Moscow")

# Дни недели как их отдаёт datetime.weekday()
MONDAY = 0
FRIDAY = 4
SATURDAY = 5
SUNDAY = 6


def now_msk() -> datetime:
    """Текущее московское время. Единая точка — удобно подменять в тестах."""
    return datetime.now(MSK)


def _in_night_window(moment: datetime, start_hour: int, end_hour: int) -> bool:
    """Попадает ли момент в окно, которое переваливает через полночь.

    start_hour=17, end_hour=9 → истина с 17:00 до 08:59 следующего дня.
    Вырожденный случай start == end трактуем как «окно всегда открыто».
    """
    if start_hour == end_hour:
        return True
    if start_hour > end_hour:
        return moment.hour >= start_hour or moment.hour < end_hour
    return start_hour <= moment.hour < end_hour


def bot_active_at(moment: datetime) -> bool:
    """Отвечает ли бот клиентам в этот момент.

    Выходные считаем нерабочим временем целиком: в субботу днём подхватить
    диалог всё равно некому, и молчащий бот означал бы просто тишину клиенту.
    """
    s = get_settings()
    if not s.BOT_SCHEDULE_ENABLE:
        return True
    if s.BOT_WEEKEND_ALWAYS_ACTIVE and moment.weekday() in (SATURDAY, SUNDAY):
        return True
    return _in_night_window(moment, s.BOT_ACTIVE_FROM_HOUR, s.BOT_ACTIVE_TO_HOUR)


def bot_active_now() -> bool:
    return bot_active_at(now_msk())


def b24_forward_allowed_at(moment: datetime) -> bool:
    """Можно ли сейчас передавать диалог менеджерам в Битрикс.

    Окно: с FROM_HOUR пятницы до TO_HOUR того дня, который назначен концом
    (по умолчанию воскресенье), суббота целиком.
    """
    s = get_settings()
    if not s.B24_FORWARD_WINDOW_ENABLE:
        return True

    day = moment.weekday()
    end_day = int(getattr(s, "B24_FORWARD_TO_WEEKDAY", SUNDAY))
    if day == FRIDAY:
        return moment.hour >= s.B24_FORWARD_FROM_HOUR
    if day == SATURDAY:
        # Суббота целиком внутри окна, если оно вообще доходит до воскресенья.
        return end_day >= SATURDAY
    if day == end_day:
        # День, которым окно заканчивается: только до указанного часа.
        # Владелец просил окно «с 17:00 пятницы до 19:00 воскресенья»
        # (23.09.2026) — в будни клиентов ведёт Татьяна из кабинета Авито.
        return moment.hour < s.B24_FORWARD_TO_HOUR
    if day == SUNDAY and end_day == MONDAY:
        return True
    return False


def b24_forward_allowed_now() -> bool:
    return b24_forward_allowed_at(now_msk())


def describe_now() -> str:
    """Строка для логов и диагностики: что открыто прямо сейчас."""
    moment = now_msk()
    return (
        f"{moment:%Y-%m-%d %H:%M} МСК ({moment:%a}), "
        f"бот={'активен' if bot_active_at(moment) else 'молчит'}, "
        f"передача в B24={'разрешена' if b24_forward_allowed_at(moment) else 'закрыта'}"
    )


def manager_reply_phrase(moment: datetime | None = None) -> str:
    """Когда менеджер выйдет на связь — фразой для клиента.

    Нужна, когда бот отвечает вне окна передачи в Битрикс: обещать «сейчас
    передам менеджеру» в три часа ночи среды нечестно, менеджер будет утром.
    """
    moment = moment or now_msk()
    s = get_settings()
    if b24_forward_allowed_at(moment):
        return "менеджер ответит в этом же чате"
    return f"менеджер ответит в этом же чате после {s.BOT_ACTIVE_TO_HOUR}:00 МСК"
