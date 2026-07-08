import argparse
import calendar
import email.utils
import html as html_lib
import html.parser
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.hot_store import upsert_hot_items  # noqa: E402
from app.knowledge_store import upsert_knowledge_items  # noqa: E402


BASE_URL = "https://en.wikipedia.org/wiki/Portal:Current_events/"
GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
WIKIDATA_SPARQL_API = "https://query.wikidata.org/sparql"
WIKIMEDIA_SUMMARY_API = "https://zh.wikipedia.org/api/rest_v1/page/summary/"
WIKIMEDIA_ONTHISDAY_API = "https://api.wikimedia.org/feed/v1/wikipedia/zh/onthisday/all/"
NAGER_HOLIDAY_API = "https://date.nager.at/api/v3/PublicHolidays/"
SOURCE_RATIO_CN = 0.7
DEFAULT_SOURCES = ("current", "game", "esports", "anime", "wikidata", "daily", "holidays", "onthisday")
OPTIONAL_SOURCES = ("gdelt", "hot")
ALL_SOURCES = DEFAULT_SOURCES + OPTIONAL_SOURCES
SOURCE_LABELS = {
    "current": "综合",
    "game": "游戏",
    "esports": "电竞",
    "anime": "动漫",
    "gdelt": "GDELT",
    "wikidata": "Wikidata",
    "daily": "日常常识",
    "holidays": "节假日",
    "onthisday": "历史上的今天",
    "hot": "热点",
}
GDELT_QUERIES = (
    ("cn", "China OR Chinese OR Beijing OR Shanghai OR Hong Kong OR Taiwan"),
    ("global", "economy OR technology OR sports OR gaming OR anime OR esports OR disaster OR election OR conflict"),
)
HOLIDAY_COUNTRIES = (
    ("CN", "中国", "cn"),
    ("US", "美国", "global"),
    ("JP", "日本", "global"),
    ("KR", "韩国", "global"),
)
COMMON_PAGE_TITLES = (
    "人工智能",
    "大语言模型",
    "机器学习",
    "DeepSeek",
    "OpenAI",
    "ChatGPT",
    "电子游戏",
    "Steam",
    "任天堂",
    "索尼互动娱乐",
    "微软",
    "Xbox",
    "PlayStation",
    "英雄联盟",
    "英雄联盟职业联赛",
    "电子竞技",
    "哔哩哔哩",
    "米哈游",
    "原神",
    "崩坏：星穹铁道",
    "明日方舟",
    "中国",
    "美国",
    "日本",
    "韩国",
    "欧盟",
    "联合国",
    "北京市",
    "上海市",
    "成都市",
    "广州市",
    "深圳市",
    "东京",
    "大阪市",
    "首尔特别市",
    "世界杯足球赛",
    "奥林匹克运动会",
)
ONTHISDAY_DAYS = 7


@dataclass(frozen=True)
class FeedSource:
    name: str
    category: str
    region: str
    url: str


@dataclass(frozen=True)
class PageSource:
    name: str
    category: str
    url: str


class GitHubTrendingParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_article = False
        self.in_title = False
        self.current_title_parts: list[str] = []
        self.current_href = ""
        self.items: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        if tag == "article" and "Box-row" in attrs_dict.get("class", ""):
            self.in_article = True
            self.current_title_parts = []
            self.current_href = ""
        if self.in_article and tag == "h2":
            self.in_title = True
        if self.in_article and self.in_title and tag == "a":
            self.current_href = absolute_url(attrs_dict.get("href", ""), "https://github.com")

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2":
            self.in_title = False
        if tag == "article" and self.in_article:
            title = normalize_space(" ".join(self.current_title_parts)).replace(" / ", "/")
            if title:
                self.items.append((title, self.current_href))
            self.in_article = False

    def handle_data(self, data: str) -> None:
        if self.in_article and self.in_title:
            text = normalize_space(data)
            if text:
                self.current_title_parts.append(text)


FEED_SOURCES = (
    FeedSource("机核", "游戏", "cn", "https://www.gcores.com/rss"),
    FeedSource("游研社", "游戏", "cn", "https://www.yystv.cn/rss/feed"),
    FeedSource("PC Gamer", "游戏", "global", "https://www.pcgamer.com/rss/"),
    FeedSource("Gematsu", "游戏", "global", "https://www.gematsu.com/feed"),
    FeedSource("GameSpot", "游戏", "global", "https://www.gamespot.com/feeds/mashup/"),
    FeedSource("玩加电竞", "电竞", "cn", "https://www.wanplus.cn/rss"),
    FeedSource("ScoreGG", "电竞", "cn", "https://www.scoregg.com/rss"),
    FeedSource("Esports Insider", "电竞", "global", "https://esportsinsider.com/feed"),
    FeedSource("HLTV", "电竞", "global", "https://www.hltv.org/rss/news"),
    FeedSource("动漫之家", "动漫", "cn", "https://news.dmzj.com/rss.xml"),
    FeedSource("Bangumi", "动漫", "cn", "https://bgm.tv/feed/news"),
    FeedSource("Anime News Network", "动漫", "global", "https://www.animenewsnetwork.com/all/rss.xml"),
    FeedSource("Crunchyroll", "动漫", "global", "https://www.crunchyroll.com/newsrss"),
)

PAGE_SOURCES = (
    PageSource("游民星空", "游戏", "https://www.gamersky.com/news/"),
    PageSource("3DM", "游戏", "https://www.3dmgame.com/news/"),
    PageSource("游侠网", "游戏", "https://www.ali213.net/news/"),
    PageSource("新浪游戏", "游戏", "https://games.sina.com.cn/"),
    PageSource("新浪电竞", "电竞", "https://dj.sina.com.cn/"),
    PageSource("游民星空动漫", "动漫", "https://acg.gamersky.com/news/"),
)

HOT_LIMIT_PER_SOURCE = 20
HOT_SEARCH_PAGES = {
    "微博": "https://s.weibo.com/top/summary",
    "百度": "https://top.baidu.com/board?tab=realtime",
    "知乎": "https://www.zhihu.com/billboard",
    "B站": "https://www.bilibili.com/v/popular/all",
    "GitHub": "https://github.com/trending?since=daily",
}
HOT_API_URLS = {
    "微博": "https://weibo.com/ajax/side/hotSearch",
    "微博热榜": "https://weibo.com/ajax/statuses/hot_band",
    "知乎": "https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=20&desktop=true",
    "B站": "https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1",
    "B站热搜": "https://s.search.bilibili.com/main/hotword",
    "B站广场": "https://api.bilibili.com/x/web-interface/search/square?limit=20",
}

WEIBO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://weibo.com/",
}


class CurrentEventsParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_date_header = False
        self.in_event_list = False
        self.in_li = False
        self.current_date = ""
        self.current_li_parts: list[str] = []
        self.current_li_links: list[str] = []
        self.items: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        class_name = attrs_dict.get("class", "")
        if tag in {"div", "h2", "h3"} and "current-events-title" in class_name:
            self.in_date_header = True
        if tag == "ul" and self.current_date:
            self.in_event_list = True
        if tag == "li" and self.in_event_list:
            self.in_li = True
            self.current_li_parts = []
            self.current_li_links = []
        if tag == "a" and self.in_li:
            href = attrs_dict.get("href", "")
            if href.startswith("/wiki/"):
                self.current_li_links.append("https://en.wikipedia.org" + href)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"div", "h2", "h3"}:
            self.in_date_header = False
        if tag == "li" and self.in_li:
            self.add_current_item()
            self.in_li = False
        if tag == "ul" and self.in_event_list:
            self.in_event_list = False

    def handle_data(self, data: str) -> None:
        text = normalize_space(data)
        if not text:
            return
        if self.in_date_header:
            parsed = parse_event_date(text)
            if parsed:
                self.current_date = parsed
        elif self.in_li:
            self.current_li_parts.append(text)

    def add_current_item(self) -> None:
        text = normalize_space(" ".join(self.current_li_parts))
        if not self.current_date or len(text) < 20:
            return
        title = text.split(".")[0][:120].strip(" :-")
        self.items.append(
            {
                "event_date": self.current_date,
                "title": title or text[:80],
                "summary": text[:280],
                "category": "综合/国际/Wikipedia",
                "source_url": self.current_li_links[0] if self.current_li_links else "",
                "region": "global",
            }
        )


class TextExtractor(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = normalize_space(data)
        if text:
            self.parts.append(text)

    def text(self) -> str:
        return normalize_space(" ".join(self.parts))


class LinkExtractor(html.parser.HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.current_href = ""
        self.current_title = ""
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attrs_dict = {key: value or "" for key, value in attrs}
        self.current_href = attrs_dict.get("href", "")
        self.current_title = attrs_dict.get("title", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self.add_link()
            self.current_href = ""
            self.current_title = ""

    def handle_data(self, data: str) -> None:
        if self.current_href and not self.current_title:
            self.current_title += data

    def add_link(self) -> None:
        title = normalize_space(self.current_title)
        href = normalize_space(self.current_href)
        if len(title) < 8 or not href:
            return
        self.links.append((title, absolute_url(href, self.base_url)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Update local knowledge base from public news feeds.")
    parser.add_argument("--from", dest="date_from", default="2025-01-01")
    parser.add_argument("--to", dest="date_to", default=date.today().isoformat())
    parser.add_argument("--sources", default=",".join(DEFAULT_SOURCES))
    args = parser.parse_args()

    start = date.fromisoformat(args.date_from)
    end = date.fromisoformat(args.date_to)
    if end < start:
        raise SystemExit("--to must be after --from")

    sources = parse_sources(args.sources)
    print(
        f"knowledge sources: {','.join(sources)}; target ratio cn/global = 70/30",
        flush=True,
    )

    all_items: list[dict[str, Any]] = []
    if "current" in sources:
        all_items.extend(fetch_current_events(start, end))
    if any(source in sources for source in ("game", "esports", "anime")):
        all_items.extend(fetch_feed_sources(sources, start, end))
        all_items.extend(fetch_page_sources(sources, start, end))
    if "gdelt" in sources:
        all_items.extend(fetch_gdelt_events(start, end))
    if "wikidata" in sources:
        all_items.extend(fetch_wikidata_events(start, end))
    if "daily" in sources:
        all_items.extend(fetch_daily_common_knowledge())
    if "holidays" in sources:
        all_items.extend(fetch_public_holidays(start, end))
    if "onthisday" in sources:
        all_items.extend(fetch_on_this_day_events(end))
    hot_items = fetch_hot_topics() if "hot" in sources else []

    selected_items = apply_region_ratio(all_items)
    count = upsert_knowledge_items(selected_items)
    if hot_items:
        hot_count = upsert_hot_items(hot_items)
        print(f"hot update done: {hot_count} items", flush=True)
    print(f"ratio selected: cn={count_region(selected_items, 'cn')}, global={count_region(selected_items, 'global')}", flush=True)
    print(f"knowledge update done: {count} items", flush=True)


def parse_sources(value: str) -> list[str]:
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
    result: list[str] = []
    for raw in value.split(","):
        source = aliases.get(raw.strip(), raw.strip().lower())
        if source in ALL_SOURCES and source not in result:
            result.append(source)
    return result or list(DEFAULT_SOURCES)


def fetch_gdelt_events(start: date, end: date) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for region, query in GDELT_QUERIES:
        try:
            time.sleep(6)
            source_items = fetch_gdelt_window(query, region, start, end)
            items.extend(source_items)
            print(f"gdelt {region}: {len(source_items)} items", flush=True)
        except Exception as exc:
            print(f"gdelt {region}: skipped {exc}", flush=True)
    return dedupe_items(items)


def fetch_gdelt_window(query: str, region: str, start: date, end: date) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "sort": "datedesc",
            "maxrecords": "25",
            "startdatetime": f"{start:%Y%m%d}000000",
            "enddatetime": f"{end:%Y%m%d}235959",
        }
    )
    response_text = fetch(f"{GDELT_DOC_API}?{params}")
    if not response_text.lstrip().startswith("{"):
        raise ValueError(trim_text(response_text, 160))
    data = json.loads(response_text)
    articles = data.get("articles") or []
    items: list[dict[str, Any]] = []
    for article in articles:
        title = normalize_space(article.get("title"))
        if not is_useful_gdelt_title(title):
            continue
        event_date = parse_gdelt_date(str(article.get("seendate") or ""))
        if not event_date or not (start.isoformat() <= event_date <= end.isoformat()):
            continue
        domain = normalize_space(article.get("domain"))
        url = normalize_space(article.get("url"))
        items.append(
            {
                "event_date": event_date,
                "title": title[:120],
                "summary": (f"{title}（来源：{domain}）" if domain else title)[:280],
                "category": f"综合/{region_label(region)}/GDELT/中可信",
                "source_url": url,
                "region": region,
            }
        )
    return dedupe_items(items[:30])


def fetch_wikidata_events(start: date, end: date) -> list[dict[str, Any]]:
    query = f"""
SELECT ?event ?eventLabel ?eventDescription ?date ?article WHERE {{
  ?event wdt:P585 ?date.
  FILTER(?date >= "{start.isoformat()}T00:00:00Z"^^xsd:dateTime && ?date <= "{end.isoformat()}T23:59:59Z"^^xsd:dateTime)
  FILTER EXISTS {{ ?event wdt:P31 ?type. }}
  OPTIONAL {{ ?article schema:about ?event; schema:isPartOf <https://en.wikipedia.org/>. }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "zh,en". }}
}}
ORDER BY DESC(?date)
LIMIT 200
"""
    params = urllib.parse.urlencode({"format": "json", "query": query})
    try:
        data = json.loads(fetch(f"{WIKIDATA_SPARQL_API}?{params}"))
    except Exception as exc:
        print(f"wikidata: skipped {exc}", flush=True)
        return []

    items: list[dict[str, Any]] = []
    for binding in data.get("results", {}).get("bindings", []):
        title = normalize_space(binding_text(binding, "eventLabel"))
        if not title or re.fullmatch(r"Q\d+", title):
            continue
        event_date = parse_iso_datetime_date(binding_text(binding, "date"))
        if not event_date or not (start.isoformat() <= event_date <= end.isoformat()):
            continue
        summary = normalize_space(binding_text(binding, "eventDescription")) or title
        if not is_useful_wikidata_item(title, summary):
            continue
        source_url = normalize_space(binding_text(binding, "article")) or normalize_space(binding_text(binding, "event"))
        items.append(
            {
                "event_date": event_date,
                "title": title[:120],
                "summary": summary[:280],
                "category": "综合/国际/Wikidata/高可信",
                "source_url": source_url,
                "region": "global",
            }
        )
    result = dedupe_items(items)
    print(f"wikidata: {len(result)} items", flush=True)
    return result


def fetch_daily_common_knowledge() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    today = date.today().isoformat()
    for title in COMMON_PAGE_TITLES:
        try:
            time.sleep(1)
            encoded = urllib.parse.quote(title.replace(" ", "_"))
            data = json.loads(fetch(WIKIMEDIA_SUMMARY_API + encoded))
            page_title = normalize_space(data.get("title")) or title
            summary = normalize_space(data.get("extract"))
            if not summary:
                print(f"daily {title}: skipped empty summary", flush=True)
                continue
            page_url = data.get("content_urls", {}).get("desktop", {}).get("page") or data.get("content_urls", {}).get("mobile", {}).get("page") or ""
            items.append(
                {
                    "event_date": today,
                    "title": page_title[:120],
                    "summary": summary[:280],
                    "category": "日常常识/Wikimedia/高可信",
                    "source_url": normalize_space(page_url),
                    "region": "cn" if has_chinese_text(page_title + summary) else "global",
                }
            )
            print(f"daily {title}: 1 item", flush=True)
        except Exception as exc:
            print(f"daily {title}: skipped {exc}", flush=True)
    return dedupe_items(items)


def fetch_public_holidays(start: date, end: date) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for year in range(start.year, end.year + 1):
        for code, country_name, region in HOLIDAY_COUNTRIES:
            try:
                data = json.loads(fetch(NAGER_HOLIDAY_API + f"{year}/{code}"))
                count = 0
                for entry in data:
                    event_date = normalize_space(entry.get("date"))
                    if not event_date or not (start.isoformat() <= event_date <= end.isoformat()):
                        continue
                    local_name = normalize_space(entry.get("localName"))
                    name = normalize_space(entry.get("name"))
                    title = f"{country_name}节假日：{local_name or name}"
                    summary = f"{event_date} 是{country_name}公共节假日：{local_name or name}。"
                    if name and local_name and name != local_name:
                        summary += f"英文名：{name}。"
                    items.append(
                        {
                            "event_date": event_date,
                            "title": title[:120],
                            "summary": summary[:280],
                            "category": f"节假日/{country_name}/Nager.Date/高可信",
                            "source_url": f"https://date.nager.at/PublicHoliday/{code}/{year}",
                            "region": region,
                        }
                    )
                    count += 1
                print(f"holidays {code} {year}: {count} items", flush=True)
            except Exception as exc:
                print(f"holidays {code} {year}: skipped {exc}", flush=True)
    return dedupe_items(items)


def fetch_on_this_day_events(end: date) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    start_day = end - timedelta(days=ONTHISDAY_DAYS - 1)
    for day in (start_day + timedelta(days=offset) for offset in range(ONTHISDAY_DAYS)):
        url = WIKIMEDIA_ONTHISDAY_API + f"{day.month:02d}/{day.day:02d}"
        try:
            time.sleep(1)
            data = json.loads(fetch(url))
            count = 0
            for section in ("events", "births", "deaths", "holidays"):
                for entry in data.get(section, [])[:8]:
                    year = entry.get("year")
                    text = normalize_space(entry.get("text"))
                    if not text:
                        continue
                    title = f"历史上的今天：{day.month}月{day.day}日"
                    if year:
                        title += f" {year}年"
                    items.append(
                        {
                            "event_date": day.isoformat(),
                            "title": title[:120],
                            "summary": text[:280],
                            "category": f"历史上的今天/Wikimedia/{onthisday_section_label(section)}/高可信",
                            "source_url": "https://zh.wikipedia.org/wiki/" + urllib.parse.quote(f"{day.month}月{day.day}日"),
                            "region": "cn",
                        }
                    )
                    count += 1
            print(f"onthisday {day:%m-%d}: {count} items", flush=True)
        except Exception as exc:
            print(f"onthisday {day:%m-%d}: skipped {exc}", flush=True)
    return dedupe_items(items)


def fetch_hot_topics() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    fetch_time = datetime.now().astimezone()
    source_fetchers = (
        ("weibo", "微博", fetch_weibo_hot_topics),
        ("baidu", "百度", fetch_baidu_hot_topics),
        ("zhihu", "知乎", fetch_zhihu_hot_topics),
        ("bilibili", "B站", fetch_bilibili_hot_topics),
        ("github", "GitHub", fetch_github_trending),
    )
    for code, source_name, fetcher in source_fetchers:
        try:
            source_items = fetcher(fetch_time)
            items.extend(source_items)
            print(f"hot {code}: {len(source_items)} items", flush=True)
        except Exception as exc:
            print(f"hot {code}: skipped {exc}", flush=True)
    return dedupe_items(items)


def fetch_weibo_hot_topics(fetch_time: datetime) -> list[dict[str, Any]]:
    for api_name, item_keys in (
        ("微博热榜", ("band_list", "realtime")),
        ("微博", ("realtime", "band_list")),
    ):
        try:
            data = json.loads(fetch(HOT_API_URLS[api_name], headers=WEIBO_HEADERS))
            rows = []
            payload = data.get("data", {})
            for item_key in item_keys:
                for entry in payload.get(item_key, []):
                    title = clean_hot_title(entry.get("word") or entry.get("note") or entry.get("word_scheme") or "")
                    if title and not is_bad_hot_title(title):
                        rows.append((title, "https://s.weibo.com/weibo?q=" + urllib.parse.quote(title)))
                if rows:
                    return make_hot_items("微博", rows, fetch_time)
        except Exception:
            pass

    html_text = fetch(
        HOT_SEARCH_PAGES["微博"],
        headers={
            **WEIBO_HEADERS,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://s.weibo.com/",
        },
    )
    matches = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html_text, flags=re.IGNORECASE | re.DOTALL)
    rows = []
    for href, title_html in matches:
        title = clean_hot_title(title_html)
        if not title or is_bad_hot_title(title):
            continue
        if "Refer=index" not in href and "q=" not in href:
            continue
        rows.append((title, absolute_url(html_lib.unescape(href), "https://s.weibo.com")))
    return make_hot_items("微博", rows, fetch_time)


def fetch_baidu_hot_topics(fetch_time: datetime) -> list[dict[str, Any]]:
    html_text = fetch(HOT_SEARCH_PAGES["百度"])
    rows = []
    seen: set[str] = set()
    for match in re.finditer(r'"word"\s*:\s*"([^"]+)"', html_text):
        title = clean_hot_title(match.group(1))
        if not title or title in seen or is_bad_hot_title(title):
            continue
        seen.add(title)
        rows.append((title, "https://www.baidu.com/s?wd=" + urllib.parse.quote(title)))
    if rows:
        return make_hot_items("百度", rows, fetch_time)

    for href, title in extract_common_links(html_text, HOT_SEARCH_PAGES["百度"]):
        if title not in seen and not is_bad_hot_title(title):
            seen.add(title)
            rows.append((title, href))
    return make_hot_items("百度", rows, fetch_time)


def fetch_zhihu_hot_topics(fetch_time: datetime) -> list[dict[str, Any]]:
    try:
        data = json.loads(fetch(HOT_API_URLS["知乎"]))
        rows = []
        for entry in data.get("data", []):
            target = entry.get("target") or {}
            title_area = target.get("title_area") or {}
            title = clean_hot_title(title_area.get("text") or target.get("title") or "")
            link = target.get("link", {}).get("url") or target.get("url") or ""
            if title and not is_bad_hot_title(title):
                rows.append((title, link or "https://www.zhihu.com/search?q=" + urllib.parse.quote(title)))
        if rows:
            return make_hot_items("知乎", rows, fetch_time)
    except Exception:
        pass

    html_text = fetch(HOT_SEARCH_PAGES["知乎"])
    rows = []
    seen: set[str] = set()
    for key in ("target_title", "title"):
        pattern = rf'"{key}"\s*:\s*"([^"]+)"'
        for match in re.finditer(pattern, html_text):
            title = clean_hot_title(match.group(1))
            if not title or title in seen or is_bad_hot_title(title):
                continue
            seen.add(title)
            rows.append((title, "https://www.zhihu.com/search?q=" + urllib.parse.quote(title)))
    if rows:
        return make_hot_items("知乎", rows, fetch_time)

    for href, title in extract_common_links(html_text, HOT_SEARCH_PAGES["知乎"]):
        if title not in seen and not is_bad_hot_title(title):
            seen.add(title)
            rows.append((title, href))
    return make_hot_items("知乎", rows, fetch_time)


def fetch_bilibili_hot_topics(fetch_time: datetime) -> list[dict[str, Any]]:
    try:
        data = json.loads(fetch(HOT_API_URLS["B站热搜"]))
        rows = []
        for entry in data.get("list", []):
            title = clean_hot_title(entry.get("show_name") or entry.get("keyword") or "")
            if title and not is_bad_hot_title(title):
                rows.append((title, "https://search.bilibili.com/all?keyword=" + urllib.parse.quote(title)))
        if rows:
            return make_hot_items("B站", rows, fetch_time)
    except Exception:
        pass

    try:
        data = json.loads(fetch(HOT_API_URLS["B站广场"]))
        rows = []
        for entry in data.get("data", {}).get("trending", {}).get("list", []):
            title = clean_hot_title(entry.get("show_name") or entry.get("keyword") or "")
            link = normalize_space(entry.get("uri")) or "https://search.bilibili.com/all?keyword=" + urllib.parse.quote(title)
            if title and not is_bad_hot_title(title):
                rows.append((title, link))
        if rows:
            return make_hot_items("B站", rows, fetch_time)
    except Exception:
        pass

    try:
        data = json.loads(fetch(HOT_API_URLS["B站"]))
        rows = []
        for entry in data.get("data", {}).get("list", []):
            title = clean_hot_title(entry.get("title") or "")
            link = normalize_space(entry.get("short_link_v2") or entry.get("short_link") or entry.get("uri"))
            if title and not is_bad_hot_title(title):
                rows.append((title, link))
        if rows:
            return make_hot_items("B站", rows, fetch_time)
    except Exception:
        pass

    html_text = fetch(HOT_SEARCH_PAGES["B站"])
    rows = []
    seen: set[str] = set()
    for match in re.finditer(r'"title"\s*:\s*"([^"]+)"', html_text):
        title = clean_hot_title(match.group(1))
        if not title or title in seen or is_bad_hot_title(title):
            continue
        seen.add(title)
        rows.append((title, "https://search.bilibili.com/all?keyword=" + urllib.parse.quote(title)))
    if rows:
        return make_hot_items("B站", rows, fetch_time)

    for href, title in extract_common_links(html_text, HOT_SEARCH_PAGES["B站"]):
        if title not in seen and not is_bad_hot_title(title):
            seen.add(title)
            rows.append((title, href))
    return make_hot_items("B站", rows, fetch_time)


def fetch_github_trending(fetch_time: datetime) -> list[dict[str, Any]]:
    html_text = fetch(HOT_SEARCH_PAGES["GitHub"])
    parser = GitHubTrendingParser()
    parser.feed(html_text)
    rows = [(title, url) for title, url in parser.items if title and url]
    return make_hot_items("GitHub", rows, fetch_time, category="科技")


def extract_common_links(html_text: str, base_url: str) -> list[tuple[str, str]]:
    extractor = LinkExtractor(base_url)
    extractor.feed(html_text)
    rows = []
    for title, href in extractor.links:
        title = clean_hot_title(title)
        if title and not is_bad_hot_title(title):
            rows.append((title, href))
    return rows


def make_hot_items(
    source_name: str,
    rows: list[tuple[str, str]],
    fetch_time: datetime,
    category: str = "综合",
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    fetched_text = fetch_time.isoformat(timespec="seconds")
    for rank, (title, url) in enumerate(dedupe_hot_rows(rows), start=1):
        if rank > HOT_LIMIT_PER_SOURCE:
            break
        items.append(
            {
                "event_date": fetch_time.date().isoformat(),
                "title": f"{source_name}热榜第{rank}名：{title}"[:120],
                "summary": f"抓取时间：{fetched_text}；来源：{source_name}；排名：{rank}；标题：{title}。热榜只代表当时热度，不等于事实结论。"[:280],
                "source": source_name,
                "rank": rank,
                "category": f"热点/{source_name}/{category}/中可信",
                "source_url": normalize_space(url),
                "fetched_at": fetched_text,
                "region": "cn" if source_name != "GitHub" else "global",
            }
        )
    return items


def dedupe_hot_rows(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for title, url in rows:
        normalized = normalize_space(title)
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append((normalized, normalize_space(url)))
    return result


def clean_hot_title(value: str) -> str:
    text = html_lib.unescape(value)
    text = strip_html(text)
    text = text.encode("utf-8", errors="ignore").decode("unicode_escape", errors="ignore") if "\\u" in text else text
    text = normalize_space(text)
    text = re.sub(r"^(热|新|爆|荐)\s*", "", text)
    return text.strip(" -_|\u3000")


def is_bad_hot_title(title: str) -> bool:
    title = normalize_space(title)
    if len(title) < 2 or len(title) > 90:
        return True
    bad_words = (
        "登录", "注册", "广告", "更多", "首页", "下载", "客户端", "版权", "隐私",
        "微博热搜", "百度热搜", "知乎热榜", "排行榜", "javascript",
    )
    return any(word.lower() in title.lower() for word in bad_words)


def fetch_current_events(start: date, end: date) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for year, month in iter_months(start, end):
        page_url = BASE_URL + f"{calendar.month_name[month]}_{year}"
        try:
            html = fetch(page_url)
        except urllib.error.URLError as exc:
            print(f"current {year}-{month:02d}: skipped {exc}", flush=True)
            continue

        parser = CurrentEventsParser()
        parser.feed(html)
        month_items = [
            item
            for item in parser.items
            if start.isoformat() <= item["event_date"] <= end.isoformat()
        ]
        items.extend(month_items)
        print(f"current {year}-{month:02d}: {len(month_items)} items", flush=True)
    return items


def fetch_feed_sources(sources: list[str], start: date, end: date) -> list[dict[str, Any]]:
    enabled_categories = {SOURCE_LABELS[source] for source in sources if source in SOURCE_LABELS}
    items: list[dict[str, Any]] = []
    for source in FEED_SOURCES:
        if source.category not in enabled_categories:
            continue
        try:
            xml_text = fetch(source.url)
            source_items = parse_feed(xml_text, source, start, end)
            items.extend(source_items)
            print(f"{source.category}/{source.region}/{source.name}: {len(source_items)} items", flush=True)
        except Exception as exc:
            print(f"{source.category}/{source.region}/{source.name}: skipped {exc}", flush=True)
    return items


def fetch_page_sources(sources: list[str], start: date, end: date) -> list[dict[str, Any]]:
    enabled_categories = {SOURCE_LABELS[source] for source in sources if source in SOURCE_LABELS}
    items: list[dict[str, Any]] = []
    for source in PAGE_SOURCES:
        if source.category not in enabled_categories:
            continue
        try:
            html_text = fetch(source.url)
            source_items = parse_page_links(html_text, source, start, end)
            items.extend(source_items)
            print(f"{source.category}/cn/{source.name}: {len(source_items)} items", flush=True)
        except Exception as exc:
            print(f"{source.category}/cn/{source.name}: skipped {exc}", flush=True)
    return items


def parse_page_links(html_text: str, source: PageSource, start: date, end: date) -> list[dict[str, Any]]:
    extractor = LinkExtractor(source.url)
    extractor.feed(html_text)
    items: list[dict[str, Any]] = []
    for title, link in extractor.links:
        if is_bad_page_link(link):
            continue
        if not is_useful_page_title(title, source.category):
            continue
        event_date = infer_date_from_url(link)
        if not event_date or not (start.isoformat() <= event_date <= end.isoformat()):
            continue
        items.append(
            {
                "event_date": event_date,
                "title": title[:120],
                "summary": title[:280],
                "category": f"{source.category}/中国/{source.name}",
                "source_url": link,
                "region": "cn",
            }
        )
        if len(items) >= 40:
            break
    return dedupe_items(items)


def parse_feed(xml_text: str, source: FeedSource, start: date, end: date) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    entries = root.findall(".//item")
    if not entries:
        entries = root.findall("{http://www.w3.org/2005/Atom}entry")

    items: list[dict[str, Any]] = []
    for entry in entries:
        title = normalize_space(find_entry_text(entry, "title"))
        if not title:
            continue

        summary = normalize_space(
            find_entry_text(entry, "description")
            or find_entry_text(entry, "summary")
            or find_entry_text(entry, "content")
            or title
        )
        event_date = parse_feed_date(
            find_entry_text(entry, "pubDate")
            or find_entry_text(entry, "published")
            or find_entry_text(entry, "updated")
        )
        if not event_date:
            event_date = date.today().isoformat()
        if not (start.isoformat() <= event_date <= end.isoformat()):
            continue

        link = find_entry_link(entry)
        items.append(
            {
                "event_date": event_date,
                "title": title[:120],
                "summary": strip_html(summary)[:280],
                "category": f"{source.category}/{region_label(source.region)}/{source.name}",
                "source_url": link,
                "region": source.region,
            }
        )
    return dedupe_items(items)


def find_entry_text(entry: ET.Element, name: str) -> str:
    for child in list(entry):
        if local_name(child.tag) == name:
            return "".join(child.itertext()).strip()
    return ""


def find_entry_link(entry: ET.Element) -> str:
    for child in list(entry):
        if local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        if href:
            return href.strip()
        text = "".join(child.itertext()).strip()
        if text:
            return text
    return ""


def parse_feed_date(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass

    match = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", value)
    if not match:
        return ""
    return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()


def parse_gdelt_date(value: str) -> str:
    match = re.search(r"(20\d{2})(\d{2})(\d{2})", value)
    if not match:
        return ""
    return safe_date(match.group(1), match.group(2), match.group(3))


def parse_iso_datetime_date(value: str) -> str:
    match = re.search(r"(20\d{2}-\d{2}-\d{2})", value)
    return match.group(1) if match else ""


def binding_text(binding: dict[str, Any], name: str) -> str:
    value = binding.get(name) or {}
    return str(value.get("value") or "")


def has_chinese_text(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", value))


def onthisday_section_label(section: str) -> str:
    labels = {
        "events": "事件",
        "births": "出生",
        "deaths": "逝世",
        "holidays": "节日",
    }
    return labels.get(section, section)


def infer_date_from_url(url: str) -> str:
    match = re.search(r"(20\d{2})[-_/](\d{1,2})[-_/](\d{1,2})", url)
    if match:
        return safe_date(match.group(1), match.group(2), match.group(3))

    match = re.search(r"/(20\d{2})(\d{2})/", url)
    if match:
        return safe_date(match.group(1), match.group(2), "01")

    return ""


def safe_date(year: str, month: str, day: str) -> str:
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return ""


def apply_region_ratio(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not items:
        return []
    deduped = dedupe_items(items)
    cn_items = [item for item in deduped if item.get("region") == "cn"]
    global_items = [item for item in deduped if item.get("region") != "cn"]
    if not cn_items or not global_items:
        selected = cn_items or global_items
        return finalize_items(selected)

    total_by_cn = int(len(cn_items) / SOURCE_RATIO_CN)
    total_by_global = int(len(global_items) / (1 - SOURCE_RATIO_CN))
    total = max(1, min(len(deduped), total_by_cn, total_by_global))
    cn_target = min(len(cn_items), round(total * SOURCE_RATIO_CN))
    global_target = min(len(global_items), total - cn_target)
    selected_cn = cn_items[:cn_target]
    selected_global = global_items[:global_target]
    selected = selected_cn + selected_global
    return finalize_items(selected)


def finalize_items(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected.sort(key=lambda item: (item.get("event_date") or "", item.get("title") or ""), reverse=True)
    return selected


def dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        key = item_key(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def item_key(item: dict[str, Any]) -> tuple[str, str, str]:
    source_url = str(item.get("source_url") or "")
    identity = source_url or normalize_space(item.get("title")).lower()
    return (
        str(item.get("event_date") or ""),
        identity.lower(),
        "",
    )


def count_region(items: list[dict[str, Any]], region: str) -> int:
    if region == "cn":
        return sum(1 for item in items if item.get("region") == "cn" or "/中国/" in str(item.get("category") or ""))
    return sum(1 for item in items if item.get("region") == "global" or "/国际/" in str(item.get("category") or ""))


def iter_months(start: date, end: date):
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        month += 1
        if month > 12:
            year += 1
            month = 1


def iter_month_ranges(start: date, end: date):
    for year, month in iter_months(start, end):
        month_start = date(year, month, 1)
        month_end = date(year, month, calendar.monthrange(year, month)[1])
        yield max(start, month_start), min(end, month_end)


def fetch(url: str, headers: dict[str, str] | None = None) -> str:
    for attempt in range(3):
        request_headers = {
            "User-Agent": "Mozilla/5.0 QQbot_v2 local knowledge updater",
            "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
            "Referer": "https://www.baidu.com/",
        }
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(
            url,
            headers=request_headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read()
                charset = response.headers.get_content_charset()
                return decode_response(data, charset)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 2:
                time.sleep(8 * (attempt + 1))
                continue
            raise
    return ""


def decode_response(data: bytes, charset: str | None = None) -> str:
    encodings = [charset, detect_html_charset(data), "utf-8", "gb18030", "big5"]
    tried: set[str] = set()
    for encoding in encodings:
        if not encoding:
            continue
        normalized = encoding.lower()
        if normalized in tried:
            continue
        tried.add(normalized)
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def detect_html_charset(data: bytes) -> str:
    head = data[:2048].decode("ascii", errors="ignore")
    match = re.search(r"charset=[\"']?([a-zA-Z0-9_\-]+)", head, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def absolute_url(href: str, base_url: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        match = re.match(r"^(https?://[^/]+)", base_url)
        return (match.group(1) if match else base_url.rstrip("/")) + href
    return base_url.rstrip("/") + "/" + href.lstrip("/")


def is_bad_page_link(url: str) -> bool:
    value = normalize_space(url).lower()
    if not value or value.startswith(("javascript:", "#")):
        return True
    bad_parts = (
        "/search",
        "search?",
        "live.bilibili.com",
        "/tag/",
        "/topic/",
        "/zt/",
        "/down/",
    )
    return any(part in value for part in bad_parts)


def is_useful_page_title(title: str, category: str = "") -> bool:
    title = normalize_space(title)
    if len(title) < 8 or len(title) > 90:
        return False
    bad_words = (
        "首页", "登录", "注册", "广告", "专题", "更多", "下载", "客服", "论坛", "投稿",
        "美女", "写真", "白丝", "黑丝", "长腿", "性感", "辣妹", "网红", "穿搭", "身材",
    )
    if any(word in title for word in bad_words):
        return False
    if category == "电竞" and has_non_event_signal(title):
        return False
    if category in {"游戏", "电竞", "动漫"} and not has_domain_signal(title, category):
        return False
    return bool(re.search(r"[\u4e00-\u9fff]{4,}", title))


def is_useful_gdelt_title(title: str) -> bool:
    title = normalize_space(title)
    if len(title) < 12 or len(title) > 140:
        return False
    bad_words = (
        "stock price", "coupon", "download", "sale", "deal", "lottery",
        "广告", "优惠", "折扣", "下载", "开奖直播", "福利视频",
    )
    return not any(word.lower() in title.lower() for word in bad_words)


def is_useful_wikidata_item(title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()
    bad_parts = (
        "date in gregorian calendar",
        "wikimedia disambiguation",
        "calendar date",
        "日历",
    )
    if any(part in text for part in bad_parts):
        return False
    return len(title) >= 3 and len(summary) >= 4


def has_non_event_signal(title: str) -> bool:
    bad_words = ("CG", "cg", "预告", "宣传片", "皮肤", "补丁", "更新", "上线", "发售", "联动")
    return any(word in title for word in bad_words)


def has_domain_signal(title: str, category: str) -> bool:
    common = ("游戏", "手游", "主机", "单机", "玩家", "上线", "发售", "预告", "更新", "开测")
    domains = {
        "游戏": common + ("Steam", "PS5", "Xbox", "Switch", "任天堂", "索尼", "微软"),
        "电竞": common + ("电竞", "赛事", "战队", "选手", "LPL", "KPL", "CS2", "DOTA", "LOL", "冠军", "联赛"),
        "动漫": ("动画", "动漫", "漫画", "新番", "番剧", "声优", "剧场版", "手办", "轻小说", "二次元"),
    }
    return any(word in title for word in domains.get(category, common))


def parse_event_date(text: str) -> str:
    match = re.search(r"([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})", text)
    if not match:
        return ""
    month = list(calendar.month_name).index(match.group(1))
    return date(int(match.group(3)), month, int(match.group(2))).isoformat()


def strip_html(value: str) -> str:
    extractor = TextExtractor()
    extractor.feed(value)
    return extractor.text() or normalize_space(value)


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def region_label(region: str) -> str:
    return "中国" if region == "cn" else "国际"


if __name__ == "__main__":
    main()
