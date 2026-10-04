"""Deterministic analytics. All numbers in reports come from here, never from the LLM."""
import re
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import audience, db, reach

MATURE_DAYS = 30        # videos younger than this are excluded from pattern comparisons
MIN_GROUP = 5           # minimum videos in a group before we report a pattern
MIN_MATURE_TOTAL = 20   # below this, pattern findings are flagged as low-power
STOPWORDS = set("the a an and or of to in on for with is are was be at by from this that it as my i you we your how what why when "
                "de la le les du des et un une en que qui pour dans sur est ne pas".split())


def list_snapshots(channel_id: str) -> list[dict]:
    with db.connect() as con:
        rows = con.execute(
            "SELECT collected_at, subscriber_count, view_count, video_count FROM channel_metrics "
            "WHERE youtube_channel_id=? ORDER BY collected_at DESC", (channel_id,)).fetchall()
    return [dict(r) for r in rows]


def load_frame(channel_id: str, as_of: str | None = None) -> tuple[dict, pd.DataFrame]:
    """Load the channel as it was at snapshot `as_of` (default: latest). Metrics are append-only, so this is exact."""
    as_of = as_of or "9999"
    with db.connect() as con:
        ch = con.execute("SELECT * FROM channels WHERE youtube_channel_id=?", (channel_id,)).fetchone()
        cm = con.execute("SELECT * FROM channel_metrics WHERE youtube_channel_id=? AND collected_at<=? ORDER BY collected_at DESC LIMIT 1",
                         (channel_id, as_of)).fetchone()
        if not ch or not cm:
            raise KeyError(channel_id)
        rows = con.execute(
            """SELECT v.*, m.view_count, m.like_count, m.comment_count, m.collected_at
               FROM videos v JOIN video_metrics m ON m.youtube_video_id=v.youtube_video_id
               WHERE v.youtube_channel_id=? AND m.collected_at=
                 (SELECT MAX(collected_at) FROM video_metrics WHERE youtube_video_id=v.youtube_video_id AND collected_at<=?)""",
            (channel_id, as_of),
        ).fetchall()
        n_snap = con.execute(
            "SELECT COUNT(DISTINCT collected_at) n FROM video_metrics m JOIN videos v USING(youtube_video_id) "
            "WHERE v.youtube_channel_id=? AND m.collected_at<=?", (channel_id, as_of)).fetchone()["n"]
    df = pd.DataFrame([dict(r) for r in rows])
    channel = {**dict(ch), "subscriber_count": cm["subscriber_count"], "view_count": cm["view_count"],
               "video_count": cm["video_count"], "snapshots": n_snap, "last_collected_at": cm["collected_at"]}
    return channel, df


def _enrich(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    df = df.copy()
    df["published"] = pd.to_datetime(df["published_at"], utc=True)
    df["age_days"] = ((now - df["published"]).dt.total_seconds() / 86400).clip(lower=0.1)
    df["views_per_day"] = df["view_count"] / df["age_days"]
    df["engagement_rate"] = (df["like_count"].fillna(0) + df["comment_count"].fillna(0)) / df["view_count"].replace(0, np.nan)
    df["is_short_form"] = df["duration_seconds"] <= 180
    df["mature"] = df["age_days"] >= MATURE_DAYS
    return df


def _bucket_duration(s: int) -> str:
    if s <= 60: return "≤1 min"
    if s <= 180: return "1–3 min"
    if s <= 600: return "3–10 min"
    if s <= 1200: return "10–20 min"
    return ">20 min"


def title_features(title: str) -> dict:
    return {
        "title has a number": bool(re.search(r"\d", title)),
        "title is a question": "?" in title,
        "title has ALL-CAPS word": bool(re.search(r"\b[A-Z]{3,}\b", title)),
        "title has emoji": bool(re.search("[\U0001F300-\U0001FAFF☀-➿]", title)),
        "title has brackets": bool(re.search(r"[\[\(].+[\]\)]", title)),
        "title has hashtag": "#" in title,
        "title has separator (| - :)": bool(re.search(r"\s[|\-–—]\s|:", title)),
        "long title (>60 chars)": len(title) > 60,
        "short title (≤30 chars)": len(title) <= 30,
    }


def _bootstrap_ratio(group: np.ndarray, base_median: float, n_boot: int = 1000) -> tuple[float, float, float]:
    rng = np.random.default_rng(42)  # fixed seed -> reproducible reports
    meds = np.median(rng.choice(group, size=(n_boot, len(group)), replace=True), axis=1) / base_median
    return float(np.median(group) / base_median), float(np.percentile(meds, 2.5)), float(np.percentile(meds, 97.5))


def _compare_groups(mature: pd.DataFrame, label_col_values: dict[str, pd.Series]) -> list[dict]:
    base = mature["view_count"].median()
    out = []
    for label, mask in label_col_values.items():
        g = mature.loc[mask, "view_count"].to_numpy(dtype=float)
        rest = mature.loc[~mask, "view_count"]
        if len(g) < MIN_GROUP or len(rest) < MIN_GROUP or base <= 0:
            continue
        ratio, lo, hi = _bootstrap_ratio(g, base)
        out.append({
            "pattern": label, "videos": int(len(g)), "median_views": int(np.median(g)),
            "ratio_vs_channel_median": round(ratio, 2), "ci95": [round(lo, 2), round(hi, 2)],
            "evidence": "supported" if (lo > 1 or hi < 1) else "inconclusive",
        })
    return sorted(out, key=lambda x: -abs(np.log(max(x["ratio_vs_channel_median"], 1e-6))))


def _title_terms(mature: pd.DataFrame, top_n: int = 25) -> dict[str, pd.Series]:
    tokens = mature["title"].str.lower().apply(lambda t: set(re.findall(r"[^\W\d_]{3,}", t)) - STOPWORDS)
    counts = pd.Series([w for s in tokens for w in s]).value_counts()
    return {f'title contains "{w}"': tokens.apply(lambda s, w=w: w in s)
            for w in counts[counts >= MIN_GROUP].head(top_n).index}


def analyze(channel_id: str, as_of: str | None = None) -> dict:
    channel, raw = load_frame(channel_id, as_of)
    if raw.empty:
        raise ValueError("No videos stored for this channel")
    df = _enrich(raw, datetime.fromisoformat(channel["last_collected_at"]))  # ages as of the snapshot, not today
    mature = df[df["mature"]].reset_index(drop=True)
    med = float(df["view_count"].median())
    mature_med = float(mature["view_count"].median()) if len(mature) else None

    def vid(r):
        return {"id": r.youtube_video_id, "title": r.title, "published": r.published.date().isoformat(),
                "views": int(r.view_count), "views_per_day": round(float(r.views_per_day), 1),
                "ratio_vs_median": round(r.view_count / mature_med, 2) if mature_med else None,
                "duration_s": int(r.duration_seconds),
                "engagement_rate": None if pd.isna(r.engagement_rate) else round(float(r.engagement_rate), 4),
                "thumbnail": r.thumbnail_url}

    ranked = mature.sort_values("view_count", ascending=False)
    top = [vid(r) for r in ranked.head(10).itertuples()]
    bottom = [vid(r) for r in ranked.tail(10).iloc[::-1].itertuples()]
    recent = [vid(r) for r in df.sort_values("published", ascending=False).head(10).itertuples()]

    # publishing cadence
    pub = df.sort_values("published")
    gaps = pub["published"].diff().dt.total_seconds().dropna() / 86400
    by_month = df.groupby(df["published"].dt.strftime("%Y-%m")).size()
    cadence = {
        "first_video": pub["published"].iloc[0].date().isoformat(),
        "last_video": pub["published"].iloc[-1].date().isoformat(),
        "median_gap_days": round(float(gaps.median()), 1) if len(gaps) else None,
        "uploads_last_90d": int((df["age_days"] <= 90).sum()),
        "uploads_per_month": {k: int(v) for k, v in by_month.tail(12).items()},
    }

    # recent vs earlier baseline (age-normalised by looking at views/day among mature videos only)
    trend = None
    if len(mature) >= 10:
        m = mature.sort_values("published")
        half = len(m) // 2
        a, b = m.iloc[:half]["view_count"].median(), m.iloc[half:]["view_count"].median()
        trend = {"earlier_half_median_views": int(a), "later_half_median_views": int(b),
                 "change_pct": round((b / a - 1) * 100, 1) if a else None}

    # patterns (mature videos only)
    patterns: dict[str, list] = {}
    if len(mature) >= MIN_GROUP * 2:
        dur = mature["duration_seconds"].apply(_bucket_duration)
        patterns["duration"] = _compare_groups(mature, {f"duration {b}": dur == b for b in dur.unique()})
        wd = mature["published"].dt.day_name()
        patterns["publish_weekday"] = _compare_groups(mature, {f"published on {d}": wd == d for d in wd.unique()})
        feats = pd.DataFrame([title_features(t) for t in mature["title"]])
        patterns["title_structure"] = _compare_groups(mature, {c: feats[c] for c in feats.columns})
        patterns["title_terms"] = _compare_groups(mature, _title_terms(mature))
        ntags = mature["tags"].apply(lambda t: len(t.split("\x1f")) if t else 0)
        patterns["tags"] = _compare_groups(mature, {"has tags": ntags > 0, "10+ tags": ntags >= 10})

    warnings = []
    if len(mature) < MIN_MATURE_TOTAL:
        warnings.append(f"Only {len(mature)} videos are older than {MATURE_DAYS} days; pattern results have low statistical power.")
    if channel["snapshots"] < 2:
        warnings.append("Only one metrics snapshot stored: growth velocity/decay cannot be computed yet (YouTube's public API has no history). Sync again on later days.")
    if df["like_count"].isna().all():
        warnings.append("Likes are hidden or unavailable for this channel's videos.")
    titles = dict(zip(df["youtube_video_id"], df["title"]))
    aud = audience.build(channel_id, as_of, titles)
    rch = reach.build(channel_id, as_of, titles)
    if not aud["has_data"]:
        warnings.append("Public data only: no watch time, retention, traffic sources, demographics, impressions or CTR. Connect your channel (Audience tab) to add them.")
    elif rch["state"] == "not_started":
        warnings.append("Thumbnail impressions and CTR are not being collected yet. Start collection on the Reach tab; YouTube delivers the first files ~48 hours later.")
    elif rch["state"] == "waiting":
        warnings.append("Reach (impressions/CTR) collection is set up, but YouTube hasn't delivered its first report yet (usually within ~48 hours of setup).")
    elif rch["state"] == "ready" and rch["days"] < 28:
        warnings.append(f"Reach data covers only {rch['days']} days so far; CTR comparisons will firm up as more days arrive.")
    warnings.append("Patterns are correlations on a small sample, tested many at once; some 'supported' results will be chance. Treat as hypotheses to test.")

    result = {
        "channel": {k: channel[k] for k in ("title", "custom_url", "subscriber_count", "view_count", "video_count",
                                              "published_at", "country", "last_collected_at", "snapshots")},
        "is_latest": channel["last_collected_at"] == list_snapshots(channel_id)[0]["collected_at"],
        "summary": {
            "videos_analyzed": int(len(df)), "mature_videos": int(len(mature)),
            "total_views_in_videos": int(df["view_count"].sum()),
            "median_views": int(med), "mean_views": int(df["view_count"].mean()),
            "median_views_mature": int(mature_med) if mature_med is not None else None,
            "median_views_per_day": round(float(df["views_per_day"].median()), 1),
            "median_engagement_rate": None if df["engagement_rate"].isna().all() else round(float(df["engagement_rate"].median()), 4),
            "median_duration_s": int(df["duration_seconds"].median()),
            "short_form_share": round(float(df["is_short_form"].mean()), 2),
        },
        "cadence": cadence, "trend": trend,
        "top_videos": top, "bottom_videos": bottom, "recent_videos": recent,
        "patterns": patterns, "warnings": warnings,
        "videos": [vid(r) | {"age_days": round(float(r.age_days), 1)} for r in df.sort_values("published").itertuples()],
        "analytics": aud, "reach": rch,
    }
    for key in ("videos", "top_videos", "bottom_videos", "recent_videos"):  # attach owner-only metrics to every video row
        for v in result[key]:
            if aud["has_data"]:
                v.update({k: x for k, x in aud["per_video"].get(v["id"], {}).items() if k != "a_views"})
            if rch["state"] == "ready":
                v.update({"impressions": (rch["per_video"].get(v["id"]) or {}).get("impressions"),
                          "ctr_pct": (rch["per_video"].get(v["id"]) or {}).get("ctr_pct")})
    return result
