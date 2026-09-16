from datetime import datetime, timezone

from src.advice import PRIORITY_HIGH, PRIORITY_LOW, PRIORITY_MEDIUM, build_reading_advice
from src.digest import build_news_lines, sort_news_for_digest
from src.fetch_news import NewsItem, clean_excerpt, persist_news_items


# --- 抜粋の整形 -------------------------------------------------------------

def test_clean_excerpt_strips_html_and_boilerplate():
    raw = "<p>DAOが<b>買い戻し</b>を承認した。</p> The post Something first appeared on NADA NEWS."
    assert clean_excerpt(raw) == "DAOが買い戻しを承認した。"


def test_clean_excerpt_unescapes_entities_and_trailing_ellipsis():
    raw = "Web3ニュース&amp;解説をお届けします [&#8230;]"
    assert clean_excerpt(raw) == "Web3ニュース&解説をお届けします"


def test_clean_excerpt_truncates_to_max_length():
    raw = "あ" * 500
    result = clean_excerpt(raw, max_length=100)
    assert len(result) == 101  # 100文字 + 省略記号
    assert result.endswith("…")


def test_clean_excerpt_handles_empty():
    assert clean_excerpt(None) == ""
    assert clean_excerpt("") == ""


# --- 読む価値アドバイス -----------------------------------------------------

def test_advice_high_when_critical_and_price_reacting(cfg):
    priority, text = build_reading_advice(
        critical_hits=["exploit"], source_count=1, score=-2,
        price_change_24h=-11.0, cfg=cfg,
    )
    assert priority == PRIORITY_HIGH
    assert "exploit" in text
    assert "-11.0%" in text


def test_advice_high_when_critical_and_multiple_media(cfg):
    priority, _ = build_reading_advice(
        critical_hits=["hack"], source_count=3, price_change_24h=0.5, cfg=cfg,
    )
    assert priority == PRIORITY_HIGH


def test_advice_medium_when_only_critical(cfg):
    priority, _ = build_reading_advice(
        critical_hits=["SEC"], source_count=1, price_change_24h=0.2, cfg=cfg,
    )
    assert priority == PRIORITY_MEDIUM


def test_advice_medium_for_dao_proposal(cfg):
    priority, text = build_reading_advice(
        critical_hits=[], source_count=1, is_dao_proposal=True, cfg=cfg,
    )
    assert priority == PRIORITY_MEDIUM
    assert "DAO" in text


def test_advice_low_for_ordinary_news(cfg):
    priority, text = build_reading_advice(
        critical_hits=[], source_count=1, score=0, price_change_24h=0.3, cfg=cfg,
    )
    assert priority == PRIORITY_LOW
    assert "流し読み" in text


# --- 並べ替えと表示 ---------------------------------------------------------

def _article(**kw):
    base = {
        "display_title": "T", "url": "https://e.com/1", "first_source": "S",
        "emoji": "📰", "is_critical": 0, "source_count": 1, "score": 0,
        "excerpt": "", "critical_hits": "", "alerted_at": None,
    }
    base.update(kw)
    return base


def test_sort_puts_high_priority_and_excerpt_first():
    low = _article(display_title="low", priority="低", source_count=1)
    high = _article(display_title="high", priority="高", source_count=1)
    mid_with_excerpt = _article(display_title="mid", priority="中", excerpt="本文あり")
    mid_without = _article(display_title="mid2", priority="中")

    ordered = sort_news_for_digest([low, mid_without, high, mid_with_excerpt])
    assert [a["display_title"] for a in ordered] == ["high", "mid", "mid2", "low"]


def test_build_news_lines_includes_excerpt_and_advice(cfg):
    a = _article(
        display_title="Arbitrum exploited", excerpt="ブリッジから2400万ドルが流出した。",
        priority="高", advice="読む価値: 高 — 重大ワード「exploit」を含む。まず最初に目を通すのがおすすめ",
    )
    lines = build_news_lines([a], cfg)
    body = "\n".join(lines)
    assert "ブリッジから2400万ドルが流出した。" in body
    assert "👉 読む価値: 高" in body
    assert "Arbitrum exploited" in body


def test_build_news_lines_respects_char_budget(cfg):
    articles = []
    for i in range(10):
        articles.append(_article(
            display_title=f"記事{i}" + "あ" * 200, excerpt="い" * 300,
            priority="中", advice="読む価値: 中 — テスト",
        ))
    lines = build_news_lines(articles, cfg, char_budget=800)
    assert sum(len(x) + 1 for x in lines) <= 800


def test_build_news_lines_marks_alerted_articles(cfg):
    a = _article(display_title="速報済み", alerted_at="2026-09-17T00:00:00", priority="中", advice="x")
    lines = build_news_lines([a], cfg)
    assert lines[0].startswith("🚨")


# --- 抜粋の補完 -------------------------------------------------------------

def test_excerpt_backfilled_when_same_story_found_with_excerpt(conn, cfg):
    now = datetime.now(timezone.utc)
    # 先にGoogle News経由（抜粋なし）で登録される
    google_item = NewsItem(
        title="Arbitrum DAO approves buyback - U.Today",
        url="https://news.google.com/rss/articles/abc",
        source="U.Today", published_at=now, coins=["ARB"],
    )
    persist_news_items(conn, [google_item], cfg)

    # 後から本文抜粋が取れる媒体で同じ記事が見つかる
    direct_item = NewsItem(
        title="Arbitrum DAO approves buyback - The Block",
        url="https://www.theblock.co/post/123",
        source="The Block", published_at=now, coins=["ARB"],
        excerpt="DAOは手数料収入の一部でARBを買い戻す提案を承認した。",
    )
    persist_news_items(conn, [direct_item], cfg)

    row = conn.execute("SELECT excerpt, excerpt_url, excerpt_source FROM articles").fetchone()
    assert row["excerpt"] == "DAOは手数料収入の一部でARBを買い戻す提案を承認した。"
    assert row["excerpt_url"] == "https://www.theblock.co/post/123"
    assert row["excerpt_source"] == "The Block"


def test_existing_excerpt_is_not_overwritten(conn, cfg):
    now = datetime.now(timezone.utc)
    first = NewsItem(
        title="Same story - The Block", url="https://theblock.co/1", source="The Block",
        published_at=now, coins=["ARB"], excerpt="最初の抜粋",
    )
    persist_news_items(conn, [first], cfg)
    second = NewsItem(
        title="Same story - CoinDesk", url="https://coindesk.com/1", source="CoinDesk",
        published_at=now, coins=["ARB"], excerpt="あとから来た抜粋",
    )
    persist_news_items(conn, [second], cfg)

    row = conn.execute("SELECT excerpt FROM articles").fetchone()
    assert row["excerpt"] == "最初の抜粋"
