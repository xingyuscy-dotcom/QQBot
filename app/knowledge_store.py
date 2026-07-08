import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .paths import KNOWLEDGE_DB_PATH


ALIASES = {
    "特朗普": ["Trump"],
    "拜登": ["Biden"],
    "美国": ["United States", "US"],
    "中国": ["China"],
    "俄乌": ["Russia", "Ukraine"],
    "乌克兰": ["Ukraine"],
    "俄罗斯": ["Russia"],
    "以色列": ["Israel"],
    "加沙": ["Gaza"],
    "巴勒斯坦": ["Palestine"],
    "人工智能": ["AI", "artificial intelligence"],
    "大模型": ["AI"],
    "英伟达": ["Nvidia"],
    "游戏": ["game", "gaming"],
    "电竞": ["esports", "e-sports"],
    "动漫": ["anime", "manga"],
    "动画": ["anime"],
    "漫画": ["manga"],
    "新番": ["anime"],
    "番剧": ["anime"],
    "Steam": ["steam"],
    "任天堂": ["Nintendo"],
    "索尼": ["Sony", "PlayStation"],
    "微软": ["Microsoft", "Xbox"],
    "英雄联盟": ["League of Legends", "LoL"],
    "LOL": ["League of Legends", "LoL"],
    "S赛": ["Worlds", "World Championship"],
    "世界赛": ["Worlds", "World Championship"],
    "全球总决赛": ["Worlds", "World Championship"],
    "T1": ["T1"],
    "KT": ["KT", "KT Rolster"],
    "LPL": ["LPL"],
    "LCK": ["LCK"],
    "成都": ["Chengdu"],
    "王者荣耀": ["Honor of Kings"],
    "原神": ["Genshin Impact"],
    "明日方舟": ["Arknights"],
    "崩坏": ["Honkai"],
}

LOL_QUERY_TOKENS = {
    "英雄联盟",
    "league of legends",
    "lol",
    "lpl",
    "lck",
    "t1",
    "kt",
    "kt rolster",
    "s赛",
    "世界赛",
    "全球总决赛",
    "worlds",
    "world championship",
}

LOL_CONTENT_TOKENS = {
    "英雄联盟",
    "league of legends",
    "lol",
    "lpl",
    "lck",
    "t1",
    "kt rolster",
}

BROAD_EVENT_TOKENS = {
    "s赛",
    "世界赛",
    "全球总决赛",
    "worlds",
    "world championship",
    "championship",
    "赛季",
    "赛事",
    "比赛",
    "赛程",
    "联赛",
}


def connect_knowledge(db_path: Path = KNOWLEDGE_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_knowledge_db(db_path: Path = KNOWLEDGE_DB_PATH) -> None:
    with connect_knowledge(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_date TEXT NOT NULL,
              title TEXT NOT NULL,
              summary TEXT NOT NULL DEFAULT '',
              category TEXT NOT NULL DEFAULT '',
              source_url TEXT NOT NULL DEFAULT '',
              keywords TEXT NOT NULL DEFAULT '',
              fetched_at TEXT NOT NULL,
              UNIQUE(event_date, title, source_url)
            );

            CREATE INDEX IF NOT EXISTS idx_knowledge_items_date
            ON knowledge_items (event_date);

            CREATE INDEX IF NOT EXISTS idx_knowledge_items_keywords
            ON knowledge_items (keywords);
            """
        )


def purge_old_hot_topics(days: int = 3) -> int:
    init_knowledge_db()
    cutoff = datetime.now(timezone.utc).date().toordinal() - max(1, int(days))
    deleted = 0
    with connect_knowledge() as conn:
        rows = conn.execute(
            """
            SELECT id, event_date
            FROM knowledge_items
            WHERE category LIKE '热点/%'
            """
        ).fetchall()
        for row in rows:
            item_date = parse_date(str(row["event_date"] or ""))
            if item_date and item_date.toordinal() < cutoff:
                conn.execute("DELETE FROM knowledge_items WHERE id = ?", (row["id"],))
                deleted += 1
    return deleted


def upsert_knowledge_items(items: list[dict[str, Any]]) -> int:
    init_knowledge_db()
    fetched_at = now_text()
    count = 0
    with connect_knowledge() as conn:
        for item in items:
            event_date = str(item.get("event_date") or "").strip()
            title = normalize_space(item.get("title"))
            if not event_date or not title:
                continue

            summary = trim_text(item.get("summary") or title, 280)
            category = trim_text(item.get("category"), 40)
            source_url = trim_text(item.get("source_url"), 500)
            keywords = build_keywords(" ".join([title, summary, category]))
            conn.execute(
                """
                INSERT INTO knowledge_items
                  (event_date, title, summary, category, source_url, keywords, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_date, title, source_url) DO UPDATE SET
                  summary = excluded.summary,
                  category = excluded.category,
                  keywords = excluded.keywords,
                  fetched_at = excluded.fetched_at
                """,
                (event_date, title, summary, category, source_url, keywords, fetched_at),
            )
            count += 1
    return count


def search_knowledge(query: str, limit: int = 8) -> list[dict[str, Any]]:
    try:
        init_knowledge_db()
    except (OSError, sqlite3.Error):
        return []
    tokens = tokenize(query)
    if not tokens:
        return []

    years = extract_query_years(query)
    query_tokens = searchable_tokens(tokens)
    where = " OR ".join(["title LIKE ? OR summary LIKE ? OR keywords LIKE ?" for _ in query_tokens])
    values: list[str] = []
    for token in query_tokens:
        pattern = f"%{token}%"
        values.extend([pattern, pattern, pattern])

    limit = min(20, max(1, int(limit)))
    try:
        with connect_knowledge() as conn:
            rows = conn.execute(
                f"""
                SELECT event_date, title, summary, category, source_url
                FROM knowledge_items
                WHERE {where}
                ORDER BY event_date DESC, id DESC
                LIMIT ?
                """,
                [*values, max(200, limit * 20)],
            ).fetchall()
    except sqlite3.Error:
        return []

    scored = []
    for row in rows:
        item = dict(row)
        score = knowledge_match_score(item, tokens, years)
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda pair: (pair[0], pair[1].get("event_date") or ""), reverse=True)
    return [item for _, item in scored[:limit]]


def list_hot_topics(source: str = "", limit: int = 10) -> list[dict[str, Any]]:
    try:
        init_knowledge_db()
    except (OSError, sqlite3.Error):
        return []

    normalized_source = normalize_hot_source(source)
    values: list[Any] = []
    where = "category LIKE '热点/%'"
    if normalized_source:
        where += " AND category LIKE ?"
        values.append(f"%/{normalized_source}/%")

    limit = min(30, max(1, int(limit)))
    try:
        with connect_knowledge() as conn:
            rows = conn.execute(
                f"""
                SELECT event_date, title, summary, category, source_url, fetched_at
                FROM knowledge_items
                WHERE {where}
                ORDER BY fetched_at DESC, event_date DESC, id ASC
                LIMIT ?
                """,
                [*values, limit],
            ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def knowledge_stats() -> dict[str, Any]:
    try:
        init_knowledge_db()
        with connect_knowledge() as conn:
            row = conn.execute(
                """
                SELECT
                  COUNT(*) AS item_count,
                  MIN(event_date) AS first_date,
                  MAX(event_date) AS latest_date,
                  MAX(fetched_at) AS latest_fetched_at
                FROM knowledge_items
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


def format_knowledge_for_prompt(query: str, limit: int = 6, items: list[dict[str, Any]] | None = None) -> str:
    items = items if items is not None else search_knowledge(query, limit=limit)
    if not items:
        return ""

    lines = []
    for item in items:
        category = str(item.get("category") or "").strip()
        prefix = f"[{item['event_date']}{' ' + category if category else ''}]"
        title = trim_text(localize_knowledge_text(item.get("title")), 90)
        summary = trim_text(localize_knowledge_text(item.get("summary")), 180)
        lines.append(f"{prefix} {title}：{summary}")
    return (
        "本地知识库检索结果（只在和当前问题相关时使用；如果不相关就忽略；不要把英文地名照抄成拼音，优先用中文表达）：\n"
        + "\n".join(lines)
    )


def format_knowledge_items_for_debug(items: list[dict[str, Any]]) -> str:
    if not items:
        return "未命中知识库"
    lines = []
    for item in items:
        lines.append(
            f"[{item.get('event_date', '-')}] {localize_knowledge_text(item.get('title', '-'))}: {localize_knowledge_text(item.get('summary', ''))}"
        )
    return "\n".join(lines)


def format_knowledge_for_direct_reply(items: list[dict[str, Any]], limit: int = 5) -> str:
    if not items:
        return "这个我现在没查到靠谱信息，先不瞎说。"

    lines = ["知识库查到："]
    for index, item in enumerate(items[:limit], start=1):
        event_date = str(item.get("event_date") or "-").strip()
        category = str(item.get("category") or "").strip()
        category_text = f" {category}" if category else ""
        title = trim_text(localize_knowledge_text(item.get("title")), 90)
        summary = trim_text(localize_knowledge_text(item.get("summary")), 180)
        lines.append(f"{index}. [{event_date}{category_text}] {title}：{summary}")
    lines.append("本地知识库只代表已抓取资料，建议按时间和来源判断可靠性。")
    return "\n".join(lines)


def now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def trim_text(value: Any, limit: int) -> str:
    return normalize_space(value)[:limit]


def tokenize(text: str) -> list[str]:
    content = normalize_space(text).lower()
    words = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9][a-zA-Z0-9_\-]{2,}", content)
    stop_words = {
        "what",
        "when",
        "where",
        "why",
        "how",
        "the",
        "and",
        "are",
        "was",
        "were",
        "这个",
        "那个",
        "什么",
        "怎么",
        "为什么",
        "最近",
        "现在",
        "今天",
        "今年",
        "明年",
        "去年",
        "多少",
        "哪个",
        "哪些",
        "赛事",
        "比赛",
        "赛程",
        "情况",
        "内容",
        "热点",
        "热搜",
        "热榜",
    }
    result: list[str] = []
    seen: set[str] = set()
    for key, aliases in ALIASES.items():
        if key in text:
            normalized_key = key.lower()
            if normalized_key not in seen:
                seen.add(normalized_key)
                result.append(normalized_key)
            for alias in aliases:
                normalized = alias.lower()
                if normalized not in seen:
                    seen.add(normalized)
                    result.append(normalized)

    for word in words:
        if word in stop_words or word in seen:
            continue
        seen.add(word)
        result.append(word)
        if len(result) >= 8:
            break
    return result


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


def extract_query_years(text: str) -> set[int]:
    years = {int(year) for year in re.findall(r"(20\d{2}|19\d{2})", text)}
    for short_year in re.findall(r"(?<!\d)(\d{2})\s*年", text):
        value = int(short_year)
        if 0 <= value <= 79:
            years.add(2000 + value)
        else:
            years.add(1900 + value)
    return years


def knowledge_match_score(item: dict[str, Any], tokens: list[str], years: set[int]) -> int:
    content = normalize_space(
        " ".join(
            [
                item.get("title"),
                item.get("summary"),
                item.get("category"),
                item.get("source_url"),
            ]
        )
    )
    content_lower = content.lower()
    if years and not knowledge_item_matches_year(item, content, years):
        return 0
    if is_lol_query(tokens) and not is_lol_content(content):
        return 0
    if is_event_query(tokens) and has_non_event_content(content):
        return 0

    required_tokens = required_content_tokens(tokens)
    if required_tokens and not any(content_has_token(content, token) for token in required_tokens):
        return 0

    score = 0
    title = str(item.get("title") or "").lower()
    summary = str(item.get("summary") or "").lower()
    for token in tokens:
        token_lower = token.lower()
        if content_has_token(title, token):
            score += 6
        elif content_has_token(summary, token):
            score += 3
        elif content_has_token(content_lower, token):
            score += 1
    if years:
        score += 10
    return score


def knowledge_item_matches_year(item: dict[str, Any], content: str, years: set[int]) -> bool:
    event_year = parse_year(str(item.get("event_date") or ""))
    mentioned_years = {int(year) for year in re.findall(r"(20\d{2}|19\d{2})", content)}
    mentioned_years.update(2010 + int(value) for value in re.findall(r"(?i)\bS(\d{1,2})\b", content))

    if mentioned_years:
        return bool(mentioned_years & years)
    return event_year in years


def parse_year(value: str) -> int | None:
    try:
        return date.fromisoformat(value[:10]).year
    except ValueError:
        return None


def meaningful_tokens(tokens: list[str]) -> list[str]:
    generic = {
        "2024",
        "2025",
        "2026",
        "2027",
        "game",
        "gaming",
        "esports",
        "e-sports",
        "anime",
        "manga",
        "league",
        "legends",
        "worlds",
        "world championship",
        "championship",
    }
    return [
        token
        for token in tokens
        if not token.isdigit() and len(token) >= 2 and token.lower() not in generic
    ]


def searchable_tokens(tokens: list[str]) -> list[str]:
    result = meaningful_tokens(tokens)
    return result or [token for token in tokens if not token.isdigit()] or tokens


def required_content_tokens(tokens: list[str]) -> list[str]:
    return [
        token
        for token in meaningful_tokens(tokens)
        if token.lower() not in BROAD_EVENT_TOKENS
    ]


def is_lol_query(tokens: list[str]) -> bool:
    return any(token.lower() in LOL_QUERY_TOKENS for token in tokens)


def is_lol_content(content: str) -> bool:
    return any(content_has_token(content, token) for token in LOL_CONTENT_TOKENS)


def is_event_query(tokens: list[str]) -> bool:
    event_tokens = {"赛事", "比赛", "赛程", "联赛", "lpl", "worlds", "msi", "s赛"}
    return any(token.lower() in event_tokens for token in tokens)


def has_non_event_content(content: str) -> bool:
    bad_words = ("CG", "cg", "预告", "宣传片", "皮肤", "补丁", "更新", "上线", "发售", "联动")
    bad_urls = ("live.bilibili.com", "/search?", "search?keywords=")
    return any(word in content for word in bad_words) or any(word in content.lower() for word in bad_urls)


def content_has_token(content: str, token: str) -> bool:
    content = str(content or "")
    token = str(token or "").strip()
    if not token:
        return False
    if re.search(r"[\u4e00-\u9fff]", token):
        return token in content
    if " " in token:
        return token.lower() in content.lower()
    return bool(re.search(rf"(?<![a-zA-Z0-9]){re.escape(token)}(?![a-zA-Z0-9])", content, flags=re.IGNORECASE))


LOCALIZE_TERMS = {
    "Chengdu": "成都",
    "China": "中国",
    "South Korean": "韩国",
    "South Korea": "韩国",
    "League of Legends esports": "英雄联盟电竞",
    "League of Legends Champions Korea": "LCK",
    "world titles": "全球总冠军",
    "World Championship": "全球总决赛",
    "Worlds": "全球总决赛",
    "three consecutive": "三连",
    "defeating": "击败",
    "representatives": "代表队",
    "final": "决赛",
    "second split": "第二赛段",
    "Spring Split": "春季赛",
    "Summer Split": "夏季赛",
    "Split": "赛段",
}


def localize_knowledge_text(value: Any) -> str:
    text = normalize_space(value)
    lol_summary = localize_lol_worlds_text(text)
    if lol_summary:
        return lol_summary
    for source, target in LOCALIZE_TERMS.items():
        text = re.sub(re.escape(source), target, text, flags=re.IGNORECASE)
    return text


def localize_lol_worlds_text(text: str) -> str:
    normalized = text.lower()
    if (
        "league of legends" in normalized
        and "t1" in normalized
        and "kt rolster" in normalized
        and "chengdu" in normalized
    ):
        return "T1 在英雄联盟全球总决赛成都决赛中 3-2 击败 KT，成为首支完成 S 赛三连冠的队伍。"
    if (
        "league of legends" in normalized
        and "t1" in normalized
        and "three consecutiv" in normalized
    ):
        return "T1 成为首支完成英雄联盟全球总决赛三连冠的队伍。"
    return ""


def build_keywords(text: str) -> str:
    return " ".join(tokenize(text))


def parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
