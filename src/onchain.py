"""DefiLlama / Morpho の無料APIからオンチェーンデータを取得する。

エンドポイントは実装前に実際にリクエストして仕様を確認済み。
World Network の利用状況（認証済み人数など）は公開APIが見つからなかったため、
`fetch_world_network_stats` は常に None を返す無効化状態にしてある（README参照）。
"""
from __future__ import annotations

import logging
import urllib.parse
from datetime import datetime, timezone

import requests

logger = logging.getLogger("crypto_news_bot.onchain")

DEFILLAMA_BASE = "https://api.llama.fi"
MORPHO_GRAPHQL_URL = "https://blue-api.morpho.org/graphql"
REQUEST_TIMEOUT = 20


def _get_json(url: str) -> dict | None:
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.warning("DefiLlama取得に失敗しました url=%s error=%s", url, e)
        return None


def fetch_fees_overview(chain: str) -> dict | None:
    """https://api.llama.fi/overview/fees/{chain} を取得する。"""
    url = f"{DEFILLAMA_BASE}/overview/fees/{urllib.parse.quote(chain)}"
    return _get_json(url)


def fetch_dex_overview(chain: str) -> dict | None:
    """https://api.llama.fi/overview/dexs/{chain} を取得する。"""
    url = f"{DEFILLAMA_BASE}/overview/dexs/{urllib.parse.quote(chain)}"
    return _get_json(url)


def fetch_dao_treasury(slug: str) -> dict | None:
    """https://api.llama.fi/treasury/{slug} を取得する。"""
    url = f"{DEFILLAMA_BASE}/treasury/{urllib.parse.quote(slug)}"
    return _get_json(url)


def _daily_series_from_chart(chart: list[list[float]]) -> list[tuple[datetime, float]]:
    series = []
    for ts, value in chart:
        series.append((datetime.fromtimestamp(ts, tz=timezone.utc), value))
    series.sort(key=lambda x: x[0])
    return series


def summarize_daily_metric(overview: dict | None) -> dict | None:
    """totalDataChart から前日値・前日比・7日平均・前週7日平均を算出する。

    DefiLlamaの日次バケットはUTC基準のため、JSTの「前日」とは厳密には一致しない
    （最大で数時間のズレが生じうる）簡易近似である。
    """
    if not overview:
        return None
    chart = overview.get("totalDataChart") or []
    if len(chart) < 2:
        return None
    series = _daily_series_from_chart(chart)

    now_utc = datetime.now(timezone.utc)
    last_dt, _ = series[-1]
    # 当日分が未確定（今日の日付）なら除外し、直近の確定済み日次だけを使う
    if last_dt.date() == now_utc.date():
        series = series[:-1]
    if len(series) < 2:
        return None

    yesterday_dt, yesterday_value = series[-1]
    day_before_value = series[-2][1] if len(series) >= 2 else None
    dod_change_pct = None
    if day_before_value:
        dod_change_pct = (yesterday_value - day_before_value) / day_before_value * 100

    last7 = series[-7:] if len(series) >= 7 else series
    avg7 = sum(v for _, v in last7) / len(last7)

    prev7 = series[-14:-7] if len(series) >= 14 else []
    avg_prev7 = sum(v for _, v in prev7) / len(prev7) if prev7 else None
    week_change_pct = None
    if avg_prev7:
        week_change_pct = (avg7 - avg_prev7) / avg_prev7 * 100

    return {
        "date": yesterday_dt.date().isoformat(),
        "yesterday_value": yesterday_value,
        "dod_change_pct": dod_change_pct,
        "avg7": avg7,
        "avg_prev7": avg_prev7,
        "week_change_pct": week_change_pct,
        "series": series,
    }


def weekly_totals_from_series(series: list[tuple[datetime, float]], weeks: int = 4) -> list[dict]:
    """日次シリーズから直近 `weeks` 週分の週間合計を算出する（古い週から順）。"""
    results = []
    n = len(series)
    for w in range(weeks):
        end_idx = n - w * 7
        start_idx = end_idx - 7
        if start_idx < 0:
            break
        chunk = series[start_idx:end_idx]
        if not chunk:
            break
        total = sum(v for _, v in chunk)
        results.append({"weeks_ago": w, "total": total, "week_ending": chunk[-1][0].date().isoformat()})
    results.reverse()
    return results


def _tvl_series_dict(tvl_entries: list[dict]) -> dict[int, float]:
    return {int(e["date"]): e.get("totalLiquidityUSD", 0.0) or 0.0 for e in tvl_entries}


def _value_at_or_before(series: dict[int, float], target_ts: int) -> float | None:
    candidates = [ts for ts in series if ts <= target_ts]
    if not candidates:
        return None
    return series[max(candidates)]


TREASURY_COMPONENT_KEYS = ("Arbitrum", "Arbitrum Nova", "Ethereum", "OwnTokens")


def treasury_total_at(treasury_data: dict, target_ts: int) -> float | None:
    """DAOトレジャリーの合計を概算する（chainTvlsの内訳を合算。参考値）。"""
    chain_tvls = treasury_data.get("chainTvls", {})
    total = 0.0
    found_any = False
    for key in TREASURY_COMPONENT_KEYS:
        entry = chain_tvls.get(key)
        if not entry:
            continue
        series = _tvl_series_dict(entry.get("tvl", []))
        value = _value_at_or_before(series, target_ts)
        if value is not None:
            total += value
            found_any = True
    return total if found_any else None


def summarize_treasury(treasury_data: dict | None, weeks_back: int = 4) -> dict | None:
    if not treasury_data:
        return None
    now_ts = int(datetime.now(timezone.utc).timestamp())
    current = treasury_total_at(treasury_data, now_ts)
    if current is None:
        return None
    history = []
    for w in range(weeks_back, -1, -1):
        ts = now_ts - w * 7 * 86400
        value = treasury_total_at(treasury_data, ts)
        history.append({"weeks_ago": w, "value": value})
    return {"current": current, "history": history}


def _morpho_query(query: str, variables: dict | None = None) -> dict | None:
    try:
        resp = requests.post(
            MORPHO_GRAPHQL_URL,
            json={"query": query, "variables": variables or {}},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.warning("Morpho API取得に失敗しました error=%s", e)
        return None
    if "errors" in data:
        logger.warning("Morpho APIがエラーを返しました error=%s", data["errors"])
        return None
    return data.get("data")


MORPHO_MARKETS_QUERY = """
query WldMarkets($search: String!) {
  markets(first: 20, where: { search: $search }) {
    items {
      marketId
      collateralAsset { symbol }
      loanAsset { symbol }
      state {
        utilization
        supplyApy
        borrowApy
        supplyAssetsUsd
        collateralAssetsUsd
      }
    }
  }
}
"""


def fetch_morpho_markets(collateral_symbol: str, min_supply_usd: float = 0) -> list[dict]:
    data = _morpho_query(MORPHO_MARKETS_QUERY, {"search": collateral_symbol})
    if not data:
        return []
    items = data.get("markets", {}).get("items", [])
    result = []
    for item in items:
        collateral = item.get("collateralAsset") or {}
        if collateral.get("symbol") != collateral_symbol:
            continue
        state = item.get("state") or {}
        collateral_usd = state.get("collateralAssetsUsd") or 0
        if collateral_usd < min_supply_usd:
            continue
        result.append(
            {
                "market_id": item.get("marketId"),
                "loan_asset": (item.get("loanAsset") or {}).get("symbol"),
                "utilization": state.get("utilization"),
                "supply_apy": state.get("supplyApy"),
                "borrow_apy": state.get("borrowApy"),
                "supply_assets_usd": state.get("supplyAssetsUsd"),
                "collateral_assets_usd": collateral_usd,
            }
        )
    return result


def summarize_morpho_wld(collateral_symbol: str, min_supply_usd: float = 0) -> dict | None:
    markets = fetch_morpho_markets(collateral_symbol, min_supply_usd)
    if not markets:
        return None
    primary = max(markets, key=lambda m: m["collateral_assets_usd"] or 0)
    total_deposited_usd = sum(m["collateral_assets_usd"] or 0 for m in markets)
    max_utilization = max((m["utilization"] or 0) for m in markets)
    return {
        "markets": markets,
        "primary": primary,
        "total_deposited_usd": total_deposited_usd,
        "max_utilization": max_utilization,
    }


def fetch_world_network_stats() -> dict | None:
    """World Network の利用状況（認証済み人数など）。

    公開APIを確認できなかったため常に None を返す（無効化）。README参照。
    """
    return None
