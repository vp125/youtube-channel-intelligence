"""Thumbnail impressions + CTR via the YouTube *Reporting* API (bulk daily CSV reports), owner-authorized.

Unlike reports.query this is asynchronous: create a job once, YouTube then generates one CSV per day (first files
~48h later, with ~30 days of history backfilled). Files expire after 60 days, so we copy rows into our own tables.

Two report types are collected:
  channel_reach_basic_*     per video per day                         -> reach_daily
  channel_reach_combined_*  + traffic source / source detail / OS / device -> reach_breakdown
The combined report is best-effort: the basic one never fails because of it.
"""
import csv
import io
from datetime import date, timedelta

import httpx

from . import auth, db, http, mathx, youtube

BASE = "https://youtubereporting.googleapis.com/v1"
PREFIX = {"basic": "channel_reach_basic", "combined": "channel_reach_combined"}
JOB_NAME = "channel-intelligence reach"
MIN_IMPRESSIONS = 100                      # CTR on fewer impressions is mostly noise; excluded from rankings

# Code tables exactly as documented by YouTube (Reporting API > dimensions).
TRAFFIC_SOURCES = {0: "Direct or unknown", 1: "YouTube advertising", 3: "Browse features", 4: "YouTube channels", 5: "YouTube search",
                   7: "Suggested videos", 8: "Other YouTube features", 9: "External", 11: "Video cards and annotations", 14: "Playlists",
                   17: "Notifications", 18: "Playlist pages", 19: "Claimed-content programming", 20: "End screens", 23: "Stories",
                   24: "Shorts", 25: "Product pages", 26: "Hashtag pages", 27: "Sound pages", 28: "Live redirects", 29: "Podcasts",
                   30: "Remixed video", 31: "Vertical live feed", 32: "Related video"}
DEVICES = {100: "Unknown", 101: "Computer", 102: "TV", 103: "Game console", 104: "Mobile phone", 105: "Tablet"}
OPERATING_SYSTEMS = {1: "Other", 2: "Windows", 3: "Windows Mobile", 4: "Android", 5: "iOS", 6: "Symbian", 7: "Blackberry", 9: "Macintosh",
                     10: "PlayStation", 13: "Linux", 17: "Xbox", 19: "Smart TV", 21: "Chromecast", 25: "KaiOS", 26: "Roku", 28: "Apple tvOS",
                     29: "Fire OS", 30: "ChromeOS", 31: "Vidaa"}
# What traffic_source_detail means per source (so the UI/AI can explain it)
DETAIL_MEANING = {5: "search term", 7: "referring video id", 3: "browse feature", 4: "referring channel id", 9: "external page",
                  8: "page type", 17: "notification type", 26: "hashtag", 27: "referring video id", 20: "referring video id"}


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


def _report_types(token: str) -> dict[str, str]:
    """Newest non-deprecated report type id for each kind we collect, e.g. {'basic': 'channel_reach_basic_a1', ...}."""
    items = _request("GET", f"{BASE}/reportTypes", token).json().get("reportTypes", [])
    out = {}
    for kind, prefix in PREFIX.items():
        ids = sorted(i["id"] for i in items if i["id"].startswith(prefix) and not i.get("deprecateTime"))
        if ids:
            out[kind] = ids[-1]
    return out


def _ensure_job(channel_id: str, token: str, rtype: str) -> tuple[dict, bool]:
    """Return (job row, created_now). One job per (channel, report type), created on first use."""
    with db.connect() as con:
        row = con.execute("SELECT * FROM reach_jobs WHERE youtube_channel_id=? AND report_type_id=?", (channel_id, rtype)).fetchone()
    if row:
        return dict(row), False
    jobs = _request("GET", f"{BASE}/jobs", token).json().get("jobs", [])
    job = next((j for j in jobs if j.get("reportTypeId") == rtype and j.get("name") == JOB_NAME), None)  # reuse after a local DB reset
    created = job is None
    if created:
        job = _request("POST", f"{BASE}/jobs", token, json={"reportTypeId": rtype, "name": JOB_NAME}).json()
    with db.connect() as con:
        con.execute("INSERT OR REPLACE INTO reach_jobs VALUES (?,?,?,?,?)",
                    (channel_id, job["id"], rtype, job.get("createTime") or youtube.now_iso(), None))
        row = con.execute("SELECT * FROM reach_jobs WHERE youtube_channel_id=? AND report_type_id=?", (channel_id, rtype)).fetchone()
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


def _ctr(raw) -> float:
    c = float(raw or 0)
    return c / 100 if c > 1 else c      # YouTube sends a fraction (verified on real files); >1 can only be a percent


def _date(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else d


def _parse_basic(text: str) -> list[tuple]:
    return [(_date(r["date"]), r["video_id"], int(float(r.get("video_thumbnail_impressions") or 0)), _ctr(r.get("video_thumbnail_impressions_ctr")))
            for r in csv.DictReader(io.StringIO(text))]


def _parse_combined(text: str) -> list[tuple]:
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        out.append((_date(r["date"]), r["video_id"], int(r.get("traffic_source_type") or 0), (r.get("traffic_source_detail") or "").strip(),
                    int(r.get("operating_system") or 0), int(r.get("device_type") or 0),
                    int(float(r.get("video_thumbnail_impressions") or 0)), _ctr(r.get("video_thumbnail_impressions_ctr"))))
    return out


def _ingest(channel_id: str, token: str, job: dict, kind: str) -> tuple[int, int]:
    with db.connect() as con:
        known = {r["report_id"] for r in con.execute(
            "SELECT report_id FROM reach_reports WHERE youtube_channel_id=? AND report_type_id=?", (channel_id, job["report_type_id"]))}
    new = [r for r in _list_reports(job["job_id"], token) if r["id"] not in known]
    new.sort(key=lambda r: r.get("createTime", ""))   # oldest first so revised reports (new id, same period) overwrite
    ingested = rows_total = 0
    for rep in new:
        text = _request("GET", rep["downloadUrl"], token).text
        with db.connect() as con:
            if kind == "basic":
                rows = _parse_basic(text)
                con.executemany("INSERT OR REPLACE INTO reach_daily VALUES (?,?,?,?,?)", [(channel_id, *r) for r in rows])
            else:
                rows = _parse_combined(text)
                con.executemany("INSERT OR REPLACE INTO reach_breakdown VALUES (?,?,?,?,?,?,?,?,?)", [(channel_id, *r) for r in rows])
            con.execute("INSERT OR REPLACE INTO reach_reports VALUES (?,?,?,?,?,?,?)",
                        (rep["id"], channel_id, rep.get("startTime"), rep.get("endTime"), rep.get("createTime"), len(rows), job["report_type_id"]))
        ingested += 1
        rows_total += len(rows)
    with db.connect() as con:
        con.execute("UPDATE reach_jobs SET last_checked=? WHERE youtube_channel_id=? AND report_type_id=?",
                    (youtube.now_iso(), channel_id, job["report_type_id"]))
    return ingested, rows_total


def sync(channel_id: str) -> dict:
    """Create the jobs if needed and download new reports. The basic report must work; the combined one is best-effort."""
    token = auth.access_token(channel_id)
    types = _report_types(token)
    if "basic" not in types:
        raise ReachError("YouTube offers no reach report for this channel.")
    job, created = _ensure_job(channel_id, token, types["basic"])
    n, rows = _ingest(channel_id, token, job, "basic")
    out = {"job_created": created, "reports_ingested": n, "rows": rows, "combined": None}
    if "combined" in types:
        try:
            cjob, ccreated = _ensure_job(channel_id, token, types["combined"])
            cn, crows = _ingest(channel_id, token, cjob, "combined")
            out["combined"] = {"job_created": ccreated, "reports_ingested": cn, "rows": crows}
            out["job_created"] = out["job_created"] or ccreated
        except ReachError as e:
            out["combined"] = {"error": str(e)}
    return out


def delete_data(con, channel_id: str):
    for t in ("reach_jobs", "reach_reports", "reach_daily", "reach_breakdown"):
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
        job = con.execute("SELECT * FROM reach_jobs WHERE youtube_channel_id=? AND report_type_id LIKE 'channel_reach_basic%'", (channel_id,)).fetchone()
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


def load_breakdown(channel_id: str, as_of: str | None = None) -> list[dict]:
    with db.connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT date, video_id, source_type, source_detail, os, device, impressions, ctr FROM reach_breakdown "
            "WHERE youtube_channel_id=? AND date<=? ORDER BY date", (channel_id, (as_of or "9999")[:10]))]


def build_breakdown(channel_id: str, as_of: str | None, titles: dict[str, str]) -> dict:
    """Impressions and CTR by traffic source, device, OS and source detail (search terms, referring videos...)."""
    with db.connect() as con:
        job = con.execute("SELECT * FROM reach_jobs WHERE youtube_channel_id=? AND report_type_id LIKE 'channel_reach_combined%'", (channel_id,)).fetchone()
    rows = load_breakdown(channel_id, as_of)
    if not job:
        return {"state": "not_started"}
    if not rows:
        return {"state": "waiting", "job_created": job["job_create_time"]}
    total = _agg(rows)

    def group(keyf, label):
        g: dict = {}
        for r in rows:
            g.setdefault(keyf(r), []).append(r)
        out = []
        for k, rs in g.items():
            t = _agg(rs)
            ci = mathx.wilson(t["clicks"], t["impressions"])
            out.append({"key": k, "label": label(k), **t, "share": round(t["impressions"] / total["impressions"], 4) if total["impressions"] else None,
                        "ctr_ci": None if not ci else [round(ci[0], 4), round(ci[1], 4)]})
        return sorted(out, key=lambda x: -x["impressions"])

    by_source = group(lambda r: r["source_type"], lambda k: TRAFFIC_SOURCES.get(k, f"Source {k}"))
    detail = {}
    for s in by_source[:6]:
        sub: dict = {}
        for r in rows:
            if r["source_type"] == s["key"] and r["source_detail"]:
                sub.setdefault(r["source_detail"], []).append(r)
        top = sorted(((d, _agg(rs)) for d, rs in sub.items()), key=lambda x: -x[1]["impressions"])[:8]
        if top:
            detail[s["label"]] = {"meaning": DETAIL_MEANING.get(s["key"], "detail"),
                                  "top": [{"detail": titles.get(d, d) if s["key"] in (7, 27, 20) else d, **t} for d, t in top]}
    dates = sorted({r["date"] for r in rows})
    return {"state": "ready", "first_date": dates[0], "last_date": dates[-1], "days": len(dates), "total": total,
            "by_source": by_source, "by_device": group(lambda r: r["device"], lambda k: DEVICES.get(k, f"Device {k}")),
            "by_os": group(lambda r: r["os"], lambda k: OPERATING_SYSTEMS.get(k, f"OS {k}"))[:8],
            "source_detail": detail,
            "by_video_source": _video_source(rows, titles)}


def _video_source(rows, titles) -> dict:
    """For each video, which sources gave its impressions (top 3), to see how each video is being found."""
    g: dict = {}
    for r in rows:
        g.setdefault(r["video_id"], {}).setdefault(r["source_type"], []).append(r)
    out = {}
    for vid, srcs in g.items():
        items = sorted(((TRAFFIC_SOURCES.get(k, f"Source {k}"), _agg(rs)) for k, rs in srcs.items()), key=lambda x: -x[1]["impressions"])
        tot = sum(t["impressions"] for _, t in items)
        out[vid] = {"title": titles.get(vid, vid), "impressions": tot,
                    "top_sources": [{"source": n, **t, "share": round(t["impressions"] / tot, 3) if tot else None} for n, t in items[:3]]}
    return out
