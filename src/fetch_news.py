"""ニュースの取得・フィルタリング・銘柄への振り分け。"""
from __future__ import annotations

import html
import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import feedparser
import requests

from . import classify, db
from .utils import contains_term, is_ascii, is_same_story, normalize_title

logger = logging.getLogger("crypto_news_bot.fetch_news")

REQUEST_TIMEOUT = 15
USER_AGENT = "crypto-news-bot/1.0 (personal Discord notifier)"
PAGE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_TAG_PATTERN = re.compile(r"<[^>]+>")
_WS_PATTERN = re.compile(r"\s+")
# RSSの抜粋末尾に付く定型文（「The post ... first appeared on ...」等）を落とす
_BOILERPLATE_PATTERNS = [
    re.compile(r"The post .* (?:first )?appeared on .*$", re.IGNORECASE | re.DOTALL),
    re.compile(r"Continue reading.*$", re.IGNORECASE | re.DOTALL),
    re.compile(r"この記事は.*の掲載記事です。?$", re.DOTALL),
    re.compile(r"\[&#8230;\]\s*$"),
    re.compile(r"\[…\]\s*$"),
]
# 日本語は分かち書きしないため、タグ除去で入った不要な空白を後から詰める
_CJK_CLASS = r"[぀-ヿ㐀-䶿一-鿿＀-￯]"
_CJK_SPACE_PATTERN = re.compile(f"(?<={_CJK_CLASS}) (?={_CJK_CLASS})")
_OG_DESCRIPTION_PATTERN = re.compile(
    r"<meta[^>]+(?:property|name)=[\"']og:description[\"'][^>]+content=[\"']([^\"']+)",
    re.IGNORECASE,
)
_META_DESCRIPTION_PATTERN = re.compile(
    r"<meta[^>]+name=[\"']description[\"'][^>]+content=[\"']([^\"']+)",
    re.IGNORECASE,
)


def clean_excerpt(raw: str | None, max_length: int = 300) -> str:
    """RSSのdescription等から、そのまま読める本文抜粋を作る。"""
    if not raw:
        return ""
    text = html.unescape(raw)
    text = _TAG_PATTERN.sub(" ", text)
    text = html.unescape(text)
    for pattern in _BOILERPLATE_PATTERNS:
        text = pattern.sub("", text)
    text = _WS_PATTERN.sub(" ", text).strip()
    text = _CJK_SPACE_PATTERN.sub("", text)
    if len(text) > max_length:
        text = text[:max_length].rstrip() + "…"
    return text


def fetch_page_description(url: str, timeout: int = 8) -> str:
    """記事ページから og:description（無ければ meta description）を取得する。

    Google Newsのリンクは記事本文に到達できないため、直接リンクの媒体にのみ使う。
    """
    if not url or "news.google.com" in url:
        return ""
    try:
        resp = requests.get(url, headers={"User-Agent": PAGE_USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.info("記事ページの取得に失敗しました url=%s error=%s", url[:80], e)
        return ""
    head = resp.text[:200_000]
    for pattern in (_OG_DESCRIPTION_PATTERN, _META_DESCRIPTION_PATTERN):
        m = pattern.search(head)
        if m:
            return m.group(1)
    return ""


@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    published_at: datetime
    coins: list[str] = field(default_factory=list)
    forced_sentiment: str | None = None  # "good" 等。DAO提案などで使用
    excerpt: str = ""  # 本文抜粋（取れた場合のみ）


def _fetch_rss(url: str) -> feedparser.FeedParserDict | None:
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("RSS取得に失敗しました url=%s error=%s", url, e)
        return None
    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        logger.warning("RSSの解析に失敗しました url=%s error=%s", url, parsed.get("bozo_exception"))
        return None
    return parsed


def _entry_published_at(entry) -> datetime:
    for key in ("published", "updated", "pubDate"):
        value = entry.get(key)
        if value:
            try:
                dt = parsedate_to_datetime(value)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except (TypeError, ValueError):
                pass
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime(*value[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _entry_source_name(entry, fallback_title: str, feed_title: str | None) -> tuple[str, str]:
    """(媒体名, 媒体名を除いたタイトル) を返す。"""
    source_name = None
    if entry.get("source") and entry["source"].get("title"):
        source_name = entry["source"]["title"]
    if not source_name and " - " in fallback_title:
        source_name = fallback_title.rsplit(" - ", 1)[-1].strip()
    if not source_name:
        source_name = feed_title or "unknown"
    title = fallback_title
    if title.endswith(f" - {source_name}"):
        title = title[: -(len(source_name) + 3)].strip()
    return source_name, title


def _google_news_url(query_terms: list[str], lang: str, quote: bool = True) -> str:
    if quote:
        # 完全一致フレーズ検索（銘柄名のように語順が決まっている語向け）
        query = " OR ".join(f'"{t}"' for t in query_terms)
    else:
        # 複合語はAND検索にする（例: "Jupiter Exchange crypto" のような
        # 不自然な完全一致フレーズを避け、紛らわしい語を文脈語と組み合わせて絞り込む）
        query = " OR ".join(f"({t})" for t in query_terms)
    encoded = urllib.parse.quote(query)
    if lang == "ja":
        return f"https://news.google.com/rss/search?q={encoded}&hl=ja&gl=JP&ceid=JP:ja"
    return f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"


def _fetch_google_news(query_terms: list[str], lang: str, quote: bool = True) -> list[NewsItem]:
    if not query_terms:
        return []
    url = _google_news_url(query_terms, lang, quote=quote)
    parsed = _fetch_rss(url)
    if not parsed:
        return []
    items = []
    for entry in parsed.entries:
        raw_title = entry.get("title", "").strip()
        if not raw_title:
            continue
        source_name, title = _entry_source_name(entry, raw_title, parsed.feed.get("title"))
        items.append(
            NewsItem(
                title=title,
                url=entry.get("link", ""),
                source=source_name,
                published_at=_entry_published_at(entry),
            )
        )
    return items


def _fetch_generic_feed(url: str, feed_label: str, excerpt_max_length: int = 300) -> list[NewsItem]:
    parsed = _fetch_rss(url)
    if not parsed:
        return []
    feed_title = parsed.feed.get("title", feed_label)
    items = []
    for entry in parsed.entries:
        raw_title = entry.get("title", "").strip()
        if not raw_title:
            continue
        source_name, title = _entry_source_name(entry, raw_title, feed_title)
        raw_excerpt = entry.get("description") or entry.get("summary") or ""
        items.append(
            NewsItem(
                title=title,
                url=entry.get("link", ""),
                source=source_name or feed_label,
                published_at=_entry_published_at(entry),
                excerpt=clean_excerpt(raw_excerpt, excerpt_max_length),
            )
        )
    return items


def match_coins(title: str, coins_cfg: dict) -> list[str]:
    matched = []
    for symbol, coin_cfg in coins_cfg.items():
        for name in coin_cfg.get("names", []):
            if contains_term(title, name):
                matched.append(symbol)
                break
    return matched


def fetch_dao_forum_items(cfg: dict) -> list[NewsItem]:
    url = cfg["sources"].get("arbitrum_dao_forum_rss")
    if not url:
        return []
    parsed = _fetch_rss(url)
    if not parsed:
        return []
    keywords = [k.lower() for k in cfg["arb"]["dao_forum_keywords"]]
    items = []
    for entry in parsed.entries:
        raw_title = entry.get("title", "").strip()
        if not raw_title:
            continue
        lower = raw_title.lower()
        if not any(kw in lower for kw in keywords):
            continue
        items.append(
            NewsItem(
                title=f"[DAO提案] {raw_title}",
                url=entry.get("link", ""),
                source="Arbitrum DAO Forum",
                published_at=_entry_published_at(entry),
                coins=["ARB"],
                forced_sentiment="good",
            )
        )
    return items


def fetch_all_news(cfg: dict) -> list[NewsItem]:
    """全取得元からニュースを集め、除外語フィルタと銘柄振り分けを行う。"""
    coins_cfg = cfg["coins"]
    exclude_keywords = cfg.get("exclude_keywords", [])
    sources_cfg = cfg["sources"]
    all_items: list[NewsItem] = []

    # 銘柄ごとの Google News（英語・日本語）
    for symbol, coin_cfg in coins_cfg.items():
        names = coin_cfg.get("names", [])
        en_names = [n for n in names if is_ascii(n)]
        ja_names = [n for n in names if not is_ascii(n)]
        if sources_cfg.get("google_news_en") and en_names:
            for item in _fetch_google_news(en_names, "en"):
                item.coins = [symbol]
                all_items.append(item)
        if sources_cfg.get("google_news_ja") and ja_names:
            for item in _fetch_google_news(ja_names, "ja"):
                item.coins = [symbol]
                all_items.append(item)

        # 銘柄ごとの追加の検索語（Robinhood Chain, USD1, Morpho 等）
        extra_terms = cfg.get(symbol.lower(), {}).get("extra_search_terms", [])
        if sources_cfg.get("google_news_en") and extra_terms:
            for item in _fetch_google_news(extra_terms, "en", quote=False):
                item.coins = [symbol]
                all_items.append(item)

    # 汎用フィード（複数銘柄に関係しうるので、あとでタイトルから銘柄を判定する）
    # 日本語メディアも含む。これらは本文抜粋が取れ、リンクも記事に直接つながる。
    excerpt_cfg = cfg.get("excerpt", {})
    excerpt_max_length = excerpt_cfg.get("max_length", 300)
    for feed in sources_cfg.get("feeds", []):
        label, url = feed.get("name"), feed.get("url")
        if not url:
            continue
        for item in _fetch_generic_feed(url, label, excerpt_max_length):
            item.coins = match_coins(item.title, coins_cfg)
            if item.coins:
                all_items.append(item)

    # Arbitrum DAO フォーラム
    all_items.extend(fetch_dao_forum_items(cfg))

    # 除外語フィルタ（DAO提案は対象外）
    filtered = []
    for item in all_items:
        if item.forced_sentiment:
            filtered.append(item)
            continue
        if classify.is_excluded(item.title, exclude_keywords):
            continue
        if not item.url or not item.coins:
            continue
        filtered.append(item)

    if excerpt_cfg.get("enabled") and excerpt_cfg.get("fetch_og_description"):
        _fill_missing_excerpts(filtered, excerpt_cfg)

    return filtered


def _fill_missing_excerpts(items: list[NewsItem], excerpt_cfg: dict) -> None:
    """RSSに抜粋が無い記事（CoinDesk等）を、記事ページから補う。

    Google Newsのリンクは本文に到達できないので対象外。取得件数には上限を設けて
    1回の実行が長くなりすぎないようにする。
    """
    max_fetches = excerpt_cfg.get("max_page_fetches", 12)
    timeout = excerpt_cfg.get("fetch_timeout_seconds", 8)
    max_length = excerpt_cfg.get("max_length", 300)

    seen_urls: set[str] = set()
    fetched = 0
    for item in items:
        if fetched >= max_fetches:
            break
        if item.excerpt or not item.url or "news.google.com" in item.url:
            continue
        if item.url in seen_urls:
            continue
        seen_urls.add(item.url)
        description = fetch_page_description(item.url, timeout=timeout)
        fetched += 1
        if description:
            item.excerpt = clean_excerpt(description, max_length)


def persist_news_items(conn, items: list[NewsItem], cfg: dict) -> list[tuple[int, bool]]:
    """記事をDBに保存し、(article_id, 新規かどうか) のリストを返す。

    正規化タイトルが一致する既存記事があれば媒体・銘柄を追加するだけにし、
    無ければ新規記事として判定結果とともに保存する。
    """
    results: list[tuple[int, bool]] = []

    dedup_cfg = cfg.get("dedup", {})
    merge_similar = dedup_cfg.get("merge_similar_headlines", True)
    min_shared_terms = dedup_cfg.get("min_shared_terms", 3)
    similarity_window_days = dedup_cfg.get("similarity_window_days", 2)
    similar_candidates = []
    if merge_similar:
        since = (datetime.now(timezone.utc) - timedelta(days=similarity_window_days)).isoformat()
        similar_candidates = db.recent_articles_for_similarity(conn, since)

    for item in items:
        norm = normalize_title(item.title)
        existing = db.find_article_by_normalized_title(conn, norm)

        if existing is None and merge_similar:
            # 完全一致で見つからない場合、言い回しの違う同じ話題を探す。
            # ただし銘柄が重ならない記事とはまとめない
            # （例:「CoinbaseがARBを上場」と「CoinbaseがSOLを上場」は別のニュース）
            item_coins = set(item.coins)
            for candidate in similar_candidates:
                candidate_coins = set((candidate["coins"] or "").split(",")) - {""}
                if candidate_coins and item_coins and not (candidate_coins & item_coins):
                    continue
                if is_same_story(norm, candidate["normalized_title"], min_shared_terms):
                    existing = conn.execute(
                        "SELECT * FROM articles WHERE id = ?", (candidate["id"],)
                    ).fetchone()
                    break

        if existing:
            db.add_source_to_article(
                conn, existing["id"], item.source, item.url, item.published_at.isoformat()
            )
            for coin in item.coins:
                conn.execute(
                    "INSERT OR IGNORE INTO article_coins(article_id, coin) VALUES (?, ?)",
                    (existing["id"], coin),
                )
            # Google News経由で先に登録された記事でも、本文抜粋が取れる媒体で
            # 同じ記事が見つかったら抜粋とリンクを補完する
            if item.excerpt:
                db.set_excerpt_if_missing(
                    conn, existing["id"], item.excerpt, item.url, item.source
                )
            conn.commit()
            results.append((existing["id"], False))
        else:
            classification = classify.score_headline(item.title, cfg)
            score = classification.score
            sentiment = classification.sentiment
            emoji = classification.emoji
            if item.forced_sentiment == "good" and score <= 0:
                score = max(score, 1)
                sentiment = "good"
                emoji = cfg["emojis"]["good"]
            article_id = db.insert_article(
                conn,
                normalized_title=norm,
                display_title=item.title,
                url=item.url,
                first_source=item.source,
                first_published_at=item.published_at.isoformat(),
                score=score,
                sentiment=sentiment,
                emoji=emoji,
                is_critical=classification.is_critical,
                critical_hits=classification.critical_hits,
                coins=item.coins,
                excerpt=item.excerpt or None,
            )
            results.append((article_id, True))
            if merge_similar:
                # 同じ実行の中で後続の言い換え記事ともまとめられるようにする
                similar_candidates.append({"id": article_id, "normalized_title": norm, "coins": ",".join(item.coins)})

    return results
