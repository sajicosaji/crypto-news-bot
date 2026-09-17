"""Discord埋め込み用の共通フォーマット関数。"""
from __future__ import annotations


def fmt_usd(value: float | None) -> str:
    if value is None:
        return "-"
    if value >= 100:
        return f"${value:,.2f}"
    if value >= 1:
        return f"${value:,.3f}"
    return f"${value:,.4f}"


def fmt_jpy(value: float | None) -> str:
    if value is None:
        return "-"
    if value >= 100:
        return f"¥{value:,.1f}"
    return f"¥{value:,.2f}"


def fmt_pct(value: float | None, decimals: int = 1) -> str:
    if value is None:
        return "-"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.{decimals}f}%"


def fmt_level(value: float) -> str:
    """価格の節目を表示用に整える（0.2 → "0.20"、1500 → "1,500"）。"""
    if value < 1:
        return f"{value:.2f}"
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}"


# Discordの埋め込み本文の上限は4096文字。超えると投稿そのものが失敗するため余裕を持たせる。
DISCORD_DESCRIPTION_LIMIT = 3900


def fit_lines(lines: list[str], limit: int = DISCORD_DESCRIPTION_LIMIT) -> list[str]:
    """Discordの文字数上限に収まるように行を切り詰める。"""
    result: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + 1
        if used + cost > limit:
            result.append("…（長いため以下省略）")
            break
        result.append(line)
        used += cost
    return result


def fmt_usd_compact(value: float | None) -> str:
    if value is None:
        return "-"
    abs_v = abs(value)
    if abs_v >= 1_000_000_000:
        return f"${value / 1_000_000_000:,.2f}B"
    if abs_v >= 1_000_000:
        return f"${value / 1_000_000:,.2f}M"
    if abs_v >= 1_000:
        return f"${value / 1_000:,.1f}K"
    return f"${value:,.0f}"
