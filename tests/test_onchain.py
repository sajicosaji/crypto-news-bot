from datetime import datetime, timedelta, timezone

from src.onchain import (
    summarize_daily_metric,
    summarize_treasury,
    treasury_total_at,
    weekly_totals_from_series,
)


def _make_chart(daily_values: list[float], end_date: datetime) -> list[list[float]]:
    chart = []
    n = len(daily_values)
    for i, v in enumerate(daily_values):
        day = end_date - timedelta(days=(n - 1 - i))
        ts = int(day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        chart.append([ts, v])
    return chart


def test_summarize_daily_metric_computes_dod_and_weekly_change():
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    # 14日分: 前半week=100固定、後半week=200固定(最終日のみ300で前日比+50%)
    values = [100] * 7 + [200] * 6 + [300]
    chart = _make_chart(values, yesterday)
    overview = {"totalDataChart": chart}

    summary = summarize_daily_metric(overview)
    assert summary is not None
    assert summary["yesterday_value"] == 300
    assert round(summary["dod_change_pct"], 1) == 50.0
    assert round(summary["avg_prev7"], 1) == 100.0
    assert summary["week_change_pct"] > 90  # 大幅増加を検知できる


def test_summarize_daily_metric_excludes_incomplete_current_day():
    now = datetime.now(timezone.utc)
    values = [100] * 13 + [999]  # 最後の値は「今日」のもの（未確定）
    chart = _make_chart(values, now)
    overview = {"totalDataChart": chart}
    summary = summarize_daily_metric(overview)
    assert summary["yesterday_value"] == 100
    assert summary["dod_change_pct"] == 0.0


def test_summarize_daily_metric_handles_missing_data():
    assert summarize_daily_metric(None) is None
    assert summarize_daily_metric({"totalDataChart": []}) is None


def test_weekly_totals_from_series_orders_oldest_first():
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    values = list(range(1, 29))  # 28日分, 増加トレンド
    chart = _make_chart([float(v) for v in values], yesterday)
    overview = {"totalDataChart": chart}
    summary = summarize_daily_metric(overview)
    totals = weekly_totals_from_series(summary["series"], weeks=4)
    assert len(totals) == 4
    # 直近の週の合計が最も大きい（増加トレンドのため）はず
    assert totals[-1]["total"] > totals[0]["total"]


def test_treasury_total_at_sums_components_with_forward_fill():
    now_ts = int(datetime.now(timezone.utc).timestamp())
    data = {
        "chainTvls": {
            "Arbitrum": {"tvl": [{"date": now_ts - 100, "totalLiquidityUSD": 100.0}]},
            "OwnTokens": {"tvl": [{"date": now_ts - 100, "totalLiquidityUSD": 400.0}]},
        }
    }
    total = treasury_total_at(data, now_ts)
    assert total == 500.0


def test_summarize_treasury_returns_history():
    now_ts = int(datetime.now(timezone.utc).timestamp())
    entries = [{"date": now_ts - w * 7 * 86400, "totalLiquidityUSD": 1000.0 + w} for w in range(6)]
    data = {"chainTvls": {"Arbitrum": {"tvl": entries}}}
    summary = summarize_treasury(data, weeks_back=4)
    assert summary is not None
    assert summary["current"] == 1000.0
    assert len(summary["history"]) == 5
