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
