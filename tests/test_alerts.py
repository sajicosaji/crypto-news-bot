from datetime import datetime, timedelta, timezone

from src import db
from src.alerts import (
    check_hourly_change_alert,
    check_onchain_weekly_change,
    check_price_change_alert,
    check_price_level_cross,
    check_usd1_depeg,
    evaluate_news_alert,
    is_in_cooldown,
)


def test_arb_critical_keyword_is_alert_eligible(cfg):
    reason = evaluate_news_alert("ARB", "Arbitrum bridge hit by exploit", ["exploit"], 1, 0.1, cfg)
    assert reason is not None
    assert "重大ワード" in reason


def test_arb_priority_bad_keyword_without_critical_is_alert_eligible(cfg):
    reason = evaluate_news_alert("ARB", "Arbitrum team announces token unlock schedule", [], 1, 0.1, cfg)
    assert reason is not None


def test_arb_dao_proposal_is_alert_eligible(cfg):
    reason = evaluate_news_alert("ARB", "[DAO提案] Proposal to enable ARB buyback and burn", [], 1, 0.1, cfg)
    assert reason is not None
    assert "DAO" in reason


def test_wld_regulatory_priority_keyword_is_alert_eligible(cfg):
    reason = evaluate_news_alert("WLD", "South Korea regulator opens investigation into Worldcoin", [], 1, 0.1, cfg)
    assert reason is not None


def test_wld_morpho_risk_combo_is_alert_eligible(cfg):
    reason = evaluate_news_alert("WLD", "Morpho WLD market hit by bad debt after oracle issue", [], 1, 0.1, cfg)
    assert reason is not None
    assert "Morpho" in reason


def test_wld_ordinary_news_without_priority_keyword_is_not_eligible(cfg):
    reason = evaluate_news_alert("WLD", "Worldcoin releases quarterly community update", [], 1, 0.1, cfg)
    assert reason is None


def test_eth_requires_two_media_within_window(cfg):
    # 1媒体だけではETHは速報にならない
    reason = evaluate_news_alert("ETH", "Ethereum hit by exploit", ["exploit"], 1, 1.0, cfg)
    assert reason is None
    # 2媒体・3時間以内なら速報になる
    reason2 = evaluate_news_alert("ETH", "Ethereum hit by exploit", ["exploit"], 2, 1.0, cfg)
    assert reason2 is not None


def test_eth_outside_window_is_not_eligible_even_with_media_count(cfg):
    # 媒体数は「注目ニュース」のしきい値(3)未満にして、時間窓の判定だけを見る
    reason = evaluate_news_alert("ETH", "Ethereum hit by exploit", ["exploit"], 2, 5.0, cfg)
    assert reason is None


def test_sol_dex_plus_critical_combo_is_eligible_without_media_threshold(cfg):
    reason = evaluate_news_alert("SOL", "Raydium exploited for millions in hack", ["hack"], 1, 0.1, cfg)
    assert reason is not None
    assert "Raydium" in reason


def test_sol_plain_critical_without_dex_needs_media_threshold(cfg):
    reason = evaluate_news_alert("SOL", "Solana network hit by exploit", ["exploit"], 1, 0.1, cfg)
    assert reason is None


def test_price_change_threshold_differs_by_coin_group(cfg):
    assert check_price_change_alert("ARB", 11.0, cfg) is not None
    assert check_price_change_alert("ARB", 9.0, cfg) is None
    assert check_price_change_alert("ETH", 9.0, cfg) is not None
    assert check_price_change_alert("ETH", 7.0, cfg) is None


def test_price_level_cross_up_and_down():
    assert check_price_level_cross(0.13, 0.15, [0.14, 0.20]) == ("up", 0.14)
    assert check_price_level_cross(0.15, 0.13, [0.14, 0.20]) == ("down", 0.14)
    assert check_price_level_cross(0.15, 0.16, [0.14, 0.20]) is None


def test_hourly_change_alert(cfg):
    assert check_hourly_change_alert("SOL", 6.0, 5.0) is not None
    assert check_hourly_change_alert("SOL", 4.0, 5.0) is None


def test_usd1_depeg_alert(cfg):
    assert check_usd1_depeg(0.99, 0.995, 1.005) is not None
    assert check_usd1_depeg(1.006, 0.995, 1.005) is not None
    assert check_usd1_depeg(1.0, 0.995, 1.005) is None


def test_onchain_weekly_change_alert():
    summary = {"week_change_pct": 35.0}
    assert check_onchain_weekly_change(summary, 30) is not None
    summary2 = {"week_change_pct": -35.0}
    assert check_onchain_weekly_change(summary2, 30) is not None
    summary3 = {"week_change_pct": 10.0}
    assert check_onchain_weekly_change(summary3, 30) is None


def test_move_is_market_wide_when_following_btc():
    """BTCと一緒に動いただけなら銘柄固有とはみなさない。"""
    from src.move_context import analyze_move

    result = analyze_move(coin_change_24h=9.0, btc_change_24h=8.0, coin_specific_pct=3)
    assert result["is_coin_specific"] is False
    assert "相場全体" in result["summary"]


def test_move_is_coin_specific_when_diverging_from_btc():
    from src.move_context import analyze_move

    result = analyze_move(coin_change_24h=11.0, btc_change_24h=0.5, coin_specific_pct=3)
    assert result["is_coin_specific"] is True
    assert result["direction"] == "up"
    assert round(result["excess_pct"], 1) == 10.5


def test_move_is_coin_specific_when_falling_against_rising_btc():
    """BTCが上がっているのに下げた場合も銘柄固有として拾う。"""
    from src.move_context import analyze_move

    result = analyze_move(coin_change_24h=-5.0, btc_change_24h=3.0, coin_specific_pct=3)
    assert result["is_coin_specific"] is True
    assert result["direction"] == "down"


def test_price_level_cooldown_prevents_repeat_alerts_within_window(conn):
    now = datetime.now(timezone.utc)
    db.log_alert(conn, "ARB", "price_level", "level:0.14", "test", now.isoformat())
    assert is_in_cooldown(conn, "ARB", "price_level", "level:0.14", 12, now + timedelta(hours=1)) is True
    assert is_in_cooldown(conn, "ARB", "price_level", "level:0.14", 12, now + timedelta(hours=13)) is False


def test_price_level_cooldown_key_is_shared_across_direction_to_avoid_flapping():
    # 上抜け・下抜けを繰り返しても、節目ごとに1つのdedupeキーにまとめて連発を防ぐ
    up = check_price_level_cross(0.13, 0.15, [0.14])
    down = check_price_level_cross(0.15, 0.13, [0.14])
    key_up = f"level:{up[1]}"
    key_down = f"level:{down[1]}"
    assert key_up == key_down


# --- 注目ニュース（重大ワードが無くても多媒体なら個別速報） --------------------

def test_widely_reported_news_alerts_even_without_critical_keyword(cfg):
    """「価値のあるニュースは朝を待たず個別に出す」という要望への対応。"""
    reason = evaluate_news_alert(
        "SOL", "Standard Chartered raises Solana target", [], 3, 1.0, cfg
    )
    assert reason is not None
    assert "3媒体" in reason


def test_widely_reported_rule_applies_to_eth_and_sol_too(cfg):
    """ETH・SOLは従来2媒体+重大ワードが必要だったが、注目ニュースは重大ワード不要。"""
    for coin in ("ETH", "SOL", "ARB", "WLD"):
        assert evaluate_news_alert(coin, "大きく報じられた話題", [], 3, 1.0, cfg) is not None


def test_thinly_reported_ordinary_news_still_does_not_alert(cfg):
    """1〜2媒体だけの平凡なニュースでは通知しない（通知過多を防ぐ）。"""
    assert evaluate_news_alert("SOL", "Solana community update", [], 2, 1.0, cfg) is None
    assert evaluate_news_alert("ETH", "Ethereum meetup announced", [], 1, 1.0, cfg) is None


def test_notable_news_is_not_limited_by_the_three_hour_window(cfg):
    """注目ニュースは「3時間以内の速報」ルールとは別枠で拾う。

    全体の「公開6時間以内」ガード(news_alert_max_age_hours)で古すぎる記事は
    別途はじかれるため、ここでは時間窓に縛られない。
    """
    reason = evaluate_news_alert("ETH", "大きく報じられた話題", [], 3, 5.0, cfg)
    assert reason is not None
