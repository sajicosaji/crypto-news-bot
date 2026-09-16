"""CoinGecko 無料APIから価格を取得する。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger("crypto_news_bot.prices")

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
REQUEST_TIMEOUT = 15


def fetch_prices(coingecko_ids: list[str]) -> dict[str, dict]:
    """複数銘柄（+ BTC + USD1）の価格を1回のリクエストでまとめて取る。

    戻り値: {coingecko_id: {"usd": ..., "jpy": ..., "usd_24h_change": ...}}
    失敗時は空の dict を返し、呼び出し側でログと欠損扱いにする。
    """
    if not coingecko_ids:
        return {}
    ids_param = ",".join(sorted(set(coingecko_ids)))
    url = f"{COINGECKO_BASE}/simple/price"
    params = {
        "ids": ids_param,
        "vs_currencies": "usd,jpy",
        "include_24hr_change": "true",
    }
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.warning("CoinGecko価格取得に失敗しました ids=%s error=%s", ids_param, e)
        return {}


def fetch_hourly_change_pct(coingecko_id: str, hours: int = 1) -> float | None:
    """直近 `hours` 時間の変化率(%)。market_chart の時系列から算出する。"""
    url = f"{COINGECKO_BASE}/coins/{coingecko_id}/market_chart"
    params = {"vs_currency": "usd", "days": "1"}
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.warning("CoinGecko時系列取得に失敗しました id=%s error=%s", coingecko_id, e)
        return None

    prices = data.get("prices", [])
    if len(prices) < 2:
        return None
    now_ts, now_price = prices[-1]
    target_ts = now_ts - hours * 3600 * 1000
    # target_ts に最も近い過去の価格を探す
    candidate = None
    for ts, price in prices:
        if ts <= target_ts:
            candidate = price
        else:
            break
    if candidate is None:
        candidate = prices[0][1]
    if not candidate:
        return None
    return (now_price - candidate) / candidate * 100


def to_price_record(coingecko_id: str, data: dict) -> dict | None:
    entry = data.get(coingecko_id)
    if not entry:
        return None
    return {
        "usd": entry.get("usd"),
        "jpy": entry.get("jpy"),
        "usd_24h_change": entry.get("usd_24h_change"),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
