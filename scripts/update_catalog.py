"""Обновление каталога SKU из Excel-выгрузки 1С (CLI).

Использование:
    .venv/bin/python -m scripts.update_catalog /tmp/30.04.xlsx
    .venv/bin/python -m scripts.update_catalog /tmp/30.04.xlsx --dry-run    # без записи
    .venv/bin/python -m scripts.update_catalog /tmp/30.04.xlsx --no-reload  # без hot-reload

Что делает:
  1. Парсит Excel через app.services.catalog_parser.parse_excel
  2. Делает бэкап текущего app/data/pilot_skus.json в data/catalog_backups/
  3. Записывает новый pilot_skus.json
  4. Дёргает запущенный сервис на 127.0.0.1:8000/admin/catalog/reload (Basic Auth)
     чтобы prompt_builder перечитал каталог без рестарта.

Аналогичная логика доступна Татьяне через веб-форму /admin/catalog (см. routes/admin.py).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import urllib.error
import urllib.request
from base64 import b64encode
from pathlib import Path

from app.config import get_settings
from app.services.catalog_parser import (
    CatalogParseError,
    build_diff,
    parse_excel,
)

BACKUP_DIR = Path("data/catalog_backups")
KEEP_BACKUPS = 30  # храним N последних, остальные удаляем


def _catalog_path() -> Path:
    s = get_settings()
    return Path(s.CATALOG_PATH) if s.CATALOG_PATH else Path("app/data/pilot_skus.json")


def _load_existing() -> dict | None:
    p = _catalog_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _backup_current() -> Path | None:
    p = _catalog_path()
    if not p.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    target = BACKUP_DIR / f"pilot_skus_{ts}.json"
    shutil.copy2(p, target)
    backups = sorted(BACKUP_DIR.glob("pilot_skus_*.json"))
    for old in backups[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return target


def _save(catalog: dict) -> None:
    p = _catalog_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: v for k, v in catalog.items() if k != "warnings"}
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _reload_running_service() -> tuple[bool, str]:
    """Дёрнуть локальный /admin/catalog/reload, чтобы запущенный uvicorn сбросил кэш промпта."""
    s = get_settings()
    url = f"http://127.0.0.1:{s.APP_PORT}/admin/catalog/reload"

    auth = b64encode(f"{s.ADMIN_USER}:{s.ADMIN_PASSWORD}".encode()).decode()
    req = urllib.request.Request(url, method="POST", headers={"Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:  # connection refused, dns, etc
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("excel", help="путь к .xlsx-выгрузке из 1С")
    ap.add_argument("--dry-run", action="store_true", help="не записывать pilot_skus.json")
    ap.add_argument("--no-reload", action="store_true", help="не дёргать живой сервис на reload")
    args = ap.parse_args()

    excel_path = Path(args.excel).expanduser().resolve()
    print(f"[..] парсинг {excel_path.name}")

    try:
        new = parse_excel(excel_path)
    except CatalogParseError as e:
        print(f"[err] парсинг провалился: {e}", file=sys.stderr)
        return 2

    old = _load_existing()
    diff = build_diff(old, new)
    warnings = new.get("warnings", {})

    print(f"[ok] распарсено: {len(new['items'])} SKU, дата выгрузки: {new['updated']}")

    print("\n=== diff ===")
    print(f"  было SKU: {diff['total_old']}, стало: {diff['total_new']}")
    print(f"  добавлено: {len(diff['added'])}, удалено: {len(diff['removed'])}")
    print(f"  изменено цена/остаток: {len(diff['changed'])}, не изменилось: {diff['unchanged']}")
    print(
        f"  без остатка: {len(diff['out_of_stock_new'])}, "
        f"мало (<5): {len(diff['low_stock_new'])}"
    )
    if diff["changed"]:
        print("\n  топ-10 изменений:")
        for c in diff["changed"][:10]:
            tag = ""
            if "price" in c:
                tag += f" цена {c['price'][0]}→{c['price'][1]}"
            if "stock" in c:
                tag += f" остаток {c['stock'][0]}→{c['stock'][1]}"
            print(f"    · {c['name'][:60]}{tag}")

    if warnings.get("duplicate_names") or warnings.get("no_article_count"):
        print("\n=== предупреждения ===")
        if warnings.get("duplicate_names"):
            print(f"  дубликаты имён ({len(warnings['duplicate_names'])}):")
            for n in warnings["duplicate_names"]:
                print(f"    · {n}")
        if warnings.get("no_article_count"):
            print(f"  без артикула: {warnings['no_article_count']} шт.")

    if args.dry_run:
        print("\n[dry-run] файл не сохраняется, сервис не дёргается")
        return 0

    backup = _backup_current()
    if backup:
        print(f"\n[ok] бэкап старого: {backup.relative_to(Path.cwd())}")
    _save(new)
    print(f"[ok] записан новый каталог: {_catalog_path()}")

    if args.no_reload:
        print("[skip] hot-reload пропущен (--no-reload)")
        return 0

    ok, msg = _reload_running_service()
    if ok:
        print(f"[ok] hot-reload: {msg}")
    else:
        print(
            f"[warn] hot-reload не удался ({msg}). "
            "Каталог сохранён, но запущенный бот его подхватит только после рестарта.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
