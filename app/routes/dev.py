"""Dev-режим: локальный чат-эмулятор для отладки бота без живого Авито/Битрикса.

Подключается в app.main только при APP_ENV='dev'. Использует ту же функцию
`chat_engine.process_message()`, что будет и в проде — поэтому поведение
в dev-чате идентично будущему поведению в реальной Открытой линии.

Эндпоинты:
  GET  /dev/chat                — HTML-страница с чатом (одна, без фреймворков)
  POST /dev/chat/send           — отправить сообщение клиента, получить ответ бота
  GET  /dev/chat/history?dialog_id=X  — последние 50 событий (in/out/system/...)
  POST /dev/chat/reset?dialog_id=X    — очистить диалог (events + флаг operator_led)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete

from app.services.chat_engine import BotReply, process_message
from app.services.logger import (
    Dialog,
    Event,
    mark_operator_led,
    session_scope,
)

router = APIRouter(prefix="/dev/chat", tags=["dev"])


class SendRequest(BaseModel):
    dialog_id: str = Field(default="dev-chat")
    user_id: str = Field(default="dev-user")
    message: str


class SendResponse(BaseModel):
    action: str
    text: str | None = None
    reason: str | None = None
    blocked_text: str | None = None
    stage: str


@router.post("/send", response_model=SendResponse)
async def dev_send(payload: SendRequest = Body(...)) -> SendResponse:
    if not payload.message.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="empty message")
    reply: BotReply = await process_message(
        dialog_id=payload.dialog_id,
        user_id=payload.user_id,
        text=payload.message,
    )
    return SendResponse(
        action=reply.action,
        text=reply.text,
        reason=reply.reason,
        blocked_text=reply.blocked_text,
        stage=reply.stage,
    )


@router.get("/history")
async def dev_history(
    dialog_id: str = Query(default="dev-chat"),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    from sqlalchemy import select

    async with session_scope() as s:
        stmt = (
            select(Event)
            .where(Event.dialog_id == str(dialog_id))
            .order_by(Event.ts.asc(), Event.id.asc())
            .limit(limit)
        )
        rows = (await s.execute(stmt)).scalars().all()
    return {
        "dialog_id": dialog_id,
        "items": [
            {
                "id": r.id,
                "ts": r.ts.isoformat() if r.ts else None,
                "direction": r.direction,
                "message": r.message,
                "meta": r.meta or {},
            }
            for r in rows
        ],
    }


@router.post("/reset")
async def dev_reset(dialog_id: str = Query(default="dev-chat")) -> dict[str, Any]:
    async with session_scope() as s:
        await s.execute(delete(Event).where(Event.dialog_id == str(dialog_id)))
        await s.execute(delete(Dialog).where(Dialog.dialog_id == str(dialog_id)))
        await s.commit()
    # явно снять operator_led, если где-то кэш
    await mark_operator_led(dialog_id, False)
    # и удалить запись обратно (чтобы диалог был «новым»)
    async with session_scope() as s:
        await s.execute(delete(Dialog).where(Dialog.dialog_id == str(dialog_id)))
        await s.commit()
    return {"status": "reset", "dialog_id": dialog_id}


@router.post("/operator_takeover")
async def dev_operator_takeover(dialog_id: str = Query(default="dev-chat")) -> dict[str, Any]:
    """Искусственно поставить флаг 'оператор в чате' — для проверки, что бот замолкает."""
    await mark_operator_led(dialog_id, True)
    return {"status": "operator_led", "dialog_id": dialog_id}


# --- HTML UI --------------------------------------------------------------

_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Avito-bot dev chat</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         max-width: 760px; margin: 20px auto; padding: 0 16px; }
  h1 { font-size: 18px; margin: 0 0 12px; }
  .row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
  .row input[type=text] { flex: 1; min-width: 120px; padding: 6px 8px; }
  button { padding: 6px 12px; cursor: pointer; }
  #log { border: 1px solid #8884; border-radius: 8px; padding: 12px; height: 440px;
         overflow-y: auto; background: canvas; }
  .msg { margin: 6px 0; padding: 8px 10px; border-radius: 10px; max-width: 85%;
         white-space: pre-wrap; word-wrap: break-word; }
  .in  { background: #2e7dff22; align-self: flex-end; margin-left: auto; }
  .out { background: #8884; }
  .sys { background: #ffb30022; font-size: 12px; color: #a06d00; }
  .esc { background: #ff3b3022; font-size: 12px; color: #b01500; }
  .blk { background: #ff3b3022; font-size: 12px; color: #b01500; text-decoration: line-through; }
  .wrap { display: flex; flex-direction: column; }
  .meta { font-size: 11px; opacity: 0.6; margin-top: 2px; }
  form { display: flex; gap: 8px; margin-top: 12px; }
  form textarea { flex: 1; min-height: 44px; padding: 6px 8px; font: inherit; resize: vertical; }
  .hint { font-size: 12px; opacity: 0.65; margin-top: 4px; }
</style>
</head>
<body>
<h1>Avito-bot — dev chat</h1>

<div class="row">
  <label>Dialog: <input id="dialog" type="text" value="dev-chat" style="width: 140px"></label>
  <label>User: <input id="user" type="text" value="dev-user" style="width: 120px"></label>
  <button id="refresh">Обновить</button>
  <button id="reset">Reset диалога</button>
  <button id="takeover">Оператор зашёл</button>
</div>
<div class="hint">Слева-справа: слева входящие от «клиента», справа — ответы бота. Системные события жёлтые, эскалации — красные.</div>

<div id="log" class="wrap"></div>

<form id="sendForm">
  <textarea id="msg" placeholder="Сообщение от клиента (Enter — отправить, Shift+Enter — перенос)"></textarea>
  <button type="submit">Отправить</button>
</form>

<script>
const logEl = document.getElementById('log');
const msgEl = document.getElementById('msg');
const dialogEl = document.getElementById('dialog');
const userEl = document.getElementById('user');

function dirClass(d) {
  if (d === 'in')        return 'msg in';
  if (d === 'out')       return 'msg out';
  if (d === 'escalate')  return 'msg esc';
  if (d === 'blocked')   return 'msg blk';
  return 'msg sys';
}
function dirLabel(d) {
  return ({in: 'клиент', out: 'бот', escalate: 'эскалация', blocked: 'заблокировано', system: 'система'})[d] || d;
}
function metaSuffix(it) {
  const m = it.meta || {};
  const parts = [];
  if (m.reason)        parts.push('причина: ' + m.reason);
  if (m.block_reason)  parts.push('паттерн: ' + m.block_reason);
  if (m.category)      parts.push('категория: ' + m.category);
  if (m.priority && m.priority !== 'normal') parts.push('приоритет: ' + m.priority);
  if (m.source)        parts.push('источник: ' + m.source);
  return parts.length ? ' · ' + parts.join(' · ') : '';
}
async function refresh() {
  const r = await fetch('/dev/chat/history?dialog_id=' + encodeURIComponent(dialogEl.value));
  const j = await r.json();
  logEl.innerHTML = '';
  for (const it of j.items) {
    const div = document.createElement('div');
    div.className = dirClass(it.direction);
    div.textContent = it.message;
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = dirLabel(it.direction) + ' · ' + (it.ts || '') + metaSuffix(it);
    div.appendChild(meta);
    logEl.appendChild(div);
  }
  logEl.scrollTop = logEl.scrollHeight;
}
async function send(text) {
  const r = await fetch('/dev/chat/send', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({dialog_id: dialogEl.value, user_id: userEl.value, message: text}),
  });
  return await r.json();
}
document.getElementById('sendForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = msgEl.value.trim();
  if (!text) return;
  msgEl.value = '';
  await send(text);
  await refresh();
});
msgEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    document.getElementById('sendForm').dispatchEvent(new Event('submit'));
  }
});
document.getElementById('refresh').onclick = refresh;
document.getElementById('reset').onclick = async () => {
  await fetch('/dev/chat/reset?dialog_id=' + encodeURIComponent(dialogEl.value), {method: 'POST'});
  await refresh();
};
document.getElementById('takeover').onclick = async () => {
  await fetch('/dev/chat/operator_takeover?dialog_id=' + encodeURIComponent(dialogEl.value), {method: 'POST'});
  alert('Флаг operator_led выставлен. Следующее сообщение бот должен проигнорировать (silent).');
};
refresh();
</script>
</body>
</html>
"""


@router.get("", response_class=HTMLResponse)
async def dev_chat_ui() -> HTMLResponse:
    return HTMLResponse(_HTML)
