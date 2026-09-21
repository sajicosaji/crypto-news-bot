"""BTCの急騰・急落告知と、ARB円建て節目のお祝いを検証する。"""
from datetime import datetime, timedelta, timezone

from src import db
from src.alerts import check_btc_market_move, run_alert_for_coin, run_btc_market_alert


# --- BTC は相場全体の指標。急変したら告知する --------------------------------

def test_btc_move_detected_above_threshold():
    assert check_btc_market_move(6.2, 5) is not None
    assert check_btc_market_move(-7.5, 5) is not None
    assert "急騰" in check_btc_market_move(6.2, 5)
    assert "急落" in check_btc_market_move(-7.5, 5)


def test_btc_small_move_or_missing_is_ignored():
    assert check_btc_market_move(3.0, 5) is None
    assert check_btc_market_move(None, 5) is None


def test_btc_alert_posts_once_and_respects_cooldown(conn, cfg, monkeypatch):
    from src import summarize
    monkeypatch.setattr(summarize, "investigate_move", lambda **kw: None)

    price_data = {
        "BTC": {"usd": 80000.0, "jpy": 12500000.0, "usd_24h_change": 8.0},
        "ARB": {"usd": 0.16, "jpy": 25.0, "usd_24h_change": 6.0},
        "ETH": {"usd": 2400.0, "jpy": 375000.0, "usd_24h_change": 7.0},
    }
    kwargs = dict(conn=conn, cfg=cfg, price_data=price_data,
                  webhook_url="https://discord.example/eth", username="BTC Market", dry_run=True)
    assert run_btc_market_alert(**kwargs) is True
    # 12時間は同じ告知を繰り返さない
    assert run_btc_market_alert(**kwargs) is False


def test_btc_alert_is_silent_when_btc_is_calm(conn, cfg):
    price_data = {"BTC": {"usd": 75000.0, "jpy": 11800000.0, "usd_24h_change": 1.2}}
    assert run_btc_market_alert(
        conn=conn, cfg=cfg, price_data=price_data,
        webhook_url="https://discord.example/eth", username="BTC Market", dry_run=True,
    ) is False


def test_btc_articles_are_stored_for_cause_analysis(cfg):
    """BTC専用チャンネルは無いが、原因分析のためBTC記事は保存対象になる。"""
    from src.fetch_news import match_coins

    matching = dict(cfg["coins"])
    matching["BTC"] = {"names": cfg["btc_market_alert"]["names"]}
    assert "BTC" in match_coins("Bitcoin surges past $80,000 on ETF inflows", matching)
    assert "BTC" in match_coins("ビットコインが急騰", matching)
    assert "BTC" not in match_coins("Solana network upgrade goes live", matching)


# --- ARB 45円のお祝い ------------------------------------------------------

def _run_arb(conn, cfg, price_data, monkeypatch):
    from src import summarize
    monkeypatch.setattr(summarize, "investigate_move", lambda **kw: None)
    return run_alert_for_coin(
        conn=conn, coin="ARB", coin_cfg=cfg["coins"]["ARB"], webhook_url="https://discord.example/arb",
        cfg=cfg, touched_article_ids=[], price_data=price_data, btc_change_24h=0.5,
        hourly_change=None, onchain_summary=None, usd1_price=None, morpho_summary=None, dry_run=True,
    )


def test_celebrates_when_arb_crosses_45_yen_upward(conn, cfg, monkeypatch, capsys):
    earlier = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    db.save_price(conn, "ARB", earlier, 0.28, 44.0, 2.0)  # 直前は44円
    price_data = {"ARB": {"usd": 0.30, "jpy": 46.0, "usd_24h_change": 3.0}}

    posted = _run_arb(conn, cfg, price_data, monkeypatch)
    out = capsys.readouterr().out
    assert posted >= 1
    assert "45円突破" in out
    assert "利確ライン到達" in out
    assert "@here" in out, "この祝福だけはメンション付き"


def test_no_celebration_when_still_below_45(conn, cfg, monkeypatch, capsys):
    earlier = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    db.save_price(conn, "ARB", earlier, 0.27, 43.0, 1.0)
    price_data = {"ARB": {"usd": 0.28, "jpy": 44.5, "usd_24h_change": 1.0}}
    _run_arb(conn, cfg, price_data, monkeypatch)
    assert "45円突破" not in capsys.readouterr().out


def test_no_celebration_on_falling_back_below_45(conn, cfg, monkeypatch, capsys):
    """下抜けは祝わない（上抜けだけ）。"""
    earlier = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    db.save_price(conn, "ARB", earlier, 0.30, 46.0, 1.0)
    price_data = {"ARB": {"usd": 0.28, "jpy": 44.0, "usd_24h_change": -4.0}}
    _run_arb(conn, cfg, price_data, monkeypatch)
    assert "45円突破" not in capsys.readouterr().out


def test_celebration_has_cooldown_against_bouncing(conn, cfg, monkeypatch, capsys):
    """45円付近で行ったり来たりしても連発しない。"""
    earlier = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    db.save_price(conn, "ARB", earlier, 0.28, 44.0, 2.0)
    price_data = {"ARB": {"usd": 0.30, "jpy": 46.0, "usd_24h_change": 3.0}}
    _run_arb(conn, cfg, price_data, monkeypatch)
    capsys.readouterr()

    # また44→46に戻ってきても、12時間以内は祝わない
    later = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    db.save_price(conn, "ARB", later, 0.28, 44.0, 2.0)
    _run_arb(conn, cfg, price_data, monkeypatch)
    assert "45円突破" not in capsys.readouterr().out
