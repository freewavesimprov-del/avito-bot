"""Тесты парсера каталога 1С Excel → JSON."""

from __future__ import annotations

from openpyxl import Workbook

from app.services.catalog_parser import (
    CatalogParseError,
    build_diff,
    days_since_update,
    parse_excel,
)


def _make_xlsx(tmp_path, rows: list[tuple]):
    """Создать тестовый xlsx с заданными строками. Первая строка — шапка."""
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    p = tmp_path / "catalog.xlsx"
    wb.save(p)
    return p


def test_parse_minimal(tmp_path):
    p = _make_xlsx(
        tmp_path,
        [
            ("Наименование", "Артикул", "Остаток", "Розничные цены"),
            ("Челси Комод 1202 (6 ящиков)", "МФ001", 17, 10750),
            ("Норд Шкаф 4-х ств. Кашемир", "МФ002", 12, 28600),
            ("Мартина Кровать 1.6м", "МФ003", 5, 10500),
        ],
    )
    cat = parse_excel(p)
    assert len(cat["items"]) == 3
    assert cat["items"][0]["name"].startswith("Челси Комод")
    assert cat["items"][0]["price"] == 10750
    assert cat["items"][0]["stock"] == 17
    assert cat["items"][0]["category"] == "Комод"
    assert cat["items"][1]["category"] == "Шкаф"
    assert cat["items"][2]["category"] == "Кровать"
    assert cat["items"][2]["status"] == "В наличии"
    assert cat["warnings"]["duplicate_names"] == []


def test_categories_full_coverage(tmp_path):
    """Все наши категории определяются из имени."""
    p = _make_xlsx(
        tmp_path,
        [
            ("Наименование", "Артикул", "Остаток", "Цена"),
            ("Диван Мадрид", "1", 2, 22800),
            ("Прихожая Стелла", "2", 10, 17350),
            ("Матрас Стандарт 140x200", "3", 13, 12950),
            ("Фиеста Тумбы", "4", 6, 5500),
            ("Челси Стеллаж", "5", 3, 8900),
            ("ЛДСП Кухня Рио", "6", 1, 19000),
            ("Мартина Вешалка", "7", 14, 11800),
            ("Стол обеденный белый", "8", 4, 7500),
        ],
    )
    cat = parse_excel(p)
    cats = {it["name"]: it["category"] for it in cat["items"]}
    assert cats["Диван Мадрид"] == "Диван"
    assert cats["Прихожая Стелла"] == "Прихожая"
    assert cats["Матрас Стандарт 140x200"] == "Матрас"
    assert cats["Фиеста Тумбы"] == "Тумба"
    assert cats["Челси Стеллаж"] == "Стеллаж"
    assert cats["ЛДСП Кухня Рио"] == "Кухня"
    assert cats["Мартина Вешалка"] == "Прихожая"  # вешалка → прихожая
    assert cats["Стол обеденный белый"] == "Стол"


def test_stock_status(tmp_path):
    p = _make_xlsx(
        tmp_path,
        [
            ("Наименование", "Артикул", "Остаток", "Цена"),
            ("A Шкаф", "1", 0, 1000),
            ("B Шкаф", "2", 3, 1000),
            ("C Шкаф", "3", 12, 1000),
        ],
    )
    cat = parse_excel(p)
    statuses = {it["name"]: it["status"] for it in cat["items"]}
    assert statuses["A Шкаф"] == "Нет в наличии"
    assert statuses["B Шкаф"] == "В наличии (мало)"
    assert statuses["C Шкаф"] == "В наличии"


def test_skip_empty_rows(tmp_path):
    p = _make_xlsx(
        tmp_path,
        [
            ("Наименование", "Артикул", "Остаток", "Цена"),
            ("Шкаф первый", "1", 5, 10000),
            (None, None, None, None),
            ("", "", "", ""),
            ("Шкаф второй", "2", 5, 10000),
        ],
    )
    cat = parse_excel(p)
    assert len(cat["items"]) == 2


def test_missing_columns(tmp_path):
    p = _make_xlsx(
        tmp_path,
        [
            ("Название",),
            ("Шкаф",),
        ],
    )
    try:
        parse_excel(p)
        raise AssertionError("должно было бросить CatalogParseError")
    except CatalogParseError as e:
        assert "не найдены колонки" in str(e)


def test_zero_price_rejected(tmp_path):
    p = _make_xlsx(
        tmp_path,
        [
            ("Наименование", "Артикул", "Остаток", "Цена"),
            ("Шкаф", "1", 5, 0),
        ],
    )
    try:
        parse_excel(p)
        raise AssertionError("должно было бросить CatalogParseError")
    except CatalogParseError as e:
        assert "цена" in str(e).lower()


def test_duplicate_articles_rejected(tmp_path):
    p = _make_xlsx(
        tmp_path,
        [
            ("Наименование", "Артикул", "Остаток", "Цена"),
            ("Шкаф первый", "МФ001", 5, 10000),
            ("Шкаф второй", "МФ001", 3, 12000),  # тот же артикул
        ],
    )
    try:
        parse_excel(p)
        raise AssertionError("должно было бросить CatalogParseError")
    except CatalogParseError as e:
        assert "дубликат артикула" in str(e)


def test_duplicate_names_warning_not_error(tmp_path):
    """Одинаковые имена с разными артикулами — это warning, не ошибка."""
    p = _make_xlsx(
        tmp_path,
        [
            ("Наименование", "Артикул", "Остаток", "Цена"),
            ("Шкаф 3-х ств", "МФ001", 5, 16000),
            ("Шкаф 3-х ств", "МФ002", 8, 17800),  # тот же name, другой article
        ],
    )
    cat = parse_excel(p)
    assert len(cat["items"]) == 2
    assert "Шкаф 3-х ств" in cat["warnings"]["duplicate_names"]


def test_empty_article_allowed(tmp_path):
    """Артикул может быть пустым — товары без артикула в 1С."""
    p = _make_xlsx(
        tmp_path,
        [
            ("Наименование", "Артикул", "Остаток", "Цена"),
            ("Матрас Стандарт", "", 5, 11500),
            ("Матрас Премиум", None, 3, 15000),
        ],
    )
    cat = parse_excel(p)
    assert len(cat["items"]) == 2
    assert cat["warnings"]["no_article_count"] == 2


# --- diff -----------------------------------------------------------------


def test_diff_added_removed_changed():
    old = {
        "updated": "2026-04-22",
        "items": [
            {"article": "A", "name": "Шкаф A", "price": 10000, "stock": 5, "category": "Шкаф"},
            {"article": "B", "name": "Шкаф B", "price": 12000, "stock": 3, "category": "Шкаф"},
            {"article": "C", "name": "Кровать C", "price": 8000, "stock": 7, "category": "Кровать"},
        ],
    }
    new = {
        "updated": "2026-04-30",
        "items": [
            {"article": "A", "name": "Шкаф A", "price": 10500, "stock": 5, "category": "Шкаф"},  # цена ↑
            {"article": "C", "name": "Кровать C", "price": 8000, "stock": 7, "category": "Кровать"},  # без изменений
            {"article": "D", "name": "Стол D", "price": 6500, "stock": 2, "category": "Стол"},  # новый
        ],
    }
    d = build_diff(old, new)
    assert d["total_old"] == 3
    assert d["total_new"] == 3
    assert len(d["added"]) == 1
    assert d["added"][0]["article"] == "D"
    assert len(d["removed"]) == 1
    assert d["removed"][0]["article"] == "B"
    assert len(d["changed"]) == 1
    assert d["changed"][0]["article"] == "A"
    assert d["changed"][0]["price"] == (10000, 10500)
    assert d["unchanged"] == 1
    assert d["low_stock_new"] == ["Стол D"]


def test_diff_works_with_duplicate_names():
    """Если в каталоге два товара с одним именем, но разными артикулами —
    diff видит их как два разных SKU (по артикулу)."""
    new = {
        "updated": "2026-04-30",
        "items": [
            {"article": "X1", "name": "Шкаф", "price": 16000, "stock": 5},
            {"article": "X2", "name": "Шкаф", "price": 18000, "stock": 8},
        ],
    }
    d = build_diff(None, new)
    assert d["total_new"] == 2
    assert len(d["added"]) == 2


def test_days_since_update():
    import datetime as dt

    today = dt.date.today().isoformat()
    assert days_since_update({"updated": today}) == 0
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    assert days_since_update({"updated": yesterday}) == 1
    assert days_since_update({}) is None
    assert days_since_update({"updated": "плохая дата"}) is None


# --- Manual layer ----------------------------------------------------------


def test_load_manual_missing_file_returns_empty(tmp_path):
    from app.services.catalog_parser import load_manual

    main = tmp_path / "pilot_skus.json"
    # Файла _manual нет — должен вернуть пустой
    res = load_manual(main)
    assert res["items"] == []


def test_load_manual_reads_existing(tmp_path):
    import json as _json

    from app.services.catalog_parser import load_manual

    main = tmp_path / "pilot_skus.json"
    manual = tmp_path / "pilot_skus_manual.json"
    manual.write_text(
        _json.dumps({"items": [{"name": "Диван X", "price": 1000, "stock": 5}]}),
        encoding="utf-8",
    )
    res = load_manual(main)
    assert len(res["items"]) == 1
    assert res["items"][0]["name"] == "Диван X"


def test_merge_appends_manual_to_main():
    from app.services.catalog_parser import merge_main_and_manual

    main = {"updated": "2026-04-30", "items": [
        {"article": "A1", "name": "Шкаф", "price": 100, "stock": 5},
    ]}
    manual = {"items": [
        {"name": "Диван Мадрид", "price": 22800, "stock": 8, "category": "Диван"},
    ]}
    merged = merge_main_and_manual(main, manual)
    assert len(merged["items"]) == 2
    assert merged["_main_count"] == 1
    assert merged["_manual_count"] == 1
    # Перенумерация
    assert merged["items"][0]["n"] == 1
    assert merged["items"][1]["n"] == 2
    # Manual уходит в конец
    assert merged["items"][1]["name"] == "Диван Мадрид"


def test_merge_manual_overrides_main_by_article():
    """Если в manual есть тот же article — он переписывает значение из main."""
    from app.services.catalog_parser import merge_main_and_manual

    main = {"items": [{"article": "A1", "name": "Шкаф", "price": 100, "stock": 10}]}
    manual = {"items": [{"article": "A1", "name": "Шкаф (исправлено)", "price": 150, "stock": 10}]}
    merged = merge_main_and_manual(main, manual)
    assert len(merged["items"]) == 1
    assert merged["items"][0]["name"] == "Шкаф (исправлено)"
    assert merged["items"][0]["price"] == 150


def test_merge_handles_empty_inputs():
    from app.services.catalog_parser import merge_main_and_manual

    assert merge_main_and_manual({}, {})["items"] == []
    assert merge_main_and_manual(None, None)["items"] == []  # type: ignore[arg-type]
    assert merge_main_and_manual({"items": [{"name": "X", "price": 1, "stock": 1}]}, {})["_main_count"] == 1
    assert merge_main_and_manual({}, {"items": [{"name": "Y", "price": 1, "stock": 1}]})["_manual_count"] == 1
