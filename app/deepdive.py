"""Deep-dive diagnosis. Everything here is deterministic: tables, statistics, and a rule engine that turns them into
ranked findings with evidence and a confidence level. The LLM (report.deep_dive) only interprets this output."""
import re
from datetime import date, datetime, timedelta, timezone

from . import db, mathx, reach as reach_mod
from .stats import STOPWORDS, title_features

CTR_BAND = (0.02, 0.10)        # widely cited rule-of-thumb range for YouTube CTR; NOT an official benchmark, always labelled so
SOLID, INDICATIVE = 100, 30     # impressions needed for a CTR to be solid / indicative
MIN_SIDE = 3                    # videos needed on each side of a feature comparison


def _tier(impr: int) -> str:
    return "solid" if impr >= SOLID else "indicative" if impr >= INDICATIVE else "insufficient"


def _pct(x, d=1):
    return None if x is None else round(x * 100, d)


def _raw_daily(channel_id: str, as_of: str | None) -> list[dict]:
    with db.connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT date, video_id, impressions, ctr FROM reach_daily WHERE youtube_channel_id=? AND date<=? ORDER BY date",
            (channel_id, (as_of or "9999")[:10]))]


def _days(a: str, b: str) -> list[str]:
    d0, d1 = date.fromisoformat(a), date.fromisoformat(b)
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


# ---------------------------------------------------------------------------------------------------------------------
def build(S: dict, channel_id: str, as_of: str | None = None) -> dict:
    rch, ax = S.get("reach") or {}, S.get("analytics") or {}
    videos = S["videos"]
    titles = {v["id"]: v["title"] for v in videos}
    have_reach = rch.get("state") == "ready"
    brk = reach_mod.build_breakdown(channel_id, as_of, titles)
    out: dict = {"meta": {"public": True, "analytics": bool(ax.get("has_data")), "reach": have_reach,
                          "breakdown": brk.get("state") == "ready", "videos": len(videos),
                          "snapshot": S["channel"]["last_collected_at"]}}
    findings: list[dict] = []

    def add(fid, area, title, severity, confidence, kind, evidence, implication):
        findings.append({"id": fid, "area": area, "title": title, "severity": severity, "confidence": confidence,
                         "kind": kind, "evidence": evidence, "implication": implication})

    now = datetime.fromisoformat(S["channel"]["last_collected_at"])
    cad = S["cadence"]
    last_up = date.fromisoformat(cad["last_video"])
    days_idle = (now.date() - last_up).days
    out["cadence"] = {**{k: cad[k] for k in ("first_video", "last_video", "median_gap_days", "uploads_last_90d")}, "days_since_last_upload": days_idle,
                      "uploads_per_month": cad["uploads_per_month"]}
    gap = cad["median_gap_days"] or 0
    if days_idle >= 30 and days_idle > 3 * max(gap, 1):
        add("stale", "cadence", "No new uploads for a long time", "high", "high", "fact",
            [f"Last upload {cad['last_video']}, {days_idle} days before this snapshot.", f"Median gap between uploads was {gap} days.",
             f"{cad['uploads_last_90d']} uploads in the last 90 days."],
            "YouTube has no recent activity to recommend; every other fix is limited until uploads resume.")

    if not have_reach:
        out["findings"] = findings
        out["limits"] = ["No reach (impressions/CTR) data yet; the deep dive covers public and Analytics data only."]
        return out

    raw = _raw_daily(channel_id, as_of)
    first, last = rch["first_date"], rch["last_date"]
    all_days = _days(first, last)
    window = len(all_days)
    by_day: dict[str, dict] = {d: {"impr": 0, "clicks": 0.0, "vids": {}} for d in all_days}
    for r in raw:
        b = by_day[r["date"]]
        b["impr"] += r["impressions"]
        b["clicks"] += r["impressions"] * r["ctr"]
        b["vids"][r["video_id"]] = b["vids"].get(r["video_id"], 0) + r["impressions"]
    series = [by_day[d]["impr"] for d in all_days]
    total_impr = sum(series)
    total_clicks = round(sum(by_day[d]["clicks"] for d in all_days))
    overall_ctr = total_clicks / total_impr if total_impr else None

    # ---- visibility ------------------------------------------------------------------------------------------------
    spike_idx = mathx.mad_spikes(series)
    spike_days = [all_days[i] for i in spike_idx]
    base_vals = [v for i, v in enumerate(series) if i not in spike_idx]
    baseline = mathx.median(base_vals) or 0
    spike_imp = sum(series[i] for i in spike_idx)
    spikes = []
    for i in spike_idx:
        d = all_days[i]
        top = sorted(by_day[d]["vids"].items(), key=lambda x: -x[1])[:3]
        sp = {"day": d, "impressions": series[i], "videos_shown": len(by_day[d]["vids"]),
              "top_videos": [{"title": titles.get(v, v), "impressions": n} for v, n in top]}
        if brk["state"] == "ready":
            src = {}
            for r in reach_mod.load_breakdown(channel_id, as_of):
                if r["date"] == d:
                    src[r["source_type"]] = src.get(r["source_type"], 0) + r["impressions"]
            sp["top_sources"] = [{"source": reach_mod.TRAFFIC_SOURCES.get(k, str(k)), "impressions": n}
                                 for k, n in sorted(src.items(), key=lambda x: -x[1])[:3]]
        spikes.append(sp)
    vid_imp = sorted(((v, p["impressions"]) for v, p in rch["per_video"].items()), key=lambda x: -x[1])
    top2_share = sum(n for _, n in vid_imp[:2]) / total_impr if total_impr else None
    herf = sum((n / total_impr) ** 2 for _, n in vid_imp) if total_impr else None
    third = max(window // 3, 1)
    early, late = mathx.median([v for i, v in enumerate(series[:third]) if i not in spike_idx]), mathx.median([v for i, v in enumerate(series[-third:], start=window - third) if i not in spike_idx])
    zero_days = sum(1 for v in series if v == 0)
    out["visibility"] = {
        "window": {"from": first, "to": last, "days": window}, "total_impressions": total_impr,
        "mean_daily": round(total_impr / window, 1), "baseline_daily_median_excl_spikes": baseline,
        "days_with_no_impressions": zero_days, "videos_with_any_impressions": len(vid_imp), "videos_total": len(videos),
        "spike_days": spikes, "spike_share_of_impressions": round(spike_imp / total_impr, 3) if total_impr else None,
        "top2_video_share": _pct(top2_share), "concentration_hhi": None if herf is None else round(herf, 3),
        "baseline_first_third": early, "baseline_last_third": late,
        "daily": [{"day": d, "impressions": series[i], "spike": i in spike_idx} for i, d in enumerate(all_days)]}
    if baseline < 50:
        add("low_visibility", "visibility", "YouTube shows the channel to very few people", "high", "high", "fact",
            [f"Typical day (median, spikes excluded): {baseline:.0f} thumbnail impressions.", f"{total_impr} impressions over {window} days, {zero_days} days with none.",
             f"{len(vid_imp)} of {len(videos)} videos received any impressions in the window."],
            "Reach, not click-through, is the limiting factor. Packaging tweaks cannot fix this on their own.")
    if spike_idx and spike_imp / total_impr >= 0.25:
        add("spike_dependence", "visibility", "A short burst produced much of the reach", "medium", "high", "fact",
            [f"{len(spike_idx)} spike day(s) ({', '.join(spike_days)}) = {_pct(spike_imp / total_impr, 0)}% of all impressions.",
             "Spike days touched " + ", ".join(f"{s['videos_shown']} of {len(videos)} videos" for s in spikes)
             + " (a broad burst across the catalog, not one breakout video)" if all(s["videos_shown"] >= 0.5 * len(videos) for s in spikes)
             else "Spike day top video: " + "; ".join(f"{t['title'][:50]} ({t['impressions']})" for t in spikes[0]["top_videos"][:2])],
            "Without the burst the baseline is far lower. Find what caused it (Studio traffic sources for those dates) and whether it repeats.")
    if top2_share is not None and top2_share >= 0.4 and len(vid_imp) >= 4:
        add("concentration", "visibility", "Reach is concentrated in two videos", "medium", "medium", "fact",
            [f"Top 2 videos hold {_pct(top2_share, 0)}% of impressions: " + "; ".join(f"{titles.get(v, v)[:48]} ({n})" for v, n in vid_imp[:2])],
            "YouTube is testing these videos with audiences; the rest are barely distributed. What these two share is the best lead.")

    # ---- CTR with uncertainty ------------------------------------------------------------------------------------------
    ci = mathx.wilson(total_clicks, total_impr)
    last28, prev28 = rch["periods"]["last_28d"], rch["periods"]["previous_28d"]
    ctr_change_p = mathx.two_prop_p(last28["clicks"], last28["impressions"], prev28["clicks"], prev28["impressions"]) if prev28 else None
    per_video = []
    rest_cache = {v: (total_clicks - p["clicks"], total_impr - p["impressions"]) for v, p in rch["per_video"].items()}
    m_tests = max(sum(1 for p in rch["per_video"].values() if p["impressions"] >= INDICATIVE), 1)
    for v in videos:
        p = rch["per_video"].get(v["id"])
        if not p:
            continue
        rc, rn = rest_cache[v["id"]]
        pv = mathx.two_prop_p(p["clicks"], p["impressions"], rc, rn)
        vci = mathx.wilson(p["clicks"], p["impressions"])
        pub = date.fromisoformat(v["published"])
        launch7 = sum(r["impressions"] for r in raw if r["video_id"] == v["id"] and pub <= date.fromisoformat(r["date"]) < pub + timedelta(days=7)) if pub >= date.fromisoformat(first) else None
        per_video.append({
            "id": v["id"], "title": v["title"], "published": v["published"], "age_days": v["age_days"], "duration_min": round(v["duration_s"] / 60, 1),
            "impressions": p["impressions"], "clicks": p["clicks"], "ctr_pct": _pct(p["ctr"]), "ctr_ci_pct": None if not vci else [_pct(vci[0]), _pct(vci[1])],
            "ctr_shrunk_pct": _pct(mathx.beta_shrunk_rate(p["clicks"], p["impressions"], overall_ctr or 0)), "tier": _tier(p["impressions"]),
            "p_vs_rest": None if pv is None else round(pv, 3), "significant_after_correction": pv is not None and pv < 0.05 / m_tests and p["impressions"] >= INDICATIVE,
            "impressions_per_day_in_window": round(p["impressions"] / min(max(v["age_days"], 1), window), 2), "launch_7d_impressions": launch7,
            "views": v["views"], "avg_view_pct": v.get("avg_view_pct"), "watch_hours": v.get("watch_hours"), "subs_gained": v.get("subs_gained")})
    per_video.sort(key=lambda x: -x["impressions"])
    n_sig = sum(1 for x in per_video if x["significant_after_correction"])
    # Test planning: how many impressions per variant to detect a given CTR lift, and how long that takes at today's reach
    plan = []
    if overall_ctr:
        for lift in (0.25, 0.5, 1.0):
            n = mathx.sample_size_two_prop(overall_ctr, min(overall_ctr * (1 + lift), 0.99))
            plan.append({"relative_lift_pct": int(lift * 100), "from_ctr_pct": _pct(overall_ctr), "to_ctr_pct": _pct(overall_ctr * (1 + lift)),
                         "impressions_per_variant": n, "days_at_baseline": None if not baseline else round(2 * n / baseline),
                         "daily_impressions_needed_for_28_days": round(2 * n / 28)})
    out["ctr"] = {"overall_pct": _pct(overall_ctr), "overall_ci_pct": None if not ci else [_pct(ci[0]), _pct(ci[1])], "clicks": total_clicks,
                  "rule_of_thumb_band_pct": [_pct(CTR_BAND[0], 0), _pct(CTR_BAND[1], 0)], "last28_vs_prev28_p": None if ctr_change_p is None else round(ctr_change_p, 3),
                  "videos_significantly_different": n_sig, "videos_tested": m_tests, "per_video": per_video, "test_plan": plan}
    if ci:
        verdict = "healthy" if ci[0] >= CTR_BAND[0] else "low" if ci[1] < CTR_BAND[0] else "inconclusive"
        add("ctr_level", "ctr", {"healthy": "Click-through rate is healthy", "low": "Click-through rate is low", "inconclusive": "Click-through rate level is uncertain"}[verdict],
            "info" if verdict == "healthy" else "high" if verdict == "low" else "medium", "medium" if verdict != "inconclusive" else "low", "fact",
            [f"CTR {_pct(overall_ctr)}% from {total_clicks} clicks on {total_impr} impressions (95% interval {_pct(ci[0])}%-{_pct(ci[1])}%).",
             f"Common rule of thumb for YouTube CTR: {_pct(CTR_BAND[0], 0)}-{_pct(CTR_BAND[1], 0)}% (not an official benchmark; varies by source and audience)."],
            "When shown, the thumbnails/titles are being clicked at a reasonable rate; focus on getting shown." if verdict == "healthy" else
            "Packaging may be holding clicks back." if verdict == "low" else "The sample is too small to call it good or bad.")
    if per_video and n_sig == 0:
        add("ctr_noise", "ctr", "Differences in CTR between videos are not statistically reliable", "medium", "high", "fact",
            [f"0 of {m_tests} videos with 30+ impressions differ significantly from the rest after multiple-comparison correction.",
             "Highest raw CTRs sit on tiny samples: " + "; ".join(f"{x['title'][:30]} {x['ctr_pct']}% on {x['impressions']} impr." for x in sorted(per_video, key=lambda z: -(z['ctr_pct'] or 0))[:3])],
            "Do not rank or redesign thumbnails by single-video CTR yet; pool across videos or wait for more impressions.")
    if plan and plan[1]["days_at_baseline"] and plan[1]["days_at_baseline"] > 90:
        add("test_infeasible", "testing", "A/B testing packaging is not feasible at the current reach", "medium", "high", "fact",
            [f"Detecting a CTR lift from {plan[1]['from_ctr_pct']}% to {plan[1]['to_ctr_pct']}% needs about {plan[1]['impressions_per_variant']} impressions per variant.",
             f"At {baseline:.0f} impressions per day that is about {plan[1]['days_at_baseline']} days for one test."],
            "Increase reach first, or compare pooled groups of videos; single-video thumbnail tests would not be conclusive.")

    # ---- funnel (impressions -> clicks -> views -> watch time) on ONE aligned window -------------------------------------
    # Analytics lags reach by a few days, so both are cut to the same 28 days ending at the earlier of the two end dates.
    if ax.get("has_data") and ax.get("daily"):
        end = min(date.fromisoformat(last), date.fromisoformat(ax["periods"]["through"]))
        start = end - timedelta(days=27)
        in_win = lambda d: start <= date.fromisoformat(d) <= end
        impr_w = sum(by_day[d]["impr"] for d in all_days if in_win(d))
        clicks_w = round(sum(by_day[d]["clicks"] for d in all_days if in_win(d)))
        dwin = [x for x in ax["daily"] if in_win(x["day"])]
        views, hours = sum(x["views"] for x in dwin), round(sum(x["watch_hours"] for x in dwin), 1)
        out["funnel"] = {"window": [start.isoformat(), end.isoformat()], "impressions": impr_w, "clicks": clicks_w,
                         "ctr_pct": _pct(clicks_w / impr_w) if impr_w else None, "views": views, "watch_hours": hours,
                         "avg_view_duration_min": round(hours * 60 / views, 1) if views else None,
                         "views_per_1000_impressions": round(views / impr_w * 1000, 1) if impr_w else None,
                         "watch_hours_per_1000_impressions": round(hours / impr_w * 1000, 1) if impr_w else None,
                         "views_not_from_impressions_pct": _pct(1 - clicks_w / views, 0) if views and clicks_w <= views else None}
        if views and clicks_w < 0.6 * views:
            add("offfeed_views", "funnel", "Many views did not come from a thumbnail impression", "info", "medium", "fact",
                [f"{clicks_w} clicks vs {views} views in the same 28 days ({start} to {end}): about {_pct(1 - clicks_w / views, 0)}% of views came from places that don't count impressions "
                 "(subscribers' feed, external links, playlists, notifications)."],
                "Reach numbers understate the audience; check Audience traffic sources.")
        elif impr_w:
            add("funnel_yield", "funnel", "What each 1,000 impressions are worth", "info", "medium", "fact",
                [f"{impr_w} impressions -> {clicks_w} clicks -> {views} views -> {hours} watch hours ({start} to {end}).",
                 f"Per 1,000 impressions: {out['funnel']['views_per_1000_impressions']} views and {out['funnel']['watch_hours_per_1000_impressions']} watch hours; average view {out['funnel']['avg_view_duration_min']} min."],
                "Viewers who do click watch for a long time, so the value per impression is high; the lever is more impressions.")

    # ---- sources / devices ------------------------------------------------------------------------------------------
    if brk["state"] == "ready":
        out["sources"] = brk
        top = brk["by_source"][0]
        search = next((s for s in brk["by_source"] if s["key"] == 5), None)
        add("source_mix", "sources", f"Most impressions come from {top['label']}", "info", "medium", "fact",
            [f"{top['label']}: {_pct(top['share'], 0)}% of impressions (CTR {_pct(top['ctr'])}%).",
             "Mix: " + ", ".join(f"{s['label']} {_pct(s['share'], 0)}%" for s in brk["by_source"][:5]),
             f"Breakdown covers {brk['days']} days ({brk['first_date']} to {brk['last_date']})."],
            "This is where YouTube already places the videos; packaging should suit that surface.")
        if search is None or search["share"] < 0.05:
            add("search_gap", "sources", "Almost no impressions come from YouTube search", "medium", "medium", "fact",
                [f"Search share of impressions: {_pct(search['share'] if search else 0, 1)}%."],
                "The videos are not surfacing for search queries; titles and descriptions are not capturing search demand.")
    else:
        out["sources"] = {"state": brk["state"]}

    # ---- content themes (title tokens) with pooled reach + retention ---------------------------------------------------
    tok = {v["id"]: set(re.findall(r"[^\W\d_]{3,}", re.sub(r"#\w+", " ", v["title"].lower()))) - STOPWORDS for v in videos}
    freq: dict[str, int] = {}
    for s in tok.values():
        for w in s:
            freq[w] = freq.get(w, 0) + 1
    generic = {w for w, c in freq.items() if c >= 0.75 * len(videos)}
    themes = []
    for w, c in freq.items():
        if c < 2 or w in generic:
            continue
        ids = [v for v, s in tok.items() if w in s]
        ps = [rch["per_video"].get(v) for v in ids]
        impr = sum(p["impressions"] for p in ps if p)
        clicks = sum(p["clicks"] for p in ps if p)
        vs = [v for v in videos if v["id"] in ids]
        rets = [v["avg_view_pct"] for v in vs if v.get("avg_view_pct") is not None]
        rest_c, rest_i = total_clicks - clicks, total_impr - impr
        themes.append({"theme": w, "videos": len(ids), "impressions": impr, "share_of_impressions": _pct(impr / total_impr, 0) if total_impr else None,
                       "clicks": round(clicks), "rest_clicks": round(rest_c), "rest_impressions": rest_i,
                       "ctr_pct": _pct(clicks / impr) if impr else None, "ctr_rest_pct": _pct(rest_c / rest_i) if rest_i else None,
                       "ctr_vs_rest_p": None if mathx.two_prop_p(clicks, impr, rest_c, rest_i) is None else round(mathx.two_prop_p(clicks, impr, rest_c, rest_i), 3),
                       "views": sum(v["views"] for v in vs),
                       "median_avg_view_pct": mathx.median(rets), "watch_hours": round(sum(v.get("watch_hours") or 0 for v in vs), 1),
                       "subs_gained": sum(v.get("subs_gained") or 0 for v in vs), "examples": [titles[v][:50] for v in ids[:2]]})
    themes.sort(key=lambda t: -t["impressions"])
    out["themes"] = {"generic_words_ignored": sorted(generic)[:12], "items": themes[:10]}
    if themes and themes[0]["impressions"] and total_impr and themes[0]["impressions"] / total_impr >= 0.3:
        t0 = themes[0]
        lower = t0["ctr_pct"] is not None and t0["ctr_rest_pct"] is not None and t0["ctr_pct"] < t0["ctr_rest_pct"]
        add("theme_lead", "content", f"The '{t0['theme']}' theme gets the most reach", "info", "low", "fact",
            [f"{t0['videos']} videos with '{t0['theme']}' hold {t0['share_of_impressions']}% of impressions, median average view {t0['median_avg_view_pct']}% (channel median {mathx.median([v['avg_view_pct'] for v in videos if v.get('avg_view_pct') is not None])}%), {t0['subs_gained']} subscribers gained.",
             f"CTR {t0['ctr_pct']}% ({t0['clicks']} clicks on {t0['impressions']} impressions) vs {t0['ctr_rest_pct']}% for the other videos ({t0['rest_clicks']} on {t0['rest_impressions']}); "
             f"unadjusted p={t0['ctr_vs_rest_p']}. This is one of several themes examined, so treat it as a lead rather than proof."],
            ("YouTube distributes this theme most but viewers click it less than the rest of the catalog: a possible packaging gap on the best-reached theme (hypothesis). "
             + ("Its retention is at or above the channel median, so the content itself does not look like the weak side."
                if (t0["median_avg_view_pct"] or 0) >= (mathx.median([v["avg_view_pct"] for v in videos if v.get("avg_view_pct") is not None]) or 0)
                else "Its retention is also below the channel median, so content may contribute too.")
             if lower else "Reach and click behavior point the same way; it is the strongest lead for new videos (small sample)."))
    # CTR tends to fall as a video is shown more widely, and tiny-sample CTRs are extreme; check how the two relate here.
    pv_rows = [x for x in per_video if x["ctr_pct"] is not None]
    ctr_reach_rho = mathx.spearman([x["impressions"] for x in pv_rows], [x["ctr_pct"] for x in pv_rows])
    out["ctr"]["impressions_vs_ctr_spearman"] = ctr_reach_rho
    if ctr_reach_rho is not None and ctr_reach_rho <= -0.4 and len(pv_rows) >= 8:
        add("ctr_vs_reach", "ctr", "The videos shown the most have the lowest CTR", "info", "low", "hypothesis",
            [f"Across {len(pv_rows)} videos, impressions and CTR are negatively related (Spearman {ctr_reach_rho}).",
             "Highest-impression videos: " + "; ".join(f"{x['title'][:28]} {x['ctr_pct']}% on {x['impressions']}" for x in pv_rows[:3])],
            "Expected: wider distribution reaches less interested viewers, and the highest CTRs sit on a handful of impressions. Compare CTR only between videos with similar reach.")

    # ---- packaging features vs reach (pooled, with tests) -------------------------------------------------------------
    feats = {v["id"]: title_features(v["title"]) | {"has 3+ hashtags": v["title"].count("#") >= 3} for v in videos}
    feat_rows = []
    for name in next(iter(feats.values())):
        yes = [v for v in videos if feats[v["id"]][name] and v["id"] in rch["per_video"]]
        no = [v for v in videos if not feats[v["id"]][name] and v["id"] in rch["per_video"]]
        if len(yes) < MIN_SIDE or len(no) < MIN_SIDE:
            continue
        agg = lambda grp: (sum(rch["per_video"][v["id"]]["impressions"] for v in grp), sum(rch["per_video"][v["id"]]["clicks"] for v in grp))
        (iy, cy), (in_, cn) = agg(yes), agg(no)
        feat_rows.append({"feature": name, "videos_with": len(yes), "videos_without": len(no),
                          "impressions_per_video_with": round(iy / len(yes), 1), "impressions_per_video_without": round(in_ / len(no), 1),
                          "ctr_with_pct": _pct(cy / iy) if iy else None, "ctr_without_pct": _pct(cn / in_) if in_ else None,
                          "ctr_p": None if mathx.two_prop_p(cy, iy, cn, in_) is None else round(mathx.two_prop_p(cy, iy, cn, in_), 3)})
    feat_rows.sort(key=lambda r: -abs((r["impressions_per_video_with"] or 0) - (r["impressions_per_video_without"] or 0)))
    out["packaging_features"] = feat_rows[:8]
    ages = [(v["age_days"], rch["per_video"].get(v["id"], {}).get("impressions", 0) / min(max(v["age_days"], 1), window)) for v in videos]
    out["age_vs_reach_spearman"] = mathx.spearman([a for a, _ in ages], [b for _, b in ages])
    rho_age = out["age_vs_reach_spearman"]
    if rho_age is not None and rho_age <= -0.4 and len(ages) >= 8:
        add("freshness", "visibility", "Newer videos get more impressions per day than older ones", "medium", "low", "hypothesis",
            [f"Video age vs impressions per day across {len(ages)} videos: Spearman {rho_age} (negative = newer videos are shown more)."],
            "Consistent with YouTube favoring recent uploads, which makes the upload gap more costly. With few videos and one window this could also be a coincidence of which videos are good.")

    # ---- packaging vs content matrix -----------------------------------------------------------------------------------
    rets = [x["avg_view_pct"] for x in per_video if x["avg_view_pct"] is not None]
    ret_med = mathx.median(rets)
    matrix = []
    for x in per_video:
        if x["tier"] == "insufficient" or x["avg_view_pct"] is None or ret_med is None or not overall_ctr:
            quad = "insufficient data" if x["tier"] == "insufficient" else "needs retention data"
        else:
            hi_c, lo_c = x["ctr_shrunk_pct"] >= overall_ctr * 100 * 1.2, x["ctr_shrunk_pct"] <= overall_ctr * 100 * 0.8
            hi_r, lo_r = x["avg_view_pct"] >= ret_med * 1.2, x["avg_view_pct"] <= ret_med * 0.8
            quad = ("winner: repeat the formula" if hi_c and hi_r else "packaging problem: good content, under-clicked" if lo_c and hi_r else
                    "promise mismatch: clicked, then left" if hi_c and lo_r else "weak on both" if lo_c and lo_r else "typical")
        matrix.append({"title": x["title"], "impressions": x["impressions"], "tier": x["tier"], "ctr_shrunk_pct": x["ctr_shrunk_pct"],
                       "avg_view_pct": x["avg_view_pct"], "verdict": quad})
    out["matrix"] = {"channel_median_avg_view_pct": ret_med, "items": matrix}
    solid = [m for m in matrix if m["tier"] != "insufficient"]
    if not solid:
        add("matrix_insufficient", "diagnosis", "No video has enough impressions to separate packaging problems from content problems", "medium", "high", "fact",
            [f"0 of {len(matrix)} videos reach {INDICATIVE} impressions with retention data; the best has {matrix[0]['impressions'] if matrix else 0}."],
            "Diagnosis by video is premature; the packaging-vs-content question can only be answered after reach grows.")

    # ---- retention shape (content-side signal from Analytics) -----------------------------------------------------------
    if ax.get("has_data") and ax.get("retention"):
        early_drop = [(v["title"], v["key_points"]["10%"]) for v in ax["retention"].values()]
        weak = [t for t, k in early_drop if k < 0.3]
        out["retention_shape"] = {"videos": len(early_drop), "videos_below_30pct_at_10pct_mark": len(weak),
                                  "per_video": [{"title": v["title"], **v["key_points"], "relative_retention": v["avg_relative_retention"]} for v in ax["retention"].values()]}
        if len(weak) >= max(2, len(early_drop) // 2):
            add("early_drop", "content", "Many viewers leave early in each video", "medium", "medium", "fact",
                [f"{len(weak)} of {len(early_drop)} videos with retention curves keep under 30% of viewers at the 10% mark."],
                "For hour-long mixes some drop is normal, but a stronger first minutes (clear promise, no slow intro) can improve how YouTube judges the video.")

    findings.sort(key=lambda f: ({"high": 0, "medium": 1, "low": 2, "info": 3}[f["severity"]], {"high": 0, "medium": 1, "low": 2}[f["confidence"]]))
    out["findings"] = findings
    out["limits"] = [f"Reach data covers {window} days; per-video CTR rests on small counts (largest video: {per_video[0]['impressions'] if per_video else 0} impressions).",
                     "Associations only: with a handful of videos nothing here proves cause and effect.",
                     "Impressions exclude views that do not start from a thumbnail impression (subscriptions page, external, direct, notifications).",
                     "Thumbnail images are interpreted by the AI step, not measured here."]
    return out
