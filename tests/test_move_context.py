"""値動きの背景ニュースの絞り込みを検証する。"""
from datetime import datetime, timedelta, timezone

from src.move_context import analyze_move, direction_label, pick_move_context

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def _article(title, sentiment, *, hours_ago=2, critical=False, sources=1):
    return {
        "display_title": title,
        "url": f"https://example.com/{title}",
        "emoji": "🚀" if sentiment == "good" else "📉",
        "sentiment": sentiment,
        "is_critical": critical,
        "source_count": sources,
        "first_published_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
    }


# --- BTC連動かどうかの切り分け ----------------------------------------------

def test_no_btc_data_is_not_treated_as_coin_specific():
    result = analyze_move(5.0, None, 3)
    assert result["is_coin_specific"] is False
    assert "判定できません" in result["summary"]


def test_missing_price_returns_no_direction():
    result = analyze_move(None, 1.0, 3)
    assert result["direction"] is None
    assert result["is_coin_specific"] is False


def test_small_divergence_from_btc_is_market_wide():
    result = analyze_move(4.0, 3.0, 3)
    assert result["is_coin_specific"] is False


# --- 背景ニュースの選別 ------------------------------------------------------

def test_picks_good_news_when_price_rose():
    articles = [
        _article("良いニュース", "good", sources=3),
        _article("悪いニュース", "bad", sources=3),
        _article("中立ニュース", "neutral", sources=3),
    ]
    picked = pick_move_context(articles, "up", NOW, 3)
    titles = [a["display_title"] for a in picked]
    assert "良いニュース" in titles
    assert "悪いニュース" not in titles, "上昇時に悪材料を背景として出さない"


def test_picks_bad_news_when_price_fell():
    articles = [
        _article("良いニュース", "good", sources=3),
        _article("ハッキング発生", "bad", critical=True, sources=3),
    ]
    picked = pick_move_context(articles, "down", NOW, 3)
    assert [a["display_title"] for a in picked] == ["ハッキング発生"]


def test_more_widely_reported_and_critical_news_ranks_higher():
    articles = [
        _article("1媒体だけの良材料", "good", sources=1),
        _article("3媒体が報じた重大な良材料", "good", critical=True, sources=3),
    ]
    picked = pick_move_context(articles, "up", NOW, 3)
    assert picked[0]["display_title"] == "3媒体が報じた重大な良材料"


def test_recent_news_ranks_higher_than_old_news():
    articles = [
        _article("20時間前の良材料", "good", hours_ago=20, sources=2),
        _article("1時間前の良材料", "good", hours_ago=1, sources=2),
    ]
    picked = pick_move_context(articles, "up", NOW, 2)
    assert picked[0]["display_title"] == "1時間前の良材料"


def test_returns_empty_when_nothing_matches_direction():
    """該当が無ければ無理に結びつけない。"""
    articles = [_article("悪いニュース", "bad", sources=3)]
    assert pick_move_context(articles, "up", NOW, 3) == []


def test_weak_single_source_neutral_news_is_not_picked():
    articles = [_article("中立の小ネタ", "neutral", sources=1)]
    assert pick_move_context(articles, "up", NOW, 3) == []


def test_respects_max_items():
    articles = [_article(f"良材料{i}", "good", critical=True, sources=3) for i in range(10)]
    assert len(pick_move_context(articles, "up", NOW, 3)) == 3


def test_direction_label():
    assert direction_label("up") == "上昇"
    assert direction_label("down") == "下落"
    assert direction_label(None) == "値動き"


def test_widely_reported_neutral_news_is_picked_as_possible_driver():
    """キーワードで中立と判定されても、多くの媒体が報じた話題は候補にする。

    見出しのキーワードだけでは強弱を拾いきれないため
    （例:「Standard ChartedがARBの目標株価を引き上げ」は好材料だが該当語が無い）。
    """
    articles = [_article("10媒体が報じた話題", "neutral", hours_ago=20, sources=10)]
    picked = pick_move_context(articles, "up", NOW, 3)
    assert [a["display_title"] for a in picked] == ["10媒体が報じた話題"]


def test_thinly_reported_neutral_news_is_still_excluded():
    articles = [_article("2媒体だけの中立記事", "neutral", hours_ago=20, sources=2)]
    assert pick_move_context(articles, "up", NOW, 3) == []


def test_direction_mismatch_is_excluded_even_when_widely_reported():
    """上昇時に、10媒体が報じた悪材料を背景として挙げない。"""
    articles = [_article("10媒体が報じた悪材料", "bad", sources=10)]
    assert pick_move_context(articles, "up", NOW, 3) == []


# --- 日次まとめへの表示 ------------------------------------------------------

def test_digest_field_lists_news_when_move_is_coin_specific(cfg):
    from src.digest import build_move_context_field

    articles = [
        _article("大きく報じられた好材料", "good", hours_ago=3, sources=5),
        _article("関係なさそうな悪材料", "bad", hours_ago=3, sources=5),
    ]
    field = build_move_context_field(
        cfg=cfg, articles=articles, coin_change_24h=11.0, btc_change_24h=0.5
    )
    assert "銘柄固有の動き" in field["value"]
    assert "上昇の背景になりそうなニュース" in field["value"]
    assert "大きく報じられた好材料" in field["value"]
    assert "関係なさそうな悪材料" not in field["value"]


def test_digest_field_is_omitted_when_following_btc(cfg):
    """BTCと一緒に動いただけなら特筆することが無いので、欄ごと出さない。"""
    from src.digest import build_move_context_field

    articles = [_article("大きく報じられた好材料", "good", hours_ago=3, sources=5)]
    field = build_move_context_field(
        cfg=cfg, articles=articles, coin_change_24h=9.5, btc_change_24h=8.0
    )
    assert field is None


def test_digest_field_stays_silent_when_no_matching_news(cfg):
    """銘柄固有の動きなら、該当ニュースが無くてもその事実だけは伝える。"""
    from src.digest import build_move_context_field

    field = build_move_context_field(
        cfg=cfg, articles=[], coin_change_24h=11.0, btc_change_24h=0.5
    )
    assert "見つかりません" not in field["value"]
    assert "背景になりそうなニュース" not in field["value"]
    assert "銘柄固有の動き" in field["value"]


def test_digest_field_is_omitted_without_price_data(cfg):
    from src.digest import build_move_context_field

    assert build_move_context_field(
        cfg=cfg, articles=[], coin_change_24h=None, btc_change_24h=0.5
    ) is None
