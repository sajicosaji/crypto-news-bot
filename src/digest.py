"""日次まとめ（--digest）の生成・投稿。"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone

from . import db, discord, summarize
from .advice import build_reading_advice, meets_minimum, priority_rank
from .move_context import analyze_move, direction_label, pick_move_context
from .formatting import fmt_jpy, fmt_pct, fmt_usd, fmt_usd_compact
from .utils import JST, is_same_story, normalize_title, now_jst

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


def drop_similar_for_display(articles: list[dict], min_shared_terms: int) -> list[dict]:
    """同じまとめの中に、同じ話題の記事を並べない。

    DB上は別記事のまま残す（統合を緩めると別のニュースまで飲み込むため）。
    表示から外すだけなので、誤って同一視しても記事自体は失われない。
    先に来たもの（＝読む価値の高い順で上位）を残す。
    """
    if min_shared_terms <= 0:
        return articles
    kept: list[dict] = []
    for article in articles:
        title = article.get("normalized_title") or normalize_title(article.get("display_title", ""))
        if any(
            is_same_story(title, k.get("normalized_title") or normalize_title(k.get("display_title", "")), min_shared_terms)
            for k in kept
        ):
            continue
        kept.append(article)
    return kept


def build_news_lines(articles: list[dict], cfg: dict, char_budget: int = 3800, conn=None) -> list[str]:
    """上位は抜粋＋アドバイス付き、残りは1行のリストで組み立てる。

    Discordの埋め込み本文には文字数上限があるため、予算を超える分は打ち切る。
    """
    digest_cfg = cfg.get("digest", {})
    max_items = digest_cfg.get("max_news_items", 10)
    detailed_items = digest_cfg.get("detailed_items", 5)
    advice_enabled = cfg.get("reading_advice", {}).get("enabled", True)

    ordered = sort_news_for_digest(articles)
    ordered = drop_similar_for_display(
        ordered, cfg.get("dedup", {}).get("display_min_shared_terms", 2)
    )[:max_items]

    # 詳しく載せる枠は、要約か本文抜粋がある記事を優先して埋める
    # （本文に到達できない記事は、見出しだけのリスト行に回す）
    # 読む価値が低い記事は、要約もアドバイスも付けず見出し1行だけにする
    minimum = cfg.get("reading_advice", {}).get("minimum_priority", "中")
    with_body = [
        a for a in ordered
        if (a.get("summary") or a.get("excerpt") or "").strip()
        and meets_minimum(a.get("priority", "低"), minimum)
    ]
    detailed = with_body[:detailed_items]
    detailed_ids = {id(a) for a in detailed}

    # 実際に詳しく載せる記事だけを要約する（表示しない記事に課金しない）
    if conn is not None:
        summarize.summarize_pending_articles(conn, detailed, cfg)

    lines: list[str] = []
    used = 0

    for a in ordered:
        prefix = "🚨" if a.get("alerted_at") else ""
        url = a.get("excerpt_url") or a["url"]
        source = a.get("excerpt_source") or a["first_source"]
        block: list[str] = []

        if id(a) in detailed_ids:
            block.append(f"{prefix}{a['emoji']} **[{a['display_title']}]({url})** - {source}")
            # 日本語要約があればそれを、無ければ媒体配信の抜粋を載せる
            summary = (a.get("summary") or "").strip()
            if summary:
                for line in summary.splitlines():
                    if line.strip():
                        block.append(f"　{line.strip()}")
            elif (a.get("excerpt") or "").strip():
                block.append(f"　{a['excerpt'].strip()}")
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


def fetch_articles_for_move_context(conn, coin: str, hours: int = 30) -> list[dict]:
    """値動きの背景候補にする記事。

    24時間の変化率を説明するため、「昨日の暦日」ではなく直近の時間幅で見る
    （今朝出た記事も候補に入れるため、少し広めに取る）。
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return db.recent_articles_with_source_count(conn, coin, since)


def build_move_context_field(
    *, cfg: dict, articles: list[dict], coin_change_24h: float | None, btc_change_24h: float | None
) -> dict | None:
    """値動きの背景になりそうなニュースを1つのフィールドにまとめる。

    BTCにつられただけの動きなら、その旨だけを書いてニュースは挙げない。
    """
    move_cfg = cfg.get("move_context", {})
    if not move_cfg.get("enabled", True) or coin_change_24h is None:
        return None

    analysis = analyze_move(coin_change_24h, btc_change_24h, move_cfg.get("coin_specific_pct", 3))

    # BTCにつられただけの動きは特筆することが無いので、欄ごと出さない
    if not analysis["is_coin_specific"]:
        return None

    lines = [analysis["summary"]]

    if analysis["is_coin_specific"]:
        picked = pick_move_context(
            articles, analysis["direction"], datetime.now(timezone.utc),
            move_cfg.get("max_items", 3),
        )
        # 該当が無ければ何も書かない（「見つかりませんでした」は書かない）
        if picked:
            label = direction_label(analysis["direction"])
            lines.append("")
            lines.append(f"{label}の背景になりそうなニュース（原因と断定するものではありません）:")
            for a in picked:
                url = a.get("excerpt_url") or a["url"]
                lines.append(f"{a['emoji']} [{a['display_title']}]({url})")

    value = "\n".join(lines)
    # Discordのフィールド値は1024文字まで
    if len(value) > 1000:
        value = value[:1000].rstrip() + "…"
    return {"name": "値動きの背景", "value": value, "inline": False}


def build_digest_embed(
    *,
    coin: str,
    coin_cfg: dict,
    cfg: dict,
    price_data: dict,
    btc_change_24h: float | None,
    articles: list[dict],
    move_articles: list[dict],
    onchain_field: str | None,
    schedule_notes: list[str],
    today: date,
    conn=None,
) -> dict:
    fields = [{"name": "価格", "value": build_price_field(coin, price_data, btc_change_24h), "inline": False}]

    move_field = build_move_context_field(
        cfg=cfg,
        articles=move_articles,
        coin_change_24h=(price_data.get(coin) or {}).get("usd_24h_change"),
        btc_change_24h=btc_change_24h,
    )
    if move_field:
        fields.append(move_field)

    if onchain_field:
        fields.append({"name": "指標", "value": onchain_field, "inline": False})
    if schedule_notes:
        fields.append({"name": "予定", "value": "\n".join(schedule_notes), "inline": False})

    if articles:
        annotate_articles(articles, cfg, (price_data.get(coin) or {}).get("usd_24h_change"))
        news_lines = build_news_lines(articles, cfg, conn=conn)
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
) -> str:
    """日次まとめを投稿する。戻り値は "posted" / "skipped" / "failed"。"""
    today = now_jst().date()
    articles = fetch_yesterday_articles(conn, coin, today)

    digest_cfg = cfg.get("digest", {})
    always_post = digest_cfg.get("always_post", [])
    if not articles and digest_cfg.get("skip_when_no_news") and coin not in always_post:
        # 静かな日に空の投稿を並べない（always_post の銘柄は必ず投稿する）
        logger.info("%s はニュースが無いため日次まとめを見送ります", coin)
        return "skipped"

    move_articles = fetch_articles_for_move_context(conn, coin)

    schedule_notes = list(macro_events_for_digest(cfg.get("macro_events", []), today))
    unlocks = cfg.get(coin.lower(), {}).get("unlocks", [])
    schedule_notes.extend(upcoming_unlocks(unlocks, today))

    embed = build_digest_embed(
        coin=coin, coin_cfg=coin_cfg, cfg=cfg, price_data=price_data,
        btc_change_24h=btc_change_24h, articles=articles, move_articles=move_articles,
        onchain_field=onchain_field,
        schedule_notes=schedule_notes, today=today, conn=conn,
    )
    ok = discord.post_webhook(
        webhook_url,
        username=coin_cfg["username"],
        embeds=[embed],
        dry_run=dry_run,
        min_interval_seconds=cfg["discord"]["post_interval_seconds"],
    )
    return "posted" if ok else "failed"
