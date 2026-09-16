"""日次まとめ（--digest）の生成・投稿。"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from . import db, discord
from .advice import build_reading_advice, priority_rank
from .formatting import fmt_jpy, fmt_pct, fmt_usd, fmt_usd_compact
from .utils import JST, now_jst

logger = logging.getLogger("crypto_news_bot.digest")


def yesterday_utc_bounds(today_jst: date) -> tuple[str, str]:
    from datetime import timezone

    start_jst = datetime.combine(today_jst - timedelta(days=1), time.min, tzinfo=JST)
    end_jst = datetime.combine(today_jst, time.min, tzinfo=JST)
    return start_jst.astimezone(timezone.utc).isoformat(), end_jst.astimezone(timezone.utc).isoformat()


def fetch_yesterday_articles(conn, coin: str, today_jst: date) -> list[dict]:
    start_iso, end_iso = yesterday_utc_bounds(today_jst)
    rows = conn.execute(
        "SELECT a.* FROM articles a JOIN article_coins ac ON ac.article_id = a.id "
        "WHERE ac.coin = ? AND a.first_published_at >= ? AND a.first_published_at < ? "
        "ORDER BY a.first_published_at DESC",
        (coin, start_iso, end_iso),
    ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["source_count"] = db.count_sources(conn, row["id"])
        result.append(d)
    return result


def upcoming_unlocks(unlocks: list[dict], today: date) -> list[str]:
    notes = []
    for u in unlocks:
        try:
            d = date.fromisoformat(u["date"])
        except (KeyError, ValueError):
            continue
        delta = (d - today).days
        if delta == 3:
            notes.append(f"{u['date']}（3日後）: {u.get('amount', '')} {u.get('description', '')}")
        elif delta == 1:
            notes.append(f"{u['date']}（明日）: {u.get('amount', '')} {u.get('description', '')}")
    return notes


def macro_events_for_digest(macro_events: list[dict], today: date) -> list[str]:
    notes = []
    tomorrow = today + timedelta(days=1)
    for e in macro_events:
        try:
            d = date.fromisoformat(e["date"])
        except (KeyError, ValueError):
            continue
        if d == today:
            notes.append(f"本日: {e.get('description', '')}")
        elif d == tomorrow:
            notes.append(f"明日: {e.get('description', '')}")
    return notes


def annotate_articles(articles: list[dict], cfg: dict, price_change_24h: float | None) -> list[dict]:
    """各記事に「読む価値」の優先度とアドバイス文を付ける。"""
    for a in articles:
        critical_hits = (a.get("critical_hits") or "").split(",") if a.get("critical_hits") else []
        priority, text = build_reading_advice(
            critical_hits=critical_hits,
            source_count=a.get("source_count", 1),
            score=a.get("score", 0),
            is_dao_proposal=a.get("display_title", "").startswith("[DAO提案]"),
            price_change_24h=price_change_24h,
            cfg=cfg,
        )
        a["priority"] = priority
        a["advice"] = text
    return articles


def sort_news_for_digest(articles: list[dict]) -> list[dict]:
    """読む価値の高い順 → 本文抜粋がある順 → 報道媒体数の多い順。"""
    return sorted(
        articles,
        key=lambda a: (
            priority_rank(a.get("priority", "低")),
            0 if (a.get("excerpt") or "").strip() else 1,
            -a.get("source_count", 1),
        ),
    )


def build_news_lines(articles: list[dict], cfg: dict, char_budget: int = 3800) -> list[str]:
    """上位は抜粋＋アドバイス付き、残りは1行のリストで組み立てる。

    Discordの埋め込み本文には文字数上限があるため、予算を超える分は打ち切る。
    """
    digest_cfg = cfg.get("digest", {})
    max_items = digest_cfg.get("max_news_items", 10)
    detailed_items = digest_cfg.get("detailed_items", 5)
    advice_enabled = cfg.get("reading_advice", {}).get("enabled", True)

    ordered = sort_news_for_digest(articles)[:max_items]

    # 詳しく載せる枠は、本文抜粋が取れている記事を優先して埋める
    # （Google News経由の記事は本文が取れないため、見出しだけのリスト行に回す）
    with_excerpt = [a for a in ordered if (a.get("excerpt") or "").strip()]
    detailed_ids = {id(a) for a in with_excerpt[:detailed_items]}

    lines: list[str] = []
    used = 0

    for a in ordered:
        prefix = "🚨" if a.get("alerted_at") else ""
        url = a.get("excerpt_url") or a["url"]
        source = a.get("excerpt_source") or a["first_source"]
        block: list[str] = []

        if id(a) in detailed_ids:
            block.append(f"{prefix}{a['emoji']} **[{a['display_title']}]({url})** - {source}")
            excerpt = (a.get("excerpt") or "").strip()
            if excerpt:
                block.append(f"　{excerpt}")
            if advice_enabled:
                block.append(f"　👉 {a['advice']}")
            block.append("")
        else:
            block.append(f"{prefix}{a['emoji']} [{a['display_title']}]({url}) - {source}")

        block_len = sum(len(line) + 1 for line in block)
        if used + block_len > char_budget:
            break
        lines.extend(block)
        used += block_len

    return lines


def good_bad_color(articles: list[dict], cfg: dict) -> int:
    good = sum(1 for a in articles if a["sentiment"] == "good")
    bad = sum(1 for a in articles if a["sentiment"] == "bad")
    if good > bad:
        return discord.COLOR_GOOD
    if bad > good:
        return discord.COLOR_BAD
    return discord.COLOR_NEUTRAL


def build_price_field(coin: str, price_data: dict, btc_change_24h: float | None) -> str:
    p = price_data.get(coin, {})
    lines = [
        f"{fmt_usd(p.get('usd'))} / {fmt_jpy(p.get('jpy'))}",
        f"24h変化率: {fmt_pct(p.get('usd_24h_change'))}（BTC: {fmt_pct(btc_change_24h)}）",
    ]
    return "\n".join(lines)


def build_onchain_field_arb(
    rh_summary: dict | None,
    arb_summary: dict | None,
    treasury_summary: dict | None,
    dex_summary: dict | None = None,
) -> str | None:
    if not rh_summary and not arb_summary and not treasury_summary:
        return None
    lines = []
    if rh_summary:
        lines.append(
            f"Robinhood Chain日次収益: {fmt_usd_compact(rh_summary['yesterday_value'])}"
            f"（前日比 {fmt_pct(rh_summary['dod_change_pct'])}）"
        )
        lines.append(
            f"　7日平均: {fmt_usd_compact(rh_summary['avg7'])}"
            f"（前週7日平均比 {fmt_pct(rh_summary['week_change_pct'])}）"
        )
    if dex_summary:
        lines.append(
            f"Robinhood Chain DEX出来高: {fmt_usd_compact(dex_summary['yesterday_value'])}"
            f"（前日比 {fmt_pct(dex_summary['dod_change_pct'])}）"
        )
    if arb_summary:
        lines.append(
            f"Arbitrum One日次収益: {fmt_usd_compact(arb_summary['yesterday_value'])}"
            f"（前日比 {fmt_pct(arb_summary['dod_change_pct'])}）"
        )
    if treasury_summary:
        lines.append(f"Arbitrum DAOトレジャリー残高（概算）: {fmt_usd_compact(treasury_summary['current'])}")
    return "\n".join(lines)


def build_sol_field(usd1_price: float | None) -> str | None:
    if usd1_price is None:
        return None
    return f"USD1価格: {fmt_usd(usd1_price)}"


def build_wld_field(morpho_summary: dict | None) -> str | None:
    if not morpho_summary:
        return None
    primary = morpho_summary["primary"]
    util_pct = (primary.get("utilization") or 0) * 100
    apy_pct = (primary.get("supply_apy") or 0) * 100
    return (
        f"Morpho WLDマーケット 利用率: {util_pct:.1f}% / 供給APY: {apy_pct:.2f}%\n"
        f"預け入れ総額(担保): {fmt_usd_compact(morpho_summary['total_deposited_usd'])}"
    )


def build_digest_embed(
    *,
    coin: str,
    coin_cfg: dict,
    cfg: dict,
    price_data: dict,
    btc_change_24h: float | None,
    articles: list[dict],
    onchain_field: str | None,
    schedule_notes: list[str],
    today: date,
) -> dict:
    fields = [{"name": "価格", "value": build_price_field(coin, price_data, btc_change_24h), "inline": False}]
    if onchain_field:
        fields.append({"name": "指標", "value": onchain_field, "inline": False})
    if schedule_notes:
        fields.append({"name": "予定", "value": "\n".join(schedule_notes), "inline": False})

    if articles:
        annotate_articles(articles, cfg, (price_data.get(coin) or {}).get("usd_24h_change"))
        news_lines = build_news_lines(articles, cfg)
        description = "\n".join(news_lines).strip()
    else:
        description = "本日の主要ニュースはありません"

    return {
        "title": f"{coin} 日次まとめ - {today.isoformat()}",
        "description": description,
        "color": good_bad_color(articles, cfg),
        "fields": fields,
        "timestamp": datetime.now(JST).isoformat(),
    }


def run_digest_for_coin(
    *,
    conn,
    coin: str,
    coin_cfg: dict,
    cfg: dict,
    webhook_url: str,
    price_data: dict,
    btc_change_24h: float | None,
    onchain_field: str | None,
    dry_run: bool,
) -> bool:
    today = now_jst().date()
    articles = fetch_yesterday_articles(conn, coin, today)

    schedule_notes = list(macro_events_for_digest(cfg.get("macro_events", []), today))
    unlocks = cfg.get(coin.lower(), {}).get("unlocks", [])
    schedule_notes.extend(upcoming_unlocks(unlocks, today))

    embed = build_digest_embed(
        coin=coin, coin_cfg=coin_cfg, cfg=cfg, price_data=price_data,
        btc_change_24h=btc_change_24h, articles=articles, onchain_field=onchain_field,
        schedule_notes=schedule_notes, today=today,
    )
    return discord.post_webhook(
        webhook_url,
        username=coin_cfg["username"],
        embeds=[embed],
        dry_run=dry_run,
        min_interval_seconds=cfg["discord"]["post_interval_seconds"],
    )
