"""「このリンクは読む価値があるか」をキーワードとルールで判定する（LLM不使用）。

重大ワード・報道媒体数・同時に起きている値動きを組み合わせて優先度と理由を作る。
"""
from __future__ import annotations

from .formatting import fmt_pct

PRIORITY_HIGH = "高"
PRIORITY_MEDIUM = "中"
PRIORITY_LOW = "低"

_ACTION_BY_PRIORITY = {
    PRIORITY_HIGH: "まず最初に目を通すのがおすすめ",
    PRIORITY_MEDIUM: "余裕があれば確認",
    PRIORITY_LOW: "流し読みでOK",
}


def build_reading_advice(
    *,
    critical_hits: list[str] | None = None,
    source_count: int = 1,
    score: int = 0,
    is_dao_proposal: bool = False,
    price_change_24h: float | None = None,
    cfg: dict,
) -> tuple[str, str]:
    """(優先度, アドバイス文) を返す。"""
    advice_cfg = cfg.get("reading_advice", {})
    price_reaction_pct = advice_cfg.get("price_reaction_pct", 5)

    critical_hits = [h for h in (critical_hits or []) if h]
    has_critical = bool(critical_hits)
    multi_media = source_count >= 2
    price_reacting = price_change_24h is not None and abs(price_change_24h) >= price_reaction_pct
    strong_tone = abs(score) >= 2

    reasons: list[str] = []
    if has_critical:
        reasons.append(f"重大ワード「{'・'.join(dict.fromkeys(critical_hits))}」を含む")
    if is_dao_proposal:
        reasons.append("Arbitrum DAOの提案")
    if multi_media:
        reasons.append(f"{source_count}媒体が報じている")
    # 値動きは銘柄共通の情報なので、全記事に同じ文言が並ばないよう
    # 重大ワードを含む記事にだけ添える（優先度の判定には常に使う）
    if price_reacting and has_critical:
        reasons.append(f"同時に24hで{fmt_pct(price_change_24h)}の値動き")
    if strong_tone and not has_critical:
        reasons.append("好悪がはっきりした見出し")

    if has_critical and (price_reacting or multi_media):
        priority = PRIORITY_HIGH
    elif has_critical or is_dao_proposal or multi_media or strong_tone:
        priority = PRIORITY_MEDIUM
    else:
        priority = PRIORITY_LOW

    if not reasons:
        reasons.append("目立った反応や重大ワードは無し")

    text = f"読む価値: {priority} — " + "、".join(reasons) + f"。{_ACTION_BY_PRIORITY[priority]}"
    return priority, text


def priority_rank(priority: str) -> int:
    """並べ替え用（高いほど先頭に来るように小さい値を返す）。"""
    return {PRIORITY_HIGH: 0, PRIORITY_MEDIUM: 1, PRIORITY_LOW: 2}.get(priority, 3)
