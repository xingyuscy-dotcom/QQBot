import asyncio
import json
import random
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .commands import CommandContext, dispatch_command, normalize_command_text
from .db import (
    ensure_conversation_config,
    get_conversation_config,
    get_conversation_reply_stats,
    log_event,
    record_conversation_ai_reply,
    record_llm_usage,
    save_conversation_message,
    upsert_bot_status,
)
from .llm_client import LLMError, generate_reply_result
from .memory_learner import update_learned_memory_if_needed
from .memory_store import bump_pending_message_count


router = APIRouter()
LEARNING_TASKS: set[str] = set()


@router.websocket("/onebot/ws")
async def onebot_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    current_bot_qq = "connected"
    client = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "unknown"
    upsert_bot_status(current_bot_qq, connected=True, nickname="NapCat connected")
    log_event("info", "onebot", "NapCat websocket connected", client)

    try:
        while True:
            event = await websocket.receive_json()
            bot_qq = event.get("self_id")
            if bot_qq is not None:
                if current_bot_qq == "connected":
                    upsert_bot_status(current_bot_qq, connected=False, nickname="NapCat connected")
                current_bot_qq = str(bot_qq)
                upsert_bot_status(current_bot_qq, connected=True)
            await handle_event(websocket, event)
    except WebSocketDisconnect:
        if current_bot_qq:
            upsert_bot_status(current_bot_qq, connected=False)
        log_event("info", "onebot", "NapCat websocket disconnected", client)
    except Exception as exc:
        if current_bot_qq:
            upsert_bot_status(current_bot_qq, connected=False)
        log_event("error", "onebot", "NapCat websocket error", repr(exc))
        await websocket.close()


async def handle_event(websocket: WebSocket, event: dict) -> None:
    post_type = event.get("post_type")

    if post_type == "meta_event":
        return

    if post_type != "message":
        if "echo" in event:
            log_event("info", "onebot", "OneBot action response", json.dumps(event, ensure_ascii=False))
        return

    bot_qq = str(event.get("self_id", "unknown"))
    message_type = event.get("message_type")
    text = extract_text(event)

    if not text:
        return

    if message_type == "group":
        await handle_group_message(websocket, event, bot_qq, text)
        return

    if message_type == "private":
        await handle_private_message(websocket, event, bot_qq, text)


async def handle_group_message(websocket: WebSocket, event: dict, bot_qq: str, text: str) -> None:
    group_id = str(event.get("group_id", ""))
    user_id = str(event.get("user_id", ""))
    message_id = str(event.get("message_id")) if event.get("message_id") is not None else None

    if not group_id or not user_id:
        return

    is_bot_message = user_id == bot_qq
    ensure_conversation_config(bot_qq, "group", group_id, f"群 {group_id}")
    log_event("info", f"group:{bot_qq}:{group_id}", "group message received", text[:500])

    if is_bot_message:
        save_conversation_message(bot_qq, "group", group_id, user_id, message_id, text, is_bot=True)
        return

    command_text = normalize_command_text(remove_at_bot(event, bot_qq, text))
    command_reply = dispatch_command(CommandContext(bot_qq, "group", group_id, user_id, command_text))
    if command_reply is not None:
        if command_reply:
            saved_reply = await send_group_command_reply(websocket, group_id, command_reply)
            save_conversation_message(bot_qq, "group", group_id, bot_qq, None, saved_reply, is_bot=True)
        return

    save_conversation_message(bot_qq, "group", group_id, user_id, message_id, text)
    config = get_conversation_config(bot_qq, "group", group_id)

    if config and int(config.get("learning_enabled") or 0) == 1:
        bump_pending_message_count(bot_qq, "group", group_id)
        schedule_memory_learning(bot_qq, "group", group_id)

    if not config or int(config["enabled"]) != 1:
        return

    should_reply, clean_text = should_reply_in_group(event, bot_qq, text, config)
    if not should_reply:
        return

    if is_test_ping(clean_text):
        await send_group_message(websocket, group_id, "QQbot_v2 已收到群消息，OneBot 通道正常。")
        return

    if not should_pass_reply_controls(bot_qq, "group", group_id, config):
        return

    try:
        result = await asyncio.to_thread(generate_reply_result, bot_qq, "group", group_id, config, clean_text)
    except LLMError as exc:
        log_event("error", f"group:{bot_qq}:{group_id}", "llm reply failed", str(exc))
        return
    except Exception as exc:
        log_event("error", f"group:{bot_qq}:{group_id}", "ai reply failed", repr(exc))
        return

    reply = result.reply
    await send_group_message(websocket, group_id, reply)
    save_conversation_message(bot_qq, "group", group_id, bot_qq, None, reply, is_bot=True)
    record_conversation_ai_reply(bot_qq, "group", group_id)
    record_llm_usage(bot_qq, "group", group_id, result.model, result.usage)


async def handle_private_message(websocket: WebSocket, event: dict, bot_qq: str, text: str) -> None:
    user_id = str(event.get("user_id", ""))
    message_id = str(event.get("message_id")) if event.get("message_id") is not None else None

    if not user_id:
        return

    is_bot_message = user_id == bot_qq
    ensure_conversation_config(bot_qq, "private", user_id, f"私聊 {user_id}")
    log_event("info", f"private:{bot_qq}:{user_id}", "private message received", text[:500])

    if is_bot_message:
        save_conversation_message(bot_qq, "private", user_id, user_id, message_id, text, is_bot=True)
        return

    command_reply = dispatch_command(CommandContext(bot_qq, "private", user_id, user_id, normalize_command_text(text)))
    if command_reply is not None:
        if command_reply:
            await send_private_message(websocket, user_id, command_reply)
            save_conversation_message(bot_qq, "private", user_id, bot_qq, None, command_reply, is_bot=True)
        return

    save_conversation_message(bot_qq, "private", user_id, user_id, message_id, text)
    config = get_conversation_config(bot_qq, "private", user_id)

    if config and int(config.get("learning_enabled") or 0) == 1:
        bump_pending_message_count(bot_qq, "private", user_id)
        schedule_memory_learning(bot_qq, "private", user_id)

    if not config or int(config["enabled"]) != 1:
        return

    if is_test_ping(text):
        await send_private_message(websocket, user_id, "QQbot_v2 已收到私聊消息，OneBot 通道正常。")
        return

    if not should_pass_reply_controls(bot_qq, "private", user_id, config):
        return

    try:
        result = await asyncio.to_thread(generate_reply_result, bot_qq, "private", user_id, config, text)
    except LLMError as exc:
        log_event("error", f"private:{bot_qq}:{user_id}", "llm reply failed", str(exc))
        return
    except Exception as exc:
        log_event("error", f"private:{bot_qq}:{user_id}", "ai reply failed", repr(exc))
        return

    reply = result.reply
    await send_private_message(websocket, user_id, reply)
    save_conversation_message(bot_qq, "private", user_id, bot_qq, None, reply, is_bot=True)
    record_conversation_ai_reply(bot_qq, "private", user_id)
    record_llm_usage(bot_qq, "private", user_id, result.model, result.usage)


def extract_text(event: dict) -> str:
    message = event.get("message")
    raw_message = event.get("raw_message")

    if isinstance(message, str):
        return message.strip()

    if isinstance(message, list):
        parts: list[str] = []
        for segment in message:
            if not isinstance(segment, dict):
                continue
            if segment.get("type") == "text":
                data = segment.get("data") or {}
                parts.append(str(data.get("text", "")))
        return "".join(parts).strip()

    return str(raw_message or "").strip()


def has_at_bot(event: dict, bot_qq: str) -> bool:
    message = event.get("message")
    if not isinstance(message, list):
        return False

    for segment in message:
        if not isinstance(segment, dict) or segment.get("type") != "at":
            continue
        data = segment.get("data") or {}
        if str(data.get("qq", "")) == bot_qq:
            return True
    return False


def remove_at_bot(event: dict, bot_qq: str, text: str) -> str:
    message = event.get("message")
    if not isinstance(message, list):
        return text.strip()

    parts: list[str] = []
    for segment in message:
        if not isinstance(segment, dict):
            continue
        segment_type = segment.get("type")
        data = segment.get("data") or {}
        if segment_type == "at" and str(data.get("qq", "")) == bot_qq:
            continue
        if segment_type == "text":
            parts.append(str(data.get("text", "")))
    return "".join(parts).strip()


def should_reply_in_group(event: dict, bot_qq: str, text: str, config: dict) -> tuple[bool, str]:
    mode = str(config.get("response_mode") or "mention")
    prefix = str(config.get("trigger_prefix") or "").strip()

    if mode == "all":
        return True, text

    if mode == "prefix":
        if prefix and text.startswith(prefix):
            return True, text[len(prefix):].strip()
        return False, text

    if has_at_bot(event, bot_qq):
        return True, remove_at_bot(event, bot_qq, text)
    return False, text


def is_test_ping(text: str) -> bool:
    return text.strip().lower() in {"/qqbot ping", "/ping", "qqbot ping"}


def should_pass_reply_controls(bot_qq: str, scope_type: str, scope_id: str, config: dict) -> bool:
    cooldown = parse_int(config.get("reply_cooldown_seconds"), 0)
    probability = min(1, max(0, parse_float(config.get("reply_probability"), 1)))
    hourly_limit = parse_int(config.get("hourly_reply_limit"), 0)
    stats = get_conversation_reply_stats(bot_qq, scope_type, scope_id)
    scope = f"{scope_type}:{bot_qq}:{scope_id}"

    if cooldown > 0 and stats.get("latest_reply_at"):
        latest = parse_datetime(str(stats["latest_reply_at"]))
        if latest:
            elapsed = (datetime.now(timezone.utc) - latest).total_seconds()
            if elapsed < cooldown:
                remaining = max(1, int(cooldown - elapsed))
                log_event("info", scope, "ai reply skipped by cooldown", f"{remaining}s remaining")
                return False

    hourly_count = int(stats.get("hourly_reply_count") or 0)
    if hourly_limit > 0 and hourly_count >= hourly_limit:
        log_event("info", scope, "ai reply skipped by hourly limit", f"{hourly_count}/{hourly_limit}")
        return False

    if probability < 1 and random.random() > probability:
        log_event("info", scope, "ai reply skipped by probability", str(probability))
        return False

    return True


def parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_int(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def parse_float(value, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def schedule_memory_learning(bot_qq: str, scope_type: str, scope_id: str) -> None:
    key = f"{bot_qq}:{scope_type}:{scope_id}"
    if key in LEARNING_TASKS:
        return

    LEARNING_TASKS.add(key)
    asyncio.create_task(run_memory_learning(bot_qq, scope_type, scope_id, key))


async def run_memory_learning(bot_qq: str, scope_type: str, scope_id: str, key: str) -> None:
    try:
        updated = await asyncio.to_thread(update_learned_memory_if_needed, bot_qq, scope_type, scope_id)
        if updated:
            log_event("info", f"{scope_type}:{bot_qq}:{scope_id}", "learned memory updated")
    except Exception as exc:
        log_event("error", f"{scope_type}:{bot_qq}:{scope_id}", "learned memory update failed", repr(exc))
    finally:
        LEARNING_TASKS.discard(key)


async def send_group_message(websocket: WebSocket, group_id: str, text: str) -> None:
    await websocket.send_json(
        {
            "action": "send_group_msg",
            "params": {
                "group_id": group_id,
                "message": [{"type": "text", "data": {"text": text}}],
            },
            "echo": f"qqbot_v2:{uuid4().hex}",
        }
    )


async def send_group_command_reply(websocket: WebSocket, group_id: str, reply) -> str:
    if isinstance(reply, dict) and reply.get("type") == "group_at":
        qq = str(reply.get("qq") or "").strip()
        text = str(reply.get("text") or "").strip()
        await send_group_at_message(websocket, group_id, qq, text)
        return f"@{qq} {text}".strip()

    text = str(reply)
    await send_group_message(websocket, group_id, text)
    return text


async def send_group_at_message(websocket: WebSocket, group_id: str, qq: str, text: str) -> None:
    await websocket.send_json(
        {
            "action": "send_group_msg",
            "params": {
                "group_id": group_id,
                "message": [
                    {"type": "at", "data": {"qq": qq}},
                    {"type": "text", "data": {"text": f" {text}"}},
                ],
            },
            "echo": f"qqbot_v2:{uuid4().hex}",
        }
    )


async def send_private_message(websocket: WebSocket, user_id: str, text: str) -> None:
    await websocket.send_json(
        {
            "action": "send_private_msg",
            "params": {
                "user_id": user_id,
                "message": [{"type": "text", "data": {"text": text}}],
            },
            "echo": f"qqbot_v2:{uuid4().hex}",
        }
    )
