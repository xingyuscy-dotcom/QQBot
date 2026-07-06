import json
from dataclasses import dataclass
from typing import Any

from .command_handlers import HANDLERS
from .db import log_event
from .paths import COMMANDS_PATH
from .settings import get_settings


def normalize_command_text(text: str) -> str:
    content = str(text or "").strip()
    while content.startswith("@"):
        parts = content.split(maxsplit=1)
        if len(parts) != 2:
            break
        content = parts[1].strip()
    return content


@dataclass
class CommandContext:
    bot_qq: str
    scope_type: str
    scope_id: str
    user_id: str
    text: str


@dataclass
class CommandMatch:
    command: dict[str, Any]
    args: str


def dispatch_command(context: CommandContext):
    match = match_command(context.text, context.scope_type)
    if match is None:
        return None

    command_name = str(match.command.get("name") or match.command.get("trigger") or "unknown")
    log_event("info", _command_scope(context), "command matched", _command_detail(context, command_name))

    if match.command.get("manager_only") and not is_manager(context.user_id):
        log_event("warning", _command_scope(context), "manager command blocked", _command_detail(context, command_name))
        return ""

    handler_name = str(match.command.get("handler") or "")
    handler = HANDLERS.get(handler_name)
    if handler is None:
        log_event("error", _command_scope(context), "command handler missing", _command_detail(context, command_name))
        return "命令配置错误：没有找到对应处理器。"

    reply = handler(context, match.args)
    log_event("info", _command_scope(context), "command handled", _command_detail(context, command_name))
    return reply


def match_command(text: str, scope_type: str) -> CommandMatch | None:
    content = normalize_command_text(text)
    if not content.startswith("/"):
        return None

    commands = sorted(
        load_command_library(),
        key=lambda item: len(str(item.get("trigger") or "")),
        reverse=True,
    )

    for command in commands:
        scopes = command.get("scopes") or []
        if scopes and scope_type not in scopes:
            continue

        trigger = str(command.get("trigger") or "").strip()
        if not trigger:
            continue
        if content == trigger:
            return CommandMatch(command=command, args="")
        if content.startswith(trigger + " "):
            return CommandMatch(command=command, args=content[len(trigger):].strip())

    return None


def load_command_library() -> list[dict[str, Any]]:
    try:
        data = json.loads(COMMANDS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def is_memory_command_text(text: str) -> bool:
    match = match_command(text, "group") or match_command(text, "private")
    if not match:
        return False
    handler = str(match.command.get("handler") or "")
    return handler.startswith("memory_") or handler.startswith("learning_")


def is_manager(user_id: str) -> bool:
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


def _command_scope(context: CommandContext) -> str:
    return f"command:{context.scope_type}:{context.bot_qq}:{context.scope_id}"


def _command_detail(context: CommandContext, command_name: str) -> str:
    return json.dumps(
        {
            "command": command_name,
            "user_id": context.user_id,
            "scope_type": context.scope_type,
            "scope_id": context.scope_id,
        },
        ensure_ascii=False,
    )
