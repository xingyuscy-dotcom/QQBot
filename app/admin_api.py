import json
from zipfile import BadZipFile
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .backup_store import create_backup, inspect_backup, list_backups, restore_backup
from .db import (
    connect,
    get_conversation_config,
    get_recent_conversation_messages,
    latest_bot_status,
    log_event,
    now_text,
    update_conversation_learning,
    update_conversation_persona,
)
from .health_check import collect_health, test_model_connection
from .memory_store import load_memory, replace_manager_memory, reset_pending_message_count
from .paths import BACKUPS_DIR, COMMANDS_PATH, DB_PATH, LOGS_DIR
from .prompts import DEFAULT_GLOBAL_SYSTEM_PROMPT
from .settings import get_settings, set_setting


router = APIRouter(prefix="/api")


class ConversationEnabledPayload(BaseModel):
    enabled: bool


class ConversationLearningPayload(BaseModel):
    learning_enabled: bool
    learning_batch_size: int | None = None
    learned_memory_weight: float | None = None


class ConversationReplyConfigPayload(BaseModel):
    response_mode: str
    trigger_prefix: str = ""
    reply_cooldown_seconds: int = 0
    reply_probability: float = 1
    hourly_reply_limit: int = 0


class AppSettingsPayload(BaseModel):
    llm_base_url: str
    llm_api_key: str = ""
    llm_model: str
    llm_temperature: str
    llm_max_tokens: str
    bot_global_system_prompt: str
    bot_manager_qqs: str = ""
    bot_memory_batch_size: str = "40"


class ConversationPersonaPayload(BaseModel):
    persona: str = ""


class ConversationMemoryPayload(BaseModel):
    manager_memory_text: str = ""


class ConversationDebugPayload(BaseModel):
    test_text: str = ""


class BackupRestorePayload(BaseModel):
    confirm_text: str = ""


class CommandLibraryPayload(BaseModel):
    content: str


@router.get("/status")
def status() -> dict:
    with connect() as conn:
        bot_count = conn.execute("SELECT COUNT(*) AS c FROM bot_status").fetchone()["c"]
        conversation_count = conn.execute(
            "SELECT COUNT(*) AS c FROM conversation_configs"
        ).fetchone()["c"]
        group_count = conn.execute(
            "SELECT COUNT(*) AS c FROM conversation_configs WHERE scope_type = 'group'"
        ).fetchone()["c"]
        private_count = conn.execute(
            "SELECT COUNT(*) AS c FROM conversation_configs WHERE scope_type = 'private'"
        ).fetchone()["c"]
        message_count = conn.execute("SELECT COUNT(*) AS c FROM conversation_messages").fetchone()["c"]
        log_count = conn.execute("SELECT COUNT(*) AS c FROM runtime_logs").fetchone()["c"]
        connected_bot_count = conn.execute(
            "SELECT COUNT(*) AS c FROM bot_status WHERE connected = 1"
        ).fetchone()["c"]

    settings = get_settings()
    bot_status = latest_bot_status()
    return {
        "service": "running",
        "database": str(DB_PATH),
        "database_exists": DB_PATH.exists(),
        "bot_count": bot_count,
        "connected_bot_count": connected_bot_count,
        "latest_bot": bot_status,
        "conversation_count": conversation_count,
        "group_count": group_count,
        "private_count": private_count,
        "message_count": message_count,
        "log_count": log_count,
        "admin_port": settings.get("admin.listen_port", "6185"),
        "onebot_port": settings.get("onebot.listen_port", "6199"),
        "onebot_path": settings.get("onebot.ws_path", "/onebot/ws"),
    }


@router.get("/health")
def health() -> dict:
    return collect_health()


@router.post("/health/model-test")
def model_test() -> dict:
    return {
        "ok": True,
        "result": test_model_connection(),
    }


@router.get("/logs")
def logs(limit: int = Query(default=100, ge=1, le=500)) -> dict:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, level, scope, message, detail, created_at
            FROM runtime_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return {
        "items": [dict(row) for row in rows],
        "logs_dir": str(LOGS_DIR),
    }


@router.get("/backups")
def backups() -> dict:
    return {
        "items": list_backups(),
        "backups_dir": str(BACKUPS_DIR),
    }


@router.post("/backups")
def create_backup_api() -> dict:
    try:
        backup = create_backup()
    except OSError as exc:
        log_event("error", "backup", "backup failed", str(exc)[:800])
        raise HTTPException(status_code=400, detail=f"备份失败：{exc}") from exc

    log_event("info", "backup", "backup created", backup["path"])
    return {"ok": True, "backup": backup}


@router.get("/backups/{name}")
def inspect_backup_api(name: str) -> dict:
    try:
        return inspect_backup(name)
    except (OSError, ValueError, BadZipFile) as exc:
        raise HTTPException(status_code=400, detail=f"读取备份失败：{exc}") from exc


@router.post("/backups/{name}/restore")
def restore_backup_api(name: str, payload: BackupRestorePayload) -> dict:
    if payload.confirm_text.strip() != "RESTORE":
        raise HTTPException(status_code=400, detail="请输入 RESTORE 确认恢复。")

    try:
        result = restore_backup(name)
    except (OSError, ValueError, BadZipFile) as exc:
        log_event("error", "backup", "restore failed", str(exc)[:800])
        raise HTTPException(status_code=400, detail=f"恢复失败：{exc}") from exc

    log_event("warning", "backup", "backup restored", result["backup"]["path"])
    return {"ok": True, **result}


@router.get("/settings")
def read_settings() -> dict:
    settings = get_settings()
    return {
        "llm_base_url": settings.get("llm.base_url", "https://api.deepseek.com"),
        "llm_model": settings.get("llm.model", "deepseek-v4-flash"),
        "llm_temperature": settings.get("llm.temperature", "0.8"),
        "llm_max_tokens": settings.get("llm.max_tokens", "800"),
        "bot_global_system_prompt": settings.get(
            "bot.global_system_prompt",
            DEFAULT_GLOBAL_SYSTEM_PROMPT,
        ) or DEFAULT_GLOBAL_SYSTEM_PROMPT,
        "bot_manager_qqs": settings.get("bot.manager_qqs", ""),
        "bot_memory_batch_size": settings.get("bot.memory_batch_size", "40"),
        "api_key_saved": bool(settings.get("llm.api_key", "").strip()),
    }


@router.patch("/settings")
def update_settings(payload: AppSettingsPayload) -> dict:
    items = {
        "llm.base_url": payload.llm_base_url.strip(),
        "llm.model": payload.llm_model.strip(),
        "llm.temperature": payload.llm_temperature.strip(),
        "llm.max_tokens": payload.llm_max_tokens.strip(),
        "bot.global_system_prompt": payload.bot_global_system_prompt.strip(),
        "bot.manager_qqs": payload.bot_manager_qqs.strip(),
        "bot.memory_batch_size": payload.bot_memory_batch_size.strip() or "40",
    }
    if not items["llm.base_url"]:
        raise HTTPException(status_code=400, detail="llm_base_url is required")
    if not items["llm.model"]:
        raise HTTPException(status_code=400, detail="llm_model is required")

    for key, value in items.items():
        set_setting(key, value)

    api_key = payload.llm_api_key.strip()
    if api_key:
        set_setting("llm.api_key", api_key)

    return {"ok": True, "api_key_updated": bool(api_key)}


@router.get("/commands")
def read_commands() -> dict:
    try:
        content = COMMANDS_PATH.read_text(encoding="utf-8")
        items = json.loads(content)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"读取命令库失败：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"命令库 JSON 格式错误：{exc}") from exc

    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="命令库必须是 JSON 数组。")

    return {
        "ok": True,
        "path": str(COMMANDS_PATH),
        "items": items,
        "content": json.dumps(items, ensure_ascii=False, indent=2),
    }


@router.patch("/commands")
def update_commands(payload: CommandLibraryPayload) -> dict:
    raw = payload.content.strip()
    if not raw:
        raise HTTPException(status_code=400, detail="命令库内容不能为空。")

    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"JSON 格式错误：{exc}") from exc

    validate_command_library(items)
    COMMANDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    COMMANDS_PATH.write_text(
        json.dumps(items, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log_event("info", "commands", "command library updated", f"{len(items)} commands")
    return {
        "ok": True,
        "path": str(COMMANDS_PATH),
        "items": items,
        "content": json.dumps(items, ensure_ascii=False, indent=2),
    }


def validate_command_library(items) -> None:
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="命令库必须是 JSON 数组。")

    names = set()
    triggers = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail=f"第 {index} 条命令必须是对象。")

        name = str(item.get("name") or "").strip()
        trigger = str(item.get("trigger") or "").strip()
        usage = str(item.get("usage") or "").strip()
        handler = str(item.get("handler") or "").strip()
        scopes = item.get("scopes")

        if not name:
            raise HTTPException(status_code=400, detail=f"第 {index} 条命令缺少 name。")
        if not trigger:
            raise HTTPException(status_code=400, detail=f"第 {index} 条命令缺少 trigger。")
        if not usage:
            raise HTTPException(status_code=400, detail=f"第 {index} 条命令缺少 usage。")
        if not handler:
            raise HTTPException(status_code=400, detail=f"第 {index} 条命令缺少 handler。")
        if name in names:
            raise HTTPException(status_code=400, detail=f"命令 name 重复：{name}")
        if trigger in triggers:
            raise HTTPException(status_code=400, detail=f"命令 trigger 重复：{trigger}")
        if not isinstance(scopes, list) or not scopes:
            raise HTTPException(status_code=400, detail=f"第 {index} 条命令 scopes 必须是非空数组。")
        if any(scope not in {"group", "private"} for scope in scopes):
            raise HTTPException(status_code=400, detail=f"第 {index} 条命令 scopes 只能包含 group/private。")
        if not isinstance(item.get("manager_only"), bool):
            raise HTTPException(status_code=400, detail=f"第 {index} 条命令 manager_only 必须是布尔值。")

        names.add(name)
        triggers.add(trigger)


@router.get("/conversations")
def conversations() -> dict:
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
              c.bot_qq,
              c.scope_type,
              c.scope_id,
              c.display_name,
              c.enabled,
              c.response_mode,
              c.trigger_prefix,
              c.learning_enabled,
              c.learning_batch_size,
              c.learned_memory_weight,
              c.reply_cooldown_seconds,
              c.reply_probability,
              c.hourly_reply_limit,
              COUNT(DISTINCT m.id) AS message_count,
              MAX(m.created_at) AS last_message_at,
              COUNT(DISTINCT r.id) AS hourly_reply_count,
              MAX(r.created_at) AS last_reply_at
            FROM conversation_configs c
            LEFT JOIN conversation_messages m
              ON m.bot_qq = c.bot_qq
             AND m.scope_type = c.scope_type
             AND m.scope_id = c.scope_id
            LEFT JOIN conversation_reply_events r
              ON r.bot_qq = c.bot_qq
             AND r.scope_type = c.scope_type
             AND r.scope_id = c.scope_id
             AND r.created_at >= ?
            GROUP BY c.bot_qq, c.scope_type, c.scope_id
            ORDER BY
              CASE c.scope_type WHEN 'group' THEN 0 ELSE 1 END,
              CAST(c.scope_id AS INTEGER),
              c.scope_id
            """,
            (one_hour_ago,),
        ).fetchall()

    return {"items": [dict(row) for row in rows]}


@router.get("/stats/conversations")
def conversation_stats(days: int = Query(default=7, ge=1, le=30)) -> dict:
    if days not in {1, 7, 30}:
        raise HTTPException(status_code=400, detail="days only supports 1, 7, 30")

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days - 1)
    start_date = start.date().isoformat()
    labels = [
        (start + timedelta(days=offset)).date().isoformat()
        for offset in range(days)
    ]

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
              c.bot_qq,
              c.scope_type,
              c.scope_id,
              c.display_name,
              SUM(CASE WHEN m.is_bot = 0 THEN 1 ELSE 0 END) AS input_messages,
              SUM(CASE WHEN m.is_bot = 1 THEN 1 ELSE 0 END) AS output_messages
            FROM conversation_configs c
            LEFT JOIN conversation_messages m
              ON m.bot_qq = c.bot_qq
             AND m.scope_type = c.scope_type
             AND m.scope_id = c.scope_id
             AND substr(m.created_at, 1, 10) >= ?
            GROUP BY c.bot_qq, c.scope_type, c.scope_id
            ORDER BY
              CASE c.scope_type WHEN 'group' THEN 0 ELSE 1 END,
              CAST(c.scope_id AS INTEGER),
              c.scope_id
            """,
            (start_date,),
        ).fetchall()

        usage_rows = conn.execute(
            """
            SELECT
              bot_qq,
              scope_type,
              scope_id,
              COUNT(*) AS llm_requests,
              SUM(prompt_tokens) AS prompt_tokens,
              SUM(completion_tokens) AS completion_tokens,
              SUM(total_tokens) AS total_tokens
            FROM llm_usage_events
            WHERE substr(created_at, 1, 10) >= ?
            GROUP BY bot_qq, scope_type, scope_id
            """,
            (start_date,),
        ).fetchall()

        daily_rows = conn.execute(
            """
            SELECT
              bot_qq,
              scope_type,
              scope_id,
              substr(created_at, 1, 10) AS day,
              SUM(CASE WHEN is_bot = 0 THEN 1 ELSE 0 END) AS input_messages,
              SUM(CASE WHEN is_bot = 1 THEN 1 ELSE 0 END) AS output_messages
            FROM conversation_messages
            WHERE substr(created_at, 1, 10) >= ?
            GROUP BY bot_qq, scope_type, scope_id, day
            """,
            (start_date,),
        ).fetchall()

        daily_usage_rows = conn.execute(
            """
            SELECT
              bot_qq,
              scope_type,
              scope_id,
              substr(created_at, 1, 10) AS day,
              COUNT(*) AS llm_requests,
              SUM(total_tokens) AS total_tokens
            FROM llm_usage_events
            WHERE substr(created_at, 1, 10) >= ?
            GROUP BY bot_qq, scope_type, scope_id, day
            """,
            (start_date,),
        ).fetchall()

    usage_map = {
        _stats_key(row): dict(row)
        for row in usage_rows
    }
    daily_map: dict[str, dict[str, dict]] = {}
    for row in daily_rows:
        daily_map.setdefault(_stats_key(row), {})[row["day"]] = dict(row)
    daily_usage_map: dict[str, dict[str, dict]] = {}
    for row in daily_usage_rows:
        daily_usage_map.setdefault(_stats_key(row), {})[row["day"]] = dict(row)

    items = []
    totals = {
        "input_messages": 0,
        "output_messages": 0,
        "llm_requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    for row in rows:
        item = dict(row)
        key = _stats_key(row)
        usage = usage_map.get(key, {})
        item.update(
            {
                "input_messages": int(item.get("input_messages") or 0),
                "output_messages": int(item.get("output_messages") or 0),
                "llm_requests": int(usage.get("llm_requests") or 0),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
                "series": [],
            }
        )
        for label in labels:
            day_messages = daily_map.get(key, {}).get(label, {})
            day_usage = daily_usage_map.get(key, {}).get(label, {})
            item["series"].append(
                {
                    "date": label,
                    "input_messages": int(day_messages.get("input_messages") or 0),
                    "output_messages": int(day_messages.get("output_messages") or 0),
                    "llm_requests": int(day_usage.get("llm_requests") or 0),
                    "total_tokens": int(day_usage.get("total_tokens") or 0),
                }
            )

        for field in totals:
            totals[field] += int(item[field])
        items.append(item)

    return {
        "days": days,
        "labels": labels,
        "totals": totals,
        "items": items,
    }


def _stats_key(row) -> str:
    return f"{row['bot_qq']}:{row['scope_type']}:{row['scope_id']}"


@router.get("/conversations/{bot_qq}/{scope_type}/{scope_id}")
def conversation_detail(bot_qq: str, scope_type: str, scope_id: str) -> dict:
    if scope_type not in {"group", "private"}:
        raise HTTPException(status_code=400, detail="invalid scope_type")

    with connect() as conn:
        row = conn.execute(
            """
            SELECT
              bot_qq,
              scope_type,
              scope_id,
              display_name,
              learning_enabled,
              learning_batch_size,
              learned_memory_weight,
              persona,
              reply_cooldown_seconds,
              reply_probability,
              hourly_reply_limit
            FROM conversation_configs
            WHERE bot_qq = ? AND scope_type = ? AND scope_id = ?
            """,
            (bot_qq, scope_type, scope_id),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="conversation not found")

    return {
        "conversation": dict(row),
        "memory": load_memory(bot_qq, scope_type, scope_id),
    }


@router.get("/conversations/{bot_qq}/{scope_type}/{scope_id}/debug")
def conversation_debug(bot_qq: str, scope_type: str, scope_id: str) -> dict:
    from .llm_client import build_messages

    config = require_conversation_config(bot_qq, scope_type, scope_id)
    settings = get_settings()
    return {
        "recent_messages": get_recent_conversation_messages(bot_qq, scope_type, scope_id, limit=20),
        "prompt_messages": build_messages(bot_qq, scope_type, scope_id, config, settings),
    }


@router.post("/conversations/{bot_qq}/{scope_type}/{scope_id}/debug-reply")
def conversation_debug_reply(
    bot_qq: str,
    scope_type: str,
    scope_id: str,
    payload: ConversationDebugPayload,
) -> dict:
    from .llm_client import LLMError, build_messages, chat_completion

    config = require_conversation_config(bot_qq, scope_type, scope_id)
    test_text = payload.test_text.strip()
    if not test_text:
        raise HTTPException(status_code=400, detail="请输入测试消息。")

    settings = get_settings()
    messages = build_messages(bot_qq, scope_type, scope_id, config, settings, f"用户调试: {test_text}")

    try:
        reply = chat_completion(settings, messages)
    except LLMError as exc:
        log_event("error", f"debug:{scope_type}:{bot_qq}:{scope_id}", "debug reply failed", str(exc)[:800])
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_event("info", f"debug:{scope_type}:{bot_qq}:{scope_id}", "debug reply generated", test_text[:500])
    return {"ok": True, "reply": reply, "prompt_messages": messages}


@router.patch("/conversations/{bot_qq}/{scope_type}/{scope_id}/enabled")
def set_conversation_enabled(
    bot_qq: str,
    scope_type: str,
    scope_id: str,
    payload: ConversationEnabledPayload,
) -> dict:
    if scope_type not in {"group", "private"}:
        raise HTTPException(status_code=400, detail="invalid scope_type")

    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE conversation_configs
            SET enabled = ?, updated_at = ?
            WHERE bot_qq = ? AND scope_type = ? AND scope_id = ?
            """,
            (int(payload.enabled), now_text(), bot_qq, scope_type, scope_id),
        )

    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="conversation not found")

    return {"ok": True, "enabled": payload.enabled}


@router.patch("/conversations/{bot_qq}/{scope_type}/{scope_id}/learning")
def set_conversation_learning(
    bot_qq: str,
    scope_type: str,
    scope_id: str,
    payload: ConversationLearningPayload,
) -> dict:
    if scope_type not in {"group", "private"}:
        raise HTTPException(status_code=400, detail="invalid scope_type")

    batch_size = None
    if payload.learning_batch_size is not None:
        batch_size = max(0, int(payload.learning_batch_size))
        if 0 < batch_size < 10:
            raise HTTPException(status_code=400, detail="learning_batch_size must be 0 or at least 10")

    memory_weight = None
    if payload.learned_memory_weight is not None:
        memory_weight = min(1, max(0, float(payload.learned_memory_weight)))

    ok = update_conversation_learning(
        bot_qq,
        scope_type,
        scope_id,
        payload.learning_enabled,
        batch_size,
        memory_weight,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="conversation not found")

    with connect() as conn:
        row = conn.execute(
            """
            SELECT learning_enabled, learning_batch_size, learned_memory_weight
            FROM conversation_configs
            WHERE bot_qq = ? AND scope_type = ? AND scope_id = ?
            """,
            (bot_qq, scope_type, scope_id),
        ).fetchone()

    return {"ok": True, **dict(row)}


@router.patch("/conversations/{bot_qq}/{scope_type}/{scope_id}/reply-config")
def set_conversation_reply_config(
    bot_qq: str,
    scope_type: str,
    scope_id: str,
    payload: ConversationReplyConfigPayload,
) -> dict:
    if scope_type not in {"group", "private"}:
        raise HTTPException(status_code=400, detail="invalid scope_type")
    if payload.response_mode not in {"mention", "prefix", "all"}:
        raise HTTPException(status_code=400, detail="invalid response_mode")
    if scope_type == "private" and payload.response_mode != "all":
        raise HTTPException(status_code=400, detail="private conversation only supports all mode")
    if payload.response_mode == "prefix" and not payload.trigger_prefix.strip():
        raise HTTPException(status_code=400, detail="trigger_prefix is required")

    cooldown = max(0, int(payload.reply_cooldown_seconds))
    probability = min(1, max(0, float(payload.reply_probability)))
    hourly_limit = max(0, int(payload.hourly_reply_limit))

    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE conversation_configs
            SET
              response_mode = ?,
              trigger_prefix = ?,
              reply_cooldown_seconds = ?,
              reply_probability = ?,
              hourly_reply_limit = ?,
              updated_at = ?
            WHERE bot_qq = ? AND scope_type = ? AND scope_id = ?
            """,
            (
                payload.response_mode,
                payload.trigger_prefix.strip(),
                cooldown,
                probability,
                hourly_limit,
                now_text(),
                bot_qq,
                scope_type,
                scope_id,
            ),
        )

    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="conversation not found")

    return {
        "ok": True,
        "response_mode": payload.response_mode,
        "trigger_prefix": payload.trigger_prefix.strip(),
        "reply_cooldown_seconds": cooldown,
        "reply_probability": probability,
        "hourly_reply_limit": hourly_limit,
    }


@router.patch("/conversations/{bot_qq}/{scope_type}/{scope_id}/persona")
def set_conversation_persona(
    bot_qq: str,
    scope_type: str,
    scope_id: str,
    payload: ConversationPersonaPayload,
) -> dict:
    if scope_type not in {"group", "private"}:
        raise HTTPException(status_code=400, detail="invalid scope_type")

    ok = update_conversation_persona(bot_qq, scope_type, scope_id, payload.persona)
    if not ok:
        raise HTTPException(status_code=404, detail="conversation not found")

    return {"ok": True, "persona": payload.persona.strip()}


@router.patch("/conversations/{bot_qq}/{scope_type}/{scope_id}/memory")
def set_conversation_memory(
    bot_qq: str,
    scope_type: str,
    scope_id: str,
    payload: ConversationMemoryPayload,
) -> dict:
    if scope_type not in {"group", "private"}:
        raise HTTPException(status_code=400, detail="invalid scope_type")

    with connect() as conn:
        exists = conn.execute(
            """
            SELECT 1 FROM conversation_configs
            WHERE bot_qq = ? AND scope_type = ? AND scope_id = ?
            """,
            (bot_qq, scope_type, scope_id),
        ).fetchone()
    if not exists:
        raise HTTPException(status_code=404, detail="conversation not found")

    lines = payload.manager_memory_text.splitlines()
    memory = replace_manager_memory(bot_qq, scope_type, scope_id, lines)
    return {"ok": True, "memory": memory}


@router.post("/conversations/{bot_qq}/{scope_type}/{scope_id}/learned-memory/update")
def update_conversation_learned_memory(bot_qq: str, scope_type: str, scope_id: str) -> dict:
    from .llm_client import LLMError
    from .memory_learner import force_update_learned_memory

    if scope_type not in {"group", "private"}:
        raise HTTPException(status_code=400, detail="invalid scope_type")

    with connect() as conn:
        row = conn.execute(
            """
            SELECT learning_enabled FROM conversation_configs
            WHERE bot_qq = ? AND scope_type = ? AND scope_id = ?
            """,
            (bot_qq, scope_type, scope_id),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="conversation not found")
    if int(row["learning_enabled"] or 0) != 1:
        raise HTTPException(status_code=400, detail="当前会话已关闭学习。")

    try:
        memory = force_update_learned_memory(bot_qq, scope_type, scope_id)
    except LLMError as exc:
        log_event("error", f"memory:{scope_type}:{bot_qq}:{scope_id}", "manual learned memory update failed", str(exc)[:800])
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_event("info", f"memory:{scope_type}:{bot_qq}:{scope_id}", "manual learned memory updated")
    return {"ok": True, "memory": memory}


@router.post("/conversations/{bot_qq}/{scope_type}/{scope_id}/learned-memory/clear-pending")
def clear_conversation_learning_pending(bot_qq: str, scope_type: str, scope_id: str) -> dict:
    if scope_type not in {"group", "private"}:
        raise HTTPException(status_code=400, detail="invalid scope_type")

    with connect() as conn:
        exists = conn.execute(
            """
            SELECT 1 FROM conversation_configs
            WHERE bot_qq = ? AND scope_type = ? AND scope_id = ?
            """,
            (bot_qq, scope_type, scope_id),
        ).fetchone()
    if not exists:
        raise HTTPException(status_code=404, detail="conversation not found")

    memory = reset_pending_message_count(bot_qq, scope_type, scope_id)
    log_event("info", f"memory:{scope_type}:{bot_qq}:{scope_id}", "learning pending cache cleared")
    return {"ok": True, "memory": memory}


def require_conversation_config(bot_qq: str, scope_type: str, scope_id: str) -> dict:
    if scope_type not in {"group", "private"}:
        raise HTTPException(status_code=400, detail="invalid scope_type")

    config = get_conversation_config(bot_qq, scope_type, scope_id)
    if not config:
        raise HTTPException(status_code=404, detail="conversation not found")
    return config
