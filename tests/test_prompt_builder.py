"""Тесты сборки system prompt."""

from __future__ import annotations

import pytest

from app.services.prompt_builder import build_system_prompt, reset_cache


@pytest.fixture(autouse=True)
def _clear_prompt_cache():
    reset_cache()
    yield
    reset_cache()


def test_prompt_contains_company_context():
    p = build_system_prompt()
    assert "Недорогая мебель" in p.system
    assert "Шатура" in p.system  # офлайн-точка из system_prompt.md


def test_placeholders_are_substituted():
    p = build_system_prompt()
    assert "{{ SKUS_TABLE }}" not in p.system
    assert "{{ UPDATED_DATE }}" not in p.system
    assert p.updated_date == "2026-04-22"


def test_skus_table_has_all_18_items():
    p = build_system_prompt()
    assert p.skus_count == 18
    # Каждый известный артикул должен встретиться в тексте
    assert "МФ0000075608" not in p.system  # артикул в текущей версии таблицы не выводится —
    # ОК: мы выводим Категория | Название | Цена. Но товары — по имени:
    assert "Ангара Комод Графит/Дуб Юкон" in p.system
    assert "Диван Мадрид" in p.system
    assert "Прихожая Стелла" in p.system


def test_skus_table_formatting():
    p = build_system_prompt()
    # Markdown-таблица: заголовок и разделитель.
    # Остатков в таблице нет намеренно — источник остатков ненадёжен, наличие
    # уточняет менеджер (см. _build_skus_table и блок «Наличие товара» в промпте).
    assert "| Категория | Название | Цена |" in p.system
    assert "|---|---|---|" in p.system
    # Цена форматируется с пробелом
    assert "14 000 ₽" in p.system


def test_skus_table_has_no_stock_columns():
    """Гарантия, что остатки не утекут в промпт: бот не должен видеть числа."""
    p = build_system_prompt()
    assert "| Остаток |" not in p.system
    assert "| Статус |" not in p.system


def test_clean_name_removes_1c_artifacts():
    """Внутренние коды 1С и пометки маркетплейсов клиенту не показываем."""
    from app.services.prompt_builder import _clean_name

    assert _clean_name("Комод 6 ящиков ЛДСП Белый (Яндекс Маркет)") == "Комод 6 ящиков ЛДСП Белый"
    assert _clean_name("Челси Комод 1202 (6 ящиков) (Ш) (Графит, графит)") == (
        "Челси Комод 1202 (6 ящиков) (Графит)"
    )
    assert _clean_name("Ангара Комод (2УПК) (Графит/Дуб Юкон, Дуб Юкон)") == (
        "Ангара Комод (Графит/Дуб Юкон)"
    )
    # вложенные скобки и дубль внутри них
    assert _clean_name("Норд Шкаф 4-х ств.(1600) (Дуб Крафт, Дуб Крафт (КРОМКА ВЕНГЕ))") == (
        "Норд Шкаф 4-х ств. (1600) (Дуб Крафт (кромка венге))"
    )
    # ширина в мм — полезна, не трогаем; десятичная запятая не ломается
    assert _clean_name("ЛДСП Кухня Рио NEW 2,0м (490) (Дуб Крафт Белый)") == (
        "ЛДСП Кухня Рио NEW 2,0м (490) (Дуб Крафт Белый)"
    )


def test_faq_extras_appended():
    p = build_system_prompt()
    assert "Справочник (FAQ)" in p.system
    assert "Подъём на этаж" in p.system


def test_cache_works_and_resets():
    p1 = build_system_prompt()
    p2 = build_system_prompt()
    assert p1 is p2  # lru_cache возвращает тот же объект
    reset_cache()
    p3 = build_system_prompt()
    assert p3 is not p1  # после сброса — новый объект
    assert p3.system == p1.system  # но содержимое одинаковое
