"""Сборщик system prompt для LLM.

Читает:
  - app/prompts/system_prompt.md   — основной текст
  - app/data/pilot_skus.json       — источник для {{ SKUS_TABLE }} и {{ UPDATED_DATE }}
  - app/data/faq_extras.md         — дополнительный справочник (дописывается в конец)

В память кэшируется при первом обращении; reset_cache() для перечитки без рестарта.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config import get_settings

# Пути по умолчанию — относительно корня репозитория (app/.. == корень).
# `pilot_skus.json` можно переопределить через CATALOG_PATH в .env (полезно в тестах).
_APP_DIR = Path(__file__).resolve().parent.parent
_PROMPT_PATH = _APP_DIR / "prompts" / "system_prompt.md"
_DEFAULT_SKUS_PATH = _APP_DIR / "data" / "pilot_skus.json"
_FAQ_PATH = _APP_DIR / "data" / "faq_extras.md"


def _skus_path() -> Path:
    override = (get_settings().CATALOG_PATH or "").strip()
    if override:
        return Path(override)
    return _DEFAULT_SKUS_PATH


@dataclass(frozen=True)
class PromptPayload:
    system: str
    updated_date: str
    skus_count: int


def _format_price(price: int | float | None) -> str:
    if price is None or price == 0:
        return "—"
    # «14 000 ₽»
    return f"{int(price):,} ₽".replace(",", " ")


# Скобочные группы, которые клиенту показывать не нужно: внутренние коды 1С
# и пометки маркетплейсов. Числовые группы вида (1600), (800) НЕ трогаем —
# это ширина в мм, она клиенту полезна.
_JUNK_PAREN = re.compile(
    r"\(\s*(?:ш|2упк|упк|яндекс\s*маркет|маркет|озон|ozon|wb|wildberries|"
    r"вайлдберриз|авито|avito)\s*\)",
    re.IGNORECASE,
)


def _split_top_level(inner: str) -> list[str]:
    """Разбить строку по запятым верхнего уровня, не залезая во вложенные скобки.

    «Дуб Крафт, Дуб Крафт (кромка венге)» → ['Дуб Крафт', 'Дуб Крафт (кромка венге)']
    """
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in inner:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _dedupe_parts(inner: str) -> str:
    """Схлопнуть повторы цвета внутри одной скобочной группы.

    «Кашемир, Кашемир» → «Кашемир»
    «Графит, графит» → «Графит»
    «Графит/Дуб Юкон, Дуб Юкон» → «Графит/Дуб Юкон»
    «Дуб Крафт, Дуб Крафт (кромка венге)» → «Дуб Крафт (кромка венге)»

    1С дублирует цвет корпуса и фасада; часто один кусок входит в другой как
    часть — тогда оставляем более полный.
    """
    kept: list[str] = []
    for p in _split_top_level(inner):
        low = p.lower()
        if any(low == k.lower() or low in k.lower() for k in kept):
            continue
        kept = [k for k in kept if k.lower() not in low]
        kept.append(p)
    return ", ".join(kept)


def _dedupe_all_groups(s: str) -> str:
    """Пройтись по всем скобочным группам верхнего уровня и схлопнуть повторы."""
    out: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(s):
        if ch == "(":
            if depth == 0:
                out.append(s[start:i])
                start = i + 1
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                out.append("(" + _dedupe_parts(s[start:i]) + ")")
                start = i + 1
    out.append(s[start:])
    return "".join(out)


def _clean_name(name: str) -> str:
    """Убрать из названия внутренние артефакты 1С перед показом клиенту.

    Только для промпта — в самом каталоге имена остаются как есть, чтобы
    сопоставление с выгрузкой 1С не сломалось.
    """
    s = name or ""
    s = _JUNK_PAREN.sub(" ", s)
    # «(КРОМКА ДУБ СОНОМА)» кричит капсом — приводим к нормальному виду
    s = re.sub(r"КРОМКА\s+([А-ЯЁ\s]+)", lambda m: "кромка " + m.group(1).lower().strip(), s)
    s = _dedupe_all_groups(s)
    s = s.replace("( ", "(").replace(" )", ")")
    # десятичные числа («2,0м») от нормализации запятых защищаем
    s = re.sub(r"(?<=\d),(?=\d)", "\x00", s)
    s = re.sub(r"\s*,\s*", ", ", s)
    s = s.replace("\x00", ",")
    # «ств.(1600)(золото)» → «ств. (1600) (золото)» — иначе читается как каша
    s = re.sub(r"(?<=[^\s(])\(", " (", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    s = re.sub(r"\(\s*\)", "", s).strip()
    return s


def _build_skus_table(items: list[dict]) -> str:
    """Компактная markdown-таблица: Категория | Название | Цена.

    Остатки и статус наличия в промпт НЕ попадают намеренно: выгрузка из 1С
    обновляется вручную и быстро устаревает (в августе 2026 бот назвал клиенту
    количество из апрельской выгрузки). Пока нет надёжного источника остатков,
    единственный безопасный вариант — модель их просто не видит и физически не
    может назвать. Наличие уточняет менеджер, см. system_prompt.md.

    Сортируем по категории → цене, чтобы LLM легче находила группы товаров.
    """
    rows_by_cat: dict[str, list[dict]] = {}
    for it in items:
        rows_by_cat.setdefault(it.get("category") or "—", []).append(it)

    lines = ["| Категория | Название | Цена |", "|---|---|---|"]
    for cat in sorted(rows_by_cat):
        for it in sorted(rows_by_cat[cat], key=lambda r: r.get("price") or 0):
            name = _clean_name(it.get("name", ""))
            price = _format_price(it.get("price"))
            lines.append(f"| {cat} | {name} | {price} |")
    return "\n".join(lines)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _legacy_system_prompt(_catalog_stamp: tuple = ()) -> PromptPayload:
    """Собрать system prompt. Кэшируется; reset_cache() для перечитки.

    Читает основной каталог (из 1С) + manual-слой (ручные дополнения) и мерджит.
    Manual-слой переживает Excel-upload — позиции, которые не выгружаются из 1С
    (например, диваны), хранятся отдельно и автоматически добавляются в промпт.
    """
    from app.services.catalog_parser import load_manual, merge_main_and_manual

    prompt_tpl = _read_text(_PROMPT_PATH)
    main_skus = json.loads(_read_text(_skus_path()))
    manual_skus = load_manual(_skus_path())
    skus_raw = merge_main_and_manual(main_skus, manual_skus)

    updated = str(skus_raw.get("updated", "")).strip() or "—"
    items = skus_raw.get("items", []) or []
    skus_table = _build_skus_table(items)

    body = prompt_tpl.replace("{{ UPDATED_DATE }}", updated).replace(
        "{{ SKUS_TABLE }}", skus_table
    )

    # Дописываем FAQ-справочник (если файл есть) как вспомогательный блок
    if _FAQ_PATH.exists():
        faq = _read_text(_FAQ_PATH).strip()
        body = f"{body}\n\n---\n\n# Справочник (FAQ)\n\n{faq}"

    return PromptPayload(system=body, updated_date=updated, skus_count=len(items))


def reset_cache() -> None:
    _legacy_system_prompt.cache_clear()


def compose_prompt(catalog: dict, behavior: dict | None = None, query: str = "", ad_id: str = "") -> PromptPayload:
    """Used by the live bot and by the isolated admin playground."""
    from app.services.avito_import import catalog_context
    from app.services.studio import defaults, render_behavior

    updated = str(catalog.get("updated") or "—")
    if catalog.get("format") == "avito":
        table = "Характеристики и цены — в структурированных данных объявлений ниже."
        context = catalog_context(catalog, query, ad_id)
    else:
        table = _build_skus_table(catalog.get("items", []))
        context = ""
    return PromptPayload(
        system=render_behavior(behavior or defaults(), table, updated) + context,
        updated_date=updated, skus_count=len(catalog.get("items", [])),
    )


def build_system_prompt() -> PromptPayload:
    from app.services.studio import active_behavior

    main = json.loads(_skus_path().read_text(encoding="utf-8"))
    behavior = active_behavior()
    if main.get("format") == "avito":
        return compose_prompt(main, behavior)
    if behavior:
        from app.services.catalog_parser import load_manual, merge_main_and_manual

        return compose_prompt(merge_main_and_manual(main, load_manual(_skus_path())), behavior)
    from app.services.catalog_parser import manual_path_for

    manual = manual_path_for(_skus_path())
    stamp = (str(_skus_path().resolve()), _skus_path().stat().st_mtime_ns, manual.stat().st_mtime_ns if manual.exists() else None)
    return _legacy_system_prompt(stamp)


def reply_prompt(text: str, history: list[dict], ad_note: str) -> str:
    from app.services.studio import active_behavior

    catalog = json.loads(_skus_path().read_text(encoding="utf-8"))
    if catalog.get("format") != "avito":
        prompt = build_system_prompt().system
        return prompt + ("\n\n" + ad_note if ad_note else "")
    selected = re.search(r"Объявление №(\d+)", ad_note)
    query = " ".join(str(h.get("message", "")) for h in history[-6:] if h.get("direction") == "in") + " " + text
    return compose_prompt(catalog, active_behavior(), query, selected[1] if selected else "").system
