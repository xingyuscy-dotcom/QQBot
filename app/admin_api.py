import json
import re
import subprocess
import sys
import threading
import time
from zipfile import BadZipFile
from datetime import date, datetime, timedelta, timezone

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
from .hot_store import format_hot_items_for_debug, hot_stats
from .knowledge_store import knowledge_stats, search_knowledge
from .memory_store import load_memory, replace_manager_memory, reset_pending_message_count
from .paths import BACKUPS_DIR, COMMANDS_PATH, DB_PATH, HOT_DB_PATH, KNOWLEDGE_DB_PATH, LOGS_DIR, PROJECT_ROOT
from .rag_store import rag_stats, rag_task_snapshot, start_rag_index, stop_rag_index
from .prompts import DEFAULT_GLOBAL_SYSTEM_PROMPT
from .settings import get_settings, set_setting


router = APIRouter(prefix="/api")
KNOWLEDGE_UPDATE_LOCK = threading.Lock()
KNOWLEDGE_UPDATE_TASK: dict = {
    "running": False,
    "status": "idle",
    "progress": 0,
    "output": [],
}


class ConversationEnabledPayload(BaseModel):
    enabled: bool


class ConversationLearningPayload(BaseModel):
    learning_enabled: bool
    learning_batch_size: int | None = None
    learned_memory_weight: float | None = None
    context_message_limit: int | None = None


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
    knowledge_enabled: str = "1"
    knowledge_sensitivity: str = "medium"
    knowledge_max_items: str = "5"
    knowledge_force_prefixes: str = "查知识库,知识库"
    rag_enabled: str = "1"
    rag_embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    rag_embedding_dimension: str = "512"
    rag_embedding_device: str = "auto"
    rag_embedding_batch_size: str = "16"
    rag_min_similarity: str = "0.30"
    rag_hot_vector_days: str = "30"
    rag_reranker_enabled: str = "0"
    rag_reranker_model: str = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
    rag_reranker_top_k: str = "20"


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


class KnowledgeSearchPayload(BaseModel):
    query: str = ""
    limit: int = 10


class KnowledgeUpdatePayload(BaseModel):
    date_from: str = "2025-01-01"
    date_to: str = ""
    sources: str = "current,game,esports,anime,wikidata,daily,holidays,onthisday,hot"


class HotHistoryUpdatePayload(BaseModel):
    date_from: str = "2025-01-01"
    date_to: str = ""


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
        "knowledge_enabled": settings.get("knowledge.enabled", "1"),
        "knowledge_sensitivity": settings.get("knowledge.sensitivity", "medium"),
        "knowledge_max_items": settings.get("knowledge.max_items", "5"),
        "knowledge_force_prefixes": settings.get("knowledge.force_prefixes", "查知识库,知识库"),
        "rag_enabled": settings.get("rag.enabled", "1"),
        "rag_embedding_model": settings.get("rag.embedding_model", "Qwen/Qwen3-Embedding-0.6B"),
        "rag_embedding_dimension": settings.get("rag.embedding_dimension", "512"),
        "rag_embedding_device": settings.get("rag.embedding_device", "auto"),
        "rag_embedding_batch_size": settings.get("rag.embedding_batch_size", "16"),
        "rag_min_similarity": settings.get("rag.min_similarity", "0.30"),
        "rag_hot_vector_days": settings.get("rag.hot_vector_days", "30"),
        "rag_reranker_enabled": settings.get("rag.reranker_enabled", "0"),
        "rag_reranker_model": settings.get("rag.reranker_model", "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"),
        "rag_reranker_top_k": settings.get("rag.reranker_top_k", "20"),
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
        "knowledge.enabled": "1" if payload.knowledge_enabled.strip() == "1" else "0",
        "knowledge.sensitivity": payload.knowledge_sensitivity.strip() or "medium",
        "knowledge.max_items": payload.knowledge_max_items.strip() or "5",
        "knowledge.force_prefixes": payload.knowledge_force_prefixes.strip() or "查知识库,知识库",
        "rag.enabled": "1" if payload.rag_enabled.strip() == "1" else "0",
        "rag.embedding_model": payload.rag_embedding_model.strip() or "Qwen/Qwen3-Embedding-0.6B",
        "rag.embedding_dimension": payload.rag_embedding_dimension.strip() or "512",
        "rag.embedding_device": payload.rag_embedding_device.strip() or "auto",
        "rag.embedding_batch_size": payload.rag_embedding_batch_size.strip() or "16",
        "rag.min_similarity": payload.rag_min_similarity.strip() or "0.30",
        "rag.hot_vector_days": payload.rag_hot_vector_days.strip() or "30",
        "rag.reranker_enabled": "1" if payload.rag_reranker_enabled.strip() == "1" else "0",
        "rag.reranker_model": payload.rag_reranker_model.strip() or "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
        "rag.reranker_top_k": payload.rag_reranker_top_k.strip() or "20",
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


@router.get("/knowledge")
def read_knowledge() -> dict:
    stats = knowledge_stats()
    settings = get_settings()
    return {
        "ok": True,
        "path": str(KNOWLEDGE_DB_PATH),
        "hot_path": str(HOT_DB_PATH),
        "system_time": datetime.now().isoformat(timespec="seconds"),
        "hot_last_daily_archive_date": settings.get("hot.last_daily_archive_date", ""),
        "stats": stats,
        "hot_stats": hot_stats(),
        "rag_stats": rag_stats(),
        "update_task": knowledge_update_snapshot(),
    }


@router.post("/knowledge/rag-index")
def build_rag_index_api(full_rebuild: bool = Query(default=False)) -> dict:
    return {
        "ok": True,
        "task": start_rag_index(full_rebuild),
    }


@router.post("/knowledge/rag-stop")
def stop_rag_index_api() -> dict:
    return {
        "ok": True,
        "task": stop_rag_index(),
    }


@router.get("/knowledge/rag-status")
def rag_status_api() -> dict:
    return {
        "ok": True,
        "stats": rag_stats(),
        "task": rag_task_snapshot(),
    }


@router.post("/knowledge/search")
def search_knowledge_api(payload: KnowledgeSearchPayload) -> dict:
    query = payload.query.strip()
    if not query:
        return {"ok": True, "items": []}
    return {
        "ok": True,
        "items": search_knowledge(query, limit=payload.limit),
    }


@router.post("/knowledge/update")
def update_knowledge_api(payload: KnowledgeUpdatePayload) -> dict:
    date_from = payload.date_from.strip() or "2025-01-01"
    date_to = payload.date_to.strip() or date.today().isoformat()
    sources = normalize_knowledge_sources(payload.sources)
    try:
        total_steps = count_knowledge_steps(date_from, date_to, sources)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="日期格式不正确，请使用 YYYY-MM-DD。") from exc

    script = PROJECT_ROOT / "scripts" / "update_knowledge.py"
    command = [sys.executable, str(script), "--from", date_from]
    command.extend(["--to", date_to])
    command.extend(["--sources", ",".join(sources)])

    with KNOWLEDGE_UPDATE_LOCK:
        if KNOWLEDGE_UPDATE_TASK.get("running"):
            return {"ok": True, "already_running": True, "task": knowledge_update_snapshot_locked()}

        stats = knowledge_stats()
        KNOWLEDGE_UPDATE_TASK.clear()
        KNOWLEDGE_UPDATE_TASK.update(
            {
                "running": True,
                "status": "running",
                "progress": 0,
                "completed_steps": 0,
                "total_steps": total_steps,
                "sources": sources,
                "source_ratio": "中国 70% / 国际 30%",
                "eta_seconds": None,
                "date_from": date_from,
                "date_to": date_to,
                "system_time": datetime.now().isoformat(timespec="seconds"),
                "latest_data_date_before": stats.get("latest_date"),
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "finished_at": None,
                "output": [],
                "error": "",
            }
        )

    thread = threading.Thread(target=run_knowledge_update_task, args=(command,), daemon=True)
    thread.start()
    log_event("info", "knowledge", "knowledge update started", f"{date_from} to {date_to}, sources={','.join(sources)}")
    return {
        "ok": True,
        "started": True,
        "task": knowledge_update_snapshot(),
    }


@router.post("/knowledge/hot-history-update")
def update_hot_history_api(payload: HotHistoryUpdatePayload) -> dict:
    date_from = payload.date_from.strip() or "2025-01-01"
    date_to = payload.date_to.strip() or date.today().isoformat()
    try:
        total_steps = count_days(date_from, date_to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="日期格式不正确，请使用 YYYY-MM-DD。") from exc

    script = PROJECT_ROOT / "scripts" / "update_hot_history.py"
    command = [sys.executable, str(script), "--from", date_from, "--to", date_to]

    with KNOWLEDGE_UPDATE_LOCK:
        if KNOWLEDGE_UPDATE_TASK.get("running"):
            return {"ok": True, "already_running": True, "task": knowledge_update_snapshot_locked()}

        stats = hot_stats()
        KNOWLEDGE_UPDATE_TASK.clear()
        KNOWLEDGE_UPDATE_TASK.update(
            {
                "running": True,
                "status": "running",
                "progress": 0,
                "completed_steps": 0,
                "total_steps": total_steps,
                "sources": ["hot-history"],
                "source_ratio": "微博历史热搜",
                "eta_seconds": None,
                "date_from": date_from,
                "date_to": date_to,
                "system_time": datetime.now().isoformat(timespec="seconds"),
                "latest_hot_data_date_before": stats.get("latest_date"),
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "finished_at": None,
                "output": [],
                "error": "",
            }
        )

    thread = threading.Thread(target=run_knowledge_update_task, args=(command,), daemon=True)
    thread.start()
    log_event("info", "hot", "hot history update started", f"{date_from} to {date_to}")
    return {
        "ok": True,
        "started": True,
        "task": knowledge_update_snapshot(),
    }


@router.get("/knowledge/update-status")
def knowledge_update_status() -> dict:
    return {
        "ok": True,
        "task": knowledge_update_snapshot(),
        "stats": knowledge_stats(),
        "hot_stats": hot_stats(),
        "system_time": datetime.now().isoformat(timespec="seconds"),
    }


def run_knowledge_update_task(command: list[str]) -> None:
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            append_knowledge_update_output(line.rstrip(), started)

        return_code = process.wait()
        with KNOWLEDGE_UPDATE_LOCK:
            KNOWLEDGE_UPDATE_TASK["running"] = False
            KNOWLEDGE_UPDATE_TASK["finished_at"] = datetime.now().isoformat(timespec="seconds")
            if return_code == 0:
                KNOWLEDGE_UPDATE_TASK["status"] = "done"
                KNOWLEDGE_UPDATE_TASK["progress"] = 100
                KNOWLEDGE_UPDATE_TASK["eta_seconds"] = 0
                KNOWLEDGE_UPDATE_TASK["latest_data_date_after"] = knowledge_stats().get("latest_date")
                KNOWLEDGE_UPDATE_TASK["latest_hot_data_date_after"] = hot_stats().get("latest_date")
                log_event("info", "knowledge", "knowledge update finished", "\n".join(KNOWLEDGE_UPDATE_TASK["output"][-10:])[:800])
                if get_settings().get("rag.enabled", "1") == "1":
                    start_rag_index(False)
            else:
                KNOWLEDGE_UPDATE_TASK["status"] = "error"
                KNOWLEDGE_UPDATE_TASK["error"] = f"脚本退出码：{return_code}"
                log_event("error", "knowledge", "knowledge update failed", KNOWLEDGE_UPDATE_TASK["error"])
    except Exception as exc:
        with KNOWLEDGE_UPDATE_LOCK:
            KNOWLEDGE_UPDATE_TASK["running"] = False
            KNOWLEDGE_UPDATE_TASK["status"] = "error"
            KNOWLEDGE_UPDATE_TASK["error"] = repr(exc)[:800]
            KNOWLEDGE_UPDATE_TASK["finished_at"] = datetime.now().isoformat(timespec="seconds")
        log_event("error", "knowledge", "knowledge update failed", repr(exc)[:800])


def append_knowledge_update_output(line: str, started: float) -> None:
    with KNOWLEDGE_UPDATE_LOCK:
        if line:
            output = KNOWLEDGE_UPDATE_TASK.setdefault("output", [])
            output.append(line)
            KNOWLEDGE_UPDATE_TASK["output"] = output[-80:]

        if is_knowledge_progress_line(line):
            total = max(1, int(KNOWLEDGE_UPDATE_TASK.get("total_steps") or 1))
            completed = min(total, int(KNOWLEDGE_UPDATE_TASK.get("completed_steps") or 0) + 1)
            elapsed = max(1, time.monotonic() - started)
            KNOWLEDGE_UPDATE_TASK["completed_steps"] = completed
            KNOWLEDGE_UPDATE_TASK["progress"] = min(99, int(completed * 100 / total))
            if completed > 0 and completed < total:
                KNOWLEDGE_UPDATE_TASK["eta_seconds"] = int((elapsed / completed) * (total - completed))
            else:
                KNOWLEDGE_UPDATE_TASK["eta_seconds"] = 0


def knowledge_update_snapshot() -> dict:
    with KNOWLEDGE_UPDATE_LOCK:
        return knowledge_update_snapshot_locked()


def knowledge_update_snapshot_locked() -> dict:
    snapshot = dict(KNOWLEDGE_UPDATE_TASK)
    snapshot["output"] = list(KNOWLEDGE_UPDATE_TASK.get("output") or [])
    return snapshot


def count_months(date_from: str, date_to: str) -> int:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if end < start:
        raise ValueError("date_to must be after date_from")
    return max(1, (end.year - start.year) * 12 + end.month - start.month + 1)


def count_years(date_from: str, date_to: str) -> int:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if end < start:
        raise ValueError("date_to must be after date_from")
    return max(1, end.year - start.year + 1)


def count_days(date_from: str, date_to: str) -> int:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    today = date.today()
    if end > today:
        end = today
    if end < start:
        raise ValueError("date_to must be after date_from")
    return max(1, (end - start).days + 1)


def normalize_knowledge_sources(value: str) -> list[str]:
    default = ["current", "game", "esports", "anime", "wikidata", "daily", "holidays", "onthisday", "hot"]
    allowed = (*default, "gdelt")
    aliases = {
        "综合": "current",
        "新闻": "current",
        "游戏": "game",
        "电竞": "esports",
        "动漫": "anime",
        "动画": "anime",
        "事件": "gdelt",
        "结构化": "wikidata",
        "日常": "daily",
        "常识": "daily",
        "节假日": "holidays",
        "节日": "holidays",
        "历史上的今天": "onthisday",
        "历史": "onthisday",
        "热点": "hot",
        "热搜": "hot",
        "热门": "hot",
    }
    result = []
    for raw in str(value or "").split(","):
        item = aliases.get(raw.strip(), raw.strip().lower())
        if item in allowed and item not in result:
            result.append(item)
    return result or default


def count_knowledge_steps(date_from: str, date_to: str, sources: list[str]) -> int:
    month_count = count_months(date_from, date_to)
    total = month_count if "current" in sources else 0
    feed_counts = {
        "game": 9,
        "esports": 5,
        "anime": 5,
    }
    total += sum(feed_counts.get(source, 0) for source in sources)
    if "gdelt" in sources:
        total += 2
    if "wikidata" in sources:
        total += 1
    if "daily" in sources:
        total += 37
    if "holidays" in sources:
        total += count_years(date_from, date_to) * 4
    if "onthisday" in sources:
        total += 7
    if "hot" in sources:
        total += 5
    return max(1, total)


def is_knowledge_progress_line(line: str) -> bool:
    return bool(
        re.match(r"^current \d{4}-\d{2}:", line)
        or re.match(r"^(游戏|电竞|动漫)/", line)
        or re.match(r"^gdelt (cn|global):", line)
        or re.match(r"^wikidata:", line)
        or re.match(r"^daily ", line)
        or re.match(r"^holidays ", line)
        or re.match(r"^onthisday ", line)
        or re.match(r"^hot ", line)
        or re.match(r"^hot history \d{4}-\d{2}-\d{2}:", line)
    )


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
              c.context_message_limit,
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
              context_message_limit,
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
    from .knowledge_store import format_knowledge_items_for_debug
    from .llm_client import build_messages, collect_hot_items, collect_knowledge_items, load_readonly_context, parse_context_limit

    config = require_conversation_config(bot_qq, scope_type, scope_id)
    settings = get_settings()
    context_items = load_readonly_context(
        bot_qq,
        scope_type,
        scope_id,
        parse_context_limit(config.get("context_message_limit")),
    )
    knowledge_items = collect_knowledge_items(settings, "", context_items)
    hot_items = collect_hot_items(settings, "", context_items)
    return {
        "recent_messages": get_recent_conversation_messages(bot_qq, scope_type, scope_id, limit=20),
        "prompt_messages": build_messages(bot_qq, scope_type, scope_id, config, settings),
        "knowledge_preview": format_knowledge_items_for_debug(knowledge_items),
        "hot_preview": format_hot_items_for_debug(hot_items),
    }


@router.post("/conversations/{bot_qq}/{scope_type}/{scope_id}/debug-reply")
def conversation_debug_reply(
    bot_qq: str,
    scope_type: str,
    scope_id: str,
    payload: ConversationDebugPayload,
) -> dict:
    from .knowledge_store import format_knowledge_items_for_debug
    from .llm_client import LLMError, build_messages, chat_completion, collect_hot_items, collect_knowledge_items, load_readonly_context, parse_context_limit

    config = require_conversation_config(bot_qq, scope_type, scope_id)
    test_text = payload.test_text.strip()
    if not test_text:
        raise HTTPException(status_code=400, detail="请输入测试消息。")

    settings = get_settings()
    context_items = load_readonly_context(
        bot_qq,
        scope_type,
        scope_id,
        parse_context_limit(config.get("context_message_limit")),
        True,
    )
    knowledge_items = collect_knowledge_items(settings, test_text, context_items)
    hot_items = collect_hot_items(settings, test_text, context_items)
    messages = build_messages(bot_qq, scope_type, scope_id, config, settings, f"用户调试: {test_text}")

    try:
        reply = chat_completion(settings, messages)
    except LLMError as exc:
        log_event("error", f"debug:{scope_type}:{bot_qq}:{scope_id}", "debug reply failed", str(exc)[:800])
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log_event("info", f"debug:{scope_type}:{bot_qq}:{scope_id}", "debug reply generated", test_text[:500])
    return {
        "ok": True,
        "reply": reply,
        "prompt_messages": messages,
        "knowledge_preview": format_knowledge_items_for_debug(knowledge_items),
        "hot_preview": format_hot_items_for_debug(hot_items),
    }


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

    context_limit = None
    if payload.context_message_limit is not None:
        context_limit = min(30, max(0, int(payload.context_message_limit)))

    ok = update_conversation_learning(
        bot_qq,
        scope_type,
        scope_id,
        payload.learning_enabled,
        batch_size,
        memory_weight,
        context_limit,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="conversation not found")

    with connect() as conn:
        row = conn.execute(
            """
            SELECT learning_enabled, learning_batch_size, learned_memory_weight, context_message_limit
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
