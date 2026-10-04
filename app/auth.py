"""Google OAuth 2.0 (authorization-code + PKCE) with encrypted token storage. Read-only scopes only."""
import base64
import hashlib
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

from . import db, http, youtube
from .config import (APP_SECRET_KEY, DB_PATH, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
                     OAUTH_REDIRECT_URI)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
# Least privilege: Analytics read-only for the reports, YouTube read-only to learn which channel was authorized.
SCOPES = ["https://www.googleapis.com/auth/yt-analytics.readonly", "https://www.googleapis.com/auth/youtube.readonly"]
STATE_TTL = 600

_pending: dict[str, tuple[str, float]] = {}   # state -> (pkce verifier, created); in-memory is fine for a single local process


class AuthError(Exception):
    pass


def configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


# ---- token encryption -------------------------------------------------------
def _fernet() -> Fernet:
    key = APP_SECRET_KEY.encode() if APP_SECRET_KEY else None
    if not key:
        path = DB_PATH.parent / "secret.key"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(Fernet.generate_key())
        key = path.read_bytes().strip()
    return Fernet(key)


def _enc(s: str) -> str:
    return _fernet().encrypt(s.encode()).decode()


def _dec(s: str) -> str:
    try:
        return _fernet().decrypt(s.encode()).decode()
    except InvalidToken as e:
        raise AuthError("Stored token can't be decrypted (secret key changed). Reconnect your channel.") from e


# ---- authorization flow -----------------------------------------------------
def start_url() -> str:
    if not configured():
        raise AuthError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set (see README, 'Connect YouTube Analytics').")
    now = time.time()
    for k in [k for k, (_, t) in _pending.items() if now - t > STATE_TTL]:
        del _pending[k]
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    _pending[state] = (verifier, now)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return AUTH_URL + "?" + urlencode({
        "client_id": GOOGLE_CLIENT_ID, "redirect_uri": OAUTH_REDIRECT_URI, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent select_account",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })


def _token_request(data: dict) -> dict:
    try:
        r = http.client.post(TOKEN_URL, data={**data, "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET})
    except httpx.HTTPError as e:
        raise AuthError(f"Network error talking to Google: {type(e).__name__}") from e
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200:
        err = body.get("error", "unknown_error")
        raise AuthError("invalid_grant" if err == "invalid_grant" else f"Google token error: {err} {body.get('error_description', '')}".strip())
    return body


def handle_callback(code: str, state: str) -> str:
    """Exchange the code, find which channel was authorized, store encrypted tokens. Returns channel id."""
    pending = _pending.pop(state, None)
    if not pending or time.time() - pending[1] > STATE_TTL:
        raise AuthError("Authorization expired or invalid state. Please try connecting again.")
    tok = _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": OAUTH_REDIRECT_URI, "code_verifier": pending[0]})
    if not tok.get("refresh_token"):
        raise AuthError("Google did not return a refresh token. Remove the app at myaccount.google.com/permissions and connect again.")
    granted = set(tok.get("scope", "").split())
    if not set(SCOPES) <= granted:
        raise AuthError("Not all requested permissions were granted. Both read-only permissions are needed.")
    # Which channel did the user authorize? (the one whose Google account/brand account they picked)
    try:
        r = http.client.get("https://www.googleapis.com/youtube/v3/channels", params={"part": "id", "mine": "true"},
                            headers={"Authorization": f"Bearer {tok['access_token']}"})
    except httpx.HTTPError as e:
        raise AuthError(f"Network error: {type(e).__name__}") from e
    items = r.json().get("items") if r.status_code == 200 else None
    if not items:
        raise AuthError("This Google account has no YouTube channel (or it is a brand account you didn't select).")
    channel_id = items[0]["id"]
    youtube.sync_channel(channel_id)  # make sure the channel exists locally
    _store(channel_id, tok["access_token"], tok["refresh_token"], tok.get("expires_in", 3600), tok.get("scope", ""))
    return channel_id


def _store(channel_id: str, access: str, refresh: str, expires_in: int, scopes: str):
    exp = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat(timespec="seconds")
    with db.connect() as con:
        con.execute("INSERT OR REPLACE INTO oauth_tokens VALUES (?,?,?,?,?,?, 'ok')",
                    (channel_id, _enc(access), _enc(refresh), exp, scopes, youtube.now_iso()))


# ---- using tokens -----------------------------------------------------------
def status(channel_id: str) -> dict:
    with db.connect() as con:
        r = con.execute("SELECT status, created_at FROM oauth_tokens WHERE youtube_channel_id=?", (channel_id,)).fetchone()
    return {"configured": configured(), "connected": bool(r) and r["status"] == "ok",
            "reauth_required": bool(r) and r["status"] != "ok", "connected_at": r["created_at"] if r else None}


def access_token(channel_id: str) -> str:
    with db.connect() as con:
        r = con.execute("SELECT * FROM oauth_tokens WHERE youtube_channel_id=?", (channel_id,)).fetchone()
    if not r:
        raise AuthError("Channel is not connected.")
    if r["status"] != "ok":
        raise AuthError("Authorization expired. Reconnect your channel.")
    if datetime.fromisoformat(r["expires_at"]) > datetime.now(timezone.utc) + timedelta(seconds=60):
        return _dec(r["access_token_enc"])
    try:
        tok = _token_request({"grant_type": "refresh_token", "refresh_token": _dec(r["refresh_token_enc"])})
    except AuthError as e:
        if str(e) == "invalid_grant":  # revoked, or 7-day expiry while the OAuth app is in Testing mode
            with db.connect() as con:
                con.execute("UPDATE oauth_tokens SET status='reauth_required' WHERE youtube_channel_id=?", (channel_id,))
            raise AuthError("Authorization expired or was revoked. Reconnect your channel.") from e
        raise
    exp = (datetime.now(timezone.utc) + timedelta(seconds=tok.get("expires_in", 3600))).isoformat(timespec="seconds")
    with db.connect() as con:
        con.execute("UPDATE oauth_tokens SET access_token_enc=?, expires_at=? WHERE youtube_channel_id=?",
                    (_enc(tok["access_token"]), exp, channel_id))
    return tok["access_token"]


def disconnect(channel_id: str):
    """Revoke at Google, delete tokens, and delete the private analytics data we hold for this channel."""
    with db.connect() as con:
        r = con.execute("SELECT refresh_token_enc FROM oauth_tokens WHERE youtube_channel_id=?", (channel_id,)).fetchone()
    if r:
        try:
            http.client.post(REVOKE_URL, data={"token": _dec(r["refresh_token_enc"])})
        except (httpx.HTTPError, AuthError):
            pass  # local cleanup below still happens; user can also revoke at myaccount.google.com/permissions
    with db.connect() as con:
        con.execute("DELETE FROM oauth_tokens WHERE youtube_channel_id=?", (channel_id,))
        con.execute("DELETE FROM analytics_data WHERE youtube_channel_id=?", (channel_id,))
        for t in ("reach_jobs", "reach_reports", "reach_daily"):  # impressions/CTR are owner-private too
            con.execute(f"DELETE FROM {t} WHERE youtube_channel_id=?", (channel_id,))
