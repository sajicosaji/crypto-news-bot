"""crypto-news-bot エントリポイント。

使い方:
    python main.py --digest       # 毎朝8:00（日次まとめ）
    python main.py --alert        # 30分ごと（速報）
    python main.py --weekly       # 毎週月曜8:00（週次振り返り）
    python main.py --test-webhooks
オプション:
    --dry-run  投稿せずに内容をコンソールに表示する
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src import alerts, config, db, digest, discord, fetch_news, onchain, prices, weekly
from src.utils import now_jst

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "bot.db"
LOG_PATH = BASE_DIR / "logs" / "bot.log"

logger = logging.getLogger("crypto_news_bot")


def setup_logging() -> None:
    # Windows のコンソール既定エンコーディング(cp932)で日本語ログが文字化けするのを防ぐ
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    console_handler = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    file_handler.setFormatter(fmt)
    console_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="crypto-news-bot")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--digest", action="store_true", help="日次まとめを投稿する")
    mode.add_argument("--alert", action="store_true", help="速報をチェックする")
    mode.add_argument("--weekly", action="store_true", help="週次振り返りを投稿する")
    mode.add_argument("--serve", action="store_true", help="自走モード（指定時間ループして各モードを実行）")
    parser.add_argument("--dry-run", action="store_true", help="投稿せず内容を表示する")
    parser.add_argument("--test-webhooks", action="store_true", help="各チャンネルにテスト投稿する")
    parser.add_argument("--max-runtime", type=int, default=330, help="自走モードの最大実行時間（分）")
    args = parser.parse_args()
    if not (args.digest or args.alert or args.weekly or args.serve or args.test_webhooks):
        parser.error("--digest / --alert / --weekly / --serve / --test-webhooks のいずれかを指定してください")
    return args


def active_coins(cfg: dict) -> dict:
    """webhook が設定されている銘柄だけを返す。未設定はログに警告して除外する。"""
    result = {}
    for symbol, coin_cfg in cfg["coins"].items():
        url = config.get_webhook_url(coin_cfg)
        if not url:
            logger.warning("%s のWebhook URLが未設定のためスキップします（%s）", symbol, coin_cfg["webhook_env"])
            continue
        result[symbol] = (coin_cfg, url)
    return result


def run_test_webhooks(cfg: dict, dry_run: bool) -> None:
    coins = active_coins(cfg)
    if not coins:
        logger.warning("有効なWebhookが1つもありません。.env を確認してください。")
        return
    for symbol, (coin_cfg, url) in coins.items():
        ok = discord.post_webhook(
            url,
            username=coin_cfg["username"],
            content=f"✅ {symbol} チャンネルへのテスト投稿です。",
            dry_run=dry_run,
            min_interval_seconds=cfg["discord"]["post_interval_seconds"],
        )
        logger.info("%s テスト投稿: %s", symbol, "成功" if ok else "失敗")


def fetch_and_persist_news(conn, cfg: dict) -> list[tuple[int, bool]]:
    items = fetch_news.fetch_all_news(cfg)
    logger.info("ニュースを%d件取得しました", len(items))
    touched = fetch_news.persist_news_items(conn, items, cfg)
    new_count = sum(1 for _, is_new in touched if is_new)
    logger.info("新規記事%d件、既存記事の媒体追加%d件", new_count, len(touched) - new_count)
    return touched


def build_price_universe(cfg: dict) -> dict:
    ids = {cfg["btc_coingecko_id"]}
    for coin_cfg in cfg["coins"].values():
        ids.add(coin_cfg["coingecko_id"])
    ids.add(cfg["sol"]["usd1_coingecko_id"])
    raw = prices.fetch_prices(list(ids))
    return raw


def to_symbol_price_data(cfg: dict, raw: dict) -> dict:
    price_data = {}
    for symbol, coin_cfg in cfg["coins"].items():
        rec = prices.to_price_record(coin_cfg["coingecko_id"], raw)
        if rec:
            price_data[symbol] = rec
    btc_rec = prices.to_price_record(cfg["btc_coingecko_id"], raw)
    if btc_rec:
        price_data["BTC"] = btc_rec
    return price_data


def fetch_arb_onchain(cfg: dict) -> dict:
    arb_cfg = cfg["arb"]["defillama"]
    rh_fees = onchain.fetch_fees_overview(arb_cfg["robinhood_chain_fees_chain"])
    rh_summary = onchain.summarize_daily_metric(rh_fees)
    rh_dex = onchain.fetch_dex_overview(arb_cfg["robinhood_chain_fees_chain"])
    dex_summary = onchain.summarize_daily_metric(rh_dex)
    arb_fees = onchain.fetch_fees_overview(arb_cfg["arbitrum_fees_chain"])
    arb_summary = onchain.summarize_daily_metric(arb_fees)
    treasury_raw = onchain.fetch_dao_treasury(arb_cfg["dao_treasury_slug"])
    treasury_summary = onchain.summarize_treasury(treasury_raw)
    return {
        "rh_summary": rh_summary,
        "dex_summary": dex_summary,
        "arb_summary": arb_summary,
        "treasury_summary": treasury_summary,
    }


def fetch_wld_onchain(cfg: dict) -> dict | None:
    morpho_cfg = cfg["wld"]["morpho"]
    if not morpho_cfg.get("enabled"):
        return None
    return onchain.summarize_morpho_wld(morpho_cfg["collateral_symbol"], morpho_cfg.get("min_supply_usd", 0))


def save_price_snapshots(conn, cfg: dict, price_data: dict, usd1_price: float | None) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    for symbol, rec in price_data.items():
        db.save_price(conn, symbol, ts, rec.get("usd"), rec.get("jpy"), rec.get("usd_24h_change"))
    if usd1_price is not None:
        db.save_price(conn, "USD1", ts, usd1_price, None, None)


def save_onchain_snapshots(conn, cfg: dict, arb_onchain: dict, wld_onchain: dict | None) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    if arb_onchain.get("rh_summary"):
        db.save_onchain_metric(conn, "ARB", "robinhood_chain_fees_daily", ts, arb_onchain["rh_summary"]["yesterday_value"])
    if arb_onchain.get("arb_summary"):
        db.save_onchain_metric(conn, "ARB", "arbitrum_fees_daily", ts, arb_onchain["arb_summary"]["yesterday_value"])
    if arb_onchain.get("treasury_summary"):
        db.save_onchain_metric(conn, "ARB", "dao_treasury_usd", ts, arb_onchain["treasury_summary"]["current"])
    if wld_onchain:
        db.save_onchain_metric(conn, "WLD", "morpho_utilization", ts, wld_onchain.get("max_utilization"))
        db.save_onchain_metric(conn, "WLD", "morpho_deposited_usd", ts, wld_onchain.get("total_deposited_usd"))


def run_digest(conn, cfg: dict, dry_run: bool) -> None:
    coins = active_coins(cfg)
    fetch_and_persist_news(conn, cfg)
    raw_prices = build_price_universe(cfg)
    price_data = to_symbol_price_data(cfg, raw_prices)
    usd1_price = raw_prices.get(cfg["sol"]["usd1_coingecko_id"], {}).get("usd")
    save_price_snapshots(conn, cfg, price_data, usd1_price)

    arb_onchain = fetch_arb_onchain(cfg)
    wld_onchain = fetch_wld_onchain(cfg)
    save_onchain_snapshots(conn, cfg, arb_onchain, wld_onchain)

    btc_change_24h = (price_data.get("BTC") or {}).get("usd_24h_change")

    for symbol, (coin_cfg, url) in coins.items():
        if symbol == "ARB":
            onchain_field = digest.build_onchain_field_arb(
                arb_onchain["rh_summary"], arb_onchain["arb_summary"],
                arb_onchain["treasury_summary"], arb_onchain["dex_summary"],
            )
        elif symbol == "SOL":
            onchain_field = digest.build_sol_field(usd1_price)
        elif symbol == "WLD":
            onchain_field = digest.build_wld_field(wld_onchain)
        else:
            onchain_field = None

        status = digest.run_digest_for_coin(
            conn=conn, coin=symbol, coin_cfg=coin_cfg, cfg=cfg, webhook_url=url,
            price_data=price_data, btc_change_24h=btc_change_24h, onchain_field=onchain_field,
            dry_run=dry_run,
        )
        label = {"posted": "成功", "skipped": "見送り（ニュース無し）", "failed": "失敗"}[status]
        logger.info("%s 日次まとめ投稿: %s", symbol, label)


def run_alert(conn, cfg: dict, dry_run: bool) -> None:
    coins = active_coins(cfg)

    touched = fetch_and_persist_news(conn, cfg)
    touched_ids = [aid for aid, _ in touched]

    raw_prices = build_price_universe(cfg)
    price_data = to_symbol_price_data(cfg, raw_prices)
    usd1_price = raw_prices.get(cfg["sol"]["usd1_coingecko_id"], {}).get("usd")
    btc_change_24h = (price_data.get("BTC") or {}).get("usd_24h_change")

    is_first_run = db.get_state(conn, "alert_initialized") is None
    if is_first_run:
        logger.info("初回のalert実行のため、保存のみ行い速報は出しません。")
        save_price_snapshots(conn, cfg, price_data, usd1_price)
        arb_onchain = fetch_arb_onchain(cfg)
        wld_onchain = fetch_wld_onchain(cfg)
        save_onchain_snapshots(conn, cfg, arb_onchain, wld_onchain)
        db.set_state(conn, "alert_initialized", "1")
        return

    arb_onchain = fetch_arb_onchain(cfg) if "ARB" in coins else {"rh_summary": None, "dex_summary": None, "arb_summary": None, "treasury_summary": None}
    wld_onchain = fetch_wld_onchain(cfg) if "WLD" in coins else None
    hourly_change = None
    if "SOL" in coins:
        hourly_change = prices.fetch_hourly_change_pct(cfg["coins"]["SOL"]["coingecko_id"], hours=1)

    for symbol, (coin_cfg, url) in coins.items():
        posted = alerts.run_alert_for_coin(
            conn=conn, coin=symbol, coin_cfg=coin_cfg, webhook_url=url, cfg=cfg,
            touched_article_ids=touched_ids, price_data=price_data, btc_change_24h=btc_change_24h,
            hourly_change=hourly_change if symbol == "SOL" else None,
            onchain_summary=arb_onchain.get("rh_summary") if symbol == "ARB" else None,
            usd1_price=usd1_price if symbol == "SOL" else None,
            morpho_summary=wld_onchain if symbol == "WLD" else None,
            dry_run=dry_run,
        )
        logger.info("%s 速報投稿件数: %d", symbol, posted)

    save_price_snapshots(conn, cfg, price_data, usd1_price)
    save_onchain_snapshots(conn, cfg, arb_onchain, wld_onchain)


def run_weekly(conn, cfg: dict, dry_run: bool) -> None:
    coins = active_coins(cfg)

    raw_prices = build_price_universe(cfg)
    price_data = to_symbol_price_data(cfg, raw_prices)
    usd1_price = raw_prices.get(cfg["sol"]["usd1_coingecko_id"], {}).get("usd")
    save_price_snapshots(conn, cfg, price_data, usd1_price)

    arb_fees = onchain.fetch_fees_overview(cfg["arb"]["defillama"]["robinhood_chain_fees_chain"])
    rh_summary = onchain.summarize_daily_metric(arb_fees)
    trend_lines = []
    if rh_summary:
        weekly_totals = onchain.weekly_totals_from_series(rh_summary["series"], cfg["weekly"]["onchain_weeks"])
        if weekly_totals:
            trend_lines.append("Robinhood Chain収益の週次推移:")
            for w in weekly_totals:
                trend_lines.append(f"・{w['week_ending']}週: {w['total']:,.0f}ドル")

    for symbol, (coin_cfg, url) in coins.items():
        ok = weekly.run_weekly_for_coin(
            conn=conn, coin=symbol, coin_cfg=coin_cfg, cfg=cfg, webhook_url=url,
            onchain_trend_lines=trend_lines if symbol == "ARB" else [],
            dry_run=dry_run,
        )
        logger.info("%s 週次振り返り投稿: %s", symbol, "成功" if ok else "失敗")


def should_run_digest(conn, cfg: dict, now) -> bool:
    sched = cfg.get("schedule", {})
    hour = sched.get("digest_hour_jst", 8)
    window = sched.get("digest_window_hours", 6)
    if not (hour <= now.hour < hour + window):
        return False
    return db.get_state(conn, "last_digest_date") != now.date().isoformat()


def should_run_weekly(conn, cfg: dict, now) -> bool:
    sched = cfg.get("schedule", {})
    hour = sched.get("digest_hour_jst", 8)
    window = sched.get("digest_window_hours", 6)
    if now.weekday() != sched.get("weekly_weekday", 0):
        return False
    if not (hour <= now.hour < hour + window):
        return False
    iso = now.isocalendar()
    return db.get_state(conn, "last_weekly_week") != f"{iso.year}-W{iso.week}"


def run_serve(conn, cfg: dict, dry_run: bool, max_runtime_minutes: int) -> None:
    """1つのジョブの中で、自分の時計を見て各モードを実行し続ける。

    GitHubのスケジュール実行は発火しないことが多いため、外部から起動された
    長時間ジョブがこのループを回して速報・日次まとめ・週次振り返りを担当する。
    """
    sched = cfg.get("schedule", {})
    interval_seconds = sched.get("alert_interval_minutes", 30) * 60
    deadline = time.monotonic() + max_runtime_minutes * 60
    logger.info("自走モードを開始します（最大%d分、%d分間隔）", max_runtime_minutes, interval_seconds // 60)

    while True:
        now = now_jst()
        try:
            if should_run_digest(conn, cfg, now):
                logger.info("日次まとめの時刻になりました（%s）", now.strftime("%m/%d %H:%M"))
                run_digest(conn, cfg, dry_run)
                if not dry_run:
                    # dry-runでは「投稿済み」と記録しない（本番の投稿を潰さないため）
                    db.set_state(conn, "last_digest_date", now.date().isoformat())

            if should_run_weekly(conn, cfg, now):
                logger.info("週次振り返りの時刻になりました（%s）", now.strftime("%m/%d %H:%M"))
                run_weekly(conn, cfg, dry_run)
                if not dry_run:
                    iso = now.isocalendar()
                    db.set_state(conn, "last_weekly_week", f"{iso.year}-W{iso.week}")

            run_alert(conn, cfg, dry_run)
        except Exception:
            # 1回の失敗でループ全体を止めない（次の巡回で自然に復帰する）
            logger.exception("巡回中にエラーが発生しました。次の巡回で再試行します。")

        remaining = deadline - time.monotonic()
        if remaining <= interval_seconds:
            logger.info("自走モードの時間が終了しました。次のジョブに引き継ぎます。")
            return
        time.sleep(interval_seconds)


def main() -> None:
    args = parse_args()
    setup_logging()
    config.load_env()
    cfg = config.load_config()

    if args.test_webhooks:
        run_test_webhooks(cfg, args.dry_run)
        return

    conn = db.connect(DB_PATH)
    try:
        if args.digest:
            run_digest(conn, cfg, args.dry_run)
        elif args.alert:
            run_alert(conn, cfg, args.dry_run)
        elif args.weekly:
            run_weekly(conn, cfg, args.dry_run)
        elif args.serve:
            run_serve(conn, cfg, args.dry_run, args.max_runtime)

        retention_days = cfg.get("data_retention_days", 30)
        removed = db.prune_old_data(conn, retention_days)
        if removed["articles"]:
            logger.info("%d日より古い記事%d件を整理しました", retention_days, removed["articles"])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
