"""E2E-тесты админ-эндпоинтов /admin/catalog/*."""

from __future__ import annotations

import io
import json
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook


@pytest_asyncio.fixture
async def admin_client(tmp_path, monkeypatch) -> AsyncIterator[AsyncClient]:
    """Чистая БД, изолированный CWD под data/, ADMIN_USER/ADMIN_PASSWORD заданы."""
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret123")
    monkeypatch.setenv("MOCK_LLM", "true")

    # Изолируем CWD чтобы admin не писал в реальный app/data/pilot_skus.json
    work = tmp_path / "work"
    (work / "app" / "data").mkdir(parents=True)
    # Скопируем реальный pilot_skus.json как baseline
    repo_root = Path(__file__).resolve().parent.parent
    real_catalog = repo_root / "app" / "data" / "pilot_skus.json"
    if real_catalog.exists():
        shutil.copy(real_catalog, work / "app" / "data" / "pilot_skus.json")
    # И system_prompt.md (нужен prompt_builder для reset_cache)
    (work / "app" / "prompts").mkdir(parents=True)
    real_prompt = repo_root / "app" / "prompts" / "system_prompt.md"
    if real_prompt.exists():
        shutil.copy(real_prompt, work / "app" / "prompts" / "system_prompt.md")
    # И faq_extras.md
    real_faq = repo_root / "app" / "data" / "faq_extras.md"
    if real_faq.exists():
        shutil.copy(real_faq, work / "app" / "data" / "faq_extras.md")

    monkeypatch.chdir(work)
    # Указываем явно — иначе prompt_builder возьмёт зашитый _DEFAULT_SKUS_PATH в репо.
    monkeypatch.setenv("CATALOG_PATH", str(work / "app" / "data" / "pilot_skus.json"))

    from app.config import get_settings
    from app.services.prompt_builder import reset_cache

    get_settings.cache_clear()
    reset_cache()

    from app import main as app_module

    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await app_module.init_db()
        try:
            yield ac
        finally:
            await app_module.dispose_db()
    get_settings.cache_clear()
    reset_cache()


def _make_xlsx_bytes(rows: list[tuple]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_info_requires_auth(admin_client):
    r = await admin_client.get("/admin/catalog/info")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_info_with_auth(admin_client):
    r = await admin_client.get("/admin/catalog/info", auth=("admin", "secret123"))
    assert r.status_code == 200
    body = r.json()
    assert body["exists"] is True
    assert body["skus_total"] > 0


@pytest.mark.asyncio
async def test_html_form_requires_auth(admin_client):
    r = await admin_client.get("/admin/catalog")
    assert r.status_code == 401
    r = await admin_client.get("/admin/catalog", auth=("admin", "secret123"))
    assert r.status_code == 200
    assert "Каталог Avito-bot" in r.text
    assert "перетащите" in r.text.lower()


@pytest.mark.asyncio
async def test_upload_full_cycle(admin_client):
    """Загрузка валидного Excel → 200, JSON ответ с diff, файл сохранился."""
    xlsx = _make_xlsx_bytes(
        [
            ("Наименование", "Артикул", "Остаток", "Розничные цены"),
            ("Тестовый шкаф", "T001", 10, 15000),
            ("Тестовый комод", "T002", 5, 9000),
        ]
    )
    files = {"file": ("test.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r = await admin_client.post(
        "/admin/catalog/upload", files=files, auth=("admin", "secret123")
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["new_total"] == 2
    assert "diff" in body
    assert "prompt" in body
    assert body["prompt"]["skus_in_prompt"] == 2

    # Проверим что файл реально записан
    saved = json.loads(Path("app/data/pilot_skus.json").read_text())
    assert len(saved["items"]) == 2
    assert saved["items"][0]["name"] == "Тестовый шкаф"


@pytest.mark.asyncio
async def test_upload_rejects_non_xlsx(admin_client):
    files = {"file": ("test.txt", b"some text", "text/plain")}
    r = await admin_client.post(
        "/admin/catalog/upload", files=files, auth=("admin", "secret123")
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_upload_rejects_invalid_excel(admin_client):
    """Excel без обязательных колонок — 400 с понятной ошибкой."""
    xlsx = _make_xlsx_bytes([("Колонка", "Без", "Нужных", "Полей"), ("A", "B", "C", "D")])
    files = {"file": ("bad.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r = await admin_client.post(
        "/admin/catalog/upload", files=files, auth=("admin", "secret123")
    )
    assert r.status_code == 400
    body = r.json()
    assert body["status"] == "error"
    assert "не найдены" in body["message"] or "не найден" in body["message"]


@pytest.mark.asyncio
async def test_reload_endpoint(admin_client):
    r = await admin_client.post("/admin/catalog/reload", auth=("admin", "secret123"))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "reloaded"
    assert body["skus_in_prompt"] > 0


@pytest.mark.asyncio
async def test_admin_503_when_creds_not_configured(admin_client, monkeypatch):
    """Если в .env пустые логин/пароль — все /admin/* возвращают 503."""
    monkeypatch.setenv("ADMIN_USER", "")
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    from app.config import get_settings

    get_settings.cache_clear()
    r = await admin_client.get("/admin/catalog/info", auth=("admin", "secret123"))
    assert r.status_code == 503

    # Восстанавливаем
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret123")
    get_settings.cache_clear()


# silence import-time linter
_ = os
