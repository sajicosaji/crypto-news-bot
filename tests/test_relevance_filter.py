"""「読むだけ無駄な投稿」を出さないための絞り込みを検証する。

実際にARBチャンネルに届いて「読む価値がない」と判断された記事を基準にしている。
外部APIはモックする。
"""
import pytest

from src import summarize


class _Resp:
    def __init__(self, text):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = "end_turn"
        self.usage = type("U", (), {"input_tokens": 100, "output_tokens": 5})()


class _Client:
    def __init__(self, verdicts):
        self._verdicts = list(verdicts)
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return _Resp(self._verdicts.pop(0) if self._verdicts else "KEEP")


def _articles(*titles):
    return [{"display_title": t, "excerpt": "本文"} for t in titles]


def test_drops_articles_the_model_rejects(cfg, monkeypatch):
    client = _Client(["DROP: 価格予測のみ", "KEEP"])
    monkeypatch.setattr(summarize, "_build_client", lambda cfg: client)
    kept = summarize.filter_relevant_articles(
        _articles("Standard Chartered Sets $10 Target", "Bridge exploited for $24M"), "ARB", cfg,
    )
    assert [a["display_title"] for a in kept] == ["Bridge exploited for $24M"]


def test_core_topics_skip_the_judgement_entirely(cfg, monkeypatch):
    """ARBにとってのRobinhood Chainのように、銘柄名が無くても重要な話題は必ず残す。"""
    client = _Client(["DROP: 他銘柄が主題"])
    monkeypatch.setattr(summarize, "_build_client", lambda cfg: client)
    kept = summarize.filter_relevant_articles(
        _articles("Robinhood Chain daily revenue drops 40%"), "ARB", cfg,
    )
    assert len(kept) == 1
    assert client.calls == [], "判定に回さず即座に残す（APIを呼ばない＝費用もかからない）"


def test_coin_name_itself_is_not_in_always_keep(cfg):
    """銘柄名を always_keep に入れると全記事が素通りしてしまうため、入れない。"""
    always = cfg["relevance_filter"]["always_keep_keywords"]
    assert "Arbitrum" not in always["ARB"]
    assert "Worldcoin" not in always["WLD"]


def test_api_failure_keeps_the_article(cfg, monkeypatch):
    """判定できないときは落とさない（安全側）。"""
    class _Broken:
        messages = property(lambda self: self)
        def create(self, **kw):
            raise RuntimeError("API error")

    monkeypatch.setattr(summarize, "_build_client", lambda cfg: _Broken())
    kept = summarize.filter_relevant_articles(_articles("何かの記事"), "ARB", cfg)
    assert len(kept) == 1


def test_disabled_filter_keeps_everything(cfg, monkeypatch):
    monkeypatch.setattr(summarize, "_build_client", lambda cfg: _Client(["DROP: x"]))
    disabled = {**cfg, "relevance_filter": {**cfg["relevance_filter"], "enabled": False}}
    assert len(summarize.filter_relevant_articles(_articles("記事"), "ARB", disabled)) == 1


def test_no_api_key_keeps_everything(cfg, monkeypatch):
    monkeypatch.setattr(summarize, "_build_client", lambda cfg: None)
    assert len(summarize.filter_relevant_articles(_articles("記事"), "ARB", cfg)) == 1


# --- キーワード側の除外（LLMに回す前の足切り） --------------------------------

@pytest.mark.parametrize("title", [
    "Arbitrum price prediction for 2027",
    "7,000% Growth by 2030: Standard Chartered Predicts ARB to Hit $10",
    "Analyst forecasts ARB price target of $10",
    "ARBの価格予測レポート",
])
def test_price_prediction_headlines_are_excluded(cfg, title):
    from src.classify import is_excluded
    assert is_excluded(title, cfg["exclude_keywords"]) is True


@pytest.mark.parametrize("title", [
    "Arbitrum bridge exploited for $24M in USDC",
    "Arbitrum DAO approves ARB buyback",
    "Coinbase lists ARB perpetual futures",
])
def test_real_news_is_not_excluded_by_keywords(cfg, title):
    from src.classify import is_excluded
    assert is_excluded(title, cfg["exclude_keywords"]) is False


# --- DAOフォーラムは提案だけ拾う ---------------------------------------------

def test_dao_forum_needs_both_proposal_and_topic_words(cfg):
    keywords = [k.lower() for k in cfg["arb"]["dao_forum_keywords"]]
    topics = [k.lower() for k in cfg["arb"]["dao_forum_topic_keywords"]]

    def picked(title):
        low = title.lower()
        return any(k in low for k in keywords) and any(t in low for t in topics)

    assert picked("[AIP] Enable ARB buyback from sequencer revenue") is True
    assert picked("Proposal: adjust staking rewards") is True
    # 提案の形をしていない雑談は拾わない
    assert picked("Does your runway number assume you can sell the treasury at spot?") is False
    # 提案でも、効く話題でなければ拾わない
    assert picked("[AIP] Update the forum moderation guidelines") is False
