"""LLM interpretation layer. Receives pre-computed stats only; never queries YouTube or computes numbers."""
import json

from .config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL_QUESTION, ANTHROPIC_MODEL_REPORT

SYSTEM = """You are a YouTube channel analyst. You receive a JSON of deterministic statistics for ONE channel.
Rules:
- Use only numbers present in the JSON. Never compute or invent numbers; quote them as given.
- Separate FACT (from the data) from INTERPRETATION (hedged) from RECOMMENDATION (a concrete, testable action).
- Patterns are correlations on small samples. Never say a feature "causes" views. Prefer patterns with evidence "supported"; treat "inconclusive" as weak hints at most.
- Respect the `warnings` list and mention the important data limitations briefly.
- Each recommendation: priority, the evidence (cite pattern + video counts), confidence (low/medium/high), and how to test it.
- If an `analytics` section is present it comes from the owner's private YouTube Analytics. Use it: retention (watch_ratio_at = share of viewers still watching at that point; avg_relative_retention ~0.5 is typical for similar-length videos), watch time, traffic sources, demographics, subscriber conversion.
- If a `reach` section is present it has thumbnail impressions and click-through rate (CTR, in percent) from the owner's private data. CTR = how often a shown thumbnail+title was clicked; it varies a lot by traffic source and by who it is shown to, so a falling CTR can come from broader recommendation, not worse packaging. Never judge a video's CTR on fewer impressions than `min_impressions_for_ranking`. Combine CTR with retention to separate packaging problems (low CTR) from content problems (low retention). If there is no `reach` section, impressions/CTR are simply unavailable; never guess them.
- All durations are in MINUTES (fields ending in _minutes). Many videos are an hour or longer; never describe them as short.
- Confidence calibration: with fewer than ~20 mature videos or groups under ~10 videos, confidence can be at most "medium", usually "low". Do not tell the user to abandon what they do on the basis of 2-3 videos.
- Be concise."""

REPORT_FORMAT = "Write the full channel report as Markdown with sections: Snapshot, What's working, What's weak, Recommendations, Data gaps."
QUESTION_FORMAT = ("Answer ONLY this question, directly and concisely, following any length or format the user asks for. "
                   "Do not write a full report. Cite the specific numbers you rely on.\nQuestion: ")


def _minutes(o):
    """Rename *_s (seconds) fields to *_minutes so no model can mistake seconds for minutes."""
    if isinstance(o, dict):
        return {(k[:-2] + "_minutes" if k.endswith("_s") and isinstance(v, (int, float)) else k):
                (round(v / 60, 1) if k.endswith("_s") and isinstance(v, (int, float)) else _minutes(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [_minutes(x) for x in o]
    return o


def _compact(stats: dict) -> dict:
    s = {k: v for k, v in stats.items() if k not in ("videos", "analytics")}
    a = stats.get("analytics") or {}
    if a.get("has_data"):  # drop bulky series; keep summaries and retention key points
        s["analytics"] = {k: v for k, v in a.items() if k not in ("daily", "per_video", "retention", "meta")}
        s["analytics"]["retention_by_video"] = {v["title"]: {"watch_ratio_at": v["key_points"], "avg_relative_retention": v["avg_relative_retention"]}
                                                for v in a["retention"].values()}
    s.pop("reach", None)
    r = stats.get("reach") or {}
    if r.get("state") == "ready":  # thumbnail impressions + CTR; ctr values below are PERCENTAGES
        pct = lambda x: None if x is None else round(x * 100, 2)
        fix = lambda p: {**{k: v for k, v in p.items() if k not in ("ctr", "clicks")}}
        s["reach"] = {"covers": f"{r['first_date']} to {r['last_date']} ({r['days']} days)",
                      "overall_ctr_pct": pct(r["overall"]["ctr"]), "overall_impressions": r["overall"]["impressions"],
                      "last_28d": {"impressions": r["periods"]["last_28d"]["impressions"], "ctr_pct": pct(r["periods"]["last_28d"]["ctr"])},
                      "previous_28d": r["periods"]["previous_28d"] and {"impressions": r["periods"]["previous_28d"]["impressions"], "ctr_pct": pct(r["periods"]["previous_28d"]["ctr"])},
                      "change": r["periods"]["change_pct"],
                      "best_ctr_videos": [fix(x) for x in r["ctr_rank"]["best"]], "worst_ctr_videos": [fix(x) for x in r["ctr_rank"]["worst"]],
                      "min_impressions_for_ranking": r["ctr_rank"]["min_impressions"]}
    for key in ("top_videos", "bottom_videos", "recent_videos"):
        s[key] = [{k: v for k, v in x.items() if k != "thumbnail"} for x in s[key]]
    s["patterns"] = {k: v[:8] for k, v in s["patterns"].items()}
    return _minutes(s)


def llm_available() -> bool:
    return bool(ANTHROPIC_API_KEY)


def model_for(question: str | None) -> str:
    return ANTHROPIC_MODEL_QUESTION if question else ANTHROPIC_MODEL_REPORT


def ask(stats: dict, question: str | None = None) -> tuple[str, str]:
    """Returns (markdown, model_used). A blank question means the full report."""
    import anthropic

    try:  # the SDK's HTTP library differs by version (httpx2 in newer releases); the transport must come from the same one
        import httpx2 as sdk_httpx
    except ImportError:
        import httpx as sdk_httpx

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, http_client=anthropic.DefaultHttpxClient(
        transport=sdk_httpx.HTTPTransport(local_address="0.0.0.0"), timeout=120))  # IPv4 only, see app/http.py
    user = f"Statistics:\n```json\n{json.dumps(_compact(stats), ensure_ascii=False)}\n```\n\n" + (
        QUESTION_FORMAT + question if question else REPORT_FORMAT)
    model = model_for(question)
    msg = client.messages.create(model=model, max_tokens=2500, system=SYSTEM,
                                 messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in msg.content if b.type == "text"), model


def fallback_report(stats: dict) -> str:
    """Template report used when no ANTHROPIC_API_KEY is configured; fully deterministic."""
    c, s = stats["channel"], stats["summary"]
    lines = [f"# {c['title']}", "",
             f"**{s['videos_analyzed']} videos**, median {s['median_views']:,} views "
             f"({s['median_views_per_day']}/day), median engagement "
             f"{'n/a' if s['median_engagement_rate'] is None else format(s['median_engagement_rate'], '.1%')}.", "",
             "## Top videos"]
    lines += [f"- {v['title']} — {v['views']:,} views ({v['ratio_vs_median']}x median)" for v in stats["top_videos"][:5]]
    lines += ["", "## Weakest videos"]
    lines += [f"- {v['title']} — {v['views']:,} views ({v['ratio_vs_median']}x median)" for v in stats["bottom_videos"][:5]]
    lines += ["", "## Supported patterns (correlations, not causes)"]
    found = False
    for group, items in stats["patterns"].items():
        for p in items:
            if p["evidence"] == "supported":
                found = True
                lines.append(f"- [{group}] {p['pattern']}: {p['videos']} videos, {p['ratio_vs_channel_median']}x median (95% CI {p['ci95'][0]}–{p['ci95'][1]})")
    if not found:
        lines.append("- None met the evidence threshold.")
    a = stats.get("analytics") or {}
    if a.get("has_data"):
        lines += ["", "## Audience (owner Analytics)"]
        p = a.get("periods")
        if p:
            lines.append(f"- Last 28 days (to {p['through']}): {p['last_28d']['views']:,} views, {p['last_28d']['watch_hours']} watch hours, "
                         f"net subscribers {p['last_28d']['subs_net']:+d} (views change vs previous 28 days: {p['change_pct']['views']}%).")
        if a.get("traffic"):
            lines.append("- Top traffic sources: " + ", ".join(f"{t['source']} {t['views_share']:.0%}" for t in a["traffic"][:3]))
        rr = a["retention_rank"]
        if rr["best"]:
            lines.append(f"- Median average view percentage: {rr['channel_median_avg_view_pct']}%. Best retained: {rr['best'][0]['title']} ({rr['best'][0]['avg_view_pct']}%).")
    r = stats.get("reach") or {}
    if r.get("state") == "ready":
        p = r["periods"]
        fmt = lambda x: "n/a" if x is None else f"{x:.1%}"
        lines += ["", "## Reach (thumbnail impressions and CTR)",
                  f"- Last 28 days (to {r['last_date']}): {p['last_28d']['impressions']:,} impressions, CTR {fmt(p['last_28d']['ctr'])}"
                  + (f" (previous 28 days: {p['previous_28d']['impressions']:,}, CTR {fmt(p['previous_28d']['ctr'])})." if p["previous_28d"] else ".")]
        if r["ctr_rank"]["best"]:
            b = r["ctr_rank"]["best"][0]
            lines.append(f"- Highest CTR (≥{r['ctr_rank']['min_impressions']} impressions): {b['title']} ({fmt(b['ctr'])}).")
    lines += ["", "## Data gaps"] + [f"- {w}" for w in stats["warnings"]]
    return "\n".join(lines)
