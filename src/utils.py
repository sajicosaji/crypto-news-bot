"""共通ユーティリティ: 時刻(JST)、タイトル正規化など。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

# RSSの「タイトル - 媒体名」形式から媒体名を取り除くための区切り文字
_TITLE_SEP_PATTERN = re.compile(r"\s+[-–—|]\s+[^-–—|]+$")
_WHITESPACE_PATTERN = re.compile(r"\s+")
_PUNCT_PATTERN = re.compile(r"[\"'’‘“”.,!?:;()\[\]]")


def now_jst() -> datetime:
    return datetime.now(JST)


def to_jst(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(JST)


def format_jst(dt: datetime, fmt: str = "%m/%d %H:%M") -> str:
    return to_jst(dt).strftime(fmt)


def strip_source_suffix(title: str) -> str:
    """「タイトル - 媒体名」の媒体名部分を取り除く。"""
    return _TITLE_SEP_PATTERN.sub("", title).strip()


def normalize_title(title: str) -> str:
    """媒体違いの同一記事をまとめるための正規化キー。"""
    t = strip_source_suffix(title)
    t = t.lower()
    t = _PUNCT_PATTERN.sub("", t)
    t = _WHITESPACE_PATTERN.sub(" ", t).strip()
    return t


# 同じ話題かどうかを見るときに無視する語（頻出しすぎて手がかりにならない語）
DEFAULT_GENERIC_TERMS = {
    "bitcoin", "ethereum", "ether", "solana", "arbitrum", "worldcoin", "crypto",
    "cryptocurrency", "cryptocurrencies", "token", "tokens", "coin", "coins",
    "price", "prices", "market", "markets", "news", "today", "amid", "after",
    "with", "from", "that", "this", "what", "when", "will", "says", "said",
    "could", "would", "about", "over", "into", "more", "than", "here",
}


def significant_terms(normalized_title: str, generic_terms: set[str] | None = None) -> set[str]:
    """見出しから、話題を見分ける手がかりになる語だけを取り出す。"""
    generic = generic_terms if generic_terms is not None else DEFAULT_GENERIC_TERMS
    tokens = re.findall(r"[a-z0-9]{4,}", normalized_title.lower())
    return {t for t in tokens if t not in generic}


def is_same_story(title_a: str, title_b: str, min_shared_terms: int = 3) -> bool:
    """言い回しが違うだけの同じ話題かどうかを判定する。

    媒体ごとに見出しが書き換えられるため、完全一致では同じニュースをまとめきれない。
    固有名詞や数字など手がかりになる語の重なりで判定する。
    """
    terms_a = significant_terms(title_a)
    terms_b = significant_terms(title_b)
    if not terms_a or not terms_b:
        return False
    shared = terms_a & terms_b
    if len(shared) < min_shared_terms:
        return False
    # 短い見出し同士で、片方がもう片方にほぼ含まれるだけの場合を除く
    smaller = min(len(terms_a), len(terms_b))
    return len(shared) >= min(min_shared_terms, smaller)


def is_ascii(term: str) -> bool:
    return all(ord(c) < 128 for c in term)


def find_all_term(text: str, term: str):
    """略語の単語境界一致（例: SOL が SOLD に一致しない）。

    日本語（非ASCII）の語は分かち書きされないため単語境界の概念がなく、
    通常の部分一致で検索する。ASCII の語（英単語・ティッカー）は
    英数字境界での完全一致のみを対象にする。
    """
    if is_ascii(term):
        pattern = r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])"
        return list(re.finditer(pattern, text, flags=re.IGNORECASE))
    return list(re.finditer(re.escape(term), text))


def contains_term(text: str, term: str) -> bool:
    return len(find_all_term(text, term)) > 0
