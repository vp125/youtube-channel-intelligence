"""Deterministic summaries of owner-authorized Analytics data (watch time, retention, traffic, demographics)."""
from datetime import date, timedelta
from statistics import median

from . import analytics

MIN_VIDEO_VIEWS = 10   # ignore videos with fewer views when ranking retention/conversion; averages on tiny samples are noise

TRAFFIC_LABELS = {
    "YT_SEARCH": "YouTube search", "SUGGESTED_VIDEO": "Suggested videos", "RELATED_VIDEO": "Suggested videos",
    "BROWSE": "Browse (Home, Subscriptions)", "SUBSCRIBER": "Subscriptions feed", "YT_CHANNEL": "Channel pages",
    "EXT_URL": "External sites", "NO_LINK_EMBEDDED": "Direct / embedded", "NO_LINK_OTHER": "Direct / unknown",
    "PLAYLIST": "Playlists", "NOTIFICATION": "Notifications", "SHORTS": "Shorts feed", "END_SCREEN": "End screens",
    "ADVERTISING": "Ads", "YT_OTHER_PAGE": "Other YouTube pages", "HASHTAGS": "Hashtag pages", "CAMPAIGN_CARD": "Cards",
    "LIVE_REDIRECT": "Live redirects", "PRODUCT_PAGE": "Product pages", "SOUND_PAGE": "Sound pages", "VIDEO_REMIXES": "Remixes",
}


def _sum(rows, key):
    return sum(r.get(key) or 0 for r in rows)


def _period(rows):
    views, mins = _sum(rows, "views"), _sum(rows, "estimatedMinutesWatched")
    return {"views": int(views), "watch_hours": round(mins / 60, 1),
            "avg_view_duration_s": round(mins * 60 / views) if views else None,
            "subs_net": int(_sum(rows, "subscribersGained") - _sum(rows, "subscribersLost")),
            "likes": int(_sum(rows, "likes")), "comments": int(_sum(rows, "comments")), "shares": int(_sum(rows, "shares"))}


def _pct_change(new, old):
    return round((new / old - 1) * 100, 1) if old else None


def _downsample(rows, n=50):
    step = max(1, len(rows) // n)
    pts = rows[::step]
    return pts if pts[-1] is rows[-1] else pts + [rows[-1]]


def build(channel_id: str, as_of: str | None, titles: dict[str, str]) -> dict:
    data = analytics.load(channel_id, as_of)
    if not data:
        return {"has_data": False}
    out: dict = {"has_data": True, "collected_at": data["_collected_at"], "meta": data.get("meta", {})}

    daily = sorted(data.get("daily", []), key=lambda r: r["day"])
    if daily:
        last = date.fromisoformat(daily[-1]["day"])
        cut = lambda days: (last - timedelta(days=days)).isoformat()
        last28 = [r for r in daily if r["day"] > cut(28)]
        prev28 = [r for r in daily if cut(56) < r["day"] <= cut(28)]
        a, b = _period(last28), _period(prev28)
        out["periods"] = {"last_28d": a, "previous_28d": b, "through": daily[-1]["day"],
                          "change_pct": {k: _pct_change(a[k], b[k]) for k in ("views", "watch_hours")},
                          "last_365d": _period(daily)}
        out["daily"] = [{"day": r["day"], "views": int(r["views"] or 0),
                         "watch_hours": round((r["estimatedMinutesWatched"] or 0) / 60, 2),
                         "subs_net": int((r["subscribersGained"] or 0) - (r["subscribersLost"] or 0))} for r in daily]

    traffic = data.get("traffic", [])
    tv = _sum(traffic, "views") or 1
    tm = _sum(traffic, "estimatedMinutesWatched") or 1
    out["traffic"] = [{"source": TRAFFIC_LABELS.get(r["insightTrafficSourceType"], r["insightTrafficSourceType"].replace("_", " ").title()),
                       "views": int(r["views"]), "views_share": round(r["views"] / tv, 3),
                       "watch_share": round(r["estimatedMinutesWatched"] / tm, 3)} for r in traffic]

    ag: dict[str, dict] = {}
    for r in data.get("age_gender", []):
        g = ag.setdefault(r["ageGroup"].replace("age", ""), {})
        g[r["gender"]] = round(r["viewerPercentage"], 1)
    out["age_gender"] = [{"age": k, **v} for k, v in sorted(ag.items())]
    # share of ALL views in the window, not of the (top-10) rows returned, so shares are honest and need not sum to 100%
    cv = (out.get("periods") or {}).get("last_365d", {}).get("views") or _sum(data.get("country", []), "views") or 1
    out["countries"] = [{"country": r["country"], "views": int(r["views"]), "share": round(r["views"] / cv, 3)} for r in data.get("country", [])]

    per_video: dict[str, dict] = {}
    for r in data.get("videos", []):
        v = r["views"] or 0
        per_video[r["video"]] = {
            "a_views": int(v), "avg_view_pct": round(r["averageViewPercentage"], 1) if r.get("averageViewPercentage") is not None else None,
            "avg_view_duration_s": int(r["averageViewDuration"] or 0), "watch_hours": round((r["estimatedMinutesWatched"] or 0) / 60, 2),
            "subs_gained": int(r["subscribersGained"] or 0), "subs_lost": int(r["subscribersLost"] or 0), "shares": int(r["shares"] or 0),
            "subs_per_1k_views": round((r["subscribersGained"] or 0) / v * 1000, 1) if v else None}
    out["per_video"] = per_video

    eligible = [(k, p) for k, p in per_video.items() if p["a_views"] >= MIN_VIDEO_VIEWS and p["avg_view_pct"] is not None]

    def row(k, p):
        return {"id": k, "title": titles.get(k, k), **p}
    out["retention_rank"] = {
        "channel_median_avg_view_pct": round(median(p["avg_view_pct"] for _, p in eligible), 1) if eligible else None,
        "best": [row(k, p) for k, p in sorted(eligible, key=lambda x: -x[1]["avg_view_pct"])[:5]],
        "worst": [row(k, p) for k, p in sorted(eligible, key=lambda x: x[1]["avg_view_pct"])[:5]],
        "min_views": MIN_VIDEO_VIEWS}
    conv = [(k, p) for k, p in per_video.items() if p["a_views"] >= MIN_VIDEO_VIEWS and p["subs_per_1k_views"] is not None]
    out["subscriber_conversion"] = [row(k, p) for k, p in sorted(conv, key=lambda x: -x[1]["subs_per_1k_views"])[:5]]

    curves = {}
    for vid, pts in (data.get("retention") or {}).items():
        if not pts:
            continue
        pts = sorted(pts)
        at = lambda ratio: next((w for t, w, _ in pts if t >= ratio - 1e-9), pts[-1][1])
        curves[vid] = {
            "title": titles.get(vid, vid),
            "key_points": {f"{int(t * 100)}%": round(at(t), 3) for t in (0.05, 0.1, 0.25, 0.5, 0.75, 1.0)},
            "avg_relative_retention": round(sum(p[2] for p in pts) / len(pts), 2),   # 0-1, ~0.5 = typical for similar-length videos
            "curve": [[round(t, 3), round(w, 3), round(rr, 3)] for t, w, rr in _downsample(pts)]}
    out["retention"] = curves
    return out
