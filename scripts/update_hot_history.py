import argparse
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.hot_store import upsert_hot_items  # noqa: E402


JSON_URL = "https://cdn.jsdelivr.net/gh/justjavac/weibo-trending-hot-search@master/raw/{day}.json"
ARCHIVE_URL = "https://cdn.jsdelivr.net/gh/justjavac/weibo-trending-hot-search@master/archives/{day}.md"


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Weibo hot-search history into local hot database.")
    parser.add_argument("--date", default="")
    parser.add_argument("--from", dest="date_from", default="")
    parser.add_argument("--to", dest="date_to", default=date.today().isoformat())
    args = parser.parse_args()

    if args.date:
        start = end = date.fromisoformat(args.date)
    else:
        start = date.fromisoformat(args.date_from or "2025-01-01")
        end = date.fromisoformat(args.date_to or date.today().isoformat())

    today = date.today()
    if end > today:
        end = today
    if end < start:
        raise SystemExit("--to must be after --from")

    total_imported = 0
    for day in iter_days(start, end):
        try:
            rows = fetch_history_rows(day)
            items = make_history_items(day, rows)
            imported = upsert_hot_items(items)
            total_imported += imported
            print(f"hot history {day.isoformat()}: {len(rows)} raw, {imported} imported", flush=True)
        except Exception as exc:
            print(f"hot history {day.isoformat()}: skipped {exc}", flush=True)
        time.sleep(0.2)

    print(f"hot history update done: {total_imported} items", flush=True)


def iter_days(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def fetch_history_rows(day: date) -> list[dict[str, Any]]:
    day_text = day.isoformat()
    try:
        return parse_json_rows(fetch(JSON_URL.format(day=day_text)))
    except Exception:
        return parse_markdown_rows(fetch(ARCHIVE_URL.format(day=day_text)))


def fetch(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 QQbot_v2 hot history updater",
            "Accept": "application/json,text/markdown,text/plain,*/*",
            "Referer": "https://github.com/justjavac/weibo-trending-hot-search",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def parse_json_rows(text: str) -> list[dict[str, Any]]:
    data = json.loads(text)
    if not isinstance(data, list):
        return []
    rows = []
    for index, entry in enumerate(data, start=1):
        if not isinstance(entry, dict):
            continue
        title = normalize_title(entry.get("title"))
        if not title:
            continue
        url = str(entry.get("url") or "")
        rows.append(
            {
                "title": title,
                "rank": parse_rank(url) or index,
                "source_url": build_weibo_search_url(title),
            }
        )
    return dedupe_rows(rows)


def parse_markdown_rows(text: str) -> list[dict[str, Any]]:
    rows = []
    for index, line in enumerate(text.splitlines(), start=1):
        match = re.search(r"\d+\.\s+\[([^\]]+)\]\(([^)]+)\)", line)
        if not match:
            continue
        title = normalize_title(match.group(1))
        if not title:
            continue
        rows.append(
            {
                "title": title,
                "rank": parse_rank(match.group(2)) or index,
                "source_url": build_weibo_search_url(title),
            }
        )
    return dedupe_rows(rows)


def dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["title"].lower()
        current = best.get(key)
        if current is None or int(row["rank"] or 9999) < int(current["rank"] or 9999):
            best[key] = row
    return sorted(best.values(), key=lambda item: int(item["rank"] or 9999))


def make_history_items(day: date, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
    items = []
    for row in rows:
        rank = int(row.get("rank") or 0)
        title = row["title"]
        items.append(
            {
                "event_date": day.isoformat(),
                "title": f"微博历史热榜第{rank}名：{title}"[:120],
                "summary": (
                    f"历史归档日期：{day.isoformat()}；来源：微博历史热搜第三方归档；"
                    f"当日记录排名：{rank}；标题：{title}。热搜只代表当时热度，不等于事实结论。"
                )[:280],
                "source": "微博",
                "rank": rank,
                "category": "热点/微博/历史/中可信",
                "source_url": row.get("source_url") or build_weibo_search_url(title),
                "fetched_at": fetched_at,
                "region": "cn",
            }
        )
    return items


def parse_rank(url: str) -> int:
    match = re.search(r"(?:band_rank|rank)=(\d+)", str(url))
    return int(match.group(1)) if match else 0


def build_weibo_search_url(title: str) -> str:
    return "https://s.weibo.com/weibo?q=" + urllib.parse.quote(title)


def normalize_title(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:90]


if __name__ == "__main__":
    main()
