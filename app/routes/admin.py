"""Админ-панель для Татьяны: обновление каталога через веб-форму.

Эндпоинты (защищены Basic Auth по ADMIN_USER / ADMIN_PASSWORD из .env):
  GET  /admin/catalog              — HTML-страница с формой загрузки + статус
  POST /admin/catalog/upload       — multipart upload Excel (xlsx)
  GET  /admin/catalog/info         — JSON-статус (последнее обновление, кол-во SKU, дни с обновления)
  POST /admin/catalog/reload       — сброс кэша prompt_builder (используется CLI-скриптом)
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import secrets
import shutil
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
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


def _verify_basic(creds: Annotated[HTTPBasicCredentials, Depends(_security)]) -> str:
    s = get_settings()
    if not s.ADMIN_USER or not s.ADMIN_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ADMIN_USER / ADMIN_PASSWORD не заданы в .env",
        )
    user_ok = secrets.compare_digest(creds.username, s.ADMIN_USER)
    pass_ok = secrets.compare_digest(creds.password, s.ADMIN_PASSWORD)
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
    _catalog_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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
        "low_stock": sum(1 for i in items if 0 < i.get("stock", 0) < 5),
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

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Пустой файл")

    # openpyxl умеет читать file-like — пишем во временный путь чтобы parse_excel принял Path
    tmp = BACKUP_DIR / f"_upload_{dt.datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
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
    cur = _load_current()
    info = catalog_info.__wrapped__ if hasattr(catalog_info, "__wrapped__") else None  # noqa: F841
    items = (cur or {}).get("items", [])
    days = days_since_update(cur or {}) if cur else None
    is_stale = days is not None and days >= STALE_DAYS

    stale_banner = ""
    if cur is None:
        stale_banner = (
            '<div class="banner red">⚠️ Каталог не загружен. Загрузите выгрузку 1С — без этого '
            'бот не сможет отвечать клиентам про наличие.</div>'
        )
    elif is_stale:
        stale_banner = (
            f'<div class="banner red">⚠️ Каталог устарел: последняя загрузка '
            f"{days} дней назад. Обновите выгрузку, чтобы бот не вводил клиентов в заблуждение.</div>"
        )

    cats = _category_counts(items)
    cat_list = ", ".join(f"{k}: {v}" for k, v in cats.items()) or "—"
    out_of_stock = sum(1 for i in items if i.get("stock") == 0)
    low_stock = sum(1 for i in items if 0 < i.get("stock", 0) < 5)

    # Manual-слой
    manual = load_manual(_catalog_path())
    manual_items = manual.get("items", [])
    if manual_items:
        manual_block = (
            f'<div class="stats" style="margin-top:12px; background:#f9c74f22; border:1px solid #f9c74f55;">'
            f"<div><b>Ручные дополнения (не из 1С):</b> {len(manual_items)} шт.</div>"
            "<div style='font-size:12px; opacity:0.8;'>Эти SKU НЕ перезаписываются при загрузке Excel.</div>"
            "<ul style='margin: 8px 0 0; padding-left: 18px; font-size: 13px;'>"
            + "".join(
                f"<li>{m.get('name', '')} — {m.get('price', '?')} ₽, {m.get('stock', '?')} шт.</li>"
                for m in manual_items[:20]
            )
            + (f"<li>… и ещё {len(manual_items) - 20}</li>" if len(manual_items) > 20 else "")
            + "</ul></div>"
        )
    else:
        manual_block = ""

    html = _HTML_TEMPLATE.format(
        user=user,
        stale_banner=stale_banner,
        updated=(cur or {}).get("updated", "—"),
        source=(cur or {}).get("source", "—"),
        total=len(items),
        cats=cat_list,
        out_of_stock=out_of_stock,
        low_stock=low_stock,
        days=days if days is not None else "—",
        manual_block=manual_block,
    )
    return HTMLResponse(html)


# --- Helpers ----------------------------------------------------------------


def _category_counts(items: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        c = it.get("category") or "—"
        out[c] = out.get(c, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# Маленький helper чтобы avoid unused-warning от io
_ = io


# --- HTML template ---------------------------------------------------------

_HTML_TEMPLATE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Каталог · Avito-bot</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         max-width: 720px; margin: 24px auto; padding: 0 16px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .sub {{ opacity: 0.7; font-size: 13px; margin-bottom: 24px; }}
  .banner {{ padding: 12px 14px; border-radius: 8px; margin: 12px 0; font-weight: 500; }}
  .banner.red {{ background: #ff3b3022; color: #b01500; border: 1px solid #ff3b3055; }}
  .banner.green {{ background: #34c75922; color: #1e7d3a; border: 1px solid #34c75955; }}
  .stats {{ background: #8884; border-radius: 8px; padding: 12px 16px; }}
  .stats div {{ margin: 4px 0; font-size: 14px; }}
  .stats b {{ font-weight: 600; }}
  form {{ margin: 24px 0; padding: 18px; border: 2px dashed #8886; border-radius: 12px;
          text-align: center; cursor: pointer; transition: 0.15s; }}
  form.drag {{ background: #2e7dff22; border-color: #2e7dff; }}
  input[type=file] {{ display: none; }}
  button {{ padding: 10px 20px; font-size: 15px; cursor: pointer; border-radius: 6px;
           border: 1px solid #8886; background: #2e7dff; color: white; }}
  button:disabled {{ opacity: 0.5; cursor: wait; }}
  #result {{ margin-top: 18px; padding: 14px; border-radius: 8px; background: #8884;
            white-space: pre-wrap; font-family: ui-monospace, Menlo, monospace; font-size: 12px;
            display: none; }}
  .small {{ font-size: 12px; opacity: 0.6; margin-top: 16px; }}
</style>
</head>
<body>

<h1>Каталог Avito-bot</h1>
<div class="sub">Вы вошли как: <b>{user}</b></div>

{stale_banner}

<div class="stats">
  <div><b>Последнее обновление:</b> {updated} ({days} дн. назад)</div>
  <div><b>Источник:</b> {source}</div>
  <div><b>Всего SKU:</b> {total}</div>
  <div><b>По категориям:</b> {cats}</div>
  <div><b>Нет в наличии:</b> {out_of_stock} · <b>Мало (&lt;5):</b> {low_stock}</div>
</div>

{manual_block}

<form id="uploadForm">
  <p style="margin: 0 0 12px;"><b>Перетащите Excel-выгрузку из 1С сюда</b><br>
  <span style="opacity: 0.7;">или нажмите «Выбрать файл». Принимаются .xlsx</span></p>
  <input type="file" id="file" accept=".xlsx,.xlsm">
  <button type="button" id="pickBtn">Выбрать файл</button>
  <button type="submit" id="upBtn" style="margin-left: 8px;" disabled>Загрузить</button>
</form>

<div id="result"></div>

<div class="small">
  Что произойдёт после загрузки: парсер прочитает Excel, проверит данные,
  заменит файл <code>app/data/pilot_skus.json</code> и сбросит кэш промпта —
  следующий ответ бота уже пойдёт со свежим каталогом. Старый файл уйдёт в бэкап.
</div>

<script>
const form = document.getElementById('uploadForm');
const file = document.getElementById('file');
const upBtn = document.getElementById('upBtn');
const result = document.getElementById('result');
let payload = null;

document.getElementById('pickBtn').onclick = () => file.click();
file.onchange = () => {{
  payload = file.files[0] || null;
  upBtn.disabled = !payload;
  if (payload) form.querySelector('p').textContent = '✓ ' + payload.name;
}};

['dragover','dragleave','drop'].forEach(ev => form.addEventListener(ev, e => {{
  e.preventDefault();
  if (ev === 'dragover') form.classList.add('drag');
  else form.classList.remove('drag');
  if (ev === 'drop' && e.dataTransfer.files.length) {{
    payload = e.dataTransfer.files[0];
    upBtn.disabled = false;
    form.querySelector('p').textContent = '✓ ' + payload.name;
  }}
}}));

form.onsubmit = async (e) => {{
  e.preventDefault();
  if (!payload) return;
  upBtn.disabled = true;
  result.style.display = 'block';
  result.textContent = '[..] загружаю...';
  const fd = new FormData();
  fd.append('file', payload);
  try {{
    const r = await fetch('/admin/catalog/upload', {{ method: 'POST', body: fd }});
    const j = await r.json();
    result.textContent = JSON.stringify(j, null, 2);
    if (r.ok && j.status === 'ok') {{
      setTimeout(() => location.reload(), 2500);
    }}
  }} catch (err) {{
    result.textContent = 'Сетевая ошибка: ' + err.message;
  }} finally {{
    upBtn.disabled = false;
  }}
}};
</script>
</body>
</html>
"""
