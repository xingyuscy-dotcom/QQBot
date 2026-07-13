import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .paths import HOT_DB_PATH


def connect_hot(db_path: Path = HOT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_hot_db(db_path: Path = HOT_DB_PATH) -> None:
    with connect_hot(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS hot_items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_date TEXT NOT NULL,
              title TEXT NOT NULL,
              summary TEXT NOT NULL DEFAULT '',
              source TEXT NOT NULL DEFAULT '',
              rank INTEGER NOT NULL DEFAULT 0,
              heat TEXT NOT NULL DEFAULT '',
              category TEXT NOT NULL DEFAULT '',
              source_url TEXT NOT NULL DEFAULT '',
              keywords TEXT NOT NULL DEFAULT '',
              fetched_at TEXT NOT NULL,
              UNIQUE(event_date, title, source_url)
            );

            CREATE INDEX IF NOT EXISTS idx_hot_items_date
            ON hot_items (event_date);

            CREATE INDEX IF NOT EXISTS idx_hot_items_source
            ON hot_items (source);

            CREATE INDEX IF NOT EXISTS idx_hot_items_keywords
            ON hot_items (keywords);
            """
        )


def upsert_hot_items(items: list[dict[str, Any]]) -> int:
    init_hot_db()
    count = 0
    fallback_fetched_at = now_text()
    with connect_hot() as conn:
        for item in items:
            event_date = str(item.get("event_date") or "").strip()
            title = normalize_space(item.get("title"))
            if not event_date or not title:
                continue

            summary = trim_text(item.get("summary") or title, 280)
            category = trim_text(item.get("category"), 60)
            source_url = trim_text(item.get("source_url"), 500)
            source = trim_text(item.get("source") or extract_hot_source(title, summary, category), 40)
            rank = parse_hot_rank(item.get("rank") or title or summary)
            heat = trim_text(item.get("heat"), 80)
            fetched_at = trim_text(item.get("fetched_at") or fallback_fetched_at, 80)
            keywords = build_keywords(" ".join([title, summary, source, category]))
            conn.execute(
                """
                INSERT INTO hot_items
                  (event_date, title, summary, source, rank, heat, category, source_url, keywords, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_date, title, source_url) DO UPDATE SET
                  summary = excluded.summary,
                  source = excluded.source,
                  rank = excluded.rank,
                  heat = excluded.heat,
                  category = excluded.category,
                  keywords = excluded.keywords,
                  fetched_at = excluded.fetched_at
                """,
                (event_date, title, summary, source, rank, heat, category, source_url, keywords, fetched_at),
            )
            count += 1
    return count


def purge_old_hot_topics(days: int = 3) -> int:
    init_hot_db()
    cutoff = datetime.now(timezone.utc).date().toordinal() - max(1, int(days))
    deleted = 0
    with connect_hot() as conn:
        rows = conn.execute("SELECT id, event_date FROM hot_items").fetchall()
        for row in rows:
            item_date = parse_date(str(row["event_date"] or ""))
            if item_date and item_date.toordinal() < cutoff:
                conn.execute("DELETE FROM hot_items WHERE id = ?", (row["id"],))
                deleted += 1
    return deleted


def list_hot_topics(source: str = "", limit: int = 10) -> list[dict[str, Any]]:
    try:
        init_hot_db()
    except (OSError, sqlite3.Error):
        return []

    normalized_source = normalize_hot_source(source)
    values: list[Any] = []
    where = "1 = 1"
    if normalized_source:
        where += " AND source = ?"
        values.append(normalized_source)

    limit = min(50, max(1, int(limit)))
    try:
        with connect_hot() as conn:
            rows = conn.execute(
                f"""
                SELECT event_date, title, summary, source, rank, heat, category, source_url, fetched_at
                FROM hot_items
                WHERE {where}
                ORDER BY fetched_at DESC, event_date DESC, rank ASC, id DESC
                LIMIT ?
                """,
                [*values, limit],
            ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def search_hot_topics(query: str, limit: int = 5) -> list[dict[str, Any]]:
    try:
        init_hot_db()
    except (OSError, sqlite3.Error):
        return []

    tokens = tokenize(query)
    if not tokens or is_broad_hot_query(query, tokens):
        return list_hot_topics(limit=limit) if has_hot_intent(query) else []

    try:
        from .rag_store import search_rag

        rag_items = search_rag(query, "hot", limit)
        if rag_items is not None:
            return rag_items
    except Exception:
        pass

    query_tokens = searchable_tokens(tokens)
    where = " OR ".join(["title LIKE ? OR summary LIKE ? OR keywords LIKE ?" for _ in query_tokens])
    values: list[str] = []
    for token in query_tokens:
        pattern = f"%{token}%"
        values.extend([pattern, pattern, pattern])

    limit = min(20, max(1, int(limit)))
    try:
        with connect_hot() as conn:
            rows = conn.execute(
                f"""
                SELECT event_date, title, summary, source, rank, heat, category, source_url, fetched_at
                FROM hot_items
                WHERE {where}
                ORDER BY fetched_at DESC, event_date DESC, rank ASC, id DESC
                LIMIT ?
                """,
                [*values, max(80, limit * 12)],
            ).fetchall()
    except sqlite3.Error:
        return []

    scored = []
    for row in rows:
        item = dict(row)
        score = hot_match_score(item, tokens)
        if score > 0:
            scored.append((score, item))
    scored.sort(
        key=lambda pair: (
            pair[0],
            pair[1].get("fetched_at") or "",
            pair[1].get("event_date") or "",
            -int(pair[1].get("rank") or 9999),
        ),
        reverse=True,
    )
    return [item for _, item in scored[:limit]]


def hot_stats() -> dict[str, Any]:
    try:
        init_hot_db()
        with connect_hot() as conn:
            row = conn.execute(
                """
                SELECT
                  COUNT(*) AS item_count,
                  MIN(event_date) AS first_date,
                  MAX(event_date) AS latest_date,
                  MAX(fetched_at) AS latest_fetched_at
                FROM hot_items
                """
            ).fetchone()
    except (OSError, sqlite3.Error) as exc:
        return {
            "item_count": 0,
            "first_date": None,
            "latest_date": None,
            "latest_fetched_at": None,
            "error": str(exc)[:300],
        }
    return dict(row) if row else {}


def format_hot_for_prompt(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""

    lines = []
    for item in items:
        rank = int(item.get("rank") or 0)
        rank_text = f" #{rank}" if rank else ""
        source = str(item.get("source") or "热点").strip()
        title = normalize_space(item.get("title"))
        summary = normalize_space(item.get("summary"))
        lines.append(f"[{item.get('event_date', '-') } {source}{rank_text}] {title}：{summary}")
    return (
        "本地热点库检索结果（短期热榜线索，只在和当前消息相关时使用；热榜不等于事实结论，回答要保留不确定性，不要编造热点库没有的细节）：\n"
        + "\n".join(lines)
    )


def format_hot_items_for_debug(items: list[dict[str, Any]]) -> str:
    if not items:
        return "未命中热点库"
    lines = []
    for item in items:
        rank = int(item.get("rank") or 0)
        rank_text = f"#{rank}" if rank else "#-"
        score = item.get("rag_score")
        score_text = f" RAG:{float(score):.3f}" if score is not None else ""
        lines.append(
            f"[{item.get('event_date', '-')} {item.get('source', '热点')} {rank_text}{score_text}] "
            f"{normalize_space(item.get('title'))}: {normalize_space(item.get('summary'))}"
        )
    return "\n".join(lines)


def normalize_hot_source(value: str) -> str:
    raw = normalize_space(value).lower()
    mapping = {
        "微博": "微博",
        "weibo": "微博",
        "百度": "百度",
        "baidu": "百度",
        "知乎": "知乎",
        "zhihu": "知乎",
        "b站": "B站",
        "哔哩哔哩": "B站",
        "bilibili": "B站",
        "github": "GitHub",
        "gh": "GitHub",
        "科技": "GitHub",
    }
    return mapping.get(raw, "")


def has_hot_intent(text: str) -> bool:
    content = normalize_space(text)
    return any(
        word in content
        for word in ("热点", "热搜", "热榜", "热门", "上热搜", "新闻", "最近", "今天", "现在", "发生", "什么情况", "咋回事", "怎么看")
    )


def hot_match_score(item: dict[str, Any], tokens: list[str]) -> int:
    title = normalize_space(item.get("title"))
    summary = normalize_space(item.get("summary"))
    content = normalize_space(" ".join([title, summary, item.get("source"), item.get("category")]))
    score = 0
    for token in tokens:
        if content_has_token(title, token):
            score += 6
        elif content_has_token(summary, token):
            score += 3
        elif content_has_token(content, token):
            score += 1
    rank = int(item.get("rank") or 0)
    if 0 < rank <= 5:
        score += 1
    return score


def tokenize(text: str) -> list[str]:
    content = normalize_space(text).lower()
    stop_words = {
        "这个",
        "那个",
        "什么",
        "怎么",
        "为啥",
        "为什么",
        "多少",
        "哪个",
        "哪些",
        "情况",
        "内容",
        "热点",
        "热搜",
        "热榜",
        "热门",
        "新闻",
        "最近",
        "今天",
        "现在",
        "发生",
        "怎么看",
        "咋回事",
        "the",
        "and",
        "what",
        "why",
        "how",
    }
    content = content.replace("什么情况", " ").replace("咋回事", " ").replace("怎么看", " ")
    for word in stop_words:
        content = content.replace(word, " ")

    words = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9][a-zA-Z0-9_\-]{2,}", content)
    result: list[str] = []
    seen: set[str] = set()
    for word in words:
        if word in stop_words or word in seen:
            continue
        seen.add(word)
        result.append(word)
        if len(result) >= 8:
            break
    return result


def is_broad_hot_query(query: str, tokens: list[str]) -> bool:
    if not has_hot_intent(query):
        return False
    return not tokens or all(token in {"一下", "看看", "聊聊", "说说"} for token in tokens)


def searchable_tokens(tokens: list[str]) -> list[str]:
    return [token for token in tokens if not token.isdigit()] or tokens


def build_keywords(text: str) -> str:
    return " ".join(tokenize(text))


def extract_hot_source(title: str, summary: str, category: str) -> str:
    parts = category.split("/")
    if len(parts) >= 2 and parts[0] == "热点":
        return parts[1]
    match = re.search(r"来源[:：]([^；;]+)", summary)
    if match:
        return match.group(1).strip()
    match = re.match(r"(.+?)热榜第\d+名", title)
    return match.group(1).strip() if match else "热点"


def parse_hot_rank(value: Any) -> int:
    text = normalize_space(value)
    if text.isdigit():
        return int(text)
    match = re.search(r"(?:第|排名[:：]?)(\d+)", text)
    return int(match.group(1)) if match else 0


def content_has_token(content: str, token: str) -> bool:
    content = str(content or "")
    token = str(token or "").strip()
    if not token:
        return False
    if re.search(r"[\u4e00-\u9fff]", token):
        return token in content
    return bool(re.search(rf"(?<![a-zA-Z0-9]){re.escape(token)}(?![a-zA-Z0-9])", content, flags=re.IGNORECASE))


def now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def trim_text(value: Any, limit: int) -> str:
    return normalize_space(value)[:limit]


def parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
