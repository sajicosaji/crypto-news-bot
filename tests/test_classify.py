from src.classify import is_excluded, score_headline


def test_good_keyword_scores_positive(cfg):
    r = score_headline("Arbitrum announces major partnership with Robinhood", cfg)
    assert r.score > 0
    assert r.sentiment == "good"
    assert r.emoji == cfg["emojis"]["good"]


def test_bad_keyword_scores_negative(cfg):
    r = score_headline("Protocol hit by hack, funds drained", cfg)
    assert r.score < 0
    assert r.sentiment == "bad"
    assert "hack" in r.critical_hits


def test_no_hack_is_not_critical(cfg):
    r = score_headline("Audit finds no hack occurred on the bridge", cfg)
    assert "hack" not in r.critical_hits
    assert r.sentiment != "bad"


def test_negation_word_cancels_critical_keyword(cfg):
    r = score_headline("Exchange denies exploit allegations after rumor spreads", cfg)
    assert "exploit" not in r.critical_hits


def test_falls_short_idiom_is_not_bad(cfg):
    r = score_headline("ARB price falls short of analyst expectations", cfg)
    assert r.sentiment == "neutral"
    assert r.bad_hits == []


def test_japanese_negation_suffix_cancels_bad_keyword(cfg):
    r = score_headline("WLDはアンロック後も下落せず、価格は安定", cfg)
    assert "下落" not in r.bad_hits


def test_japanese_bad_keyword_without_negation_counts(cfg):
    r = score_headline("WLDが急落、規制当局の発表を受けて", cfg)
    assert r.sentiment == "bad"
    assert r.score < 0


def test_sol_does_not_match_sold(cfg):
    from src.fetch_news import match_coins

    coins_cfg = {"SOL": {"names": ["Solana", "SOL token"]}}
    matched = match_coins("Investor sold his entire portfolio yesterday", coins_cfg)
    assert matched == []


def test_ticker_word_boundary_matches_standalone(cfg):
    from src.utils import contains_term

    assert contains_term("Solana price rallies", "Solana") is True
    assert contains_term("He sold everything", "Solana") is False


def test_exclude_keywords(cfg):
    assert is_excluded("ARB price prediction for next month", cfg["exclude_keywords"]) is True
    assert is_excluded("ARB price surges 20%", cfg["exclude_keywords"]) is False


def test_neutral_headline_has_zero_score(cfg):
    r = score_headline("Ethereum developers hold weekly community call", cfg)
    assert r.score == 0
    assert r.sentiment == "neutral"
    assert r.emoji == cfg["emojis"]["neutral"]
