# YouTube Channel Intelligence

V1 (public data, DESIGN.md §34) + V2 (owner-authorized YouTube Analytics, §35).

```
python -m uvicorn app.main:app --reload     # then open http://127.0.0.1:8000
```

Setup: `pip install -r requirements.txt`, copy `.env.example` to `.env`, set `YOUTUBE_API_KEY`
(enable "YouTube Data API v3" in Google Cloud; create an API key). `ANTHROPIC_API_KEY` is optional.

## Layout

`app/youtube.py` (Data API adapter + quota log) → `app/db.py` (SQLite, append-only snapshots) →
`app/stats.py` + `app/audience.py` (all numbers, deterministic) → `app/report.py` (LLM only interprets) →
`app/main.py` + `static/index.html`. V2 adds `app/auth.py` (OAuth + encrypted tokens) and `app/analytics.py` (Analytics API adapter).

## Connect YouTube Analytics (V2, your own channel only)

Adds watch time, average view duration/percentage, audience retention curves, traffic sources, demographics,
subscribers gained/lost and subscriber conversion.

1. Google Cloud Console → enable **YouTube Analytics API** and **YouTube Data API v3**.
2. **OAuth consent screen**: User type External; add your Google account under *Test users*.
3. **Credentials → Create credentials → OAuth client ID → Web application**. Authorized redirect URI:
   `http://127.0.0.1:8000/auth/google/callback` (exact match; use `127.0.0.1`, not `localhost`).
4. Put `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` in `.env`, restart, open a channel → **Audience → Connect**.
   At Google's consent screen pick the account/brand channel you own.

Notes
- Scopes are read-only: `yt-analytics.readonly` and `youtube.readonly` (the latter only to learn which channel you authorized).
- **Testing mode tokens expire after 7 days**; the app then shows "Reconnect". Publishing the consent screen to "In production" (still unverified, personal use) avoids this.
- Tokens are encrypted at rest (Fernet). Key: `APP_SECRET_KEY`, or auto-generated in `data/secret.key`. Keep `data/` private; don't commit it.
- **Disconnect** revokes the grant at Google and deletes stored tokens and all private analytics data.
- **Thumbnail impressions and CTR (Reach tab).** Not in the interactive Analytics API; they come from YouTube's separate bulk **Reporting API** (`channel_reach_basic_a1`, same OAuth scope, no reconnect needed).
  1. Also enable **YouTube Reporting API** in Google Cloud (APIs & Services > Library).
  2. Reach tab > **Start collecting** (creates a read-only reporting job on your channel; the first click is deliberate, Sync Analytics never creates it).
  3. YouTube backfills ~30 days and delivers the first files within ~48 hours; each later Sync Analytics or "Check for new reports" pulls new daily files. Files expire after 60 days, so the app copies the rows into its own table. History before the job was created can't be recovered.
  CTR is impressions-weighted everywhere (never an average of daily rates); videos under 100 impressions are excluded from rankings. Disconnect deletes this data too.
- Analytics lags ~2 days; the per-video report returns the top 200 videos; retention curves are fetched for the top 8 videos.

A full public sync of a ~1000-video channel costs ~40 Data API quota units (daily default 10,000).
Every sync appends a snapshot (Analytics included, stamped identically), and any past snapshot can be viewed from the UI.
