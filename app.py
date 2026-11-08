"""BrewPOS - Coffee Shop POS and Inventory System (Flask + PostgreSQL).

Pure coffee-focused POS. Authentication uses local email/password accounts
(securely hashed). All data lives in PostgreSQL.
"""

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, send_file, send_from_directory
)
from datetime import datetime, timedelta
from typing import Optional
import os
import json
import uuid
from io import BytesIO
from functools import wraps

from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from db import (
    init_db, get_cursor, query_all, query_one, execute,
    LOW_STOCK_THRESHOLD, EXPIRING_DAYS, DEFAULT_CATEGORIES,
    DEFAULT_ADDONS, DEFAULT_STORE_SETTINGS,
)
from models import Product, StockMovement, User, Admin, Cashier
from services import AuthService, ProductRepository, SalesService
from otp import (
    generate_otp, send_otp_email, store_otp, verify_otp,
    cleanup_expired_otps, can_resend,
)
from firebase_config import verify_id_token, get_firebase_web_config


def _to_naive(dt):
    if dt is None:
        return datetime.now()
    if isinstance(dt, datetime):
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    return datetime.now()


def _current_owner_id():
    if session.get('role') == 'admin':
        return session.get('admin_id')
    if session.get('role') == 'cashier':
        cid = session.get('cashier_id')
        if cid:
            user = auth_service.get_user_by_id(cid)
            if user:
                return user.get('owner_id') or user.get('id')
    return None


# ---------------------------------------------------------------
# Activity logging helpers
# ---------------------------------------------------------------

def _log_admin_activity(action: str, details: str = ''):
    admin_id = session.get('admin_id')
    execute(
        "INSERT INTO admin_activity (admin_id, action, timestamp, details, owner_id) "
        "VALUES (%s, %s, %s, %s, %s)",
        (admin_id, action, datetime.now(), details, admin_id),
    )


def _log_cashier_activity(action: str, details: str = ''):
    cashier_id = session.get('cashier_id')
    owner_id = _current_owner_id()
    execute(
        "INSERT INTO cashier_activity (cashier_id, action, timestamp, owner_id) "
        "VALUES (%s, %s, %s, %s)",
        (cashier_id, action, datetime.now(), owner_id),
    )


# ---------------------------------------------------------------
# Store settings
# ---------------------------------------------------------------

def get_store_settings(owner_id: Optional[str] = None):
    doc_id = f'config_{owner_id}' if owner_id else 'config'
    row = query_one("SELECT * FROM store_settings WHERE id = %s", (doc_id,))
    if row:
        return {
            'storeName': row['store_name'],
            'subtitle': row['subtitle'],
            'address': row['address'],
            'contact': row['contact'],
            'footer': row['footer'],
        }
    return dict(DEFAULT_STORE_SETTINGS)


# ---------------------------------------------------------------
# App setup
# ---------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("BREWPOS_SECRET_KEY", "brewpos_secret_key")
app.config['SESSION_PERMANENT'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = 1800  # 30 minutes
app.config['SESSION_COOKIE_NAME'] = 'brewpos_session'

LOW_STOCK_THRESHOLD = LOW_STOCK_THRESHOLD
EXPIRING_DAYS = EXPIRING_DAYS
DEFAULT_CATEGORIES = DEFAULT_CATEGORIES

try:
    init_db()
    print("[BREWPOS] PostgreSQL schema initialized.")
except Exception as e:
    print(f"[BREWPOS] WARNING: Could not initialize PostgreSQL schema: {e}")
    print("[BREWPOS] Set up PostgreSQL and configure .env (see README.md), then restart.")

auth_service = AuthService()
product_repository = ProductRepository()
sales_service = SalesService()

CUP_ADD = {'S': 0, 'M': 10, 'L': 20}

# ---------------------------------------------------------------
# Local product image upload (replaces Firebase Storage)
# ---------------------------------------------------------------
UPLOAD_FOLDER = os.path.join(app.static_folder, 'images', 'products')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}
MAX_IMAGE_SIZE_MB = 5
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def allowed_file(filename):
    return '.' in filename and \
        filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def clean_input(value):
    return value.strip() if value else ""


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') != 'admin' or not session.get('admin_id'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def cashier_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        is_api = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        if session.get('role') != 'cashier' or not session.get('cashier_id'):
            if is_api:
                return jsonify({'success': False, 'message': 'Session expired. Please login again.'}), 401
            return redirect(url_for('login'))
        user = auth_service.get_user_by_id(session.get('cashier_id'))
        if not user or (user.get('status', 'active') or 'active') != 'active':
            session.clear()
            if is_api:
                return jsonify({'success': False, 'message': 'Cashier account is inactive.'}), 401
            flash("Cashier account is inactive. Contact the administrator.", "error")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


@app.route('/api/upload_image', methods=['POST'])
@admin_required
def api_upload_image():
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': 'No file part.'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'message': 'No selected file.'}), 400
    if not allowed_file(file.filename):
        return jsonify({'success': False, 'message': 'Invalid file type. Allowed: PNG, JPG, JPEG, WEBP.'}), 400

    filename = secure_filename(file.filename)
    # Avoid collisions by prefixing with a uuid
    name, ext = os.path.splitext(filename)
    filename = f"{name}_{uuid.uuid4().hex[:8]}{ext}"
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(file_path)

    image_url = url_for('product_image', filename=filename, _external=True)
    return jsonify({'success': True, 'image_url': image_url, 'filename': filename})


# Inject globals into templates
@app.context_processor
def inject_globals():
    return dict(
        LOW_STOCK_THRESHOLD=LOW_STOCK_THRESHOLD,
        DEFAULT_CATEGORIES=DEFAULT_CATEGORIES,
        DEFAULT_ADDONS=DEFAULT_ADDONS,
        now=datetime.now,
    )


# ---------------------------------------------------------------
# Authentication (local email/password, replaces Firebase Auth)
# ---------------------------------------------------------------
@app.route('/')
def index():
    if session.get('role') == 'admin':
        return redirect(url_for('admin_dashboard'))
    if session.get('role') == 'cashier':
        return redirect(url_for('cashier_dashboard'))
    return redirect(url_for('login'))


@app.route('/login')
def login():
    if session.get('role') in ('admin', 'cashier'):
        return redirect(url_for('index'))
    return render_template('login.html')


@app.route('/register')
def register():
    if session.get('role') in ('admin', 'cashier'):
        return redirect(url_for('index'))
    return redirect(url_for('login'))


@app.route('/api/auth/login', methods=['POST'])
def api_auth_login():
    data = request.get_json(silent=True) or {}
    email = clean_input(data.get('email', ''))
    password = data.get('password', '')
    if not email or not password:
        return jsonify({'success': False, 'message': 'Email and password are required.'}), 400

    user = auth_service.authenticate(email, password)
    if not user:
        return jsonify({'success': False, 'message': 'Invalid email or password.'}), 401

    session.clear()
    session.update(auth_service.get_session_data(user))
    session['user_email'] = user.get('email')
    session['user_display_name'] = user.get('display_name')
    session.permanent = True

    if user.get('role') == 'admin':
        _log_admin_activity('Login', f'Admin {user.get("email")} logged in')
    else:
        _log_cashier_activity('Login', f'Cashier {user.get("email")} logged in')

    redirect_url = url_for('admin_dashboard') if user.get('role') == 'admin' else url_for('cashier_dashboard')
    return jsonify({'success': True, 'redirect': redirect_url})


@app.route('/api/auth/register', methods=['POST'])
def api_auth_register():
    data = request.get_json(silent=True) or {}
    email = clean_input(data.get('email', ''))
    password = data.get('password', '')
    display_name = clean_input(data.get('display_name', ''))
    if not email or not password or not display_name:
        return jsonify({'success': False, 'message': 'All fields are required.'}), 400

    row = query_one("SELECT COUNT(*) AS cnt FROM users")
    existing_count = int(row['cnt']) if row else 0
    role = 'admin' if existing_count == 0 else 'cashier'

    user = auth_service.create_user(email, password, display_name, role=role)
    if not user:
        return jsonify({'success': False, 'message': 'Email already registered.'}), 400

    if role == 'admin':
        execute("UPDATE users SET owner_id = id WHERE id = %s", (user['id'],))
    else:
        admin = query_one("SELECT id FROM users WHERE role = 'admin' LIMIT 1")
        if admin:
            execute("UPDATE users SET owner_id = %s WHERE id = %s", (admin['id'], user['id']))

    redirect_url = url_for('admin_dashboard') if role == 'admin' else url_for('cashier_dashboard')
    return jsonify({'success': True, 'redirect': redirect_url})


def _google_get_or_create_user(claims: dict) -> dict:
    """Find an existing user by Google email, or create one from the ID token claims.

    First Google account to sign in becomes the admin; subsequent accounts are cashiers.
    Session is set by the caller so every Google account stays logged in.
    """
    email = claims.get('email')
    google_id = claims.get('sub')
    display_name = claims.get('name') or claims.get('email') or 'Google User'
    if not email or not google_id:
        return None

    user = auth_service.get_user_by_email(email)
    if user:
        return user

    row_count = query_one("SELECT COUNT(*) AS cnt FROM users")
    existing_count = int(row_count['cnt']) if row_count else 0
    role = 'admin' if existing_count == 0 else 'cashier'

    user_id = str(uuid.uuid4())
    execute(
        "INSERT INTO users (id, email, password_hash, display_name, role, status, "
        "provider, google_id, owner_id, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            user_id, email, None, display_name, role, 'active',
            'google', google_id, None, now(), now(),
        ),
    )
    user = auth_service.get_user_by_id(user_id)
    if not user:
        return None

    if role == 'admin':
        execute("UPDATE users SET owner_id = id WHERE id = %s", (user_id,))
    else:
        admin = query_one("SELECT id FROM users WHERE role = 'admin' LIMIT 1")
        if admin:
            execute("UPDATE users SET owner_id = %s WHERE id = %s", (admin['id'], user_id))

    return auth_service.get_user_by_id(user_id)


@app.route('/api/auth/google_login', methods=['POST'])
def api_auth_google_login():
    """Sign in with a Google ID token. Verifies the token, finds/creates the user,
    and establishes a session so the Google account stays logged in."""
    data = request.get_json(silent=True) or {}
    id_token = data.get('id_token', '')
    if not id_token:
        return jsonify({'success': False, 'message': 'Google ID token is required.'}), 400

    claims = verify_id_token(id_token)
    if not claims:
        return jsonify({'success': False, 'message': 'Invalid Google ID token.'}), 401

    email = claims.get('email')
    if not email:
        return jsonify({'success': False, 'message': 'Google account has no email.'}), 400

    user = _google_get_or_create_user(claims)
    if not user:
        return jsonify({'success': False, 'message': 'Failed to create Google account.'}), 500

    session.clear()
    session.update(auth_service.get_session_data(user))
    session['user_email'] = user.get('email')
    session['user_display_name'] = user.get('display_name')
    session['auth_provider'] = 'google'
    session.permanent = True

    if user.get('role') == 'admin':
        _log_admin_activity('Google Sign-In', f'Admin {user.get("email")} signed in via Google')
    else:
        _log_cashier_activity('Google Sign-In', f'Cashier {user.get("email")} signed in via Google')

    redirect_url = url_for('admin_dashboard') if user.get('role') == 'admin' else url_for('cashier_dashboard')
    return jsonify({'success': True, 'redirect': redirect_url, 'role': user.get('role'), 'email': email})


@app.route('/api/auth/firebase-config', methods=['GET'])
def api_auth_firebase_config():
    """Return the public Firebase web config for the client-side Google Sign-In SDK."""
    return jsonify(get_firebase_web_config())


@app.route('/api/auth/send_otp', methods=['POST'])
def api_auth_send_otp():
    """Send an OTP code to the user's email for login or registration via Resend."""
    data = request.get_json(silent=True) or {}
    email = clean_input(data.get('email', ''))
    purpose = data.get('purpose', 'login')
    if purpose not in ('login', 'register', 'password_reset'):
        purpose = 'login'
    if not email:
        return jsonify({'success': False, 'message': 'Email is required.'}), 400
    if not _is_valid_email_app(email):
        return jsonify({'success': False, 'message': 'Please enter a valid email address.'}), 400

    cleanup_expired_otps()

    # Enforce 60-second resend cooldown
    cooldown = can_resend(email, purpose)
    if not cooldown['allowed']:
        return jsonify({
            'success': False,
            'message': f'Please wait {cooldown["remaining_seconds"]}s before requesting another OTP.',
            'cooldown_seconds': cooldown['remaining_seconds'],
        }), 429

    # For login, the account must already exist
    owner_id = None
    if purpose == 'login':
        user = auth_service.get_user_by_email(email)
        if not user:
            return jsonify({'success': False, 'message': 'No account found with this email.'}), 404
        owner_id = user['id']
    elif purpose == 'register':
        # Cannot register an email that already has an account
        existing = auth_service.get_user_by_email(email)
        if existing:
            return jsonify({'success': False, 'message': 'An account already exists for this email.'}), 409

    otp_code = generate_otp()
    result = store_otp(email, otp_code, purpose, owner_id=owner_id)
    if not result['ok']:
        return jsonify({'success': False, 'message': result['message']}), 500

    sent = send_otp_email(email, otp_code, purpose)
    if not sent:
        # OTP stored but email delivery failed; surface the error, never leak the OTP
        return jsonify({
            'success': False,
            'message': 'OTP generated but email delivery failed. Please try again.',
        }), 502

    return jsonify({'success': True, 'message': f'OTP sent to {email}'})


def _is_valid_email_app(email: str) -> bool:
    import re
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""))


@app.route('/api/auth/verify_otp', methods=['POST'])
def api_auth_verify_otp():
    """Verify an OTP code and log the user in (login purpose) or confirm email (register purpose)."""
    data = request.get_json(silent=True) or {}
    email = clean_input(data.get('email', ''))
    code = data.get('code', '')
    purpose = data.get('purpose', 'login')
    if not email or not code:
        return jsonify({'success': False, 'message': 'Email and OTP code are required.'}), 400

    result = verify_otp(email, code, purpose)
    if not result['success']:
        return jsonify({'success': False, 'message': result['message']}), 400

    if purpose == 'login':
        user = auth_service.get_user_by_email(email)
        if not user:
            return jsonify({'success': False, 'message': 'User not found.'}), 404

        session.clear()
        session.update(auth_service.get_session_data(user))
        session['user_email'] = user.get('email')
        session['user_display_name'] = user.get('display_name')
        session.permanent = True

        if user.get('role') == 'admin':
            _log_admin_activity('OTP Login', f'Admin {user.get("email")} logged in via OTP')
        else:
            _log_cashier_activity('OTP Login', f'Cashier {user.get("email")} logged in via OTP')

        redirect_url = url_for('admin_dashboard') if user.get('role') == 'admin' else url_for('cashier_dashboard')
        return jsonify({'success': True, 'redirect': redirect_url, 'role': user.get('role')})

    # register / password_reset: OTP verified, email confirmed
    return jsonify({'success': True, 'message': 'OTP verified. Email confirmed.', 'email': email})


@app.route('/api/auth/register_complete', methods=['POST'])
def api_auth_register_complete():
    """Complete account registration after OTP email verification.

    Requires a verified OTP for the email with purpose='register'.
    Creates the user account only after email ownership is proven.
    """
    data = request.get_json(silent=True) or {}
    email = clean_input(data.get('email', ''))
    password = data.get('password', '')
    display_name = clean_input(data.get('display_name', ''))
    if not email or not password or not display_name:
        return jsonify({'success': False, 'message': 'All fields are required.'}), 400
    if not _is_valid_email_app(email):
        return jsonify({'success': False, 'message': 'Please enter a valid email address.'}), 400

    # Ensure a verified OTP exists for this email (purpose=register)
    row = query_one(
        "SELECT id FROM otp_codes WHERE email = %s AND purpose = 'register' "
        "AND is_verified = TRUE ORDER BY created_at DESC LIMIT 1",
        (email,),
    )
    if not row:
        return jsonify({'success': False, 'message': 'Email not verified. Please complete OTP verification first.'}), 403

    # Invalidate the used registration OTP
    execute("UPDATE otp_codes SET is_verified = TRUE WHERE email = %s AND purpose = 'register'", (email,))

    existing = auth_service.get_user_by_email(email)
    if existing:
        return jsonify({'success': False, 'message': 'An account already exists for this email.'}), 409

    row_count = query_one("SELECT COUNT(*) AS cnt FROM users")
    existing_count = int(row_count['cnt']) if row_count else 0
    role = 'admin' if existing_count == 0 else 'cashier'

    user = auth_service.create_user(email, password, display_name, role=role)
    if not user:
        return jsonify({'success': False, 'message': 'Email already registered.'}), 400

    if role == 'admin':
        execute("UPDATE users SET owner_id = id WHERE id = %s", (user['id'],))
    else:
        admin = query_one("SELECT id FROM users WHERE role = 'admin' LIMIT 1")
        if admin:
            execute("UPDATE users SET owner_id = %s WHERE id = %s", (admin['id'], user['id']))

    redirect_url = url_for('admin_dashboard') if role == 'admin' else url_for('cashier_dashboard')
    return jsonify({'success': True, 'redirect': redirect_url, 'role': role})


@app.route('/select_role')
def select_role():
    if session.get('role') == 'admin' and session.get('admin_id'):
        return redirect(url_for('admin_dashboard'))
    if session.get('role') == 'cashier' and session.get('cashier_id'):
        return redirect(url_for('cashier_dashboard'))
    return redirect(url_for('login'))


# ---------------------------------------------------------------
# Admin Dashboard
# ---------------------------------------------------------------
@app.route('/admin')
@admin_required
def admin_dashboard():
    owner_id = _current_owner_id()
    cashier_rows = query_all(
        "SELECT id, email, username, display_name, status, created_at FROM users "
        "WHERE role = 'cashier' AND owner_id = %s ORDER BY display_name",
        (owner_id,),
    )
    cashiers = [dict(r) for r in cashier_rows]

    activity_logs = []
    logs_raw = query_all(
        "SELECT * FROM cashier_activity WHERE owner_id = %s ORDER BY timestamp DESC LIMIT 10",
        (session.get('admin_id'),),
    )
    for a in logs_raw:
        activity_logs.append(dict(a))

    owner_id = _current_owner_id()
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)

    return render_template('admin_dashboard.html',
                           cashiers=cashiers,
                           activity_logs=activity_logs,
                           low_stock_count=low_stock_count,
                           expiring_count=expiring_count,
                           active_main='admin', active_sub='dashboard')


@app.route('/admin/cashier_logs')
@admin_required
def cashier_logs():
    logs_raw = query_all(
        "SELECT * FROM cashier_activity WHERE owner_id = %s ORDER BY timestamp DESC",
        (session.get('admin_id'),),
    )
    logs = [dict(a) for a in logs_raw]
    owner_id = _current_owner_id()
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
    return render_template('admin_cashier_logs.html', activity_logs=logs,
                           low_stock_count=low_stock_count, expiring_count=expiring_count,
                           active_main='dashboard', active_sub='cashier_logs')


@app.route('/admin/activity_logs')
@admin_required
def admin_activity_logs():
    logs_raw = query_all(
        "SELECT * FROM admin_activity WHERE owner_id = %s ORDER BY timestamp DESC LIMIT 50",
        (session.get('admin_id'),),
    )
    logs = [dict(a) for a in logs_raw]
    owner_id = _current_owner_id()
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
    return render_template('admin_activity_logs.html', admin_logs=logs,
                           low_stock_count=low_stock_count, expiring_count=expiring_count,
                           active_main='dashboard', active_sub='activity_logs')


# ---------------------------------------------------------------
# Rankings (top-selling coffees)
# ---------------------------------------------------------------
@app.route('/rankings')
@admin_required
def rankings():
    return redirect(url_for('sales_dashboard'))


# ---------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------
@app.route('/all_products')
@admin_required
def all_products():
    owner_id = _current_owner_id()
    products = []
    for p in product_repository.find_all(owner_id=owner_id):
        d = {
            'id': p.id, 'name': p.name, 'barcode': p.barcode, 'category': p.category,
            'description': p.description, 'imageUrl': p.image_url, 'image': p.image_url,
            'imagePath': p.image_path, 'status': p.status, 'pricingType': p.pricing_type,
            'price': p.price, 'variants': p.variants, 'addons': p.addons,
            'stock': p.stock_quantity, 'stockMinimum': p.stock_minimum,
            'expirationDate': str(p.expiration_date) if p.expiration_date else None,
            'createdAt': p.created_at, 'updatedAt': p.updated_at, 'ownerId': p.owner_id,
        }
        products.append(d)
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
    return render_template('all_products.html', products=products,
                           low_stock_count=low_stock_count, expiring_count=expiring_count,
                           active_main='catalog', active_sub='all_products')


@app.route('/add_product', methods=['GET', 'POST'])
@admin_required
def add_product():
    owner_id = _current_owner_id()
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
    categories = DEFAULT_CATEGORIES

    if request.method == 'POST':
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.form.get('ajax') == 'true'
        name = clean_input(request.form.get('product_name'))
        category = clean_input(request.form.get('category'))
        description = clean_input(request.form.get('description'))
        status = clean_input(request.form.get('status')) or 'Available'
        pricing_type = clean_input(request.form.get('pricing_type')) or 'single'
        barcode = clean_input(request.form.get('barcode'))

        errors = []
        if not name:
            errors.append('Product name is required')
        if not category:
            errors.append('Category is required')

        stock_qty = 0
        stock_min = 0
        try:
            stock_qty = int(request.form.get('stock_quantity', 0))
            stock_min = int(request.form.get('stock_minimum', 0))
        except (ValueError, TypeError):
            errors.append('Invalid stock values')

        variants = []
        price = 0
        if pricing_type == 'multiple':
            variant_names = request.form.getlist('variant_name[]')
            variant_prices = request.form.getlist('variant_price[]')
            for vname, vprice in zip(variant_names, variant_prices):
                vname = clean_input(vname)
                vprice_str = clean_input(vprice)
                if not vname:
                    continue
                try:
                    vprice = float(vprice_str)
                except (ValueError, TypeError):
                    errors.append(f'Invalid price for variant "{vname}"')
                    continue
                if vprice < 0:
                    errors.append(f'Price cannot be negative for variant "{vname}"')
                    continue
                variants.append({'name': vname, 'price': vprice})
            if not variants:
                errors.append('At least one variant with a valid price is required')
        else:
            price_str = clean_input(request.form.get('price'))
            if not price_str:
                errors.append('Price is required')
            else:
                try:
                    price = float(price_str)
                except (ValueError, TypeError):
                    errors.append('Invalid price')
                    price = 0
                if price < 0:
                    errors.append('Price cannot be negative')

        if stock_qty < 0:
            errors.append('Stock quantity cannot be negative')
        if stock_min < 0:
            errors.append('Minimum stock cannot be negative')

        selected_addons = []
        addon_names = request.form.getlist('addons')
        addon_prices = request.form.getlist('addon_price[]')
        for idx, addon_name in enumerate(addon_names):
            addon_name = clean_input(addon_name)
            if not addon_name:
                continue
            price = 0
            if idx < len(addon_prices):
                try:
                    price = float(addon_prices[idx])
                except (ValueError, TypeError):
                    price = 0
            selected_addons.append({'name': addon_name, 'price': price})
        if errors:
            msg = 'Error: ' + ', '.join(errors)
            if is_ajax:
                return jsonify({'success': False, 'message': msg})
            flash(msg, 'error')
            return render_template('add_product.html', categories=categories,
                                   low_stock_count=low_stock_count, expiring_count=expiring_count,
                                   active_main='catalog', active_sub='add_product')

        # Duplicate detection: same name within same category and owner
        existing = None
        owner_id = session.get('admin_id')
        rows = query_all(
            "SELECT id FROM products WHERE name = %s AND owner_id = %s LIMIT 1",
            (name, owner_id),
        )
        if rows:
            existing = rows[0]['id']

        try:
            now = datetime.now()
            if existing:
                product_id = existing
                product = product_repository.find_by_id(product_id, owner_id=owner_id)
                new_stock = product.stock_quantity + stock_qty
                product_repository.update(product_id, {
                    'stock_quantity': new_stock,
                    'stock_minimum': stock_min,
                    'updatedAt': now,
                })
                message = f"Stock updated! Added {stock_qty} units. Total: {new_stock}"
                action = 'Stock Update'
            else:
                # Image handling: uploaded file takes precedence over image_url text
                image_url = clean_input(request.form.get('image_url', ''))
                uploaded_file = request.files.get('product_image')
                if uploaded_file and uploaded_file.filename and allowed_file(uploaded_file.filename):
                    filename = secure_filename(uploaded_file.filename)
                    name, ext = os.path.splitext(filename)
                    filename = f"{name}_{uuid.uuid4().hex[:8]}{ext}"
                    file_path = os.path.join(UPLOAD_FOLDER, filename)
                    uploaded_file.save(file_path)
                    image_url = url_for('product_image', filename=filename, _external=True)
                elif uploaded_file and uploaded_file.filename:
                    # invalid file type
                    pass  # fall back to image_url text

                product_data = {
                    'name': name,
                    'barcode': barcode,
                    'category': category,
                    'description': description,
                    'imageUrl': image_url,
                    'status': status,
                    'pricingType': pricing_type,
                    'variants': variants,
                    'addons': selected_addons,
                    'stock_quantity': stock_qty,
                    'stock_minimum': stock_min,
                    'createdAt': now,
                    'updatedAt': now,
                    'ownerId': session.get('admin_id'),
                }
                if pricing_type == 'single':
                    product_data['price'] = price
                product_id = product_repository.save_from_dict(product_data)
                message = "Product added successfully."
                action = 'Add New Product'

                execute(
                    "INSERT INTO stock_movements (id, product_id, movement_type, quantity, reason, timestamp, owner_id, receipt_no) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (str(uuid.uuid4()), product_id, 'IN', stock_qty, 'Stock Addition',
                     now, session.get('admin_id'), None),
                )

            _log_admin_activity(action, f'{name} - {stock_qty} units added')

            if is_ajax:
                return jsonify({'success': True, 'message': message})
            flash(message, 'success')
            return redirect(url_for('all_products'))
        except Exception as e:
            msg = f'Error: {e}'
            if is_ajax:
                return jsonify({'success': False, 'message': msg})
            flash(msg, 'error')
            return render_template('add_product.html', categories=categories,
                                   low_stock_count=low_stock_count, expiring_count=expiring_count,
                                   active_main='catalog', active_sub='add_product')

    return render_template('add_product.html', categories=categories,
                           low_stock_count=low_stock_count, expiring_count=expiring_count,
                           active_main='catalog', active_sub='add_product')


@app.route('/delete_product/<product_id>', methods=['GET', 'POST'])
@admin_required
def delete_product(product_id):
    owner_id = _current_owner_id()
    if request.method == 'GET':
        product = product_repository.find_by_id(product_id, owner_id=owner_id)
        if not product:
            flash("Product not found", "error")
            return redirect(url_for('all_products'))
        p = {
            'id': product.id, 'name': product.name, 'barcode': product.barcode,
            'category': product.category, 'description': product.description,
            'imageUrl': product.image_url, 'image': product.image_url,
            'imagePath': product.image_path, 'status': product.status,
            'pricingType': product.pricing_type, 'price': product.price,
            'variants': product.variants, 'addons': product.addons,
            'stock': product.stock_quantity, 'stockMinimum': product.stock_minimum,
            'expirationDate': str(product.expiration_date) if product.expiration_date else None,
        }
        low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
        expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
        return render_template('confirm_delete_product.html', product=p,
                               low_stock_count=low_stock_count, expiring_count=expiring_count,
                               active_main='catalog', active_sub='all_products')

    # POST delete
    product = product_repository.find_by_id(product_id, owner_id=owner_id)
    if product:
        name = product.name
        product_repository.delete(product_id)
        _log_admin_activity('Delete Product', f'Deleted product: {name}')
    flash("Product deleted successfully", "success")
    return redirect(url_for('all_products'))


@app.route('/edit_product/<product_id>', methods=['GET', 'POST'])
@admin_required
def edit_product(product_id):
    owner_id = _current_owner_id()
    if request.method == 'POST':
        product = product_repository.find_by_id(product_id, owner_id=owner_id)
        if not product:
            flash("Product not found", "error")
            return redirect(url_for('all_products'))
        name = clean_input(request.form.get('product_name'))
        barcode = clean_input(request.form.get('barcode'))
        category = clean_input(request.form.get('category'))
        try:
            price = float(request.form.get('price'))
            stock = int(request.form.get('stock'))
            stock_min = int(request.form.get('stock_minimum', 0))
        except ValueError:
            flash("Invalid price or stock value", "error")
            return redirect(url_for('edit_product', product_id=product_id))
        expiry = clean_input(request.form.get('expiration_date')) or None

        selected_addons = []
        addon_names = request.form.getlist('addons')
        addon_prices = request.form.getlist('addon_price[]')
        for idx, addon_name in enumerate(addon_names):
            addon_name = clean_input(addon_name)
            if not addon_name:
                continue
            price = 0
            if idx < len(addon_prices):
                try:
                    price = float(addon_prices[idx])
                except (ValueError, TypeError):
                    price = 0
            selected_addons.append({'name': addon_name, 'price': price})

        update_data = {
            'name': name, 'barcode': barcode, 'category': category,
            'price': price, 'stock_quantity': stock, 'stock_minimum': stock_min,
            'expiration_date': expiry, 'updated_at': datetime.now(),
            'addons': selected_addons,
        }

        # Image handling
        image_url = clean_input(request.form.get('image_url', ''))
        uploaded_file = request.files.get('product_image')
        if uploaded_file and uploaded_file.filename and allowed_file(uploaded_file.filename):
            filename = secure_filename(uploaded_file.filename)
            name, ext = os.path.splitext(filename)
            filename = f"{name}_{uuid.uuid4().hex[:8]}{ext}"
            file_path = os.path.join(UPLOAD_FOLDER, filename)
            uploaded_file.save(file_path)
            image_url = url_for('product_image', filename=filename, _external=True)
        if image_url:
            update_data['image_url'] = image_url

        product_repository.update(product_id, update_data)
        _log_admin_activity('Edit Product', f'Edited product: {name}')
        flash("Product updated successfully", "success")
        return redirect(url_for('all_products'))

    product = product_repository.find_by_id(product_id, owner_id=owner_id)
    if not product:
        flash("Product not found", "error")
        return redirect(url_for('all_products'))
    p = {
        'id': product.id, 'name': product.name, 'barcode': product.barcode,
        'category': product.category, 'description': product.description,
        'imageUrl': product.image_url, 'image': product.image_url,
        'imagePath': product.image_path, 'status': product.status,
        'pricingType': product.pricing_type, 'price': product.price,
        'variants': product.variants, 'addons': product.addons,
        'stock': product.stock_quantity, 'stockMinimum': product.stock_minimum,
        'expirationDate': str(product.expiration_date) if product.expiration_date else None,
    }
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
    return render_template('edit_product.html', product=p, categories=DEFAULT_CATEGORIES,
                           low_stock_count=low_stock_count, expiring_count=expiring_count,
                           active_main='catalog', active_sub='all_products')


# ---------------------------------------------------------------
# Sales Reports
# ---------------------------------------------------------------
def _period_totals(owner_id: Optional[str] = None):
    """Compute today/week/month/year/overall totals and counts."""
    rows = query_all(
        "SELECT * FROM sales WHERE owner_id = %s ORDER BY date DESC",
        (owner_id,),
    ) if owner_id else []
    sales = []
    for s in rows:
        sd = dict(s)
        sd['date'] = _to_naive(sd.get('date'))
        sales.append(sd)

    now = datetime.now()
    today = now.date()
    week_ago = now - timedelta(days=7)
    month_start = now.replace(day=1)
    year_start = now.replace(month=1, day=1)

    def filt(start=None, end=None):
        total = 0.0
        count = 0
        for s in sales:
            dt = _to_naive(s.get('date'))
            if start and dt < start:
                continue
            if end and dt > end:
                continue
            total += float(s.get('total', 0))
            count += 1
        return total, count

    daily = filt(start=datetime(today.year, today.month, today.day))
    weekly = filt(start=week_ago)
    monthly = filt(start=month_start)
    yearly = filt(start=year_start)
    overall = filt()
    return sales, {
        'daily': daily, 'weekly': weekly, 'monthly': monthly,
        'yearly': yearly, 'overall': overall,
    }


@app.route('/sales_dashboard')
@admin_required
def sales_dashboard():
    owner_id = _current_owner_id()
    sales, totals = _period_totals(owner_id=owner_id)

    def split(start=None):
        coffee = 0.0
        pastry = 0.0
        coffee_c = 0
        pastry_c = 0
        for s in sales:
            dt = s['date']
            if start and dt < start:
                continue
            if s.get('product_type') == 'Coffee':
                coffee += float(s.get('total', 0))
                coffee_c += 1
            else:
                pastry += float(s.get('total', 0))
                pastry_c += 1
        return coffee, coffee_c, pastry, pastry_c

    now = datetime.now()
    today = datetime(now.year, now.month, now.day)
    week_ago = now - timedelta(days=7)
    month_start = now.replace(day=1)
    year_start = now.replace(month=1, day=1)

    daily_c, daily_cc, daily_p, daily_pc = split(today)
    weekly_c, weekly_cc, weekly_p, weekly_pc = split(week_ago)
    monthly_c, monthly_cc, monthly_p, monthly_pc = split(month_start)
    yearly_c, yearly_cc, yearly_p, yearly_pc = split(year_start)
    overall_c, overall_cc, overall_p, overall_pc = split()

    labels = []
    coffee_vals = []
    pastry_vals = []
    for i in range(29, -1, -1):
        day = (now - timedelta(days=i)).date()
        labels.append(day.strftime('%m-%d'))
        c_total = sum(float(s.get('total', 0)) for s in sales
                      if s['date'].date() == day and s.get('product_type') == 'Coffee')
        p_total = sum(float(s.get('total', 0)) for s in sales
                      if s['date'].date() == day and s.get('product_type') == 'Pastry')
        coffee_vals.append(round(c_total, 2))
        pastry_vals.append(round(p_total, 2))

    agg = {}
    rows = query_all(
        "SELECT si.name AS name, si.quantity AS quantity "
        "FROM sale_items si JOIN sales s ON si.sale_id = s.id WHERE s.owner_id = %s",
        (owner_id,),
    )
    for r in rows:
        key = r['name']
        agg[key] = agg.get(key, 0) + int(r['quantity'] or 0)

    popular = sorted(agg.items(), key=lambda x: x[1], reverse=True)[:10]

    owner_id = _current_owner_id()
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)

    return render_template('sales_dashboard_content.html',
                           low_stock_count=low_stock_count, expiring_count=expiring_count,
                           daily_sales=totals['daily'][0], daily_count=totals['daily'][1],
                           daily_coffee_sales=daily_c, daily_pastry_sales=daily_p,
                           weekly_sales=totals['weekly'][0], weekly_count=totals['weekly'][1],
                           weekly_coffee_sales=weekly_c, weekly_pastry_sales=weekly_p,
                           monthly_sales=totals['monthly'][0], monthly_count=totals['monthly'][1],
                           monthly_coffee_sales=monthly_c, monthly_pastry_sales=monthly_p,
                           yearly_sales=totals['yearly'][0], yearly_count=totals['yearly'][1],
                           yearly_coffee_sales=yearly_c, yearly_pastry_sales=yearly_p,
                           overall_sales=totals['overall'][0], overall_count=totals['overall'][1],
                           overall_coffee_sales=overall_c, overall_pastry_sales=overall_p,
                           popular=popular,
                           chart_labels=labels, coffee_values=coffee_vals, pastry_values=pastry_vals,
                           active_main='sales', active_sub='sales_dashboard')


@app.route('/coffee_sales')
@admin_required
def coffee_sales():
    owner_id = _current_owner_id()
    rows = query_all(
        "SELECT * FROM sales WHERE owner_id = %s AND product_type = 'Coffee' ORDER BY date DESC",
        (owner_id,),
    )
    sales = [dict(r) for r in rows]
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
    return render_template('sales_coffee_content.html', sales=sales,
                           low_stock_count=low_stock_count, expiring_count=expiring_count,
                           active_main='sales', active_sub='coffee_sales')


@app.route('/pastry_sales')
@admin_required
def pastry_sales():
    owner_id = _current_owner_id()
    rows = query_all(
        "SELECT * FROM sales WHERE owner_id = %s AND product_type = 'Pastry' ORDER BY date DESC",
        (owner_id,),
    )
    sales = [dict(r) for r in rows]
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
    return render_template('sales_pastry_content.html', sales=sales,
                           low_stock_count=low_stock_count, expiring_count=expiring_count,
                           active_main='sales', active_sub='pastry_sales')


# ---------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------
@app.route('/out_of_stock')
@admin_required
def out_of_stock():
    owner_id = _current_owner_id()
    category_filter = request.args.get('category', 'all')
    rows = query_all(
        "SELECT * FROM products WHERE owner_id = %s AND stock_quantity <= %s ORDER BY name",
        (owner_id, LOW_STOCK_THRESHOLD),
    )
    products = []
    for r in rows:
        p = dict(r)
        p['stock'] = int(p.get('stock_quantity') or 0)
        if category_filter == 'all' or p.get('category') == category_filter:
            products.append(p)
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
    return render_template('inventory_out_of_stock.html', products=products,
                           low_stock_count=low_stock_count, expiring_count=expiring_count,
                           active_main='inventory', active_sub='out_of_stock',
                           category_filter=category_filter)


@app.route('/expiring')
@admin_required
def expiring_products():
    category_filter = request.args.get('category', 'all')
    owner_id = _current_owner_id()
    threshold = datetime.now() + timedelta(days=EXPIRING_DAYS)
    rows = query_all(
        "SELECT * FROM products WHERE owner_id = %s AND expiration_date IS NOT NULL "
        "AND expiration_date <= %s ORDER BY name",
        (owner_id, threshold.date()),
    )
    products = [dict(r) for r in rows if category_filter == 'all' or r.get('category') == category_filter]
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
    return render_template('inventory_expiring.html', products=products,
                           low_stock_count=low_stock_count, expiring_count=expiring_count,
                           active_main='inventory', active_sub='expiring',
                           category_filter=category_filter)


# ---------------------------------------------------------------
# Staff Management
# ---------------------------------------------------------------
@app.route('/register_cashier', methods=['GET', 'POST'])
@admin_required
def register_cashier():
    owner_id = _current_owner_id()
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
    if request.method == 'POST':
        username = clean_input(request.form.get('username', '')).strip()
        display_name = clean_input(request.form.get('display_name', '')).strip()
        password = request.form.get('password', '')
        if not username or not display_name or not password:
            flash("Username, full name, and password are required", "error")
            return redirect(url_for('register_cashier'))

        existing = query_one(
            "SELECT id FROM users WHERE username = %s LIMIT 1", (username,)
        )
        if existing:
            flash("Username already exists", "error")
            return redirect(url_for('register_cashier'))

        user = auth_service.create_user(
            email=f"{username}@brewpos.local", password=password,
            display_name=display_name, role='cashier',
        )
        if not user:
            flash("Username already exists", "error")
            return redirect(url_for('register_cashier'))
        execute("UPDATE users SET username = %s, owner_id = %s WHERE id = %s",
                (username, owner_id, user['id']))
        _log_admin_activity('Register Cashier',
                            f'Registered cashier: {username} ({display_name})')
        flash("Cashier registered successfully", "success")
        return redirect(url_for('register_cashier'))

    cashiers = query_all(
        "SELECT id AS uid, username, display_name, status, created_at FROM users "
        "WHERE role = 'cashier' AND owner_id = %s ORDER BY display_name",
        (owner_id,),
    )
    return render_template('register_cashier.html', cashiers=[dict(c) for c in cashiers],
                           low_stock_count=low_stock_count, expiring_count=expiring_count,
                           active_main='management', active_sub='register_cashier')


@app.route('/delete_cashier', methods=['GET', 'POST'])
@admin_required
def delete_cashier():
    owner_id = _current_owner_id()
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
    if request.method == 'POST':
        uid = request.form.get('uid')
        user = auth_service.get_user_by_id(uid)
        username = user.get('username', 'Unknown') if user else 'Unknown'
        execute(
            "UPDATE users SET status = 'inactive', role = 'cashier_disabled' "
            "WHERE id = %s AND owner_id = %s", (uid, owner_id)
        )
        _log_admin_activity('Delete Cashier', f'Deleted cashier: {username}')
        flash("Cashier deleted successfully", "success")

    cashiers = query_all(
        "SELECT id AS uid, username, display_name, status, created_at FROM users "
        "WHERE role = 'cashier' AND owner_id = %s ORDER BY display_name",
        (owner_id,),
    )
    return render_template('delete_cashier.html', cashiers=[dict(c) for c in cashiers],
                           low_stock_count=low_stock_count, expiring_count=expiring_count,
                           active_main='management', active_sub='delete_cashier')


@app.route('/edit_cashier', methods=['POST'])
@admin_required
def edit_cashier():
    uid = request.form.get('uid')
    username = clean_input(request.form.get('username'))
    display_name = clean_input(request.form.get('display_name'))
    status = request.form.get('status')
    execute(
        "UPDATE users SET username = %s, display_name = %s, status = %s WHERE id = %s",
        (username, display_name, status, uid),
    )
    _log_admin_activity('Update Cashier', f'Updated cashier: {username}')
    flash("Cashier updated successfully!", "success")
    return redirect(url_for('delete_cashier'))


@app.route('/change_admin_password', methods=['GET', 'POST'])
@admin_required
def change_admin_password():
    owner_id = _current_owner_id()
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
    if request.method == 'POST':
        _log_admin_activity('Security Settings Update', 'Admin updated security settings')
        flash("Security settings updated successfully", "success")
        return redirect(url_for('change_admin_password'))
    return render_template('change_admin_password.html',
                           low_stock_count=low_stock_count, expiring_count=expiring_count,
                           active_main='management', active_sub='change_admin_password')


# ---------------------------------------------------------------
# Receipt Customization
# ---------------------------------------------------------------
@app.route('/receipt_customization', methods=['GET', 'POST'])
@admin_required
def receipt_customization():
    owner_id = _current_owner_id()
    if request.method == 'POST':
        settings = {
            'storeName': clean_input(request.form.get('store_name')) or DEFAULT_STORE_SETTINGS['storeName'],
            'subtitle': clean_input(request.form.get('subtitle')) or DEFAULT_STORE_SETTINGS['subtitle'],
            'address': clean_input(request.form.get('address')),
            'contact': clean_input(request.form.get('contact')),
            'footer': clean_input(request.form.get('footer')) or DEFAULT_STORE_SETTINGS['footer'],
        }
        doc_id = f'config_{owner_id}'
        row = query_one("SELECT id FROM store_settings WHERE id = %s", (doc_id,))
        if row:
            execute(
                "UPDATE store_settings SET store_name = %s, subtitle = %s, address = %s, "
                "contact = %s, footer = %s, updated_at = %s WHERE id = %s",
                (settings['storeName'], settings['subtitle'], settings['address'],
                 settings['contact'], settings['footer'], datetime.now(), doc_id),
            )
        else:
            execute(
                "INSERT INTO store_settings (id, store_name, subtitle, address, contact, footer, owner_id, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (doc_id, settings['storeName'], settings['subtitle'], settings['address'],
                 settings['contact'], settings['footer'], owner_id, datetime.now()),
            )
        flash("Receipt settings saved successfully!", "success")

    settings = get_store_settings(owner_id=owner_id)
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
    return render_template('receipt_customization.html', settings=settings,
                           low_stock_count=low_stock_count, expiring_count=expiring_count,
                           active_main='management', active_sub='receipt_customization')


# ---------------------------------------------------------------
# Backup / Restore
# ---------------------------------------------------------------
@app.route('/backup_restore')
@admin_required
def backup_restore():
    owner_id = _current_owner_id()
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
    return render_template('backup_restore.html',
                           low_stock_count=low_stock_count, expiring_count=expiring_count,
                           active_main='management', active_sub='backup_restore',
                           project_id='brewpos-postgres')


def _row_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


@app.route('/backup_database', methods=['POST'])
@admin_required
def backup_database():
    owner_id = _current_owner_id()
    export = {}
    owner_tables = ['users', 'products', 'sales', 'stock_movements',
                    'store_settings', 'admin_activity', 'cashier_activity']
    for col in owner_tables:
        export[col] = [dict(r) for r in
                       query_all(f"SELECT * FROM {col} WHERE owner_id = %s ORDER BY id", (owner_id,))]
    # Tables without an owner_id column
    export['categories'] = [dict(r) for r in query_all("SELECT * FROM categories ORDER BY id")]
    export['sale_items'] = [dict(r) for r in
                            query_all("SELECT si.* FROM sale_items si "
                                      "JOIN sales s ON si.sale_id = s.id "
                                      "WHERE s.owner_id = %s ORDER BY si.id", (owner_id,))]
    mem = BytesIO()
    mem.write(json.dumps(export, default=_row_default, indent=2).encode('utf-8'))
    mem.seek(0)
    return send_file(mem, as_attachment=True,
                     download_name=f"brewpos_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                     mimetype='application/json')


@app.route('/restore_database', methods=['POST'])
@admin_required
def restore_database():
    if 'backup_file' not in request.files:
        flash("No file uploaded", "error")
        return redirect(url_for('backup_restore'))
    f = request.files['backup_file']
    if not f.filename.endswith('.json'):
        flash("Please upload a .json backup file", "error")
        return redirect(url_for('backup_restore'))
    try:
        data = json.loads(f.read().decode('utf-8'))
        owner_id = _current_owner_id()
        allowed_cols = [
            'users', 'categories', 'products', 'sale_items', 'sales',
            'stock_movements', 'store_settings', 'admin_activity', 'cashier_activity',
        ]
        owner_tables = ['users', 'products', 'sales', 'stock_movements',
                        'store_settings', 'admin_activity', 'cashier_activity']
        # Tables with SERIAL primary keys - do not insert id, let DB auto-generate
        serial_tables = {'categories', 'sale_items', 'stock_movements', 'admin_activity', 'cashier_activity'}
        for col, rows in data.items():
            if col not in allowed_cols:
                continue
            for row in rows:
                row = dict(row)
                if col in owner_tables:
                    row['owner_id'] = owner_id
                if col == 'users':
                    row.pop('password_hash', None)
                # For SERIAL tables, let the database auto-generate the id
                if col in serial_tables:
                    row.pop('id', None)
                # Ensure every row has an id (primary key) for non-SERIAL tables
                elif not row.get('id'):
                    row['id'] = str(uuid.uuid4())
                columns = list(row.keys())
                placeholders = ", ".join(["%s"] * len(columns))
                col_list = ", ".join(columns)
                execute(
                    f"INSERT INTO {col} ({col_list}) VALUES ({placeholders}) "
                    f"ON CONFLICT (id) DO NOTHING",
                    [row[c] for c in columns],
                )
        flash("Backup restored successfully!", "success")
    except Exception as e:
        flash(f"Restore error: {e}", "error")
    return redirect(url_for('backup_restore'))


# ---------------------------------------------------------------
# Cashier POS
# ---------------------------------------------------------------
@app.route('/cashier')
@cashier_required
def cashier_dashboard():
    cid = session['cashier_id']
    owner_id = _current_owner_id()
    today = datetime.now().date()
    today_start = datetime(today.year, today.month, today.day)
    rows = query_all(
        "SELECT * FROM sales WHERE cashier_id = %s AND owner_id = %s ORDER BY date DESC",
        (cid, owner_id),
    )
    sales = [dict(r) for r in rows]
    today_total = sum(float(s.get('total', 0)) for s in sales
                      if _to_naive(s.get('date')) >= today_start)
    today_count = sum(1 for s in sales
                      if _to_naive(s.get('date')) >= today_start)

    recent = sorted(sales, key=lambda s: _to_naive(s.get('date')), reverse=True)[:5]
    recent_sales = [dict(s, id=s.get('id')) for s in recent]

    settings = get_store_settings(owner_id=owner_id)
    return render_template('cashier_dashboard.html', recent_sales=recent_sales,
                           today_total=today_total, today_count=today_count,
                           settings=settings)


@app.route('/cashier_history')
@cashier_required
def cashier_history():
    cid = session['cashier_id']
    owner_id = _current_owner_id()
    today = datetime.now().date()
    today_start = datetime(today.year, today.month, today.day)
    sales_data = []
    chart_sales = []
    chart_transactions = []
    labels = [today.strftime('%m-%d')]
    rows = query_all(
        "SELECT * FROM sales WHERE cashier_id = %s AND owner_id = %s ORDER BY date DESC",
        (cid, owner_id),
    )
    for srow in rows:
        sdoc = dict(srow)
        dt = _to_naive(sdoc.get('date'))
        if dt < today_start:
            continue
        item_rows = query_all(
            "SELECT * FROM sale_items WHERE sale_id = %s", (sdoc['id'],),
        )
        items = [dict(it) for it in item_rows]
        sales_data.append({
            'id': sdoc['id'], 'receipt_number': sdoc.get('receipt_no'),
            'total_amount': float(sdoc.get('total', 0)), 'sale_date': dt, 'items': items,
        })
        chart_sales.append(float(sdoc.get('total', 0)))
        chart_transactions.append(1)
    return render_template('cashier_history.html', sales=sales_data,
                           chart_labels=labels, chart_sales=chart_sales,
                           chart_transactions=chart_transactions)


@app.route('/api/products')
@cashier_required
def api_products():
    products = []
    owner_id = _current_owner_id()
    for p in product_repository.find_in_stock(owner_id=owner_id):
        price = float(p.price) if p.price else 0
        if p.variants:
            price = float(p.variants[0].get('price', 0)) if p.variants else price
        products.append({
            'id': p.id, 'name': p.name, 'price': price,
            'stock': p.stock_quantity, 'barcode': p.barcode, 'category': p.category,
            'image': p.image_url, 'imageUrl': p.image_url, 'imagePath': p.image_path,
            'variants': p.variants, 'pricingType': p.pricing_type, 'addons': p.addons,
        })
    return jsonify(products)


@app.route('/api/search_by_name')
@admin_required
def search_by_name():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])
    results = []
    owner_id = _current_owner_id()
    for p in product_repository.find_all(owner_id=owner_id):
        if query.lower() in p.name.lower():
            results.append(p.name)
        if len(results) >= 5:
            break
    return jsonify(results)


@app.route('/api/product/<product_id>')
@admin_required
def api_get_product(product_id):
    owner_id = _current_owner_id()
    p = product_repository.find_by_id(product_id, owner_id=owner_id)
    if not p:
        return jsonify({'success': False})
    return jsonify({'success': True, 'product': {
        'id': p.id, 'name': p.name, 'barcode': p.barcode, 'category': p.category,
        'price': float(p.price), 'stock': p.stock_quantity, 'image': p.image_url,
        'expirationDate': str(p.expiration_date) if p.expiration_date else None,
        'description': p.description, 'status': p.status,
        'pricingType': p.pricing_type, 'variants': p.variants,
        'addons': p.addons, 'stockMinimum': p.stock_minimum,
    }})


@app.route('/api/update_product', methods=['POST'])
@admin_required
def api_update_product():
    data = request.get_json() or {}
    product_id = data.get('id')
    owner_id = _current_owner_id()
    product = product_repository.find_by_id(product_id, owner_id=owner_id)
    if not product:
        return jsonify({'success': False, 'message': 'Product not found'}), 404
    update_data = {
        'name': clean_input(data.get('name')),
        'barcode': clean_input(data.get('barcode')),
        'category': clean_input(data.get('category')),
        'price': float(data.get('price', 0)),
        'stock_quantity': int(data.get('stock', 0)),
        'stock_minimum': int(data.get('stockMinimum', 0)),
        'expiration_date': data.get('expirationDate'),
        'updated_at': datetime.now(),
    }
    product_repository.update(product_id, update_data)
    _log_admin_activity('Edit Product', f"Edited product: {update_data['name']}")
    return jsonify({'success': True, 'message': 'Product updated successfully!'})


def _price_sale_item(item, owner_id):
    if not isinstance(item, dict):
        raise ValueError('Invalid cart item.')
    product = product_repository.find_by_id(str(item.get('id', '')), owner_id=owner_id)
    if product is None:
        raise ValueError('A product in this cart is no longer available.')
    try:
        quantity = int(item.get('quantity', 0))
        client_price = float(item.get('price', 0))
    except (TypeError, ValueError):
        raise ValueError('Invalid quantity or price.')
    if quantity <= 0:
        raise ValueError('Quantity must be greater than zero.')

    requested_addons = item.get('addons', [])
    if not isinstance(requested_addons, list) or len(set(requested_addons)) != len(requested_addons):
        raise ValueError('Invalid add-ons selected.')
    addon_prices = {}
    for addon in product.addons or []:
        if isinstance(addon, str):
            addon_prices[addon] = 0.0
        elif isinstance(addon, dict) and addon.get('name'):
            addon_prices[str(addon['name'])] = float(addon.get('price', 0) or 0)
    if any(addon not in addon_prices for addon in requested_addons):
        raise ValueError('An add-on in this cart is no longer available.')
    addons_total = sum(addon_prices[addon] for addon in requested_addons)

    variants = product.variants or []
    variant_name = str(item.get('variantName', ''))
    if variants:
        variant = next((v for v in variants if isinstance(v, dict) and v.get('name') == variant_name), None)
        if variant is None:
            raise ValueError('Choose a valid product size or variant.')
        base_price = float(variant.get('price', 0))
    else:
        cup_size = item.get('cupSize', 'S')
        if cup_size not in CUP_ADD:
            raise ValueError('Choose a valid cup size.')
        base_price = float(product.price) + CUP_ADD[cup_size]

    expected_price = round(base_price + addons_total, 2)
    if client_price < 0 or abs(client_price - expected_price) > 0.01:
        raise ValueError('Price changed. Please refresh the menu and try again.')
    item['price'] = expected_price
    item['name'] = product.name + (f' ({variant_name})' if variants else (f" ({item.get('cupSize')})" if item.get('cupSize', 'S') != 'S' else ''))
    if requested_addons:
        item['name'] += ' + ' + ', '.join(requested_addons)
    item['image'] = product.image_url or ''
    return expected_price, quantity


@app.route('/complete_sale', methods=['POST'])
@cashier_required
def complete_sale():
    if not request.is_json:
        return jsonify({'success': False, 'message': 'Invalid request format'})
    data = request.get_json() or {}
    items = data.get('items', [])
    tendered = data.get('tendered', 0)
    try:
        tendered = float(tendered)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Invalid tendered amount'})
    if not items:
        return jsonify({'success': False, 'message': 'No items in cart'})

    total_amount = 0.0
    for item in items:
        try:
            price, _ = _price_sale_item(item, _current_owner_id())
            qty = int(item.get('quantity', 0))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'message': 'Invalid item data'})
        if price < 0 or qty <= 0:
            return jsonify({'success': False, 'message': 'Invalid item data'})
        total_amount += price * qty

    if tendered < total_amount:
        return jsonify({'success': False, 'message': 'Amount tendered is less than total'})

    result = sales_service.process_sale(items, session['cashier_id'], _current_owner_id())
    if not result.get('success'):
        return jsonify({'success': False, 'message': result.get('message', 'Sale failed')})

    result['tendered'] = round(tendered, 2)
    result['change'] = round(tendered - total_amount, 2)
    return jsonify(result)


@app.route('/api/mark_receipt_printed', methods=['POST'])
@cashier_required
def api_mark_receipt_printed():
    data = request.get_json(silent=True) or {}
    receipt_number = data.get('receipt_number', '')
    if not receipt_number:
        return jsonify({'success': False, 'message': 'Receipt number is required.'})
    ok = sales_service.mark_receipt_printed(receipt_number, _current_owner_id())
    if not ok:
        return jsonify({'success': False, 'message': 'Receipt not found or already completed.'})
    return jsonify({'success': True, 'message': 'Receipt marked as printed.'})


@app.route('/api/cancel_pending_sale', methods=['POST'])
@cashier_required
def api_cancel_pending_sale():
    data = request.get_json(silent=True) or {}
    receipt_number = data.get('receipt_number', '')
    if not receipt_number:
        return jsonify({'success': False, 'message': 'Receipt number is required.'})
    ok = sales_service.cancel_pending_sale(receipt_number, _current_owner_id())
    if not ok:
        return jsonify({'success': False, 'message': 'Pending sale not found.'})
    return jsonify({'success': True, 'message': 'Pending sale cancelled and stock restored.'})


# ---------------------------------------------------------------
# Logout
# ---------------------------------------------------------------
@app.route('/admin_logout')
def admin_logout():
    if session.get('role') == 'admin':
        _log_admin_activity('Admin Logout', 'Admin logged out')
    session.clear()
    return redirect(url_for('login'))


@app.route('/cashier_logout')
def cashier_logout():
    if session.get('role') == 'cashier':
        _log_cashier_activity('Logout', 'Cashier logged out')
    session.clear()
    return redirect(url_for('login'))


@app.route('/logout')
def logout():
    session.pop('role', None)
    session.pop('admin_user', None)
    session.pop('admin_id', None)
    session.pop('cashier_user', None)
    session.pop('cashier_id', None)
    session.pop('user_email', None)
    session.pop('user_display_name', None)
    return redirect(url_for('login'))


# ---------------------------------------------------------------
# Static product images
# ---------------------------------------------------------------
@app.route('/static/images/products/<path:filename>')
def product_image(filename):
    return send_from_directory(os.path.join(app.static_folder, 'images', 'products'), filename)


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
