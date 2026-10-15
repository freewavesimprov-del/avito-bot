"""Тесты install-store: save/load + автозахват из ONAPPINSTALL + рефреш access_token."""

from __future__ import annotations

import os
import time

import httpx
import pytest

from app.services.b24_install import (
    InstallAuth,
    get_application_token,
    load_install,
    save_install,
    update_access_token,
)


@pytest.fixture(autouse=True)
def _isolate_db_dir(tmp_path, monkeypatch):
    """Каждый тест — свой data/.. через DB_PATH."""
    db = tmp_path / "bot.db"
    monkeypatch.setenv("DB_PATH", str(db))
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_save_then_load_roundtrip():
    rec = save_install(
        {
            "domain": "company.bitrix24.ru",
            "access_token": "AT123",
            "refresh_token": "RT456",
            "application_token": "APP789",
            "expires_in": 3600,
            "member_id": "mem1",
        }
    )
    assert rec.application_token == "APP789"
    loaded = load_install()
    assert loaded is not None
    assert loaded.access_token == "AT123"
    assert loaded.refresh_token == "RT456"
    assert loaded.application_token == "APP789"
    assert loaded.domain == "company.bitrix24.ru"


def test_load_returns_none_when_no_file():
    assert load_install() is None


def test_application_token_fallback_to_env(monkeypatch):
    # Файла нет, но в env есть
    monkeypatch.setenv("B24_APPLICATION_TOKEN", "from-env")
    from app.config import get_settings

    get_settings.cache_clear()
    assert get_application_token() == "from-env"


def test_application_token_prefers_install_over_env(monkeypatch):
    save_install({"domain": "x", "application_token": "from-file"})
    monkeypatch.setenv("B24_APPLICATION_TOKEN", "from-env")
    from app.config import get_settings

    get_settings.cache_clear()
    assert get_application_token() == "from-file"


def test_access_token_expired_logic():
    rec = InstallAuth(
        access_token="x",
        expires_in=3600,
        obtained_at=int(time.time()) - 4000,  # давно истёк
    )
    assert rec.access_token_expired is True

    fresh = InstallAuth(
        access_token="x",
        expires_in=3600,
        obtained_at=int(time.time()),
    )
    assert fresh.access_token_expired is False


def test_update_access_token_overwrites_only_token():
    save_install(
        {
            "domain": "company.bitrix24.ru",
            "access_token": "old",
            "refresh_token": "RT",
            "application_token": "APP",
            "expires_in": 3600,
        }
    )
    update_access_token("new", expires_in=7200)
    cur = load_install()
    assert cur is not None
    assert cur.access_token == "new"
    assert cur.expires_in == 7200
    # application_token и refresh_token не тронуты
    assert cur.application_token == "APP"
    assert cur.refresh_token == "RT"


# --- ONAPPINSTALL: webhook без проверки токена на первой установке -----------


@pytest.mark.asyncio
async def test_onappinstall_captures_tokens_via_webhook(client):
    """ONAPPINSTALL должен пройти 401-проверку (т.к. токенов ещё нет) и сохранить их."""
    evt = {
        "event": "ONAPPINSTALL",
        "auth": {
            "access_token": "FRESH_ACCESS",
            "refresh_token": "FRESH_REFRESH",
            "application_token": "FRESH_APP_TOKEN",
            "expires_in": 3600,
            "domain": "company.bitrix24.ru",
            "member_id": "abc",
        },
        "data": {"VERSION": "1"},
    }
    r = await client.post("/b24/webhook", json=evt)
    assert r.status_code == 200, r.text

    inst = load_install()
    assert inst is not None
    assert inst.application_token == "FRESH_APP_TOKEN"
    assert inst.access_token == "FRESH_ACCESS"
    assert inst.refresh_token == "FRESH_REFRESH"


@pytest.mark.asyncio
async def test_subsequent_event_validates_against_install_token(client):
    """После ONAPPINSTALL обычное событие с правильным app_token из install проходит,
    с неправильным — 401."""
    install_evt = {
        "event": "ONAPPINSTALL",
        "auth": {
            "access_token": "A",
            "refresh_token": "R",
            "application_token": "STORED_TOKEN",
            "expires_in": 3600,
            "domain": "company.bitrix24.ru",
        },
        "data": {},
    }
    await client.post("/b24/webhook", json=install_evt)

    # Правильный токен
    good = {
        "event": "ONIMBOTJOINCHAT",
        "auth": {"application_token": "STORED_TOKEN", "domain": "company.bitrix24.ru"},
        "data": {"PARAMS": {"DIALOG_ID": "x"}},
    }
    assert (await client.post("/b24/webhook", json=good)).status_code == 200

    # Неправильный токен
    bad = dict(good)
    bad["auth"] = {"application_token": "WRONG", "domain": "x"}
    assert (await client.post("/b24/webhook", json=bad)).status_code == 401


# --- refresh_access_token (моки httpx) ---------------------------------------


@pytest.mark.asyncio
async def test_refresh_access_token_happy_path(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "access_token": "NEW_AT",
                "expires_in": 3600,
                "refresh_token": "NEW_RT",
                "scope": "im,imbot,imopenlines,crm",
            },
        )

    import app.services.bitrix as bx

    class _PatchedClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(bx.httpx, "AsyncClient", _PatchedClient)

    res = await bx.refresh_access_token("OLD_RT", "client_id_x", "client_secret_x")
    assert res["access_token"] == "NEW_AT"
    assert res["refresh_token"] == "NEW_RT"
    assert "client_id=client_id_x" in captured["url"]
    assert "grant_type=refresh_token" in captured["url"]


@pytest.mark.asyncio
async def test_client_from_install_uses_existing_when_fresh(monkeypatch):
    save_install(
        {
            "domain": "company.bitrix24.ru",
            "access_token": "GOOD",
            "refresh_token": "RT",
            "application_token": "APP",
            "expires_in": 3600,
        }
    )
    # access_token свеж — рефреш НЕ должен вызываться
    import app.services.bitrix as bx

    async def _boom(*a, **kw):
        raise AssertionError("refresh не должен был вызываться")

    monkeypatch.setattr(bx, "refresh_access_token", _boom)

    client = await bx.client_from_install("cid", "csec")
    assert client._creds.access_token == "GOOD"
    assert client._creds.domain == "company.bitrix24.ru"


@pytest.mark.asyncio
async def test_client_from_install_refreshes_when_expired(monkeypatch):
    # Старый access_token
    save_install(
        {
            "domain": "company.bitrix24.ru",
            "access_token": "EXPIRED",
            "refresh_token": "RT_OLD",
            "application_token": "APP",
            "expires_in": 3600,
        }
    )
    # Принудительно делаем его «истёкшим»
    cur = load_install()
    assert cur is not None
    cur.obtained_at = 1
    update_access_token("EXPIRED", expires_in=3600)
    # Костыль: update_access_token обновит obtained_at в now, поэтому хитро —
    # перезапишем напрямую через save_install с истёкшим временем.
    import json
    from pathlib import Path

    p = Path(os.environ["DB_PATH"]).parent / "b24_install.json"
    raw = json.loads(p.read_text())
    raw["obtained_at"] = 1
    p.write_text(json.dumps(raw))

    import app.services.bitrix as bx

    called = {"refresh": 0}

    async def _fake_refresh(rt, cid, csec):
        called["refresh"] += 1
        assert rt == "RT_OLD"
        return {"access_token": "REFRESHED", "expires_in": 3600, "refresh_token": "RT_NEW"}

    monkeypatch.setattr(bx, "refresh_access_token", _fake_refresh)

    client = await bx.client_from_install("cid", "csec")
    assert called["refresh"] == 1
    assert client._creds.access_token == "REFRESHED"
    # И в store обновилось
    upd = load_install()
    assert upd is not None
    assert upd.access_token == "REFRESHED"
    assert upd.refresh_token == "RT_NEW"
