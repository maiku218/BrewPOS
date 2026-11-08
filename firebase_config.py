"""BrewPOS - Firebase Admin SDK for Google Sign-In ID token verification.

Initializes the Firebase Admin SDK using service-account credentials and
provides verify_id_token() to validate Google ID tokens sent by the
client-side Firebase Web SDK.

Environment variables:
  FIREBASE_CREDENTIALS_JSON  (or FIREBASE_CREDENTIALS_JSON_BASE64)
  BREWPOS_FIREBASE_API_KEY, BREWPOS_FIREBASE_AUTH_DOMAIN, ... (public web config)
"""

import os
import json
import base64
from dotenv import load_dotenv

import firebase_admin
from firebase_admin import credentials, auth

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(BASE_DIR, "firebase_credentials.json")

# Public Firebase web config (used by the client-side Google Sign-In page).
FIREBASE_WEB_CONFIG = {
    "apiKey": os.environ.get("BREWPOS_FIREBASE_API_KEY", ""),
    "authDomain": os.environ.get("BREWPOS_FIREBASE_AUTH_DOMAIN", ""),
    "projectId": os.environ.get("BREWPOS_FIREBASE_PROJECT_ID", ""),
    "storageBucket": os.environ.get("BREWPOS_FIREBASE_STORAGE_BUCKET", ""),
    "messagingSenderId": os.environ.get("BREWPOS_FIREBASE_SENDER_ID", ""),
    "appId": os.environ.get("BREWPOS_FIREBASE_APP_ID", ""),
}


def _load_credentials():
    """Load service-account credentials from env var or JSON file."""
    env_b64 = os.environ.get("FIREBASE_CREDENTIALS_JSON_BASE64")
    env_json = os.environ.get("FIREBASE_CREDENTIALS_JSON")

    if env_b64:
        return credentials.Certificate(
            json.loads(base64.b64decode(env_b64).decode("utf-8"))
        )

    if env_json:
        cleaned = env_json.replace('\\n', '\n').replace('\\"', '"').strip()
        return credentials.Certificate(json.loads(cleaned))

    if not os.path.exists(CREDENTIALS_PATH):
        raise FileNotFoundError(
            "Firebase credentials not found. Set FIREBASE_CREDENTIALS_JSON "
            "or place firebase_credentials.json in the project root."
        )
    with open(CREDENTIALS_PATH, "r", encoding="utf-8") as fh:
        return credentials.Certificate(json.load(fh))


def init_firebase():
    """Initialize the Firebase Admin SDK once. Safe to call repeatedly."""
    if not firebase_admin._apps:
        cred = _load_credentials()
        firebase_admin.initialize_app(cred)


def verify_id_token(id_token: str):
    """Verify a Google ID token and return the decoded claims dict.

    Returns None if the token is invalid or Firebase is not configured.
    """
    try:
        init_firebase()
        return auth.verify_id_token(id_token, clock_skew_seconds=60)
    except Exception as exc:
        print(f"[Google Sign-In] ID token verification failed: {exc}")
        return None


def get_firebase_web_config():
    """Return the public Firebase web config for the client SDK."""
    return FIREBASE_WEB_CONFIG
