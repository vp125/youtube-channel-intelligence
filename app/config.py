import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Full reports need careful reasoning over many numbers; quick questions are cheap. Two knobs, two defaults.
ANTHROPIC_MODEL_REPORT = os.getenv("ANTHROPIC_MODEL_REPORT", "claude-sonnet-5-5")
ANTHROPIC_MODEL_QUESTION = os.getenv("ANTHROPIC_MODEL_QUESTION", "claude-haiku-4-5-20251001")
DB_PATH = Path(os.getenv("DB_PATH", ROOT / "data" / "youtube.db"))
# OAuth client for YouTube Analytics (V2). Create a "Web application" client in Google Cloud Console.
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "http://127.0.0.1:8000/auth/google/callback")
# Fernet key used to encrypt stored OAuth tokens. If unset, one is generated into data/secret.key.
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "")
# Skip the API if stored data is newer than this many hours (local cache, design §11.1)
CACHE_HOURS = float(os.getenv("CACHE_HOURS", "6"))
