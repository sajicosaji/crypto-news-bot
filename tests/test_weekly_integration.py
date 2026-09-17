"""週次振り返りを、1週間分のデータが溜まった状態で通しで検証する。

週次は月曜の朝に1回しか動かないため、本番で初めて壊れると気づくのが遅れる。
価格スナップショット・記事・速報履歴を実運用に近い形で作って確認する。
"""
from datetime import datetime, timedelta, timezone

import pytest

from src import db
from src.utils import JST
from src.weekly import (
    daily_closes,
    daily_sentiment_counts,
    last_week_bounds,
    nearest_price,
    news_before,
    run_weekly_for_coin,
    top_move_days,
    week_alerts,
)


@pytest.fixture()
def week_of_data(conn):
    """先週1週間分の価格・記事・速報を用意する（30分ごとの価格記録を想定）。"""
    start, end = last_week_bounds()
    # 日ごとに終値が動くように価格を作る（3日目に大きく下落、5日目に大きく上昇）
    daily_prices = [0.20, 0.21, 0.15, 0.16, 0.22, 0.21, 0.20]
    btc_prices = [70000, 70500, 69000, 69500, 71000, 70800, 70600]

    for day_index in range(7):
        day_start = start + timedelta(days=day_index)
        for half_hour in range(0, 48, 6):  # 3時間おきに記録（テストを軽くするため）
            ts = (day_start + timedelta(minutes=30 * half_hour)).astimezone(timezone.utc)
            if ts >= end:
                break
            db.save_price(conn, "ARB", ts.isoformat(), daily_prices[day_index], None, 0.0)
            db.save_price(conn, "BTC", ts.isoformat(), btc_prices[day_index], None, 0.0)

        # 各日にニュースを2件ずつ（値動きの直前12時間に入る時刻に置く）
        for n in range(2):
            published = (day_start + timedelta(hours=14 + n)).astimezone(timezone.utc)
            db.insert_article(
                conn,
                normalized_title=f"day{day_index} news{n}",
                display_title=f"{day_index}日目のニュース{n}",
                url=f"https://example.com/{day_index}/{n}",
                first_source="テスト媒体",
                first_published_at=published.isoformat(),
                score=-1 if day_index == 2 else 1,
                sentiment="bad" if day_index == 2 else "good",
                emoji="📉" if day_index == 2 else "🚀",
                is_critical=(day_index == 2),
                critical_hits=["exploit"] if day_index == 2 else [],
                coins=["ARB"],
                excerpt="本文の抜粋テキスト。",
            )

        db.log_alert(
            conn, "ARB", "price_change", "24h", f"{day_index}日目の速報",
            (day_start + timedelta(hours=15)).astimezone(timezone.utc).isoformat(),
        )
    return start, end


def test_daily_closes_picks_last_price_of_each_day(conn, week_of_data):
    start, end = week_of_data
    closes = daily_closes(conn, "ARB", start, end)
    assert len(closes) == 7
    # JSTの日付でまとまっていること
    assert sorted(closes.keys())[0] == start.date()


def test_top_move_days_finds_biggest_swings(conn, week_of_data):
    start, end = week_of_data
    closes = daily_closes(conn, "ARB", start, end)
    moves = top_move_days(closes, 3)
    assert len(moves) == 3

    # 変動率の絶対値が大きい順に並ぶこと（上げ・下げを区別せず拾う）
    assert [abs(m["pct"]) for m in moves] == sorted((abs(m["pct"]) for m in moves), reverse=True)

    # 最大は5日目の急騰(0.16→0.22 = +37.5%)、次が3日目の急落(0.21→0.15 = -28.6%)
    assert moves[0]["date"] == (start + timedelta(days=4)).date()
    assert moves[0]["pct"] > 35
    assert moves[1]["date"] == (start + timedelta(days=2)).date()
    assert moves[1]["pct"] < -25


def test_news_before_returns_preceding_12_hours(conn, week_of_data):
    start, end = week_of_data
    closes = daily_closes(conn, "ARB", start, end)
    day = (start + timedelta(days=2)).date()
    reference = datetime.fromisoformat(closes[day]["ts"])
    news = news_before(conn, "ARB", reference)
    assert news, "直前のニュースが取得できること"
    for n in news:
        published = datetime.fromisoformat(n["first_published_at"])
        assert published < reference
        assert published >= reference - timedelta(hours=12)


def test_weekly_open_close_and_alerts(conn, week_of_data):
    start, end = week_of_data
    open_price = nearest_price(conn, "ARB", start)
    close_price = nearest_price(conn, "ARB", end)
    assert open_price["usd"] == 0.20
    assert close_price["usd"] == 0.20
    assert len(week_alerts(conn, "ARB", start, end)) == 7
    counts = daily_sentiment_counts(conn, "ARB", start, end)
    assert counts[(start + timedelta(days=2)).date()]["bad"] == 2


def test_run_weekly_for_coin_renders_without_posting(conn, cfg, week_of_data, capsys):
    ok = run_weekly_for_coin(
        conn=conn,
        coin="ARB",
        coin_cfg=cfg["coins"]["ARB"],
        cfg=cfg,
        webhook_url="https://discord.example/webhook",
        onchain_trend_lines=["Robinhood Chain収益の週次推移:", "・2026-09-13週: 1,000ドル"],
        dry_run=True,
    )
    assert ok is True
    output = capsys.readouterr().out
    assert "週次振り返り" in output
    assert "値動きが大きかった日" in output
    assert "先週の速報" in output
