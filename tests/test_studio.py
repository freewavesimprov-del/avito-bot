"""Import and publication boundaries; playground must never deliver messages."""

from __future__ import annotations

import asyncio
import io
import json
import re
import zipfile

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook

from app.services.avito_import import choose_items, read_export
from app.services.catalog_parser import CatalogParseError

AUTH = ("editor", "local-test-password")
HEADERS = ["Номер объявления на Авито", "Название объявления", "Цена", "Ширина", "Глубина", "Высота", "Цвет от производителя", "Подвид товара", "AvitoStatus", "Адрес", "Номер телефона", "Описание объявления"]


def workbook(rows=None, broken_dimension=False):
    book = Workbook()
    sheet = book.active
    sheet.title = "Комоды"
    sheet.append(["Категория"])
    sheet.append(HEADERS)
    sheet.append(["Необязательный"])
    sheet.append(["Подробнее о параметре"])
    for row in rows or [
        ["11111111", "Комод Норд кашемир", 15550, 160, 45, 90, "Кашемир", "Комоды", "Активно", "Город А", "SECRET_CONTACT", "<p>Комод с дверями</p>"],
        ["22222222", "Комод с ящиками Норд кашемир", 15550, 160, 45, 90, "Кашемир", "Комоды", "Активно", "Город Б", "SECRET_CONTACT", "<p>Другой текст</p>"],
        ["33333333", "Комод Норд графит", 14800, 160, 45, 90, "Графит", "Комоды", "Активно"],
        ["44444444", "Комод Норд графит 120", 12250, 120, 45, 90, "Графит", "Комоды", "Активно"],
    ]:
        sheet.append(row)
    refs = book.create_sheet("Спр-Комоды")
    refs.append(["Не объявления"])
    data = io.BytesIO()
    book.save(data)
    if not broken_dimension:
        return data.getvalue()
    out = io.BytesIO()
    with zipfile.ZipFile(data) as source, zipfile.ZipFile(out, "w") as target:
        for item in source.infolist():
            raw = source.read(item.filename)
            if item.filename.startswith("xl/worksheets/"):
                raw = re.sub(rb'<dimension ref="[^"]+"', b'<dimension ref="A1"', raw)
            target.writestr(item, raw)
    return out.getvalue()


def test_avito_dimensions_geo_and_contact_exclusion():
    catalog = read_export(workbook(broken_dimension=True), "export.xlsx")
    assert len(catalog["items"]) == 4
    first, second, other, smaller = catalog["items"]
    assert first["product_key"] == second["product_key"]
    assert first["product_key"] != other["product_key"] != smaller["product_key"]
    assert first["dimensions_cm"] == {"width": 160, "depth": 45, "height": 90}
    assert first["stock"] is None
    assert "SECRET_CONTACT" not in json.dumps(catalog)
    assert len(choose_items(catalog, "Норд")) == 3
    chosen = choose_items(catalog, "другой цвет", "22222222")
    assert chosen[0]["ad_id"] == "22222222"


def test_avito_rejects_bad_rows_and_duplicate_ids():
    with pytest.raises(CatalogParseError, match="цену"):
        read_export(workbook([["123456", "Комод", -2, 100, 40, 80]]), "x.xlsx")
    with pytest.raises(CatalogParseError, match="Повтор"):
        read_export(workbook([["123456", "Комод", 100], ["123456", "Комод", 200]]), "x.xlsx")
    with pytest.raises(CatalogParseError):
        read_export(b"not a workbook", "x.xlsx")


def test_avito_skips_inactive_and_keeps_unknown_stock():
    catalog = read_export(workbook([
        ["111", "Комод", 100, 100, 40, 80, "Белый", "Комоды", "Активно"],
        ["222", "Комод", 100, 100, 40, 80, "Белый", "Комоды", "Снято с публикации"],
    ]), "x.xlsx")
    assert [i["ad_id"] for i in catalog["items"]] == ["111"]


@pytest_asyncio.fixture
async def studio_client(tmp_path, monkeypatch):
    from app.config import get_settings
    from app.services import studio
    from app.services.prompt_builder import reset_cache

    monkeypatch.setenv("ADMIN_USER", AUTH[0])
    monkeypatch.setenv("ADMIN_PASSWORD", AUTH[1])
    monkeypatch.setenv("CATALOG_PATH", str(tmp_path / "catalog.json"))
    monkeypatch.setenv("YC_API_KEY", "test-key")
    monkeypatch.setenv("YC_FOLDER_ID", "test-folder")
    get_settings.cache_clear()
    studio.atomic_json(studio.catalog_path(), {"updated": "2026-10-01", "items": [{"article": "old", "name": "Старый товар", "price": 100}]})
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", auth=AUTH) as client:
        yield client
    get_settings.cache_clear()
    reset_cache()


async def preview(client):
    response = await client.post("/admin/studio/catalog/preview", files={"file": ("export.xlsx", workbook())})
    assert response.status_code == 200, response.text
    return response.json()["draft_id"]


@pytest.mark.asyncio
async def test_preview_apply_conflict_and_restore(studio_client):
    from app.services import studio

    client = studio_client
    old = studio.catalog_path().read_bytes()
    token = await preview(client)
    assert studio.catalog_path().read_bytes() == old
    second = await preview(client)
    response = await client.post("/admin/studio/catalog/apply", json={"draft_id": token})
    assert response.status_code == 200, response.text
    assert len(json.loads(studio.catalog_path().read_text())["items"]) == 4
    assert (await client.post("/admin/studio/catalog/apply", json={"draft_id": second})).status_code == 409
    versions = (await client.get("/admin/studio/catalog/versions")).json()["versions"]
    state = (await client.get("/admin/studio/state")).json()
    restored = await client.post("/admin/studio/catalog/restore", json={"version": versions[0]["id"], "baseline": state["baseline"]})
    assert restored.status_code == 200
    assert studio.read_json(studio.catalog_path())["items"][0]["name"] == "Старый товар"


@pytest.mark.asyncio
async def test_behavior_draft_publish_restore_and_conflict(studio_client):
    from app.services.studio import active_behavior

    client = studio_client
    state = (await client.get("/admin/studio/state")).json()["behavior"]
    default = state["active"]["faq"]
    behavior = state["draft"]
    behavior["faq"] = "УНИКАЛЬНЫЙ НОВЫЙ СПРАВОЧНИК"
    behavior["scenarios"] = [{"id": "price", "name": "Цена", "condition": "Спрашивает цену", "steps": ["Назови цену и размеры", "Спроси, подходят ли размеры"], "enabled": True}]
    saved = await client.post("/admin/studio/behavior", json={"action": "save", "revision": state["revision"], "behavior": behavior})
    assert saved.status_code == 200, saved.text
    assert active_behavior() is None
    # Merely saving a draft must not change the prompt used in production.
    from app.services.prompt_builder import build_system_prompt

    assert "УНИКАЛЬНЫЙ НОВЫЙ СПРАВОЧНИК" not in build_system_prompt().system
    stale = await client.post("/admin/studio/behavior", json={"action": "publish", "revision": state["revision"]})
    assert stale.status_code == 409
    published = (await client.post("/admin/studio/behavior", json={"action": "publish", "revision": saved.json()["revision"]})).json()
    assert active_behavior()["faq"] == behavior["faq"]
    restored = (await client.post("/admin/studio/behavior", json={"action": "restore", "revision": published["revision"], "version": published["history"][0]["id"]})).json()
    assert restored["draft"]["faq"] == default
    assert active_behavior()["faq"] == behavior["faq"]


@pytest.mark.asyncio
async def test_playground_uses_draft_selected_ad_without_logging_or_delivery(studio_client, monkeypatch):
    from app.services import chat_engine

    client = studio_client
    token = await preview(client)
    state = (await client.get("/admin/studio/state")).json()["behavior"]
    state["draft"]["faq"] = "DRAFT_ONLY_MARKER"
    await client.post("/admin/studio/behavior", json={"action": "save", "revision": state["revision"], "behavior": state["draft"]})
    calls = []

    async def fake_generate(**kwargs):
        calls.append(kwargs)
        return "Комод стоит 15 550 ₽. Подходят размеры 160 × 45 × 90 см?"

    async def forbidden(*args, **kwargs):
        pytest.fail("Playground must not touch dialog storage")

    monkeypatch.setattr(chat_engine, "generate", fake_generate)
    monkeypatch.setattr(chat_engine, "log_event", forbidden)
    response = await client.post("/admin/studio/playground", json={"message": "Цена актуальная?", "mode": "live", "catalog_draft": token, "ad_id": "22222222"})
    assert response.status_code == 200, response.text
    assert response.json()["action"] == "sent"
    prompt = calls[0]["system_prompt"]
    assert "DRAFT_ONLY_MARKER" in prompt
    assert '"ad_id": "22222222"' in prompt and '"selected": true' in prompt
    assert '"width": 160.0' in prompt
    assert "SECRET_CONTACT" not in prompt
    assert chat_engine.get_settings().MOCK_LLM is True


@pytest.mark.asyncio
async def test_playground_context_is_request_local(studio_client, monkeypatch):
    from app.services import chat_engine, studio

    async def fake_generate(**kwargs):
        await asyncio.sleep(0)
        return "Цена из каталога"

    monkeypatch.setattr(chat_engine, "generate", fake_generate)
    responses = await asyncio.gather(
        studio_client.post("/admin/studio/playground", json={"message": "Цена?", "mode": "live"}),
        studio_client.post("/admin/studio/playground", json={"message": "Цена?", "mode": "mock"}),
    )
    assert responses[0].json()["text"] == "Цена из каталога"
    assert "MOCK" in responses[1].json()["text"]
    assert studio.preview_context.get() is None


@pytest.mark.asyncio
async def test_playground_filters_and_hard_triggers(studio_client, monkeypatch):
    from app.services import chat_engine

    async def fake_generate(**kwargs):
        return "Телефон +7 495 123-45-67"

    monkeypatch.setattr(chat_engine, "generate", fake_generate)
    response = await studio_client.post("/admin/studio/playground", json={"message": "Какие размеры?", "mode": "live"})
    assert response.json()["action"] == "blocked"
    assert response.json()["reason"] == "phone"
    response = await studio_client.post("/admin/studio/playground", json={"message": "Хочу вернуть товар", "mode": "live"})
    assert response.json()["action"] == "escalated"
    assert response.json()["model_called"] is False


@pytest.mark.asyncio
async def test_auth_csrf_invalid_file_and_missing_model(studio_client, monkeypatch):
    client = studio_client
    assert (await client.get("/admin/studio/state", auth=("wrong", "wrong"))).status_code == 401
    assert (await client.post("/admin/studio/playground", json={"message": "Hi"}, headers={"Origin": "https://other.example"})).status_code == 403
    response = await client.post("/admin/studio/catalog/preview", files={"file": ("invalid.xlsx", b"broken")})
    assert response.status_code == 400
    from app.config import get_settings

    monkeypatch.setenv("YC_API_KEY", "")
    get_settings.cache_clear()
    assert (await client.post("/admin/studio/playground", json={"message": "Hi", "mode": "live"})).status_code == 503


@pytest.mark.asyncio
async def test_html_is_available_in_production_and_has_editors(studio_client):
    response = await studio_client.get("/admin/catalog")
    assert response.status_code == 200
    for value in ("Playground", "Сценарии разговора", "Шаблоны ответов", "Применить каталог", "Новый разговор"):
        assert value in response.text


def test_published_catalog_and_behavior_reach_live_prompt(tmp_path, monkeypatch):
    from app.config import get_settings
    from app.services import studio
    from app.services.prompt_builder import reply_prompt

    monkeypatch.setenv("CATALOG_PATH", str(tmp_path / "catalog.json"))
    get_settings.cache_clear()
    studio.atomic_json(studio.catalog_path(), read_export(workbook(), "test.xlsx"))
    state = studio.behavior_state()
    behavior = state["draft"]
    behavior["faq"] = "PUBLISHED_CONDITIONS"
    behavior["templates"] = [{"id": "hello", "name": "Приветствие", "text": "PUBLISHED_TEMPLATE", "enabled": True}]
    state = studio.mutate_behavior(state["revision"], "save", behavior)
    studio.mutate_behavior(state["revision"], "publish")
    prompt = reply_prompt("Другой цвет?", [], "Объявление №22222222")
    assert "PUBLISHED_CONDITIONS" in prompt and "PUBLISHED_TEMPLATE" in prompt
    assert '"selected": true' in prompt
    assert "SECRET_CONTACT" not in prompt
    get_settings.cache_clear()
