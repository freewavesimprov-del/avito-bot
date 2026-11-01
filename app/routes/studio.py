"""Authenticated catalog drafts, conversation editor and delivery-free playground."""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, SecretStr
from starlette.concurrency import run_in_threadpool

from app.routes.admin import _verify_basic
from app.services import studio as store
from app.services.avito_import import MAX_UPLOAD, choose_items, read_export, summarize
from app.services.catalog_parser import (
    CatalogParseError,
    build_diff,
    load_manual,
    merge_main_and_manual,
    parse_excel,
)
from app.services.prompt_builder import compose_prompt, reset_cache
from app.services.yandex_connection import Connection, connection, metadata, save_connection

router = APIRouter(prefix="/admin/studio", tags=["admin"])
User = Annotated[str, Depends(_verify_basic)]


def current_catalog() -> dict:
    return store.read_json(store.catalog_path(), {"items": []})


def effective_catalog(catalog: dict) -> dict:
    if catalog.get("format") == "avito":
        return catalog
    return merge_main_and_manual(catalog, load_manual(store.catalog_path()))


def draft_path(draft_id: str) -> Path:
    return store.store_root() / "catalog_drafts" / (store.identifier(draft_id) + ".json")


def get_draft(draft_id: str, user: str) -> dict:
    try:
        draft = store.read_json(draft_path(draft_id))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not draft or draft["owner"] != user:
        raise HTTPException(404, "Черновик каталога не найден")
    return draft


def parse_upload(raw: bytes, filename: str) -> dict:
    try:
        catalog = read_export(raw, filename)
        if catalog is not None:
            return catalog
        with tempfile.TemporaryDirectory(prefix="avito-import-") as directory:
            path = Path(directory) / "upload.xlsx"
            path.write_bytes(raw)
            return parse_excel(path, source_label=f"1С: {filename}")
    except CatalogParseError:
        raise
    except Exception as exc:
        raise CatalogParseError("Файл повреждён или имеет неподдерживаемую структуру Excel") from exc


@router.post("/catalog/preview")
async def catalog_preview(user: User, file: Annotated[UploadFile, File()]) -> dict:
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Выберите Excel .xlsx или .xlsm")
    raw = await file.read(MAX_UPLOAD + 1)
    if not raw or len(raw) > MAX_UPLOAD:
        raise HTTPException(400, "Файл пуст или больше 16 МиБ")
    baseline = store.fingerprint(store.catalog_path())
    try:
        catalog = await run_in_threadpool(parse_upload, raw, Path(file.filename).name)
    except (CatalogParseError, ValueError, KeyError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    token = uuid.uuid4().hex
    with store.locked():
        drafts = store.store_root() / "catalog_drafts"
        drafts.mkdir(parents=True, exist_ok=True)
        for old in sorted(drafts.glob("*.json"), key=lambda p: p.stat().st_mtime)[:-19]:
            old.unlink()
        store.atomic_json(draft_path(token), {"owner": user, "catalog": catalog, "baseline": baseline})
    diff = build_diff(current_catalog(), catalog)
    return {"draft_id": token, **summarize(catalog), "diff": {
        "added": len(diff["added"]), "removed": len(diff["removed"]), "changed": len(diff["changed"]),
        "samples": {"added": [i["name"] for i in diff["added"][:8]], "removed": [i["name"] for i in diff["removed"][:8]], "changed": diff["changed"][:5]},
    }}


class CatalogAction(BaseModel):
    draft_id: str


def backup_catalog() -> str | None:
    if not store.catalog_path().exists():
        return None
    token = uuid.uuid4().hex
    store.atomic_json(store.store_root() / "catalog_versions" / f"{token}.json", current_catalog())
    versions = sorted((store.store_root() / "catalog_versions").glob("*.json"), key=lambda p: p.stat().st_mtime)
    for old in versions[:-30]:
        old.unlink()
    return token


@router.post("/catalog/apply")
def catalog_apply(body: CatalogAction, user: User) -> dict:
    with store.locked():
        draft = get_draft(body.draft_id, user)
        if store.fingerprint(store.catalog_path()) != draft["baseline"]:
            raise HTTPException(409, "Каталог изменился после предпросмотра. Загрузите файл заново.")
        # Render before writing so an invalid draft cannot replace working data.
        compose_prompt(effective_catalog(draft["catalog"]), store.active_behavior())
        backup = backup_catalog()
        store.atomic_json(store.catalog_path(), draft["catalog"])
        draft_path(body.draft_id).unlink()
        reset_cache()
    return {"status": "applied", "backup_id": backup, **summarize(draft["catalog"])}


@router.get("/catalog/versions")
def catalog_versions(user: User) -> dict:
    paths = sorted((store.store_root() / "catalog_versions").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {"versions": [{"id": p.stem, "source": store.read_json(p).get("source"), "saved_at": p.stat().st_mtime} for p in paths[:30]]}


class RestoreCatalog(BaseModel):
    version: str
    baseline: str


@router.post("/catalog/restore")
def catalog_restore(body: RestoreCatalog, user: User) -> dict:
    try:
        path = store.store_root() / "catalog_versions" / f"{store.identifier(body.version)}.json"
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with store.locked():
        if store.fingerprint(store.catalog_path()) != body.baseline:
            raise HTTPException(409, "Каталог изменился. Обновите страницу.")
        catalog = store.read_json(path)
        if catalog is None:
            raise HTTPException(404, "Версия не найдена")
        compose_prompt(effective_catalog(catalog), store.active_behavior())
        backup_catalog()
        store.atomic_json(store.catalog_path(), catalog)
        reset_cache()
    return {"status": "restored"}


@router.get("/state")
def studio_state(user: User) -> dict:
    return {"behavior": store.behavior_state(), "catalog": summarize(current_catalog()),
            "baseline": store.fingerprint(store.catalog_path()),
            "yandex": metadata(),
            "baseline_source": "Действующие настройки бота: system_prompt.md, faq_extras.md и фраза передачи из кода. Опубликованные правки имеют приоритет.",
            "filter_notice": "Действуют общие фильтры: контакты, внешние ссылки, скидки и точные даты. Редактор их не отключает."}


class BehaviorAction(BaseModel):
    revision: str
    action: Literal["save", "publish", "restore", "baseline"]
    behavior: store.Behavior | None = None
    version: str = ""


@router.post("/behavior")
def behavior_action(body: BehaviorAction, user: User) -> dict:
    if body.action == "save" and body.behavior is None:
        raise HTTPException(400, "Нужен текст настроек")
    try:
        state = store.mutate_behavior(body.revision, body.action, body.behavior.model_dump() if body.behavior else None, body.version)
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    reset_cache()
    return state


class HistoryMessage(BaseModel):
    direction: Literal["in", "out"]
    message: str = Field(max_length=4000)


class PlaygroundMessage(BaseModel):
    model_config = {"extra": "forbid"}
    message: str = Field(min_length=1, max_length=4000)
    history: list[HistoryMessage] = Field(default_factory=list, max_length=30)
    source: Literal["draft", "active"] = "draft"
    catalog_draft: str = ""
    ad_id: str = Field(default="", max_length=30)


@router.get("/products")
def products(user: User, query: str = "", draft_id: str = "") -> dict:
    catalog = get_draft(draft_id, user)["catalog"] if draft_id else current_catalog()
    items = choose_items(catalog, query) if query.strip() else catalog.get("items", [])[:35]
    return {"items": [{k: i.get(k) for k in ("ad_id", "name", "price", "color", "dimensions_cm", "location")} for i in items]}


@router.post("/playground")
async def playground(body: PlaygroundMessage, user: User) -> dict:
    from app.services import chat_engine
    from app.services.filters import ESCALATION_CLIENT_MESSAGE
    from app.services.triggers import check_hard_triggers

    if not body.message.strip():
        raise HTTPException(400, "Введите сообщение")
    if not metadata()["configured"]:
        raise HTTPException(503, "Подключите YandexGPT в форме на этой странице.")
    if body.source == "active" and body.catalog_draft:
        raise HTTPException(400, "Черновик каталога доступен только в режиме черновика")
    state = store.behavior_state()
    behavior = state[body.source]
    catalog = get_draft(body.catalog_draft, user)["catalog"] if body.catalog_draft else current_catalog()
    if body.ad_id and not any(str(i.get("ad_id", "")) == body.ad_id for i in catalog.get("items", [])):
        raise HTTPException(400, "Объявление не найдено в выбранном каталоге")
    history = [m.model_dump() for m in body.history]
    query = " ".join(h["message"] for h in history[-6:] if h["direction"] == "in") + " " + body.message
    prompt = compose_prompt(effective_catalog(catalog), behavior, query, body.ad_id)
    hit = check_hard_triggers(body.message)
    repeats = chat_engine._count_repeats([*history, {"direction": "in", "message": body.message}], body.message)
    if hit or repeats >= chat_engine.REPEAT_THRESHOLD:
        return {"text": ESCALATION_CLIENT_MESSAGE, "action": "escalated", "reason": hit.reason if hit else "repeat_loop", "model_called": False}
    token = store.preview_context.set(store.PreviewContext(prompt=prompt.system))
    try:
        raw = await chat_engine._generate(body.message, history)
    except chat_engine.LLMError as exc:
        raise HTTPException(502, "YandexGPT не ответил. Проверьте соединение, ключ, каталог и доступ к модели.") from exc
    finally:
        store.preview_context.reset(token)
    cleaned, reason, blocked = chat_engine.interpret_response(raw)
    return {"text": ESCALATION_CLIENT_MESSAGE if blocked else cleaned,
            "action": "blocked" if blocked else "escalated" if reason else "sent",
            "reason": blocked or reason, "blocked_text": cleaned if blocked else None,
            "model_called": True,
            "source": body.source, "catalog": summarize(catalog)}


class YandexForm(BaseModel):
    api_key: SecretStr | None = None
    folder_id: str = Field(max_length=100)
    model: str = Field(default="yandexgpt-lite", min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


def candidate_connection(body: YandexForm) -> Connection:
    current = connection()
    entered = body.api_key.get_secret_value().strip() if body.api_key else ""
    if entered and any(ord(c) < 33 or ord(c) > 126 for c in entered):
        raise HTTPException(400, "Проверьте API-ключ: он не должен содержать пробелы или переносы строк")
    key = SecretStr(entered) if entered else current.api_key
    if not key.get_secret_value() or not body.folder_id.strip():
        raise HTTPException(400, "Нужны API-ключ и ID каталога Yandex Cloud")
    return Connection(api_key=key, folder_id=body.folder_id.strip(), model=body.model, timeout=current.timeout)


@router.post("/yandex/save")
def yandex_save(body: YandexForm, user: User) -> dict:
    value = candidate_connection(body)
    save_connection(value, bool(body.api_key and body.api_key.get_secret_value().strip()))
    return metadata()


@router.post("/yandex/test")
async def yandex_test(body: YandexForm, user: User) -> dict:
    from app.services.llm import ChatMessage, LLMError, generate

    value = candidate_connection(body)
    try:
        answer = await generate(system_prompt="Ответь одним словом: ОК.",
                                user_messages=[ChatMessage(role="user", text="Проверка соединения")],
                                connection_override=value, max_tokens=10, max_retries=0)
        if not answer.strip():
            raise LLMError("empty response")
    except LLMError as exc:
        raise HTTPException(502, "Нет ответа YandexGPT. Проверьте права ключа, ID каталога, модель и сетевой доступ.") from exc
    return {"status": "ok", "message": "YandexGPT ответил. Проверенные параметры ещё нужно сохранить."}


@router.get("", response_class=HTMLResponse)
def studio_html(user: User):
    return HTMLResponse((Path(__file__).resolve().parent.parent / "static" / "studio.html").read_text())
