"""Versioned local drafts and request-scoped playground overrides."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from app.config import get_settings


class Template(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[\w-]+$")
    name: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=4000)
    enabled: bool = True


class Scenario(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[\w-]+$")
    name: str = Field(min_length=1, max_length=120)
    condition: str = Field(min_length=1, max_length=1000)
    steps: list[str] = Field(min_length=1, max_length=20)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_steps(self):
        if any(not s.strip() or len(s) > 2000 for s in self.steps):
            raise ValueError("Шаг должен содержать от 1 до 2000 символов")
        return self


class Behavior(BaseModel):
    system_prompt: str = Field(min_length=20, max_length=40000)
    faq: str = Field(max_length=30000)
    templates: list[Template] = Field(default_factory=list, max_length=60)
    scenarios: list[Scenario] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def unique_ids(self):
        for values in (self.templates, self.scenarios):
            if len({v.id for v in values}) != len(values):
                raise ValueError("Идентификаторы должны быть уникальными")
        return self


class PreviewContext(BaseModel):
    prompt: str


preview_context: ContextVar[PreviewContext | None] = ContextVar("studio_preview", default=None)


def catalog_path() -> Path:
    return Path(get_settings().CATALOG_PATH or "app/data/pilot_skus.json")


def store_root() -> Path:
    return catalog_path().resolve().parent / ".studio"


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp = tempfile.mkstemp(prefix=".save-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


@contextmanager
def locked():
    store_root().mkdir(parents=True, exist_ok=True)
    with (store_root() / ".lock").open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Неверный идентификатор версии")
    return value


def defaults() -> dict:
    from app.services.filters import ESCALATION_CLIENT_MESSAGE
    from app.services.prompt_builder import _FAQ_PATH, _PROMPT_PATH

    prompt = _PROMPT_PATH.read_text()
    sections = [
        ("availability", "Наличие", "Клиент спрашивает наличие", "Наличие товара — уточняет менеджер"),
        ("delivery", "Доставка", "Клиент спрашивает доставку", "Доставка — как отвечать"),
        ("variants", "Варианты модели", "Клиент не уточнил цвет или размер", "Если есть несколько вариантов модели (разные цвета, размеры) — спрашивай уточнение"),
        ("handover", "Передача менеджеру", "Запрос требует менеджера", "Когда передавать менеджеру"),
    ]
    scenarios = []
    for key, name, condition, heading in sections:
        match = re.search(r"(?m)^#{2,3} " + re.escape(heading) + r"\n(.*?)(?=\n#{2,3} |\Z)", prompt, re.S)
        if match:
            paragraphs = [p.strip() for p in match[1].split("\n\n") if p.strip()]
            steps = [p[i:i + 1900] for p in paragraphs for i in range(0, len(p), 1900)][:20]
            scenarios.append(Scenario(id=key, name=name, condition=condition, steps=steps))
    return Behavior(
        system_prompt=prompt, faq=_FAQ_PATH.read_text(),
        templates=[Template(id="handover", name="Текущая фраза передачи менеджеру", text=ESCALATION_CLIENT_MESSAGE)],
        scenarios=scenarios,
    ).model_dump()


def behavior_state() -> dict:
    state = read_json(store_root() / "behavior.json")
    if state is None:
        default = defaults()
        return {"revision": "initial", "active": default, "draft": default, "history": [], "published": False}
    return state


def active_behavior() -> dict | None:
    state = read_json(store_root() / "behavior.json")
    return state.get("active") if state and state.get("published") else None


def render_behavior(behavior: dict, table: str, updated: str) -> str:
    content = Behavior.model_validate(behavior)
    text = content.system_prompt.replace("{{ SKUS_TABLE }}", table).replace("{{ UPDATED_DATE }}", updated)
    text += "\n\n# Условия магазина и FAQ\n" + content.faq
    text += (
        "\n\n# Настроенные сценарии общения\n"
        "Выбери подходящий включённый сценарий по смыслу запроса и истории. "
        "Выполняй шаги последовательно, учитывая ответы клиента; не переспрашивай известное. "
        "Условия ветвления написаны в шагах. Шаблоны — образцы формулировок; "
        "подставляй факты только из карточек товара и условий магазина. "
        "Если факта нет, уточни у клиента или менеджера. Не показывай имена сценариев и маркеры. "
        "При финальной передаче используй ESCALATE: причина. "
        "Сценарии уточняют базовую инструкцию, но не отменяют программные фильтры.\n"
    )
    text += json.dumps({
        "templates": [t.model_dump() for t in content.templates if t.enabled],
        "scenarios": [s.model_dump() for s in content.scenarios if s.enabled],
    }, ensure_ascii=False)
    return text


def mutate_behavior(expected: str, action: str, behavior: dict | None = None, version: str = "") -> dict:
    with locked():
        state = behavior_state()
        if state["revision"] != expected:
            raise FileExistsError("Настройки изменились. Обновите страницу перед сохранением.")
        if action == "save":
            state["draft"] = Behavior.model_validate(behavior).model_dump()
        else:
            if action == "restore":
                old = next((h for h in state["history"] if h["id"] == identifier(version)), None)
                if old is None:
                    raise FileNotFoundError("Версия не найдена")
                state["draft"] = old["behavior"]
            elif action == "publish":
                state["history"].insert(0, {"id": uuid.uuid4().hex, "behavior": state["active"]})
                state["history"] = state["history"][:20]
                state["active"] = state["draft"]
                state["published"] = True
            elif action == "baseline":
                state["draft"] = state["active"] if state.get("published") else defaults()
            else:
                raise ValueError("Неизвестное действие")
        state["revision"] = uuid.uuid4().hex
        atomic_json(store_root() / "behavior.json", state)
        return state
