"""見出しのキーワード・ルールベース判定（LLM不使用）。

差し替えやすいように、スコア計算・絵文字選択・重大ワード判定を
それぞれ独立した関数に分けている。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .utils import find_all_term, is_ascii


@dataclass
class Classification:
    score: int
    good_hits: list[str] = field(default_factory=list)
    bad_hits: list[str] = field(default_factory=list)
    critical_hits: list[str] = field(default_factory=list)
    emoji: str = "📰"

    @property
    def sentiment(self) -> str:
        if self.score > 0:
            return "good"
        if self.score < 0:
            return "bad"
        return "neutral"

    @property
    def is_critical(self) -> bool:
        return len(self.critical_hits) > 0


def _strip_idioms(title: str, idioms: list[str]) -> str:
    working = title
    for phrase in idioms:
        working = re.sub(re.escape(phrase), " ", working, flags=re.IGNORECASE)
    return working


def _is_negated_en(text: str, match_start: int, negation_words: list[str]) -> bool:
    """マッチ直前の数語に否定語があるかどうか（英語）。"""
    window = text[max(0, match_start - 20) : match_start]
    words = re.findall(r"[a-zA-Z']+", window.lower())
    tail = words[-2:] if len(words) >= 2 else words
    return any(neg.lower() in tail for neg in negation_words)


def _is_negated_ja(text: str, match_end: int, negation_suffixes: list[str]) -> bool:
    """マッチ直後に否定の接尾表現があるかどうか（日本語。例: 下落せず）。"""
    window = text[match_end : match_end + 6]
    return any(window.startswith(suf) for suf in negation_suffixes)


def score_headline(title: str, cfg: dict) -> Classification:
    keywords = cfg["keywords"]
    negation_words_en = cfg.get("negation_words_en", [])
    negation_suffixes_ja = cfg.get("negation_suffixes_ja", [])
    idioms = cfg.get("idiom_neutralizers", [])

    working = _strip_idioms(title, idioms)

    score = 0
    good_hits: list[str] = []
    bad_hits: list[str] = []

    def scan(word_list: list[str], sign: int, hit_bucket: list[str]) -> None:
        nonlocal score
        for kw in word_list:
            for m in find_all_term(working, kw):
                if is_ascii(kw):
                    if _is_negated_en(working, m.start(), negation_words_en):
                        continue
                else:
                    if _is_negated_ja(working, m.end(), negation_suffixes_ja):
                        continue
                score += sign
                hit_bucket.append(kw)

    scan(keywords["good"]["en"], 1, good_hits)
    scan(keywords["good"]["ja"], 1, good_hits)
    scan(keywords["bad"]["en"], -1, bad_hits)
    scan(keywords["bad"]["ja"], -1, bad_hits)

    critical_hits = detect_critical(working, cfg, negation_words_en, negation_suffixes_ja)

    emojis = cfg["emojis"]
    if score > 0:
        emoji = emojis["good"]
    elif score < 0:
        emoji = emojis["bad"]
    else:
        emoji = emojis["neutral"]

    return Classification(
        score=score,
        good_hits=good_hits,
        bad_hits=bad_hits,
        critical_hits=critical_hits,
        emoji=emoji,
    )


def detect_critical(
    title: str,
    cfg: dict,
    negation_words_en: list[str] | None = None,
    negation_suffixes_ja: list[str] | None = None,
) -> list[str]:
    """重大ワード（速報候補）に一致する語のリストを返す。否定されていれば除外。"""
    negation_words_en = negation_words_en or cfg.get("negation_words_en", [])
    negation_suffixes_ja = negation_suffixes_ja or cfg.get("negation_suffixes_ja", [])
    idioms = cfg.get("idiom_neutralizers", [])
    working = _strip_idioms(title, idioms)

    critical = cfg["critical_keywords"]
    hits: list[str] = []
    for kw in critical["en"] + critical["ja"]:
        for m in find_all_term(working, kw):
            if is_ascii(kw):
                if _is_negated_en(working, m.start(), negation_words_en):
                    continue
            else:
                if _is_negated_ja(working, m.end(), negation_suffixes_ja):
                    continue
            hits.append(kw)
    return hits


def is_excluded(title: str, exclude_keywords: list[str]) -> bool:
    lower = title.lower()
    return any(kw.lower() in lower for kw in exclude_keywords)
