"""Хранилище токенов B24-приложения, полученных при установке.

Локальное приложение Bitrix24 (тип «серверное, использует только API»)
**не показывает** access_token / application_token в UI. Они приходят
в payload события `ONAPPINSTALL` при первой установке (или «Переустановить»):

    auth: {
      access_token,    # ~1 час жизни
      refresh_token,   # ~1 месяц, для рефреша
      expires_in,      # обычно 3600
      domain,          # ваш-портал.bitrix24.ru
      member_id,
      application_token, # стабильный, для аутентификации событий
    }

Эта же payload может приходить в `ONAPPUPDATE` при обновлении прав.
Мы их сохраняем в `data/b24_install.json` (chmod 0600 в проде через systemd
ProtectSystem/ReadWritePaths) и читаем дальше из всех скриптов и хэндлеров.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.config import get_settings

logger = logging.getLogger("avito_bot.b24_install")


@dataclass
class InstallAuth:
    domain: str = ""
    access_token: str = ""
    refresh_token: str = ""
    application_token: str = ""
    expires_in: int = 0
    obtained_at: int = 0  # unix ts
    member_id: str = ""

    @property
    def access_token_expired(self) -> bool:
        if not self.obtained_at or not self.expires_in:
            return True
        # Считаем "почти истёк" за 60 секунд до конца — рефрешим заранее.
        return time.time() >= (self.obtained_at + self.expires_in - 60)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _store_path() -> Path:
    s = get_settings()
    db_dir = Path(s.DB_PATH).parent
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "b24_install.json"


def save_install(auth: dict[str, Any]) -> InstallAuth:
    """Сохранить пришедший в ONAPPINSTALL/ONAPPUPDATE auth-блок."""
    record = InstallAuth(
        domain=str(auth.get("domain") or ""),
        access_token=str(auth.get("access_token") or ""),
        refresh_token=str(auth.get("refresh_token") or ""),
        application_token=str(auth.get("application_token") or ""),
        expires_in=int(auth.get("expires_in") or 0),
        obtained_at=int(time.time()),
        member_id=str(auth.get("member_id") or ""),
    )
    path = _store_path()
    path.write_text(json.dumps(record.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    logger.info(
        "B24 install saved: domain=%s, has_app_token=%s, has_access=%s, has_refresh=%s",
        record.domain,
        bool(record.application_token),
        bool(record.access_token),
        bool(record.refresh_token),
    )
    return record


def load_install() -> InstallAuth | None:
    """Прочитать сохранённый install. None если файла нет или он битый."""
    path = _store_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("install file unreadable: %s", e)
        return None
    return InstallAuth(
        domain=str(data.get("domain") or ""),
        access_token=str(data.get("access_token") or ""),
        refresh_token=str(data.get("refresh_token") or ""),
        application_token=str(data.get("application_token") or ""),
        expires_in=int(data.get("expires_in") or 0),
        obtained_at=int(data.get("obtained_at") or 0),
        member_id=str(data.get("member_id") or ""),
    )


def update_access_token(access_token: str, expires_in: int, refresh_token: str | None = None) -> None:
    """Перезаписать только access_token (после рефреша)."""
    cur = load_install()
    if cur is None:
        return
    cur.access_token = access_token
    cur.expires_in = int(expires_in)
    cur.obtained_at = int(time.time())
    if refresh_token:
        cur.refresh_token = refresh_token
    path = _store_path()
    path.write_text(json.dumps(cur.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def get_application_token() -> str:
    """Текущий application_token — из install-файла, иначе из .env (fallback).

    Используется в `app/main.py` для аутентификации входящих B24-вебхуков.
    """
    inst = load_install()
    if inst and inst.application_token:
        return inst.application_token
    s = get_settings()
    return s.B24_APPLICATION_TOKEN or ""
