import socket
from datetime import datetime
from pathlib import Path
from typing import Any

from .db import connect, latest_bot_status, log_event
from .paths import BACKUPS_DIR, DATA_DIR, DB_PATH, LOGS_DIR, MEMORIES_DIR
from .settings import get_settings


LAST_MODEL_TEST: dict[str, Any] | None = None


def collect_health() -> dict[str, Any]:
    settings = get_settings()
    database_ok, database_detail = _check_database()
    directories = {
        "data": _check_writable_dir(DATA_DIR),
        "memories": _check_writable_dir(MEMORIES_DIR),
        "logs": _check_writable_dir(LOGS_DIR),
        "backups": _check_writable_dir(BACKUPS_DIR),
    }

    return {
        "database": {
            "ok": database_ok,
            "path": str(DB_PATH),
            "detail": database_detail,
        },
        "napcat": _check_napcat(),
        "onebot": _check_onebot_port(settings),
        "llm_config": _check_llm_config(settings),
        "directories": directories,
        "model_test": LAST_MODEL_TEST,
    }


def test_model_connection() -> dict[str, Any]:
    global LAST_MODEL_TEST

    from .llm_client import LLMError, chat_completion

    settings = get_settings()
    test_settings = dict(settings)
    test_settings["llm.temperature"] = "0"
    test_settings["llm.max_tokens"] = "128"
    messages = [
        {"role": "system", "content": "你是连接测试助手，只回答 OK。"},
        {"role": "user", "content": "ping"},
    ]

    started_at = datetime.now().isoformat(timespec="seconds")
    try:
        reply = chat_completion(test_settings, messages)
    except LLMError as exc:
        LAST_MODEL_TEST = {
            "ok": False,
            "tested_at": started_at,
            "detail": str(exc)[:800],
        }
        log_event("error", "health", "model test failed", LAST_MODEL_TEST["detail"])
        return LAST_MODEL_TEST

    LAST_MODEL_TEST = {
        "ok": True,
        "tested_at": started_at,
        "detail": reply[:120],
    }
    log_event("info", "health", "model test ok", LAST_MODEL_TEST["detail"])
    return LAST_MODEL_TEST


def format_health_report(health: dict[str, Any]) -> str:
    dirs = health.get("directories") or {}
    bad_dirs = [name for name, item in dirs.items() if not item.get("ok")]
    llm_config = health.get("llm_config") or {}
    model_test = health.get("model_test") or {}

    lines = [
        "健康检查：",
        f"NapCat：{_ok_text((health.get('napcat') or {}).get('connected'))}",
        f"OneBot 端口：{_ok_text((health.get('onebot') or {}).get('listening'))}",
        f"数据库：{_ok_text((health.get('database') or {}).get('ok'))}",
        f"目录写入：{'异常：' + '、'.join(bad_dirs) if bad_dirs else '正常'}",
        f"模型配置：{_ok_text(llm_config.get('ok'))}",
    ]
    if not llm_config.get("ok"):
        missing = llm_config.get("missing") or []
        lines.append("缺少配置：" + "、".join(missing))

    if model_test:
        lines.append(f"最近模型测试：{_ok_text(model_test.get('ok'))} {model_test.get('tested_at', '')}")
    else:
        lines.append("最近模型测试：未测试")

    return "\n".join(lines)


def _check_database() -> tuple[bool, str]:
    try:
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:
        return False, str(exc)[:300]
    return True, "ok"


def _check_napcat() -> dict[str, Any]:
    latest = latest_bot_status()
    connected = bool(latest and int(latest.get("connected") or 0) == 1)
    return {
        "connected": connected,
        "latest_bot": latest,
    }


def _check_onebot_port(settings: dict[str, str]) -> dict[str, Any]:
    port = _parse_int(settings.get("onebot.listen_port"), 6199)
    path = settings.get("onebot.ws_path", "/onebot/ws")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            listening = True
            detail = "ok"
    except OSError as exc:
        listening = False
        detail = str(exc)[:300]

    return {
        "listening": listening,
        "address": f"ws://127.0.0.1:{port}{path}",
        "detail": detail,
    }


def _check_llm_config(settings: dict[str, str]) -> dict[str, Any]:
    required = {
        "Base URL": settings.get("llm.base_url", "").strip(),
        "API Key": settings.get("llm.api_key", "").strip(),
        "Model": settings.get("llm.model", "").strip(),
    }
    missing = [name for name, value in required.items() if not value]
    return {
        "ok": not missing,
        "missing": missing,
        "base_url": settings.get("llm.base_url", ""),
        "model": settings.get("llm.model", ""),
        "api_key_saved": bool(settings.get("llm.api_key", "").strip()),
    }


def _check_writable_dir(path: Path) -> dict[str, Any]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        test_file = path / ".healthcheck.tmp"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
    except OSError as exc:
        return {
            "ok": False,
            "path": str(path),
            "detail": str(exc)[:300],
        }

    return {
        "ok": True,
        "path": str(path),
        "detail": "ok",
    }


def _parse_int(value: str | None, fallback: int) -> int:
    try:
        return int(value) if value is not None else fallback
    except ValueError:
        return fallback


def _ok_text(value: Any) -> str:
    return "正常" if bool(value) else "异常"
