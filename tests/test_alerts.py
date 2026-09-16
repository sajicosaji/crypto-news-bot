from datetime import datetime, timedelta, timezone

from src import db
from src.alerts import (
    check_hourly_change_alert,
    check_onchain_weekly_change,
    check_price_change_alert,
    check_price_level_cross,
    check_usd1_depeg,
    classify_move_scope,
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
    reason = evaluate_news_alert("ETH", "Ethereum hit by exploit", ["exploit"], 3, 5.0, cfg)
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


def test_move_scope_market_wide_vs_coin_specific():
    assert classify_move_scope(4.0) == "相場全体の動き"
    assert classify_move_scope(0.5) == "銘柄固有の動き"
    assert classify_move_scope(None) == "銘柄固有の動き"


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
