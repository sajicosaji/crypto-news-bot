from datetime import datetime, timezone

from src.fetch_news import NewsItem, persist_news_items
from src.utils import normalize_title


def test_normalize_title_strips_source_suffix():
    a = normalize_title("Arbitrum Announces New Partnership - CoinDesk")
    b = normalize_title("Arbitrum announces new partnership - Cointelegraph")
    assert a == b


def test_normalize_title_differs_for_different_stories():
    a = normalize_title("Arbitrum announces new partnership - CoinDesk")
    b = normalize_title("Arbitrum suffers sequencer outage - The Block")
    assert a != b


def test_persist_merges_duplicate_articles_across_sources(conn, cfg):
    now = datetime.now(timezone.utc)
    items = [
        NewsItem(
            title="Arbitrum announces new partnership - CoinDesk",
            url="https://coindesk.example/1",
            source="CoinDesk",
            published_at=now,
            coins=["ARB"],
        ),
        NewsItem(
            title="Arbitrum announces new partnership - Cointelegraph",
            url="https://cointelegraph.example/1",
            source="Cointelegraph",
            published_at=now,
            coins=["ARB"],
        ),
    ]
    results = persist_news_items(conn, items, cfg)
    assert len(results) == 2
    article_ids = {aid for aid, _ in results}
    assert len(article_ids) == 1, "同一記事は1件にまとめられるべき"

    is_new_flags = [is_new for _, is_new in results]
    assert is_new_flags == [True, False]

    from src import db

    article_id = next(iter(article_ids))
    assert db.count_sources(conn, article_id) == 2


def test_persist_unions_coin_associations_for_multi_coin_article(conn, cfg):
    now = datetime.now(timezone.utc)
    items = [
        NewsItem(
            title="Coinbase lists ETH and ARB pairs - CoinDesk",
            url="https://coindesk.example/2",
            source="CoinDesk",
            published_at=now,
            coins=["ETH"],
        ),
        NewsItem(
            title="Coinbase lists ETH and ARB pairs - The Block",
            url="https://theblock.example/2",
            source="The Block",
            published_at=now,
            coins=["ARB"],
        ),
    ]
    results = persist_news_items(conn, items, cfg)
    article_id = results[0][0]
    rows = conn.execute(
        "SELECT coin FROM article_coins WHERE article_id = ?", (article_id,)
    ).fetchall()
    coins = {r["coin"] for r in rows}
    assert coins == {"ETH", "ARB"}


# --- 言い回し違いの同一ニュースのまとめ ---------------------------------------

def test_is_same_story_merges_rewritten_headlines():
    from src.utils import is_same_story, normalize_title

    a = normalize_title("Standard Chartered says Arbitrum could outperform Bitcoin, Ether through 2030")
    b = normalize_title("Standard Chartered Forecasted ARB to Rise to $10 by 2030")
    assert is_same_story(a, b) is True


def test_is_same_story_keeps_different_stories_apart():
    from src.utils import is_same_story, normalize_title

    pairs = [
        ("Arbitrum bridge exploited for $24M in USDC", "Arbitrum DAO approves buyback and burn proposal"),
        ("Ethereum upgrade goes live on mainnet", "Ethereum upgrade delayed until next quarter"),
        ("Solana network hit by outage", "Solana DEX volume hits record high"),
    ]
    for a, b in pairs:
        assert is_same_story(normalize_title(a), normalize_title(b)) is False, f"{a} / {b}"


def test_generic_coin_names_alone_do_not_merge_stories():
    from src.utils import is_same_story, normalize_title

    a = normalize_title("Bitcoin and Ethereum prices climb today")
    b = normalize_title("Bitcoin and Ethereum prices fall today")
    assert is_same_story(a, b) is False


def test_persist_merges_rewritten_headline_into_one_article(conn, cfg):
    now = datetime.now(timezone.utc)
    items = [
        NewsItem(
            title="Standard Chartered says Arbitrum could outperform Bitcoin, Ether through 2030",
            url="https://a.example/1", source="Cointelegraph", published_at=now, coins=["ARB"],
        ),
        NewsItem(
            title="Standard Chartered Forecasted ARB to Rise to $10 by 2030",
            url="https://b.example/2", source="incrypted", published_at=now, coins=["ARB"],
        ),
    ]
    results = persist_news_items(conn, items, cfg)
    assert len({aid for aid, _ in results}) == 1
    assert conn.execute("SELECT COUNT(*) AS c FROM articles").fetchone()["c"] == 1
