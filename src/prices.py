"""価格を取得する（CoinPaprika の無料API。APIキー不要）。

2026-09-29 から CoinGecko がキー無しの価格リクエストを 403 で拒否するようになったため
（ping だけ200、simple/price は手元でもクラウドでも403）、CoinPaprika に切り替えた。
設定ファイルの `coingecko_id` はそのまま銘柄の識別子として使い、ここで CoinPaprika の id に
読み替える。対応表は 2026-09-30 に実際にリクエストして価格を確認済み
（WLD は Coinbase の WLD-USD と一致、USD1 は約$1.00）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger("crypto_news_bot.prices")

PAPRIKA_BASE = "https://api.coinpaprika.com/v1"
COINBASE_BASE = "https://api.exchange.coinbase.com"
REQUEST_TIMEOUT = 15

# 設定上の識別子（旧CoinGecko id）→ CoinPaprika の id
PAPRIKA_IDS = {
    "bitcoin": "btc-bitcoin",
    "ethereum": "eth-ethereum",
    "solana": "sol-solana",
    "arbitrum": "arb-arbitrum",
    "worldcoin-wld": "wld-worldcoin",
    "usd1-wlfi": "usd1-usd1",
    "optimism": "op-optimism",
    "zksync": "zk-zksync",
    "starknet": "strk-starknet",
}


def _fetch_ticker(paprika_id: str) -> dict | None:
    try:
        resp = requests.get(
            f"{PAPRIKA_BASE}/tickers/{paprika_id}", params={"quotes": "USD,JPY"}, timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        quotes = resp.json().get("quotes") or {}
    except (requests.RequestException, ValueError) as e:
        logger.warning("価格取得に失敗しました id=%s error=%s", paprika_id, e)
        return None
    usd, jpy = quotes.get("USD") or {}, quotes.get("JPY") or {}
    if usd.get("price") is None:
        return None
    return {
        "usd": usd.get("price"),
        "jpy": jpy.get("price"),
        "usd_24h_change": usd.get("percent_change_24h"),
        "usd_1h_change": usd.get("percent_change_1h"),
    }


def fetch_prices(coingecko_ids: list[str]) -> dict[str, dict]:
    """複数銘柄の価格。

    戻り値: {設定上のid: {"usd": ..., "jpy": ..., "usd_24h_change": ..., "usd_1h_change": ...}}
    取れなかった銘柄は含めない（呼び出し側で欠損扱いにする）。
    """
    result: dict[str, dict] = {}
    for cid in sorted(set(coingecko_ids)):
        paprika_id = PAPRIKA_IDS.get(cid)
        if not paprika_id:
            logger.warning("価格の取得先が未設定の銘柄です id=%s", cid)
            continue
        ticker = _fetch_ticker(paprika_id)
        if ticker:
            result[cid] = ticker
    return result


def fetch_hourly_change_pct(coingecko_id: str, hours: int = 1) -> float | None:
    """直近1時間の変化率(%)。"""
    ticker = _fetch_ticker(PAPRIKA_IDS.get(coingecko_id, coingecko_id))
    return ticker.get("usd_1h_change") if ticker else None


def fetch_hourly_candles(symbol: str, hours: int = 48) -> list[tuple[datetime, float]]:
    """直近の1時間足の終値（Coinbase の公開API）。古い順。取れなければ空。"""
    try:
        resp = requests.get(
            f"{COINBASE_BASE}/products/{symbol}-USD/candles",
            params={"granularity": 3600}, timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("時間足の取得に失敗しました symbol=%s error=%s", symbol, e)
        return []
    # [time, low, high, open, close, volume] が新しい順で返る
    points = [(datetime.fromtimestamp(r[0], tz=timezone.utc), float(r[4])) for r in rows[:hours + 1]]
    return list(reversed(points))


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
