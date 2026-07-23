"""BrewPOS - Firebase configuration and Admin SDK initialization.

Loads the service-account credentials from the FIREBASE_CREDENTIALS_JSON
environment variable (for deployment) or from firebase_credentials.json
(for local development). Initializes the Firebase Admin SDK used by the
Flask backend to read/write Cloud Firestore and verify Google ID tokens.
"""

import os
import json
import uuid
from io import BytesIO
from datetime import datetime
from dotenv import load_dotenv

import firebase_admin
from firebase_admin import credentials, auth, firestore, storage

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(BASE_DIR, "firebase_credentials.json")

# Public Firebase web config (used by the client-side Google Sign-In page).
# Fill these after registering a web app in the Firebase console.
FIREBASE_WEB_CONFIG = {
    "apiKey": os.environ.get("BREWPOS_FIREBASE_API_KEY", ""),
    "authDomain": os.environ.get("BREWPOS_FIREBASE_AUTH_DOMAIN", ""),
    "projectId": os.environ.get("BREWPOS_FIREBASE_PROJECT_ID", ""),
    "storageBucket": os.environ.get("BREWPOS_FIREBASE_STORAGE_BUCKET", ""),
    "messagingSenderId": os.environ.get("BREWPOS_FIREBASE_SENDER_ID", ""),
    "appId": os.environ.get("BREWPOS_FIREBASE_APP_ID", ""),
}

_db = None


def _load_credentials():
    """Load service-account credentials from env var or JSON file."""
    env_json = os.environ.get("FIREBASE_CREDENTIALS_JSON")
    if env_json:
        return credentials.Certificate(json.loads(env_json))
    if not os.path.exists(CREDENTIALS_PATH):
        raise FileNotFoundError(
            "Firebase credentials not found. Set FIREBASE_CREDENTIALS_JSON "
            "environment variable or place firebase_credentials.json in the "
            "project root."
        )
    with open(CREDENTIALS_PATH, "r", encoding="utf-8") as fh:
        return credentials.Certificate(json.load(fh))


def init_firebase():
    """Initialize the Firebase Admin SDK once."""
    global _db
    if firebase_admin._apps:
        if _db is None:
            _db = firestore.client()
        return _db

    cred = _load_credentials()
    firebase_admin.initialize_app(cred, {
        'storageBucket': os.environ.get(
            'BREWPOS_STORAGE_BUCKET',
            FIREBASE_WEB_CONFIG.get('storageBucket', '')
        )
    })
    _db = firestore.client()
    return _db


def get_db():
    """Return the Firestore client, initializing on first use."""
    if _db is None:
        return init_firebase()
    return _db


def verify_id_token(id_token: str):
    """Verify a Google ID token and return the decoded claims."""
    return auth.verify_id_token(id_token, clock_skew_seconds=60)


PRODUCT_IMAGES_FOLDER = 'product_images/'


def validate_image_file(file_obj, max_size_mb: int = 5) -> tuple:
    """Validate image file type and size. Returns (is_valid, error_message)."""
    if not file_obj or not hasattr(file_obj, 'content_type') or not file_obj.filename:
        return False, 'No file selected.'
    allowed = {'image/jpeg', 'image/png', 'image/webp', 'image/jpg'}
    if file_obj.content_type not in allowed:
        return False, 'Invalid file type. Only JPG, PNG, and WEBP are allowed.'
    file_obj.seek(0, 2)
    size = file_obj.tell()
    file_obj.seek(0)
    if size > max_size_mb * 1024 * 1024:
        return False, f'File size exceeds {max_size_mb}MB limit.'
    if size == 0:
        return False, 'File is empty.'
    return True, ''


def compress_image(file_obj, max_size_mb: int = 1, max_dim: int = 1200) -> BytesIO:
    """Compress image if larger than max_size_mb or max_dim. Returns BytesIO."""
    if not HAS_PIL:
        return file_obj
    try:
        img = Image.open(file_obj)
        if img.mode in ('RGBA', 'LA', 'P'):
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        buf = BytesIO()
        fmt = 'JPEG'
        save_kwargs = {'quality': 85, 'optimize': True}
        if file_obj.content_type == 'image/png':
            fmt = 'PNG'
            save_kwargs = {'optimize': True}
        img.save(buf, format=fmt, **save_kwargs)
        buf.seek(0)
        return buf
    except Exception:
        file_obj.seek(0)
        return file_obj


def build_image_path(owner_uid: str, product_id: str, filename_hint: str = '') -> str:
    """Build a unique product image path: workspaces/{ownerUid}/products/{productId}.jpg"""
    ext = os.path.splitext(filename_hint)[1].lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.webp'):
        ext = '.jpg'
    return f"workspaces/{owner_uid}/products/{product_id}{ext}"


def upload_image_to_storage(file_obj, destination_path: str, content_type: str = 'image/jpeg') -> str:
    """Upload a file-like object to Firebase Storage and return its public URL."""
    bucket_name = os.environ.get(
        'BREWPOS_STORAGE_BUCKET',
        FIREBASE_WEB_CONFIG.get('storageBucket', '')
    )
    bucket = storage.bucket(bucket_name)
    blob = bucket.blob(destination_path)
    blob.upload_from_file(file_obj, content_type=content_type)
    blob.make_public()
    return blob.public_url


def delete_image_from_storage(path: str) -> bool:
    """Delete an image from Firebase Storage by its path."""
    try:
        bucket_name = os.environ.get(
            'BREWPOS_STORAGE_BUCKET',
            FIREBASE_WEB_CONFIG.get('storageBucket', '')
        )
        bucket = storage.bucket(bucket_name)
        blob = bucket.blob(path)
        if blob.exists():
            blob.delete()
        return True
    except Exception:
        return False


def build_image_path_legacy(filename_hint: str = '') -> str:
    """Build a unique product image path: product_images/<filename>."""
    ext = os.path.splitext(filename_hint)[1].lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.webp'):
        ext = '.jpg'
    name = f"{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:8]}{ext}"
    return f"{PRODUCT_IMAGES_FOLDER}{name}"


# Constants
LOW_STOCK_THRESHOLD = 10
EXPIRING_DAYS = 7
DEFAULT_CATEGORIES = [
    "Coffee", "Non-Coffee", "Milk Tea", "Tea",
    "Pastry", "Cakes", "Sandwich", "Snacks",
    "Add-ons", "Others",
]
DEFAULT_ADDONS = [
    "Extra Espresso Shot", "Whipped Cream", "Cheese Foam",
    "Caramel Syrup", "Chocolate Syrup",
]
CUP_SMALL_ADD = 0
CUP_MEDIUM_ADD = 10
CUP_LARGE_ADD = 20
DEFAULT_STORE_SETTINGS = {
    "storeName": "BrewPOS",
    "subtitle": "Point of Sale System",
    "address": "",
    "contact": "",
    "footer": "Thank you for your purchase!\nPlease come again.",
}


if __name__ == "__main__":
    init_firebase()
    print("Firebase initialized successfully.")
