from pathlib import Path

from fastapi import FastAPI, HTTPException
from urllib.parse import quote

from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import analytics, auth, db, deepdive, reach, report, stats, youtube
from .config import ROOT

app = FastAPI(title="YouTube Channel Intelligence")
STATIC = Path(ROOT) / "static"


class AnalyzeReq(BaseModel):
    url: str
    force: bool = False


class AskReq(BaseModel):
    question: str | None = None
    as_of: str | None = None


def _stats(channel_id: str, as_of: str | None = None) -> dict:
    try:
        return stats.analyze(channel_id, as_of)
    except KeyError:
        raise HTTPException(404, "No stored data for this channel/snapshot")
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/channels/analyze")
def analyze(req: AnalyzeReq):
    try:
        sync = youtube.sync_channel(req.url, force=req.force)
    except youtube.YouTubeError as e:
        raise HTTPException(400, str(e))
    return {"channel_id": sync["channel_id"], "sync": sync, "stats": _stats(sync["channel_id"]),
            "llm_available": report.llm_available()}


@app.get("/api/channels")
def list_channels():
    """Previously analyzed channels, most recently synced first."""
    with db.connect() as con:
        rows = con.execute(
            """SELECT c.youtube_channel_id id, c.title, c.custom_url,
                      (SELECT COUNT(*) FROM channel_metrics WHERE youtube_channel_id=c.youtube_channel_id) snapshots,
                      (SELECT MAX(collected_at) FROM channel_metrics WHERE youtube_channel_id=c.youtube_channel_id) last_synced,
                      (SELECT COUNT(*) FROM videos WHERE youtube_channel_id=c.youtube_channel_id) videos
               FROM channels c ORDER BY last_synced DESC""").fetchall()
    return [dict(r) for r in rows]


@app.get("/auth/google/start")
def auth_start():
    try:
        return RedirectResponse(auth.start_url())
    except auth.AuthError as e:
        return RedirectResponse("/?auth_error=" + quote(str(e)))


@app.get("/auth/google/callback")
def auth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error or not code or not state:
        return RedirectResponse("/?auth_error=" + quote("Access was not granted." if error == "access_denied" else f"Google returned: {error or 'no code'}"))
    try:
        return RedirectResponse("/?connected=" + quote(auth.handle_callback(code, state)))
    except (auth.AuthError, youtube.YouTubeError) as e:
        return RedirectResponse("/?auth_error=" + quote(str(e)))


@app.get("/api/channels/{channel_id}/analytics/status")
def analytics_status(channel_id: str):
    return auth.status(channel_id)


@app.post("/api/channels/{channel_id}/analytics/sync")
def analytics_sync(channel_id: str):
    try:
        res = analytics.sync(channel_id)
    except auth.AuthError as e:
        raise HTTPException(401, str(e))
    except (analytics.AnalyticsError, youtube.YouTubeError) as e:
        raise HTTPException(502, str(e))
    return {**res, "stats": _stats(channel_id), "llm_available": report.llm_available()}


@app.post("/api/channels/{channel_id}/reach/sync")
def reach_sync(channel_id: str):
    """Start (first call) or refresh thumbnail impressions/CTR collection from the Reporting API."""
    try:
        res = reach.sync(channel_id)
    except auth.AuthError as e:
        raise HTTPException(401, str(e))
    except reach.ReachError as e:
        raise HTTPException(502, str(e))
    return {**res, "stats": _stats(channel_id), "llm_available": report.llm_available()}


@app.post("/api/channels/{channel_id}/analytics/disconnect")
def analytics_disconnect(channel_id: str):
    auth.disconnect(channel_id)
    return {"ok": True}


@app.get("/api/channels/{channel_id}")
def get_stats(channel_id: str, as_of: str | None = None):
    """Analysis of a stored snapshot (default latest), recomputed from stored data, no YouTube calls."""
    return {"channel_id": channel_id, "stats": _stats(channel_id, as_of), "llm_available": report.llm_available()}


@app.get("/api/channels/{channel_id}/snapshots")
def snapshots(channel_id: str):
    return stats.list_snapshots(channel_id)


@app.get("/api/channels/{channel_id}/reports")
def list_reports(channel_id: str):
    with db.connect() as con:
        rows = con.execute(
            "SELECT id, created_at, snapshot_at, question, source, model, kind FROM reports WHERE youtube_channel_id=? ORDER BY created_at DESC LIMIT 50",
            (channel_id,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/reports/{report_id}")
def get_report(report_id: int):
    with db.connect() as con:
        r = con.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Report not found")
    return dict(r)


@app.post("/api/channels/{channel_id}/report")
def make_report(channel_id: str, req: AskReq):
    s = _stats(channel_id, req.as_of)
    out = {"markdown": report.fallback_report(s), "source": "template"}
    if report.llm_available():
        try:
            md, model = report.ask(s, req.question)
            out = {"markdown": md, "source": "llm", "model": model}
        except Exception as e:  # surface the LLM failure but still give the deterministic report
            out["error"] = str(e)
    if "error" not in out:  # don't archive fallbacks caused by an LLM failure
        with db.connect() as con:
            cur = con.execute(
                "INSERT INTO reports (youtube_channel_id, created_at, snapshot_at, question, source, model, markdown, kind) VALUES (?,?,?,?,?,?,?,?)",
                (channel_id, youtube.now_iso(), s["channel"]["last_collected_at"], req.question,
                 out["source"], out.get("model"), out["markdown"], "question" if req.question else "report"))
            out["id"] = cur.lastrowid
    return out


@app.get("/api/channels/{channel_id}/deepdive")
def deep_dive_data(channel_id: str, as_of: str | None = None):
    """Deterministic diagnosis (free, instant, reproducible from stored data)."""
    s = _stats(channel_id, as_of)
    return {"deepdive": deepdive.build(s, channel_id, as_of), "llm_available": report.llm_available()}


@app.post("/api/channels/{channel_id}/deepdive/report")
def deep_dive_report(channel_id: str, req: AskReq):
    """AI-written deep dive over the deterministic diagnosis plus the actual thumbnails. Saved like any report."""
    if not report.llm_available():
        raise HTTPException(400, "Set ANTHROPIC_API_KEY to generate the AI deep dive. The data diagnosis above works without it.")
    s = _stats(channel_id, req.as_of)
    dd = deepdive.build(s, channel_id, req.as_of)
    try:
        md, model, meta = report.deep_dive(s, dd)
    except Exception as e:
        raise HTTPException(502, f"AI deep dive failed: {e}")
    with db.connect() as con:
        cur = con.execute(
            "INSERT INTO reports (youtube_channel_id, created_at, snapshot_at, question, source, model, markdown, kind) VALUES (?,?,?,?,?,?,?,?)",
            (channel_id, youtube.now_iso(), s["channel"]["last_collected_at"], "Deep dive", "llm", model, md, "deep_dive"))
    return {"id": cur.lastrowid, "markdown": md, "model": model, "meta": meta}


@app.get("/api/usage")
def usage():
    with db.connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM api_usage ORDER BY date DESC, endpoint LIMIT 60")]


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
