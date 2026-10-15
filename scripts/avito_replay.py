"""Прогон реальных диалогов из Авито через нашего бота.

Берёт `data/avito_dataset.json` (создаётся через `avito_fetch.py`),
для каждого чата проигрывает входящие сообщения клиента в исходном порядке,
вызывая `chat_engine.process_message()`. Делает отдельный `dialog_id` для replay,
чтобы не смешивать с настоящими диалогами в SQLite.

После каждого замоканного клиентского сообщения смотрим, что бот ответил —
и записываем в отчёт. Никаких записей в Avito или Битрикс — только локально.

Использование:
    .venv/bin/python -m scripts.avito_replay
    .venv/bin/python -m scripts.avito_replay --max-chats 30 --first-n-msgs 3
    .venv/bin/python -m scripts.avito_replay --report report.md

Вывод:
  • Markdown-отчёт с агрегатами и примерами
  • JSON со всеми реплеями (опц., через --json-out)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.services.chat_engine import process_message
from app.services.logger import dispose_db, init_db, mark_operator_led

# Префикс для replay dialog_id, чтобы он не пересекался с настоящими DIALOG_ID
REPLAY_PREFIX = "replay__"


@dataclass
class TurnResult:
    chat_id: str
    turn_idx: int
    client_text: str
    real_seller_reply: str | None  # что отвечал живой менеджер (для сравнения)
    bot_action: str
    bot_text: str | None
    bot_reason: str | None
    bot_blocked_text: str | None


def _client_turns(messages: list[dict[str, Any]], max_turns: int) -> list[tuple[str, str | None]]:
    """Извлекает «ходы»: пары (сообщение клиента, следующий ответ продавца, если был).

    Возвращает до max_turns ходов в исходном порядке.
    """
    turns: list[tuple[str, str | None]] = []
    i = 0
    while i < len(messages) and len(turns) < max_turns:
        m = messages[i]
        if m.get("direction") != "in":
            i += 1
            continue
        text = (m.get("text") or "").strip()
        if not text:
            i += 1
            continue
        # Ищем ближайший out-ответ продавца
        seller_reply: str | None = None
        for j in range(i + 1, len(messages)):
            nxt = messages[j]
            if nxt.get("direction") == "out":
                seller_reply = (nxt.get("text") or "").strip() or None
                break
            if nxt.get("direction") == "in":
                break
        turns.append((text, seller_reply))
        i += 1
    return turns


async def replay_chat(chat: dict[str, Any], first_n_msgs: int) -> list[TurnResult]:
    chat_id = chat.get("id") or ""
    dialog_id = f"{REPLAY_PREFIX}{chat_id}"

    # На случай если предыдущий запуск пометил диалог operator_led — снимаем
    await mark_operator_led(dialog_id, False)

    msgs = chat.get("messages") or []
    turns = _client_turns(msgs, max_turns=first_n_msgs)

    results: list[TurnResult] = []
    for idx, (client_text, seller_reply) in enumerate(turns, 1):
        # Префикс «карточки товара» в первое сообщение, если это первый ход —
        # чтобы LLM знал контекст объявления (как в реальном Авито).
        ctx_text = client_text
        if idx == 1:
            ctx_value = (chat.get("context") or {}).get("value") or {}
            item_title = ctx_value.get("title")
            if item_title:
                ctx_text = f"[Объявление: {item_title}]\n{client_text}"

        reply = await process_message(
            dialog_id=dialog_id, user_id=str(idx), text=ctx_text
        )
        results.append(
            TurnResult(
                chat_id=chat_id,
                turn_idx=idx,
                client_text=client_text,
                real_seller_reply=seller_reply,
                bot_action=reply.action,
                bot_text=reply.text,
                bot_reason=reply.reason,
                bot_blocked_text=reply.blocked_text,
            )
        )
        # Если бот эскалировал/заблокировал — он бы вышел из чата. Прерываем replay.
        if reply.action in ("escalated", "blocked"):
            break
    return results


def _build_report(turns: list[TurnResult]) -> str:
    """Markdown-отчёт: агрегаты + примеры по каждой категории."""
    by_action = Counter(t.bot_action for t in turns)
    by_reason = Counter((t.bot_reason or "—") for t in turns if t.bot_action != "sent")

    lines: list[str] = []
    lines.append(f"# Avito replay — {len(turns)} ходов")
    lines.append("")
    lines.append("## Распределение действий бота")
    lines.append("")
    lines.append("| Action | Кол-во | % |")
    lines.append("|---|---:|---:|")
    total = sum(by_action.values()) or 1
    for action, n in by_action.most_common():
        lines.append(f"| {action} | {n} | {100 * n / total:.1f}% |")
    lines.append("")

    if by_reason:
        lines.append("## Топ причин эскалации/блокировки")
        lines.append("")
        lines.append("| Reason | Кол-во |")
        lines.append("|---|---:|")
        for reason, n in by_reason.most_common(15):
            lines.append(f"| {reason} | {n} |")
        lines.append("")

    # Примеры
    def _section(title: str, action: str, limit: int = 8) -> None:
        examples = [t for t in turns if t.bot_action == action][:limit]
        if not examples:
            return
        lines.append(f"## Примеры: {title}")
        lines.append("")
        for t in examples:
            lines.append(f"**chat={t.chat_id} · turn {t.turn_idx}**")
            lines.append("")
            lines.append(f"> **Клиент:** {t.client_text}")
            if t.real_seller_reply:
                lines.append(f"> **Реальный менеджер:** {t.real_seller_reply}")
            if t.bot_text:
                lines.append(f"> **Бот:** {t.bot_text}")
            if t.bot_reason:
                lines.append(f"> _причина: {t.bot_reason}_")
            if t.bot_blocked_text:
                lines.append(f"> _LLM хотел отправить: {t.bot_blocked_text}_")
            lines.append("")

    _section("обычные ответы (sent)", "sent")
    _section("эскалации (escalated)", "escalated")
    _section("блокировки post-filter (blocked)", "blocked")
    _section("молчание (silent)", "silent")

    return "\n".join(lines)


async def main(args: argparse.Namespace) -> int:
    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        print(f"ERROR: датасет {dataset_path} не найден. Сначала запусти avito_fetch.", file=sys.stderr)
        return 2

    data = json.loads(dataset_path.read_text(encoding="utf-8"))
    chats: list[dict[str, Any]] = data.get("chats") or []
    print(f"[ok] загружено чатов: {len(chats)}")

    if args.max_chats:
        chats = chats[: args.max_chats]
        print(f"     ограничено первыми {len(chats)}")

    await init_db()
    try:
        all_turns: list[TurnResult] = []
        for i, chat in enumerate(chats, 1):
            try:
                results = await replay_chat(chat, first_n_msgs=args.first_n_msgs)
            except Exception as e:
                print(f"  [warn] chat {chat.get('id')}: {e}", file=sys.stderr)
                continue
            all_turns.extend(results)
            if i % 10 == 0:
                print(f"  ... прогнано {i}/{len(chats)}, ходов: {len(all_turns)}")
    finally:
        await dispose_db()

    print(f"[ok] всего ходов: {len(all_turns)}")

    report = _build_report(all_turns)
    Path(args.report).write_text(report, encoding="utf-8")
    print(f"[done] отчёт: {args.report}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps([asdict(t) for t in all_turns], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[done] полные реплеи: {args.json_out}")
    return 0


def cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="data/avito_dataset.json")
    ap.add_argument("--report", default="data/avito_replay_report.md")
    ap.add_argument("--json-out", default=None, help="опц.: сохранить полные реплеи в JSON")
    ap.add_argument("--max-chats", type=int, default=None)
    ap.add_argument(
        "--first-n-msgs",
        type=int,
        default=3,
        help="сколько первых клиентских сообщений прогонять в каждом чате",
    )
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args)))


if __name__ == "__main__":
    cli()
