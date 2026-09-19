"""日本語要約（Claude Haiku）の検証。外部APIは必ずモックする。

要約はBOTの中で唯一の有料API呼び出しなので、
「失敗しても止まらない」「同じ記事に二重課金しない」「件数の上限を守る」
の3点を重点的に確認する。
"""
import pytest

from src import db, summarize


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Response:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_Block(text)]
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return self._response


class _FakeClient:
    def __init__(self, response=None, error=None):
        self.messages = _FakeMessages(response, error)


# --- 単体の要約 --------------------------------------------------------------

def test_returns_japanese_summary(cfg):
    client = _FakeClient(_Response("DAOが買い戻しを承認した。\n対象は年間2000万ドル。"))
    result = summarize.summarize_article(
        client, title="Arbitrum DAO approves buyback", body="The DAO voted...", cfg=cfg
    )
    assert result == "DAOが買い戻しを承認した。\n対象は年間2000万ドル。"


def test_uses_haiku_model_and_configured_limits(cfg):
    client = _FakeClient(_Response("要約"))
    summarize.summarize_article(client, title="T", body="本文", cfg=cfg)
    call = client.messages.calls[0]
    assert call["model"] == "claude-haiku-4-5"
    assert call["max_tokens"] == cfg["summary"]["max_output_tokens"]
    assert "日本語" in call["system"]


def test_returns_none_without_client(cfg):
    """APIキーが無いときは要約せず、抜粋表示にフォールバックする。"""
    assert summarize.summarize_article(None, title="T", body="本文", cfg=cfg) is None


def test_api_error_does_not_raise(cfg):
    """要約が失敗してもBOT全体を止めない。"""
    client = _FakeClient(error=RuntimeError("API down"))
    assert summarize.summarize_article(client, title="T", body="本文", cfg=cfg) is None


def test_refusal_returns_none(cfg):
    client = _FakeClient(_Response("", stop_reason="refusal"))
    assert summarize.summarize_article(client, title="T", body="本文", cfg=cfg) is None


def test_no_source_text_is_not_summarized(cfg):
    """本文も抜粋も無ければ、見出しだけを訳しても価値が薄いので呼ばない。"""
    client = _FakeClient(_Response("要約"))
    assert summarize.summarize_article(client, title="T", body="", excerpt="", cfg=cfg) is None
    assert client.messages.calls == []


def test_falls_back_to_excerpt_when_body_missing(cfg):
    client = _FakeClient(_Response("要約"))
    summarize.summarize_article(client, title="T", body="", excerpt="抜粋テキスト", cfg=cfg)
    assert "抜粋テキスト" in client.messages.calls[0]["messages"][0]["content"]


# --- まとめて要約するときの費用ガード ----------------------------------------

@pytest.fixture()
def patched(monkeypatch):
    client = _FakeClient(_Response("日本語の要約"))
    monkeypatch.setattr(summarize, "_build_client", lambda cfg: client)
    monkeypatch.setattr(summarize, "fetch_article_text", lambda url, timeout=10: "本文テキスト")
    return client


def _article(conn, title, summary=None):
    article_id = db.insert_article(
        conn, normalized_title=title.lower(), display_title=title,
        url=f"https://example.com/{title}", first_source="媒体",
        first_published_at="2026-09-19T00:00:00+00:00", score=0, sentiment="neutral",
        emoji="📰", is_critical=False, critical_hits=[], coins=["ARB"], excerpt="抜粋",
    )
    if summary:
        db.set_summary(conn, article_id, summary)
    return {"id": article_id, "display_title": title, "url": f"https://example.com/{title}",
            "excerpt": "抜粋", "summary": summary}


def test_summaries_are_saved_and_reused(conn, cfg, patched):
    article = _article(conn, "記事A")
    assert summarize.summarize_pending_articles(conn, [article], cfg) == 1
    assert article["summary"] == "日本語の要約"

    saved = conn.execute("SELECT summary FROM articles WHERE id = ?", (article["id"],)).fetchone()
    assert saved["summary"] == "日本語の要約"

    # 2回目は要約済みなのでAPIを呼ばない（同じ記事に二重課金しない）
    calls_before = len(patched.messages.calls)
    assert summarize.summarize_pending_articles(conn, [article], cfg) == 0
    assert len(patched.messages.calls) == calls_before


def test_respects_max_articles_per_run(conn, cfg, patched):
    cfg = {**cfg, "summary": {**cfg["summary"], "max_articles_per_run": 2}}
    articles = [_article(conn, f"記事{i}") for i in range(5)]
    assert summarize.summarize_pending_articles(conn, articles, cfg) == 2
    assert len(patched.messages.calls) == 2


def test_disabled_flag_skips_everything(conn, cfg, patched):
    cfg = {**cfg, "summary": {**cfg["summary"], "enabled": False}}
    articles = [_article(conn, "記事X")]
    assert summarize.summarize_pending_articles(conn, articles, cfg) == 0
    assert patched.messages.calls == []


def test_missing_api_key_is_not_an_error(conn, cfg, monkeypatch):
    monkeypatch.setattr(summarize, "_build_client", lambda cfg: None)
    articles = [_article(conn, "記事Y")]
    assert summarize.summarize_pending_articles(conn, articles, cfg) == 0


# --- 表示側 ------------------------------------------------------------------

def test_digest_prefers_summary_over_excerpt(cfg):
    from src.digest import build_news_lines

    article = {
        "display_title": "見出し", "url": "https://example.com/1", "emoji": "📰",
        "first_source": "媒体", "is_critical": 0, "source_count": 1, "score": 0,
        "excerpt": "English excerpt text", "summary": "日本語の要約です。\n2行目です。",
        "critical_hits": "", "alerted_at": None, "priority": "中", "advice": "読む価値: 中",
    }
    body = "\n".join(build_news_lines([article], cfg))
    assert "日本語の要約です。" in body
    assert "2行目です。" in body
    assert "English excerpt text" not in body


def test_digest_falls_back_to_excerpt_without_summary(cfg):
    from src.digest import build_news_lines

    article = {
        "display_title": "見出し", "url": "https://example.com/1", "emoji": "📰",
        "first_source": "媒体", "is_critical": 0, "source_count": 1, "score": 0,
        "excerpt": "English excerpt text", "summary": "",
        "critical_hits": "", "alerted_at": None, "priority": "中", "advice": "読む価値: 中",
    }
    body = "\n".join(build_news_lines([article], cfg))
    assert "English excerpt text" in body
