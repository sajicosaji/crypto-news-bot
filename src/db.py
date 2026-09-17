"""SQLiteへの保存。記事・価格・オンチェーン指標・速報履歴を保持する。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalized_title TEXT NOT NULL,
    display_title TEXT NOT NULL,
    url TEXT NOT NULL,
    first_source TEXT NOT NULL,
    first_published_at TEXT NOT NULL,
    score INTEGER NOT NULL,
    sentiment TEXT NOT NULL,
    emoji TEXT NOT NULL,
    is_critical INTEGER NOT NULL DEFAULT 0,
    critical_hits TEXT,
    alerted_at TEXT,
    alert_reason TEXT,
    created_at TEXT NOT NULL,
    excerpt TEXT,
    excerpt_url TEXT,
    excerpt_source TEXT
);

CREATE INDEX IF NOT EXISTS idx_articles_normalized_title
    ON articles(normalized_title);
CREATE INDEX IF NOT EXISTS idx_articles_first_published_at
    ON articles(first_published_at);

CREATE TABLE IF NOT EXISTS article_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL REFERENCES articles(id),
    source_name TEXT NOT NULL,
    url TEXT NOT NULL,
    published_at TEXT NOT NULL,
    UNIQUE(article_id, source_name, url)
);

CREATE TABLE IF NOT EXISTS article_coins (
    article_id INTEGER NOT NULL REFERENCES articles(id),
    coin TEXT NOT NULL,
    PRIMARY KEY (article_id, coin)
);

CREATE TABLE IF NOT EXISTS prices (
    coin TEXT NOT NULL,
    ts TEXT NOT NULL,
    usd REAL,
    jpy REAL,
    usd_24h_change REAL,
    PRIMARY KEY (coin, ts)
);

CREATE TABLE IF NOT EXISTS onchain_metrics (
    coin TEXT NOT NULL,
    metric TEXT NOT NULL,
    ts TEXT NOT NULL,
    value REAL,
    PRIMARY KEY (coin, metric, ts)
);

CREATE TABLE IF NOT EXISTS alerts_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    message TEXT,
    ts TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_log_lookup
    ON alerts_log(coin, alert_type, dedupe_key, ts);

CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """既存のDBに後から追加した列を足す（古いDBでも壊さずに使えるようにする）。"""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
    for column in ("excerpt", "excerpt_url", "excerpt_source"):
        if column not in existing:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {column} TEXT")
    conn.commit()


def prune_old_data(conn: sqlite3.Connection, retention_days: int = 30) -> dict[str, int]:
    """古いデータを削除してDBが際限なく大きくならないようにする。

    速報の重複防止・日次まとめ・週次振り返りに必要なのは直近数週間分だけ。
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    old_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM articles WHERE first_published_at < ?", (cutoff,)
        )
    ]
    for article_id in old_ids:
        conn.execute("DELETE FROM article_sources WHERE article_id = ?", (article_id,))
        conn.execute("DELETE FROM article_coins WHERE article_id = ?", (article_id,))
        conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))

    prices_cur = conn.execute("DELETE FROM prices WHERE ts < ?", (cutoff,))
    metrics_cur = conn.execute("DELETE FROM onchain_metrics WHERE ts < ?", (cutoff,))
    alerts_cur = conn.execute("DELETE FROM alerts_log WHERE ts < ?", (cutoff,))
    conn.commit()
    conn.execute("VACUUM")
    return {
        "articles": len(old_ids),
        "prices": prices_cur.rowcount,
        "onchain_metrics": metrics_cur.rowcount,
        "alerts": alerts_cur.rowcount,
    }


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv_state(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def find_article_by_normalized_title(
    conn: sqlite3.Connection, normalized_title: str
) -> sqlite3.Row | None:
    """正規化タイトルが一致する既存記事を探す（公開日時に関わらず全期間から検索する）。

    Google Newsの検索結果には数ヶ月前の記事も混ざるため、直近数日だけに絞ると
    同じ記事が何度も「新規記事」として重複登録され、速報が繰り返されてしまう。
    """
    return conn.execute(
        "SELECT * FROM articles WHERE normalized_title = ? "
        "ORDER BY first_published_at DESC LIMIT 1",
        (normalized_title,),
    ).fetchone()


def recent_articles_with_source_count(
    conn: sqlite3.Connection, coin: str, since_iso: str
) -> list[dict]:
    """指定時刻以降の記事を、報道媒体数つきで返す（値動きの背景候補用）。"""
    rows = conn.execute(
        "SELECT a.*, "
        "  (SELECT COUNT(DISTINCT source_name) FROM article_sources s WHERE s.article_id = a.id) "
        "  AS source_count "
        "FROM articles a JOIN article_coins ac ON ac.article_id = a.id "
        "WHERE ac.coin = ? AND a.first_published_at >= ? "
        "ORDER BY a.first_published_at DESC",
        (coin, since_iso),
    ).fetchall()
    return [dict(r) for r in rows]


def recent_articles_for_similarity(
    conn: sqlite3.Connection, since_iso: str, limit: int = 300
) -> list[sqlite3.Row]:
    """言い換え記事の判定に使う、直近の記事一覧（銘柄付き）。"""
    return conn.execute(
        "SELECT a.id, a.normalized_title, "
        "       (SELECT GROUP_CONCAT(ac.coin) FROM article_coins ac WHERE ac.article_id = a.id) AS coins "
        "FROM articles a WHERE a.first_published_at >= ? "
        "ORDER BY a.first_published_at DESC LIMIT ?",
        (since_iso, limit),
    ).fetchall()


def insert_article(
    conn: sqlite3.Connection,
    *,
    normalized_title: str,
    display_title: str,
    url: str,
    first_source: str,
    first_published_at: str,
    score: int,
    sentiment: str,
    emoji: str,
    is_critical: bool,
    critical_hits: list[str],
    coins: list[str],
    excerpt: str | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO articles ("
        "normalized_title, display_title, url, first_source, first_published_at, "
        "score, sentiment, emoji, is_critical, critical_hits, created_at, "
        "excerpt, excerpt_url, excerpt_source"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            normalized_title,
            display_title,
            url,
            first_source,
            first_published_at,
            score,
            sentiment,
            emoji,
            1 if is_critical else 0,
            ",".join(critical_hits),
            now,
            excerpt,
            url if excerpt else None,
            first_source if excerpt else None,
        ),
    )
    article_id = cur.lastrowid
    conn.execute(
        "INSERT OR IGNORE INTO article_sources(article_id, source_name, url, published_at) "
        "VALUES (?, ?, ?, ?)",
        (article_id, first_source, url, first_published_at),
    )
    for coin in coins:
        conn.execute(
            "INSERT OR IGNORE INTO article_coins(article_id, coin) VALUES (?, ?)",
            (article_id, coin),
        )
    conn.commit()
    return article_id


def add_source_to_article(
    conn: sqlite3.Connection, article_id: int, source_name: str, url: str, published_at: str
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO article_sources(article_id, source_name, url, published_at) "
        "VALUES (?, ?, ?, ?)",
        (article_id, source_name, url, published_at),
    )
    conn.commit()


def set_excerpt_if_missing(
    conn: sqlite3.Connection, article_id: int, excerpt: str, url: str, source: str
) -> bool:
    """抜粋が未設定の記事にだけ抜粋を補う。

    同じ記事でもGoogle News経由だと本文が取れないため、あとから本文抜粋が取れる媒体で
    見つかった場合に、そちらの抜粋とリンクで補完する。
    """
    row = conn.execute("SELECT excerpt FROM articles WHERE id = ?", (article_id,)).fetchone()
    if row is None or (row["excerpt"] or "").strip():
        return False
    conn.execute(
        "UPDATE articles SET excerpt = ?, excerpt_url = ?, excerpt_source = ? WHERE id = ?",
        (excerpt, url, source, article_id),
    )
    conn.commit()
    return True


def count_sources(conn: sqlite3.Connection, article_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT source_name) AS c FROM article_sources WHERE article_id = ?",
        (article_id,),
    ).fetchone()
    return row["c"] if row else 0


def mark_alerted(conn: sqlite3.Connection, article_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE articles SET alerted_at = ?, alert_reason = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), reason, article_id),
    )
    conn.commit()


def save_price(conn: sqlite3.Connection, coin: str, ts: str, usd: float, jpy: float, usd_24h_change: float | None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO prices(coin, ts, usd, jpy, usd_24h_change) VALUES (?, ?, ?, ?, ?)",
        (coin, ts, usd, jpy, usd_24h_change),
    )
    conn.commit()


def save_onchain_metric(conn: sqlite3.Connection, coin: str, metric: str, ts: str, value: float | None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO onchain_metrics(coin, metric, ts, value) VALUES (?, ?, ?, ?)",
        (coin, metric, ts, value),
    )
    conn.commit()


def log_alert(conn: sqlite3.Connection, coin: str, alert_type: str, dedupe_key: str, message: str, ts: str) -> None:
    conn.execute(
        "INSERT INTO alerts_log(coin, alert_type, dedupe_key, message, ts) VALUES (?, ?, ?, ?, ?)",
        (coin, alert_type, dedupe_key, message, ts),
    )
    conn.commit()


def last_alert_time(conn: sqlite3.Connection, coin: str, alert_type: str, dedupe_key: str) -> str | None:
    row = conn.execute(
        "SELECT ts FROM alerts_log WHERE coin = ? AND alert_type = ? AND dedupe_key = ? "
        "ORDER BY ts DESC LIMIT 1",
        (coin, alert_type, dedupe_key),
    ).fetchone()
    return row["ts"] if row else None
