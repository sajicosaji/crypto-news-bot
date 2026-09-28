"""価格急変の原因調査（Web検索つき）を検証する。APIはモックする。

本人の方針: 原因をある程度断定する / 出典を出す / 分からないなら通知しない。
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src import alerts, investigate

NOW = datetime(2026, 9, 29, 3, 0, tzinfo=timezone.utc)

FOUND = {
    "verdict": "found", "confidence": "中", "type": "sector",
    "headline": "L2全体の利益確定売り",
    "cause": "1か月で2倍超に上がった反動で、OP・ZKなどL2銘柄がまとめて売られた。",
    "evidence": [
        {"point": "L2トークンが軒並み下落", "url": "https://example.com/l2-selloff"},
        {"point": "次のアンロックは10/16", "url": "https://tokenomist.ai/arbitrum"},
    ],
}
UNKNOWN = {"verdict": "unknown", "confidence": "低", "headline": "", "cause": "", "evidence": []}


class FakeClient:
    """messages.create の呼び出し回数を数え、決まった応答を返す。"""

    def __init__(self, result, stop_reasons=("end_turn",)):
        self.calls = 0
        self.result = result
        self.stop_reasons = list(stop_reasons)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        stop = self.stop_reasons[min(self.calls, len(self.stop_reasons) - 1)]
        self.calls += 1
        text = "調べた結果です。\n```json\n" + json.dumps(self.result, ensure_ascii=False) + "\n```"
        usage = SimpleNamespace(
            input_tokens=1000, output_tokens=100,
            server_tool_use=SimpleNamespace(web_search_requests=2),
        )
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)], stop_reason=stop, usage=usage,
        )


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(investigate, "fetch_prices", lambda ids: {
        "optimism": {"usd_24h_change": -9.3}, "zksync": {"usd_24h_change": -7.1},
        "starknet": {"usd_24h_change": -8.0},
    })
    monkeypatch.setattr(investigate, "fetch_hourly_prices", lambda cid, days=2: [
        (NOW - timedelta(hours=h), 0.23 - 0.001 * (48 - h)) for h in range(48, -1, -1)
    ])


PRICES = {
    "ARB": {"usd": 0.205, "jpy": 30.5, "usd_24h_change": -10.9},
    "BTC": {"usd": 80000.0, "jpy": 12000000.0, "usd_24h_change": -0.5},
    "ETH": {"usd": 2400.0, "jpy": 360000.0, "usd_24h_change": -0.2},
}


def _investigate(conn, cfg, client, now=NOW):
    return investigate.investigate(
        conn=conn, coin="ARB", coingecko_id="arbitrum", change_24h=-10.9,
        price_data=PRICES, cfg=cfg, now=now, client=client,
    )


# --- 結果の判定 ----------------------------------------------------------------

def test_found_cause_with_sources_is_confident(conn, cfg):
    result = _investigate(conn, cfg, FakeClient(FOUND))
    assert investigate.is_confident(result, cfg)
    assert ("OP", -9.3) in [tuple(c) for c in result["comparison"]], "同業との比較を添える"


def test_unknown_cause_is_not_confident(conn, cfg):
    assert not investigate.is_confident(_investigate(conn, cfg, FakeClient(UNKNOWN)), cfg)


def test_low_confidence_is_not_confident(conn, cfg):
    low = {**FOUND, "confidence": "低"}
    assert not investigate.is_confident(_investigate(conn, cfg, FakeClient(low)), cfg)


def test_cause_without_source_urls_is_treated_as_unknown(conn, cfg):
    """出典の無い断定は出さない。"""
    no_source = {**FOUND, "evidence": [{"point": "噂", "url": ""}]}
    result = _investigate(conn, cfg, FakeClient(no_source))
    assert result["verdict"] == "unknown"


def test_broken_response_is_treated_as_unknown():
    assert investigate._normalize(investigate._extract_json("JSONがありません"))["verdict"] == "unknown"


def test_pause_turn_is_continued(conn, cfg):
    client = FakeClient(FOUND, stop_reasons=("pause_turn", "end_turn"))
    result = _investigate(conn, cfg, client)
    assert client.calls == 2
    assert result["verdict"] == "found"


# --- 費用を抑える使い回し ------------------------------------------------------

def test_result_is_reused_within_cache_window(conn, cfg):
    """30分ごとの監視で毎回調べ直さない（1回¥80〜120のため）。"""
    client = FakeClient(UNKNOWN)
    _investigate(conn, cfg, client)
    _investigate(conn, cfg, client, now=NOW + timedelta(hours=6))
    assert client.calls == 1, "「不明」も含めて12時間は使い回す"
    _investigate(conn, cfg, client, now=NOW + timedelta(hours=13))
    assert client.calls == 2


# --- 通知するかどうか ----------------------------------------------------------

def _run(conn, cfg, monkeypatch, client, *, btc_change=-0.5, arb_change=-10.9):
    monkeypatch.setattr(investigate.summarize, "_build_client", lambda cfg: client)
    prices = {**PRICES, "ARB": {**PRICES["ARB"], "usd_24h_change": arb_change}}
    return alerts.run_alert_for_coin(
        conn=conn, coin="ARB", coin_cfg=cfg["coins"]["ARB"], webhook_url="https://discord.example/arb",
        cfg=cfg, touched_article_ids=[], price_data=prices, btc_change_24h=btc_change,
        hourly_change=None, onchain_summary=None, usd1_price=None, morpho_summary=None, dry_run=True,
    )


def test_price_alert_is_posted_with_cause_and_sources(conn, cfg, monkeypatch, capsys):
    assert _run(conn, cfg, monkeypatch, FakeClient(FOUND)) == 1
    out = capsys.readouterr().out
    assert "L2全体の利益確定売り" in out
    assert "https://example.com/l2-selloff" in out
    assert "OP -9.3%" in out
    assert "断定するものではありません" not in out, "言い訳付きの記事一覧は出さない"


def test_price_alert_is_suppressed_when_cause_is_unknown(conn, cfg, monkeypatch, capsys):
    """分からないなら通知しない。"""
    assert _run(conn, cfg, monkeypatch, FakeClient(UNKNOWN)) == 0
    assert "価格急変" not in capsys.readouterr().out


def test_price_alert_following_btc_is_not_investigated(conn, cfg, monkeypatch):
    """BTCにつられただけの急変は、BTC告知に任せて調べもしない（費用ゼロ）。"""
    client = FakeClient(FOUND)
    assert _run(conn, cfg, monkeypatch, client, btc_change=-9.0, arb_change=-11.0) == 0
    assert client.calls == 0


def test_web_search_cost_is_reported(conn, cfg):
    from src import summarize
    summarize._usage.clear()
    _investigate(conn, cfg, FakeClient(FOUND))
    report = summarize.usage_report(cfg)
    assert "検索2回" in report
    summarize._usage.clear()
