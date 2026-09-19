"""記事を日本語で要約する（Claude Haiku を使用）。

ここだけがLLMを使う部分。APIキーが無い／失敗した場合は必ず None を返し、
本文抜粋にそのまま切り替わる。要約が理由でBOTが止まることはない。

費用は「表示する記事だけ要約する」「一度要約したらDBに保存して使い回す」
「1回の実行での件数に上限を設ける」の3点で抑えている。
"""
from __future__ import annotations

import logging
import os
import re

import requests

logger = logging.getLogger("crypto_news_bot.summarize")

PAGE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_SCRIPT_STYLE_PATTERN = re.compile(
    r"<(script|style|noscript|svg)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_TAG_PATTERN = re.compile(r"<[^>]+>")
_WS_PATTERN = re.compile(r"\s+")

SYSTEM_PROMPT = """あなたは暗号資産ニュースを日本語でまとめる編集者です。

出力の条件:
- 日本語で3〜5行。1行はおよそ40〜60文字。
- 記事に書かれている事実だけを使う。推測・一般論・投資判断は書かない。
- 金額・日付・割合・企業名などの具体的な数字と固有名詞は正確に残す。
- 専門用語には短い補足を付ける（例:「シーケンサー（取引を並べる装置）」）。
- 「この記事は」「本記事では」などの前置きは書かず、内容から始める。
- 箇条書きの記号（・や-）は付けず、文章を改行で区切る。
- 記事の内容が薄くて3行に満たない場合は、無理に埋めず書ける分だけ書く。"""


def fetch_article_text(url: str, timeout: int = 10, max_chars: int = 6000) -> str:
    """記事ページの本文らしきテキストを取得する。

    Google Newsのリンクは記事本文に到達できないため対象外。
    厳密な本文抽出はせず、タグを落として繋げただけのテキストを渡す
    （要約する側が不要部分を捨てられるため、これで足りる）。
    """
    if not url or "news.google.com" in url:
        return ""
    try:
        resp = requests.get(url, headers={"User-Agent": PAGE_USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.info("記事本文の取得に失敗しました url=%s error=%s", url[:80], e)
        return ""

    html = resp.text
    html = _SCRIPT_STYLE_PATTERN.sub(" ", html)
    text = _TAG_PATTERN.sub(" ", html)
    text = _WS_PATTERN.sub(" ", text).strip()
    return text[:max_chars]


def _build_client(cfg: dict):
    """Anthropicクライアントを作る。使えない場合は None。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.info("ANTHROPIC_API_KEY が未設定のため要約は行いません")
        return None
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic ライブラリが入っていないため要約は行いません")
        return None
    # ワークスペースに紐づいていないAPIキーは、リクエストごとに
    # どのワークスペースを使うかの指定が必要になる（未指定だと400）。
    # ワークスペース内で作ったキーなら不要なので、設定されているときだけ付ける。
    workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None

    try:
        return anthropic.Anthropic(default_headers=headers)
    except Exception as e:  # 認証情報の組み立て失敗など
        logger.warning("Anthropicクライアントの初期化に失敗しました error=%s", e)
        return None


def summarize_article(
    client,
    *,
    title: str,
    body: str,
    excerpt: str = "",
    cfg: dict,
) -> str | None:
    """1記事を日本語で要約する。失敗したら None（呼び出し側は抜粋にフォールバック）。"""
    if client is None:
        return None

    summary_cfg = cfg.get("summary", {})
    source_text = (body or "").strip() or (excerpt or "").strip()
    if not source_text:
        # 本文も抜粋も無ければ、見出しだけを訳しても価値が薄いので要約しない
        return None

    user_content = f"見出し: {title}\n\n本文:\n{source_text}"

    try:
        response = client.messages.create(
            model=summary_cfg.get("model", "claude-haiku-4-5"),
            max_tokens=summary_cfg.get("max_output_tokens", 500),
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as e:
        # 要約が失敗してもBOT全体は止めない（抜粋表示に戻るだけ）
        logger.warning("要約に失敗しました title=%s error=%s", title[:40], e)
        return None

    if getattr(response, "stop_reason", None) == "refusal":
        logger.info("要約が拒否されました title=%s", title[:40])
        return None

    parts = [block.text for block in response.content if block.type == "text"]
    summary = "\n".join(parts).strip()
    return summary or None


def summarize_pending_articles(conn, articles: list[dict], cfg: dict) -> int:
    """表示予定の記事のうち、まだ要約が無いものをまとめて要約してDBに保存する。

    戻り値は要約した件数。
    """
    from . import db

    summary_cfg = cfg.get("summary", {})
    if not summary_cfg.get("enabled"):
        return 0

    targets = [
        a for a in articles
        if not (a.get("summary") or "").strip() and (a.get("excerpt_url") or a.get("url"))
    ]
    if not targets:
        return 0

    client = _build_client(cfg)
    if client is None:
        return 0

    max_articles = summary_cfg.get("max_articles_per_run", 15)
    fetch_timeout = summary_cfg.get("fetch_timeout_seconds", 10)
    done = 0

    for article in targets[:max_articles]:
        url = article.get("excerpt_url") or article.get("url")
        body = fetch_article_text(url, timeout=fetch_timeout)
        summary = summarize_article(
            client,
            title=article.get("display_title", ""),
            body=body,
            excerpt=article.get("excerpt", ""),
            cfg=cfg,
        )
        if not summary:
            continue
        article["summary"] = summary
        if article.get("id"):
            db.set_summary(conn, article["id"], summary)
        done += 1

    if done:
        logger.info("記事を%d件要約しました", done)
    return done
