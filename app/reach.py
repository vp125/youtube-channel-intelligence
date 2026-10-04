"""Thumbnail impressions + CTR via the YouTube *Reporting* API (bulk daily CSV reports), owner-authorized.

Unlike reports.query this is asynchronous: create a job once, YouTube then generates one CSV per day (first files
~48h later, with ~30 days of history backfilled). Files expire after 60 days, so we copy rows into our own table.
"""
import csv
import io
from datetime import date, datetime, timedelta, timezone

import httpx

from . import auth, db, http, youtube

BASE = "https://youtubereporting.googleapis.com/v1"
REPORT_PREFIX = "channel_reach_basic"      # per channel / video / day
JOB_NAME = "channel-intelligence reach"
MIN_IMPRESSIONS = 100                      # CTR on fewer impressions is mostly noise; excluded from rankings


class ReachError(Exception):
    pass


def _request(method: str, url: str, token: str, **kw):
    try:
        r = http.client.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
    except httpx.HTTPError as e:
        raise ReachError(f"network error ({type(e).__name__})") from e
    with db.connect() as con:
        con.execute("INSERT INTO api_usage VALUES (?,?,1,0) ON CONFLICT(date, endpoint) DO UPDATE SET request_count=request_count+1",
                    (date.today().isoformat(), "reporting"))
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", r.text)
        except ValueError:
            msg = r.text
        if r.status_code == 403 and ("has not been used" in msg or "disabled" in msg or "accessNotConfigured" in r.text):
            raise ReachError("The YouTube Reporting API is not enabled in your Google Cloud project. Enable "
                             "'YouTube Reporting API' (APIs & Services > Library), wait a minute, then try again.")
        raise ReachError(f"{r.status_code}: {msg[:300]}")
    return r


def has_job(channel_id: str) -> bool:
    with db.connect() as con:
        return con.execute("SELECT 1 FROM reach_jobs WHERE youtube_channel_id=?", (channel_id,)).fetchone() is not None


def _report_type(token: str) -> str:
    items = _request("GET", f"{BASE}/reportTypes", token).json().get("reportTypes", [])
    ids = sorted(i["id"] for i in items if i["id"].startswith(REPORT_PREFIX) and not i.get("deprecateTime"))
    if not ids:
        raise ReachError("YouTube offers no reach report for this channel.")
    return ids[-1]  # newest version


def _ensure_job(channel_id: str, token: str) -> tuple[dict, bool]:
    """Return (job row, created_now). One job per channel; created on first use."""
    with db.connect() as con:
        row = con.execute("SELECT * FROM reach_jobs WHERE youtube_channel_id=?", (channel_id,)).fetchone()
    if row:
        return dict(row), False
    rtype = _report_type(token)
    jobs = _request("GET", f"{BASE}/jobs", token).json().get("jobs", [])
    job = next((j for j in jobs if j.get("reportTypeId") == rtype and j.get("name") == JOB_NAME), None)  # reuse after a local DB reset
    created = job is None
    if created:
        job = _request("POST", f"{BASE}/jobs", token, json={"reportTypeId": rtype, "name": JOB_NAME}).json()
    with db.connect() as con:
        con.execute("INSERT OR REPLACE INTO reach_jobs VALUES (?,?,?,?,?)",
                    (channel_id, job["id"], rtype, job.get("createTime") or youtube.now_iso(), None))
        row = con.execute("SELECT * FROM reach_jobs WHERE youtube_channel_id=?", (channel_id,)).fetchone()
    return dict(row), created


def _list_reports(job_id: str, token: str) -> list[dict]:
    out, page = [], None
    while True:
        params = {"pageSize": 100, **({"pageToken": page} if page else {})}
        body = _request("GET", f"{BASE}/jobs/{job_id}/reports", token, params=params).json()
        out += body.get("reports", [])
        page = body.get("nextPageToken")
        if not page:
            return out


def _parse(csv_text: str) -> list[tuple[str, str, int, float]]:
    rows = []
    for r in csv.DictReader(io.StringIO(csv_text)):
        d = r["date"]
        d = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else d
        impr = int(float(r.get("video_thumbnail_impressions") or 0))
        ctr = float(r.get("video_thumbnail_impressions_ctr") or 0)
        rows.append((d, r["video_id"], impr, ctr))
    # The docs don't pin the unit down. A fraction never exceeds 1; a percent value can. If any exceeds 1, treat as percent.
    if any(c > 1 for _, _, _, c in rows):
        rows = [(d, v, i, c / 100) for d, v, i, c in rows]
    return rows


def sync(channel_id: str) -> dict:
    token = auth.access_token(channel_id)
    job, created = _ensure_job(channel_id, token)
    with db.connect() as con:
        known = {r["report_id"] for r in con.execute("SELECT report_id FROM reach_reports WHERE youtube_channel_id=?", (channel_id,))}
    new = [r for r in _list_reports(job["job_id"], token) if r["id"] not in known]
    new.sort(key=lambda r: r.get("createTime", ""))   # oldest first so revised reports (new id, same period) overwrite
    ingested = rows_total = 0
    for rep in new:
        csv_text = _request("GET", rep["downloadUrl"], token).text
        rows = _parse(csv_text)
        with db.connect() as con:
            con.executemany("INSERT OR REPLACE INTO reach_daily VALUES (?,?,?,?,?)", [(channel_id, d, v, i, c) for d, v, i, c in rows])
            con.execute("INSERT OR REPLACE INTO reach_reports VALUES (?,?,?,?,?,?)",
                        (rep["id"], channel_id, rep.get("startTime"), rep.get("endTime"), rep.get("createTime"), len(rows)))
        ingested += 1
        rows_total += len(rows)
    with db.connect() as con:
        con.execute("UPDATE reach_jobs SET last_checked=? WHERE youtube_channel_id=?", (youtube.now_iso(), channel_id))
    return {"job_created": created, "reports_ingested": ingested, "rows": rows_total}


def delete_data(con, channel_id: str):
    for t in ("reach_jobs", "reach_reports", "reach_daily"):
        con.execute(f"DELETE FROM {t} WHERE youtube_channel_id=?", (channel_id,))


# ---- analysis (deterministic) -------------------------------------------------
def _agg(rows) -> dict:
    impr = sum(r["impressions"] for r in rows)
    clicks = sum(r["impressions"] * r["ctr"] for r in rows)   # CTR must be impressions-weighted, never an average of daily CTRs
    return {"impressions": int(impr), "clicks": int(round(clicks)), "ctr": round(clicks / impr, 4) if impr else None}


def _change(a, b):
    return round((a / b - 1) * 100, 1) if b else None


def build(channel_id: str, as_of: str | None, titles: dict[str, str]) -> dict:
    with db.connect() as con:
        job = con.execute("SELECT * FROM reach_jobs WHERE youtube_channel_id=?", (channel_id,)).fetchone()
        rows = [dict(r) for r in con.execute(
            "SELECT date, video_id, impressions, ctr FROM reach_daily WHERE youtube_channel_id=? AND date<=? ORDER BY date",
            (channel_id, (as_of or "9999")[:10]))]
    if not job:
        return {"state": "not_started"}
    out = {"job_created": job["job_create_time"], "last_checked": job["last_checked"]}
    if not rows:
        return {**out, "state": "waiting"}
    by_day: dict[str, list] = {}
    by_vid: dict[str, list] = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(r)
        by_vid.setdefault(r["video_id"], []).append(r)
    last = date.fromisoformat(max(by_day))
    cut = lambda n: (last - timedelta(days=n)).isoformat()
    last28 = [r for r in rows if r["date"] > cut(28)]
    prev28 = [r for r in rows if cut(56) < r["date"] <= cut(28)]
    a, b = _agg(last28), _agg(prev28)
    out.update(state="ready", first_date=min(by_day), last_date=max(by_day), days=len(by_day), overall=_agg(rows),
               periods={"last_28d": a, "previous_28d": b if prev28 else None,
                        "change_pct": {"impressions": _change(a["impressions"], b["impressions"]) if prev28 else None,
                                       "ctr_points": round((a["ctr"] - b["ctr"]) * 100, 2) if prev28 and a["ctr"] is not None and b["ctr"] is not None else None}},
               daily=[{"day": d, **_agg(rs)} for d, rs in sorted(by_day.items())])
    per_video = {}
    for vid, rs in by_vid.items():
        t = _agg(rs)
        per_video[vid] = {"impressions": t["impressions"], "ctr": t["ctr"], "clicks": t["clicks"],
                          "ctr_pct": None if t["ctr"] is None else round(t["ctr"] * 100, 2)}
    out["per_video"] = per_video
    eligible = [(v, p) for v, p in per_video.items() if p["impressions"] >= MIN_IMPRESSIONS and p["ctr"] is not None]
    row = lambda v, p: {"id": v, "title": titles.get(v, v), **p}
    out["ctr_rank"] = {"min_impressions": MIN_IMPRESSIONS,
                       "best": [row(v, p) for v, p in sorted(eligible, key=lambda x: -x[1]["ctr"])[:5]],
                       "worst": [row(v, p) for v, p in sorted(eligible, key=lambda x: x[1]["ctr"])[:5]],
                       "videos_with_enough_impressions": len(eligible)}
    out["videos_table"] = sorted((row(v, p) for v, p in per_video.items()), key=lambda x: -x["impressions"])
    return out
