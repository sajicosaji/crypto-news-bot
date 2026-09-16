from datetime import datetime, timedelta, timezone

from src import db
from src.alerts import run_alert_for_coin
from src.fetch_news import NewsItem, persist_news_items


def _make_article(conn, *, title, hours_ago, critical_hits, coins=("ARB",)):
    published_at = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    return db.insert_article(
        conn,
        normalized_title=title.lower(),
        display_title=title,
        url=f"https://example.com/{title}",
        first_source="Test Media",
        first_published_at=published_at,
        score=-1,
        sentiment="bad",
        emoji="📉",
        is_critical=bool(critical_hits),
        critical_hits=critical_hits,
        coins=list(coins),
    )


def test_old_article_first_seen_today_does_not_trigger_alert(conn, cfg):
    coin_cfg = cfg["coins"]["ARB"]
    recent_id = _make_article(conn, title="Arbitrum hack drains funds recently", hours_ago=0.5, critical_hits=["hack"])
    old_id = _make_article(conn, title="Arbitrum hack drains funds months ago", hours_ago=24 * 90, critical_hits=["hack"])

    price_data = {"ARB": {"usd": 0.16, "usd_24h_change": 1.0}}
    posted = run_alert_for_coin(
        conn=conn, coin="ARB", coin_cfg=coin_cfg, webhook_url="https://discord.example/webhook",
        cfg=cfg, touched_article_ids=[recent_id, old_id], price_data=price_data, btc_change_24h=0.5,
        hourly_change=None, onchain_summary=None, usd1_price=None, morpho_summary=None, dry_run=True,
    )

    assert posted == 1
    recent_row = conn.execute("SELECT alerted_at FROM articles WHERE id = ?", (recent_id,)).fetchone()
    old_row = conn.execute("SELECT alerted_at FROM articles WHERE id = ?", (old_id,)).fetchone()
    assert recent_row["alerted_at"] is not None
    assert old_row["alerted_at"] is None


def test_same_old_article_is_not_reinserted_as_duplicate_across_runs(conn, cfg):
    old_time = datetime.now(timezone.utc) - timedelta(days=90)
    item = NewsItem(
        title="Old Arbitrum story - CoinDesk",
        url="https://example.com/old-story",
        source="CoinDesk",
        published_at=old_time,
        coins=["ARB"],
    )
    results_run1 = persist_news_items(conn, [item], cfg)
    assert results_run1[0][1] is True  # 新規

    # 同じ記事が別媒体から再度取得された場合（4日以上経過後の別実行を想定）
    item2 = NewsItem(
        title="Old Arbitrum story - Cointelegraph",
        url="https://example.com/old-story-2",
        source="Cointelegraph",
        published_at=old_time,
        coins=["ARB"],
    )
    results_run2 = persist_news_items(conn, [item2], cfg)
    assert results_run2[0][1] is False  # 既存記事にマージされ、新規にはならない
    assert results_run2[0][0] == results_run1[0][0]

    count = conn.execute("SELECT COUNT(*) AS c FROM articles").fetchone()["c"]
    assert count == 1
