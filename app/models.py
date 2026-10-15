"""Pydantic-модели для событий Bitrix24 bot-platform.

Все модели с extra="allow", чтобы пережить новые поля от Битрикса без падения.
Поля — в UPPER_CASE там, где их так шлёт B24 (это документированный контракт).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class B24Auth(BaseModel):
    """Блок auth из тела события. application_token — ключевой для аутентификации вебхука."""

    model_config = ConfigDict(extra="allow")

    access_token: str | None = None
    domain: str | None = None
    application_token: str | None = None
    refresh_token: str | None = None
    expires_in: int | None = None
    scope: str | None = None
    user_id: int | None = None


class B24EventBase(BaseModel):
    """Обёртка любого события B24. data парсим нестрого — конкретный payload отдельно."""

    model_config = ConfigDict(extra="allow")

    event: str = Field(..., description="Напр. ONIMBOTMESSAGEADD")
    ts: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    auth: B24Auth = Field(default_factory=B24Auth)


# --- Payload-ы конкретных событий ---


class B24User(BaseModel):
    model_config = ConfigDict(extra="allow")

    ID: int | str | None = None
    NAME: str | None = None
    LAST_NAME: str | None = None
    IS_EXTRANET: bool | Literal["Y", "N"] | None = None


class MessageAddParams(BaseModel):
    model_config = ConfigDict(extra="allow")

    MESSAGE_ID: int | str | None = None
    MESSAGE: str = ""
    DIALOG_ID: str | None = None
    CHAT_ID: int | str | None = None
    FROM_USER_ID: int | str | None = None
    TO_USER_ID: int | str | None = None
    BOT_ID: int | str | None = None


class MessageAddPayload(BaseModel):
    """PARAMS+USER для ONIMBOTMESSAGEADD."""

    model_config = ConfigDict(extra="allow")

    PARAMS: MessageAddParams = Field(default_factory=MessageAddParams)
    USER: B24User = Field(default_factory=B24User)


class BotJoinPayload(BaseModel):
    """ONIMBOTJOINCHAT — бот добавлен в чат."""

    model_config = ConfigDict(extra="allow")

    PARAMS: dict[str, Any] = Field(default_factory=dict)
    USER: B24User = Field(default_factory=B24User)


class BotDeletePayload(BaseModel):
    """ONIMBOTDELETE — бота удалили из чата (обычно оператором)."""

    model_config = ConfigDict(extra="allow")

    BOT_ID: int | str | None = None
    DIALOG_ID: str | None = None


class AppInstallPayload(BaseModel):
    """ONAPPINSTALL — установка приложения в B24. application_token здесь же в auth."""

    model_config = ConfigDict(extra="allow")

    VERSION: str | None = None
    LANGUAGE_ID: str | None = None


def extract_dialog_id(event: B24EventBase) -> str | None:
    """Унифицированная извлекалка DIALOG_ID из data разных событий."""
    data = event.data or {}
    # ONIMBOTMESSAGEADD / JOIN: data.PARAMS.DIALOG_ID
    params = data.get("PARAMS")
    if isinstance(params, dict):
        dlg = params.get("DIALOG_ID")
        if dlg:
            return str(dlg)
    # ONIMBOTDELETE: data.DIALOG_ID (верхний уровень)
    dlg = data.get("DIALOG_ID")
    if dlg:
        return str(dlg)
    return None
