import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    youtube_channel_id TEXT PRIMARY KEY,
    title TEXT, description TEXT, custom_url TEXT, published_at TEXT,
    country TEXT, uploads_playlist_id TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS channel_metrics (
    youtube_channel_id TEXT, collected_at TEXT,
    subscriber_count INTEGER, view_count INTEGER, video_count INTEGER,
    source TEXT DEFAULT 'youtube_data_api_v3'
);
CREATE TABLE IF NOT EXISTS videos (
    youtube_video_id TEXT PRIMARY KEY,
    youtube_channel_id TEXT, title TEXT, description TEXT, published_at TEXT,
    duration_seconds INTEGER, category_id TEXT, tags TEXT, thumbnail_url TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS video_metrics (
    youtube_video_id TEXT, collected_at TEXT,
    view_count INTEGER, like_count INTEGER, comment_count INTEGER,
    source TEXT DEFAULT 'youtube_data_api_v3'
);
CREATE INDEX IF NOT EXISTS idx_vm ON video_metrics(youtube_video_id, collected_at);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    youtube_channel_id TEXT, created_at TEXT,
    snapshot_at TEXT,          -- which data snapshot the report was written from
    question TEXT, source TEXT, model TEXT, markdown TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports ON reports(youtube_channel_id, created_at);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    youtube_channel_id TEXT PRIMARY KEY,
    access_token_enc TEXT, refresh_token_enc TEXT, expires_at TEXT,
    scopes TEXT, created_at TEXT, status TEXT DEFAULT 'ok'   -- 'ok' | 'reauth_required'
);
-- Private Analytics API results. One row per (sync, kind); payload is JSON. Append-only like video_metrics.
-- collected_at equals the public-data snapshot taken in the same sync, so snapshot views line up.
CREATE TABLE IF NOT EXISTS analytics_data (
    youtube_channel_id TEXT, collected_at TEXT, kind TEXT, payload TEXT,
    source TEXT DEFAULT 'youtube_analytics_api_v2',
    PRIMARY KEY (youtube_channel_id, collected_at, kind)
);
-- Thumbnail impressions / CTR from the bulk YouTube Reporting API (reach reports). Owner-private: deleted on disconnect.
CREATE TABLE IF NOT EXISTS reach_jobs (   -- one job per (channel, report type): basic and combined
    youtube_channel_id TEXT, job_id TEXT, report_type_id TEXT,
    job_create_time TEXT, last_checked TEXT,
    PRIMARY KEY (youtube_channel_id, report_type_id)
);
CREATE TABLE IF NOT EXISTS reach_reports (
    report_id TEXT PRIMARY KEY, youtube_channel_id TEXT, start_time TEXT, end_time TEXT, create_time TEXT, row_count INTEGER
);
-- channel_reach_combined_a1: impressions/CTR split by traffic source, device and OS (codes as documented by YouTube)
CREATE TABLE IF NOT EXISTS reach_breakdown (
    youtube_channel_id TEXT, date TEXT, video_id TEXT,
    source_type INTEGER, source_detail TEXT, os INTEGER, device INTEGER,
    impressions INTEGER, ctr REAL,
    PRIMARY KEY (youtube_channel_id, date, video_id, source_type, source_detail, os, device)
);
CREATE TABLE IF NOT EXISTS reach_daily (
    youtube_channel_id TEXT, date TEXT, video_id TEXT,
    impressions INTEGER, ctr REAL,            -- ctr stored as a fraction (0.05 = 5%)
    PRIMARY KEY (youtube_channel_id, date, video_id)
);
CREATE TABLE IF NOT EXISTS api_usage (
    date TEXT, endpoint TEXT, request_count INTEGER, quota_units INTEGER,
    PRIMARY KEY (date, endpoint)
);
"""


def _columns(con, table: str) -> dict[str, int]:
    return {r["name"]: r["pk"] for r in con.execute(f"PRAGMA table_info({table})")}


def _migrate(con):
    """In-place upgrades for databases created by earlier versions (CREATE IF NOT EXISTS can't alter tables)."""
    jobs = _columns(con, "reach_jobs")
    if jobs and not jobs.get("report_type_id"):   # old schema: one reach job per channel, keyed by channel only
        con.execute("ALTER TABLE reach_jobs RENAME TO reach_jobs_old")
        con.execute("""CREATE TABLE reach_jobs (youtube_channel_id TEXT, job_id TEXT, report_type_id TEXT,
                       job_create_time TEXT, last_checked TEXT, PRIMARY KEY (youtube_channel_id, report_type_id))""")
        con.execute("INSERT INTO reach_jobs SELECT youtube_channel_id, job_id, report_type_id, job_create_time, last_checked FROM reach_jobs_old")
        con.execute("DROP TABLE reach_jobs_old")
    if "report_type_id" not in _columns(con, "reach_reports"):
        con.execute("ALTER TABLE reach_reports ADD COLUMN report_type_id TEXT")
        con.execute("UPDATE reach_reports SET report_type_id='channel_reach_basic_a1' WHERE report_type_id IS NULL")
    if "kind" not in _columns(con, "reports"):
        con.execute("ALTER TABLE reports ADD COLUMN kind TEXT DEFAULT 'report'")   # 'report' | 'question' | 'deep_dive'


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _migrate(con)
    try:
        yield con
        con.commit()
    finally:
        con.close()
