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

# この実行で使ったトークン数（モデル別）。費用の目安をログに出すために集計する。
_usage: dict[str, dict[str, int]] = {}


def _record_usage(model: str, response) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    entry = _usage.setdefault(model, {"input": 0, "output": 0, "calls": 0, "searches": 0})
    entry["input"] += getattr(usage, "input_tokens", 0) or 0
    entry["output"] += getattr(usage, "output_tokens", 0) or 0
    entry["calls"] += 1
    server = getattr(usage, "server_tool_use", None)
    entry["searches"] += (getattr(server, "web_search_requests", 0) or 0) if server else 0


def usage_report(cfg: dict) -> str | None:
    """この実行でのLLM使用量と費用の目安（円）。何も使っていなければ None。"""
    if not _usage:
        return None
    pricing = cfg.get("llm_pricing", {})
    usd_jpy = pricing.get("usd_jpy", 150)
    parts = []
    total_usd = 0.0
    for model, u in _usage.items():
        p = pricing.get(model, {})
        usd = (u["input"] * p.get("input", 0) + u["output"] * p.get("output", 0)) / 1_000_000
        searches = u.get("searches", 0)
        usd += searches * pricing.get("web_search_per_search", 0.01)
        total_usd += usd
        extra = f" 検索{searches}回" if searches else ""
        parts.append(f"{model}: {u['calls']}回 入力{u['input']:,}/出力{u['output']:,}トークン{extra} ≈ ¥{usd * usd_jpy:.1f}")
    return f"LLM使用量 合計≈¥{total_usd * usd_jpy:.1f} | " + " | ".join(parts)


def _model_kwargs(model: str, effort: str | None = None) -> dict:
    """モデルごとの追加パラメータ。

    Haiku 4.5 は effort を受け付けない（400になる）ので付けない。
    Sonnet 5 / Opus 5 は既定で思考が走るため、効いてほしい強さを effort で指定する。
    """
    if "haiku" in model:
        return {}
    return {"output_config": {"effort": effort}} if effort else {}

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

    model = summary_cfg.get("model", "claude-haiku-4-5")
    try:
        response = client.messages.create(
            model=model,
            max_tokens=summary_cfg.get("max_output_tokens", 500),
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
            **_model_kwargs(model, effort="low"),
        )
    except Exception as e:
        # 要約が失敗してもBOT全体は止めない（抜粋表示に戻るだけ）
        logger.warning("要約に失敗しました title=%s error=%s", title[:40], e)
        return None
    _record_usage(model, response)

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

    # 原因の特定は高度な判断なので、要約とは別に強いモデルを使う
    model = move_cfg.get("analysis_model") or cfg.get("summary", {}).get("model", "claude-haiku-4-5")
    try:
        response = client.messages.create(
            model=model,
            max_tokens=2000,  # 思考分を含む余裕（本文は2〜4行）
            system=MOVE_ANALYSIS_PROMPT,
            messages=[{"role": "user", "content": "\n".join(lines)}],
            **_model_kwargs(model, effort="high"),
        )
    except Exception as e:
        logger.warning("値動きの分析に失敗しました coin=%s error=%s", coin, e)
        return None
    _record_usage(model, response)

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


RELEVANCE_PROMPT = """あなたは暗号資産ニュースの編集者です。渡された記事が、その銘柄を
保有・監視している人にとって「読む意味があるか」を判定してください。

落とすべき記事（KEEPしない）:
{drop_reasons}

残すべき記事（KEEP）:
- 起きた事実が具体的に書かれていて、価格・供給量・利用状況・規制・セキュリティなど
  保有者の判断材料になるもの。
- 提携、上場、アップグレード、障害、ハッキング、規制当局の動き、トークンの供給変化など。

必ず次の形式だけで答えてください。説明は書かないでください。
KEEP
または
DROP: 落とす理由を10文字程度で"""


# 価値判定の結果（銘柄, 見出し）→ 残すか。自走モードは1プロセスで5.5時間動くので、
# 30分ごとに同じ記事をLLMで判定し直さないよう覚えておく（実測で同じ記事を30回判定していた）。
_relevance_verdicts: dict[tuple[str, str], bool] = {}


def filter_relevant_articles(articles: list[dict], coin: str, cfg: dict) -> list[dict]:
    """「読む意味がある」記事だけに絞る。

    キーワードでは価格予測の言い換えやDAOの内輪ネタを追い切れないため、
    最終判断をLLMに任せる。判定できない場合は落とさない（安全側に倒す）。
    """
    filter_cfg = cfg.get("relevance_filter", {})
    if not filter_cfg.get("enabled") or not articles:
        return articles

    client = _build_client(cfg)
    if client is None:
        return articles

    model = filter_cfg.get("model", "claude-haiku-4-5")
    drop_reasons = "\n".join(f"- {r}" for r in filter_cfg.get("drop_reasons", []))
    system = RELEVANCE_PROMPT.format(drop_reasons=drop_reasons)

    from .utils import contains_term

    always_keep = (filter_cfg.get("always_keep_keywords") or {}).get(coin, [])

    kept = []
    for article in articles:
        title = (article.get("display_title") or "").strip()
        body = (article.get("summary") or article.get("excerpt") or "").strip()

        # 銘柄の中核に触れている記事は判定に回さず必ず残す
        # （例: ARBにとってのRobinhood Chainは、見出しにARBが無くても重要）
        if any(contains_term(title, kw) for kw in always_keep):
            kept.append(article)
            continue
        remembered = _relevance_verdicts.get((coin, title))
        if remembered is not None:
            if remembered:
                kept.append(article)
            continue
        content = f"銘柄: {coin}\n見出し: {title}"
        if body:
            content += f"\n本文: {body[:600]}"
        try:
            response = client.messages.create(
                model=model, max_tokens=60, system=system,
                messages=[{"role": "user", "content": content}],
                **_model_kwargs(model, effort="low"),
            )
        except Exception as e:
            logger.info("価値判定に失敗したため残します title=%s error=%s", title[:30], e)
            kept.append(article)
            continue
        _record_usage(model, response)

        verdict = "".join(b.text for b in response.content if b.type == "text").strip()
        if verdict.upper().startswith("DROP"):
            _relevance_verdicts[(coin, title)] = False
            logger.info("%s 投稿する価値が低いため除外: %s（%s）", coin, title[:40], verdict[:40])
            continue
        _relevance_verdicts[(coin, title)] = True
        kept.append(article)

    dropped = len(articles) - len(kept)
    if dropped:
        logger.info("%s 価値の低い記事を%d件除外しました（残り%d件）", coin, dropped, len(kept))
    return kept
