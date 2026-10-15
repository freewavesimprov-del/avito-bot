"""Объявление, по которому пишет клиент.

Зачем. Клиент открывает чат из конкретного объявления и спрашивает «есть ли
в других цветах?» — не называя товар. Бот этого объявления не знал и
переспрашивал «какой комод вас интересует?», а на «Бася» отвечал, что такого
нет: каталог бота — старая выгрузка из 1С, где половины витрины нет
(01.10.2026, chat166254).

Ссылку на объявление коннектор Авито кладёт в чат Битрикса служебной строкой
«Переданы дополнительные данные: Ссылка на объявление в Avito: …». Номер
объявления — хвост адреса. Название и цену берём из API Авито (список
активных объявлений кабинета, кэш на полчаса), там же ищем соседние
объявления той же модели — это и есть «другие цвета», которые реально
выставлены.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

logger = logging.getLogger("avito_bot.ad_context")

_URL_RE = re.compile(r"https?://(?:www\.)?avito\.ru/[^\s\[\]<>\"]+")
_ID_RE = re.compile(r"_(\d{6,})(?:[/?#]|$)")
_ITEMS: dict[str, dict[str, Any]] = {}
_ITEMS_AT = 0.0
_TTL = 1800
_BY_DIALOG: dict[str, str] = {}

# Слова, которые не являются названием модели.
_GENERIC = {
    "комод", "шкаф", "шкафы", "диван", "кровать", "тумба", "прихожая", "стол",
    "стеллаж", "пенал", "антресоль", "кухня", "кухонный", "гарнитур", "угловой",
    "угловая", "прямой", "распашной", "купе", "двуспальная", "односпальная",
    "двухстворчатый", "трехстворчатый", "трёхстворчатый", "четырехстворчатый",
    "одностворчатый", "ящиками", "ящиком", "дверцами", "дверцей", "полками",
    "зеркалом", "одежды", "белья", "новый", "новая", "новое", "мебель", "лдсп",
    "раскладной", "раскладным", "спальным", "местом", "механизмом", "подъемным",
    "подъёмным", "для", "это", "цвет", "цвета",
}


def _model_word(title: str) -> str:
    for word in re.findall(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё-]{2,}", title):
        low = word.lower()
        if low in _GENERIC or low.isdigit():
            continue
        if re.fullmatch(r"(бел|черн|сер|граф|дуб|венге|сонома|кашемир|беж).*", low):
            continue
        return low
    return ""


async def _load_items() -> dict[str, dict[str, Any]]:
    global _ITEMS, _ITEMS_AT
    if _ITEMS and time.time() - _ITEMS_AT < _TTL:
        return _ITEMS
    from app.services.avito_client import AvitoClient

    client = AvitoClient.from_settings()
    out: dict[str, dict[str, Any]] = {}
    try:
        for page in range(1, 20):
            data = await client._request("GET", "/core/v1/items", params={
                "per_page": 100, "page": page, "status": "active"})
            rows = data.get("resources") or []
            for r in rows:
                out[str(r.get("id"))] = {"title": str(r.get("title") or ""),
                                         "price": r.get("price"),
                                         "url": str(r.get("url") or ""),
                                         "address": str(r.get("address") or "")}
            if len(rows) < 100:
                break
    finally:
        await client.close()
    if out:
        _ITEMS, _ITEMS_AT = out, time.time()
    return _ITEMS


async def item_of_dialog(bitrix_client: Any, dialog_id: str) -> str:
    if dialog_id in _BY_DIALOG:
        return _BY_DIALOG[dialog_id]
    try:
        msgs = await bitrix_client.im_dialog_messages_get(dialog_id=dialog_id, limit=50)
    except Exception as exc:                              # noqa: BLE001
        logger.info("%s: история чата не прочиталась: %s", dialog_id, str(exc)[:100])
        return ""
    for m in sorted(msgs, key=lambda x: int(x.get("id") or 0), reverse=True):
        found = _URL_RE.search(str(m.get("text") or ""))
        if not found:
            continue
        item = _ID_RE.search(found.group(0).split("[")[0])
        if item:
            _BY_DIALOG[dialog_id] = item.group(1)
            return item.group(1)
    return ""


def _price(value: Any) -> str:
    try:
        return f"{int(value):,} ₽".replace(",", " ")
    except (TypeError, ValueError):
        return "цена не указана"


async def note_for(bitrix_client: Any, dialog_id: str) -> str:
    """Блок для системного промпта. Пустая строка, если объявление не нашлось."""
    item_id = await item_of_dialog(bitrix_client, dialog_id)
    if not item_id:
        return ""
    try:
        items = await _load_items()
    except Exception as exc:                              # noqa: BLE001
        logger.warning("объявления Авито не загрузились: %s", str(exc)[:120])
        items = {}
    ad = items.get(item_id)
    if not ad:
        return (f"# Объявление клиента\n\nКлиент пишет из объявления №{item_id}, "
                "но оно сейчас не активно. Если вопрос без названия товара — "
                "попроси уточнить, о каком товаре речь.")
    model = _model_word(ad["title"])
    kind = (ad["title"].split() or [""])[0].lower()
    seen, siblings = {(ad["title"], ad["price"])}, []
    if model:
        for iid, other in items.items():
            if iid == item_id:
                continue
            title = other["title"]
            if model in title.lower() and title.lower().startswith(kind):
                key = (title, other["price"])
                if key not in seen:
                    seen.add(key)
                    siblings.append(f"- {title} — {_price(other['price'])}")
    lines = [
        "# Объявление, из которого пишет клиент",
        "",
        f"Клиент открыл чат из объявления «{ad['title']}», цена {_price(ad['price'])}.",
        "Все вопросы без названия товара («есть ли в других цветах», «какие размеры», "
        "«в наличии?», «сколько стоит доставка») относятся к ЭТОМУ товару. "
        "Не переспрашивай, какой товар интересует: ты это знаешь.",
    ]
    if siblings:
        lines += ["", "Другие объявления той же модели, которые сейчас выставлены "
                  "(это и есть доступные варианты расцветок и размеров; конструкция у них может "
                  "отличаться — ящики, дверцы, ширина видны в названии, называй разницу честно):", *siblings[:15]]
    else:
        lines += ["", "Других объявлений этой модели сейчас нет. Если спрашивают другие "
                  "цвета — скажи, что уточнишь у менеджера, и передай вопрос."]
    return "\n".join(lines)
