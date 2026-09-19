"""同じ話題が別見出しで並ぶのを防ぐ処理の検証。

DBの統合(min_shared_terms=3)は保守的にしてあり、言い換え記事が別レコードとして
残ることがある。それをそのまま出すと「同じ話題が2件並ぶ」ので、表示・通知の段階で
ゆるいしきい値(display_min_shared_terms=2)で弾く。
"""
from datetime import datetime, timedelta, timezone

from src import db
from src.alerts import _already_alerted_similar
from src.digest import drop_similar_for_display
from src.utils import normalize_title


def _a(title, **kw):
    article = {
        "display_title": title,
        "normalized_title": normalize_title(title),
        "url": f"https://example.com/{abs(hash(title))}",
        "emoji": "📰",
        "sentiment": "neutral",
        "is_critical": 0,
        "source_count": 1,
        "excerpt": "",
        "first_source": "テスト媒体",
        "alerted_at": None,
    }
    article.update(kw)
    return article


# --- 実際に重複していた組み合わせ --------------------------------------------

def test_drops_rewritten_headline_of_same_arbitrum_story():
    """実データで並んでいた2件。同じDAO提案の続報で、言い回しだけが違う。"""
    articles = [
        _a("Arbitrum Watchdog Seeks Permanent Bans For Three Grant Recipients"),
        _a("Arbitrum Proposal Seeks To Exclude Three DeFi Projects From Future Grants"),
    ]
    kept = drop_similar_for_display(articles, 2)
    assert len(kept) == 1
    assert kept[0]["display_title"].startswith("Arbitrum Watchdog")


def test_keeps_the_first_one_which_is_the_higher_ranked_article():
    """並べ替え済みの順で先に来たもの（＝読む価値の高い方）を残す。"""
    articles = [
        _a("Standard Chartered Sets $10 Arbitrum Target", source_count=8),
        _a("Standard Chartered Forecasted ARB to Rise to $10", source_count=1),
    ]
    kept = drop_similar_for_display(articles, 2)
    assert kept[0]["source_count"] == 8


# --- 別の話題まで消さないこと（こちらの方が重要） ------------------------------

def test_does_not_drop_genuinely_different_stories():
    articles = [
        _a("Arbitrum bridge exploited for $24M in USDC"),
        _a("Arbitrum DAO approves buyback and burn proposal"),
        _a("Ethereum upgrade goes live on mainnet"),
        _a("Ethereum upgrade delayed until next quarter"),
        _a("Solana network hit by outage"),
        _a("Solana DEX volume hits record high"),
    ]
    kept = drop_similar_for_display(articles, 2)
    assert len(kept) == len(articles), "別の話題は1件も消えてはいけない"


def test_coin_name_alone_does_not_merge():
    articles = [
        _a("Bitcoin and Ethereum prices climb today"),
        _a("Bitcoin and Ethereum prices fall today"),
    ]
    assert len(drop_similar_for_display(articles, 2)) == 2


def test_disabled_when_threshold_is_zero():
    articles = [_a("同じ話題A seeks three"), _a("同じ話題B seeks three")]
    assert len(drop_similar_for_display(articles, 0)) == 2


# --- 速報側の二重通知防止 ----------------------------------------------------

def test_similar_story_is_not_alerted_twice(conn, cfg):
    now = datetime.now(timezone.utc)
    article_id = db.insert_article(
        conn,
        normalized_title=normalize_title("Arbitrum Watchdog Seeks Permanent Bans For Three Grant Recipients"),
        display_title="Arbitrum Watchdog Seeks Permanent Bans For Three Grant Recipients",
        url="https://example.com/1", first_source="The Defiant",
        first_published_at=now.isoformat(), score=0, sentiment="neutral",
        emoji="📰", is_critical=False, critical_hits=[], coins=["ARB"],
    )
    db.mark_alerted(conn, article_id, "テスト")

    # 言い回しが違うだけの同じ話題 → 見送り
    similar = normalize_title("Arbitrum Proposal Seeks To Exclude Three DeFi Projects From Future Grants")
    assert _already_alerted_similar(conn, "ARB", similar, cfg, now) is True

    # 別の話題 → 通知してよい
    different = normalize_title("Arbitrum bridge exploited for $24M in USDC")
    assert _already_alerted_similar(conn, "ARB", different, cfg, now) is False


def test_duplicate_guard_expires_after_the_window(conn, cfg):
    now = datetime.now(timezone.utc)
    article_id = db.insert_article(
        conn, normalized_title=normalize_title("Arbitrum Watchdog Seeks Permanent Bans"),
        display_title="Arbitrum Watchdog Seeks Permanent Bans",
        url="https://example.com/2", first_source="The Defiant",
        first_published_at=now.isoformat(), score=0, sentiment="neutral",
        emoji="📰", is_critical=False, critical_hits=[], coins=["ARB"],
    )
    db.mark_alerted(conn, article_id, "テスト")

    similar = normalize_title("Arbitrum Proposal Seeks To Exclude Three Projects")
    later = now + timedelta(hours=cfg["dedup"]["alert_duplicate_window_hours"] + 1)
    assert _already_alerted_similar(conn, "ARB", similar, cfg, later) is False


def test_duplicate_guard_is_per_coin(conn, cfg):
    """ARBで速報済みでも、SOLの通知は止めない。"""
    now = datetime.now(timezone.utc)
    article_id = db.insert_article(
        conn, normalized_title=normalize_title("Exchange listing seeks approval for three tokens"),
        display_title="Exchange listing seeks approval for three tokens",
        url="https://example.com/3", first_source="媒体",
        first_published_at=now.isoformat(), score=0, sentiment="neutral",
        emoji="📰", is_critical=False, critical_hits=[], coins=["ARB"],
    )
    db.mark_alerted(conn, article_id, "テスト")
    same = normalize_title("Exchange listing seeks approval for three tokens")
    assert _already_alerted_similar(conn, "ARB", same, cfg, now) is True
    assert _already_alerted_similar(conn, "SOL", same, cfg, now) is False
