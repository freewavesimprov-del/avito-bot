"""SQLite-хранилище событий и флагов диалогов (через SQLAlchemy async).

Используется как журнал (`events`) и как источник истории для LLM + флаг "оператор ведёт диалог".
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
    select,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import get_settings

Direction = Literal["in", "out", "escalate", "blocked", "system"]


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    dialog_id: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    direction: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "direction IN ('in', 'out', 'escalate', 'blocked', 'system')",
            name="ck_events_direction",
        ),
        Index("idx_events_dialog", "dialog_id", "ts"),
        Index("idx_events_direction", "direction", "ts"),
    )


class Dialog(Base):
    __tablename__ = "dialogs"

    dialog_id: Mapped[str] = mapped_column(String, primary_key=True)
    operator_led: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )


class AvitoSystemMessage(Base):
    """Системное сообщение из Avito-чата, обнаруженное поллером.

    Хранит все system-msgs из Avito Messenger API. Используется для:
      1. Идемпотентности (не дублируем обработку одного msg_id)
      2. Аудита неизвестных flow_id (чтобы менеджер видел незнакомые события)
      3. Истории обработанных заказов

    `processed` ставится в True после того как watcher выполнил все side-effects
    (ack-сообщение клиенту, B24 deal, лог в OL chat).
    `classification` — что watcher решил по этому msg: 'order' | 'preorder' | 'ignore' | 'unknown'.
    """

    __tablename__ = "avito_system_messages"

    msg_id: Mapped[str] = mapped_column(String, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String, nullable=False)
    flow_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_ts: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    classification: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    b24_deal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ack_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    __table_args__ = (
        Index("idx_avito_sys_chat", "chat_id", "created_ts"),
        Index("idx_avito_sys_class", "classification", "processed"),
    )


class OLAlert(Base):
    """Отправленные алерты о зависших диалогах Открытой линии.

    Нужна только для дедупликации: один алерт на сессию, чтобы сторож
    не долбил в Telegram каждые N минут по одному и тому же диалогу.
    """

    __tablename__ = "ol_alerts"

    session_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    alerted_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


# --- Engine lifecycle -------------------------------------------------------

_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _db_url() -> str:
    path = get_settings().DB_PATH
    # Гарантируем, что каталог существует
    Path(os.path.dirname(path) or ".").mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{path}"


async def init_db() -> None:
    """Создать engine, sessionmaker и таблицы. Идемпотентна."""
    global _engine, _sessionmaker
    if _engine is not None:
        return
    _engine = create_async_engine(_db_url(), future=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose_db() -> None:
    """Закрыть engine. Для graceful shutdown и для тестов."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Контекстный менеджер сессии SQLAlchemy. Публичный API для других модулей
    (например routes/dev.py делает прямые SELECT/DELETE по events/dialogs)."""
    if _sessionmaker is None:
        await init_db()
    assert _sessionmaker is not None
    async with _sessionmaker() as session:
        yield session


# алиас для внутреннего использования в этом модуле
_session = session_scope


# --- Public API -------------------------------------------------------------


async def log_event(
    dialog_id: str,
    direction: Direction,
    message: str,
    user_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> int:
    """Записать строку в events, вернуть id."""
    async with _session() as s:
        # meta хранится как JSON; SQLAlchemy's JSON-тип сам сериализует, но для гарантии
        # нормализуем значения, которые не сериализуются по-умолчанию (datetime и т.п.).
        safe_meta = json.loads(json.dumps(meta, default=str)) if meta is not None else None
        row = Event(
            dialog_id=str(dialog_id),
            user_id=user_id,
            direction=direction,
            message=message,
            meta=safe_meta,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row.id


async def get_recent_history(dialog_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Последние сообщения диалога, отсортированные по возрастанию ts (старое → новое).

    Возвращает только in/out — системные и заблокированные не идут в контекст LLM.
    """
    async with _session() as s:
        stmt = (
            select(Event)
            .where(Event.dialog_id == str(dialog_id))
            .where(Event.direction.in_(("in", "out")))
            .order_by(Event.ts.desc(), Event.id.desc())
            .limit(limit)
        )
        rows = (await s.execute(stmt)).scalars().all()
    return [
        {
            "ts": row.ts.isoformat() if row.ts else None,
            "direction": row.direction,
            "message": row.message,
        }
        for row in reversed(rows)
    ]


async def mark_operator_led(dialog_id: str, value: bool = True) -> None:
    """Upsert флага operator_led."""
    async with _session() as s:
        stmt = (
            sqlite_insert(Dialog)
            .values(dialog_id=str(dialog_id), operator_led=value)
            .on_conflict_do_update(
                index_elements=[Dialog.dialog_id],
                set_={"operator_led": value, "updated_at": func.current_timestamp()},
            )
        )
        await s.execute(stmt)
        await s.commit()


async def is_operator_led(dialog_id: str) -> bool:
    async with _session() as s:
        stmt = select(Dialog.operator_led).where(Dialog.dialog_id == str(dialog_id))
        val = (await s.execute(stmt)).scalar_one_or_none()
    return bool(val)


async def ensure_dialog(dialog_id: str) -> None:
    """Создать запись dialogs если ещё нет. Используется на ONIMBOTJOINCHAT."""
    async with _session() as s:
        stmt = (
            sqlite_insert(Dialog)
            .values(dialog_id=str(dialog_id), operator_led=False)
            .on_conflict_do_nothing(index_elements=[Dialog.dialog_id])
        )
        await s.execute(stmt)
        await s.commit()


# --- Avito system messages -------------------------------------------------


async def avito_sys_msg_exists(msg_id: str) -> bool:
    async with _session() as s:
        stmt = select(AvitoSystemMessage.msg_id).where(AvitoSystemMessage.msg_id == str(msg_id))
        val = (await s.execute(stmt)).scalar_one_or_none()
        return val is not None


async def avito_sys_msg_record(
    *,
    msg_id: str,
    chat_id: str,
    flow_id: str | None,
    created_ts: int,
    text: str,
    classification: str = "unknown",
) -> None:
    """Сохранить системное сообщение Avito. Идемпотентно (insert or ignore)."""
    async with _session() as s:
        stmt = (
            sqlite_insert(AvitoSystemMessage)
            .values(
                msg_id=str(msg_id),
                chat_id=str(chat_id),
                flow_id=flow_id,
                created_ts=int(created_ts),
                text=text,
                classification=classification,
                processed=False,
                ack_sent=False,
            )
            .on_conflict_do_nothing(index_elements=[AvitoSystemMessage.msg_id])
        )
        await s.execute(stmt)
        await s.commit()


async def avito_sys_msg_mark_processed(
    msg_id: str, *, classification: str | None = None,
    b24_deal_id: int | None = None, ack_sent: bool | None = None,
) -> None:
    async with _session() as s:
        values: dict[str, Any] = {"processed": True}
        if classification is not None:
            values["classification"] = classification
        if b24_deal_id is not None:
            values["b24_deal_id"] = b24_deal_id
        if ack_sent is not None:
            values["ack_sent"] = ack_sent
        await s.execute(
            update(AvitoSystemMessage)
            .where(AvitoSystemMessage.msg_id == str(msg_id))
            .values(**values)
        )
        await s.commit()


# --- OL watchdog alerts ----------------------------------------------------


async def ol_alert_exists(session_id: int) -> bool:
    async with _session() as s:
        stmt = select(OLAlert.session_id).where(OLAlert.session_id == int(session_id))
        return (await s.execute(stmt)).scalar_one_or_none() is not None


async def ol_alert_record(session_id: int, chat_id: int | None = None) -> None:
    async with _session() as s:
        stmt = (
            sqlite_insert(OLAlert)
            .values(session_id=int(session_id), chat_id=chat_id)
            .on_conflict_do_nothing(index_elements=[OLAlert.session_id])
        )
        await s.execute(stmt)
        await s.commit()


# Экспорт для тестов (переопределение sessionmaker):
__all__ = [
    "AvitoSystemMessage",
    "OLAlert",
    "ol_alert_exists",
    "ol_alert_record",
    "Base",
    "Dialog",
    "Event",
    "avito_sys_msg_exists",
    "avito_sys_msg_mark_processed",
    "avito_sys_msg_record",
    "dispose_db",
    "ensure_dialog",
    "get_recent_history",
    "init_db",
    "is_operator_led",
    "log_event",
    "mark_operator_led",
]


# mypy/ruff: update импортирован для будущего использования; явно оставляем как no-op.
_ = update
