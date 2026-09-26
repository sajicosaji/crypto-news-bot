"""DAOの投票（Snapshot）を監視して速報を出す。

ソースは Snapshot の公開GraphQL API（APIキー不要・無料）。
Arbitrumは Snapshot で先に投票してからオンチェーンに進む流れなので、
ここを見るのが一番早い。Tally はAPIキーが必須（401）なため使わない。

同じ提案の同じ段階を二度通知しないよう、通知済みは alerts_log に記録する。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from . import db, discord
from .utils import contains_term, format_jst

logger = logging.getLogger("crypto_news_bot.governance")

REQUEST_TIMEOUT = 20

PROPOSALS_QUERY = """
query Proposals($space: String!) {
  proposals(
    first: 20
    where: { space: $space }
    orderBy: "created"
    orderDirection: desc
  ) {
    id
    title
    body
    state
    start
    end
    link
    choices
    scores
    scores_total
    quorum
  }
}
"""


def fetch_proposals(api_url: str, space: str) -> list[dict]:
    """Snapshotから直近の提案を取得する。失敗しても例外は投げない。"""
    try:
        resp = requests.post(
            api_url, json={"query": PROPOSALS_QUERY, "variables": {"space": space}},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("Snapshotの取得に失敗しました space=%s error=%s", space, e)
        return []
    if data.get("errors"):
        logger.warning("Snapshotがエラーを返しました space=%s error=%s", space, data["errors"])
        return []
    return data.get("data", {}).get("proposals") or []


def is_relevant(title: str, keywords: list[str]) -> bool:
    """通知する価値のある議題か（役員選挙や細かい助成金は拾わない）。"""
    return any(contains_term(title, kw) for kw in keywords)


def decide_stage(
    proposal: dict, now: datetime, hours_before_end: int, max_age_hours_for_closed: int = 48
) -> str | None:
    """この提案について今知らせるべき段階を返す。

    "start"（投票開始）/ "ending"（締切間近）/ "closed"（結果確定）/ None（何もしない）

    終了からだいぶ経った投票は通知しない。初回実行やDB引き継ぎ失敗のときに、
    過去の結果を一斉に投稿してしまうのを防ぐため。
    """
    state = proposal.get("state")
    end = proposal.get("end")
    if state == "closed":
        if end is None:
            return None
        hours_since_end = (now.timestamp() - end) / 3600
        if hours_since_end > max_age_hours_for_closed:
            return None
        return "closed"
    if state == "active":
        if end is None:
            return "start"
        hours_left = (end - now.timestamp()) / 3600
        if 0 < hours_left <= hours_before_end:
            return "ending"
        return "start"
    return None  # pending（投票前）は通知しない


def format_scores(proposal: dict) -> list[str]:
    """賛否の内訳を読める形にする。"""
    choices = proposal.get("choices") or []
    scores = proposal.get("scores") or []
    total = proposal.get("scores_total") or 0
    lines = []
    for choice, score in zip(choices, scores):
        pct = (score / total * 100) if total else 0
        lines.append(f"　{choice}: {score:,.0f} ARB（{pct:.1f}%）")
    return lines


def build_embed(proposal: dict, stage: str, now: datetime) -> dict:
    """段階に応じた投稿内容を作る。"""
    title = proposal.get("title", "")
    end_dt = datetime.fromtimestamp(proposal["end"], tz=timezone.utc) if proposal.get("end") else None
    start_dt = datetime.fromtimestamp(proposal["start"], tz=timezone.utc) if proposal.get("start") else None

    lines: list[str] = []
    if stage == "start":
        heading = "🗳️ DAO投票が始まりました"
        color = 0x3498DB
        if start_dt and end_dt:
            lines.append(f"投票期間: {format_jst(start_dt)} 〜 {format_jst(end_dt)}")
        if end_dt:
            hours_left = (proposal["end"] - now.timestamp()) / 3600
            lines.append(f"締切まで約{hours_left:.0f}時間")
    elif stage == "ending":
        hours_left = (proposal["end"] - now.timestamp()) / 3600
        heading = f"⏰ DAO投票の締切まで約{hours_left:.0f}時間"
        color = 0xE67E22
        if end_dt:
            lines.append(f"締切: {format_jst(end_dt)}")
        lines.append("現在の得票:")
        lines.extend(format_scores(proposal))
    else:  # closed
        scores = proposal.get("scores") or []
        choices = proposal.get("choices") or []
        total = proposal.get("scores_total") or 0
        top = ""
        if scores and total:
            idx = scores.index(max(scores))
            top_pct = scores[idx] / total * 100
            top = f"{choices[idx]}（{top_pct:.1f}%）"
        heading = "✅ DAO投票が終了しました"
        color = 0x2ECC71
        if top:
            lines.append(f"結果: **{top}**")
        lines.append("内訳:")
        lines.extend(format_scores(proposal))

    body = (proposal.get("body") or "").strip()
    if body:
        snippet = " ".join(body.split())[:300]
        lines.append("")
        lines.append(snippet + ("…" if len(body) > 300 else ""))

    return {
        "title": f"{heading}: {title[:200]}",
        "url": proposal.get("link"),
        "description": "\n".join(lines)[:3900],
        "color": color,
    }


def run_governance_alerts(
    *, conn, cfg: dict, coins: dict, dry_run: bool
) -> int:
    """監視対象スペースの投票をチェックして速報を出す。投稿件数を返す。"""
    gov_cfg = cfg.get("governance", {})
    if not gov_cfg.get("enabled"):
        return 0

    api_url = gov_cfg.get("api_url", "https://hub.snapshot.org/graphql")
    keywords = gov_cfg.get("topic_keywords", [])
    hours_before_end = gov_cfg.get("hours_before_end", 24)
    now = datetime.now(timezone.utc)
    posted = 0

    stage_enabled = {
        "start": gov_cfg.get("notify_on_start", True),
        "ending": gov_cfg.get("notify_before_end", True),
        "closed": gov_cfg.get("notify_on_close", True),
    }

    for entry in gov_cfg.get("spaces", []):
        space, coin = entry.get("space"), entry.get("coin")
        if not space or coin not in coins:
            continue
        coin_cfg, webhook_url = coins[coin]

        for proposal in fetch_proposals(api_url, space):
            title = proposal.get("title", "")
            if not is_relevant(title, keywords):
                continue
            stage = decide_stage(
                proposal, now, hours_before_end,
                gov_cfg.get("max_age_hours_for_closed", 48),
            )
            if not stage or not stage_enabled.get(stage):
                continue

            # 同じ提案の同じ段階は一度だけ通知する
            dedupe_key = f"snapshot:{proposal['id'][:20]}:{stage}"
            if db.last_alert_time(conn, coin, "governance", dedupe_key):
                continue

            embed = build_embed(proposal, stage, now)
            ok = discord.post_webhook(
                webhook_url, username=coin_cfg["username"],
                content=cfg.get("mention") or None, embeds=[embed],
                dry_run=dry_run, min_interval_seconds=cfg["discord"]["post_interval_seconds"],
            )
            if ok:
                db.log_alert(conn, coin, "governance", dedupe_key, title[:120], now.isoformat())
                posted += 1
                logger.info("%s DAO投票の速報を投稿しました（%s）: %s", coin, stage, title[:50])

    return posted
