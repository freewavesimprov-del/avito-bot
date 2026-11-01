"""Server-side Yandex connection settings; credentials never enter public metadata."""

from __future__ import annotations

from pydantic import BaseModel, SecretStr

from app.config import get_settings
from app.services.studio import atomic_json, locked, read_json, store_root


class Connection(BaseModel):
    api_key: SecretStr
    folder_id: str
    model: str
    timeout: int


def connection() -> Connection:
    settings = get_settings()
    saved = read_json(store_root() / "credentials" / "yandex.json", {})
    return Connection(
        api_key=saved.get("api_key") or settings.YC_API_KEY,
        folder_id=saved.get("folder_id", settings.YC_FOLDER_ID),
        model=saved.get("model", settings.YC_MODEL), timeout=settings.YC_TIMEOUT_SEC,
    )


def metadata() -> dict:
    value = connection()
    return {"api_key_set": bool(value.api_key.get_secret_value()), "folder_id": value.folder_id,
            "model": value.model, "configured": bool(value.api_key.get_secret_value() and value.folder_id)}


def save_connection(value: Connection, replace_key: bool) -> None:
    with locked():
        directory = store_root() / "credentials"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        previous = read_json(directory / "yandex.json", {})
        payload = {"folder_id": value.folder_id, "model": value.model}
        if replace_key:
            payload["api_key"] = value.api_key.get_secret_value()
        elif previous.get("api_key"):
            payload["api_key"] = previous["api_key"]
        # atomic_json creates the file with mode 0600, including on replacement.
        atomic_json(directory / "yandex.json", payload)
