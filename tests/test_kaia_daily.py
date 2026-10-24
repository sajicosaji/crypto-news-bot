from datetime import datetime, timedelta, timezone

from src import kaia_daily as k
from src.utils import JST

NOW = datetime(2026, 10, 24, 7, 0, tzinfo=JST)


def _kcfg():
    return {"hashtags": ["#Kaia", "#カイア"], "link_in_reply": True, "x_max_weighted": 280,
            "news_threshold": 8, "news_min_importance": 3, "news_max_age_days": 10,
            "weekday_themes": {0: "recap", 1: "howto", 2: "data", 3: "ecosystem", 4: "tech", 5: "japan", 6: "basics"}}


def _add(conn, url, *, title="Kaia news", kind="media", lang="en", hours_ago=10, story="s1",
         importance=4, sentiment="positive", price_talk=0, japan=0, used_on=None, source="Media"):
    pub = (NOW - timedelta(hours=hours_ago)).astimezone(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO kaia_items(url, source, kind, lang, title, summary, body, published_at, first_seen, triaged, "
        "relevant, story, ja_title, importance, sentiment, price_talk, japan, used_on) "
        "VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, 1, 1, ?, ?, ?, ?, ?, ?, ?)",
        (url, source, kind, lang, title, "x" * 500, pub, pub, story, title, importance, sentiment,
         price_talk, japan, used_on),
    )
    conn.commit()


def test_x_weight_counts_japanese_as_two():
    assert k.x_weight("abc") == 3
    assert k.x_weight("カイア") == 6
    assert k.x_weight("見て https://example.com/very/long/path") == 4 + 1 + 23
    # 絵文字の異体字セレクタは数えない
    assert k.x_weight("❤️") == 2


def test_compose_x_fits_limit_with_hashtags():
    post = {"x_post": "あ" * 130}
    text = k.compose_x(post, _kcfg(), ["https://example.com"])
    assert text.endswith("#Kaia #カイア")
    assert "https://" not in text  # リンクはリプ欄に回す
    assert k.x_weight(text) <= 280


def test_unsupported_numbers_flags_made_up_figures():
    material = "流通額は3.3億円を突破。2026年10月14日に発表。参加者は1,360人。"
    text = "3.3億円を突破し、参加者は1360人に。10月14日に発表、利用者は25万人です。"
    assert k.unsupported_numbers(text, material, NOW) == ["25"]


def test_hype_words_detected():
    assert k.HYPE_WORDS.search("今が買い時です")
    assert not k.HYPE_WORDS.search("秘密鍵は絶対に人に教えないでください")


def test_pick_news_prefers_fresh_important_and_skips_bad_ones(conn):
    k.ensure_schema(conn)
    cfg = {"kaia_daily": _kcfg()}
    _add(conn, "https://a", story="minor", importance=2, hours_ago=5)
    _add(conn, "https://b", story="hack", importance=5, sentiment="negative", hours_ago=5)
    _add(conn, "https://c", story="price", importance=5, price_talk=1, hours_ago=5)
    _add(conn, "https://d", story="used", importance=5, hours_ago=5, used_on="2026-10-23")
    _add(conn, "https://e", story="partner", importance=4, japan=1, kind="official", hours_ago=20)
    _add(conn, "https://f", story="old", importance=5, hours_ago=24 * 12)
    story = k.pick_news(conn, cfg, NOW)
    assert story is not None and story["story"] == "partner"
    # 悪いニュースは投稿案にせず「知っておきたいニュース」として出す
    assert [g["story"] for g in k.cautions(conn, NOW)] == ["hack"]


def test_pick_news_returns_none_when_nothing_strong(conn):
    k.ensure_schema(conn)
    _add(conn, "https://a", story="weak", importance=3, hours_ago=24 * 6)  # 6 + 1 = 7 < 8
    assert k.pick_news(conn, {"kaia_daily": _kcfg()}, NOW) is None


def test_topics_rotate_least_recently_used(conn):
    k.ensure_schema(conn)
    topics = k.pick_topics(conn, "tech")
    assert topics, "定番ネタが読み込めること"
    first = topics[0]["id"]
    conn.execute("INSERT INTO kaia_posts(date, kind, topic_id, created_at) VALUES ('2026-10-20', 'tech', ?, 'x')", (first,))
    assert k.pick_topics(conn, "tech")[0]["id"] != first
    assert k.pick_topics(conn, "tech")[-1]["id"] == first


def test_every_topic_has_required_fields():
    themes = set()
    for t in k.load_topics():
        assert t["id"] and t["title"] and t["angle"] and t["urls"]
        assert t["theme"] in k.THEME_LABELS
        themes.add(t["theme"])
    assert {"howto", "ecosystem", "tech", "japan", "basics"} <= themes


def test_should_run_needs_webhook_and_time(conn, monkeypatch):
    cfg = {"kaia_daily": {**_kcfg(), "enabled": True, "hour_jst": 7, "window_hours": 6}}
    monkeypatch.delenv("WEBHOOK_KAIA", raising=False)
    assert not k.should_run(conn, cfg, NOW)
    monkeypatch.setenv("WEBHOOK_KAIA", "https://discord.example/webhook")
    assert k.should_run(conn, cfg, NOW)
    assert not k.should_run(conn, cfg, NOW.replace(hour=6))
    from src import db
    db.set_state(conn, "kaia_last_date", NOW.date().isoformat())
    assert not k.should_run(conn, cfg, NOW)
