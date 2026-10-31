from datetime import datetime, timedelta, timezone

from src import kaia_daily as k
from src.utils import JST

NOW = datetime(2026, 10, 24, 7, 0, tzinfo=JST)


def _kcfg():
    return {"hashtags": ["#Kaia", "#カイア"], "link_in_reply": True, "x_max_weighted": 280,
            "news_threshold": 8, "news_min_importance": 3, "news_max_age_days": 10,
            "weekday_themes": {0: "recap", 1: "howto", 2: "data", 3: "ecosystem", 4: "tech", 5: "japan", 6: "basics"}}


def _add(conn, url, *, title="Kaia news", kind="media", lang="en", hours_ago=10, story="s1",
         importance=4, sentiment="positive", price_talk=0, japan=0, used_on=None, source="Media",
         topic="kaia", digest_on=None):
    pub = (NOW - timedelta(hours=hours_ago)).astimezone(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO kaia_items(url, source, kind, lang, title, summary, body, published_at, first_seen, triaged, "
        "relevant, story, ja_title, importance, sentiment, price_talk, japan, used_on, topic, digest_on) "
        "VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, 1, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (url, source, kind, lang, title, "x" * 500, pub, pub, story, title, importance, sentiment,
         price_talk, japan, used_on, topic, digest_on),
    )
    conn.commit()


def _digest_cfg(**overrides):
    return {"kaia_daily": {**_kcfg(), "digest_max_items": 5, "digest_window_hours": 72,
                           "digest_first_window_hours": 168, "digest_min_importance": 2,
                           "digest_topic_limits": {"stablecoin": 2}, **overrides}}


def test_digest_picks_new_stories_in_section_order(conn):
    k.ensure_schema(conn)
    # 前回のまとめがある（初回扱いにしない）
    conn.execute("INSERT INTO kaia_posts(date, kind, created_at) VALUES ('2026-10-23', 'digest', 'x')")
    _add(conn, "https://sc1", story="sc1", topic="stablecoin", importance=4, lang="ja", japan=1, hours_ago=5)
    _add(conn, "https://sc2", story="sc2", topic="stablecoin", importance=4, lang="ja", japan=1, hours_ago=6)
    _add(conn, "https://sc3", story="sc3", topic="stablecoin", importance=4, lang="ja", japan=1, hours_ago=7)
    _add(conn, "https://j1", story="jpyc1", topic="jpyc", importance=3, kind="partner", lang="ja", hours_ago=20)
    _add(conn, "https://k1", story="kaia1", topic="kaia", importance=3, kind="official", hours_ago=30)
    _add(conn, "https://b1", story="both1", topic="both", importance=4, kind="partner", lang="ja", hours_ago=40)
    _add(conn, "https://old", story="old", topic="kaia", importance=5, hours_ago=24 * 4)       # 72時間より前
    _add(conn, "https://done", story="done", topic="jpyc", importance=5, hours_ago=3, digest_on="2026-10-23")
    _add(conn, "https://bad", story="bad", topic="kaia", importance=5, sentiment="negative", hours_ago=3)
    _add(conn, "https://price", story="price", topic="jpyc", importance=5, price_talk=1, hours_ago=3)
    _add(conn, "https://tiny", story="tiny", topic="kaia", importance=1, hours_ago=3)
    picked = k.digest_stories(conn, _digest_cfg(), NOW)
    assert [g["story"] for g in picked][:4] == ["both1", "kaia1", "jpyc1", "sc1"]
    assert len(picked) == 5
    assert sum(g["topic"] == "stablecoin" for g in picked) == 2  # 円ステーブルコインは2件まで


def test_digest_first_run_looks_back_one_week(conn):
    k.ensure_schema(conn)
    _add(conn, "https://w", story="week-old", topic="jpyc", importance=4, hours_ago=24 * 5)
    assert [g["story"] for g in k.digest_stories(conn, _digest_cfg(), NOW)] == ["week-old"]
    conn.execute("INSERT INTO kaia_posts(date, kind, created_at) VALUES ('2026-10-23', 'digest', 'x')")
    assert k.digest_stories(conn, _digest_cfg(), NOW) == []


def test_compose_community_inserts_sources_and_urls_by_program(conn):
    k.ensure_schema(conn)
    conn.execute("INSERT INTO kaia_posts(date, kind, created_at) VALUES ('2026-10-23', 'digest', 'x')")
    _add(conn, "https://prtimes.jp/a", story="both1", topic="both", kind="partner", source="JPYC（PR TIMES）",
         importance=4, hours_ago=5)
    _add(conn, "https://news.example/b", story="jpyc1", topic="jpyc", source="ニュース", importance=3, hours_ago=5)
    stories = k.digest_stories(conn, _digest_cfg(), NOW)
    digest = {"intro": "今日は2件です。", "closing": "", "items": [
        {"id": 1, "title": "JPYCとLINE NEXTが提携", "summary": "覚書を結びました。", "point": "チャージが便利に"},
        {"id": 2, "title": "空の話題", "summary": "", "point": ""},
    ]}
    text = k.compose_community(digest, stories, {"community_footer": "※投資助言ではありません"}, NOW)
    assert text.startswith("📰 Unifi・JPYC・Kaiaニュース｜10/24（土）")
    assert "【🤝 Kaia × JPYC】" in text and "1️⃣ JPYCとLINE NEXTが提携" in text
    assert "🔗 JPYC（PR TIMES）｜提携先の公式発表\nhttps://prtimes.jp/a" in text
    assert "空の話題" not in text and "【💴 JPYC】" not in text  # 要約が空の話題は載せない
    assert text.endswith("※投資助言ではありません")


def test_keyword_topic():
    # Unifi（LINE NEXT）の話は、JPYCが出てきてもUnifiの話として先頭に置く
    assert k._keyword_topic("JPYC、LINE NEXTと覚書") == "unifi"
    assert k._keyword_topic("LINEのUnifi、JPYCに対応") == "unifi"
    assert k._keyword_topic("JPYC、Kaiaチェーンに対応") == "both"
    assert k._keyword_topic("Kaia launches X") == "kaia"
    assert k._keyword_topic("JPYC累計発行額100億円") == "jpyc"
    assert k._keyword_topic("3メガバンクが円建てステーブルコイン") == "stablecoin"


def test_split_message_keeps_blocks_under_limit():
    text = "\n\n".join(["あ" * 800, "い" * 800, "う" * 800])
    parts = k._split_message(text, limit=1900)
    assert len(parts) == 2 and all(len(p) <= 1900 for p in parts)
    assert parts[1] == "う" * 800


def test_check_texts_ignores_numbers_in_urls():
    warnings = k.check_texts("JPYCの発表\n\n#Kaia", "詳しくは https://prtimes.jp/main/html/rd/p/000000333.000054018.html",
                             _kcfg(), "JPYCの発表", NOW)
    assert warnings == []


def test_x_weight_counts_japanese_as_two():
    assert k.x_weight("abc") == 3
    assert k.x_weight("カイア") == 6
    assert k.x_weight("見て https://example.com/very/long/path") == 4 + 1 + 23
    # 絵文字の異体字セレクタは数えない
    assert k.x_weight("❤️") == 2


def test_compose_x_fits_limit_with_hashtags():
    post = {"x_post": "あ" * 130}
    text = k.compose_x(post, _kcfg(), ["https://example.com"])
    assert text.endswith("#Kaia #カイア")
    assert "https://" not in text  # リンクはリプ欄に回す
    assert k.x_weight(text) <= 280


def test_unsupported_numbers_flags_made_up_figures():
    material = "流通額は3.3億円を突破。2026年10月14日に発表。参加者は1,360人。"
    text = "3.3億円を突破し、参加者は1360人に。10月14日に発表、利用者は25万人です。"
    assert k.unsupported_numbers(text, material, NOW) == ["25"]


def test_hype_words_detected():
    assert k.HYPE_WORDS.search("今が買い時です")
    assert not k.HYPE_WORDS.search("秘密鍵は絶対に人に教えないでください")


def test_pick_news_prefers_fresh_important_and_skips_bad_ones(conn):
    k.ensure_schema(conn)
    cfg = {"kaia_daily": _kcfg()}
    _add(conn, "https://a", story="minor", importance=2, hours_ago=5)
    _add(conn, "https://b", story="hack", importance=5, sentiment="negative", hours_ago=5)
    _add(conn, "https://c", story="price", importance=5, price_talk=1, hours_ago=5)
    _add(conn, "https://d", story="used", importance=5, hours_ago=5, used_on="2026-10-23")
    _add(conn, "https://e", story="partner", importance=4, japan=1, kind="official", hours_ago=20)
    _add(conn, "https://f", story="old", importance=5, hours_ago=24 * 12)
    story = k.pick_news(conn, cfg, NOW)
    assert story is not None and story["story"] == "partner"
    # 悪いニュースは投稿案にせず「知っておきたいニュース」として出す
    assert [g["story"] for g in k.cautions(conn, NOW)] == ["hack"]


def test_pick_news_returns_none_when_nothing_strong(conn):
    k.ensure_schema(conn)
    _add(conn, "https://a", story="weak", importance=3, hours_ago=24 * 6)  # 6 + 1 = 7 < 8
    assert k.pick_news(conn, {"kaia_daily": _kcfg()}, NOW) is None


def test_topics_rotate_least_recently_used(conn):
    k.ensure_schema(conn)
    topics = k.pick_topics(conn, "tech")
    assert topics, "定番ネタが読み込めること"
    first = topics[0]["id"]
    conn.execute("INSERT INTO kaia_posts(date, kind, topic_id, created_at) VALUES ('2026-10-20', 'tech', ?, 'x')", (first,))
    assert k.pick_topics(conn, "tech")[0]["id"] != first
    assert k.pick_topics(conn, "tech")[-1]["id"] == first


def test_every_topic_has_required_fields():
    themes = set()
    for t in k.load_topics():
        assert t["id"] and t["title"] and t["angle"] and t["urls"]
        assert t["theme"] in k.THEME_LABELS
        themes.add(t["theme"])
    assert {"howto", "ecosystem", "tech", "japan", "basics"} <= themes


def test_should_run_needs_webhook_and_time(conn, monkeypatch):
    cfg = {"kaia_daily": {**_kcfg(), "enabled": True, "hour_jst": 7, "window_hours": 6}}
    monkeypatch.delenv("WEBHOOK_KAIA", raising=False)
    assert not k.should_run(conn, cfg, NOW)
    monkeypatch.setenv("WEBHOOK_KAIA", "https://discord.example/webhook")
    assert k.should_run(conn, cfg, NOW)
    assert not k.should_run(conn, cfg, NOW.replace(hour=6))
    from src import db
    db.set_state(conn, "kaia_last_date", NOW.date().isoformat())
    assert not k.should_run(conn, cfg, NOW)


def test_title_similarity_separates_stories():
    same = ("JPYC、SBJ銀行・LINE NEXTと覚書 口座連携チャージ実証へ",
            "JPYC、韓国系SBJ銀行・LINE NEXTと提携──銀行口座からのチャージ機能 ...")
    other = ("Zept株式会社、日本円ステーブルコイン「JPYC」の取引を自動記録 ...",
             "Kaia、貢献報酬プログラム「CR」の対象資産に日本円ステーブルコイン「JPYC」が導入")
    assert k.title_similarity(*same) >= k.SAME_STORY_SIMILARITY
    assert k.title_similarity(*other) < k.SAME_STORY_SIMILARITY


def test_heuristic_triage_merges_same_story(conn):
    k.ensure_schema(conn)
    pub = NOW.astimezone(timezone.utc).isoformat()
    for n, title in enumerate(["JPYC、SBJ銀行・LINE NEXTと覚書 口座連携チャージ実証へ",
                               "JPYC、韓国系SBJ銀行・LINE NEXTと提携──銀行口座からのチャージ機能 ...",
                               "SBI、韓国最大級のカード会社と提携──円建てステーブルコインの ..."]):
        conn.execute("INSERT INTO kaia_items(url, source, kind, lang, title, summary, published_at, first_seen) "
                     "VALUES (?, 'm', 'media', 'ja', ?, '', ?, ?)", (f"https://x/{n}", title, pub, pub))
    k.triage(conn, {"kaia_daily": {}}, None, NOW)
    stories = [r["story"] for r in conn.execute("SELECT story FROM kaia_items ORDER BY url")]
    assert stories[0] == stories[1] != stories[2]


def test_headline_digest_without_ai(conn):
    k.ensure_schema(conn)
    _add(conn, "https://a", story="s", topic="jpyc", lang="ja", title="JPYC、累計発行額100億円を突破",
         kind="partner", source="JPYC（PR TIMES）", hours_ago=3)
    conn.execute("UPDATE kaia_items SET summary='JPYC株式会社は、累計発行額が100億円を突破したと発表しました。今後も拡大を目指します。'")
    stories = k.digest_stories(conn, _digest_cfg(), NOW)
    digest = k.headline_digest(stories)
    assert digest["x_post"] == "" and digest["items"][0]["summary"].endswith("。")
    text = k.compose_community(digest, stories, {}, NOW, require_summary=False)
    assert "1️⃣ JPYC、累計発行額100億円を突破" in text and "https://a" in text


def test_digest_skips_urls_already_on_web(conn):
    k.ensure_schema(conn)
    _add(conn, "https://shown", story="shown", topic="jpyc", hours_ago=3)
    _add(conn, "https://new", story="new", topic="jpyc", hours_ago=3)
    picked = k.digest_stories(conn, _digest_cfg(), NOW, published_urls={"https://shown"})
    assert [g["story"] for g in picked] == ["new"]


def test_publish_web_keeps_archive_and_writes_preview(conn, tmp_path, monkeypatch):
    k.ensure_schema(conn)
    monkeypatch.setattr(k, "BASE_DIR", tmp_path)
    monkeypatch.setattr(k, "chain_numbers", lambda: {"jpy": 1})
    monkeypatch.setattr(k, "unifi_rates", lambda now=None: {"jpyc": {"baseRate": 0.02}})
    monkeypatch.setattr(k, "kaia_price", lambda: {"jpy": 5.0})
    monkeypatch.setattr(k, "jpyc_supply", lambda: {"total": 3_000_000_000})
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    _add(conn, "https://a", story="s", topic="kaia", hours_ago=3)
    cfg = {"kaia_daily": {**_kcfg(), "web": {"enabled": True}}}
    site = {"archive": [{"date": "2026-10-23", "items": []}, {"date": "2026-10-24", "items": [], "old": True}]}
    entry = {"date": "2026-10-24", "items": []}
    assert k.publish_web(conn, cfg, NOW, entry=entry, site=site)
    import json
    data = json.loads((tmp_path / "data" / "kaia_site_preview.json").read_text(encoding="utf-8"))
    assert [a["date"] for a in data["archive"]] == ["2026-10-24", "2026-10-23"]
    assert "old" not in data["archive"][0]  # 同じ日のまとめは差し替える
    assert data["news"][0]["url"] == "https://a" and data["numbers"] == {"jpy": 1}
    assert data["topics"] and data["status"]["ai"] in ("none", "ok", "invalid_key")


def test_glossary_lists_terms_in_order_of_appearance():
    g = k.glossary_for("JPYCとLINE NEXTが覚書を締結。ステーブルコインのPoCを進める。", limit=4)
    assert [x["term"] for x in g] == ["JPYC", "LINE NEXT", "覚書（MOU）", "ステーブルコイン"]
    assert all(x["desc"] for x in g)
    # KAIA DLT Foundation の「KAIA」はトークンの説明を出さない
    assert not any(x["term"] == "KAIA" for x in k.glossary_for("KAIA DLT Foundationが発表"))


def _rows():
    return [{"topic": "both", "title": "JPYC×LINE NEXT、口座連携PoC", "summary": "SBJ銀行と3社で覚書。",
             "point": "Unifiへの円チャージ導線", "easy_title": "LINEのお財布に銀行から円を入れる実験",
             "easy_summary": "3つの会社が、銀行の口座からデジタルのお金を入れる仕組みを試します。",
             "easy_point": "LINEで円が使いやすくなるかも", "source": "JPYC（PR TIMES）", "label": "提携先の公式発表",
             "url": "https://prtimes.jp/a"}]


def test_two_versions_differ_by_audience():
    versions, glossary = k.compose_versions(_rows(), {"intro": "", "easy_intro": ""}, {"community_footer": "※注意書き"},
                                            "10/28（水）")
    easy, pro = versions["easy"], versions["pro"]
    assert easy.startswith("📰 今日のUnifi・JPYC・Kaiaニュース（やさしい版）｜10/28（水）")
    assert "1️⃣ LINEのお財布に銀行から円を入れる実験" in easy and "💡 ひとことで言うと：LINEで円が使いやすくなるかも" in easy
    assert "📖 今日のことば" in easy and "📖" not in pro
    assert pro.startswith("📰 Unifi・JPYC・Kaiaニュース｜10/28（水）")
    assert "1️⃣ JPYC×LINE NEXT、口座連携PoC" in pro and "👉 Unifiへの円チャージ導線" in pro
    assert easy.endswith("※注意書き") and pro.endswith("※注意書き")
    assert "https://prtimes.jp/a" in easy and "https://prtimes.jp/a" in pro
    assert glossary and glossary[0]["term"]


def test_restyle_old_entry_builds_both_versions():
    old = {"date": "2026-10-27", "mode": "headline", "community_text": "古い文章",
           "items": [{"topic": "jpyc", "title": "JPYC、累計発行額100億円を突破", "summary": "JPYCが発表しました。",
                      "point": "", "source": "JPYC（PR TIMES）", "label": "提携先の公式発表", "url": "https://p/1"}]}
    new = k.restyle_entry(old, {})
    assert [v["key"] for v in new["versions"]] == ["easy", "pro"]
    assert "10/27（火）" in new["versions"][0]["text"] and "JPYC" in new["versions"][1]["text"]
    assert new["items"][0]["easy_title"] == "JPYC、累計発行額100億円を突破"
    assert any(g["term"] == "JPYC" for g in new["glossary"])
    assert k.restyle_entry(new, {}) is new  # 直したものはそのまま


def test_single_versions_for_no_news_day():
    post = {"long_post": "Kaiaのガス代の仕組み", "easy_post": "Kaiaでお金を送るときの手数料（ガス代）の話"}
    versions, glossary = k.compose_single_versions(post, {"kind": "tech"}, {}, NOW)
    assert versions["easy"].startswith("💡 今日のKaia豆知識（やさしい版）")
    assert "ガス代" in [g["term"] for g in glossary]
    assert versions["pro"].startswith("💡 今日のKaia豆知識｜")


def test_easy_title_rules_for_governance_forum():
    assert k.easy_title_rule("[GP-27] StakingTrackerV3 Upgrade") == "Kaiaのルール変更の提案（GP-27）：StakingTrackerV3 Upgrade"
    assert k.easy_title_rule("Update on GC Member: Bughole") == "Kaiaの運営メンバー（GC）についてのお知らせ：Bughole"
    assert k.easy_title_rule("[CR] Contribution Reward Monthly Report - Epoch 2") == "貢献報酬（CR）の月ごとの報告（第2期）"
    assert k.easy_title_rule("JPYC、累計発行額100億円を突破") == "JPYC、累計発行額100億円を突破"


def test_detect_lang_from_title():
    assert k.detect_lang("JPYC stablecoin falls short of targets", "ja") == "en"
    assert k.detect_lang("JPYC、累計発行額100億円を突破", "en") == "ja"
    assert k.detect_lang("카이아, 업비트 상장", "ja") == "ko"
    assert k.detect_lang("円建て決済実証開始", "ja") == "ja"


def test_headline_mode_skips_english_article_from_japanese_search(conn):
    k.ensure_schema(conn)
    # 日本語のニュース検索から来た英語の記事（設定上は lang=ja）
    _add(conn, "https://en", story="en", topic="jpyc", lang="ja", title="JPYC stablecoin falls short of targets", hours_ago=3)
    _add(conn, "https://ja", story="ja", topic="jpyc", lang="ja", title="JPYC、累計発行額100億円を突破", hours_ago=3)
    picked = k.digest_stories(conn, _digest_cfg(), NOW, japanese_only=True)
    assert [g["story"] for g in picked] == ["ja"]


def test_unifi_is_told_apart_from_network_gear():
    assert k.mentions_unifi("LINEのステーブルコインウォレット「Unifi」")
    assert k.mentions_unifi("Unifi mini、日本で提供開始")
    assert k.mentions_unifi("LINE NEXT、Danalと提携")
    # ネットワーク機器のUniFi（Ubiquiti）は拾わない
    assert not k.mentions_unifi("Ubiquiti UniFi Dream Machine review")
    assert not k.mentions_unifi("UniFi Network 9.0 released")
    assert k.topic_match("Unifi Payが正式ローンチ")
    assert not k.topic_match("UniFi Protect camera firmware update")


def test_no_ai_when_use_ai_is_off(conn, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    cfg = {"kaia_daily": {"use_ai": False}}
    assert k._ai_client(cfg) is None
    k.ensure_schema(conn)
    assert k.site_status(conn, cfg)["ai"] == "off"


def test_unifi_section_comes_first(conn):
    k.ensure_schema(conn)
    conn.execute("INSERT INTO kaia_posts(date, kind, created_at) VALUES ('2026-10-23', 'digest', 'x')")
    _add(conn, "https://kaia1", story="kaia1", topic="kaia", lang="ja", hours_ago=3)
    _add(conn, "https://unifi1", story="unifi1", topic="unifi", kind="unifi", lang="ja", hours_ago=8)
    picked = k.digest_stories(conn, _digest_cfg(), NOW, set())
    assert [g["story"] for g in picked] == ["unifi1", "kaia1"]


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_unifi_announcements_become_feed_entries(monkeypatch):
    data = {"content": [{"id": "abc", "title": "【お知らせ】CRミッションにJPYCが追加", "content": "<p>本文</p>",
                         "createdAt": 1792486800000}], "hasNext": False}
    seen = {}

    def fake_get(url, params=None, timeout=None, headers=None):
        seen["lang"] = headers.get("Accept-Language")
        return _FakeResp(data)

    monkeypatch.setattr(k.requests, "get", fake_get)
    entries = k.unifi_announcement_entries()
    assert seen["lang"] == "ja-JP"
    assert entries[0]["link"] == "https://www.unifi.me/announcement/abc"
    assert k._entry_time(entries[0]).date().isoformat() == "2026-10-20"


def test_unifi_rates_by_token(monkeypatch):
    data = {"interestInfos": [
        {"contractAddress": "0xd077A400968890Eacc75cdc901F0356c943e4fDb", "baseRate": 0.03, "plusBaseRate": 0.07,
         "maxRate": 0.1, "kaiaBoostMaxRate": 0.03},
        {"contractAddress": "0xE7C3D8C9a439feDe00D2600032D5dB0Be71C3c29", "baseRate": 0.02, "maxRate": 0.02}]}
    monkeypatch.setattr(k.requests, "get", lambda *a, **kw: _FakeResp(data))
    rates = k.unifi_rates(NOW)
    assert rates["usdt"]["plusBaseRate"] == 0.07
    assert rates["jpyc"]["baseRate"] == 0.02
    assert rates["checked_at"].startswith("2026-10-24")


def test_daily_snapshots_keep_one_row_per_day():
    site = {"daily": [{"date": "2026-10-23", "kaia_jpy": 5.1}, {"date": "2026-10-24", "kaia_jpy": 5.2, "jpyc": 100}]}
    market = {"kaia": {"jpy": 5.3}, "jpyc": {}}
    unifi = {"jpyc": {"baseRate": 0.02}, "usdt": {"baseRate": 0.03, "plusBaseRate": 0.07}}
    days = k.daily_snapshots(site, NOW, market, unifi, {"tvl": 1})
    assert [d["date"] for d in days] == ["2026-10-23", "2026-10-24"]
    today = days[-1]
    # 今日の値は新しい値で上書きし、取れなかったJPYCは前の値を残す
    assert today["kaia_jpy"] == 5.3 and today["jpyc"] == 100 and today["unifi_usdt_plus"] == 0.07


def test_negative_words_without_false_alarms():
    for title in ("JPYC流通量、20億円割れ", "ブーストUSDT追加利息の支払い終了のお知らせ", "Orbit Bridge hacked for $81M",
                  "JPYC stablecoin falls short of targets", "商品券の購入一時停止について"):
        assert k.NEGATIVE_WORDS.search(title), title
    for title in ("KAIA airdrop campaign", "CR Mission Guide", "Unifi Pay正式ローンチ", "JPYC 1周年、累計発行額は120億円超"):
        assert not k.NEGATIVE_WORDS.search(title), title
    row = {"title": "JPYC、アップビットで1円割れ", "summary": "", "kind": "media", "lang": "ja", "url": "https://x"}
    assert k._heuristic_triage(row)["sentiment"] == "negative"


def test_unified_is_not_unifi_and_greeting_is_dropped():
    assert not k.mentions_unifi("Carnegie Mellon research shows unified theory of Web3 wallets")
    assert k.mentions_unifi("LINEのUnifiでJPYCが使える")
    item = {"lang": "ja", "summary": "こんにちは、Unifiチームです。 商品券の購入をメンテナンスのため一時停止します。"}
    assert k.excerpt_of(item, "商品券の購入一時停止について").startswith("商品券の購入をメンテナンス")


def test_japanese_only_titles_do_not_share_a_story(conn):
    a, b = k.story_key("商品券の購入一時停止について"), k.story_key("注目の「ステーブルコイン」、一体何が便利になるのか？")
    assert a != b and a != "-" and b != "-"
    k.ensure_schema(conn)
    _add(conn, "https://x1", title="商品券の購入一時停止について", story="-")
    _add(conn, "https://x2", title="注目のステーブルコイン", story="-")
    k.ensure_schema(conn)  # 古い目印でまとまっていた記事はほどける
    stories = {r["story"] for r in conn.execute("SELECT story FROM kaia_items")}
    assert len(stories) == 2 and "-" not in stories


def test_rate_changes_are_recorded_and_shown_as_news():
    prev = {"jpyc": {"baseRate": 0.02, "maxRate": 0.02}, "usdt": {"baseRate": 0.03, "plusBaseRate": 0.07, "kaiaBoostMaxRate": 0.03}}
    cur = {"jpyc": {"baseRate": 0.015, "maxRate": 0.015}, "usdt": {"baseRate": 0.03, "plusBaseRate": 0.07, "kaiaBoostMaxRate": 0}}
    ch = k.rate_changes(prev, cur, NOW)
    assert [c["text"] for c in ch] == ["JPYCのリワード率が年2%→年1.5%に", "USDTのKAIAブーストの上乗せ（年3%）が終了"]
    news = k.rate_change_news(ch, NOW)
    assert news[0]["title"].startswith("Unifiの") and news[0]["negative"] and news[0]["importance"] == 5
    assert k.rate_changes({}, cur, NOW) == []  # 前回が取れていないときは比べない
