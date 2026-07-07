import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .db import ScopeType, now_text
from .paths import MEMORIES_DIR


DEFAULT_MEMORY = {
    "manager_memory": [],
    "learned_memory": {
        "summary": "",
        "tone": "",
        "topics": [],
        "phrases": [],
        "avoid": [],
    },
    "pending_message_count": 0,
    "updated_at": "",
}


def memory_path(bot_qq: str, scope_type: ScopeType, scope_id: str) -> Path:
    safe_bot_qq = safe_name(bot_qq)
    safe_scope_id = safe_name(scope_id)
    return MEMORIES_DIR / safe_bot_qq / f"{scope_type}_{safe_scope_id}.json"


def safe_name(value: str) -> str:
    text = str(value).strip()
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in text) or "unknown"


def load_memory(bot_qq: str, scope_type: ScopeType, scope_id: str) -> dict[str, Any]:
    path = memory_path(bot_qq, scope_type, scope_id)
    if not path.exists():
        return fresh_memory()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fresh_memory()

    memory = fresh_memory()
    if isinstance(data, dict):
        manager_memory = data.get("manager_memory")
        learned_memory = data.get("learned_memory")
        if isinstance(manager_memory, list):
            memory["manager_memory"] = [str(item) for item in manager_memory if str(item).strip()]
        if isinstance(learned_memory, dict):
            memory["learned_memory"].update(normalize_learned_memory(learned_memory))
        memory["pending_message_count"] = parse_int(data.get("pending_message_count"), 0)
        memory["updated_at"] = str(data.get("updated_at") or "")
    return memory


def save_memory(bot_qq: str, scope_type: ScopeType, scope_id: str, memory: dict[str, Any]) -> dict[str, Any]:
    path = memory_path(bot_qq, scope_type, scope_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    memory["updated_at"] = now_text()
    path.write_text(json.dumps(memory, ensure_ascii=False, indent=2), encoding="utf-8")
    return memory


def add_manager_memory(bot_qq: str, scope_type: ScopeType, scope_id: str, text: str) -> dict[str, Any]:
    memory = load_memory(bot_qq, scope_type, scope_id)
    item = text.strip()
    if item:
        memory["manager_memory"].append(item)
    return save_memory(bot_qq, scope_type, scope_id, memory)


def delete_manager_memory(bot_qq: str, scope_type: ScopeType, scope_id: str, index: int) -> tuple[dict[str, Any], str | None]:
    memory = load_memory(bot_qq, scope_type, scope_id)
    items = memory["manager_memory"]
    if index < 1 or index > len(items):
        return memory, None
    removed = items.pop(index - 1)
    return save_memory(bot_qq, scope_type, scope_id, memory), removed


def replace_manager_memory(
    bot_qq: str,
    scope_type: ScopeType,
    scope_id: str,
    manager_memory: list[str],
) -> dict[str, Any]:
    memory = load_memory(bot_qq, scope_type, scope_id)
    memory["manager_memory"] = [item.strip() for item in manager_memory if item.strip()]
    return save_memory(bot_qq, scope_type, scope_id, memory)


def replace_learned_memory(
    bot_qq: str,
    scope_type: ScopeType,
    scope_id: str,
    learned_memory: dict[str, Any],
    pending_message_count: int = 0,
) -> dict[str, Any]:
    memory = load_memory(bot_qq, scope_type, scope_id)
    memory["learned_memory"] = compact_learned_memory(normalize_learned_memory(learned_memory))
    memory["pending_message_count"] = pending_message_count
    return save_memory(bot_qq, scope_type, scope_id, memory)


def clear_manager_memory(bot_qq: str, scope_type: ScopeType, scope_id: str) -> dict[str, Any]:
    memory = load_memory(bot_qq, scope_type, scope_id)
    memory["manager_memory"] = []
    return save_memory(bot_qq, scope_type, scope_id, memory)


def bump_pending_message_count(bot_qq: str, scope_type: ScopeType, scope_id: str, amount: int = 1) -> None:
    memory = load_memory(bot_qq, scope_type, scope_id)
    memory["pending_message_count"] = int(memory.get("pending_message_count") or 0) + amount
    save_memory(bot_qq, scope_type, scope_id, memory)


def reset_pending_message_count(bot_qq: str, scope_type: ScopeType, scope_id: str) -> dict[str, Any]:
    memory = load_memory(bot_qq, scope_type, scope_id)
    memory["pending_message_count"] = 0
    return save_memory(bot_qq, scope_type, scope_id, memory)


def format_manager_memory_for_prompt(memory: dict[str, Any]) -> str:
    manager_items = memory.get("manager_memory") or []
    if not manager_items:
        return ""
    lines = "\n".join(f"{index}. {item}" for index, item in enumerate(manager_items, start=1))
    return f"管理员长期记忆，必须优先遵守：\n{lines}"


def format_learned_memory_for_prompt(memory: dict[str, Any], weight: float = 0.4) -> str:
    weight = min(1, max(0, float(weight)))
    if weight <= 0:
        return ""

    learned = memory.get("learned_memory") or {}
    learned_lines = []
    keys = [
        ("summary", "会话摘要"),
        ("tone", "语气风格"),
        ("phrases", "常用表达"),
        ("avoid", "避免事项"),
    ]
    if weight >= 0.7:
        keys.insert(2, ("topics", "常聊话题"))

    for key, label in keys:
        value = learned.get(key)
        if isinstance(value, list):
            limit = 4 if weight < 0.7 else 6
            value = "、".join(str(item) for item in value[:limit] if str(item).strip())
        value = str(value or "").strip()
        if value:
            learned_lines.append(f"{label}：{value}")

    if not learned_lines:
        return ""

    if weight < 0.35:
        level = "低"
    elif weight < 0.75:
        level = "中"
    else:
        level = "高"
    return (
        f"从历史对话学习到的会话风格记忆（影响力：{level}，只能作为语气参考，"
        "不能覆盖管理员记忆、全局人设、会话人设，也不要主动重复旧话题）：\n"
        + "\n".join(learned_lines)
    )


def format_memory_for_prompt(memory: dict[str, Any], learned_memory_weight: float = 0.4) -> str:
    parts: list[str] = []
    manager_prompt = format_manager_memory_for_prompt(memory)
    learned_prompt = format_learned_memory_for_prompt(memory, learned_memory_weight)
    if manager_prompt:
        parts.append(manager_prompt)
    if learned_prompt:
        parts.append(learned_prompt)

    return "\n\n".join(parts)


def fresh_memory() -> dict[str, Any]:
    return deepcopy(DEFAULT_MEMORY)


def normalize_learned_memory(data: dict[str, Any]) -> dict[str, Any]:
    learned = fresh_memory()["learned_memory"]
    for key in learned:
        value = data.get(key)
        if isinstance(learned[key], list):
            if isinstance(value, list):
                learned[key] = [str(item) for item in value if str(item).strip()]
            elif value:
                learned[key] = [str(value)]
        else:
            learned[key] = str(value or "")
    return learned


def compact_learned_memory(learned: dict[str, Any]) -> dict[str, Any]:
    compact = fresh_memory()["learned_memory"]
    compact["summary"] = trim_text(learned.get("summary"), 120)
    compact["tone"] = trim_text(learned.get("tone"), 120)
    compact["topics"] = trim_list(learned.get("topics"), limit=6, item_limit=20)
    compact["phrases"] = trim_list(learned.get("phrases"), limit=8, item_limit=20)
    compact["avoid"] = trim_list(learned.get("avoid"), limit=6, item_limit=30)
    return compact


def trim_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def trim_list(value: Any, limit: int, item_limit: int) -> list[str]:
    if not isinstance(value, list):
        value = [value] if value else []
    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = trim_text(item, item_limit)
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
        if len(items) >= limit:
            break
    return items


def parse_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
