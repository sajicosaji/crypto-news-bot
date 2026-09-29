"""価格の取得（CoinPaprika）を検証する。通信はモックする。"""
from src import prices


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_every_configured_coin_has_a_price_source(cfg):
    """設定にある銘柄がすべて CoinPaprika の id に読み替えられる。"""
    ids = {cfg["btc_coingecko_id"], cfg["sol"]["usd1_coingecko_id"]}
    ids |= {c["coingecko_id"] for c in cfg["coins"].values()}
    ids |= {p["id"] for peers in cfg["investigation"]["peers"].values() for p in peers}
    assert ids <= set(prices.PAPRIKA_IDS)


def test_wld_uses_the_real_worldcoin_token():
    assert prices.PAPRIKA_IDS["worldcoin-wld"] == "wld-worldcoin"


def test_fetch_prices_keeps_the_old_shape(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        assert url.endswith("/tickers/arb-arbitrum")
        return _Resp({"quotes": {
            "USD": {"price": 0.2, "percent_change_24h": -10.9, "percent_change_1h": -1.2},
            "JPY": {"price": 31.0},
        }})
    monkeypatch.setattr(prices.requests, "get", fake_get)
    data = prices.fetch_prices(["arbitrum"])
    assert data == {"arbitrum": {"usd": 0.2, "jpy": 31.0, "usd_24h_change": -10.9, "usd_1h_change": -1.2}}


def test_failed_coin_is_left_out(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        raise prices.requests.ConnectionError("down")
    monkeypatch.setattr(prices.requests, "get", fake_get)
    assert prices.fetch_prices(["arbitrum"]) == {}
