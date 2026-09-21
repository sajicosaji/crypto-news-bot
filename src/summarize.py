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


MOVE_ANALYSIS_PROMPT = """あなたは暗号資産の値動きを調査するアナリストです。
ある銘柄がビットコイン（相場全体）から乖離して大きく動きました。渡された直近のニュース一覧から、
この値動きの原因になった可能性が高いものを特定してください。

出力の条件:
- 日本語で2〜4行。
- 原因として考えられる記事があれば、何が起きたかと、なぜ値動きにつながるかを書く。
- 複数の要因がありそうなら、影響が大きい順に書く。
- ニュース一覧の中に原因らしいものが無ければ、必ず「直近のニュースには明確な原因が見当たりません」と
  書き、相場全体・大口の売買・テクニカル要因など、ニュース以外の可能性に一言触れる。
- 記事に書かれていないことを断定しない。「〜の可能性」「〜とみられる」と書く。
- 投資判断（買うべき・売るべき）は書かない。
- 前置きは不要。結論から書く。"""


def analyze_move_with_llm(
    client,
    *,
    coin: str,
    coin_change_24h: float,
    btc_change_24h: float,
    excess_pct: float,
    articles: list[dict],
    cfg: dict,
) -> str | None:
    """銘柄固有の値動きについて、直近の記事から原因を分析する。

    キーワードでは拾えない原因（言い換え、間接的な要因）を読み取るため、
    向きに関係なく直近の記事をすべて渡して判断させる。失敗時は None。
    """
    if client is None:
        return None

    move_cfg = cfg.get("move_context", {})
    max_articles = move_cfg.get("analysis_max_articles", 15)

    direction = "上昇" if coin_change_24h >= 0 else "下落"
    lines = [
        f"銘柄: {coin}",
        f"24時間の変化: {coin_change_24h:+.1f}%（{direction}）",
        f"同じ期間のBTC: {btc_change_24h:+.1f}%",
        f"BTCとの差（銘柄固有の動き）: {excess_pct:+.1f}%",
        "",
        "直近のニュース一覧（新しい順）:",
    ]
    if not articles:
        lines.append("（該当する記事はありません）")
    for i, a in enumerate(articles[:max_articles], 1):
        title = (a.get("display_title") or "").strip()
        source = a.get("excerpt_source") or a.get("first_source") or ""
        body = (a.get("summary") or a.get("excerpt") or "").strip()
        n = a.get("source_count", 1)
        lines.append(f"{i}. {title}（{source} / {n}媒体）")
        if body:
            lines.append(f"   {body[:300]}")

    try:
        response = client.messages.create(
            model=cfg.get("summary", {}).get("model", "claude-haiku-4-5"),
            max_tokens=400,
            system=MOVE_ANALYSIS_PROMPT,
            messages=[{"role": "user", "content": "\n".join(lines)}],
        )
    except Exception as e:
        logger.warning("値動きの分析に失敗しました coin=%s error=%s", coin, e)
        return None

    if getattr(response, "stop_reason", None) == "refusal":
        return None
    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    return text or None


def investigate_move(
    *, coin: str, analysis: dict, coin_change_24h: float | None, btc_change_24h: float | None,
    recent_articles: list[dict], cfg: dict,
) -> str | None:
    """銘柄固有の値動きの原因を、直近の記事からHaikuに分析させる。

    「乱高下があったら必ず調べる」ための処理。LLMが使えない場合は None を返し、
    呼び出し側はキーワードで拾った記事の一覧だけを出す。
    """
    move_cfg = cfg.get("move_context", {})
    if not move_cfg.get("llm_analysis", True) or not analysis.get("is_coin_specific"):
        return None
    if coin_change_24h is None or btc_change_24h is None:
        return None
    client = _build_client(cfg)
    return analyze_move_with_llm(
        client, coin=coin, coin_change_24h=coin_change_24h, btc_change_24h=btc_change_24h,
        excess_pct=analysis.get("excess_pct") or 0.0, articles=recent_articles, cfg=cfg,
    )
