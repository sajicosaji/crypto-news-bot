"""ニュースが無い日の投稿見送りと、ARBの毎日投稿を検証する。"""
from src.digest import run_digest_for_coin

PRICE_DATA = {"ARB": {"usd": 0.16, "jpy": 25.0, "usd_24h_change": 1.0},
              "ETH": {"usd": 2400.0, "jpy": 375000.0, "usd_24h_change": 1.0}}


def _run(conn, cfg, coin):
    return run_digest_for_coin(
        conn=conn, coin=coin, coin_cfg=cfg["coins"][coin], cfg=cfg,
        webhook_url="https://discord.example/webhook", price_data=PRICE_DATA,
        btc_change_24h=0.8, onchain_field=None, dry_run=True,
    )


def test_skips_digest_when_no_news(conn, cfg):
    """静かな日に空の投稿を並べない。"""
    assert _run(conn, cfg, "ETH") == "skipped"


def test_arb_is_posted_every_day_even_without_news(conn, cfg):
    """ARBだけはニュースが無くても毎日届く（価格・オンチェーンデータのため）。"""
    assert _run(conn, cfg, "ARB") == "posted"


def test_always_post_list_comes_from_config(cfg):
    assert cfg["digest"]["always_post"] == ["ARB"]
    assert cfg["digest"]["skip_when_no_news"] is True


def test_digest_is_posted_when_news_exists(conn, cfg):
    from datetime import datetime, timedelta, timezone

    from src import db
    from src.utils import JST

    # 「前日」のJST日付に入る時刻で記事を作る
    yesterday_jst = datetime.now(JST) - timedelta(days=1)
    published = yesterday_jst.replace(hour=12).astimezone(timezone.utc)
    db.insert_article(
        conn, normalized_title="eth news", display_title="ETHのニュース",
        url="https://example.com/1", first_source="テスト媒体",
        first_published_at=published.isoformat(), score=1, sentiment="good",
        emoji="🚀", is_critical=False, critical_hits=[], coins=["ETH"],
    )
    assert _run(conn, cfg, "ETH") == "posted"
