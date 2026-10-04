"""Adapter over the YouTube Data API v3. Nothing outside this module sees raw API shapes (design Principle 7)."""
import re
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

import httpx

from . import db, http
from .config import CACHE_HOURS, YOUTUBE_API_KEY

BASE = "https://www.googleapis.com/youtube/v3"
QUOTA_COST = {"channels": 1, "playlistItems": 1, "videos": 1, "search": 100}


class YouTubeError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _call(endpoint: str, **params) -> dict:
    if not YOUTUBE_API_KEY:
        raise YouTubeError("YOUTUBE_API_KEY is not set (see .env.example)")
    try:
        r = http.client.get(f"{BASE}/{endpoint}", params={**params, "key": YOUTUBE_API_KEY})
    except httpx.HTTPError as e:
        raise YouTubeError(f"Network error talking to YouTube: {type(e).__name__}") from e
    with db.connect() as con:
        con.execute(
            "INSERT INTO api_usage VALUES (?,?,1,?) ON CONFLICT(date, endpoint) DO UPDATE SET "
            "request_count=request_count+1, quota_units=quota_units+excluded.quota_units",
            (datetime.now(timezone.utc).date().isoformat(), endpoint, QUOTA_COST[endpoint]),
        )
    if r.status_code != 200:
        msg = r.json().get("error", {}).get("message", r.text)
        raise YouTubeError(f"YouTube API {r.status_code}: {msg}")
    return r.json()


def parse_duration(iso: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def parse_channel_url(url: str) -> tuple[str, str]:
    """Return (kind, value) with kind in handle|id|user|custom."""
    url = url.strip()
    if url.startswith("@"):
        return "handle", url
    if re.fullmatch(r"UC[\w-]{22}", url):
        return "id", url
    p = urlparse(url if "//" in url else "https://" + url)
    if "youtube.com" not in p.netloc:
        raise YouTubeError("Not a YouTube URL")
    parts = [unquote(x) for x in p.path.split("/") if x]
    if parts and parts[0].startswith("@"):
        return "handle", parts[0]
    if len(parts) >= 2 and parts[0] == "channel":
        return "id", parts[1]
    if len(parts) >= 2 and parts[0] == "user":
        return "user", parts[1]
    raise YouTubeError("Unsupported channel URL; use https://www.youtube.com/@handle or /channel/UC...")


def resolve_channel(url: str) -> dict:
    kind, value = parse_channel_url(url)
    key = {"handle": "forHandle", "id": "id", "user": "forUsername"}[kind]
    data = _call("channels", part="snippet,statistics,contentDetails", **{key: value})
    items = data.get("items") or []
    if not items:
        raise YouTubeError("Channel not found")
    c = items[0]
    return {
        "id": c["id"],
        "title": c["snippet"]["title"],
        "description": c["snippet"].get("description", ""),
        "custom_url": c["snippet"].get("customUrl"),
        "published_at": c["snippet"]["publishedAt"],
        "country": c["snippet"].get("country"),
        "uploads_playlist_id": c["contentDetails"]["relatedPlaylists"]["uploads"],
        "subscriber_count": None if c["statistics"].get("hiddenSubscriberCount") else int(c["statistics"].get("subscriberCount", 0)),
        "view_count": int(c["statistics"].get("viewCount", 0)),
        "video_count": int(c["statistics"].get("videoCount", 0)),
    }


def _video_ids(playlist_id: str) -> list[str]:
    ids, token = [], None
    while True:
        params = dict(part="contentDetails", playlistId=playlist_id, maxResults=50)
        if token:
            params["pageToken"] = token
        data = _call("playlistItems", **params)
        ids += [i["contentDetails"]["videoId"] for i in data.get("items", [])]
        token = data.get("nextPageToken")
        if not token:
            return ids


def _video_details(ids: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(ids), 50):
        data = _call("videos", part="snippet,contentDetails,statistics", id=",".join(ids[i:i + 50]))
        for v in data.get("items", []):
            s, st = v["snippet"], v.get("statistics", {})
            thumbs = s.get("thumbnails", {})
            thumb = (thumbs.get("maxres") or thumbs.get("high") or thumbs.get("default") or {}).get("url")
            out.append({
                "id": v["id"], "title": s["title"], "description": s.get("description", ""),
                "published_at": s["publishedAt"], "category_id": s.get("categoryId"),
                "tags": s.get("tags", []), "thumbnail_url": thumb,
                "duration_seconds": parse_duration(v["contentDetails"]["duration"]),
                # likes/comments can be hidden by the owner -> None, never 0
                "view_count": int(st["viewCount"]) if "viewCount" in st else None,
                "like_count": int(st["likeCount"]) if "likeCount" in st else None,
                "comment_count": int(st["commentCount"]) if "commentCount" in st else None,
            })
    return out


def sync_channel(url: str, force: bool = False) -> dict:
    """Collect a channel and store a new metrics snapshot. Skips the API when data is fresh."""
    ch = resolve_channel(url)
    ts = now_iso()
    with db.connect() as con:
        last = con.execute(
            "SELECT MAX(collected_at) t FROM channel_metrics WHERE youtube_channel_id=?", (ch["id"],)
        ).fetchone()["t"]
        if last and not force:
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
            if age_h < CACHE_HOURS:
                return {"channel_id": ch["id"], "synced": False, "reason": f"data is {age_h:.1f}h old (cache {CACHE_HOURS}h)"}
    ids = _video_ids(ch["uploads_playlist_id"])
    videos = _video_details(ids)
    with db.connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO channels VALUES (?,?,?,?,?,?,?,?)",
            (ch["id"], ch["title"], ch["description"], ch["custom_url"], ch["published_at"],
             ch["country"], ch["uploads_playlist_id"], ts),
        )
        con.execute("INSERT INTO channel_metrics (youtube_channel_id, collected_at, subscriber_count, view_count, video_count) VALUES (?,?,?,?,?)",
                    (ch["id"], ts, ch["subscriber_count"], ch["view_count"], ch["video_count"]))
        for v in videos:
            con.execute(
                "INSERT OR REPLACE INTO videos VALUES (?,?,?,?,?,?,?,?,?,?)",
                (v["id"], ch["id"], v["title"], v["description"], v["published_at"], v["duration_seconds"],
                 v["category_id"], "\x1f".join(v["tags"]), v["thumbnail_url"], ts),
            )
            con.execute("INSERT INTO video_metrics (youtube_video_id, collected_at, view_count, like_count, comment_count) VALUES (?,?,?,?,?)",
                        (v["id"], ts, v["view_count"], v["like_count"], v["comment_count"]))
    return {"channel_id": ch["id"], "synced": True, "videos": len(videos), "collected_at": ts}
