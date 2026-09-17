"""値動きの背景になりそうなニュースを、キーワードとルールで拾う（LLM不使用）。

相場全体（BTC）につられた動きなのか、その銘柄だけが動いたのかをまず切り分ける。
BTCと一緒に動いただけなら個別ニュースのせいにしない。銘柄固有の動きのときだけ、
値動きの向きに合うニュースを関連度の高い順に出す。
"""
from __future__ import annotations

from datetime import datetime, timezone

UP = "up"
DOWN = "down"


def analyze_move(
    coin_change_24h: float | None,
    btc_change_24h: float | None,
    coin_specific_pct: float = 3.0,
) -> dict:
    """値動きを「BTC連動分」と「銘柄固有分」に切り分ける。

    戻り値の `is_coin_specific` が True のときだけ、背景ニュースを探す価値がある。
    """
    if coin_change_24h is None:
        return {
            "direction": None,
            "excess_pct": None,
            "is_coin_specific": False,
            "summary": "値動きのデータが取得できませんでした。",
        }

    direction = UP if coin_change_24h >= 0 else DOWN

    if btc_change_24h is None:
        # BTCと比べられない以上、相場連動か銘柄固有かは判断できない。
        # 分からないまま個別ニュースのせいにはしない。
        return {
            "direction": direction,
            "excess_pct": None,
            "is_coin_specific": False,
            "summary": "BTCの値動きが取得できないため、相場連動か銘柄固有かは判定できません。",
        }

    excess = coin_change_24h - btc_change_24h
    btc = btc_change_24h
    is_coin_specific = abs(excess) >= coin_specific_pct

    if is_coin_specific:
        # 超過分の向き（銘柄固有に動いた向き）を示す
        excess_dir = "上振れ" if excess > 0 else "下振れ"
        summary = (
            f"BTC {btc:+.1f}% に対してこの銘柄は {coin_change_24h:+.1f}%。"
            f"差し引き {excess:+.1f}% の{excess_dir}で、銘柄固有の動きが大きいです。"
        )
    else:
        summary = (
            f"BTC {btc:+.1f}% に対してこの銘柄は {coin_change_24h:+.1f}%。"
            f"差は {excess:+.1f}% で、相場全体につられた動きとみられます。"
        )

    return {
        "direction": direction,
        "excess_pct": excess,
        "is_coin_specific": is_coin_specific,
        "summary": summary,
    }


def _relevance_score(article: dict, direction: str, now: datetime) -> int:
    """値動きの説明になりそうな度合い。高いほど関連がありそう。"""
    sentiment = article.get("sentiment")
    score = 0

    # 値動きの向きと記事の方向性が一致しているか（最重要）
    if direction == UP and sentiment == "good":
        score += 3
    elif direction == DOWN and sentiment == "bad":
        score += 3
    elif sentiment == "neutral":
        score += 0
    else:
        # 向きが逆の記事（上昇時の悪材料など）は背景として出さない
        return -1

    if article.get("is_critical"):
        score += 2

    # 多くの媒体が報じた話題は、キーワード判定で中立になっていても値動きの
    # きっかけになりやすい（見出しのキーワードだけでは強弱を拾いきれないため）
    sources = article.get("source_count", 1)
    if sources >= 5:
        score += 4
    elif sources >= 3:
        score += 3
    elif sources >= 2:
        score += 2
    else:
        score += 1

    published = article.get("first_published_at")
    if published:
        try:
            dt = datetime.fromisoformat(published)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            hours_ago = (now - dt).total_seconds() / 3600
            if hours_ago <= 6:
                score += 2
            elif hours_ago <= 12:
                score += 1
        except (TypeError, ValueError):
            pass

    return score


def pick_move_context(
    articles: list[dict], direction: str, now: datetime, max_items: int = 3
) -> list[dict]:
    """値動きの背景になりそうな記事を、関連度の高い順に返す。

    向きが合わない記事（上昇時の悪材料など）は除外する。該当が無ければ空を返す。
    """
    if not direction:
        return []
    scored = []
    for article in articles:
        score = _relevance_score(article, direction, now)
        # 向きが一致しない、または手がかりが弱いものは出さない
        if score < 4:
            continue
        scored.append((score, article))
    scored.sort(key=lambda x: -x[0])
    return [a for _, a in scored[:max_items]]


def direction_label(direction: str | None) -> str:
    if direction == UP:
        return "上昇"
    if direction == DOWN:
        return "下落"
    return "値動き"
