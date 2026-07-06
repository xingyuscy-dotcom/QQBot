import json
from typing import Any

from .paths import CONFIG_LOCAL_PATH
from .prompts import DEFAULT_GLOBAL_SYSTEM_PROMPT


DEFAULT_SETTINGS = {
    "llm.base_url": "https://api.deepseek.com",
    "llm.api_key": "",
    "llm.model": "deepseek-v4-flash",
    "llm.temperature": "0.8",
    "llm.max_tokens": "800",
    "bot.global_system_prompt": DEFAULT_GLOBAL_SYSTEM_PROMPT,
    "bot.manager_qqs": "",
    "bot.memory_batch_size": "40",
    "bot.test_reply_enabled": "1",
    "onebot.ws_path": "/onebot/ws",
    "onebot.listen_host": "0.0.0.0",
    "onebot.listen_port": "6199",
    "admin.listen_port": "6185",
}


def get_settings() -> dict[str, str]:
    settings = dict(DEFAULT_SETTINGS)
    settings.update(_load_db_settings())
    settings.update(_load_local_settings())
    return settings


def set_setting(key: str, value: str) -> None:
    local_settings = _load_local_settings()
    local_settings[key] = value
    _save_local_settings(local_settings)

    # Keep SQLite in sync for old backups and older local installs.
    from .db import connect

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def ensure_local_config() -> None:
    if CONFIG_LOCAL_PATH.exists():
        return

    settings = dict(DEFAULT_SETTINGS)
    settings.update(_load_db_settings())
    _save_local_settings(settings)


def _load_db_settings() -> dict[str, str]:
    from .db import connect

    try:
        with connect() as conn:
            rows = conn.execute("SELECT key, value FROM app_settings ORDER BY key").fetchall()
    except Exception:
        return {}

    return {str(row["key"]): str(row["value"]) for row in rows}


def _load_local_settings() -> dict[str, str]:
    try:
        data = json.loads(CONFIG_LOCAL_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(data, dict):
        return {}

    settings: dict[str, str] = {}
    for key, value in data.items():
        settings[str(key)] = _stringify(value)
    return settings


def _save_local_settings(settings: dict[str, str]) -> None:
    CONFIG_LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    ordered: dict[str, str] = {}
    for key in DEFAULT_SETTINGS:
        ordered[key] = str(settings.get(key, DEFAULT_SETTINGS[key]))
    for key in sorted(key for key in settings if key not in ordered):
        ordered[key] = str(settings[key])

    CONFIG_LOCAL_PATH.write_text(
        json.dumps(ordered, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)
