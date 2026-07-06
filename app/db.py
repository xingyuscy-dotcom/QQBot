import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from .paths import DATA_DIR, DB_PATH, LOGS_DIR
from .prompts import DEFAULT_GLOBAL_SYSTEM_PROMPT
from .settings import DEFAULT_SETTINGS


ScopeType = Literal["group", "private"]


def now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path = DB_PATH) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bot_status (
              bot_qq TEXT PRIMARY KEY,
              nickname TEXT,
              connected INTEGER NOT NULL DEFAULT 0,
              last_seen_at TEXT
            );

            CREATE TABLE IF NOT EXISTS conversation_configs (
              bot_qq TEXT NOT NULL,
              scope_type TEXT NOT NULL,
              scope_id TEXT NOT NULL,
              display_name TEXT NOT NULL DEFAULT '',
              enabled INTEGER NOT NULL DEFAULT 0,
              response_mode TEXT NOT NULL DEFAULT 'mention',
              trigger_prefix TEXT NOT NULL DEFAULT '/bot',
              learning_enabled INTEGER NOT NULL DEFAULT 1,
              learning_batch_size INTEGER NOT NULL DEFAULT 0,
              persona TEXT NOT NULL DEFAULT '',
              reply_cooldown_seconds INTEGER NOT NULL DEFAULT 0,
              reply_probability REAL NOT NULL DEFAULT 1,
              hourly_reply_limit INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (bot_qq, scope_type, scope_id)
            );

            CREATE TABLE IF NOT EXISTS conversation_messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              bot_qq TEXT NOT NULL,
              scope_type TEXT NOT NULL,
              scope_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              message_id TEXT,
              text TEXT NOT NULL,
              is_bot INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_conversation_messages_scope
            ON conversation_messages (bot_qq, scope_type, scope_id, id);

            CREATE TABLE IF NOT EXISTS conversation_styles (
              bot_qq TEXT NOT NULL,
              scope_type TEXT NOT NULL,
              scope_id TEXT NOT NULL,
              tone TEXT NOT NULL DEFAULT '',
              common_phrases TEXT NOT NULL DEFAULT '',
              common_topics TEXT NOT NULL DEFAULT '',
              joke_patterns TEXT NOT NULL DEFAULT '',
              reply_style TEXT NOT NULL DEFAULT '',
              examples_json TEXT NOT NULL DEFAULT '[]',
              updated_at TEXT NOT NULL,
              PRIMARY KEY (bot_qq, scope_type, scope_id)
            );

            CREATE TABLE IF NOT EXISTS runtime_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              level TEXT NOT NULL,
              scope TEXT NOT NULL,
              message TEXT NOT NULL,
              detail TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversation_reply_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              bot_qq TEXT NOT NULL,
              scope_type TEXT NOT NULL,
              scope_id TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_conversation_reply_events_scope
            ON conversation_reply_events (bot_qq, scope_type, scope_id, created_at);

            CREATE TABLE IF NOT EXISTS llm_usage_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              bot_qq TEXT NOT NULL,
              scope_type TEXT NOT NULL,
              scope_id TEXT NOT NULL,
              model TEXT NOT NULL DEFAULT '',
              prompt_tokens INTEGER NOT NULL DEFAULT 0,
              completion_tokens INTEGER NOT NULL DEFAULT 0,
              total_tokens INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_llm_usage_events_scope
            ON llm_usage_events (bot_qq, scope_type, scope_id, created_at);

            CREATE TABLE IF NOT EXISTS group_configs (
              bot_qq TEXT NOT NULL,
              group_id TEXT NOT NULL,
              group_name TEXT,
              enabled INTEGER NOT NULL DEFAULT 0,
              response_mode TEXT NOT NULL DEFAULT 'mention',
              trigger_prefix TEXT NOT NULL DEFAULT '',
              learning_enabled INTEGER NOT NULL DEFAULT 1,
              persona TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL,
              PRIMARY KEY (bot_qq, group_id)
            );

            CREATE TABLE IF NOT EXISTS group_messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              bot_qq TEXT NOT NULL,
              group_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              message_id TEXT,
              text TEXT NOT NULL,
              is_bot INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS group_styles (
              bot_qq TEXT NOT NULL,
              group_id TEXT NOT NULL,
              tone TEXT NOT NULL DEFAULT '',
              common_phrases TEXT NOT NULL DEFAULT '',
              common_topics TEXT NOT NULL DEFAULT '',
              joke_patterns TEXT NOT NULL DEFAULT '',
              reply_style TEXT NOT NULL DEFAULT '',
              examples_json TEXT NOT NULL DEFAULT '[]',
              updated_at TEXT NOT NULL,
              PRIMARY KEY (bot_qq, group_id)
            );
            """
        )
        seed_default_settings(conn)
        migrate_conversation_rate_fields(conn)
        migrate_default_model_name(conn)
        repair_corrupt_chinese_text(conn)
        migrate_legacy_group_data(conn)


def seed_default_settings(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
        DEFAULT_SETTINGS.items(),
    )


def migrate_default_model_name(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE app_settings
        SET value = 'deepseek-v4-flash'
        WHERE key = 'llm.model' AND value = 'deepseek4'
        """
    )


def migrate_conversation_rate_fields(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(conversation_configs)").fetchall()
    }
    migrations = {
        "reply_cooldown_seconds": "ALTER TABLE conversation_configs ADD COLUMN reply_cooldown_seconds INTEGER NOT NULL DEFAULT 0",
        "reply_probability": "ALTER TABLE conversation_configs ADD COLUMN reply_probability REAL NOT NULL DEFAULT 1",
        "hourly_reply_limit": "ALTER TABLE conversation_configs ADD COLUMN hourly_reply_limit INTEGER NOT NULL DEFAULT 0",
        "learning_batch_size": "ALTER TABLE conversation_configs ADD COLUMN learning_batch_size INTEGER NOT NULL DEFAULT 0",
    }
    for column, sql in migrations.items():
        if column not in columns:
            conn.execute(sql)
    conn.execute(
        """
        UPDATE conversation_configs
        SET learning_batch_size = 0
        WHERE learning_batch_size IS NULL
        """
    )


def repair_corrupt_chinese_text(conn: sqlite3.Connection) -> None:
    default_prompt = DEFAULT_GLOBAL_SYSTEM_PROMPT
    conn.execute(
        """
        UPDATE app_settings
        SET value = ?
        WHERE key = 'bot.global_system_prompt'
          AND (
            value = ''
            OR value LIKE '%???%'
            OR value LIKE '%锛%'
            OR value LIKE '%鑱%'
            OR value LIKE '%ç%'
            OR value LIKE '%ä%'
            OR value LIKE '%æ%'
            OR value LIKE '%ã%'
          )
        """,
        (default_prompt,),
    )
    conn.execute(
        """
        UPDATE conversation_configs
        SET display_name = CASE
          WHEN scope_type = 'group' THEN '群 ' || scope_id
          ELSE '私聊 ' || scope_id
        END
        WHERE display_name LIKE '%???%'
           OR display_name LIKE '%锛%'
           OR display_name LIKE '%鑱%'
           OR display_name LIKE '%ç%'
           OR display_name LIKE '%ä%'
           OR display_name LIKE '%æ%'
           OR display_name LIKE '%ã%'
        """
    )


def migrate_legacy_group_data(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO conversation_configs
          (bot_qq, scope_type, scope_id, display_name, enabled, response_mode,
           trigger_prefix, learning_enabled, persona, updated_at)
        SELECT
          bot_qq,
          'group',
          group_id,
          CASE
            WHEN group_name IS NOT NULL AND group_name != '' THEN group_name
            ELSE '群 ' || group_id
          END,
          enabled,
          response_mode,
          trigger_prefix,
          learning_enabled,
          persona,
          updated_at
        FROM group_configs
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO conversation_messages
          (id, bot_qq, scope_type, scope_id, user_id, message_id, text, is_bot, created_at)
        SELECT id, bot_qq, 'group', group_id, user_id, message_id, text, is_bot, created_at
        FROM group_messages
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO conversation_styles
          (bot_qq, scope_type, scope_id, tone, common_phrases, common_topics,
           joke_patterns, reply_style, examples_json, updated_at)
        SELECT
          bot_qq,
          'group',
          group_id,
          tone,
          common_phrases,
          common_topics,
          joke_patterns,
          reply_style,
          examples_json,
          updated_at
        FROM group_styles
        """
    )


def log_event(level: str, scope: str, message: str, detail: str = "") -> None:
    created_at = now_text()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO runtime_logs (level, scope, message, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (level, scope, message, detail, created_at),
        )
    write_file_log(level, scope, message, detail, created_at)


def write_file_log(level: str, scope: str, message: str, detail: str, created_at: str) -> None:
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        date_text = created_at[:10]
        record = {
            "time": created_at,
            "level": level,
            "scope": scope,
            "message": message,
            "detail": detail,
        }
        with (LOGS_DIR / f"{date_text}.log").open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def upsert_bot_status(bot_qq: str, connected: bool, nickname: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO bot_status (bot_qq, nickname, connected, last_seen_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(bot_qq) DO UPDATE SET
              nickname = COALESCE(excluded.nickname, bot_status.nickname),
              connected = excluded.connected,
              last_seen_at = excluded.last_seen_at
            """,
            (bot_qq, nickname, int(connected), now_text()),
        )


def ensure_conversation_config(
    bot_qq: str,
    scope_type: ScopeType,
    scope_id: str,
    display_name: str = "",
) -> None:
    default_response_mode = "mention" if scope_type == "group" else "all"
    default_trigger_prefix = "/bot"
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO conversation_configs
              (bot_qq, scope_type, scope_id, display_name, enabled, response_mode,
               trigger_prefix, learning_enabled, learning_batch_size, persona, updated_at)
            VALUES (?, ?, ?, ?, 0, ?, ?, 1, 0, '', ?)
            ON CONFLICT(bot_qq, scope_type, scope_id) DO UPDATE SET
              display_name = CASE
                WHEN excluded.display_name != '' THEN excluded.display_name
                ELSE conversation_configs.display_name
              END
            """,
            (
                bot_qq,
                scope_type,
                scope_id,
                display_name,
                default_response_mode,
                default_trigger_prefix,
                now_text(),
            ),
        )


def get_conversation_config(
    bot_qq: str,
    scope_type: ScopeType,
    scope_id: str,
) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
              bot_qq,
              scope_type,
              scope_id,
              display_name,
              enabled,
              response_mode,
              trigger_prefix,
              learning_enabled,
              learning_batch_size,
              persona,
              reply_cooldown_seconds,
              reply_probability,
              hourly_reply_limit,
              updated_at
            FROM conversation_configs
            WHERE bot_qq = ? AND scope_type = ? AND scope_id = ?
            """,
            (bot_qq, scope_type, scope_id),
        ).fetchone()
    return dict(row) if row else None


def update_conversation_persona(
    bot_qq: str,
    scope_type: ScopeType,
    scope_id: str,
    persona: str,
) -> bool:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE conversation_configs
            SET persona = ?, updated_at = ?
            WHERE bot_qq = ? AND scope_type = ? AND scope_id = ?
            """,
            (persona.strip(), now_text(), bot_qq, scope_type, scope_id),
        )
    return cursor.rowcount > 0


def get_conversation_reply_stats(
    bot_qq: str,
    scope_type: ScopeType,
    scope_id: str,
) -> dict[str, Any]:
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with connect() as conn:
        latest = conn.execute(
            """
            SELECT created_at
            FROM conversation_reply_events
            WHERE bot_qq = ? AND scope_type = ? AND scope_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (bot_qq, scope_type, scope_id),
        ).fetchone()
        hourly_count = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM conversation_reply_events
            WHERE bot_qq = ? AND scope_type = ? AND scope_id = ? AND created_at >= ?
            """,
            (bot_qq, scope_type, scope_id, one_hour_ago),
        ).fetchone()["c"]

    return {
        "latest_reply_at": latest["created_at"] if latest else None,
        "hourly_reply_count": hourly_count,
    }


def record_conversation_ai_reply(
    bot_qq: str,
    scope_type: ScopeType,
    scope_id: str,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO conversation_reply_events (bot_qq, scope_type, scope_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (bot_qq, scope_type, scope_id, now_text()),
        )


def record_llm_usage(
    bot_qq: str,
    scope_type: ScopeType,
    scope_id: str,
    model: str,
    usage: dict[str, Any] | None,
) -> None:
    usage = usage or {}
    prompt_tokens = parse_int(usage.get("prompt_tokens"), 0)
    completion_tokens = parse_int(usage.get("completion_tokens"), 0)
    total_tokens = parse_int(usage.get("total_tokens"), prompt_tokens + completion_tokens)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO llm_usage_events
              (bot_qq, scope_type, scope_id, model, prompt_tokens, completion_tokens, total_tokens, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bot_qq,
                scope_type,
                scope_id,
                model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                now_text(),
            ),
        )


def parse_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def update_conversation_learning(
    bot_qq: str,
    scope_type: ScopeType,
    scope_id: str,
    learning_enabled: bool | None = None,
    learning_batch_size: int | None = None,
) -> bool:
    updates = []
    values: list[Any] = []
    if learning_enabled is not None:
        updates.append("learning_enabled = ?")
        values.append(int(learning_enabled))
    if learning_batch_size is not None:
        updates.append("learning_batch_size = ?")
        values.append(max(0, learning_batch_size))
    if not updates:
        return False

    updates.append("updated_at = ?")
    values.append(now_text())
    values.extend([bot_qq, scope_type, scope_id])
    with connect() as conn:
        cursor = conn.execute(
            f"""
            UPDATE conversation_configs
            SET {", ".join(updates)}
            WHERE bot_qq = ? AND scope_type = ? AND scope_id = ?
            """,
            values,
        )
    return cursor.rowcount > 0


def ensure_group_config(bot_qq: str, group_id: str) -> None:
    ensure_conversation_config(bot_qq, "group", group_id, f"群 {group_id}")


def save_conversation_message(
    bot_qq: str,
    scope_type: ScopeType,
    scope_id: str,
    user_id: str,
    message_id: str | None,
    text: str,
    is_bot: bool = False,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO conversation_messages
              (bot_qq, scope_type, scope_id, user_id, message_id, text, is_bot, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (bot_qq, scope_type, scope_id, user_id, message_id, text, int(is_bot), now_text()),
        )


def get_recent_conversation_messages(
    bot_qq: str,
    scope_type: ScopeType,
    scope_id: str,
    limit: int = 30,
) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT user_id, text, is_bot, created_at
            FROM conversation_messages
            WHERE bot_qq = ? AND scope_type = ? AND scope_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (bot_qq, scope_type, scope_id, limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def save_group_message(
    bot_qq: str,
    group_id: str,
    user_id: str,
    message_id: str | None,
    text: str,
    is_bot: bool = False,
) -> None:
    save_conversation_message(bot_qq, "group", group_id, user_id, message_id, text, is_bot)


def latest_bot_status() -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT bot_qq, nickname, connected, last_seen_at
            FROM bot_status
            ORDER BY last_seen_at DESC
            LIMIT 1
            """
        ).fetchone()
    return dict(row) if row else None
