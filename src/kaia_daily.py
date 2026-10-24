"""Kaiaアンバサダー向け: 毎日の発信案づくり（コミュニティ向けニュースまとめ＋X用1本）。

流れ: 集める → 見分ける → 選ぶ → 材料をそろえる → 書く → 確かめる → 届ける

- 集める: Kaia公式ブログ・KAIA DLT Foundation（PR TIMES）・ガバナンスフォーラム・GitHub・
  JPYC（PR TIMES・公式note）・ニュース検索（Kaia / JPYC / 円建てステーブルコイン）から話題をDBにためる。
- 見分ける: 安いモデル（Haiku）で「Kaia/JPYC/円ステーブルコインのどれの話か・大事さ・良い話/悪い話・
  値段の話か・同じ出来事か」を付ける（同じ出来事を何媒体が報じても1件にまとめるため）。
- 選ぶ: まだ届けていない新しい話題を最大5件（Kaia×JPYC → Kaia → JPYC → 円ステーブルコインの順）。
  悪い話・値段の話は発信案にしない（本人にだけ「知っておきたいニュース」として知らせる）。
  新しい話題が無い日は、曜日ごとの定番テーマ（kaia_topics.yaml）で「今日の豆知識」を作る。
- 書く: 上位モデルに「材料に書いてあることだけ」で、コミュニティ向けのまとめとX用の本文を書かせる。
  URLと出典名はプログラムが差し込む（モデルに書かせると間違うことがあるため）。
- 確かめる: Xの文字数（日本語は1字=2）・材料に無い数字・煽り表現を機械的に点検する。
- 届ける: 本人のDiscordへ届ける。コミュニティ（LINE・Telegram・Discord）やXへは本人が確認してから貼る。
"""
from __future__ import annotations

import calendar
import html as html_lib
import json
import logging
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import feedparser
import requests
import yaml

from . import db, discord, summarize
from .utils import now_jst

logger = logging.getLogger("crypto_news_bot.kaia")

BASE_DIR = Path(__file__).resolve().parent.parent
TOPICS_PATH = BASE_DIR / "kaia_topics.yaml"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
REQUEST_TIMEOUT = 20

# 見出し・概要にこれが無い記事は拾わない（ニュース検索や一般媒体のノイズよけ）
TOPIC_KEYWORDS = re.compile(
    r"\bKaia\b|カイア|카이아|LINE NEXT|Mini ?Dapps?|ミニDapp|Klaytn|クレイトン|Finschia|Unifi|"
    r"JPYC|日本円ステーブルコイン|円建てステーブルコイン|円ステーブルコイン|日本円建てステーブルコイン",
    re.IGNORECASE,
)
_KW_KAIA = re.compile(r"\bKaia\b|カイア|카이아|LINE NEXT|Mini ?Dapp|Klaytn|Finschia|Unifi", re.IGNORECASE)
_KW_JPYC = re.compile(r"JPYC", re.IGNORECASE)
# 値段の話（予想・急騰急落・チャート分析）。発信案にはしない
PRICE_TALK = re.compile(
    r"price prediction|price analysis|price forecast|surges?|plunges?|rall(y|ies)|soars?|"
    r"価格予想|急騰|急落|高騰|暴落|상승|하락|급등|급락|시세",
    re.IGNORECASE,
)
# 煽り・投資勧誘ととられる表現。出たら書き直させる
HYPE_WORDS = re.compile(
    r"爆上げ|爆益|買い時|今が買い|儲か|億り人|ムーン|to the moon|価格予想|仕込み|チャンス到来|今すぐ買",
    re.IGNORECASE,
)

THEME_LABELS = {
    "digest": "📰 ニュースまとめ",
    "news": "📰 ニュース",
    "recap": "🗓 先週のまとめ",
    "howto": "🔰 使い方",
    "data": "📊 数字で見るKaia",
    "ecosystem": "🌱 エコシステム",
    "tech": "⚙️ 仕組み",
    "japan": "🗾 日本とKaia",
    "basics": "📘 Kaiaの基本",
}
# まとめの並び順と見出し
TOPIC_SECTIONS = [
    ("both", "🤝 Kaia × JPYC"),
    ("kaia", "🟢 Kaia"),
    ("jpyc", "💴 JPYC"),
    ("stablecoin", "🏦 円ステーブルコインの動き"),
]
TOPICS = {t for t, _ in TOPIC_SECTIONS}
NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
WEEKDAY_JA = "月火水木金土日"
OFFICIAL_KINDS = {"official", "forum", "release"}
KIND_LABELS = {"official": "Kaia公式", "forum": "Kaia公式", "release": "Kaia公式", "partner": "提携先の公式発表"}


def _kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, "報道")


SCHEMA = """
CREATE TABLE IF NOT EXISTS kaia_items (
    url TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    lang TEXT,
    title TEXT NOT NULL,
    summary TEXT,
    body TEXT,
    published_at TEXT,
    first_seen TEXT NOT NULL,
    triaged INTEGER DEFAULT 0,
    relevant INTEGER DEFAULT 1,
    story TEXT,
    ja_title TEXT,
    importance INTEGER,
    sentiment TEXT,
    price_talk INTEGER DEFAULT 0,
    japan INTEGER DEFAULT 0,
    topic TEXT,
    used_on TEXT,
    digest_on TEXT
);
CREATE TABLE IF NOT EXISTS kaia_posts (
    date TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    topic_id TEXT,
    story TEXT,
    headline TEXT,
    x_post TEXT,
    created_at TEXT NOT NULL
);
"""


# ---------------------------------------------------------------- 共通

def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(kaia_items)")}
    for column in ("topic", "digest_on"):
        if column not in existing:
            conn.execute(f"ALTER TABLE kaia_items ADD COLUMN {column} TEXT")
    conn.commit()


def _kcfg(cfg: dict) -> dict:
    return cfg.get("kaia_daily") or {}


def _strip_html(html: str) -> str:
    html = re.sub(r"<(head|script|style|noscript|svg|nav|footer|header)\b[^>]*>.*?</\1>", " ", html or "",
                  flags=re.IGNORECASE | re.DOTALL)
    # 属性の中に ">" があってもタグを取りこぼさないようにする
    text = re.sub(r"""<(?:[^>"']|"[^"]*"|'[^']*')*>""", " ", html)
    text = html_lib.unescape(text).replace("​", "")
    return re.sub(r"\s+", " ", text).strip()


def _http_get(url: str) -> requests.Response | None:
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp
    except requests.RequestException as e:
        logger.info("取得に失敗しました url=%s error=%s", url[:90], e)
        return None


def _decode(resp: requests.Response) -> str:
    # 文字コードの指定が無いページを latin-1 で読んで日本語が化けるのを防ぐ
    if resp.encoding and resp.encoding.lower() not in ("iso-8859-1", "latin-1"):
        return resp.text
    return resp.content.decode("utf-8", errors="replace")


def fetch_page_text(url: str, max_chars: int = 6000) -> str:
    """記事・ドキュメントの本文らしきテキスト。<article> があればそこだけ使う。"""
    resp = _http_get(url)
    if resp is None:
        return ""
    html = _decode(resp)
    m = re.search(r"<article\b[^>]*>(.*?)</article>", html, re.IGNORECASE | re.DOTALL)
    text = _strip_html(m.group(1) if m else html)
    return text[:max_chars]


def _real_url(link: str) -> str:
    """ニュース検索(Bing)の転送リンクから記事の本当のURLを取り出す。"""
    if "bing.com/news/apiclick" in link:
        q = parse_qs(urlparse(link).query)
        if q.get("url"):
            return q["url"][0]
    return link


def _entry_time(entry) -> datetime | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ja_date(dt: datetime | None) -> str:
    if dt is None:
        return "日付不明"
    d = dt.astimezone(timezone(timedelta(hours=9)))
    return f"{d.year}年{d.month}月{d.day}日（{d.month}/{d.day}）"


# ---------------------------------------------------------------- 集める

def collect(conn, cfg: dict, now: datetime) -> int:
    """情報源を巡回して、まだ持っていない話題をDBに入れる。戻り値は新しく入れた件数。"""
    kcfg = _kcfg(cfg)
    sources = list(kcfg.get("sources", []))
    if kcfg.get("include_general_feeds", True):
        for f in cfg.get("sources", {}).get("feeds", []):
            sources.append({"name": f["name"], "url": f["url"], "kind": "media",
                            "lang": "ja" if re.search(r"[ぁ-んァ-ン]", f["name"]) or "Japan" in f["name"] else "en",
                            "max_age_days": 7, "require_keywords": True})

    added = 0
    for src in sources:
        resp = _http_get(src["url"])
        if resp is None:
            continue
        feed = feedparser.parse(resp.content)
        max_age = timedelta(days=src.get("max_age_days", 7))
        pattern = re.compile(src["title_pattern"]) if src.get("title_pattern") else None
        for entry in feed.entries:
            title = _strip_html(entry.get("title", ""))
            link = _real_url(entry.get("link", ""))
            if not title or not link:
                continue
            published = _entry_time(entry)
            if published and now - published > max_age:
                continue
            if published is None and src.get("kind") == "media":
                continue  # 日付の無い報道は古い可能性があるので使わない
            if pattern and not pattern.search(title):
                continue
            summary = _strip_html(entry.get("summary", ""))[:6000]
            if src.get("require_keywords") and not TOPIC_KEYWORDS.search(f"{title} {summary[:400]}"):
                continue
            body = ""
            if entry.get("content"):
                body = _strip_html(entry["content"][0].get("value", ""))[:8000]
            # ニュース検索の結果は、検索名ではなく実際の媒体名（CoinPost等）を出典にする
            source_name = src["name"]
            if "bing.com" in src["url"]:
                source_name = (entry.get("news_source") or "").strip() or urlparse(link).netloc or src["name"]
            cur = conn.execute(
                "INSERT OR IGNORE INTO kaia_items(url, source, kind, lang, title, summary, body, published_at, first_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (link, source_name, src.get("kind", "media"), src.get("lang", "en"), title, summary, body,
                 published.isoformat() if published else None, now.astimezone(timezone.utc).isoformat()),
            )
            added += cur.rowcount
    conn.commit()
    logger.info("Kaia・JPYCの話題: 新しく%d件を記録しました", added)
    return added


# ---------------------------------------------------------------- 見分ける

TRIAGE_SYSTEM = """あなたは、ブロックチェーン「Kaia」（KlaytnとFinschiaが統合したチェーン）と、
日本円ステーブルコイン「JPYC」（Kaiaでも発行されている）のニュースを仕分ける係です。
各記事について次を判定してください。
- relevant: Kaia・JPYC・日本円建てステーブルコインのどれかの話ならtrue。人名のKaiaなど無関係ならfalse。
- topic: both（KaiaとJPYCの両方が関わる。LINE NEXTやUnifiとJPYCの話もここ）/ kaia（Kaia・LINE NEXT・Mini Dapp・Unifi）/
  jpyc（JPYC単独）/ stablecoin（JPYC以外の日本円建てステーブルコインや国内のステーブルコイン制度）/ other
- story: 同じ出来事には同じ短い英語のキー（例 jpyc-sbj-linenext-mou）。媒体や言語が違っても同じ出来事なら同じキー。
  「既知の話題」と同じ出来事ならそのキーをそのまま使う。
- ja_title: 日本語の見出し（40字以内、事実だけ）。
- importance: 1〜5。5=大手取引所への上場・大企業や銀行との提携・大型アップデート・規制の大きな変化。
  3=新機能・中規模の提携・ガバナンスの提案・月次レポート。1=ごく小さい話・宣伝・名前が少し出てくるだけ。
- sentiment: positive / neutral / negative（ハッキング・障害・上場廃止・訴訟・行政処分などはnegative）。
- price_talk: 値動き・価格予想・チャート分析・額面からの乖離が中心の記事ならtrue。
- japan: 日本の企業・日本円・日本市場に関係するならtrue。"""

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "relevant": {"type": "boolean"},
                    "topic": {"type": "string", "enum": ["both", "kaia", "jpyc", "stablecoin", "other"]},
                    "story": {"type": "string"},
                    "ja_title": {"type": "string"},
                    "importance": {"type": "integer"},
                    "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
                    "price_talk": {"type": "boolean"},
                    "japan": {"type": "boolean"},
                },
                "required": ["id", "relevant", "topic", "story", "ja_title", "importance", "sentiment",
                             "price_talk", "japan"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def _keyword_topic(text: str) -> str:
    kaia, jpyc = bool(_KW_KAIA.search(text)), bool(_KW_JPYC.search(text))
    if kaia and jpyc:
        return "both"
    if kaia:
        return "kaia"
    if jpyc:
        return "jpyc"
    return "stablecoin" if "ステーブルコイン" in text else "other"


def _heuristic_triage(row) -> dict:
    """モデルが使えないときの簡易判定（公式は大事め・値段の話は除外）。"""
    title = row["title"]
    topic = _keyword_topic(f"{title} {(row['summary'] or '')[:300]}")
    return {
        "relevant": topic != "other" or row["kind"] in OFFICIAL_KINDS,
        "topic": "kaia" if topic == "other" else topic,
        "story": re.sub(r"[^a-z0-9]+", "-", title.lower())[:60] or row["url"],
        "ja_title": title[:60],
        "importance": 4 if row["kind"] in OFFICIAL_KINDS else 3,
        "sentiment": "neutral",
        "price_talk": bool(PRICE_TALK.search(title)),
        "japan": row["lang"] == "ja",
    }


def _apply_triage(conn, rowid: int, t: dict) -> None:
    topic = t.get("topic") if t.get("topic") in TOPICS else "other"
    conn.execute(
        "UPDATE kaia_items SET triaged=1, relevant=?, topic=?, story=?, ja_title=?, importance=?, sentiment=?, "
        "price_talk=?, japan=? WHERE rowid=?",
        (int(bool(t["relevant"]) and topic != "other"), topic, t["story"].strip()[:80], t["ja_title"].strip()[:80],
         max(0, min(5, int(t["importance"]))), t["sentiment"], int(bool(t["price_talk"])),
         int(bool(t["japan"])), rowid),
    )


def triage(conn, cfg: dict, client, now: datetime) -> int:
    """まだ仕分けていない話題に、種類・大事さ・良し悪し・同じ出来事のキーを付ける。"""
    kcfg = _kcfg(cfg)
    rows = conn.execute(
        "SELECT rowid AS id, * FROM kaia_items WHERE triaged=0 ORDER BY published_at DESC LIMIT 60"
    ).fetchall()
    if not rows:
        return 0

    since = (now - timedelta(days=21)).astimezone(timezone.utc).isoformat()
    known = conn.execute(
        "SELECT story, MAX(ja_title) AS ja_title FROM kaia_items WHERE triaged=1 AND relevant=1 "
        "AND story IS NOT NULL AND first_seen >= ? GROUP BY story LIMIT 80",
        (since,),
    ).fetchall()

    results: dict[int, dict] = {}
    if client is not None:
        lines = []
        for r in rows:
            pub = _parse_iso(r["published_at"])
            lines.append(json.dumps({
                "id": r["id"], "source": r["source"], "lang": r["lang"],
                "date": pub.date().isoformat() if pub else None,
                "title": r["title"], "excerpt": (r["summary"] or r["body"] or "")[:300],
            }, ensure_ascii=False))
        known_text = "\n".join(f"- {k['story']}: {k['ja_title']}" for k in known) or "（なし）"
        model = kcfg.get("triage_model", "claude-haiku-5-5")
        try:
            response = client.messages.create(
                model=model,
                max_tokens=8000,
                system=TRIAGE_SYSTEM,
                messages=[{"role": "user", "content": f"既知の話題:\n{known_text}\n\n記事（1行1件のJSON）:\n" + "\n".join(lines)}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": TRIAGE_SCHEMA}},
            )
            summarize._record_usage(model, response)
            if response.stop_reason == "end_turn":
                text = next(b.text for b in response.content if b.type == "text")
                for item in json.loads(text)["items"]:
                    results[int(item["id"])] = item
            else:
                logger.warning("仕分けが途中で止まりました stop_reason=%s", response.stop_reason)
        except Exception as e:  # 仕分けに失敗しても簡易判定で続ける
            logger.warning("仕分けに失敗しました（簡易判定で続けます） error=%s", e)

    for r in rows:
        _apply_triage(conn, r["id"], results.get(r["id"]) or _heuristic_triage(r))
    conn.commit()
    logger.info("話題を%d件仕分けました（モデル判定%d件）", len(rows), len(results))
    return len(rows)


# ---------------------------------------------------------------- 選ぶ

def _rep_key(item: dict):
    """同じ出来事の記事のうち、代表にする順（公式 → 提携先の発表 → 日本語 → URLが短い）。"""
    return (item["kind"] not in OFFICIAL_KINDS, item["kind"] != "partner", item["lang"] != "ja", len(item["url"]))


def _stories(conn, now: datetime, days: float) -> list[dict]:
    """直近の話題を「同じ出来事」ごとにまとめる。"""
    since = (now - timedelta(days=days)).astimezone(timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT * FROM kaia_items WHERE triaged=1 AND relevant=1 AND story IS NOT NULL "
        "AND COALESCE(published_at, first_seen) >= ?",
        (since,),
    ).fetchall()
    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.setdefault(r["story"], {"story": r["story"], "items": [], "importance": 0,
                                           "negative": False, "price_talk": False, "japan": False,
                                           "official": False, "used": False, "digested": False,
                                           "ja_title": r["ja_title"], "topic": r["topic"] or "kaia"})
        g["items"].append(dict(r))
        if (r["importance"] or 0) > g["importance"]:
            g["importance"] = r["importance"] or 0
            g["ja_title"] = r["ja_title"]
            g["topic"] = r["topic"] or g["topic"]
        g["negative"] |= r["sentiment"] == "negative"
        g["price_talk"] |= bool(r["price_talk"])
        g["japan"] |= bool(r["japan"])
        g["official"] |= r["kind"] in OFFICIAL_KINDS
        g["used"] |= bool(r["used_on"])
        g["digested"] |= bool(r["digest_on"])
    for g in groups.values():
        g["items"].sort(key=_rep_key)
        times = [_parse_iso(i["published_at"]) or _parse_iso(i["first_seen"]) for i in g["items"]]
        g["first_at"] = min(times)
        g["latest_at"] = max(times)
        g["sources"] = len({i["source"] for i in g["items"]})
    return list(groups.values())


def score_story(g: dict, now: datetime) -> float:
    age_days = (now - g["latest_at"]).total_seconds() / 86400
    fresh = 3 if age_days <= 1 else 2 if age_days <= 3 else 1 if age_days <= 7 else 0
    return (g["importance"] * 2 + fresh + (1 if g["official"] else 0) + (2 if g["japan"] else 0)
            + min(g["sources"] - 1, 2))


def digest_stories(conn, cfg: dict, now: datetime) -> list[dict]:
    """今日のまとめに入れる話題（まだ届けていないもの）。並びはまとめの見出し順。"""
    kcfg = _kcfg(cfg)
    first_time = conn.execute("SELECT 1 FROM kaia_posts WHERE kind='digest' LIMIT 1").fetchone() is None
    # 初回だけは直近1週間から選ぶ（いきなり空っぽにしない）
    hours = kcfg.get("digest_first_window_hours", 168) if first_time else kcfg.get("digest_window_hours", 72)
    candidates = [
        g for g in _stories(conn, now, days=hours / 24)
        if not g["digested"] and not g["negative"] and not g["price_talk"]
        and g["topic"] in TOPICS and g["importance"] >= kcfg.get("digest_min_importance", 2)
    ]
    candidates.sort(key=lambda g: score_story(g, now), reverse=True)

    limits = kcfg.get("digest_topic_limits", {"stablecoin": 2})
    picked, per_topic = [], {}
    for g in candidates:
        if per_topic.get(g["topic"], 0) >= limits.get(g["topic"], 99):
            continue
        picked.append(g)
        per_topic[g["topic"]] = per_topic.get(g["topic"], 0) + 1
        if len(picked) >= kcfg.get("digest_max_items", 5):
            break
    order = {t: n for n, (t, _) in enumerate(TOPIC_SECTIONS)}
    picked.sort(key=lambda g: (order[g["topic"]], -score_story(g, now)))
    return picked


def pick_news(conn, cfg: dict, now: datetime) -> dict | None:
    """X用に1本だけ書くときの、新しくて大事な話題。無ければ None。"""
    kcfg = _kcfg(cfg)
    threshold = kcfg.get("news_threshold", 8)
    min_importance = kcfg.get("news_min_importance", 3)
    best = None
    for g in _stories(conn, now, days=14):
        if g["used"] or g["negative"] or g["price_talk"] or g["importance"] < min_importance:
            continue
        if now - g["first_at"] > timedelta(days=kcfg.get("news_max_age_days", 10)):
            continue
        s = score_story(g, now)
        if s >= threshold and (best is None or s > best[0]):
            best = (s, g)
    return best[1] if best else None


def cautions(conn, now: datetime) -> list[dict]:
    """知っておきたい悪いニュース（発信案にはしない。本人にだけ知らせる）。"""
    return [g for g in _stories(conn, now, days=2) if g["negative"] and g["importance"] >= 3]


def recap_stories(conn, now: datetime, limit: int = 4) -> list[dict]:
    gs = [g for g in _stories(conn, now, days=7)
          if not g["negative"] and not g["price_talk"] and g["importance"] >= 2]
    gs.sort(key=lambda g: score_story(g, now), reverse=True)
    return gs[:limit]


def load_topics() -> list[dict]:
    with open(TOPICS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f).get("topics", [])


def pick_topics(conn, theme: str) -> list[dict]:
    """そのテーマの定番ネタを、長く使っていない順に並べる。"""
    topics = [t for t in load_topics() if t["theme"] == theme]
    last_used = {r["topic_id"]: r["d"] for r in conn.execute(
        "SELECT topic_id, MAX(date) AS d FROM kaia_posts WHERE topic_id IS NOT NULL GROUP BY topic_id")}
    return sorted(topics, key=lambda t: last_used.get(t["id"], ""))


def theme_for(cfg: dict, now: datetime) -> str:
    themes = _kcfg(cfg).get("weekday_themes", {})
    return themes.get(now.weekday(), themes.get(str(now.weekday()), "basics"))


# ---------------------------------------------------------------- 材料をそろえる

def _item_text(item: dict, max_chars: int = 6000) -> str:
    # RSSに本文が入っている情報源（ブログ・フォーラム）はページを取りに行かない
    rss_text = max(item.get("body") or "", item.get("summary") or "", key=len)
    if len(rss_text) > 400:
        return rss_text[:max_chars]
    text = fetch_page_text(item["url"], max_chars=max_chars)
    return text if len(text) >= 200 else rss_text[:max_chars]


def news_material(story: dict) -> tuple[str, list[str]]:
    parts, urls = [], []
    for n, item in enumerate(story["items"][:3], 1):
        text = _item_text(item)
        if not text:
            continue
        pub = _parse_iso(item["published_at"])
        parts.append(f"[資料{n}] {item['source']}（{_kind_label(item['kind'])}） 公開日: {_ja_date(pub)}\n"
                     f"見出し: {item['title']}\nURL: {item['url']}\n本文: {text}")
        urls.append(item["url"])
    return "\n\n".join(parts), urls


def digest_material(stories: list[dict]) -> str:
    """まとめ用の材料。話題ごとに代表の記事1本（足りなければ2本目も）の本文を付ける。"""
    parts = []
    for n, g in enumerate(stories, 1):
        texts = []
        for item in g["items"][:2]:
            text = _item_text(item, max_chars=2500)
            if text:
                pub = _parse_iso(item["published_at"])
                texts.append(f"出典: {item['source']}（{_kind_label(item['kind'])}） 公開日: {_ja_date(pub)}\n"
                             f"見出し: {item['title']}\n本文: {text}")
            if sum(len(t) for t in texts) > 1500:
                break
        if not texts:
            texts = [f"見出し: {g['items'][0]['title']}"]
        section = dict(TOPIC_SECTIONS)[g["topic"]]
        parts.append(f"[話題id={n}] 区分: {section}\n" + "\n---\n".join(texts))
    return "\n\n".join(parts)


def recap_material(stories: list[dict]) -> tuple[str, list[str]]:
    parts, urls = [], []
    for n, g in enumerate(stories, 1):
        item = g["items"][0]
        text = (item.get("body") or item.get("summary") or "")[:800]
        parts.append(f"[話題{n}] {g['ja_title']}\n出典: {item['source']}（{_kind_label(item['kind'])}） 公開日: "
                     f"{_ja_date(_parse_iso(item['published_at']))}\n見出し: {item['title']}\n概要: {text}")
        urls.append(item["url"])
    return "\n\n".join(parts), urls


def topic_material(topic: dict) -> tuple[str, list[str]]:
    parts, urls = [], []
    for n, url in enumerate(topic.get("urls", [])[:2], 1):
        text = fetch_page_text(url, max_chars=7000)
        if len(text) < 300:
            continue
        parts.append(f"[資料{n}] 公式の資料\nURL: {url}\n本文: {text}")
        urls.append(url)
    return "\n\n".join(parts), urls


def _ja_amount(x: float) -> str:
    if x >= 1e8:
        return f"{x / 1e8:.2f}億"
    if x >= 1e4:
        return f"{x / 1e4:,.0f}万"
    return f"{x:,.0f}"


def data_material(now: datetime) -> tuple[str, list[str]]:
    """数字で見るKaia: DefiLlamaのステーブルコイン流通額とTVL。値段は使わない。"""
    lines = []
    resp = _http_get("https://stablecoins.llama.fi/stablecoincharts/Kaia")
    if resp is not None:
        sc = resp.json()
        if len(sc) > 31:
            last, w1, m1 = sc[-1], sc[-8], sc[-31]
            day = datetime.fromtimestamp(int(last["date"]), tz=timezone.utc)

            def usd_total(rec):
                return sum((rec.get("totalCirculatingUSD") or {}).values())

            def jpy_native(rec):
                return (rec.get("totalCirculating") or {}).get("peggedJPY") or 0

            t0, t7, t30 = usd_total(last), usd_total(w1), usd_total(m1)
            lines.append(f"Kaia上のステーブルコイン流通額の合計（{_ja_date(day)}時点）: {t0:,.0f}ドル（約{_ja_amount(t0)}ドル）")
            lines.append(f"  7日前: {t7:,.0f}ドル（約{_ja_amount(t7)}ドル） / 30日前: {t30:,.0f}ドル（約{_ja_amount(t30)}ドル）")
            if t7:
                lines.append(f"  7日間の増減: {(t0 / t7 - 1) * 100:+.1f}% / 30日間の増減: {(t0 / t30 - 1) * 100:+.1f}%")
            j0, j30 = jpy_native(last), jpy_native(m1)
            if j0:
                lines.append(f"うち日本円建てステーブルコイン（JPYCなど）: {j0:,.0f}円（約{_ja_amount(j0)}円）")
                if j30:
                    lines.append(f"  30日前: {j30:,.0f}円（約{_ja_amount(j30)}円） / 30日間の増減: {(j0 / j30 - 1) * 100:+.1f}%")
    resp = _http_get("https://api.llama.fi/v2/historicalChainTvl/Kaia")
    if resp is not None:
        tv = resp.json()
        if len(tv) > 8:
            day = datetime.fromtimestamp(int(tv[-1]["date"]), tz=timezone.utc)
            v0, v7 = tv[-1]["tvl"], tv[-8]["tvl"]
            lines.append(f"Kaia上のDeFiに預けられている資産（TVL, {_ja_date(day)}時点）: {v0:,.0f}ドル（約{_ja_amount(v0)}ドル）、"
                         f"7日前: {v7:,.0f}ドル（7日間の増減 {(v0 / v7 - 1) * 100:+.1f}%）")
    if not lines:
        return "", []
    urls = ["https://defillama.com/stablecoins/Kaia", "https://defillama.com/chain/Kaia"]
    text = ("[資料1] DefiLlama（ブロックチェーンのデータを集計している公開サイト）の数値\n"
            + "\n".join(lines) + f"\nURL: {urls[0]} / {urls[1]}")
    return text, urls


# ---------------------------------------------------------------- 書く

WRITER_SYSTEM = """あなたは、ブロックチェーン「Kaia」の日本人アンバサダーが発信する文章を書く編集者です。
Kaiaは、KakaoのKlaytnとLINEのFinschiaが統合して生まれたブロックチェーンです。
日本円ステーブルコイン「JPYC」はKaiaでも発行されています。

読者は日本のKaiaコミュニティとXのユーザーで、暗号資産に詳しくない人も多くいます。
Kaia・JPYCの動きを、正確に・わかりやすく伝えることが目的です。

守ること:
- 事実は「材料」に書かれていることだけを使う。材料に無い数字・日付・社名・機能は書かない。推測で補わない。
- 数字・日付・社名・プロジェクト名は材料どおり正確に書く。
- 「Kaia公式」「提携先の公式発表」の資料は「◯◯が発表しました」と発表元を書く。「報道」の資料は「〜と報じられています」のように伝聞で書く。
- 材料の公開日が今日より前なら「◯月◯日に発表」のように時点を書き、古い情報を最新のように書かない。
- 価格・値上がり・投資判断の話はしない。「買い」「爆上げ」「儲かる」「今がチャンス」のような煽る表現は使わない。
- 専門用語には短い言い換えを添える（例: ガス代（取引手数料）、PoC（実証実験））。
- ハッシュタグとURLは書かない（後で自動で付ける）。
- LINEやTelegramにそのまま貼るので、太字（**）や見出し記号（#）などの書式記号は使わない。
- 絵文字は控えめに（X本文は2個まで）。"""

WRITER_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "20字以内の要約（確認用）"},
        "x_post": {"type": "string", "description": "Xの本文"},
        "x_reply": {"type": "string", "description": "リプ欄に付ける一言（30字以内、URLは書かない）"},
        "long_post": {"type": "string", "description": "LINE・Telegram向けの長文版"},
        "facts": {"type": "array", "items": {"type": "string"}, "description": "使った事実（各40字以内）"},
    },
    "required": ["headline", "x_post", "x_reply", "long_post", "facts"],
    "additionalProperties": False,
}

DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "今日のまとめを20字以内で（確認用）"},
        "intro": {"type": "string", "description": "今日の見どころを1文（40字以内）"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "title": {"type": "string", "description": "見出し（30字以内、事実だけ）"},
                    "summary": {"type": "string", "description": "何が起きたか（2〜3文、合わせて120字以内）"},
                    "point": {"type": "string", "description": "それで何が変わるか（1文40字以内。材料に無ければ空文字）"},
                },
                "required": ["id", "title", "summary", "point"],
                "additionalProperties": False,
            },
        },
        "closing": {"type": "string", "description": "締めの一言（30字以内、無くてよい）"},
        "x_item_id": {"type": "integer", "description": "X向けに一番ふさわしい話題のid"},
        "x_post": {"type": "string", "description": "その話題のXの本文"},
        "x_reply": {"type": "string", "description": "リプ欄に付ける一言（30字以内、URLは書かない）"},
    },
    "required": ["headline", "intro", "items", "closing", "x_item_id", "x_post", "x_reply"],
    "additionalProperties": False,
}

KIND_GUIDE = {
    "news": "この話題を、Kaiaを知らない人にも伝わるように紹介する。何が起きたか → それで何が変わるのか（材料にある範囲で）→ 一言。",
    "recap": "先週のKaia・JPYCの動きを、話題ごとに1行ずつ（3〜4行）まとめる。1行目は「先週のKaia」などの見出しにする。",
    "data": "数字で見るKaia。材料の数字を正確に使う。増えた数字を中心にしてよいが、増減を誇張しない。減った数字に触れるなら事実として淡々と。",
}


def x_weight(text: str) -> int:
    """Xの文字数（日本語・絵文字は1字=2、URLは23）。上限は280。"""
    text = re.sub(r"https?://\S+", "x" * 23, text)
    total = 0
    for ch in unicodedata.normalize("NFC", text):
        cp = ord(ch)
        if cp in (0xFE0F, 0xFE0E):
            continue
        if cp <= 0x10FF or 0x2000 <= cp <= 0x200D or 0x2010 <= cp <= 0x201F or 0x2032 <= cp <= 0x2037:
            total += 1
        else:
            total += 2
    return total


def compose_x(post: dict, kcfg: dict, urls: list[str]) -> str:
    tags = " ".join(kcfg.get("hashtags", ["#Kaia"]))
    text = post["x_post"].strip()
    if urls and not kcfg.get("link_in_reply", True):
        text += f"\n{urls[0]}"
    return f"{text}\n\n{tags}" if tags else text


def _x_max_chars(kcfg: dict) -> int:
    tags = " ".join(kcfg.get("hashtags", ["#Kaia"]))
    room = kcfg.get("x_max_weighted", 280) - x_weight(f"\n\n{tags}") - (25 if not kcfg.get("link_in_reply", True) else 0)
    return max(60, room // 2 - 4)


_NUM = re.compile(r"\d+(?:\.\d+)?")


def _norm_numbers(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"(?<=\d),(?=\d{3})", "", text)


def unsupported_numbers(text: str, material: str, now: datetime) -> list[str]:
    """本文に出てくる2桁以上の数字のうち、材料に無いもの（書き間違い・作り話の疑い）。"""
    allowed = set(_NUM.findall(_norm_numbers(material)))
    allowed |= {str(now.year), str(now.month), str(now.day)}
    out = []
    for n in _NUM.findall(_norm_numbers(text)):
        if len(n.replace(".", "")) < 2 or n in allowed or n in out:
            continue
        out.append(n)
    return out


def _call_writer(client, kcfg: dict, prompt: str, schema: dict) -> dict | None:
    model = kcfg.get("writer_model", "claude-opus-5-5")
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=WRITER_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": kcfg.get("writer_effort", "medium"),
                           "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except Exception as e:
        logger.warning("発信案の作成に失敗しました error=%s", e)
        return None
    summarize._record_usage(model, response)
    if response.stop_reason != "end_turn":
        logger.warning("発信案の作成が途中で止まりました stop_reason=%s", response.stop_reason)
        return None
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("発信案の形式が読めませんでした")
        return None


def _problems(post: dict, kcfg: dict, extra_text: str = "") -> list[str]:
    problems = []
    over = x_weight(compose_x(post, kcfg, [])) - kcfg.get("x_max_weighted", 280)
    if over > 0:
        problems.append(f"X本文が長すぎます（約{over // 2 + 1}字オーバー）。全角{_x_max_chars(kcfg)}字以内に必ず縮めてください。")
    hype = HYPE_WORDS.findall(post["x_post"] + extra_text)
    if hype:
        problems.append(f"煽る表現（{'・'.join(sorted(set(hype)))}）を使わずに書き直してください。")
    return problems


def _write_with_retry(client, kcfg: dict, prompt: str, schema: dict, extra_text_of) -> dict | None:
    """書かせて、文字数オーバーや煽り表現があれば1回だけ書き直させる。"""
    post, feedback = None, ""
    for _ in range(2):
        result = _call_writer(client, kcfg, prompt + feedback, schema)
        if result is None:
            return post
        post = result
        problems = _problems(post, kcfg, extra_text_of(post))
        if not problems:
            return post
        logger.info("発信案を書き直します: %s", " / ".join(problems))
        feedback = "\n\n## 前回の案への修正指示\n" + "\n".join(problems) + f"\n前回のX本文:\n{post['x_post']}"
    return post


def _history_text(history: list[dict]) -> str:
    return "\n".join(f"- {h['date']} {h['headline']}: {(h['x_post'] or '')[:80]}" for h in history) or "（なし）"


def write_post(client, cfg: dict, plan: dict, material: str, now: datetime, history: list[dict]) -> dict | None:
    """1つの話題・定番ネタから、X本文と長文版（コミュニティ向け）を書く。"""
    kcfg = _kcfg(cfg)
    guide = KIND_GUIDE.get(plan["kind"]) or plan.get("angle") or KIND_GUIDE["news"]
    prompt = (
        f"今日: {now.year}年{now.month}月{now.day}日（{WEEKDAY_JA[now.weekday()]}）\n"
        f"今日の枠: {THEME_LABELS.get(plan['kind'], plan['kind'])}"
        + (f" ／ ネタ: {plan['title']}" if plan.get("title") else "") + "\n"
        f"書き方: {guide}\n"
        f"X本文の長さ: 全角{_x_max_chars(kcfg)}字以内（改行も1字に数える）。1行目は思わず読みたくなる短い見出しにする。\n"
        f"長文版: 300〜500字。見出し1行、要点2〜4つ、最後に一言。LINEのオープンチャットやTelegramにそのまま貼れる形。\n"
        f"{kcfg.get('tone', '')}\n\n"
        f"最近の投稿（同じ言い回し・同じ切り口を避ける）:\n{_history_text(history)}\n\n"
        f"## 材料\n{material}"
    )
    return _write_with_retry(client, kcfg, prompt, WRITER_SCHEMA, lambda p: p["long_post"])


def write_digest(client, cfg: dict, stories: list[dict], material: str, now: datetime,
                 history: list[dict]) -> dict | None:
    """今日のニュースまとめ（コミュニティ向け）と、その中の1本のX本文を書く。"""
    kcfg = _kcfg(cfg)
    prompt = (
        f"今日: {now.year}年{now.month}月{now.day}日（{WEEKDAY_JA[now.weekday()]}）\n"
        f"作るもの: Kaiaコミュニティ（LINEオープンチャット・Telegram・Discord）に毎朝届ける「Kaia・JPYCニュースまとめ」と、"
        f"その中から1本選んだXの投稿。\n"
        f"まとめ: 材料の話題{len(stories)}件すべてについて、id・見出し・何が起きたか・それで何が変わるかを書く。"
        f"見出しと要約だけで内容が分かるようにする（リンクを開かない人が多い）。\n"
        f"X本文: 全角{_x_max_chars(kcfg)}字以内（改行も1字に数える）。1行目は思わず読みたくなる短い見出しにする。\n"
        f"{kcfg.get('tone', '')}\n\n"
        f"最近のX投稿（同じ言い回しを避ける）:\n{_history_text(history)}\n\n"
        f"## 材料\n{material}"
    )
    return _write_with_retry(client, kcfg, prompt, DIGEST_SCHEMA,
                             lambda p: " ".join(i["summary"] + i["point"] for i in p["items"]))


def compose_community(digest: dict, stories: list[dict], kcfg: dict, now: datetime) -> str:
    """まとめ本文を組み立てる。出典名とURLはモデルではなくプログラムが入れる。"""
    by_id = {i["id"]: i for i in digest["items"]}
    lines = [f"📰 Kaia・JPYCニュース｜{now.month}/{now.day}（{WEEKDAY_JA[now.weekday()]}）"]
    if digest.get("intro", "").strip():
        lines.append(digest["intro"].strip())
    current_topic, number = None, 0
    for n, g in enumerate(stories, 1):
        item = by_id.get(n)
        if not item or not item["summary"].strip():
            continue
        if g["topic"] != current_topic:
            current_topic = g["topic"]
            lines += ["", f"【{dict(TOPIC_SECTIONS)[current_topic]}】"]
        rep = g["items"][0]
        mark = NUMBER_EMOJI[number] if number < len(NUMBER_EMOJI) else "▶"
        number += 1
        lines.append(f"{mark} {item['title'].strip()}")
        lines.append(item["summary"].strip())
        if item["point"].strip():
            lines.append(f"👉 {item['point'].strip()}")
        lines.append(f"🔗 {rep['source']}｜{_kind_label(rep['kind'])}")
        lines.append(rep["url"])
    if digest.get("closing", "").strip():
        lines += ["", digest["closing"].strip()]
    if kcfg.get("community_footer"):
        lines += ["", kcfg["community_footer"]]
    return "\n".join(lines)


def compose_community_single(post: dict, plan: dict, kcfg: dict, now: datetime) -> str:
    """ニュースが無い日のコミュニティ向け（定番ネタの長文版）。"""
    label = "🗓 先週のKaia・JPYCまとめ" if plan["kind"] == "recap" else "💡 今日のKaia豆知識"
    lines = [f"{label}｜{now.month}/{now.day}（{WEEKDAY_JA[now.weekday()]}）", "", post["long_post"].strip()]
    if kcfg.get("community_footer"):
        lines += ["", kcfg["community_footer"]]
    return "\n".join(lines)


# ---------------------------------------------------------------- 確かめる・届ける

def check_texts(x_text: str, community_text: str, kcfg: dict, material: str, now: datetime) -> list[str]:
    warnings = []
    weight = x_weight(x_text)
    if weight > kcfg.get("x_max_weighted", 280):
        warnings.append(f"X本文の文字数オーバー（{weight}/{kcfg.get('x_max_weighted', 280)}）。少し削ってください")
    # URLの中の数字は見ない
    body = re.sub(r"https?://\S+", " ", f"{x_text}\n{community_text}")
    nums = unsupported_numbers(body, material, now)
    if nums:
        warnings.append("材料に見当たらない数字: " + "、".join(nums) + "（出典で確認してください）")
    hype = HYPE_WORDS.findall(body)
    if hype:
        warnings.append("煽りに見える表現: " + "、".join(sorted(set(hype))))
    return warnings


def _split_message(text: str, limit: int = 1900) -> list[str]:
    """Discordの1通の上限に収まるよう、話題の切れ目（空行）で分ける。"""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = block[:limit]
    if current:
        chunks.append(current)
    return chunks


def deliver(cfg: dict, plan: dict, headline: str, community_text: str, x_text: str, reply_text: str,
            urls: list[str], warnings: list[str], caution_list: list[dict], now: datetime, dry_run: bool) -> bool:
    kcfg = _kcfg(cfg)
    webhook = os.environ.get(kcfg.get("webhook_env", "WEBHOOK_KAIA"), "").strip()
    if not webhook and not dry_run:
        logger.warning("%s が未設定のため発信案を届けられません", kcfg.get("webhook_env", "WEBHOOK_KAIA"))
        return False
    username = kcfg.get("username", "Kaia 発信係")

    head = [f"📣 **{now.month}/{now.day}（{WEEKDAY_JA[now.weekday()]}）のKaia・JPYC発信案**　"
            f"{THEME_LABELS.get(plan['kind'], plan['kind'])}",
            f"**{headline}**",
            f"選んだ理由: {plan['reason']}"]
    intent = "https://x.com/intent/post?text=" + quote(x_text, safe="")
    if len(intent) < 1500:
        head.append(f"👉 [ワンタップでXの投稿画面を開く]({intent})")
    head.append("✅ 点検: 問題なし" if not warnings else "⚠️ 点検:\n" + "\n".join(f"- {w}" for w in warnings))
    if caution_list:
        head.append("🔎 知っておきたいニュース（発信案には入れていません）:\n"
                    + "\n".join(f"- {g['ja_title']} <{g['items'][0]['url']}>" for g in caution_list[:3]))
    head.append("-# 下に ①コミュニティ向け ②X本文 ③リプ欄用 が届きます。長押し→「テキストをコピー」で使えます")

    messages: list[tuple[str, str | None]] = [("\n".join(head)[:1990], None)]
    parts = _split_message(community_text)
    for n, part in enumerate(parts, 1):
        suffix = f"（{n}/{len(parts)}）" if len(parts) > 1 else ""
        messages.append((part, f"① コミュニティ向け{suffix}（LINE・Telegram・Discordにそのまま貼れます）"))
    messages.append((x_text, "② X本文（このまま投稿できます）"))
    if reply_text.strip():
        messages.append((reply_text, "③ リプ欄用（リンクはリプに置くと表示されやすくなります）"))

    ok = True
    for content, label in messages:
        embeds = [{"description": label, "color": 0xBFF009}] if label else None
        ok &= discord.post_webhook(webhook or "dry-run", username=username, content=content,
                                   embeds=embeds, dry_run=dry_run)
    return ok


# ---------------------------------------------------------------- 全体

def build_plan(conn, cfg: dict, now: datetime, theme: str | None = None) -> tuple[dict, str, list[str]] | None:
    """X用1本の枠と材料を決める（ニュースまとめが無い日・試し実行用）。材料がそろわなければ次の候補へ。"""
    forced = theme not in (None, "auto", "news")
    theme = theme or theme_for(cfg, now)

    # 月曜のまとめ以外は、大事なニュースがあればそれを優先する（枠を指定した場合は除く）
    if not forced and theme != "recap":
        story = pick_news(conn, cfg, now)
        if story is not None:
            material, urls = news_material(story)
            if len(material) >= 300:
                kinds = "Kaia公式の発表" if story["official"] else _kind_label(story["items"][0]["kind"])
                reason = (f"{_ja_date(story['latest_at'])}の{kinds}（{story['items'][0]['source']}ほか、"
                          f"{story['sources']}媒体）・大事さ{story['importance']}/5"
                          + ("・日本に関係" if story["japan"] else ""))
                # 定番ネタと同じ記事なら、そのネタも使ったことにする（二重に書かない）
                item_urls = {i["url"] for i in story["items"]}
                same_topic = next((t["id"] for t in load_topics() if item_urls & set(t.get("urls", []))), None)
                return ({"kind": "news", "story": story["story"], "topic_id": same_topic, "reason": reason,
                         "title": story["ja_title"]}, material, urls)
            logger.info("ニュースの材料が足りないため定番テーマに切り替えます story=%s", story["story"])
        if theme in ("news", "auto"):
            theme = theme_for(cfg, now)

    if theme == "recap":
        stories = recap_stories(conn, now)
        if len(stories) >= 2:
            material, urls = recap_material(stories)
            return ({"kind": "recap", "stories": [g["story"] for g in stories],
                     "reason": f"月曜は先週のまとめ（直近7日の話題{len(stories)}件）"}, material, urls)
        theme = "basics"

    if theme == "data":
        material, urls = data_material(now)
        if material:
            return ({"kind": "data", "topic_id": "chain-data", "reason": "水曜は数字で見るKaia（DefiLlamaの最新値）"},
                    material, urls)
        theme = "basics"

    order = [theme] + [t for t in ("basics", "tech", "ecosystem", "howto", "japan") if t != theme]
    for th in order:
        for topic in pick_topics(conn, th):
            material, urls = topic_material(topic)
            if material:
                reason = f"{THEME_LABELS.get(th, th)}の定番ネタ（しばらく使っていないもの）"
                if not forced:
                    reason = f"今日は新しいニュースが無いので、{WEEKDAY_JA[now.weekday()]}曜の枠「{reason}」"
                return ({"kind": th, "topic_id": topic["id"], "title": topic["title"], "angle": topic["angle"],
                         "reason": reason}, material, urls)
            logger.info("定番ネタの材料が取れませんでした topic=%s", topic["id"])
    return None


def should_run(conn, cfg: dict, now: datetime) -> bool:
    kcfg = _kcfg(cfg)
    if not kcfg.get("enabled"):
        return False
    if not os.environ.get(kcfg.get("webhook_env", "WEBHOOK_KAIA"), "").strip():
        return False
    hour = kcfg.get("hour_jst", 7)
    if not (hour <= now.hour < hour + kcfg.get("window_hours", 6)):
        return False
    ensure_schema(conn)
    return db.get_state(conn, "kaia_last_date") != now.date().isoformat()


def run(conn, cfg: dict, dry_run: bool = False, theme: str | None = None, now: datetime | None = None) -> bool:
    """1日分の発信案（コミュニティ向けまとめ＋X用1本）を作って届ける。成功したら True。

    theme を指定すると（試し実行）、まとめは作らずその枠でX用1本と長文版を作る。
    """
    kcfg = _kcfg(cfg)
    now = now or now_jst()
    today = now.date().isoformat()
    ensure_schema(conn)

    client = summarize._build_client(cfg)
    if client is None:
        logger.warning("Claude APIが使えないため、発信案は作れません")
        return False

    try:
        collect(conn, cfg, now)
        triage(conn, cfg, client, now)
        history = [dict(r) for r in conn.execute(
            "SELECT date, headline, x_post FROM kaia_posts ORDER BY date DESC LIMIT 7")]

        stories = digest_stories(conn, cfg, now) if theme in (None, "auto", "digest") else []
        if stories:
            material = digest_material(stories)
            digest = write_digest(client, cfg, stories, material, now, history)
            if digest is None:
                raise RuntimeError("ニュースまとめを書けませんでした")
            x_index = digest.get("x_item_id", 1)
            x_story = stories[x_index - 1] if 1 <= x_index <= len(stories) else stories[0]
            n_by_topic = {}
            for g in stories:
                n_by_topic[g["topic"]] = n_by_topic.get(g["topic"], 0) + 1
            breakdown = "・".join(f"{dict(TOPIC_SECTIONS)[t]} {c}件" for t, c in n_by_topic.items())
            plan = {"kind": "digest", "digest": [g["story"] for g in stories], "story": x_story["story"],
                    "reason": f"まだ届けていない新しい話題 {len(stories)}件（{breakdown}）。Xはその中から1本"}
            community = compose_community(digest, stories, kcfg, now)
            urls = [x_story["items"][0]["url"]]
            post = digest
        else:
            if theme in (None, "auto", "digest"):
                theme = theme_for(cfg, now)  # 新しいニュースが無い日は曜日の定番ネタ（豆知識）
            built = build_plan(conn, cfg, now, theme)
            if built is None:
                raise RuntimeError("材料がそろいませんでした")
            plan, material, urls = built
            post = write_post(client, cfg, plan, material, now, history)
            if post is None:
                raise RuntimeError("発信案を書けませんでした")
            community = compose_community_single(post, plan, kcfg, now)
        logger.info("発信案: 枠=%s 理由=%s", plan["kind"], plan["reason"])

        x_text = compose_x(post, kcfg, urls)
        reply_text = post["x_reply"].strip()
        if urls and kcfg.get("link_in_reply", True):
            reply_text = f"{reply_text}\n{urls[0]}"
        warnings = check_texts(x_text, community, kcfg, material, now)
        ok = deliver(cfg, plan, post["headline"], community, x_text, reply_text, urls, warnings,
                     cautions(conn, now), now, dry_run)
        if not ok:
            raise RuntimeError("Discordへ届けられませんでした")
    except Exception as e:
        logger.exception("発信案づくりに失敗しました")
        if not dry_run:
            _record_failure(conn, cfg, now, str(e))
        return False

    if not dry_run:
        for s in plan.get("digest", []):
            conn.execute("UPDATE kaia_items SET digest_on=? WHERE story=? AND digest_on IS NULL", (today, s))
        x_stories = plan.get("stories") or ([plan["story"]] if plan.get("story") else [])
        for s in x_stories:
            conn.execute("UPDATE kaia_items SET used_on=? WHERE story=? AND used_on IS NULL", (today, s))
        conn.execute(
            "INSERT OR REPLACE INTO kaia_posts(date, kind, topic_id, story, headline, x_post, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (today, plan["kind"], plan.get("topic_id"), ",".join(x_stories) or None, post["headline"],
             post["x_post"], datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        db.set_state(conn, "kaia_last_date", today)
        prune(conn)
    return True


def _record_failure(conn, cfg: dict, now: datetime, reason: str) -> None:
    """失敗は30分後にやり直す。3回失敗した日はあきらめて一言だけ知らせる。"""
    kcfg = _kcfg(cfg)
    today = now.date().isoformat()
    prev = db.get_state(conn, "kaia_attempts") or ""
    count = int(prev.split(":")[1]) + 1 if prev.startswith(today + ":") else 1
    db.set_state(conn, "kaia_attempts", f"{today}:{count}")
    if count >= kcfg.get("max_attempts", 3):
        db.set_state(conn, "kaia_last_date", today)
        webhook = os.environ.get(kcfg.get("webhook_env", "WEBHOOK_KAIA"), "").strip()
        if webhook:
            discord.post_webhook(webhook, username=kcfg.get("username", "Kaia 発信係"),
                                 content=f"今日（{now.month}/{now.day}）の発信案は作れませんでした。理由: {reason[:200]}")


def prune(conn, keep_days: int = 60) -> None:
    since = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    conn.execute("DELETE FROM kaia_items WHERE first_seen < ?", (since,))
    conn.execute("DELETE FROM kaia_posts WHERE date < ?", (since[:10],))
    conn.commit()
