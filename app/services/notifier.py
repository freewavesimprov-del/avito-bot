"""Telegram-уведомления о работе бота: новые обращения клиентов и ответы бота.

Отдельно от healthcheck-мониторинга (`deploy/healthcheck.sh`): тот следит за
инфраструктурой, этот — за бизнес-событиями.

Принципы:
  - Никогда не ломает основной поток: любая ошибка отправки только логируется.
  - Отправка в фоне (fire-and-forget), чтобы не задерживать ответ клиенту.
  - Выключается флагом TG_NOTIFY_ENABLE без правки кода.
"""

from __future__ import annotations

import asyncio
import html
import logging

import httpx

from app.config import get_settings

logger = logging.getLogger("avito_bot.notifier")

_TG_API = "https://api.telegram.org"

# Человекочитаемые статусы обработки для строки «Статус:» в уведомлении.
_ACTION_LABELS = {
    "sent": "отвечено ботом",
    "escalated": "передано менеджеру",
    "blocked": "ответ заблокирован пост-фильтром",
    "silent": "бот промолчал",
}

_BACKGROUND: set[asyncio.Task] = set()


def _trim(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


async def _send(text: str) -> None:
    settings = get_settings()
    token, chat_id = settings.TG_BOT_TOKEN, settings.TG_CHAT_ID
    if not token or not chat_id:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_TG_API}/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
        if resp.status_code != 200:
            logger.warning("telegram notify failed: %s %s", resp.status_code, resp.text[:200])
    except Exception:
        # Уведомления — вспомогательная функция, их сбой не должен влиять на бота.
        logger.exception("telegram notify: unexpected error")


def _spawn(coro) -> None:
    """Fire-and-forget с удержанием ссылки (иначе GC может убить задачу)."""
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        # Нет запущенного event loop (например, вызов из синхронного скрипта)
        return
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


def notify_dialog(
    *,
    dialog_id: str,
    client_text: str,
    bot_text: str | None,
    action: str,
    reason: str | None = None,
) -> None:
    """Уведомление о завершённой обработке обращения.

    Вызывается после process_message. Собирает в одно сообщение и вопрос клиента,
    и ответ бота — так в телефоне видно всю картину без переходов в B24.
    """
    settings = get_settings()
    if not settings.TG_NOTIFY_ENABLE:
        return

    lines = ["📩 <b>Обращение с Авито</b>", ""]
    lines.append(f"<b>Клиент:</b> {html.escape(_trim(client_text, 500))}")

    if bot_text:
        lines.append("")
        lines.append(f"🤖 <b>Ответ бота:</b> {html.escape(_trim(bot_text, 700))}")

    status = _ACTION_LABELS.get(action, action)
    lines.append("")
    lines.append(f"<b>Статус:</b> {html.escape(status)}")
    if reason:
        lines.append(f"<b>Причина:</b> {html.escape(_trim(reason, 200))}")
    lines.append(f"<i>диалог {html.escape(dialog_id)}</i>")

    _spawn(_send("\n".join(lines)))


async def notify_stale_dialog(
    *,
    session_id: int,
    subject: str,
    created: str,
    last_text: str,
    minutes: int,
    portal: str = "",
) -> bool:
    """Алерт: диалог Открытой линии висит без ответа оператора.

    В отличие от notify_dialog — ждём отправки (await), потому что сторож
    помечает сессию как «оповещённую» только после успешной доставки.
    Возвращает True если отправлено.
    """
    settings = get_settings()
    if not settings.TG_NOTIFY_ENABLE:
        return False
    if not settings.TG_BOT_TOKEN or not settings.TG_CHAT_ID:
        return False

    lines = [
        f"⚠️ <b>Диалог Авито без ответа &gt; {minutes} мин</b>",
        "",
        f"<b>{html.escape(_trim(subject, 200))}</b>",
        f"Начат: {html.escape(created)}",
        f"Сессия: №{session_id}",
        "",
        f"<b>Последнее сообщение:</b>",
        html.escape(_trim(last_text, 500)),
    ]
    if portal:
        lines.append("")
        lines.append(
            f'<a href="https://{portal}/online/?IM_HISTORY=imol|{session_id}">'
            "Открыть диалог в Битрикс24</a>"
        )

    await _send("\n".join(lines))
    return True
