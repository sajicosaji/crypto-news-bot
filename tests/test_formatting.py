from src.discord import is_custom_emoji
from src.formatting import fmt_level, fmt_pct, fmt_usd


def test_fmt_level_keeps_two_decimals_for_sub_dollar_levels():
    assert fmt_level(0.20) == "0.20"
    assert fmt_level(0.14) == "0.14"
    assert fmt_level(0.5) == "0.50"


def test_fmt_level_for_whole_numbers():
    assert fmt_level(1.0) == "1"
    assert fmt_level(2500.0) == "2,500"


def test_fmt_pct_includes_sign():
    assert fmt_pct(6.25) == "+6.2%"
    assert fmt_pct(-6.25) == "-6.2%"
    assert fmt_pct(None) == "-"


def test_fmt_usd_precision_by_magnitude():
    assert fmt_usd(0.1620) == "$0.1620"
    assert fmt_usd(2393.57) == "$2,393.57"


def test_custom_emoji_detection():
    assert is_custom_emoji("<:arbup:123456789012345678>") is True
    assert is_custom_emoji("<a:spin:123456789012345678>") is True
    assert is_custom_emoji("🚀") is False
    assert is_custom_emoji("") is False


# --- Discordの文字数上限対策 -------------------------------------------------

def test_fit_lines_truncates_when_over_limit():
    from src.formatting import fit_lines

    lines = ["あ" * 100 for _ in range(100)]  # 約10,100文字
    result = fit_lines(lines, limit=500)
    assert sum(len(x) + 1 for x in result) <= 500 + len("…（長いため以下省略）") + 1
    assert result[-1] == "…（長いため以下省略）"


def test_fit_lines_keeps_everything_when_short():
    from src.formatting import fit_lines

    lines = ["短い行", "もう1行"]
    assert fit_lines(lines, limit=500) == lines


def test_weekly_embed_description_stays_within_discord_limit():
    """週次振り返りは情報量が多く、上限超過で投稿が丸ごと失敗しうるため。"""
    from datetime import date, datetime, timedelta, timezone

    from src.weekly import build_weekly_embed

    start = datetime(2026, 9, 7, tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    day = date(2026, 9, 9)
    # 長い見出しと長いURLのニュースが大量にある最悪ケース
    news = [
        {
            "display_title": "非常に長い見出し" * 12,
            "url": "https://example.com/" + "a" * 180,
            "first_published_at": "2026-09-09T00:00:00+00:00",
            "emoji": "📰",
            "first_source": "テスト媒体",
        }
        for _ in range(5)
    ]
    embed = build_weekly_embed(
        coin="ARB",
        start=start,
        end=end,
        open_price={"usd": 0.1},
        close_price={"usd": 0.2},
        btc_open={"usd": 70000},
        btc_close={"usd": 75000},
        top_moves=[{"date": day, "pct": 10.0, "usd": 0.2} for _ in range(3)],
        news_by_day={day: news},
        onchain_trend_lines=["オンチェーン推移" * 20] * 5,
        alerts=[
            {"alert_type": "news", "message": "速報メッセージ" * 15, "ts": "2026-09-09T00:00:00+00:00"}
            for _ in range(10)
        ],
        sentiment_counts={day: {"good": 1, "bad": 2, "neutral": 3}},
    )
    assert len(embed["description"]) <= 4096
