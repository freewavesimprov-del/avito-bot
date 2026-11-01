"""Интеграционные тесты chat_engine.process_message — полный pipeline.

Подменяем LLM на стуб через monkeypatch — нужен только ответ функции _generate.
"""

from __future__ import annotations

import os

import pytest

from app.services import chat_engine
from app.services.filters import ESCALATION_CLIENT_MESSAGE
from app.services.logger import dispose_db, init_db


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    os.environ.setdefault("B24_APPLICATION_TOKEN", "t")
    os.environ.setdefault("B24_DOMAIN", "test.bitrix24.ru")
    os.environ.setdefault("YC_API_KEY", "k")
    os.environ.setdefault("YC_FOLDER_ID", "f")
    os.environ["MOCK_LLM"] = "false"  # хотим контролировать ответ через monkeypatch
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _db():
    # Сбрасываем module-level engine, чтобы каждый тест работал с новой tmp-БД.
    # Без dispose_db() init_db() делает early-return и переиспользует engine
    # от прошлого теста (с уже не существующим tmp-файлом).
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


def _stub_llm(reply: str):
    async def _fake(_text: str, _history: list[dict], _ad_note: str = "") -> str:
        return reply

    return _fake


@pytest.mark.asyncio
async def test_normal_flow_clean_reply(monkeypatch):
    monkeypatch.setattr(chat_engine, "_generate", _stub_llm("Здравствуйте, чем могу помочь?"))
    r = await chat_engine.process_message("d-clean", "u", "Привет!")
    assert r.action == "sent"
    assert r.text == "Здравствуйте, чем могу помочь?"
    assert r.reason is None
    assert r.blocked_text is None


@pytest.mark.asyncio
async def test_llm_escalate_marker_is_parsed(monkeypatch):
    monkeypatch.setattr(
        chat_engine,
        "_generate",
        _stub_llm("Скидки согласует менеджер, передам запрос. ESCALATE: запрос скидки"),
    )
    r = await chat_engine.process_message("d-esc", "u", "А скидку дадите?")
    assert r.action == "escalated"
    assert "ESCALATE" not in (r.text or "")
    assert "Скидки согласует менеджер" in (r.text or "")
    assert r.reason == "запрос скидки"


@pytest.mark.asyncio
async def test_post_filter_blocks_phone(monkeypatch):
    monkeypatch.setattr(
        chat_engine, "_generate", _stub_llm("Звоните +7 495 123-45-67, поможем")
    )
    r = await chat_engine.process_message("d-block", "u", "Дайте телефончик…")
    # Hard-trigger 'manager' сработает раньше — это нормально, но тогда LLM не вызывается.
    # Чтобы проверить именно post-filter, обходим триггер невинным вопросом про подушку:
    monkeypatch.setattr(
        chat_engine, "_generate", _stub_llm("Звоните +7 495 123-45-67")
    )
    r = await chat_engine.process_message("d-block2", "u", "А есть подушки декоративные?")
    assert r.action == "blocked"
    assert r.text == ESCALATION_CLIENT_MESSAGE
    assert r.reason == "phone"
    assert r.blocked_text is not None
    assert "+7 495" in r.blocked_text


@pytest.mark.asyncio
async def test_hard_trigger_complaint_skips_llm(monkeypatch):
    """LLM не должен вызываться, если сработал hard-trigger."""
    called = {"n": 0}

    async def _spy(_text, _history, _ad_note=""):
        called["n"] += 1
        return "это не должно попасть в ответ"

    monkeypatch.setattr(chat_engine, "_generate", _spy)
    r = await chat_engine.process_message("d-trig", "u", "Прислали с браком, ужас")
    assert r.action == "escalated"
    assert r.text == ESCALATION_CLIENT_MESSAGE
    assert "complaint" in (r.reason or "")
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_operator_led_silences_bot(monkeypatch):
    from app.services.logger import mark_operator_led

    await mark_operator_led("d-op", True)
    monkeypatch.setattr(chat_engine, "_generate", _stub_llm("не должен вызваться"))
    r = await chat_engine.process_message("d-op", "u", "Здравствуйте")
    assert r.action == "silent"
    assert r.reason == "operator_led"


@pytest.mark.asyncio
async def test_empty_input_is_silent(monkeypatch):
    monkeypatch.setattr(chat_engine, "_generate", _stub_llm("never"))
    r = await chat_engine.process_message("d-empty", "u", "   ")
    assert r.action == "silent"
    assert r.reason == "empty_input"


@pytest.mark.asyncio
async def test_loose_escalate_form_is_handled_not_leaked(monkeypatch):
    """LLM пишет 'передам менеджеру. ESCALATE' (без причины) — парсер должен
    распознать как маркер с reason='unspecified', а не уйти в leaked_marker."""
    monkeypatch.setattr(
        chat_engine, "_generate", _stub_llm("Передам менеджеру для уточнения. ESCALATE")
    )
    r = await chat_engine.process_message("d-loose", "u", "Какие матрасы есть?")
    assert r.action == "escalated"
    assert r.reason == "unspecified"
    assert "ESCALATE" not in (r.text or "")




@pytest.mark.asyncio
async def test_repeat_loop_triggers_escalation(monkeypatch):
    """Если клиент задаёт один и тот же вопрос 3+ раз — эскалация без LLM."""
    calls = {"n": 0}

    async def _spy(text, history, _ad_note=""):
        calls["n"] += 1
        return "обычный ответ"

    monkeypatch.setattr(chat_engine, "_generate", _spy)

    # 1-й и 2-й — обычные ответы
    r1 = await chat_engine.process_message("d-rep", "u", "Есть Ангара?")
    r2 = await chat_engine.process_message("d-rep", "u", "Есть Ангара?")
    assert r1.action == "sent"
    assert r2.action == "sent"
    # 3-й (тот же текст) — должен эскалироваться без вызова LLM
    calls_before = calls["n"]
    r3 = await chat_engine.process_message("d-rep", "u", "Есть Ангара?")
    assert r3.action == "escalated"
    assert r3.reason and r3.reason.startswith("repeat_loop")
    # LLM на 3-м не вызывалась
    assert calls["n"] == calls_before


@pytest.mark.asyncio
async def test_repeat_loop_normalizes_punctuation(monkeypatch):
    """Похожие сообщения с разной пунктуацией/регистром считаются одним вопросом."""

    async def _stub(text, history, _ad_note=""):
        return "ок"

    monkeypatch.setattr(chat_engine, "_generate", _stub)

    await chat_engine.process_message("d-norm", "u", "Есть Ангара?")
    await chat_engine.process_message("d-norm", "u", "ЕСТЬ АНГАРА")
    r = await chat_engine.process_message("d-norm", "u", "есть ангара!!!")
    assert r.action == "escalated"
    assert "repeat_loop" in (r.reason or "")


@pytest.mark.asyncio
async def test_after_any_escalation_bot_is_silent(monkeypatch):
    """После любой эскалации (hard-trigger / LLM-маркер / post-filter) бот должен
    в этом диалоге больше ничего не отвечать — operator_led=True."""

    async def _llm_with_escalate(text, history, _ad_note=""):
        return "Передам менеджеру.\nESCALATE: проверка"

    monkeypatch.setattr(chat_engine, "_generate", _llm_with_escalate)

    r1 = await chat_engine.process_message("d-stop", "u", "Какой-то вопрос")
    assert r1.action == "escalated"

    # Подменяем LLM чтобы убедиться: даже если LLM теперь вернул бы что-то — бот молчит
    async def _should_not_be_called(text, history, _ad_note=""):
        raise AssertionError("LLM не должна вызываться после эскалации")

    monkeypatch.setattr(chat_engine, "_generate", _should_not_be_called)
    r2 = await chat_engine.process_message("d-stop", "u", "Ещё вопрос")
    assert r2.action == "silent"
    assert r2.reason == "operator_led"


@pytest.mark.asyncio
async def test_after_hard_trigger_bot_is_silent(monkeypatch):
    """То же самое для hard-trigger (например, complaint)."""

    async def _no_llm(text, history, _ad_note=""):
        raise AssertionError("LLM не должна вызываться")

    monkeypatch.setattr(chat_engine, "_generate", _no_llm)

    r1 = await chat_engine.process_message("d-trig", "u", "Прислали с браком!")
    assert r1.action == "escalated"
    r2 = await chat_engine.process_message("d-trig", "u", "Когда возврат?")
    assert r2.action == "silent"


@pytest.mark.asyncio
async def test_history_passed_to_llm(monkeypatch):
    """Проверяем, что в LLM уезжает накопленная история диалога."""
    seen_history: list[dict] = []

    async def _capture(text, history, _ad_note=""):
        seen_history.extend(history)
        return "ок"

    monkeypatch.setattr(chat_engine, "_generate", _capture)

    await chat_engine.process_message("d-hist", "u", "Здравствуйте")
    seen_history.clear()
    await chat_engine.process_message("d-hist", "u", "А по диванам что есть?")
    # Во втором вызове история должна содержать предыдущие in/out
    assert any("Здравствуйте" in h.get("message", "") for h in seen_history)
    assert any(h.get("direction") == "out" for h in seen_history)
