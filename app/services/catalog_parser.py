"""Парсер выгрузки каталога 1С в Excel → JSON pilot_skus.

Формат входа (одна вкладка, обычно `TDSheet`):
    Наименование | Артикул | Остаток | Розничные цены

Формат выхода (`app/data/pilot_skus.json`):
    {
      "updated": "2026-04-30",
      "source": "1С Excel: 30.04.xlsx",
      "items": [
        {"n": 1, "article": "...", "name": "...", "category": "...",
         "price": 14000, "stock": 5, "status": "В наличии", "note": ""},
        ...
      ]
    }
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

# Категория = первое попавшееся ключевое слово в названии (порядок важен).
# (паттерн в нижнем регистре, итоговая категория для каталога)
CATEGORY_RULES: list[tuple[str, str]] = [
    ("комод", "Комод"),
    ("шкаф", "Шкаф"),
    ("диван", "Диван"),
    ("кровать", "Кровать"),
    ("прихож", "Прихожая"),
    ("вешалка", "Прихожая"),  # вешалка — часть прихожей
    ("матрас", "Матрас"),
    ("тумб", "Тумба"),  # «Тумбы», «Тумба», «Тумбочка»
    ("стеллаж", "Стеллаж"),
    ("кухня", "Кухня"),
    ("стол", "Стол"),
    ("стул", "Стул"),
]

# Ожидаемые заголовки колонок (нормализуем к нижнему регистру и убираем спец-символы)
EXPECTED_HEADERS = {
    "name": ["наименование", "название", "товар"],
    "article": ["артикул"],
    "stock": ["остаток", "остатки", "количество", "кол-во"],
    "price": ["цена", "розничные цены", "розн цена", "стоимость"],
}


class CatalogParseError(Exception):
    """Ошибка парсинга/валидации Excel-выгрузки."""


@dataclass
class SkuItem:
    n: int
    article: str
    name: str
    category: str
    price: int
    stock: int
    status: str
    note: str


# --- Helpers ---------------------------------------------------------------


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _detect_columns(header_row: tuple) -> dict[str, int]:
    """Сопоставить ожидаемые поля с индексами колонок в шапке."""
    found: dict[str, int] = {}
    for idx, raw in enumerate(header_row):
        h = _norm(raw)
        if not h:
            continue
        for field, aliases in EXPECTED_HEADERS.items():
            if field in found:
                continue
            if any(alias in h for alias in aliases):
                found[field] = idx
                break

    missing = [f for f in EXPECTED_HEADERS if f not in found]
    if missing:
        raise CatalogParseError(
            f"В шапке Excel не найдены колонки: {missing}. "
            f"Найденные заголовки: {[_norm(h) for h in header_row if h]}"
        )
    return found


def _detect_category(name: str) -> str:
    low = name.lower()
    for pattern, category in CATEGORY_RULES:
        if pattern in low:
            return category
    return "Прочее"


def _stock_status(stock: int) -> str:
    if stock <= 0:
        return "Нет в наличии"
    if stock < 5:
        return "В наличии (мало)"
    return "В наличии"


def _parse_int(v: Any, what: str, row_no: int) -> int:
    if v is None or (isinstance(v, str) and not v.strip()):
        return 0
    try:
        return int(float(v))
    except (TypeError, ValueError) as e:
        raise CatalogParseError(f"строка {row_no}: не удалось распарсить {what}: {v!r}") from e


# --- Public API ------------------------------------------------------------


def parse_excel(path: str | Path, source_label: str | None = None) -> dict[str, Any]:
    """Прочитать Excel и вернуть pilot_skus-структуру.

    Бросает CatalogParseError при невалидных данных.
    """
    p = Path(path)
    if not p.exists():
        raise CatalogParseError(f"файл не найден: {p}")

    wb = load_workbook(p, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise CatalogParseError("пустой Excel — нет ни одной строки")

    cols = _detect_columns(rows[0])

    items: list[SkuItem] = []
    seen_articles: set[str] = set()
    n = 0
    for row_no, row in enumerate(rows[1:], start=2):
        name = (row[cols["name"]] or "").strip() if row[cols["name"]] is not None else ""
        if not name:
            continue  # пустая строка — пропускаем
        article = (str(row[cols["article"]] or "")).strip()
        price = _parse_int(row[cols["price"]], "цену", row_no)
        stock = _parse_int(row[cols["stock"]], "остаток", row_no)

        if price <= 0:
            raise CatalogParseError(f"строка {row_no} ({name!r}): цена должна быть > 0, получили {price}")
        if stock < 0:
            raise CatalogParseError(f"строка {row_no} ({name!r}): остаток < 0")

        # Дубликаты артикулов — предупреждение, но не ошибка (артикул может быть пустым у нескольких).
        if article and article in seen_articles:
            raise CatalogParseError(f"строка {row_no}: дубликат артикула {article!r}")
        if article:
            seen_articles.add(article)

        n += 1
        items.append(
            SkuItem(
                n=n,
                article=article,
                name=name,
                category=_detect_category(name),
                price=price,
                stock=stock,
                status=_stock_status(stock),
                note="",
            )
        )

    if not items:
        raise CatalogParseError("после обработки в Excel не осталось ни одного SKU")

    # Предупреждения (не ошибки): дубликаты имён, отсутствие артикулов
    name_counts: dict[str, int] = {}
    for it in items:
        name_counts[it.name] = name_counts.get(it.name, 0) + 1
    duplicate_names = [n for n, c in name_counts.items() if c > 1]
    no_article_count = sum(1 for it in items if not it.article)

    return {
        "updated": dt.date.today().isoformat(),
        "source": source_label or f"1С Excel: {p.name}",
        "items": [asdict(it) for it in items],
        "warnings": {
            "duplicate_names": duplicate_names,
            "no_article_count": no_article_count,
        },
    }


# --- Diff ------------------------------------------------------------------


def _index_items(catalog: dict[str, Any] | None) -> dict[str, dict]:
    """Индексировать SKU по article, иначе по name. Чтобы diff был стабильным
    даже если в выгрузке два товара имеют одинаковое name (а артикулы разные)."""
    out: dict[str, dict] = {}
    for it in (catalog or {}).get("items", []):
        key = (it.get("article") or "").strip() or f"name::{it.get('name', '')}"
        out[key] = it
    return out


def build_diff(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """Сравнить два каталога. Используется для UI-отчёта после загрузки."""
    old_items = _index_items(old)
    new_items = _index_items(new)

    added_keys = sorted(new_items.keys() - old_items.keys())
    removed_keys = sorted(old_items.keys() - new_items.keys())
    changed: list[dict[str, Any]] = []
    same = 0
    for key in sorted(new_items.keys() & old_items.keys()):
        o, n = old_items[key], new_items[key]
        diffs: dict[str, Any] = {}
        if o.get("price") != n.get("price"):
            diffs["price"] = (o.get("price"), n.get("price"))
        if o.get("stock") != n.get("stock"):
            diffs["stock"] = (o.get("stock"), n.get("stock"))
        if diffs:
            changed.append({"name": n.get("name"), "article": n.get("article"), **diffs})
        else:
            same += 1

    return {
        "updated_old": (old or {}).get("updated"),
        "updated_new": new.get("updated"),
        "total_old": len(old_items),
        "total_new": len(new_items),
        "added": [new_items[k] for k in added_keys],
        "removed": [old_items[k] for k in removed_keys],
        "changed": changed,
        "unchanged": same,
        "out_of_stock_new": [it.get("name") for it in new_items.values() if it.get("stock") == 0],
        "low_stock_new": [it.get("name") for it in new_items.values() if 0 < it.get("stock", 0) < 5],
    }


def manual_path_for(main_path: str | Path) -> Path:
    """Путь к manual-файлу рядом с основным каталогом.

    `pilot_skus.json` → `pilot_skus_manual.json`. Этот файл хранит SKU, которые
    добавляются вручную (не из 1С) и НЕ должны затираться при upload Excel.
    """
    p = Path(main_path)
    return p.with_name(p.stem + "_manual" + p.suffix)


def load_manual(main_path: str | Path) -> dict[str, Any]:
    """Прочитать manual-файл. Если его нет — вернуть пустой каталог."""
    mp = manual_path_for(main_path)
    if not mp.exists():
        return {"updated": "", "source": "manual (нет файла)", "items": []}
    try:
        return json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"updated": "", "source": "manual (битый файл)", "items": []}


def merge_main_and_manual(
    main: dict[str, Any] | None, manual: dict[str, Any] | None
) -> dict[str, Any]:
    """Объединить основной каталог 1С и ручные дополнения в один.

    SKU из manual идут **в конец** списка с пере-нумерацией `n`.
    Если у manual-SKU есть артикул, совпадающий с артикулом из основного — manual выигрывает
    (используется как override). Это позволяет руками править неверные данные из 1С.
    """
    main = main or {}
    manual = manual or {}
    main_items = list(main.get("items") or [])
    manual_items = list(manual.get("items") or [])

    final_main = []
    overridden_articles: set[str] = set()
    for it in main_items:
        art = (it.get("article") or "").strip()
        # Проверим, есть ли override в manual
        m_override = next(
            (m for m in manual_items if (m.get("article") or "").strip() and m.get("article") == art),
            None,
        )
        if m_override:
            final_main.append({**it, **m_override})  # manual поверх
            overridden_articles.add(art)
        else:
            final_main.append(it)

    # Manual без overrides добавляются в конец
    manual_extra = [
        m for m in manual_items
        if not ((m.get("article") or "").strip() and m.get("article") in overridden_articles)
    ]

    merged_items = final_main + manual_extra
    # Перенумерация
    for i, it in enumerate(merged_items, start=1):
        it["n"] = i

    return {
        "updated": main.get("updated"),
        "source": main.get("source"),
        "items": merged_items,
        "_manual_count": len(manual_extra),
        "_main_count": len(main_items),
    }


def days_since_update(catalog: dict[str, Any]) -> int | None:
    s = catalog.get("updated")
    if not s:
        return None
    try:
        d = dt.date.fromisoformat(str(s))
    except ValueError:
        return None
    return (dt.date.today() - d).days
