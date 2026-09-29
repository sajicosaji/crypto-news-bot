"""価格急変の原因をWeb検索で調べる（Opus 5.5 + web_search）。

方針（2026-09-29 本人の指定）:
- 「〜の可能性」を並べるだけの分析は使いものにならない。実際に調べて、原因をある程度断定する。
- 根拠のURLを必ず付ける。
- 原因が分からない・確度が低いなら通知しない。

調査は1回¥30前後（検索3回）と他の処理より桁違いに重いので、
- 同じ銘柄は cache_hours の間、結果（「不明」も含む）を kv_state に保存して使い回す
- 月あたりの回数に上限（max_per_month）を設け、費用の天井を固定する
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from . import db, summarize
from .prices import fetch_hourly_candles, fetch_prices

logger = logging.getLogger("crypto_news_bot.investigate")

CONFIDENCE_RANK = {"低": 0, "中": 1, "高": 2}

PROMPT = """あなたは暗号資産の値動きの原因を調べる調査担当です。Web検索を使って、
実際に何が起きたのかを突き止めてください。読者はこの銘柄の保有者で、
「〜の可能性があります」を並べただけの分析は役に立たないと言っています。

調べ方:
- 渡した価格データ（BTC・ETH・同業トークンとの比較、時間ごとの推移）をまず見て、
  銘柄固有か、セクター全体か、相場全体かを判断する。
- 次に検索で具体的な出来事を探す: トークンアンロック、ハッキング、上場/上場廃止、提携・撤退、
  大口の売買（取引所への送金）、清算、規制、ガバナンス決定、セクター全体の利益確定など。
- 価格予想記事・テクニカル分析だけの記事・「下がった」ことを示すだけの価格ページは根拠にしない。
- 出来事の日時と値動きの時間が合っているかを確認する。

結論の出し方:
- 根拠のある出来事が見つかったら、それを原因として言い切る（「〜が原因」「〜による売り」）。
- 複数要因なら、影響が大きいものから2つまで。
- 根拠が見つからなければ、推測で埋めずに verdict を "unknown" にする。それで構わない。
- 投資判断（買い・売り）は書かない。

最後に、次のJSONだけを ```json ``` で囲んで出力する（それ以外の文章は最後に書かない）:
{
  "verdict": "found" または "unknown",
  "confidence": "高" / "中" / "低",
  "type": "coin_specific" / "sector" / "market",
  "headline": "原因を20字以内で（例: L2全体の利益確定売り）",
  "cause": "何が起きて、なぜ値動きにつながったかを日本語2〜3文で言い切る",
  "evidence": [{"point": "根拠を1文で", "url": "https://..."}]
}
evidence には実際に検索で確認したページのURLだけを1〜4件入れる。"""


# --- 価格データ -------------------------------------------------------------

def describe_timeline(points: list[tuple[datetime, float]], step_hours: int = 4) -> list[str]:
    """いつ動いたかをLLMが読める形にする（JSTで4時間ごと＋1時間で最も動いた時間）。"""
    if len(points) < 2:
        return []
    jst = timezone(timedelta(hours=9))
    lines = []
    last_shown = None
    for ts, price in points:
        if last_shown is None or (ts - last_shown) >= timedelta(hours=step_hours):
            lines.append(f"{ts.astimezone(jst):%m/%d %H:%M} JST  ${price:.4f}")
            last_shown = ts
    end_ts, end_price = points[-1]
    if last_shown != end_ts:
        lines.append(f"{end_ts.astimezone(jst):%m/%d %H:%M} JST  ${end_price:.4f}（現在）")

    biggest = None
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        pct = (p1 - p0) / p0 * 100
        if biggest is None or abs(pct) > abs(biggest[1]):
            biggest = (t1, pct)
    if biggest:
        lines.append(f"1時間で最も動いたのは {biggest[0].astimezone(jst):%m/%d %H時} JST 頃（{biggest[1]:+.1f}%）")
    return lines


def comparison_changes(cfg: dict, coin: str, price_data: dict) -> list[tuple[str, float]]:
    """BTC・ETH・同業トークンの24h変化率。[(表示名, %)]"""
    move_cfg = cfg.get("investigation", {})
    peers = move_cfg.get("peers", {}).get(coin, [])
    changes: list[tuple[str, float]] = []
    for symbol in ("BTC", "ETH"):
        if symbol == coin:
            continue
        pct = (price_data.get(symbol) or {}).get("usd_24h_change")
        if pct is not None:
            changes.append((symbol, pct))
    if peers:
        fetched = fetch_prices([p["id"] for p in peers])
        for p in peers:
            pct = (fetched.get(p["id"]) or {}).get("usd_24h_change")
            if pct is not None:
                changes.append((p["symbol"], pct))
    return changes


# --- 調査本体 ----------------------------------------------------------------

def _extract_json(text: str) -> dict | None:
    """応答の最後のJSONを取り出す。壊れていれば None。"""
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    candidates = blocks or re.findall(r"(\{.*\})", text, flags=re.S)
    for raw in reversed(candidates):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _normalize(result: dict | None) -> dict:
    """形の崩れた結果を「不明」に倒す。URLの無い根拠は捨てる。"""
    if not result:
        return {"verdict": "unknown", "confidence": "低"}
    evidence = [
        {"point": str(e.get("point", "")).strip(), "url": str(e.get("url", "")).strip()}
        for e in (result.get("evidence") or [])
        if isinstance(e, dict) and str(e.get("url", "")).startswith("http")
    ][:4]
    confidence = result.get("confidence") if result.get("confidence") in CONFIDENCE_RANK else "低"
    verdict = result.get("verdict") if result.get("verdict") in ("found", "unknown") else "unknown"
    cause = str(result.get("cause") or "").strip()
    if not evidence or not cause:
        verdict = "unknown"  # 出典の無い断定は出さない
    return {
        "verdict": verdict,
        "confidence": confidence,
        "type": result.get("type") or "",
        "headline": str(result.get("headline") or "").strip()[:40],
        "cause": cause[:600],
        "evidence": evidence,
    }


def run_web_investigation(client, *, coin: str, facts: list[str], cfg: dict) -> dict:
    """Web検索つきで原因を調べる。失敗しても例外は投げず「不明」を返す。"""
    inv_cfg = cfg.get("investigation", {})
    model = inv_cfg.get("model", "claude-opus-5-5")
    tools = [{
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": inv_cfg.get("max_searches", 3),
    }]
    messages = [{"role": "user", "content": "\n".join(facts)}]
    text = ""
    try:
        for _ in range(4):  # サーバー側の検索が長引くと pause_turn で一旦返ってくる
            response = client.messages.create(
                model=model,
                max_tokens=inv_cfg.get("max_tokens", 8000),
                system=PROMPT,
                messages=messages,
                tools=tools,
                **summarize._model_kwargs(model, effort=inv_cfg.get("effort", "high")),
            )
            summarize._record_usage(model, response)
            text = "\n".join(b.text for b in response.content if getattr(b, "type", "") == "text")
            if getattr(response, "stop_reason", None) != "pause_turn":
                break
            messages = messages + [{"role": "assistant", "content": response.content}]
    except Exception as e:
        logger.warning("値動きの調査に失敗しました coin=%s error=%s", coin, e)
        return _normalize(None)
    return _normalize(_extract_json(text))


def _cache_key(coin: str) -> str:
    return f"investigation:{coin}"


def load_cached(conn, coin: str, now: datetime, max_age_hours: float) -> dict | None:
    raw = db.get_state(conn, _cache_key(coin))
    if not raw:
        return None
    try:
        data = json.loads(raw)
        at = datetime.fromisoformat(data["at"])
    except (ValueError, KeyError, TypeError):
        return None
    if (now - at).total_seconds() / 3600 > max_age_hours:
        return None
    return data


def investigate(
    *, conn, coin: str, coingecko_id: str | None, change_24h: float, price_data: dict,
    cfg: dict, now: datetime, client=None,
) -> dict | None:
    """値動きの原因を調べる。結果（不明も含む）は保存して cache_hours の間使い回す。

    戻り値の dict には比較用の変化率 "comparison" も入る。LLMが使えなければ None。
    """
    inv_cfg = cfg.get("investigation", {})
    if not inv_cfg.get("enabled", True):
        return None

    cached = load_cached(conn, coin, now, inv_cfg.get("cache_hours", 24))
    if cached:
        logger.info("%s の値動き調査は%sに実施済みのため使い回します", coin, cached["at"])
        return cached

    # 月あたりの調査回数の上限。これで費用の天井が決まる（上限に達したら急変は通知しない）
    month_key = f"investigation_count:{now:%Y-%m}"
    used = int(db.get_state(conn, month_key, "0") or 0)
    limit = inv_cfg.get("max_per_month", 10)
    if used >= limit:
        logger.info("今月の値動き調査は上限%d回に達したため、%s は調べません", limit, coin)
        return None

    client = client or summarize._build_client(cfg)
    if client is None:
        return None

    comparison = comparison_changes(cfg, coin, price_data)
    facts = [
        f"調査対象: {coin}",
        f"24時間の変化: {change_24h:+.1f}%（{'上昇' if change_24h >= 0 else '下落'}）",
        f"現在時刻: {now.astimezone(timezone(timedelta(hours=9))):%Y-%m-%d %H:%M} JST",
        "",
        "同じ24時間の比較（USD建て）:",
    ]
    facts += [f"　{name}: {pct:+.1f}%" for name, pct in comparison] or ["　（取得できず）"]
    if coingecko_id:
        timeline = describe_timeline(fetch_hourly_candles(coin))
        if timeline:
            facts += ["", f"{coin} の直近48時間の推移:"] + [f"　{line}" for line in timeline]

    since = (now - timedelta(hours=36)).isoformat()
    articles = db.recent_articles_with_source_count(conn, coin, since)[: inv_cfg.get("max_articles", 12)]
    if articles:
        facts += ["", "BOTが集めた直近の記事の見出し（手がかり。これだけで判断しないこと）:"]
        for a in articles:
            facts.append(f"　- {a.get('display_title', '')[:120]}（{a.get('url', '')}）")

    notes = inv_cfg.get("notes", {}).get(coin)
    if notes:
        facts += ["", f"補足: {notes}"]

    result = run_web_investigation(client, coin=coin, facts=facts, cfg=cfg)
    db.set_state(conn, month_key, str(used + 1))
    result["comparison"] = comparison
    result["change_24h"] = change_24h
    result["at"] = now.isoformat()
    db.set_state(conn, _cache_key(coin), json.dumps(result, ensure_ascii=False))
    logger.info(
        "%s 値動き調査: %s（確度 %s）%s", coin, result["verdict"], result["confidence"], result.get("headline", ""),
    )
    return result


def is_confident(result: dict | None, cfg: dict) -> bool:
    """通知してよい確度か。"""
    if not result or result.get("verdict") != "found":
        return False
    minimum = cfg.get("investigation", {}).get("min_confidence", "中")
    return CONFIDENCE_RANK.get(result.get("confidence"), 0) >= CONFIDENCE_RANK.get(minimum, 1)


# --- 表示 --------------------------------------------------------------------

def _domain(url: str) -> str:
    host = urlparse(url).netloc
    return host[4:] if host.startswith("www.") else host


def format_lines(result: dict) -> list[str]:
    """Discordに出す行。原因 → 比較 → 出典の順。"""
    lines = [f"**原因（確度: {result['confidence']}）**", result["cause"]]
    comparison = result.get("comparison") or []
    if comparison:
        lines.append("")
        lines.append("同じ24h: " + " / ".join(f"{name} {pct:+.1f}%" for name, pct in comparison))
    lines.append("")
    lines.append("出典:")
    for e in result.get("evidence", []):
        point = e["point"] or _domain(e["url"])
        lines.append(f"・{point}（[{_domain(e['url'])}]({e['url']})）")
    return lines
