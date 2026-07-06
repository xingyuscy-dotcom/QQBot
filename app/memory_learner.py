import json
from typing import Any

from .commands import is_memory_command_text
from .db import ScopeType, get_conversation_config, get_recent_conversation_messages, record_llm_usage
from .llm_client import LLMError, chat_completion_result
from .memory_store import load_memory, replace_learned_memory
from .settings import get_settings


def update_learned_memory_if_needed(bot_qq: str, scope_type: ScopeType, scope_id: str) -> bool:
    if not is_learning_enabled(bot_qq, scope_type, scope_id):
        return False

    settings = get_settings()
    if not settings.get("llm.api_key", "").strip():
        return False

    memory = load_memory(bot_qq, scope_type, scope_id)
    config = get_conversation_config(bot_qq, scope_type, scope_id) or {}
    threshold = get_learning_batch_size(config, settings)
    if int(memory.get("pending_message_count") or 0) < threshold:
        return False

    recent = load_learning_messages(bot_qq, scope_type, scope_id)
    if len(recent) < 8:
        return False

    update_learned_memory_from_messages(bot_qq, scope_type, scope_id, settings, memory, recent)
    return True


def force_update_learned_memory(bot_qq: str, scope_type: ScopeType, scope_id: str) -> dict[str, Any]:
    if not is_learning_enabled(bot_qq, scope_type, scope_id):
        raise LLMError("当前会话已关闭学习。")

    settings = get_settings()
    if not settings.get("llm.api_key", "").strip():
        raise LLMError("还没有配置 API Key。")

    memory = load_memory(bot_qq, scope_type, scope_id)
    recent = load_learning_messages(bot_qq, scope_type, scope_id)
    if len(recent) < 3:
        raise LLMError("有效聊天消息太少，至少需要 3 条。")

    return update_learned_memory_from_messages(bot_qq, scope_type, scope_id, settings, memory, recent)


def is_learning_enabled(bot_qq: str, scope_type: ScopeType, scope_id: str) -> bool:
    config = get_conversation_config(bot_qq, scope_type, scope_id)
    return bool(config and int(config.get("learning_enabled") or 0) == 1)


def get_learning_batch_size(config: dict[str, Any], settings: dict[str, str] | None = None) -> int:
    custom_size = parse_int(config.get("learning_batch_size"), 0, minimum=0)
    if custom_size > 0:
        return custom_size
    settings = settings or get_settings()
    return parse_int(settings.get("bot.memory_batch_size"), 40)


def load_learning_messages(bot_qq: str, scope_type: ScopeType, scope_id: str) -> list[dict[str, Any]]:
    return [
        item
        for item in get_recent_conversation_messages(bot_qq, scope_type, scope_id, limit=120)
        if int(item.get("is_bot") or 0) != 1
        and not is_memory_command_text(str(item.get("text") or ""))
    ]


def update_learned_memory_from_messages(
    bot_qq: str,
    scope_type: ScopeType,
    scope_id: str,
    settings: dict[str, str],
    memory: dict[str, Any],
    recent: list[dict[str, Any]],
) -> dict[str, Any]:
    learned = memory.get("learned_memory") or {}
    scope_name = "群聊" if scope_type == "group" else "私聊"
    conversation_text = "\n".join(f"用户{item['user_id']}: {item['text']}" for item in recent[-80:])
    prompt = f"""
你在维护一个 QQ 机器人的会话级长期记忆。当前会话类型：{scope_name}。
只总结这个会话里所有人的整体聊天风格和共同话题，不要把风格绑定到某个单独的人。
请基于“已有学习记忆”和“新增对话”输出一个 JSON 对象，不要输出 Markdown。

JSON 字段固定为：
{{
  "summary": "一句话概括这个会话的整体特点",
  "tone": "整体语气风格",
  "topics": ["常聊话题，最多 8 个"],
  "phrases": ["常用表达或口头禅，最多 12 个"],
  "avoid": ["回复时应该避免的点，最多 8 个"]
}}

已有学习记忆：
{json.dumps(learned, ensure_ascii=False)}

新增对话：
{conversation_text}
""".strip()

    result = chat_completion_result(
        settings,
        [
            {"role": "system", "content": "你只负责把聊天记录压缩成结构化长期记忆。"},
            {"role": "user", "content": prompt},
        ],
    )
    record_llm_usage(bot_qq, scope_type, scope_id, result.model, result.usage)
    learned_memory = parse_json_object(result.reply)
    return replace_learned_memory(bot_qq, scope_type, scope_id, learned_memory, pending_message_count=0)


def parse_json_object(text: str) -> dict[str, Any]:
    content = text.strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:].strip()

    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMError("memory learner response is not json")
    data = json.loads(content[start : end + 1])
    if not isinstance(data, dict):
        raise LLMError("memory learner response is not object")
    return data


def parse_int(value: Any, fallback: int, minimum: int = 10) -> int:
    try:
        result = int(value) if value is not None else fallback
    except (TypeError, ValueError):
        return fallback
    return max(minimum, result)
