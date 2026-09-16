from src.fetch_news import match_coins


def test_match_single_coin(cfg):
    matched = match_coins("Ethereum upgrade goes live on mainnet", cfg["coins"])
    assert matched == ["ETH"]


def test_match_multiple_coins_routes_to_all(cfg):
    matched = match_coins("Robinhood adds Solana and Worldcoin trading pairs", cfg["coins"])
    assert set(matched) == {"SOL", "WLD"}


def test_no_match_returns_empty(cfg):
    matched = match_coins("Dogecoin community celebrates anniversary", cfg["coins"])
    assert matched == []


def test_japanese_name_matches(cfg):
    matched = match_coins("ソラナ、新機能を発表", cfg["coins"])
    assert matched == ["SOL"]


def test_arb_bare_ticker_not_in_default_names_avoids_false_positive(cfg):
    # config.yaml のデフォルトでは "ARB" 単体は名前リストに含めていない
    # （arbitrage の略として誤検出しやすいため）。
    matched = match_coins("Traders look for arb opportunities across exchanges", cfg["coins"])
    assert "ARB" not in matched
