"""Админ-панель для Татьяны: обновление каталога через веб-форму.

Эндпоинты (защищены Basic Auth по ADMIN_USER / ADMIN_PASSWORD из .env):
  GET  /admin/catalog              — HTML-страница с формой загрузки + статус
  POST /admin/catalog/upload       — multipart upload Excel (xlsx)
  GET  /admin/catalog/info         — JSON-статус (последнее обновление, кол-во SKU, дни с обновления)
  POST /admin/catalog/reload       — сброс кэша prompt_builder (используется CLI-скриптом)
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
import shutil
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import get_settings
from app.services.catalog_parser import (
    CatalogParseError,
    build_diff,
    days_since_update,
    load_manual,
    parse_excel,
)
from app.services.prompt_builder import build_system_prompt, reset_cache

logger = logging.getLogger("avito_bot.admin")

router = APIRouter(prefix="/admin", tags=["admin"])
_security = HTTPBasic()

BACKUP_DIR = Path("data/catalog_backups")
KEEP_BACKUPS = 30
STALE_DAYS = 7  # после стольких дней показываем красный баннер «каталог устарел»


def _catalog_path() -> Path:
    """Путь к pilot_skus.json — берём из настроек, иначе дефолт."""
    s = get_settings()
    if s.CATALOG_PATH:
        return Path(s.CATALOG_PATH)
    return Path("app/data/pilot_skus.json")


def _verify_basic(request: Request, creds: Annotated[HTTPBasicCredentials, Depends(_security)]) -> str:
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        from urllib.parse import urlsplit

        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (
            origin and urlsplit(origin).netloc != request.headers.get("host")
        ):
            raise HTTPException(403, "Запрос должен быть отправлен со страницы администратора")
    s = get_settings()
    if not s.ADMIN_USER or not s.ADMIN_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ADMIN_USER / ADMIN_PASSWORD не заданы в .env",
        )
    user_ok = secrets.compare_digest(creds.username.encode(), s.ADMIN_USER.encode())
    pass_ok = secrets.compare_digest(creds.password.encode(), s.ADMIN_PASSWORD.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username


def _load_current() -> dict[str, Any] | None:
    if not _catalog_path().exists():
        return None
    try:
        return json.loads(_catalog_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _backup_current() -> Path | None:
    if not _catalog_path().exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    target = BACKUP_DIR / f"pilot_skus_{ts}.json"
    shutil.copy2(_catalog_path(), target)
    backups = sorted(BACKUP_DIR.glob("pilot_skus_*.json"))
    for old in backups[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return target


def _save(catalog: dict[str, Any]) -> None:
    _catalog_path().parent.mkdir(parents=True, exist_ok=True)
    payload = {k: v for k, v in catalog.items() if k != "warnings"}
    from app.services.studio import atomic_json

    atomic_json(_catalog_path(), payload)


# --- Endpoints --------------------------------------------------------------


@router.get("/catalog/info")
def catalog_info(_user: Annotated[str, Depends(_verify_basic)]) -> dict[str, Any]:
    cur = _load_current()
    if cur is None:
        return {
            "exists": False,
            "msg": "Каталог не загружен. Загрузите Excel через /admin/catalog.",
        }
    days = days_since_update(cur)
    items = cur.get("items", [])
    # Manual-слой (ручные дополнения, не из 1С)
    manual = load_manual(_catalog_path())
    manual_items = manual.get("items", [])
    return {
        "exists": True,
        "updated": cur.get("updated"),
        "source": cur.get("source"),
        "skus_total": len(items),
        "out_of_stock": sum(1 for i in items if i.get("stock") == 0),
        "low_stock": sum(1 for i in items if 0 < (i.get("stock") or 0) < 5),
        "categories": _category_counts(items),
        "days_since_update": days,
        "is_stale": (days is not None and days >= STALE_DAYS),
        "stale_threshold_days": STALE_DAYS,
        "manual": {
            "count": len(manual_items),
            "items": [
                {
                    "name": m.get("name"),
                    "price": m.get("price"),
                    "stock": m.get("stock"),
                    "category": m.get("category"),
                }
                for m in manual_items
            ],
        },
    }


@router.post("/catalog/reload")
def catalog_reload(_user: Annotated[str, Depends(_verify_basic)]) -> dict[str, Any]:
    """Сбросить кэш prompt_builder — следующий вызов LLM перечитает pilot_skus.json."""
    reset_cache()
    p = build_system_prompt()  # тут же прогреваем
    return {
        "status": "reloaded",
        "skus_in_prompt": p.skus_count,
        "updated_date": p.updated_date,
    }


@router.post("/catalog/upload")
async def catalog_upload(
    _user: Annotated[str, Depends(_verify_basic)],
    file: Annotated[UploadFile, File(description="Excel-выгрузка из 1С (.xlsx)")],
) -> JSONResponse:
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="Принимается только .xlsx / .xlsm")

    from app.services.avito_import import MAX_UPLOAD

    raw = await file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(400, "Файл больше 16 МиБ")
    if not raw:
        raise HTTPException(status_code=400, detail="Пустой файл")

    from app.services.avito_import import read_export

    try:
        avito = read_export(raw, file.filename)
    except CatalogParseError as exc:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(exc)})
    if avito is not None:
        raise HTTPException(400, "Для выгрузки Авито используйте предпросмотр на странице /admin/catalog")

    # openpyxl умеет читать file-like — пишем во временный путь чтобы parse_excel принял Path
    tmp = BACKUP_DIR / f"_upload_{uuid.uuid4().hex}.xlsx"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(raw)

    try:
        new = parse_excel(tmp, source_label=f"Web upload: {file.filename}")
    except CatalogParseError as e:
        logger.warning("upload: parse failed: %s", e)
        return JSONResponse(
            status_code=400,
            content={"status": "error", "stage": "parse", "message": str(e)},
        )
    finally:
        tmp.unlink(missing_ok=True)

    from app.services.studio import locked

    with locked():
        old = _load_current()
        diff = build_diff(old, new)
        backup = _backup_current()
        _save(new)
        reset_cache()
        p = build_system_prompt()  # прогрев

    logger.warning(
        "catalog uploaded by %s: %d SKU, +%d -%d ~%d",
        _user,
        len(new["items"]),
        len(diff["added"]),
        len(diff["removed"]),
        len(diff["changed"]),
    )

    return JSONResponse(
        {
            "status": "ok",
            "filename": file.filename,
            "uploaded_at": dt.datetime.now().isoformat(),
            "new_total": len(new["items"]),
            "warnings": new.get("warnings", {}),
            "diff": {
                "added": len(diff["added"]),
                "removed": len(diff["removed"]),
                "changed": len(diff["changed"]),
                "unchanged": diff["unchanged"],
                "out_of_stock_new": len(diff["out_of_stock_new"]),
                "low_stock_new": len(diff["low_stock_new"]),
                "samples": {
                    "added": [i["name"] for i in diff["added"][:5]],
                    "removed": [i["name"] for i in diff["removed"][:5]],
                    "changed": diff["changed"][:5],
                },
            },
            "backup": str(backup) if backup else None,
            "prompt": {"skus_in_prompt": p.skus_count, "updated_date": p.updated_date},
        }
    )


@router.get("/catalog", response_class=HTMLResponse)
def catalog_html(user: Annotated[str, Depends(_verify_basic)]) -> HTMLResponse:
    path = Path(__file__).resolve().parent.parent / "static" / "studio.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


# --- Helpers ----------------------------------------------------------------


def _category_counts(items: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        c = it.get("category") or "—"
        out[c] = out.get(c, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
