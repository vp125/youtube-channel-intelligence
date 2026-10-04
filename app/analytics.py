"""YouTube Analytics API collector (owner-authorized data). Adapter: nothing else sees raw API shapes."""
import json
from datetime import datetime, timedelta, timezone

import httpx

from . import auth, db, http, reach, youtube

REPORTS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
CORE = "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained,subscribersLost,likes,comments,shares"
DAILY = "views,estimatedMinutesWatched,averageViewDuration,subscribersGained,subscribersLost,likes,comments,shares"
RETENTION_VIDEOS = 8   # retention needs one API call per video, so only the top videos get a curve
WINDOW_DAYS = 365


class AnalyticsError(Exception):
    pass


def _report(token: str, **params) -> list[dict]:
    try:
        r = http.client.get(REPORTS_URL, params={"ids": "channel==MINE", **params}, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as e:
        raise AnalyticsError(f"network error ({type(e).__name__})") from e
    with db.connect() as con:  # Analytics API has its own quota; track calls anyway (design §11.2)
        from datetime import date
        con.execute("INSERT INTO api_usage VALUES (?,?,1,0) ON CONFLICT(date, endpoint) DO UPDATE SET request_count=request_count+1",
                    (date.today().isoformat(), "analytics.reports"))
    if r.status_code != 200:
        msg = r.json().get("error", {}).get("message", r.text) if r.headers.get("content-type", "").startswith("application/json") else r.text
        raise AnalyticsError(f"{r.status_code}: {msg}")
    body = r.json()
    names = [h["name"] for h in body.get("columnHeaders", [])]
    return [dict(zip(names, row)) for row in body.get("rows", [])]


def _put(con, channel_id: str, ts: str, kind: str, payload):
    con.execute("INSERT OR REPLACE INTO analytics_data (youtube_channel_id, collected_at, kind, payload) VALUES (?,?,?,?)",
                (channel_id, ts, kind, json.dumps(payload)))


def sync(channel_id: str) -> dict:
    """Refresh public data and pull Analytics under ONE timestamp, so snapshots line up. Partial failures are reported, not fatal."""
    token = auth.access_token(channel_id)
    pub = youtube.sync_channel(channel_id, force=True)
    ts = pub["collected_at"]
    today = datetime.now(timezone.utc).date()
    end = today.isoformat()
    win_start = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    with db.connect() as con:
        first = con.execute("SELECT MIN(published_at) p FROM videos WHERE youtube_channel_id=?", (channel_id,)).fetchone()["p"]
    life_start = (first or win_start)[:10]

    jobs = {
        "daily": dict(dimensions="day", metrics=DAILY, startDate=win_start, endDate=end, sort="day"),
        "videos": dict(dimensions="video", metrics=CORE, startDate=life_start, endDate=end, sort="-views", maxResults=200),
        "traffic": dict(dimensions="insightTrafficSourceType", metrics="views,estimatedMinutesWatched", startDate=win_start, endDate=end, sort="-views"),
        "age_gender": dict(dimensions="ageGroup,gender", metrics="viewerPercentage", startDate=win_start, endDate=end),
        "country": dict(dimensions="country", metrics="views,estimatedMinutesWatched", startDate=win_start, endDate=end, sort="-views", maxResults=10),
    }
    result: dict[str, int] = {}
    errors: dict[str, str] = {}
    data: dict[str, list] = {}
    for kind, p in jobs.items():
        try:
            data[kind] = _report(token, **p)
            result[kind] = len(data[kind])
        except AnalyticsError as e:
            errors[kind] = str(e)

    retention: dict[str, list] = {}
    for row in data.get("videos", [])[:RETENTION_VIDEOS]:
        vid = row["video"]
        try:
            rows = _report(token, dimensions="elapsedVideoTimeRatio", metrics="audienceWatchRatio,relativeRetentionPerformance",
                           filters=f"video=={vid}", startDate=life_start, endDate=end)
            retention[vid] = [[r["elapsedVideoTimeRatio"], r["audienceWatchRatio"], r["relativeRetentionPerformance"]] for r in rows]
        except AnalyticsError as e:
            errors[f"retention:{vid}"] = str(e)
    result["retention_videos"] = len(retention)
    if reach.has_job(channel_id):  # only refresh once the user has opted in (starting creates a job on their channel)
        try:  # impressions/CTR live in the separate Reporting API; its failure (e.g. API not enabled) must not sink the rest
            result["reach_reports"] = reach.sync(channel_id)["reports_ingested"]
        except reach.ReachError as e:
            errors["reach"] = str(e)

    core_errors = {k: v for k, v in errors.items() if k != "reach"}
    if not data and core_errors:  # nothing worked: most likely scopes/permissions; don't store an empty snapshot
        first_err = next(iter(core_errors.values()))
        raise AnalyticsError(f"YouTube Analytics API request failed: {first_err}")
    with db.connect() as con:
        for kind, rows in data.items():
            _put(con, channel_id, ts, kind, rows)
        _put(con, channel_id, ts, "retention", retention)
        _put(con, channel_id, ts, "meta", {"window": [win_start, end], "lifetime_start": life_start,
                                          "api": "youtubeAnalytics v2", "errors": errors})
    return {"collected_at": ts, "counts": result, "errors": errors}


def load(channel_id: str, as_of: str | None = None) -> dict | None:
    """Latest analytics snapshot at or before as_of, as {kind: payload, '_collected_at': ts}; None if never synced."""
    with db.connect() as con:
        r = con.execute("SELECT MAX(collected_at) t FROM analytics_data WHERE youtube_channel_id=? AND collected_at<=?",
                        (channel_id, as_of or "9999")).fetchone()
        if not r["t"]:
            return None
        rows = con.execute("SELECT kind, payload FROM analytics_data WHERE youtube_channel_id=? AND collected_at=?", (channel_id, r["t"])).fetchall()
    out = {x["kind"]: json.loads(x["payload"]) for x in rows}
    out["_collected_at"] = r["t"]
    return out
