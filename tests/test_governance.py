"""DAO投票（Snapshot）の速報を検証する。外部APIはモックする。"""
from datetime import datetime, timedelta, timezone

import pytest

from src import db, governance

# 実行処理は実際の現在時刻で判定するため、テストの基準時刻も現在時刻に合わせる
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _proposal(title, state, *, start_offset_h=-24, end_offset_h=48, scores=None, pid="0xabc"):
    return {
        "id": pid,
        "title": title,
        "body": "提案の本文",
        "state": state,
        "start": (NOW + timedelta(hours=start_offset_h)).timestamp(),
        "end": (NOW + timedelta(hours=end_offset_h)).timestamp(),
        "link": "https://snapshot.box/#/s:arbitrumfoundation.eth/proposal/0xabc",
        "choices": ["FOR", "AGAINST", "ABSTAIN"],
        "scores": scores or [90_000_000.0, 5_000_000.0, 1_000_000.0],
        "scores_total": sum(scores or [90_000_000.0, 5_000_000.0, 1_000_000.0]),
        "quorum": 0,
    }


# --- 通知する議題の絞り込み --------------------------------------------------

@pytest.mark.parametrize("title", [
    "[AIP] Enable ARB buyback from sequencer revenue",
    "Proposal: Adjust staking rewards",
    "[Constitutional] AIP: Automate Timeboost Proceeds Split",
    "Treasury management update",
    "Token burn mechanism for ARB",
])
def test_relevant_topics_are_notified(cfg, title):
    assert governance.is_relevant(title, cfg["governance"]["topic_keywords"]) is True


@pytest.mark.parametrize("title", [
    "OAT Elections",
    "Ratification of Security Council Election",
    "Banning projects identified in the high-severity Watchdog Program",
    "Extending DRIP's Mandate",
])
def test_irrelevant_topics_are_skipped(cfg, title):
    """役員選挙や内輪の手続きは通知しない（実データで届いていた無駄な議題）。"""
    assert governance.is_relevant(title, cfg["governance"]["topic_keywords"]) is False


# --- どの段階で知らせるか ----------------------------------------------------

def test_active_vote_is_announced_as_start():
    p = _proposal("ARB buyback", "active", end_offset_h=72)
    assert governance.decide_stage(p, NOW, 24) == "start"


def test_active_vote_near_deadline_is_announced_as_ending():
    p = _proposal("ARB buyback", "active", end_offset_h=12)
    assert governance.decide_stage(p, NOW, 24) == "ending"


def test_pending_vote_is_not_announced():
    """投票前（pending）の段階では通知しない。"""
    p = _proposal("ARB buyback", "pending", end_offset_h=100)
    assert governance.decide_stage(p, NOW, 24) is None


def test_recently_closed_vote_is_announced():
    p = _proposal("ARB buyback", "closed", end_offset_h=-6)
    assert governance.decide_stage(p, NOW, 24, 48) == "closed"


def test_old_closed_vote_is_not_announced():
    """初回実行で過去の結果を一斉に投稿しないためのガード。"""
    p = _proposal("ARB buyback", "closed", end_offset_h=-200)
    assert governance.decide_stage(p, NOW, 24, 48) is None


# --- 投稿内容 ----------------------------------------------------------------

def test_start_embed_shows_the_deadline():
    embed = governance.build_embed(_proposal("ARB buyback", "active", end_offset_h=48), "start", NOW)
    assert "投票が始まりました" in embed["title"]
    assert "投票期間" in embed["description"]
    assert "締切まで約48時間" in embed["description"]


def test_ending_embed_shows_current_tally():
    embed = governance.build_embed(_proposal("ARB buyback", "active", end_offset_h=10), "ending", NOW)
    assert "締切まで約10時間" in embed["title"]
    assert "93.8%" in embed["description"]  # 90M / 96M


def test_closed_embed_shows_the_result():
    embed = governance.build_embed(_proposal("ARB buyback", "closed", end_offset_h=-2), "closed", NOW)
    assert "終了しました" in embed["title"]
    assert "FOR" in embed["description"]
    assert "結果" in embed["description"]


def test_embed_stays_within_discord_limit():
    p = _proposal("ARB buyback", "closed", end_offset_h=-2)
    p["body"] = "長い本文。" * 2000
    embed = governance.build_embed(p, "closed", NOW)
    assert len(embed["description"]) <= 4096


# --- 重複通知の防止 ----------------------------------------------------------

def test_same_proposal_and_stage_is_not_posted_twice(conn, cfg, monkeypatch):
    monkeypatch.setattr(
        governance, "fetch_proposals",
        lambda api_url, space: [_proposal("[AIP] ARB buyback from revenue", "active", end_offset_h=48)],
    )
    coins = {"ARB": (cfg["coins"]["ARB"], "https://discord.example/arb")}
    kwargs = dict(conn=conn, cfg=cfg, coins=coins, dry_run=True)

    assert governance.run_governance_alerts(**kwargs) == 1
    assert governance.run_governance_alerts(**kwargs) == 0, "同じ段階は一度だけ"


def test_ending_is_posted_even_after_start_was_posted(conn, cfg, monkeypatch):
    """開始を知らせた提案でも、締切間近はもう一度知らせる。"""
    coins = {"ARB": (cfg["coins"]["ARB"], "https://discord.example/arb")}

    monkeypatch.setattr(governance, "fetch_proposals",
                        lambda a, s: [_proposal("[AIP] ARB buyback", "active", end_offset_h=48)])
    assert governance.run_governance_alerts(conn=conn, cfg=cfg, coins=coins, dry_run=True) == 1

    monkeypatch.setattr(governance, "fetch_proposals",
                        lambda a, s: [_proposal("[AIP] ARB buyback", "active", end_offset_h=6)])
    assert governance.run_governance_alerts(conn=conn, cfg=cfg, coins=coins, dry_run=True) == 1


def test_irrelevant_proposal_is_never_posted(conn, cfg, monkeypatch):
    monkeypatch.setattr(governance, "fetch_proposals",
                        lambda a, s: [_proposal("OAT Elections", "active", end_offset_h=48)])
    coins = {"ARB": (cfg["coins"]["ARB"], "https://discord.example/arb")}
    assert governance.run_governance_alerts(conn=conn, cfg=cfg, coins=coins, dry_run=True) == 0


def test_api_failure_is_harmless(conn, cfg, monkeypatch):
    monkeypatch.setattr(governance, "fetch_proposals", lambda a, s: [])
    coins = {"ARB": (cfg["coins"]["ARB"], "https://discord.example/arb")}
    assert governance.run_governance_alerts(conn=conn, cfg=cfg, coins=coins, dry_run=True) == 0


def test_disabled_governance_does_nothing(conn, cfg, monkeypatch):
    called = []
    monkeypatch.setattr(governance, "fetch_proposals", lambda a, s: called.append(1) or [])
    disabled = {**cfg, "governance": {**cfg["governance"], "enabled": False}}
    coins = {"ARB": (cfg["coins"]["ARB"], "https://discord.example/arb")}
    assert governance.run_governance_alerts(conn=conn, cfg=disabled, coins=coins, dry_run=True) == 0
    assert called == []


def test_snapshot_is_the_source_and_needs_no_api_key(cfg):
    """ソースはSnapshotの公開API。Tallyは401でキーが必要なため使わない。"""
    assert cfg["governance"]["api_url"] == "https://hub.snapshot.org/graphql"
    assert cfg["governance"]["spaces"][0]["space"] == "arbitrumfoundation.eth"
