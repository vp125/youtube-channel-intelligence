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
CREATE TABLE IF NOT EXISTS reach_jobs (
    youtube_channel_id TEXT PRIMARY KEY, job_id TEXT, report_type_id TEXT,
    job_create_time TEXT, last_checked TEXT
);
CREATE TABLE IF NOT EXISTS reach_reports (
    report_id TEXT PRIMARY KEY, youtube_channel_id TEXT, start_time TEXT, end_time TEXT, create_time TEXT, row_count INTEGER
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


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    try:
        yield con
        con.commit()
    finally:
        con.close()
