"""BrewPOS - Coffee Shop POS and Inventory System (Flask + Firebase/Firestore).

Pure coffee-focused POS. Authentication uses Google Sign-In (firebase-admin
token verification). All data lives in Cloud Firestore.
"""

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, send_file, send_from_directory
)
from datetime import datetime, timedelta
from typing import Optional
import os
import random
import json
import uuid
from io import BytesIO
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash

from firebase_config import (
    init_firebase, get_db, FIREBASE_WEB_CONFIG,
    LOW_STOCK_THRESHOLD, EXPIRING_DAYS,
    DEFAULT_CATEGORIES, DEFAULT_ADDONS, DEFAULT_STORE_SETTINGS,
    upload_image_to_storage, delete_image_from_storage, build_image_path,
    validate_image_file, compress_image,
)

from models import Product, StockMovement, User, Admin, Cashier
from services import AuthService, ProductRepository, SalesService


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
            doc = db.collection('cashiers').document(cid).get()
            if doc.exists:
                return doc.to_dict().get('ownerId')
    return None


# ---------------------------------------------------------------
# App setup
# ---------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("BREWPOS_SECRET_KEY", "brewpos_secret_key")
app.config['SESSION_PERMANENT'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = 86400 * 30  # 30 days auto-login
app.config['SESSION_COOKIE_NAME'] = 'brewpos_session'

LOW_STOCK_THRESHOLD = LOW_STOCK_THRESHOLD
EXPIRING_DAYS = EXPIRING_DAYS
DEFAULT_CATEGORIES = DEFAULT_CATEGORIES

db = init_firebase()
auth_service = AuthService()
product_repository = ProductRepository(db)
sales_service = SalesService(db)

CUP_ADD = {'S': 0, 'M': 10, 'L': 20}

# Inject globals into templates
@app.context_processor
def inject_globals():
    return dict(
        LOW_STOCK_THRESHOLD=LOW_STOCK_THRESHOLD,
        DEFAULT_CATEGORIES=DEFAULT_CATEGORIES,
        DEFAULT_ADDONS=DEFAULT_ADDONS,
        firebase_web_config=FIREBASE_WEB_CONFIG,
        now=datetime.now,
    )


def clean_input(value):
    return value.strip() if value else ""


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') != 'admin' or not session.get('admin_id'):
            return redirect(url_for('google_login'))
        return f(*args, **kwargs)
    return decorated


def cashier_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        is_api = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        if session.get('role') != 'cashier' or not session.get('cashier_id'):
            if is_api:
                return jsonify({'success': False, 'message': 'Session expired. Please login again.'}), 401
            return redirect(url_for('google_login'))
        # Verify cashier is still active
        doc = db.collection('cashiers').document(session.get('cashier_id')).get()
        if not doc.exists or (doc.to_dict().get('status', 'active') or 'active') != 'active':
            session.clear()
            if is_api:
                return jsonify({'success': False, 'message': 'Cashier account is inactive.'}), 401
            flash("Cashier account is inactive. Contact the administrator.", "error")
            return redirect(url_for('google_login'))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------
# Google Sign-In
# ---------------------------------------------------------------
@app.route('/')
def index():
    if session.get('role') == 'admin':
        return redirect(url_for('admin_dashboard'))
    if session.get('role') == 'cashier':
        return redirect(url_for('cashier_dashboard'))
    if session.get('google_auth'):
        return redirect(url_for('select_role'))
    return redirect(url_for('google_login'))


@app.route('/login')
def google_login():
    if session.get('role') in ('admin', 'cashier'):
        return redirect(url_for('index'))
    if session.get('google_auth'):
        return redirect(url_for('select_role'))
    return render_template('google_login.html')


@app.route('/api/google_signin', methods=['POST'])
def api_google_signin():
    data = request.get_json(silent=True) or {}
    id_token = data.get('idToken')
    if not id_token:
        return jsonify({'success': False, 'message': 'Missing ID token.'}), 400

    claims = auth_service.verify_google_user(id_token)
    if not claims:
        return jsonify({'success': False, 'message': 'Invalid Google token.'}), 401

    user_doc = auth_service.get_or_create_user(claims)
    session.clear()
    session['google_auth'] = {
        'uid': user_doc.get('uid'),
        'email': user_doc.get('email'),
        'displayName': user_doc.get('displayName'),
    }
    session.permanent = True

    # Log activity
    try:
        role = user_doc.get('role')
        if role == 'admin':
            db.collection('admin_activity').add({
                'adminId': user_doc.get('uid'),
                'action': 'Admin Login',
                'timestamp': datetime.now(),
                'ownerId': session.get('admin_id'),
            })
        else:
            db.collection('cashier_activity').add({
                'cashierId': user_doc.get('uid'),
                'action': 'Login',
                'timestamp': datetime.now(),
                'ownerId': session.get('admin_id'),
            })
    except Exception:
        pass

    return jsonify({'success': True})


# ---------------------------------------------------------------
# Role Selection
# ---------------------------------------------------------------
@app.route('/select_role')
def select_role():
    if session.get('role') == 'admin' and session.get('admin_id'):
        return redirect(url_for('admin_dashboard'))
    if session.get('role') == 'cashier' and session.get('cashier_id'):
        return redirect(url_for('cashier_dashboard'))
    if 'google_auth' not in session:
        return redirect(url_for('google_login'))
    cashier_username = ''
    if 'google_auth' in session:
        uid = session['google_auth'].get('uid')
        if uid:
            doc = db.collection('cashiers').document(uid).get()
            if doc.exists:
                cashier_username = doc.to_dict().get('username', '')
    return render_template('select_role.html',
                           cashier_username=cashier_username,
                           active_main='select', active_sub='select_role')


@app.route('/api/auth/select_role', methods=['POST'])
def api_auth_select_role():
    if 'google_auth' not in session:
        return jsonify({'success': False, 'message': 'Not authenticated.'}), 401

    data = request.get_json(silent=True) or {}
    role = data.get('role')

    if role == 'admin':
        username = clean_input(data.get('username', ''))
        password = clean_input(data.get('password', ''))
        if username == 'admin' and password == 'admin123':
            session['role'] = 'admin'
            session['admin_user'] = session['google_auth']['email']
            session['admin_id'] = session['google_auth']['uid']
            return jsonify({'success': True, 'redirect': '/admin'})
        return jsonify({'success': False, 'message': 'Invalid admin credentials.'}), 401

    if role == 'cashier':
        username = clean_input(data.get('username', ''))
        password = data.get('password', '')
        if not username or not password:
            return jsonify({'success': False, 'message': 'Username and password are required.'}), 401
        doc = db.collection('cashiers').document(username).get()
        if not doc.exists:
            return jsonify({'success': False, 'message': 'Cashier account not found. Contact admin.'}), 401
        cashier = doc.to_dict()
        if not cashier.get('passwordHash') or not check_password_hash(cashier['passwordHash'], password):
            return jsonify({'success': False, 'message': 'Invalid username or password.'}), 401
        session['role'] = 'cashier'
        session['cashier_user'] = cashier.get('username', username)
        session['cashier_id'] = username
        return jsonify({'success': True, 'redirect': '/cashier'})

    return jsonify({'success': False, 'message': 'Invalid role.'}), 400


# ---------------------------------------------------------------
# Admin Dashboard
# ---------------------------------------------------------------
@app.route('/admin')
@admin_required
def admin_dashboard():
    owner_id = _current_owner_id()
    cashiers = [d.to_dict() | {'uid': d.id} for d in
                db.collection('cashiers').where('ownerId', '==', owner_id).stream()]
    active = [a.to_dict() for a in
              db.collection('cashier_activity').where('ownerId', '==', session.get('admin_id')).stream()]
    active = [a for a in active if a.get('action') == 'Login']
    # Build active set (logins without matching logout)
    activity_logs = []
    logs_raw = list(db.collection('cashier_activity').where('ownerId', '==', session.get('admin_id')).stream())
    for a in sorted(logs_raw, key=lambda x: x.to_dict().get('timestamp', ''), reverse=True)[:10]:
        activity_logs.append(a.to_dict())

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
    logs_raw = list(db.collection('cashier_activity').where('ownerId', '==', session.get('admin_id')).stream())
    logs = sorted([a.to_dict() for a in logs_raw],
                  key=lambda x: x.get('timestamp', ''), reverse=True)
    owner_id = _current_owner_id()
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
    return render_template('admin_cashier_logs.html', activity_logs=logs,
                           low_stock_count=low_stock_count, expiring_count=expiring_count,
                           active_main='dashboard', active_sub='cashier_logs')


@app.route('/admin/activity_logs')
@admin_required
def admin_activity_logs():
    logs_raw = list(db.collection('admin_activity').where('ownerId', '==', session.get('admin_id')).stream())
    logs = sorted([a.to_dict() for a in logs_raw],
                  key=lambda x: x.get('timestamp', ''), reverse=True)[:50]
    owner_id = _current_owner_id()
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
    return render_template('admin_activity_logs.html', admin_logs=logs,
                           low_stock_count=low_stock_count, expiring_count=expiring_count,
                           active_main='dashboard', active_sub='activity_logs')


# ---------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------
@app.route('/all_products')
@admin_required
def all_products():
    owner_id = _current_owner_id()
    products = []
    for p in product_repository.find_all(owner_id=owner_id):
        d = p.to_dict() | {'id': p.id}
        d['image'] = d.get('imageUrl', '') or d.get('image', '')
        stock_data = d.get('stock', {})
        if isinstance(stock_data, dict):
            d['stock'] = stock_data.get('quantity', 0)
        d['price'] = d.get('price', 0) or (d.get('variants', [{}])[0].get('price', 0) if d.get('variants') else 0)
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
        for d in db.collection('products').where('name', '==', name).stream():
            ed = d.to_dict()
            if ed.get('ownerId') == owner_id and ed.get('category') == category:
                existing = d
                break

        try:
            now = datetime.now()
            if existing:
                existing_data = existing.to_dict()
                new_stock = existing_data.get('stock', {}).get('quantity', 0) + stock_qty if isinstance(existing_data.get('stock'), dict) else existing_data.get('stock', 0) + stock_qty
                existing.reference.update({
                    'stock': {'quantity': new_stock, 'minimum': stock_min},
                    'updatedAt': now,
                })
                product_id = existing.id
                message = f"Stock updated! Added {stock_qty} units. Total: {new_stock}"
                action = 'Stock Update'
            else:
                image_url = clean_input(request.form.get('image_url', ''))
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
                    'stock': {'quantity': stock_qty, 'minimum': stock_min},
                    'createdAt': now,
                    'updatedAt': now,
                    'ownerId': session.get('admin_id'),
                }
                if pricing_type == 'single':
                    product_data['price'] = price
                product_id = product_repository.save_from_dict(product_data)
                message = "Product added successfully."
                action = 'Add New Product'

            db.collection('stock_movements').add({
                'productId': product_id,
                'movementType': 'IN',
                'quantity': stock_qty,
                'reason': 'Stock Addition',
                'timestamp': now,
            })
            db.collection('admin_activity').add({
                'adminId': session.get('admin_id'),
                'action': action,
                'timestamp': now,
                'details': f'{name} - {stock_qty} units added',
                'ownerId': session.get('admin_id'),
            })

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
        doc = db.collection('products').document(product_id).get()
        if not doc.exists or doc.to_dict().get('ownerId') != owner_id:
            flash("Product not found", "error")
            return redirect(url_for('all_products'))
        raw = doc.to_dict()
        product = raw | {'id': doc.id}
        product['image'] = product.get('imageUrl', '') or product.get('image', '')
        product['imagePath'] = product.get('imagePath', '')
        stock_data = product.get('stock', {})
        if isinstance(stock_data, dict):
            product['stock'] = stock_data.get('quantity', 0)
        low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
        expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
        return render_template('confirm_delete_product.html', product=product,
                               low_stock_count=low_stock_count, expiring_count=expiring_count,
                               active_main='catalog', active_sub='all_products')

    # POST delete
    doc = db.collection('products').document(product_id).get()
    if doc.exists and doc.to_dict().get('ownerId') == owner_id:
        data = doc.to_dict()
        name = data.get('name', 'Unknown')
        product_repository.delete(product_id)
        db.collection('admin_activity').add({
            'adminId': session.get('admin_id'),
            'action': 'Delete Product',
            'timestamp': datetime.now(),
            'details': f'Deleted product: {name}',
            'ownerId': session.get('admin_id'),
        })
    flash("Product deleted successfully", "success")
    return redirect(url_for('all_products'))


@app.route('/edit_product/<product_id>', methods=['GET', 'POST'])
@admin_required
def edit_product(product_id):
    owner_id = _current_owner_id()
    if request.method == 'POST':
        doc = db.collection('products').document(product_id).get()
        if not doc.exists or doc.to_dict().get('ownerId') != owner_id:
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
            'price': price, 'stock': {'quantity': stock, 'minimum': stock_min},
            'expirationDate': expiry, 'updatedAt': datetime.now(),
            'addons': selected_addons,
        }

        # Image handling
        doc = db.collection('products').document(product_id).get()
        old_data = doc.to_dict() if doc.exists else {}
        old_image_url = old_data.get('imageUrl', '')

        image_url = clean_input(request.form.get('image_url', ''))
        if image_url:
            update_data['imageUrl'] = image_url

        product_repository.update(product_id, update_data)
        db.collection('admin_activity').add({
            'adminId': session.get('admin_id'),
            'action': 'Edit Product',
            'timestamp': datetime.now(),
            'details': f'Edited product: {name}',
            'ownerId': session.get('admin_id'),
        })
        flash("Product updated successfully", "success")
        return redirect(url_for('all_products'))

    doc = db.collection('products').document(product_id).get()
    if not doc.exists or doc.to_dict().get('ownerId') != owner_id:
        flash("Product not found", "error")
        return redirect(url_for('all_products'))
    raw = doc.to_dict()
    product = raw | {'id': doc.id}
    product['image'] = product.get('imageUrl', '') or product.get('image', '')
    stock_data = product.get('stock', {})
    if isinstance(stock_data, dict):
        product['stock'] = stock_data.get('quantity', 0)
        product['stockMinimum'] = stock_data.get('minimum', 0)
    else:
        product['stockMinimum'] = 0
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
    return render_template('edit_product.html', product=product, categories=DEFAULT_CATEGORIES,
                           low_stock_count=low_stock_count, expiring_count=expiring_count,
                           active_main='catalog', active_sub='all_products')


# ---------------------------------------------------------------
# Sales Reports
# ---------------------------------------------------------------
def _period_totals(owner_id: Optional[str] = None):
    """Compute today/week/month/year/overall totals and counts."""
    query = db.collection('sales')
    if owner_id:
        query = query.where('ownerId', '==', owner_id)
    sales_docs = list(query.stream())
    sales = []
    for d in sales_docs:
        s = d.to_dict()
        s['date'] = _to_naive(s.get('date'))
        sales.append(s)

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

    # Coffee vs Pastry split for each period
    def split(start=None):
        coffee = 0.0
        pastry = 0.0
        coffee_c = 0
        pastry_c = 0
        for s in sales:
            dt = s['date']
            if start and dt < start:
                continue
            if s.get('productType') == 'Coffee':
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

    # Daily sales chart (last 30 days) coffee vs pastry
    labels = []
    coffee_vals = []
    pastry_vals = []
    for i in range(29, -1, -1):
        day = (now - timedelta(days=i)).date()
        labels.append(day.strftime('%m-%d'))
        c_total = sum(float(s.get('total', 0)) for s in sales
                      if s['date'].date() == day and s.get('productType') == 'Coffee')
        p_total = sum(float(s.get('total', 0)) for s in sales
                      if s['date'].date() == day and s.get('productType') == 'Pastry')
        coffee_vals.append(round(c_total, 2))
        pastry_vals.append(round(p_total, 2))

    # Top selling products
    agg = {}
    for s in sales:
        for item in s.get('items', []) if 'items' in s else []:
            key = item.get('name')
            agg[key] = agg.get(key, 0) + int(item.get('quantity', 0))
    # If embedded items not present, aggregate from subcollections
    if not agg:
        for s_doc in db.collection('sales').where('ownerId', '==', owner_id).stream():
            for it in s_doc.reference.collection('items').stream():
                itd = it.to_dict()
                agg[itd.get('name')] = agg.get(itd.get('name'), 0) + int(itd.get('quantity', 0))

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
    sales_raw = list(db.collection('sales').where('productType', '==', 'Coffee').stream())
    sales = sorted([d.to_dict() | {'id': d.id} for d in sales_raw],
                   key=lambda s: s.get('date', ''), reverse=True)
    sales = [s for s in sales if s.get('ownerId') == owner_id]
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
    return render_template('sales_coffee_content.html', sales=sales,
                           low_stock_count=low_stock_count, expiring_count=expiring_count,
                           active_main='sales', active_sub='coffee_sales')


@app.route('/pastry_sales')
@admin_required
def pastry_sales():
    owner_id = _current_owner_id()
    sales_raw = list(db.collection('sales').where('productType', '==', 'Pastry').stream())
    sales = sorted([d.to_dict() | {'id': d.id} for d in sales_raw],
                   key=lambda s: s.get('date', ''), reverse=True)
    sales = [s for s in sales if s.get('ownerId') == owner_id]
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
    products = []
    for d in db.collection('products').stream():
        p = d.to_dict() | {'id': d.id}
        if p.get('ownerId') != owner_id:
            continue
        stock_data = p.get('stock', {})
        if isinstance(stock_data, dict):
            qty = int(stock_data.get('quantity', 0))
        else:
            qty = int(stock_data) if stock_data is not None else 0
        if qty <= LOW_STOCK_THRESHOLD:
            p['stock'] = qty
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
    products = []
    for d in db.collection('products').where('ownerId', '==', owner_id).stream():
        p = d.to_dict() | {'id': d.id}
        exp = _to_naive(p.get('expirationDate'))
        if exp and exp <= threshold:
            stock_data = p.get('stock', {})
            if isinstance(stock_data, dict):
                p['stock'] = stock_data.get('quantity', 0)
            if category_filter == 'all' or p.get('category') == category_filter:
                products.append(p)
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

        existing = list(db.collection('cashiers').where('username', '==', username).limit(1).stream())
        if existing:
            flash("Username already exists", "error")
            return redirect(url_for('register_cashier'))

        uid = username
        password_hash = generate_password_hash(password)
        db.collection('users').document(uid).set({
            'uid': uid, 'username': username, 'displayName': display_name,
            'role': 'cashier', 'status': 'active', 'createdAt': datetime.now(),
            'ownerId': session.get('admin_id'),
        })
        db.collection('cashiers').document(uid).set({
            'uid': uid, 'username': username, 'displayName': display_name,
            'passwordHash': password_hash, 'status': 'active', 'createdAt': datetime.now(),
            'ownerId': session.get('admin_id'),
        })
        db.collection('admin_activity').add({
            'adminId': session.get('admin_id'), 'action': 'Register Cashier',
            'timestamp': datetime.now(), 'details': f'Registered cashier: {username} ({display_name})',
            'ownerId': session.get('admin_id'),
        })
        flash("Cashier registered successfully", "success")
        return redirect(url_for('register_cashier'))

    cashiers = [d.to_dict() | {'uid': d.id} for d in
                db.collection('cashiers').where('ownerId', '==', session.get('admin_id')).stream()]
    return render_template('register_cashier.html', cashiers=cashiers,
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
        doc = db.collection('cashiers').document(uid).get()
        username = doc.to_dict().get('username', 'Unknown') if doc.exists else 'Unknown'
        db.collection('cashiers').document(uid).delete()
        db.collection('users').document(uid).update({'role': 'cashier_disabled', 'status': 'inactive'})
        db.collection('admin_activity').add({
            'adminId': session.get('admin_id'), 'action': 'Delete Cashier',
            'timestamp': datetime.now(), 'details': f'Deleted cashier: {username}',
            'ownerId': session.get('admin_id'),
        })
        flash("Cashier deleted successfully", "success")

    cashiers = [d.to_dict() | {'uid': d.id} for d in
                db.collection('cashiers').where('ownerId', '==', session.get('admin_id')).stream()]
    return render_template('delete_cashier.html', cashiers=cashiers,
                           low_stock_count=low_stock_count, expiring_count=expiring_count,
                           active_main='management', active_sub='delete_cashier')


@app.route('/edit_cashier', methods=['POST'])
@admin_required
def edit_cashier():
    uid = request.form.get('uid')
    username = clean_input(request.form.get('username'))
    display_name = clean_input(request.form.get('display_name'))
    status = request.form.get('status')
    db.collection('cashiers').document(uid).update({
        'username': username, 'displayName': display_name, 'status': status,
    })
    db.collection('users').document(uid).update({
        'username': username, 'displayName': display_name, 'status': status,
    })
    db.collection('admin_activity').add({
        'adminId': session.get('admin_id'), 'action': 'Update Cashier',
        'timestamp': datetime.now(), 'details': f'Updated cashier: {username}',
        'ownerId': session.get('admin_id'),
    })
    flash("Cashier updated successfully!", "success")
    return redirect(url_for('delete_cashier'))


@app.route('/change_admin_password', methods=['GET', 'POST'])
@admin_required
def change_admin_password():
    owner_id = _current_owner_id()
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
    if request.method == 'POST':
        # Google-auth only: here we just record a security settings change.
        db.collection('admin_activity').add({
            'adminId': session.get('admin_id'), 'action': 'Security Settings Update',
            'timestamp': datetime.now(), 'details': 'Admin updated security settings',
            'ownerId': session.get('admin_id'),
        })
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
        db.collection('store_settings').document(f'config_{owner_id}').set(settings)
        flash("Receipt settings saved successfully!", "success")

    settings_doc = db.collection('store_settings').document(f'config_{owner_id}').get()
    settings = settings_doc.to_dict() if settings_doc.exists else DEFAULT_STORE_SETTINGS
    low_stock_count = product_repository.count_low_stock(owner_id=owner_id)
    expiring_count = product_repository.count_expiring(EXPIRING_DAYS, owner_id=owner_id)
    return render_template('receipt_customization.html', settings=settings,
                           low_stock_count=low_stock_count, expiring_count=expiring_count,
                           active_main='management', active_sub='receipt_customization')


def get_store_settings(owner_id: Optional[str] = None):
    doc_id = f'config_{owner_id}' if owner_id else 'config'
    doc = db.collection('store_settings').document(doc_id).get()
    return doc.to_dict() if doc.exists else DEFAULT_STORE_SETTINGS


# ---------------------------------------------------------------
# Backup / Restore (Firestore export info)
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
                           project_id=FIREBASE_WEB_CONFIG.get('projectId', ''))


@app.route('/backup_database', methods=['POST'])
@admin_required
def backup_database():
    owner_id = _current_owner_id()
    export = {}
    for col in ['products', 'sales', 'stock_movements', 'cashiers', 'users']:
        export[col] = [d.to_dict() | {'__id': d.id} for d in
                       db.collection(col).where('ownerId', '==', owner_id).stream()]
    for col in ['categories', 'store_settings']:
        export[col] = [d.to_dict() | {'__id': d.id} for d in db.collection(col).stream()]
    mem = BytesIO()
    mem.write(json.dumps(export, default=str, indent=2).encode('utf-8'))
    mem.seek(0)
    try:
        return send_file(mem, as_attachment=True,
                         download_name=f"brewpos_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                         mimetype='application/json')
    except TypeError:
        return send_file(mem, as_attachment=True,
                         attachment_filename=f"brewpos_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
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
        for col, rows in data.items():
            for row in rows:
                doc_id = row.pop('__id', None)
                if col in ['products', 'sales', 'stock_movements', 'cashiers', 'users']:
                    row['ownerId'] = owner_id
                if doc_id:
                    db.collection(col).document(doc_id).set(row)
                else:
                    db.collection(col).add(row)
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
    sales = list(db.collection('sales').where('cashierId', '==', cid).stream())
    sales = [s for s in sales if s.to_dict().get('ownerId') == owner_id]
    today_total = sum(float(s.to_dict().get('total', 0)) for s in sales
                      if _to_naive(s.to_dict().get('date')) >= today_start)
    today_count = sum(1 for s in sales
                      if _to_naive(s.to_dict().get('date')) >= today_start)

    recent = sorted(sales, key=lambda s: _to_naive(s.to_dict().get('date')), reverse=True)[:5]
    recent_sales = [s.to_dict() | {'id': s.id} for s in recent]

    owner_id = _current_owner_id()
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
    for s in db.collection('sales').where('cashierId', '==', cid).stream():
        sdoc = s.to_dict()
        if sdoc.get('ownerId') != owner_id:
            continue
        dt = _to_naive(sdoc.get('date'))
        if dt < today_start:
            continue
        items = [it.to_dict() for it in s.reference.collection('items').stream()]
        sales_data.append({
            'id': s.id, 'receipt_number': sdoc.get('receiptNo'),
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
    doc = db.collection('products').document(product_id).get()
    if not doc.exists or doc.to_dict().get('ownerId') != owner_id:
        return jsonify({'success': False, 'message': 'Product not found'}), 404
    update_data = {
        'name': clean_input(data.get('name')),
        'barcode': clean_input(data.get('barcode')),
        'category': clean_input(data.get('category')),
        'price': float(data.get('price', 0)),
        'stock': {'quantity': int(data.get('stock', 0)), 'minimum': int(data.get('stockMinimum', 0))},
        'expirationDate': data.get('expirationDate'),
        'updatedAt': datetime.now(),
    }
    product_repository.update(product_id, update_data)
    db.collection('admin_activity').add({
        'adminId': session.get('admin_id'), 'action': 'Edit Product',
        'timestamp': datetime.now(), 'details': f"Edited product: {update_data['name']}",
        'ownerId': session.get('admin_id'),
    })
    return jsonify({'success': True, 'message': 'Product updated successfully!'})


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

    # Validate cup-size pricing + totals client-independent
    total_amount = 0.0
    for item in items:
        try:
            price = float(item.get('price', 0))
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
        db.collection('admin_activity').add({
            'adminId': session.get('admin_id'), 'action': 'Admin Logout',
            'timestamp': datetime.now(),
            'ownerId': session.get('admin_id'),
        })
    session.clear()
    return redirect(url_for('google_login'))


@app.route('/cashier_logout')
def cashier_logout():
    if session.get('role') == 'cashier':
        db.collection('cashier_activity').add({
            'cashierId': session.get('cashier_id'), 'action': 'Logout',
            'timestamp': datetime.now(),
            'ownerId': session.get('admin_id'),
        })
    session.clear()
    return redirect(url_for('google_login'))


@app.route('/logout')
def logout():
    session.pop('role', None)
    session.pop('admin_user', None)
    session.pop('admin_id', None)
    session.pop('cashier_user', None)
    session.pop('cashier_id', None)
    return redirect(url_for('select_role'))


# ---------------------------------------------------------------
# Static product images
# ---------------------------------------------------------------
@app.route('/static/images/products/<path:filename>')
def product_image(filename):
    return send_from_directory(os.path.join(app.static_folder, 'images', 'products'), filename)


if __name__ == '__main__':
    # Seed default categories if missing
    try:
        for cat in DEFAULT_CATEGORIES:
            existing = list(db.collection('categories').where('name', '==', cat).limit(1).stream())
            if not existing:
                db.collection('categories').add({'name': cat, 'createdAt': datetime.now()})
        # Seed default store settings if missing
        if not db.collection('store_settings').document('config').get().exists:
            db.collection('store_settings').document('config').set(DEFAULT_STORE_SETTINGS)
    except Exception as e:
        print(f"Seed error: {e}")
    app.run(debug=True, host='0.0.0.0', port=5000)
