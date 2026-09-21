"""日次まとめの投稿見送り判定を検証する。

- ニュースが無い日は全銘柄とも投稿しない（ARBも例外ではない）
- ETH・SOLは「読む価値: 中」以上の記事が無ければ投稿しない
- ARB・WLDは記事が1件でもあれば投稿する
"""
from datetime import datetime, timedelta, timezone

from src import db
from src.digest import run_digest_for_coin
from src.utils import JST

PRICE_DATA = {
    "ARB": {"usd": 0.16, "jpy": 25.0, "usd_24h_change": 1.0},
    "ETH": {"usd": 2400.0, "jpy": 375000.0, "usd_24h_change": 1.0},
    "SOL": {"usd": 97.0, "jpy": 15000.0, "usd_24h_change": 1.0},
    "WLD": {"usd": 0.36, "jpy": 56.0, "usd_24h_change": 1.0},
}


def _run(conn, cfg, coin):
    return run_digest_for_coin(
        conn=conn, coin=coin, coin_cfg=cfg["coins"][coin], cfg=cfg,
        webhook_url="https://discord.example/webhook", price_data=PRICE_DATA,
        btc_change_24h=0.8, onchain_field=None, dry_run=True,
    )


def _yesterday_article(conn, coin, *, sources=1, critical=False):
    """「前日」のJST日付に入る時刻で記事を作る。sources を増やすと読む価値が上がる。"""
    published = (datetime.now(JST) - timedelta(days=1)).replace(hour=12).astimezone(timezone.utc)
    article_id = db.insert_article(
        conn, normalized_title=f"{coin} news {sources} {critical}",
        display_title=f"{coin}のニュース", url=f"https://example.com/{coin}/{sources}",
        first_source="媒体1", first_published_at=published.isoformat(), score=0,
        sentiment="neutral", emoji="📰", is_critical=critical,
        critical_hits=["hack"] if critical else [], coins=[coin],
    )
    for i in range(2, sources + 1):
        db.add_source_to_article(conn, article_id, f"媒体{i}", f"https://example.com/{coin}/{i}", published.isoformat())
    return article_id


def test_no_coin_is_posted_without_news(conn, cfg):
    """ニュースが無い日は、ARBを含む全銘柄が見送り。"""
    for coin in ("ARB", "ETH", "SOL", "WLD"):
        assert _run(conn, cfg, coin) == "skipped", coin


def test_always_post_is_empty_by_default(cfg):
    assert cfg["digest"]["always_post"] == []
    assert cfg["digest"]["minimum_priority_to_post"] == {"ETH": "中", "SOL": "中"}


def test_arb_posts_with_any_article_even_low_value(conn, cfg):
    _yesterday_article(conn, "ARB", sources=1)   # 1媒体・中立 = 読む価値「低」
    assert _run(conn, cfg, "ARB") == "posted"


def test_eth_skips_when_only_low_value_articles(conn, cfg):
    _yesterday_article(conn, "ETH", sources=1)   # 読む価値「低」
    assert _run(conn, cfg, "ETH") == "skipped"


def test_eth_posts_when_a_medium_value_article_exists(conn, cfg):
    _yesterday_article(conn, "ETH", sources=1)   # 低
    _yesterday_article(conn, "ETH", sources=3)   # 3媒体 = 中
    assert _run(conn, cfg, "ETH") == "posted"


def test_sol_uses_the_same_rule_as_eth(conn, cfg):
    _yesterday_article(conn, "SOL", sources=1)
    assert _run(conn, cfg, "SOL") == "skipped"
    _yesterday_article(conn, "SOL", critical=True, sources=2)  # 重大ワード+2媒体 = 高
    assert _run(conn, cfg, "SOL") == "posted"
