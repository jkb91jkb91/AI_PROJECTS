"""SQLite storage for video summaries + full-text search (FTS5).

One file on disk (YT_MCP_DB, default: ./yt_mcp.db). A new connection is opened per
call, so the MCP request threads and the background indexing worker can both use it.
"""

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("YT_MCP_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "yt_mcp.db"))

# Bump when the summarisation prompt/schema changes; videos with an older version are re-summarised.
SUMMARY_VERSION = 2

# Errors that retrying will not fix (the video simply has no subtitles).
PERMANENT_ERROR = "No subtitles available"

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id        TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    channel         TEXT,
    title           TEXT,
    upload_date     TEXT,              -- YYYYMMDD
    language        TEXT,              -- language of the transcript that was summarised
    status          TEXT NOT NULL,     -- pending | done | error
    summary         TEXT,
    topics          TEXT,              -- JSON list
    stances         TEXT,              -- JSON list of {topic, position}
    summary_version INTEGER,
    error           TEXT,
    input_tokens    INTEGER,           -- Haiku tokens actually billed for this summary
    output_tokens   INTEGER,
    summarised_at   TEXT,              -- UTC timestamp of when the summary was produced
    updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_videos_channel_date ON videos(channel, upload_date);

-- One open row per sync_channel run (until it reports complete=true); lets every call of the
-- same run count 'from database' vs 'summarised now' against the same starting point.
CREATE TABLE IF NOT EXISTS sync_runs (
    run_key    TEXT PRIMARY KEY,
    started_at TEXT NOT NULL
);

-- channel address -> channel name, learned whenever a channel is listed on YouTube. Lets the
-- database-only tools accept the same channel URL as sync_channel.
CREATE TABLE IF NOT EXISTS channel_urls (
    url_key TEXT PRIMARY KEY,
    channel TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts USING fts5(
    title, summary, topics, stances,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


_NEW_COLUMNS = {"input_tokens": "INTEGER", "output_tokens": "INTEGER", "summarised_at": "TEXT"}


def now() -> str:
    """UTC timestamp as a sortable string (compare with <, >=)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that older database files do not have yet."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
    for name, column_type in _NEW_COLUMNS.items():
        if name not in existing:
            try:
                conn.execute(f"ALTER TABLE videos ADD COLUMN {name} {column_type}")
            except sqlite3.OperationalError:
                pass  # another thread added it first


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_video(video_id: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()


def upsert_pending(video_id: str, url: str, channel: str | None, title: str | None) -> bool:
    """Register a video for summarising. Returns True if it still needs processing
    (new, previously failed, or summarised with an older prompt version).
    Videos that failed with PERMANENT_ERROR are not retried."""
    with connect() as conn:
        row = conn.execute(
            "SELECT status, summary_version, error FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()
        if row is not None and row["status"] == "error" and PERMANENT_ERROR in (row["error"] or ""):
            return False  # no subtitles will not appear on retry; don't hit YouTube again
        if row is None:
            conn.execute(
                "INSERT INTO videos (video_id, url, channel, title, status) VALUES (?, ?, ?, ?, 'pending')",
                (video_id, url, channel, title),
            )
            return True
        if row["status"] == "done" and row["summary_version"] == SUMMARY_VERSION:
            return False
        conn.execute(
            "UPDATE videos SET status = 'pending', error = NULL, "
            "channel = COALESCE(channel, ?), title = COALESCE(title, ?) WHERE video_id = ?",
            (channel, title, video_id),
        )
        return True


def save_summary(
    video_id: str,
    *,
    channel: str | None,
    title: str | None,
    upload_date: str | None,
    language: str | None,
    summary: str,
    topics: list[str],
    stances: list[dict],
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE videos SET channel = COALESCE(?, channel), title = COALESCE(?, title), "
            "upload_date = COALESCE(?, upload_date), language = ?, status = 'done', summary = ?, "
            "topics = ?, stances = ?, summary_version = ?, input_tokens = ?, output_tokens = ?, summarised_at = ?, error = NULL, updated_at = CURRENT_TIMESTAMP "
            "WHERE video_id = ?",
            (
                channel, title, upload_date, language, summary,
                json.dumps(topics, ensure_ascii=False),
                json.dumps(stances, ensure_ascii=False),
                SUMMARY_VERSION, input_tokens, output_tokens, now(), video_id,
            ),
        )
        rowid = conn.execute("SELECT rowid FROM videos WHERE video_id = ?", (video_id,)).fetchone()[0]
        conn.execute("DELETE FROM videos_fts WHERE rowid = ?", (rowid,))
        stances_text = " ".join(f"{s.get('topic', '')} {s.get('position', '')}" for s in stances)
        conn.execute(
            "INSERT INTO videos_fts (rowid, title, summary, topics, stances) "
            "SELECT rowid, COALESCE(title, ''), ?, ?, ? FROM videos WHERE video_id = ?",
            (summary, " ".join(topics), stances_text, video_id),
        )


def mark_error(video_id: str, error: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE videos SET status = 'error', error = ?, updated_at = CURRENT_TIMESTAMP WHERE video_id = ?",
            (error[:500], video_id),
        )


_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _fts_query(text: str) -> str:
    """Turn free text into a forgiving FTS5 query: every word OR-ed, with prefix matching.

    Polish inflection ("podatek" / "podatkach" / "podatku") is handled crudely: words longer
    than 5 characters are cut to their first 5 characters and matched as a prefix.
    """
    terms = []
    for word in _WORD_RE.findall(text.lower()):
        stem = word[:5] if len(word) > 5 else word
        terms.append(f'"{stem}"*')
    return " OR ".join(terms)


def search(query: str, channel: str | None, limit: int, order_by_date: bool) -> list[sqlite3.Row]:
    match = _fts_query(query)
    if not match:
        return []
    sql = (
        "SELECT v.* FROM videos_fts f JOIN videos v ON v.rowid = f.rowid "
        "WHERE videos_fts MATCH ? AND v.status = 'done'"
    )
    params: list = [match]
    if channel:
        sql += " AND v.channel LIKE ?"
        params.append(f"%{channel}%")
    sql += " ORDER BY f.rank LIMIT ?"
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    if order_by_date:
        rows.sort(key=lambda r: r["upload_date"] or "")
    return rows


def recent(channel: str | None, limit: int | None) -> list[sqlite3.Row]:
    """Newest summarised videos (by upload date), optionally restricted to one channel."""
    sql = "SELECT * FROM videos WHERE status = 'done'"
    params: list = []
    if channel:
        sql += " AND channel LIKE ?"
        params.append(f"%{channel}%")
    sql += " ORDER BY upload_date DESC"
    if limit is not None:  # None = everything
        sql += " LIMIT ?"
        params.append(limit)
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def status_counts(channel: str | None = None) -> dict[str, int]:
    sql = "SELECT status, COUNT(*) AS n FROM videos"
    params: list = []
    if channel:
        sql += " WHERE channel LIKE ?"
        params.append(f"%{channel}%")
    sql += " GROUP BY status"
    with connect() as conn:
        counts = {r["status"]: r["n"] for r in conn.execute(sql, params)}
    return {"done": 0, "pending": 0, "error": 0, **counts}


def recent_errors(channel: str | None = None, limit: int = 5) -> list[dict]:
    sql = "SELECT video_id, title, error FROM videos WHERE status = 'error'"
    params: list = []
    if channel:
        sql += " AND channel LIKE ?"
        params.append(f"%{channel}%")
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def list_channels() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT channel, COUNT(*) AS videos, "
            "SUM(status = 'done') AS summarised, "
            "MIN(upload_date) AS oldest, MAX(upload_date) AS newest "
            "FROM videos GROUP BY channel ORDER BY videos DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def open_run(run_key: str) -> str:
    """Return the start time of the open run for this key, creating a new run if there is none."""
    with connect() as conn:
        row = conn.execute("SELECT started_at FROM sync_runs WHERE run_key = ?", (run_key,)).fetchone()
        if row:
            return row["started_at"]
        started = now()
        conn.execute("INSERT INTO sync_runs (run_key, started_at) VALUES (?, ?)", (run_key, started))
        return started


def close_run(run_key: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sync_runs WHERE run_key = ?", (run_key,))


def token_totals(channel: str | None = None) -> dict[str, int]:
    """Haiku tokens spent so far on the summaries that are stored (all runs, all time)."""
    sql = "SELECT COALESCE(SUM(input_tokens), 0) AS i, COALESCE(SUM(output_tokens), 0) AS o FROM videos"
    params: list = []
    if channel:
        sql += " WHERE channel LIKE ?"
        params.append(f"%{channel}%")
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return {"input": row["i"], "output": row["o"]}


def remember_channel_url(url_key: str, channel: str | None) -> None:
    if not channel:
        return
    with connect() as conn:
        conn.execute(
            "INSERT INTO channel_urls (url_key, channel) VALUES (?, ?) "
            "ON CONFLICT(url_key) DO UPDATE SET channel = excluded.channel",
            (url_key, channel),
        )


def channel_for_url(url_key: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT channel FROM channel_urls WHERE url_key = ?", (url_key,)).fetchone()
    return row["channel"] if row else None
