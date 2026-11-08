"""BrewPOS - Email OTP (One-Time Password) via Resend API.

Generates 6-digit OTP codes, stores hashed versions in PostgreSQL with expiry,
and sends them to users' email addresses using the Resend API.

Environment variables:
  RESEND_API_KEY=re_xxxxxxxxxxxxxxxx   (server-side only, never exposed to frontend)
  EMAIL_FROM=BrewPOS <noreply@yourdomain.com>
  OTP_EXPIRY_MINUTES=5
  OTP_MAX_ATTEMPTS=5
  OTP_RESEND_COOLDOWN_SECONDS=60
"""

import os
import re
import random
from datetime import datetime, timedelta

import resend
from werkzeug.security import generate_password_hash, check_password_hash

from db import get_cursor, execute, query_one

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "BrewPOS <noreply@brewpos.local>")
OTP_EXPIRY_MINUTES = int(os.environ.get("OTP_EXPIRY_MINUTES", "5"))
OTP_MAX_ATTEMPTS = int(os.environ.get("OTP_MAX_ATTEMPTS", "5"))
OTP_RESEND_COOLDOWN_SECONDS = int(os.environ.get("OTP_RESEND_COOLDOWN_SECONDS", "60"))

if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY


def generate_otp(length: int = 6) -> str:
    """Generate a numeric OTP code of the given length."""
    return "".join(str(random.randint(0, 9)) for _ in range(length))


def _hash_otp(code: str) -> str:
    """Hash an OTP code for secure storage."""
    return generate_password_hash(code)


def _verify_hash(code: str, hashed: str) -> bool:
    """Verify a plaintext OTP against its stored hash."""
    try:
        return check_password_hash(hashed, code)
    except Exception:
        return False


def _is_valid_email(email: str) -> bool:
    """Basic email format validation."""
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""))


def _now():
    return datetime.now()


def send_otp_email(to_email: str, otp_code: str, purpose: str = "login") -> bool:
    """Send an OTP code via the Resend API.

    Returns True if sent successfully, False otherwise.
    Never returns the OTP code to the caller (prevents frontend exposure).
    """
    if not RESEND_API_KEY:
        print(f"[OTP] Resend not configured. OTP for {to_email}: {otp_code}")
        return False

    purpose_labels = {
        "login": "Login",
        "register": "Account Registration",
        "password_reset": "Password Reset",
    }
    label = purpose_labels.get(purpose, "Verification")
    subject = f"BrewPOS - {label} OTP Code"

    html_body = f"""\
<!DOCTYPE html>
<html>
<body style="font-family: system-ui, -apple-system, sans-serif; color:#1c1917; line-height:1.5; margin:0; padding:20px;">
  <div style="max-width:600px; margin:0 auto; background:#ffffff; border-radius:12px; padding:32px; border:1px solid #e7e5e4;">
    <div style="font-size:24px; font-weight:800; color:#7c2d12; margin-bottom:8px;">BrewPOS</div>
    <div style="font-size:14px; color:#78716c; margin-bottom:24px;">{label}</div>
    <p style="font-size:16px; margin:0 0 16px 0;">Your OTP verification code is:</p>
    <div style="font-size:32px; font-weight:bold; letter-spacing:6px; color:#b45309; padding:20px; background:#fdf4e7; border-radius:10px; text-align:center; display:inline-block; margin:16px 0;">
      {otp_code}
    </div>
    <p style="font-size:14px; color:#78716c; margin:0 0 8px 0;">This code expires in {OTP_EXPIRY_MINUTES} minutes.</p>
    <p style="font-size:14px; color:#78716c; margin:0 0 24px 0;">Do not share this code with anyone.</p>
    <p style="font-size:13px; color:#a8a29e; margin:0;">If you did not request this, please ignore this email.</p>
  </div>
</body>
</html>
"""

    try:
        resend.Emails.send({
            "from": EMAIL_FROM,
            "to": to_email,
            "subject": subject,
            "html": html_body,
        })
        return True
    except Exception as e:
        print(f"[OTP] Failed to send email to {to_email}: {e}")
        return False


def store_otp(email: str, code: str, purpose: str = "login",
              owner_id: str | None = None) -> dict:
    """Store a hashed OTP code in the database with expiry and cooldown.

    Returns {'ok': bool, 'message': str, 'cooldown_seconds': int}.
    """
    try:
        now = _now()
        expires_at = now + timedelta(minutes=OTP_EXPIRY_MINUTES)
        cooldown_at = now + timedelta(seconds=OTP_RESEND_COOLDOWN_SECONDS)
        code_hash = _hash_otp(code)

        # Invalidate any previous unused OTPs for this email/purpose
        execute(
            "UPDATE otp_codes SET is_verified = TRUE WHERE email = %s AND purpose = %s AND is_verified = FALSE",
            (email, purpose),
        )

        # Remove old expired/verified rows for this email/purpose
        execute(
            "DELETE FROM otp_codes WHERE email = %s AND purpose = %s "
            "AND (is_verified = TRUE OR expires_at < %s)",
            (email, purpose, now),
        )

        execute(
            "INSERT INTO otp_codes (email, code_hash, purpose, expires_at, resend_cooldown_at, owner_id) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (email, code_hash, purpose, expires_at, cooldown_at, owner_id),
        )
        return {"ok": True, "message": "OTP stored.", "cooldown_seconds": OTP_RESEND_COOLDOWN_SECONDS}
    except Exception as e:
        print(f"[OTP] Failed to store OTP: {e}")
        return {"ok": False, "message": "Failed to store OTP.", "cooldown_seconds": 0}


def can_resend(email: str, purpose: str = "login") -> dict:
    """Check if a new OTP can be sent (respects 60s cooldown).

    Returns {'allowed': bool, 'remaining_seconds': int}.
    """
    try:
        row = query_one(
            "SELECT resend_cooldown_at FROM otp_codes "
            "WHERE email = %s AND purpose = %s AND is_verified = FALSE "
            "ORDER BY created_at DESC LIMIT 1",
            (email, purpose),
        )
        if not row:
            return {"allowed": True, "remaining_seconds": 0}
        cooldown_at = row["resend_cooldown_at"]
        if isinstance(cooldown_at, str):
            cooldown_at = datetime.fromisoformat(cooldown_at)
        remaining = int((cooldown_at - _now()).total_seconds())
        if remaining > 0:
            return {"allowed": False, "remaining_seconds": remaining}
        return {"allowed": True, "remaining_seconds": 0}
    except Exception as e:
        print(f"[OTP] Cooldown check error: {e}")
        return {"allowed": True, "remaining_seconds": 0}


def verify_otp(email: str, code: str, purpose: str = "login") -> dict:
    """Verify an OTP code against the hashed record.

    Returns {'success': bool, 'message': str, 'owner_id': str|None}.
    Enforces max 5 incorrect attempts, 5-minute expiry, and invalidation after success.
    """
    try:
        row = query_one(
            "SELECT id, code_hash, expires_at, is_verified, failed_attempts, owner_id FROM otp_codes "
            "WHERE email = %s AND purpose = %s AND is_verified = FALSE "
            "ORDER BY created_at DESC LIMIT 1",
            (email, purpose),
        )
        if not row:
            return {"success": False, "message": "OTP not found or already used.", "owner_id": None}

        if row["is_verified"]:
            return {"success": False, "message": "OTP already verified.", "owner_id": None}

        failed_attempts = int(row["failed_attempts"] or 0)
        if failed_attempts >= OTP_MAX_ATTEMPTS:
            return {"success": False, "message": "Maximum verification attempts reached. Request a new OTP.", "owner_id": None}

        expires_at = row["expires_at"]
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)

        if _now() > expires_at:
            execute("UPDATE otp_codes SET is_verified = TRUE WHERE id = %s", (row["id"],))
            return {"success": False, "message": "OTP has expired. Request a new one.", "owner_id": None}

        if not _verify_hash(code, row["code_hash"]):
            execute(
                "UPDATE otp_codes SET failed_attempts = failed_attempts + 1 WHERE id = %s",
                (row["id"],),
            )
            remaining = OTP_MAX_ATTEMPTS - (failed_attempts + 1)
            return {"success": False, "message": f"Invalid OTP code. {remaining} attempt(s) remaining.", "owner_id": None}

        # Success: mark OTP as verified (invalidated, prevents reuse)
        execute("UPDATE otp_codes SET is_verified = TRUE WHERE id = %s", (row["id"],))
        return {"success": True, "message": "OTP verified.", "owner_id": row["owner_id"]}
    except Exception as e:
        print(f"[OTP] Verification error: {e}")
        return {"success": False, "message": "OTP verification failed.", "owner_id": None}


def cleanup_expired_otps():
    """Mark all expired OTPs as verified (invalidate)."""
    try:
        execute(
            "UPDATE otp_codes SET is_verified = TRUE WHERE expires_at < %s AND is_verified = FALSE",
            (_now(),),
        )
    except Exception as e:
        print(f"[OTP] Cleanup error: {e}")
