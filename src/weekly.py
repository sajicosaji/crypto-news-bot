"""週次振り返り（--weekly）の生成・投稿。"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone

from . import discord
from .formatting import fit_lines, fmt_pct, fmt_usd
from .utils import JST, now_jst, to_jst

logger = logging.getLogger("crypto_news_bot.weekly")


def last_week_bounds(today: date | None = None) -> tuple[datetime, datetime]:
    today = today or now_jst().date()
    this_monday = today - timedelta(days=today.weekday())
    last_monday = this_monday - timedelta(days=7)
    start = datetime.combine(last_monday, time.min, tzinfo=JST)
    end = datetime.combine(this_monday, time.min, tzinfo=JST)
    return start, end


def nearest_price(conn, coin: str, target_dt: datetime) -> dict | None:
    target_iso = target_dt.astimezone(timezone.utc).isoformat()
    before = conn.execute(
        "SELECT * FROM prices WHERE coin = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
        (coin, target_iso),
    ).fetchone()
    after = conn.execute(
        "SELECT * FROM prices WHERE coin = ? AND ts >= ? ORDER BY ts ASC LIMIT 1",
        (coin, target_iso),
    ).fetchone()
    candidates = [r for r in (before, after) if r is not None]
    if not candidates:
        return None

    def diff(r):
        return abs((datetime.fromisoformat(r["ts"]) - target_dt.astimezone(timezone.utc)).total_seconds())

    best = min(candidates, key=diff)
    return dict(best)


def daily_closes(conn, coin: str, start: datetime, end: datetime) -> dict[date, dict]:
    rows = conn.execute(
        "SELECT * FROM prices WHERE coin = ? AND ts >= ? AND ts < ? ORDER BY ts ASC",
        (coin, start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()),
    ).fetchall()
    by_date: dict[date, dict] = {}
    for r in rows:
        d = to_jst(datetime.fromisoformat(r["ts"])).date()
        by_date[d] = dict(r)
    return by_date


def top_move_days(closes: dict[date, dict], top_n: int = 3) -> list[dict]:
    dates = sorted(closes.keys())
    moves = []
    for i in range(1, len(dates)):
        prev_v = closes[dates[i - 1]]["usd"]
        curr_v = closes[dates[i]]["usd"]
        if not prev_v:
            continue
        pct = (curr_v - prev_v) / prev_v * 100
        moves.append({"date": dates[i], "pct": pct, "usd": curr_v})
    moves.sort(key=lambda m: abs(m["pct"]), reverse=True)
    return moves[:top_n]


def news_before(conn, coin: str, reference: datetime, hours: int = 12, limit: int = 20) -> list[dict]:
    since = (reference - timedelta(hours=hours)).astimezone(timezone.utc).isoformat()
    until = reference.astimezone(timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT a.display_title, a.url, a.first_published_at, a.emoji, a.first_source "
        "FROM articles a JOIN article_coins ac ON ac.article_id = a.id "
        "WHERE ac.coin = ? AND a.first_published_at >= ? AND a.first_published_at < ? "
        "ORDER BY a.first_published_at ASC LIMIT ?",
        (coin, since, until, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def week_alerts(conn, coin: str, start: datetime, end: datetime) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM alerts_log WHERE coin = ? AND ts >= ? AND ts < ? ORDER BY ts ASC",
        (coin, start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()),
    ).fetchall()
    return [dict(r) for r in rows]


def daily_sentiment_counts(conn, coin: str, start: datetime, end: datetime) -> dict[date, dict]:
    rows = conn.execute(
        "SELECT a.first_published_at, a.sentiment FROM articles a "
        "JOIN article_coins ac ON ac.article_id = a.id "
        "WHERE ac.coin = ? AND a.first_published_at >= ? AND a.first_published_at < ?",
        (coin, start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()),
    ).fetchall()
    counts: dict[date, dict] = {}
    for r in rows:
        d = to_jst(datetime.fromisoformat(r["first_published_at"])).date()
        c = counts.setdefault(d, {"good": 0, "bad": 0, "neutral": 0})
        c[r["sentiment"]] += 1
    return counts


def build_weekly_embed(
    *,
    coin: str,
    start: datetime,
    end: datetime,
    open_price: dict | None,
    close_price: dict | None,
    btc_open: dict | None,
    btc_close: dict | None,
    top_moves: list[dict],
    news_by_day: dict[date, list[dict]],
    onchain_trend_lines: list[str],
    alerts: list[dict],
    sentiment_counts: dict[date, dict],
) -> dict:
    lines = []

    if open_price and close_price and open_price.get("usd"):
        week_pct = (close_price["usd"] - open_price["usd"]) / open_price["usd"] * 100
        lines.append(
            f"先週の推移: {fmt_usd(open_price['usd'])} → {fmt_usd(close_price['usd'])}（{fmt_pct(week_pct)}）"
        )
        if btc_open and btc_close and btc_open.get("usd"):
            btc_pct = (btc_close["usd"] - btc_open["usd"]) / btc_open["usd"] * 100
            lines.append(f"BTC: {fmt_pct(btc_pct)}")
    else:
        lines.append("先週の価格データが不足しているため、週間の変化率は算出できませんでした。")

    lines.append("")
    if top_moves:
        lines.append("値動きが大きかった日:")
        for m in top_moves:
            lines.append(f"・{m['date'].isoformat()}: {fmt_pct(m['pct'])}（{fmt_usd(m['usd'])}）")
            for n in news_by_day.get(m["date"], [])[:5]:
                dt = datetime.fromisoformat(n["first_published_at"])
                lines.append(f"　　{n['emoji']} [{n['display_title']}]({n['url']}) - {to_jst(dt).strftime('%m/%d %H:%M')}")
    else:
        lines.append("値動きの大きい日を判定するためのデータが不足しています。")

    if onchain_trend_lines:
        lines.append("")
        lines.extend(onchain_trend_lines)

    if alerts:
        lines.append("")
        lines.append(f"先週の速報: {len(alerts)}件")
        for a in alerts[:10]:
            dt = to_jst(datetime.fromisoformat(a["ts"]))
            lines.append(f"・[{a['alert_type']}] {a['message']}（{dt.strftime('%m/%d %H:%M')}）")

    if sentiment_counts:
        lines.append("")
        lines.append("良い/悪いニュース件数の推移:")
        for d in sorted(sentiment_counts.keys()):
            c = sentiment_counts[d]
            lines.append(f"・{d.isoformat()}: 良{c['good']} / 悪{c['bad']} / 中立{c['neutral']}")

    total_good = sum(c["good"] for c in sentiment_counts.values())
    total_bad = sum(c["bad"] for c in sentiment_counts.values())
    if total_good > total_bad:
        color = discord.COLOR_GOOD
    elif total_bad > total_good:
        color = discord.COLOR_BAD
    else:
        color = discord.COLOR_NEUTRAL

    return {
        "title": f"{coin} 週次振り返り - {start.date().isoformat()} 〜 {(end - timedelta(days=1)).date().isoformat()}",
        # 上限を超えると投稿そのものが失敗するため、長すぎる場合は切り詰める
        "description": "\n".join(fit_lines(lines)),
        "color": color,
        "timestamp": now_jst().isoformat(),
    }


def run_weekly_for_coin(
    *, conn, coin: str, coin_cfg: dict, cfg: dict, webhook_url: str,
    onchain_trend_lines: list[str], dry_run: bool,
) -> bool:
    start, end = last_week_bounds()
    open_price = nearest_price(conn, coin, start)
    close_price = nearest_price(conn, coin, end)
    btc_open = nearest_price(conn, "BTC", start)
    btc_close = nearest_price(conn, "BTC", end)

    closes = daily_closes(conn, coin, start, end)
    top_moves = top_move_days(closes, cfg["weekly"]["top_move_days"])
    news_by_day = {}
    for m in top_moves:
        ref_dt = closes[m["date"]]
        ref_datetime = datetime.fromisoformat(ref_dt["ts"])
        news_by_day[m["date"]] = news_before(conn, coin, ref_datetime)

    alerts = week_alerts(conn, coin, start, end)
    sentiment_counts = daily_sentiment_counts(conn, coin, start, end)

    embed = build_weekly_embed(
        coin=coin, start=start, end=end, open_price=open_price, close_price=close_price,
        btc_open=btc_open, btc_close=btc_close, top_moves=top_moves, news_by_day=news_by_day,
        onchain_trend_lines=onchain_trend_lines, alerts=alerts, sentiment_counts=sentiment_counts,
    )
    return discord.post_webhook(
        webhook_url,
        username=coin_cfg["username"],
        embeds=[embed],
        dry_run=dry_run,
        min_interval_seconds=cfg["discord"]["post_interval_seconds"],
    )
