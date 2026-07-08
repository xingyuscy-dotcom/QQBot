import json
import re
import time
from typing import Callable

from .backup_store import create_backup, list_backups
from .db import (
    connect,
    log_event,
    now_text,
    update_conversation_learning,
    update_conversation_persona,
)
from .health_check import collect_health, format_health_report
from .memory_store import (
    add_manager_memory,
    clear_manager_memory,
    delete_manager_memory,
    load_memory,
    reset_pending_message_count,
)
from .paths import COMMANDS_PATH
from .settings import get_settings


def memory_view(context, args: str) -> str:
    memory = load_memory(context.bot_qq, context.scope_type, context.scope_id)
    items = memory.get("manager_memory") or []
    if not items:
        return "这个会话还没有管理员长期记忆。"
    lines = "\n".join(f"{index}. {item}" for index, item in enumerate(items, start=1))
    return f"当前管理员长期记忆：\n{lines}"


def memory_add(context, args: str) -> str:
    item = args.strip()
    if not item:
        return "用法：/记忆 添加 需要记住的内容"
    add_manager_memory(context.bot_qq, context.scope_type, context.scope_id, item)
    return "已添加到这个会话的管理员长期记忆。"


def memory_delete(context, args: str) -> str:
    raw_index = args.strip()
    try:
        index = int(raw_index)
    except ValueError:
        return "用法：/记忆 删除 1"

    _, removed = delete_manager_memory(context.bot_qq, context.scope_type, context.scope_id, index)
    if removed is None:
        return "没有找到这个编号的记忆。"
    return f"已删除：{removed}"


def memory_clear(context, args: str) -> str:
    clear_manager_memory(context.bot_qq, context.scope_type, context.scope_id)
    return "已清空这个会话的管理员长期记忆。"


def memory_help(context, args: str) -> str:
    if not args.strip():
        return memory_view(context, args)
    return "记忆指令：/记忆 查看、/记忆 添加 内容、/记忆 删除 编号、/记忆 清空"


def learning_view(context, args: str) -> str:
    return _format_learned_memory(load_memory(context.bot_qq, context.scope_type, context.scope_id))


def learning_update(context, args: str) -> str:
    from .llm_client import LLMError
    from .memory_learner import force_update_learned_memory

    try:
        memory = force_update_learned_memory(context.bot_qq, context.scope_type, context.scope_id)
    except LLMError as exc:
        log_event("error", _memory_scope(context), "manual learned memory update failed", str(exc)[:800])
        return f"学习记忆更新失败：{exc}"
    except Exception as exc:
        log_event("error", _memory_scope(context), "manual learned memory update failed", repr(exc)[:800])
        return "学习记忆更新失败，详情看后台日志。"

    log_event("info", _memory_scope(context), "manual learned memory updated")
    return "已更新当前会话学习记忆。\n" + _format_learned_memory(memory)


def learning_enable(context, args: str) -> str:
    if not _set_learning_enabled(context, True):
        return "当前会话还没有配置记录。"
    return "已开启当前会话学习。"


def learning_disable(context, args: str) -> str:
    if not _set_learning_enabled(context, False):
        return "当前会话还没有配置记录。"
    return "已关闭当前会话学习。"


def learning_batch(context, args: str) -> str:
    value = args.strip()
    try:
        batch_size = int(value)
    except ValueError:
        return "用法：/学习 批量 数量。填 0 表示使用全局批量。"

    if batch_size < 0:
        return "学习批量不能小于 0。"
    if batch_size > 0 and batch_size < 10:
        return "学习批量至少 10 条；填 0 表示使用全局批量。"
    if not _set_learning_batch_size(context, batch_size):
        return "当前会话还没有配置记录。"
    return f"已设置当前会话学习批量：{batch_size if batch_size > 0 else '使用全局'}。"


def learning_weight(context, args: str) -> str:
    value = args.strip()
    try:
        weight = float(value)
    except ValueError:
        return "用法：/学习 权重 0.4。范围 0 到 1。"

    if weight < 0 or weight > 1:
        return "学习记忆影响权重必须在 0 到 1 之间。"
    if not _set_learned_memory_weight(context, weight):
        return "当前会话还没有配置记录。"
    return f"已设置当前会话学习记忆影响权重：{weight:.2f}。"


def learning_clear_pending(context, args: str) -> str:
    reset_pending_message_count(context.bot_qq, context.scope_type, context.scope_id)
    return "已清空当前会话待学习消息数。"


def learning_help(context, args: str) -> str:
    return "学习指令：/学习 查看、/学习 更新、/学习 开启、/学习 关闭、/学习 批量 数量、/学习 权重 0.4、/学习 清空缓存"


def command_help(context, args: str) -> str:
    commands = _load_command_library()
    manager = _is_manager(context.user_id)
    lines: list[str] = []
    seen_usages: set[str] = set()

    for command in commands:
        scopes = command.get("scopes") or []
        if scopes and context.scope_type not in scopes:
            continue
        if command.get("manager_only") and not manager:
            continue

        usage = str(command.get("usage") or command.get("trigger") or "").strip()
        description = str(command.get("description") or "").strip()
        if not usage or usage in seen_usages:
            continue

        seen_usages.add(usage)
        lines.append(f"{usage}：{description}" if description else usage)

    if not lines:
        return "当前没有可用指令。"
    return "可用指令：\n" + "\n".join(lines)


def knowledge_query(context, args: str) -> str:
    query = args.strip()
    if not query:
        return "用法：/知识库 内容"

    from .knowledge_store import format_knowledge_for_direct_reply, search_knowledge
    from .llm_client import MINIMUM_REPLY

    started = time.perf_counter()
    items = search_knowledge(query, limit=5)
    search_ms = elapsed_ms(started)
    log_knowledge_timing(
        context,
        "knowledge fast query",
        f"search_ms={search_ms}; items={len(items)}; query={query[:200]}",
    )
    if not items:
        return MINIMUM_REPLY

    return format_knowledge_for_direct_reply(items, limit=5)


def knowledge_summary_query(context, args: str) -> str:
    query = args.strip()
    if not query:
        return "用法：/知识库总结 内容"

    from .knowledge_store import search_knowledge
    from .llm_client import LLMError, MINIMUM_REPLY, generate_reply

    total_started = time.perf_counter()
    search_started = time.perf_counter()
    items = search_knowledge(query, limit=5)
    search_ms = elapsed_ms(search_started)
    if not items:
        log_knowledge_timing(
            context,
            "knowledge summary query",
            f"search_ms={search_ms}; llm_ms=0; total_ms={elapsed_ms(total_started)}; items=0; query={query[:200]}",
        )
        return MINIMUM_REPLY

    config = _get_conversation_config(context) or {}
    if not config:
        return "当前会话还没有配置记录。"

    try:
        llm_started = time.perf_counter()
        return generate_reply(
            context.bot_qq,
            context.scope_type,
            context.scope_id,
            config,
            f"/知识库 {query}",
            True,
            preloaded_knowledge_items=items,
        )
    except LLMError as exc:
        log_event("error", f"knowledge:{context.scope_type}:{context.bot_qq}:{context.scope_id}", "knowledge reply failed", str(exc)[:800])
        return f"知识库回答失败：{exc}"
    except Exception as exc:
        log_event("error", f"knowledge:{context.scope_type}:{context.bot_qq}:{context.scope_id}", "knowledge reply failed", repr(exc)[:800])
        return "知识库回答失败，详情看后台日志。"
    finally:
        llm_ms = elapsed_ms(llm_started) if "llm_started" in locals() else 0
        total_ms = elapsed_ms(total_started)
        log_knowledge_timing(
            context,
            "knowledge summary query",
            f"search_ms={search_ms}; llm_ms={llm_ms}; total_ms={total_ms}; items={len(items)}; query={query[:200]}",
        )


def hot_topics(context, args: str) -> str:
    from .hot_store import list_hot_topics, normalize_hot_source, search_hot_topics

    query = args.strip()
    source = normalize_hot_source(query)
    if query and not source:
        items = search_hot_topics(query, limit=10)
    else:
        items = list_hot_topics(source=source, limit=10)
    if not items:
        return "当前本地还没有热点数据。可以先在后台知识库里勾选“热点”并手动更新。"

    lines = ["当前热点（热榜只代表抓取时的热度，不等于事实结论）："]
    for item in items[:10]:
        lines.append(format_hot_topic_line(item))
    return "\n".join(lines)


def backup_create(context, args: str) -> str:
    try:
        backup = create_backup()
    except OSError as exc:
        log_event("error", "backup", "backup failed", str(exc)[:800])
        return f"备份失败：{exc}"

    log_event("info", "backup", "backup created", backup["path"])
    return f"备份完成：{backup['name']}"


def backup_list(context, args: str) -> str:
    backups = list_backups()[:5]
    if not backups:
        return "当前还没有备份。"

    lines = [f"{index}. {item['name']}（{_format_size(item['size'])}）" for index, item in enumerate(backups, start=1)]
    return "最近备份：\n" + "\n".join(lines)


def health_check(context, args: str) -> str:
    return format_health_report(collect_health())


def format_hot_topic_line(item: dict) -> str:
    title = str(item.get("title") or "").strip()
    summary = str(item.get("summary") or "").strip()
    category = str(item.get("category") or "").strip()
    source = category.split("/")[1] if "/" in category else "热点"
    rank_match = re.search(r"第(\d+)名[:：](.+)$", title)
    rank = rank_match.group(1) if rank_match else "-"
    topic = rank_match.group(2).strip() if rank_match else title
    time_match = re.search(r"抓取时间[:：]([^；;]+)", summary)
    fetched_time = time_match.group(1).strip() if time_match else str(item.get("fetched_at") or item.get("event_date") or "-")
    return f"[{fetched_time} {source}] #{rank} {topic}"


def elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def log_knowledge_timing(context, message: str, detail: str) -> None:
    try:
        log_event(
            "info",
            f"knowledge:{context.scope_type}:{context.bot_qq}:{context.scope_id}",
            message,
            detail,
        )
    except Exception:
        pass


def at_test(context, args: str):
    if context.scope_type != "group":
        return "这个命令只能在群聊里使用。"

    parts = args.strip().split(maxsplit=1)
    if not parts:
        return "用法：/at QQ号 内容"

    qq = parts[0].strip()
    text = parts[1].strip() if len(parts) > 1 else "测试一下"
    if not qq.isdigit():
        return "QQ号只能是数字。"

    return {"type": "group_at", "qq": qq, "text": text}


def conversation_status(context, args: str) -> str:
    config = _get_conversation_config(context)
    if not config:
        return "当前会话还没有配置记录。"

    memory = load_memory(context.bot_qq, context.scope_type, context.scope_id)
    manager_memory_count = len(memory.get("manager_memory") or [])
    message_count = _get_message_count(context)

    return "\n".join(
        [
            "当前会话状态：",
            f"会话：{_scope_label(context)} {context.scope_id}",
            f"机器人：{'已启用' if int(config.get('enabled') or 0) == 1 else '已停用'}",
            f"学习：{'已开启' if int(config.get('learning_enabled') or 0) == 1 else '已关闭'}",
            f"学习批量：{_format_learning_batch(config)}",
            f"学习记忆影响：{float(config.get('learned_memory_weight') or 0.4):.2f}",
            f"回复模式：{_format_reply_mode(config)}",
            f"会话人设：{'已设置' if str(config.get('persona') or '').strip() else '未设置'}",
            f"管理员记忆：{manager_memory_count} 条",
            f"学习记忆：{'已生成' if _has_learned_memory(memory) else '未生成'}",
            f"历史消息：{message_count} 条",
        ]
    )


def conversation_enable(context, args: str) -> str:
    if not _set_conversation_enabled(context, True):
        return "当前会话还没有配置记录。"
    return "已启用当前会话机器人。"


def conversation_disable(context, args: str) -> str:
    if not _set_conversation_enabled(context, False):
        return "当前会话还没有配置记录。"
    return "已停用当前会话机器人。"


def persona_view(context, args: str) -> str:
    config = _get_conversation_config(context)
    persona = str((config or {}).get("persona") or "").strip()
    if not persona:
        return "当前会话还没有单独人设。"
    return f"当前会话人设：\n{persona}"


def persona_set(context, args: str) -> str:
    persona = args.strip()
    if not persona:
        return "用法：/人设 设置 内容"
    if not update_conversation_persona(context.bot_qq, context.scope_type, context.scope_id, persona):
        return "当前会话还没有配置记录。"
    return "已更新当前会话人设。"


def persona_clear(context, args: str) -> str:
    if not update_conversation_persona(context.bot_qq, context.scope_type, context.scope_id, ""):
        return "当前会话还没有配置记录。"
    return "已清空当前会话人设。"


def persona_help(context, args: str) -> str:
    return "人设指令：/人设 查看、/人设 设置 内容、/人设 清空"


def mode_view(context, args: str) -> str:
    config = _get_conversation_config(context)
    if not config:
        return "当前会话还没有配置记录。"
    return f"当前回复模式：{_format_reply_mode(config)}"


def mode_mention(context, args: str) -> str:
    if context.scope_type == "private":
        return "私聊固定为全部消息模式，不支持 @机器人 模式。"
    if not _set_reply_mode(context, "mention", _current_prefix(context)):
        return "当前会话还没有配置记录。"
    return "已设置为：仅 @ 机器人时回复。"


def mode_prefix(context, args: str) -> str:
    if context.scope_type == "private":
        return "私聊固定为全部消息模式，不支持前缀模式。"

    prefix = args.strip().split(maxsplit=1)[0] if args.strip() else ""
    if not prefix:
        return "用法：/模式 前缀 /bot"
    if not _set_reply_mode(context, "prefix", prefix):
        return "当前会话还没有配置记录。"
    return f"已设置为：前缀 {prefix} 触发。"


def mode_all(context, args: str) -> str:
    if not _set_reply_mode(context, "all", _current_prefix(context)):
        return "当前会话还没有配置记录。"
    return "已设置为：全部消息都触发回复。"


def mode_help(context, args: str) -> str:
    if context.scope_type == "private":
        return "模式指令：/模式 查看、/模式 全部消息"
    return "模式指令：/模式 查看、/模式 @机器人、/模式 前缀 /bot、/模式 全部消息"


def _load_command_library() -> list[dict]:
    try:
        data = json.loads(COMMANDS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _is_manager(user_id: str) -> bool:
    raw = get_settings().get("bot.manager_qqs", "")
    normalized = (
        raw.replace("，", ",")
        .replace("；", ",")
        .replace(";", ",")
        .replace("\n", ",")
        .replace("\t", ",")
        .replace(" ", ",")
    )
    manager_qqs = {item.strip() for item in normalized.split(",") if item.strip()}
    return str(user_id) in manager_qqs


def _get_conversation_config(context) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT bot_qq, scope_type, scope_id, display_name, enabled,
                   response_mode, trigger_prefix, learning_enabled,
                   learning_batch_size, learned_memory_weight, persona
            FROM conversation_configs
            WHERE bot_qq = ? AND scope_type = ? AND scope_id = ?
            """,
            (context.bot_qq, context.scope_type, context.scope_id),
        ).fetchone()
    return dict(row) if row else None


def _get_message_count(context) -> int:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM conversation_messages
            WHERE bot_qq = ? AND scope_type = ? AND scope_id = ?
            """,
            (context.bot_qq, context.scope_type, context.scope_id),
        ).fetchone()
    return int(row["c"]) if row else 0


def _set_conversation_enabled(context, enabled: bool) -> bool:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE conversation_configs
            SET enabled = ?, updated_at = ?
            WHERE bot_qq = ? AND scope_type = ? AND scope_id = ?
            """,
            (int(enabled), now_text(), context.bot_qq, context.scope_type, context.scope_id),
        )
    return cursor.rowcount > 0


def _set_learning_enabled(context, enabled: bool) -> bool:
    return update_conversation_learning(
        context.bot_qq,
        context.scope_type,
        context.scope_id,
        learning_enabled=enabled,
    )


def _set_learning_batch_size(context, batch_size: int) -> bool:
    return update_conversation_learning(
        context.bot_qq,
        context.scope_type,
        context.scope_id,
        learning_batch_size=batch_size,
    )


def _set_learned_memory_weight(context, weight: float) -> bool:
    return update_conversation_learning(
        context.bot_qq,
        context.scope_type,
        context.scope_id,
        learned_memory_weight=weight,
    )


def _set_reply_mode(context, response_mode: str, trigger_prefix: str) -> bool:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE conversation_configs
            SET response_mode = ?, trigger_prefix = ?, updated_at = ?
            WHERE bot_qq = ? AND scope_type = ? AND scope_id = ?
            """,
            (
                response_mode,
                trigger_prefix.strip(),
                now_text(),
                context.bot_qq,
                context.scope_type,
                context.scope_id,
            ),
        )
    return cursor.rowcount > 0


def _current_prefix(context) -> str:
    config = _get_conversation_config(context) or {}
    return str(config.get("trigger_prefix") or "/bot").strip() or "/bot"


def _format_reply_mode(config: dict) -> str:
    mode = str(config.get("response_mode") or "mention")
    prefix = str(config.get("trigger_prefix") or "").strip()
    if mode == "all":
        return "全部消息"
    if mode == "prefix":
        return f"前缀 {prefix or '/bot'}"
    return "仅 @ 机器人"


def _format_learning_batch(config: dict) -> str:
    batch_size = int(config.get("learning_batch_size") or 0)
    return str(batch_size) if batch_size > 0 else "使用全局"


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _scope_label(context) -> str:
    return "群聊" if context.scope_type == "group" else "私聊"


def _memory_scope(context) -> str:
    return f"memory:{context.scope_type}:{context.bot_qq}:{context.scope_id}"


def _has_learned_memory(memory: dict) -> bool:
    learned = memory.get("learned_memory") or {}
    for value in learned.values():
        if isinstance(value, list) and value:
            return True
        if isinstance(value, str) and value.strip():
            return True
    return False


def _format_learned_memory(memory: dict) -> str:
    learned = memory.get("learned_memory") or {}
    lines: list[str] = []
    summary = str(learned.get("summary") or "").strip()
    tone = str(learned.get("tone") or "").strip()
    topics = learned.get("topics") or []
    phrases = learned.get("phrases") or []
    avoid = learned.get("avoid") or []

    if summary:
        lines.append(f"摘要：{summary}")
    if tone:
        lines.append(f"语气：{tone}")
    if topics:
        lines.append("话题：" + "、".join(str(item) for item in topics if str(item).strip()))
    if phrases:
        lines.append("表达：" + "、".join(str(item) for item in phrases if str(item).strip()))
    if avoid:
        lines.append("避免：" + "、".join(str(item) for item in avoid if str(item).strip()))

    pending_count = int(memory.get("pending_message_count") or 0)
    if not lines:
        return f"当前会话还没有学习记忆。\n待学习消息数：{pending_count}"
    return "当前学习记忆：\n" + "\n".join(lines) + f"\n待学习消息数：{pending_count}"


HANDLERS: dict[str, Callable] = {
    "memory_view": memory_view,
    "memory_add": memory_add,
    "memory_delete": memory_delete,
    "memory_clear": memory_clear,
    "memory_help": memory_help,
    "learning_view": learning_view,
    "learning_update": learning_update,
    "learning_enable": learning_enable,
    "learning_disable": learning_disable,
    "learning_batch": learning_batch,
    "learning_weight": learning_weight,
    "learning_clear_pending": learning_clear_pending,
    "learning_help": learning_help,
    "command_help": command_help,
    "knowledge_query": knowledge_query,
    "knowledge_summary_query": knowledge_summary_query,
    "hot_topics": hot_topics,
    "backup_create": backup_create,
    "backup_list": backup_list,
    "health_check": health_check,
    "at_test": at_test,
    "conversation_status": conversation_status,
    "conversation_enable": conversation_enable,
    "conversation_disable": conversation_disable,
    "persona_view": persona_view,
    "persona_set": persona_set,
    "persona_clear": persona_clear,
    "persona_help": persona_help,
    "mode_view": mode_view,
    "mode_mention": mode_mention,
    "mode_prefix": mode_prefix,
    "mode_all": mode_all,
    "mode_help": mode_help,
}
