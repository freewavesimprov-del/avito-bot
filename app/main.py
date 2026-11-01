"""FastAPI-приложение: /health + webhook-приёмник событий Bitrix24.

Обработки бизнес-логики здесь нет — только:
  1) приём события,
  2) аутентификация по application_token,
  3) логирование в SQLite.
Конкретные обработчики будут подключены в Stage 2+ (handlers/message.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.config import get_settings
from app.services import weekday_owner
from app.models import B24EventBase, extract_dialog_id
from app.services.b24_install import get_application_token, save_install
from app.services.logger import (
    dispose_db,
    ensure_dialog,
    init_db,
    log_event,
    mark_operator_led,
)

APP_VERSION = "0.1.0"

logger = logging.getLogger("avito_bot")


def _configure_logging() -> None:
    level = getattr(logging, get_settings().LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


# Активные фоновые обработчики — отслеживаем для graceful shutdown.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


async def _run_handler_safely(event: B24EventBase) -> None:
    """Fire-and-forget обёртка над handle_message_add с логированием ошибок."""
    from app.handlers.message import handle_message_add

    try:
        result = await handle_message_add(event)
        logger.info("handler bg: %s", result)
    except Exception:
        logger.exception("handler bg: unhandled error processing %s", event.event)


def _spawn_bg_task(coro) -> None:
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def wait_for_background_tasks(timeout: float = 10.0) -> None:
    """Ждать завершения текущих фоновых обработчиков. Используется в тестах."""
    if not _BACKGROUND_TASKS:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*list(_BACKGROUND_TASKS), return_exceptions=True),
            timeout=timeout,
        )
    except TimeoutError:
        logger.warning("wait_for_background_tasks: timeout")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    await init_db()
    logger.info("avito-bot %s started", APP_VERSION)
    # Avito-поллер заказов (опциональный, включается через AVITO_WATCHER_ENABLE)
    from app.services.avito_orders_watcher import watcher as avito_watcher
    from app.services.lead_router import router as lead_router
    from app.services.ol_queue import keeper as ol_queue
    from app.services.ol_watchdog import watchdog as ol_watchdog
    await avito_watcher.start()
    await ol_watchdog.start()
    await ol_queue.start()
    await lead_router.start()
    await weekday_owner.resume_pending()
    try:
        yield
    finally:
        await ol_watchdog.stop()
        await ol_queue.stop()
        await lead_router.stop()
        await avito_watcher.stop()
        if _BACKGROUND_TASKS:
            logger.info("waiting for %d background tasks", len(_BACKGROUND_TASKS))
            try:
                await asyncio.wait_for(
                    asyncio.gather(*_BACKGROUND_TASKS, return_exceptions=True),
                    timeout=45.0,
                )
            except TimeoutError:
                logger.warning("some background tasks did not finish in 45s")
        await dispose_db()
        logger.info("avito-bot %s stopped", APP_VERSION)


app = FastAPI(title="avito-bot", version=APP_VERSION, lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": APP_VERSION, "env": get_settings().APP_ENV}


_dev_routes_registered = False


def maybe_register_dev_routes() -> bool:
    """Подключаем /dev/chat только в APP_ENV=dev. В проде роутов просто нет.

    Идемпотентна: повторный вызов не добавит дубликаты. Используется также в тестах —
    можно переключить env и явно вызвать.
    """
    global _dev_routes_registered
    if _dev_routes_registered:
        return True
    settings = get_settings()
    if settings.APP_ENV != "dev":
        return False
    from app.routes.dev import router as dev_router

    app.include_router(dev_router)
    _dev_routes_registered = True
    logger.warning("DEV routes enabled at /dev/chat (APP_ENV=dev)")
    return True


maybe_register_dev_routes()


# Admin-роуты подключаем всегда — они защищены Basic Auth.
# Если ADMIN_USER / ADMIN_PASSWORD пустые в .env — эндпоинты вернут 503.
def _register_admin_routes() -> None:
    from app.routes.admin import router as admin_router

    from app.routes.studio import router as studio_router

    app.include_router(admin_router)
    app.include_router(studio_router)


_register_admin_routes()


def _register_webhook_route() -> None:
    """Регистрируем webhook по пути из настроек (WEBHOOK_PATH), чтобы он был конфигурируем."""

    path = get_settings().WEBHOOK_PATH

    @app.get(path)
    async def b24_webhook_get() -> JSONResponse:
        """B24 / browser открыли webhook URL → отдаём 200 OK, чтобы health-check'и
        не считали хэндлер сломанным (B24 в этом случае может отключить event delivery)."""
        return JSONResponse(
            {"status": "ok", "info": "Avito-bot B24 webhook endpoint. POST events here."}
        )

    @app.post(path)
    async def b24_webhook(request: Request) -> JSONResponse:
        # B24 шлёт и application/json, и application/x-www-form-urlencoded. Поддерживаем оба.
        raw_body = await request.body()
        content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()

        payload: dict[str, Any]
        try:
            if content_type == "application/json":
                payload = json.loads(raw_body.decode("utf-8") or "{}")
            else:
                form = await request.form()
                payload = _form_to_nested(dict(form))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("webhook: invalid body (%s)", exc)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="invalid body"
            ) from exc

        # B24 вызывает наш URL не только на event-callback'и, но и:
        # - при открытии приложения в UI (PLACEMENT-запрос с APPLICATION_TOKEN/AUTH_ID/REFRESH_ID)
        # - при OAuth-инсталляции (поля auth[*])
        # - при healthcheck'ах
        # ВАЖНО: PLACEMENT-запросы содержат СВЕЖИЕ токены приложения. B24 ротирует их
        # при переустановке handler'ов — если не сохранить, наш сервер не сможет
        # аутентифицировать последующие events (старый application_token уже невалиден).
        if not isinstance(payload, dict) or not payload.get("event"):
            keys = sorted(list(payload.keys())) if isinstance(payload, dict) else []
            # Если в payload есть APPLICATION_TOKEN — это свежие токены, сохраняем
            if isinstance(payload, dict) and payload.get("APPLICATION_TOKEN"):
                save_install(
                    {
                        "domain": payload.get("DOMAIN")
                        or request.query_params.get("DOMAIN")
                        or "",
                        "access_token": payload.get("AUTH_ID") or "",
                        "refresh_token": payload.get("REFRESH_ID") or "",
                        "application_token": payload.get("APPLICATION_TOKEN") or "",
                        "expires_in": int(payload.get("AUTH_EXPIRES") or 0),
                        "member_id": payload.get("member_id") or "",
                    }
                )
                logger.warning(
                    "webhook: PLACEMENT/install POST captured fresh tokens (member_id=%s)",
                    payload.get("member_id"),
                )
            else:
                logger.info("webhook: non-event POST — 200 OK; keys=%s", keys)
            return JSONResponse(
                {"status": "ok", "info": "handler is alive, no event payload"}
            )

        try:
            event = B24EventBase.model_validate(payload)
        except ValidationError as exc:
            # Свой неизвестный event тип — логируем но не валим (200), чтобы B24
            # не помечал handler сломанным.
            logger.warning("webhook: cannot parse event :: %s", exc.errors()[:3])
            return JSONResponse(
                {"status": "ok", "info": "unknown event shape, ignored"}
            )

        # --- Аутентификация ---
        # Спецслучай: ONAPPINSTALL / ONAPPUPDATE — пришли при первой установке
        # приложения на портал. application_token мы тогда ещё не знаем —
        # как раз ИЗ этого события его и забираем. Если в .env пусто и в install-файле тоже —
        # мы доверяем подписи через client_secret приложения... но для локальных
        # B24-приложений client_secret в payload не приходит. Поэтому ВПЕРВЫЕ
        # принимаем install безусловно, а сразу после — все остальные события
        # уже сверяем со свежесохранённым application_token.
        incoming_token = event.auth.application_token
        is_install_event = event.event in {"ONAPPINSTALL", "ONAPPUPDATE"}
        expected = get_application_token()  # из install-файла или .env

        if is_install_event:
            # Сохраняем токены из payload — со следующего вебхука уже валидируем строго.
            saved = save_install(event.auth.model_dump())
            logger.warning(
                "webhook: %s captured (domain=%s, has_app_token=%s)",
                event.event,
                saved.domain,
                bool(saved.application_token),
            )
        elif not incoming_token or not expected or incoming_token != expected:
            logger.warning(
                "webhook: application_token mismatch (event=%s, has_token=%s, expected_set=%s)",
                event.event,
                bool(incoming_token),
                bool(expected),
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="application_token invalid"
            )

        # --- Минимальная маршрутизация + логирование ---
        dialog_id = extract_dialog_id(event) or "unknown"
        meta = {
            "event": event.event,
            "domain": event.auth.domain,
            "user_id": _safe_user_id(event),
            "raw_data_keys": sorted(list((event.data or {}).keys())),
        }

        # Базовая поддержка системных событий — без отправки ответа клиенту.
        match event.event:
            case "ONIMBOTJOINCHAT":
                # B24 свежо пригласил бота в чат = новая сессия (новое «обращение»
                # клиента в Avito). Сбрасываем operator_led, накопленный за прошлые
                # сессии этого же dialog_id (Avito переиспользует chat<id> для пары
                # клиент+объявление, поэтому без сброса бот молчал бы навсегда после
                # первой эскалации).
                if dialog_id != "unknown":
                    await ensure_dialog(dialog_id)
                    await mark_operator_led(dialog_id, False)
                await log_event(
                    dialog_id, "system", f"bot_joined: {event.event} (operator_led reset)", meta=meta
                )
                # Будни: диалог сразу адресуем магазину, а не ждём, пока линия
                # через 10 минут разошлёт его всей очереди (30.09.2026).
                _spawn_bg_task(weekday_owner.hand_over(dialog_id))
            case "ONIMBOTDELETE":
                if dialog_id != "unknown":
                    await mark_operator_led(dialog_id, True)
                await log_event(
                    dialog_id, "system", f"bot_removed: {event.event}", meta=meta
                )
            case "ONAPPINSTALL":
                await log_event(
                    dialog_id,
                    "system",
                    "app_installed",
                    meta={**meta, "has_app_token": bool(incoming_token)},
                )
            case "ONIMBOTMESSAGEADD":
                # Stage 6.5: запускаем обработку в фоне (B24 таймаутит вебхуки за ~10с,
                # а у нас debounce 30с + LLM 2-3с).
                _spawn_bg_task(_run_handler_safely(event))
            case _:
                await log_event(dialog_id, "system", f"received: {event.event}", meta=meta)

        return JSONResponse({"status": "accepted", "event": event.event})


def _extract_message_text(event: B24EventBase) -> str:
    data = event.data or {}
    params = data.get("PARAMS")
    if isinstance(params, dict):
        msg = params.get("MESSAGE")
        if isinstance(msg, str):
            return msg
    return ""


def _safe_user_id(event: B24EventBase) -> str | None:
    data = event.data or {}
    user = data.get("USER")
    if isinstance(user, dict):
        uid = user.get("ID")
        if uid is not None:
            return str(uid)
    params = data.get("PARAMS")
    if isinstance(params, dict):
        uid = params.get("FROM_USER_ID")
        if uid is not None:
            return str(uid)
    return None


def _form_to_nested(form: dict[str, Any]) -> dict[str, Any]:
    """Примитивный парсер form-urlencoded с PHP-style ключами типа 'auth[application_token]'.

    Этого достаточно для B24 bot-events — глубина обычно до 3 уровней.
    """
    out: dict[str, Any] = {}
    for key, value in form.items():
        parts = _split_php_key(key)
        _insert_nested(out, parts, value)
    return out


def _split_php_key(key: str) -> list[str]:
    # "auth[application_token]" -> ["auth", "application_token"]
    # "data[PARAMS][MESSAGE]"   -> ["data", "PARAMS", "MESSAGE"]
    if "[" not in key:
        return [key]
    head, _, rest = key.partition("[")
    tail = rest.rstrip("]").replace("][", "\x00").split("\x00")
    return [head, *tail]


def _insert_nested(target: dict[str, Any], parts: list[str], value: Any) -> None:
    cur: Any = target
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


_register_webhook_route()
