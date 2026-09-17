"""速報（--alert）の判定ロジックと実行。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import db, discord
from .formatting import fmt_level, fmt_pct, fmt_usd
from .move_context import analyze_move, direction_label, pick_move_context
from .utils import contains_term, format_jst, now_jst

logger = logging.getLogger("crypto_news_bot.alerts")


# ---------------------------------------------------------------------------
# 判定ロジック（DBに依存しない純粋関数。ユニットテスト対象）
# ---------------------------------------------------------------------------

def evaluate_news_alert(
    coin: str,
    title: str,
    critical_hits: list[str],
    source_count: int,
    hours_since_first: float,
    cfg: dict,
) -> str | None:
    """ニュース記事が速報候補かどうかを判定し、理由文字列を返す（対象外なら None）。"""
    thresholds = cfg["alert_thresholds"]

    if title.startswith("[DAO提案]"):
        return "Arbitrum DAO提案（buyback/burn等）"

    # 多くの媒体が同じニュースを追いかけていれば、重大ワードが無くても価値のある話題と
    # みなして全銘柄で個別に速報する（朝のまとめまで待たせない）
    notable_count = thresholds.get("notable_media_count")
    if notable_count and source_count >= notable_count:
        return f"{source_count}媒体が報じている注目ニュース"

    if coin in ("ARB", "WLD"):
        if critical_hits:
            return f"重大ワード: {', '.join(critical_hits)}"
        coin_cfg = cfg[coin.lower()]
        for kw in coin_cfg.get("priority_good_keywords", []):
            if contains_term(title, kw):
                return f"注目の良材料: {kw}"
        for kw in coin_cfg.get("priority_bad_keywords", []):
            if contains_term(title, kw):
                return f"注目の悪材料: {kw}"
        if coin == "WLD":
            morpho_cfg = cfg["wld"].get("morpho", {})
            if morpho_cfg.get("enabled") and contains_term(title, "Morpho"):
                for kw in morpho_cfg.get("risk_keywords", []):
                    if contains_term(title, kw):
                        return f"WLD関連のDeFiリスク情報: Morpho {kw}"
        return None

    if coin in ("ETH", "SOL"):
        if (
            critical_hits
            and source_count >= thresholds["eth_sol_critical_media_count"]
            and hours_since_first <= thresholds["eth_sol_critical_window_hours"]
        ):
            return f"重大ワード: {', '.join(critical_hits)}（{source_count}媒体が報道）"
        if coin == "SOL" and critical_hits:
            dex_names = cfg["sol"].get("dex_names", [])
            for dex in dex_names:
                if contains_term(title, dex):
                    return f"主要DEX({dex})関連の重大ワード: {', '.join(critical_hits)}"
        return None

    return None


def check_price_change_alert(coin: str, pct_change_24h: float | None, cfg: dict) -> str | None:
    thresholds = cfg["alert_thresholds"]
    if pct_change_24h is None:
        return None
    if coin in ("ARB", "WLD"):
        limit = thresholds["arb_wld_price_change_pct"]
    elif coin in ("ETH", "SOL"):
        limit = thresholds["eth_sol_price_change_pct"]
    else:
        return None
    if abs(pct_change_24h) >= limit:
        return f"{coin} {fmt_pct(pct_change_24h)}（24h）"
    return None


def check_price_level_cross(
    prev_price: float | None, curr_price: float | None, levels: list[float]
) -> tuple[str, float] | None:
    """節目を上抜け・下抜けしたら (方向, 節目) を返す。"""
    if prev_price is None or curr_price is None or prev_price == curr_price:
        return None
    for level in levels:
        if prev_price < level <= curr_price:
            return ("up", level)
        if prev_price >= level > curr_price:
            return ("down", level)
    return None


def check_hourly_change_alert(coin: str, pct_change_1h: float | None, threshold_pct: float) -> str | None:
    if pct_change_1h is None:
        return None
    if abs(pct_change_1h) >= threshold_pct:
        return f"{coin} {fmt_pct(pct_change_1h)}（1h）"
    return None


def check_usd1_depeg(price_usd: float | None, low: float, high: float) -> str | None:
    if price_usd is None:
        return None
    if price_usd < low:
        return f"USD1が{price_usd:.4f}ドルに下落（デペッグ懸念）"
    if price_usd > high:
        return f"USD1が{price_usd:.4f}ドルに上昇（デペッグ懸念）"
    return None


def check_onchain_weekly_change(summary: dict | None, threshold_pct: float) -> str | None:
    if not summary or summary.get("week_change_pct") is None:
        return None
    pct = summary["week_change_pct"]
    if abs(pct) >= threshold_pct:
        return f"Robinhood Chain収益の7日平均が前週比{fmt_pct(pct)}"
    return None


def check_morpho_utilization(summary: dict | None, threshold_pct: float) -> str | None:
    if not summary:
        return None
    util_pct = (summary.get("max_utilization") or 0) * 100
    if util_pct >= threshold_pct:
        return f"WLD関連Morphoマーケットの利用率が{util_pct:.1f}%"
    return None


def is_in_cooldown(
    conn, coin: str, alert_type: str, dedupe_key: str, cooldown_hours: float, now: datetime
) -> bool:
    last = db.last_alert_time(conn, coin, alert_type, dedupe_key)
    if not last:
        return False
    last_dt = datetime.fromisoformat(last)
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return (now - last_dt) < timedelta(hours=cooldown_hours)


# ---------------------------------------------------------------------------
# 実行（Discordへの投稿を含む）
# ---------------------------------------------------------------------------

def _mention(cfg: dict) -> str:
    return cfg.get("mention", "@here") or ""


def _post_news_alert(
    *, webhook_url, username, coin, article_row, reason, source_count,
    price_data, cfg, dry_run,
):
    emojis = cfg["emojis"]
    title = article_row["display_title"]
    emoji = article_row["emoji"]
    color = discord.COLOR_ALERT_GOOD if article_row["sentiment"] == "good" else discord.COLOR_ALERT_BAD
    first_published = datetime.fromisoformat(article_row["first_published_at"])
    price = price_data.get(coin, {})
    description_lines = []

    excerpt = (article_row["excerpt"] or "").strip() if "excerpt" in article_row.keys() else ""
    if excerpt:
        description_lines.append(excerpt)
        description_lines.append("")

    description_lines += [
        f"判定理由: {reason}",
        f"最初に報じた媒体: {article_row['first_source']}（{format_jst(first_published)}）",
        f"報道媒体数: {source_count}",
    ]
    if price:
        description_lines.append(
            f"現在価格: {fmt_usd(price.get('usd'))} / 24h {fmt_pct(price.get('usd_24h_change'))}"
        )

    # 独自絵文字はembedタイトルでは描画されないため、本文の先頭に回す
    alert_emoji = emojis["alert"]
    body_emojis = [e for e in (alert_emoji, emoji) if discord.is_custom_emoji(e)]
    if body_emojis:
        description_lines.insert(0, " ".join(body_emojis))
    title_parts = [
        "" if discord.is_custom_emoji(alert_emoji) else alert_emoji,
        "速報",
        "" if discord.is_custom_emoji(emoji) else emoji,
        title,
    ]

    embed = {
        "title": " ".join(p for p in title_parts if p),
        "url": article_row["url"],
        "description": "\n".join(description_lines),
        "color": color,
    }
    return discord.post_webhook(
        webhook_url,
        username=username,
        content=_mention(cfg),
        embeds=[embed],
        dry_run=dry_run,
        min_interval_seconds=cfg["discord"]["post_interval_seconds"],
    )


def _recent_articles(conn, coin: str, now: datetime, hours: int = 24) -> list[dict]:
    """値動きの背景候補にする、直近の記事（媒体数つき）。"""
    since = (now - timedelta(hours=hours)).isoformat()
    return db.recent_articles_with_source_count(conn, coin, since)


def _article_lines(articles: list[dict]) -> list[str]:
    lines = []
    for a in articles:
        url = a.get("excerpt_url") or a["url"]
        dt = datetime.fromisoformat(a["first_published_at"])
        lines.append(f"{a['emoji']} [{a['display_title']}]({url}) - {format_jst(dt)}")
    return lines


def _recent_news_lines(conn, coin: str, now: datetime, hours: int = 12, limit: int = 5) -> list[str]:
    since = (now - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        "SELECT a.display_title, a.url, a.first_published_at, a.emoji "
        "FROM articles a JOIN article_coins ac ON ac.article_id = a.id "
        "WHERE ac.coin = ? AND a.first_published_at >= ? "
        "ORDER BY a.first_published_at DESC LIMIT ?",
        (coin, since, limit),
    ).fetchall()
    lines = []
    for row in rows:
        dt = datetime.fromisoformat(row["first_published_at"])
        lines.append(f"{row['emoji']} [{row['display_title']}]({row['url']}) - {format_jst(dt)}")
    return lines


def _todays_macro_events(cfg: dict, now_jst_dt: datetime) -> list[str]:
    today = now_jst_dt.date().isoformat()
    return [e["description"] for e in cfg.get("macro_events", []) if e.get("date") == today]


def _post_data_alert(
    *, webhook_url, username, title_text, reason, coin, btc_change_24h, coin_change_24h,
    cfg, conn, now, dry_run,
):
    emojis = cfg["emojis"]
    move_cfg = cfg.get("move_context", {})
    analysis = analyze_move(coin_change_24h, btc_change_24h, move_cfg.get("coin_specific_pct", 3))
    lines = [reason, analysis["summary"]]

    macro = _todays_macro_events(cfg, now_jst())
    if macro:
        lines.append("本日のマクロ予定: " + " / ".join(macro))

    if move_cfg.get("enabled", True) and analysis["is_coin_specific"]:
        # BTCと切り離して動いているので、向きの合うニュースを背景候補として出す
        picked = pick_move_context(
            _recent_articles(conn, coin, now), analysis["direction"], now,
            move_cfg.get("max_items", 3),
        )
        label = direction_label(analysis["direction"])
        if picked:
            lines.append("")
            lines.append(f"{label}の背景になりそうなニュース（原因と断定するものではありません）:")
            lines.extend(_article_lines(picked))
        else:
            lines.append("")
            lines.append(f"{label}の背景になりそうなニュースは見つかりませんでした。")

    recent = _recent_news_lines(conn, coin, now)
    if recent:
        lines.append("")
        lines.append("直前のニュース（速報の原因と断定するものではありません）:")
        lines.extend(recent)

    alert_emoji = emojis["alert"]
    if discord.is_custom_emoji(alert_emoji):
        # 独自絵文字はembedタイトルでは描画されないため本文の先頭に回す
        lines.insert(0, alert_emoji)
        title = title_text
    else:
        title = f"{alert_emoji} {title_text}"

    embed = {
        "title": title,
        "description": "\n".join(lines),
        "color": discord.COLOR_ALERT_BAD if (btc_change_24h or 0) < 0 else discord.COLOR_ALERT_GOOD,
    }
    return discord.post_webhook(
        webhook_url,
        username=username,
        content=_mention(cfg),
        embeds=[embed],
        dry_run=dry_run,
        min_interval_seconds=cfg["discord"]["post_interval_seconds"],
    )


def run_alert_for_coin(
    *,
    conn,
    coin: str,
    coin_cfg: dict,
    webhook_url: str,
    cfg: dict,
    touched_article_ids: list[int],
    price_data: dict,
    btc_change_24h: float | None,
    hourly_change: float | None,
    onchain_summary: dict | None,
    usd1_price: float | None,
    morpho_summary: dict | None,
    dry_run: bool,
) -> int:
    """1銘柄分の速報を評価・投稿する。投稿件数を返す。"""
    thresholds = cfg["alert_thresholds"]
    max_alerts = thresholds["max_alerts_per_channel_per_run"]
    cooldown_hours = thresholds["price_alert_cooldown_hours"]
    now = datetime.now(timezone.utc)
    username = coin_cfg["username"]
    coin_change_24h = (price_data.get(coin) or {}).get("usd_24h_change")
    posted = 0

    # 1) ニュース速報
    for article_id in touched_article_ids:
        if posted >= max_alerts:
            break
        row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
        if row is None or row["alerted_at"] is not None:
            continue
        coin_rows = conn.execute(
            "SELECT coin FROM article_coins WHERE article_id = ?", (article_id,)
        ).fetchall()
        if coin not in {r["coin"] for r in coin_rows}:
            continue
        source_count = db.count_sources(conn, article_id)
        first_published = datetime.fromisoformat(row["first_published_at"])
        if first_published.tzinfo is None:
            first_published = first_published.replace(tzinfo=timezone.utc)
        hours_since_first = (now - first_published).total_seconds() / 3600
        if hours_since_first > thresholds["news_alert_max_age_hours"]:
            # 今回はじめてDBに保存された記事でも、公開自体が古ければ速報にしない
            # （Google Newsの検索結果に混ざる過去記事の誤検知を防ぐ）
            continue
        critical_hits = row["critical_hits"].split(",") if row["critical_hits"] else []
        reason = evaluate_news_alert(coin, row["display_title"], critical_hits, source_count, hours_since_first, cfg)
        if not reason:
            continue
        ok = _post_news_alert(
            webhook_url=webhook_url, username=username, coin=coin, article_row=row,
            reason=reason, source_count=source_count, price_data=price_data, cfg=cfg, dry_run=dry_run,
        )
        if ok:
            db.mark_alerted(conn, article_id, reason)
            db.log_alert(conn, coin, "news", f"article:{article_id}", reason, now.isoformat())
            posted += 1

    # 2) 価格急変（24h）
    if posted < max_alerts:
        pct24 = (price_data.get(coin) or {}).get("usd_24h_change")
        reason = check_price_change_alert(coin, pct24, cfg)
        if reason and not is_in_cooldown(conn, coin, "price_change", "24h", cooldown_hours, now):
            title_text = f"価格急変 {coin} {fmt_pct(pct24)}（24h）"
            if _post_data_alert(
                webhook_url=webhook_url, username=username, title_text=title_text, reason=reason,
                coin=coin, btc_change_24h=btc_change_24h, coin_change_24h=coin_change_24h,
                cfg=cfg, conn=conn, now=now, dry_run=dry_run,
            ):
                db.log_alert(conn, coin, "price_change", "24h", reason, now.isoformat())
                posted += 1

    # 3) 価格の節目
    if posted < max_alerts:
        levels = coin_cfg.get("price_levels", [])
        if levels:
            prev_row = conn.execute(
                "SELECT usd FROM prices WHERE coin = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
                (coin, now.isoformat()),
            ).fetchone()
            prev_price = prev_row["usd"] if prev_row else None
            curr_price = (price_data.get(coin) or {}).get("usd")
            cross = check_price_level_cross(prev_price, curr_price, levels)
            if cross:
                direction, level = cross
                dedupe_key = f"level:{level}"
                if not is_in_cooldown(conn, coin, "price_level", dedupe_key, cooldown_hours, now):
                    verb = "上抜け" if direction == "up" else "下抜け"
                    title_text = f"{coin} {fmt_level(level)}ドルを{verb}"
                    reason = f"{coin}が{fmt_usd(level)}を{verb}しました（現在値 {fmt_usd(curr_price)}）"
                    if _post_data_alert(
                        webhook_url=webhook_url, username=username, title_text=title_text, reason=reason,
                        coin=coin, btc_change_24h=btc_change_24h, coin_change_24h=coin_change_24h,
                cfg=cfg, conn=conn, now=now, dry_run=dry_run,
                    ):
                        db.log_alert(conn, coin, "price_level", dedupe_key, reason, now.isoformat())
                        posted += 1

    # 4) SOLの追加監視: USD1デペッグ・1時間変化率
    if coin == "SOL" and posted < max_alerts:
        sol_cfg = cfg["sol"]
        usd1_reason = check_usd1_depeg(usd1_price, sol_cfg["usd1_depeg_low"], sol_cfg["usd1_depeg_high"])
        if usd1_reason and not is_in_cooldown(conn, coin, "usd1_depeg", "usd1", cooldown_hours, now):
            if _post_data_alert(
                webhook_url=webhook_url, username=username, title_text="USD1 デペッグ懸念", reason=usd1_reason,
                coin=coin, btc_change_24h=btc_change_24h, coin_change_24h=coin_change_24h,
                cfg=cfg, conn=conn, now=now, dry_run=dry_run,
            ):
                db.log_alert(conn, coin, "usd1_depeg", "usd1", usd1_reason, now.isoformat())
                posted += 1

        if posted < max_alerts:
            hourly_reason = check_hourly_change_alert(coin, hourly_change, sol_cfg["hourly_change_alert_pct"])
            if hourly_reason and not is_in_cooldown(conn, coin, "hourly_change", "1h", cooldown_hours, now):
                title_text = f"価格急変 {coin} {fmt_pct(hourly_change)}（1h）"
                if _post_data_alert(
                    webhook_url=webhook_url, username=username, title_text=title_text, reason=hourly_reason,
                    coin=coin, btc_change_24h=btc_change_24h, coin_change_24h=coin_change_24h,
                cfg=cfg, conn=conn, now=now, dry_run=dry_run,
                ):
                    db.log_alert(conn, coin, "hourly_change", "1h", hourly_reason, now.isoformat())
                    posted += 1

    # 5) ARBのオンチェーン変化
    if coin == "ARB" and posted < max_alerts:
        reason = check_onchain_weekly_change(onchain_summary, cfg["arb"]["onchain_weekly_change_alert_pct"])
        if reason and not is_in_cooldown(conn, coin, "onchain_change", "robinhood_fees_7d", cooldown_hours, now):
            if _post_data_alert(
                webhook_url=webhook_url, username=username, title_text="Robinhood Chain収益の急変", reason=reason,
                coin=coin, btc_change_24h=btc_change_24h, coin_change_24h=coin_change_24h,
                cfg=cfg, conn=conn, now=now, dry_run=dry_run,
            ):
                db.log_alert(conn, coin, "onchain_change", "robinhood_fees_7d", reason, now.isoformat())
                posted += 1

    # 6) WLDのMorpho利用率
    if coin == "WLD" and posted < max_alerts:
        morpho_cfg = cfg["wld"]["morpho"]
        reason = check_morpho_utilization(morpho_summary, morpho_cfg["utilization_alert_pct"])
        if reason and not is_in_cooldown(conn, coin, "morpho_utilization", "wld", cooldown_hours, now):
            if _post_data_alert(
                webhook_url=webhook_url, username=username, title_text="WLD関連Morphoマーケットの利用率上昇", reason=reason,
                coin=coin, btc_change_24h=btc_change_24h, coin_change_24h=coin_change_24h,
                cfg=cfg, conn=conn, now=now, dry_run=dry_run,
            ):
                db.log_alert(conn, coin, "morpho_utilization", "wld", reason, now.isoformat())
                posted += 1

    return posted
