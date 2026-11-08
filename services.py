"""BrewPOS - Service layer for PostgreSQL.

Replaces services.py (Firestore). Implements the same abstraction interfaces
(IAuthService, ISalesService) and ProductRepository, but all data access is
now parameterized SQL against PostgreSQL via db.py.
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Dict
import random
import uuid
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash, check_password_hash

from db import (
    get_connection, transaction, query_all, query_one, execute,
    execute_returning, now, LOW_STOCK_THRESHOLD, EXPIRING_DAYS,
)
from models import (
    User, Admin, Cashier, Product, StockMovement,
    StockInProcessor, StockOutProcessor, get_movement_processor,
)


# ============================================================================
# ABSTRACTION: Service interfaces
# ============================================================================

class IAuthService(ABC):
    @abstractmethod
    def authenticate(self, email: str, password: str) -> Optional[dict]:
        ...

    @abstractmethod
    def create_user(self, email: str, password: str, display_name: str,
                    role: str = 'cashier') -> Optional[dict]:
        ...

    @abstractmethod
    def get_user_by_id(self, user_id: str) -> Optional[dict]:
        ...

    @abstractmethod
    def get_user_by_email(self, email: str) -> Optional[dict]:
        ...

    @abstractmethod
    def get_session_data(self, user: dict) -> dict:
        ...


class ISalesService(ABC):
    @abstractmethod
    def process_sale(self, items: list, cashier_id: str, owner_id: str) -> dict:
        ...

    @abstractmethod
    def mark_receipt_printed(self, receipt_number: str, owner_id: str) -> bool:
        ...

    @abstractmethod
    def cancel_pending_sale(self, receipt_number: str, owner_id: str) -> bool:
        ...


# ============================================================================
# IMPLEMENTATIONS
# ============================================================================

class AuthService(IAuthService):
    """Handles local email/password authentication and user management."""

    def authenticate(self, email: str, password: str) -> Optional[dict]:
        """Verify email/password and return the user dict, or None."""
        user = self.get_user_by_email(email)
        if not user:
            return None
        password_hash = user.get('password_hash')
        if not password_hash or not check_password_hash(password_hash, password):
            return None
        if user.get('status') != 'active':
            return None
        return user

    def create_user(self, email: str, password: str, display_name: str,
                    role: str = 'cashier') -> Optional[dict]:
        """Create a new user. Returns the user dict or None if email exists."""
        existing = self.get_user_by_email(email)
        if existing:
            return None
        user_id = str(uuid.uuid4())
        password_hash = generate_password_hash(password)
        try:
            execute(
                "INSERT INTO users (id, email, password_hash, display_name, role, status, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (user_id, email, password_hash, display_name, role, 'active', now(), now()),
            )
        except Exception:
            return None
        return self.get_user_by_id(user_id)

    def get_user_by_id(self, user_id: str) -> Optional[dict]:
        return query_one(
            "SELECT id, email, password_hash, display_name, role, status, created_at, updated_at "
            "FROM users WHERE id = %s", (user_id,)
        )

    def get_user_by_email(self, email: str) -> Optional[dict]:
        return query_one(
            "SELECT id, email, password_hash, display_name, role, status, created_at, updated_at "
            "FROM users WHERE email = %s", (email,)
        )

    def get_session_data(self, user: dict) -> dict:
        if user.get('role') == 'admin':
            return {
                'admin_user': user.get('email'),
                'admin_id': user.get('id'),
                'role': 'admin',
            }
        return {
            'cashier_user': user.get('email'),
            'cashier_id': user.get('id'),
            'role': 'cashier',
        }


class ProductRepository:
    """Hides all PostgreSQL access; clients work with Product objects."""

    def __init__(self):
        pass

    def _row_to_product(self, row: dict) -> Product:
        variants = row.get('variants') or []
        if not isinstance(variants, list):
            variants = []
        addons = row.get('addons') or []
        if not isinstance(addons, list):
            addons = []
        normalized_addons = []
        for a in addons:
            if isinstance(a, str):
                normalized_addons.append({'name': a, 'price': 0})
            elif isinstance(a, dict):
                normalized_addons.append({
                    'name': a.get('name', ''),
                    'price': float(a.get('price', 0) or 0),
                })
        addons = normalized_addons
        expiration = row.get('expiration_date')
        if isinstance(expiration, str):
            try:
                expiration = datetime.strptime(expiration, '%Y-%m-%d')
            except ValueError:
                expiration = None
        return Product(
            product_id=row['id'],
            name=row.get('name', ''),
            barcode=row.get('barcode') or '',
            category=row.get('category') or '',
            description=row.get('description') or '',
            image_url=row.get('image_url') or '',
            image_path=row.get('image_path') or '',
            status=row.get('status', 'Available'),
            pricing_type=row.get('pricing_type', 'single'),
            price=float(row.get('price') or 0),
            variants=variants,
            addons=addons,
            stock_quantity=int(row.get('stock_quantity') or 0),
            stock_minimum=int(row.get('stock_minimum') or 0),
            expiration_date=expiration,
            created_at=row.get('created_at'),
            updated_at=row.get('updated_at'),
            owner_id=row.get('owner_id'),
        )

    def find_by_id(self, product_id: str, owner_id: Optional[str] = None) -> Optional[Product]:
        if owner_id:
            row = query_one(
                "SELECT * FROM products WHERE id = %s AND owner_id = %s",
                (product_id, owner_id),
            )
        else:
            row = query_one(
                "SELECT * FROM products WHERE id = %s", (product_id,)
            )
        if not row:
            return None
        return self._row_to_product(row)

    def find_by_barcode(self, barcode: str, owner_id: Optional[str] = None) -> Optional[Product]:
        if owner_id:
            rows = query_all(
                "SELECT * FROM products WHERE barcode = %s AND owner_id = %s LIMIT 1",
                (barcode, owner_id),
            )
        else:
            rows = query_all(
                "SELECT * FROM products WHERE barcode = %s LIMIT 1",
                (barcode,),
            )
        if not rows:
            return None
        return self._row_to_product(rows[0])

    def find_all(self, owner_id: Optional[str] = None) -> List[Product]:
        if not owner_id:
            raise ValueError("owner_id is required for find_all")
        rows = query_all(
            "SELECT * FROM products WHERE owner_id = %s ORDER BY name",
            (owner_id,),
        )
        return [self._row_to_product(r) for r in rows]

    def find_in_stock(self, owner_id: Optional[str] = None) -> List[Product]:
        return [p for p in self.find_all(owner_id) if p.stock_quantity > 0]

    def find_low_stock(self, threshold: int = LOW_STOCK_THRESHOLD,
                       owner_id: Optional[str] = None) -> List[Product]:
        rows = query_all(
            "SELECT * FROM products WHERE owner_id = %s AND stock_quantity <= stock_minimum "
            "ORDER BY name",
            (owner_id,),
        )
        return [self._row_to_product(r) for r in rows]

    def save(self, product: Product, product_id: Optional[str] = None):
        pid = product_id or product.id
        data = product.to_dict()
        data['id'] = pid
        data['owner_id'] = product.owner_id
        self._upsert_product(data)
        return pid

    def save_from_dict(self, data: dict, product_id: Optional[str] = None):
        pid = product_id or data.get('id') or str(uuid.uuid4())
        data['id'] = pid
        self._upsert_product(data)
        return pid

    def update(self, product_id: str, data: dict):
        self._upsert_product(data, product_id=product_id)

    def delete(self, product_id: str):
        execute("DELETE FROM products WHERE id = %s", (product_id,))

    def count_low_stock(self, threshold: int = LOW_STOCK_THRESHOLD,
                        owner_id: Optional[str] = None) -> int:
        return len(self.find_low_stock(threshold, owner_id))

    def count_expiring(self, days: int, owner_id: Optional[str] = None) -> int:
        threshold = datetime.now() + timedelta(days=days)
        row = query_one(
            "SELECT COUNT(*) AS cnt FROM products WHERE owner_id = %s "
            "AND expiration_date IS NOT NULL AND expiration_date <= %s",
            (owner_id, threshold.date() if hasattr(threshold, 'date') else threshold),
        )
        return int(row['cnt']) if row else 0

    def link_stock_movement(self, product: Product, movement: StockMovement):
        processor = get_movement_processor(movement, self)
        processor.process(product)

    # ------------------------------------------------------------------
    # Internal upsert
    # ------------------------------------------------------------------
    def _upsert_product(self, data: dict, product_id: Optional[str] = None):
        pid = product_id or data.get('id')
        stock = data.get('stock')
        if isinstance(stock, dict):
            stock_qty = int(stock.get('quantity', 0) or 0)
            stock_min = int(stock.get('minimum', 0) or 0)
        else:
            stock_qty = int(data.get('stock_quantity', stock or 0) or 0)
            stock_min = int(data.get('stock_minimum', 0) or 0)

        variants = data.get('variants', [])
        if not isinstance(variants, list):
            variants = []
        addons = data.get('addons', [])
        if not isinstance(addons, list):
            addons = []

        expiration = data.get('expirationDate') or data.get('expiration_date')
        if isinstance(expiration, str):
            try:
                expiration = datetime.strptime(expiration, '%Y-%m-%d').date()
            except ValueError:
                expiration = None
        elif isinstance(expiration, datetime):
            expiration = expiration.date()

        now_ts = now()
        created_at = data.get('createdAt') or data.get('created_at') or now_ts
        updated_at = data.get('updatedAt') or data.get('updated_at') or now_ts

        execute(
            "INSERT INTO products (id, name, barcode, category, description, image_url, image_path, "
            "status, pricing_type, price, variants, addons, stock_quantity, stock_minimum, "
            "expiration_date, created_at, updated_at, owner_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET "
            "name = EXCLUDED.name, barcode = EXCLUDED.barcode, category = EXCLUDED.category, "
            "description = EXCLUDED.description, image_url = EXCLUDED.image_url, "
            "image_path = EXCLUDED.image_path, status = EXCLUDED.status, "
            "pricing_type = EXCLUDED.pricing_type, price = EXCLUDED.price, "
            "variants = EXCLUDED.variants, addons = EXCLUDED.addons, "
            "stock_quantity = EXCLUDED.stock_quantity, stock_minimum = EXCLUDED.stock_minimum, "
            "expiration_date = EXCLUDED.expiration_date, updated_at = EXCLUDED.updated_at",
            (
                pid,
                data.get('name', ''),
                data.get('barcode') or '',
                data.get('category') or '',
                data.get('description') or '',
                data.get('imageUrl') or data.get('image_url') or '',
                data.get('imagePath') or data.get('image_path') or '',
                data.get('status', 'Available'),
                data.get('pricingType') or data.get('pricing_type') or 'single',
                float(data.get('price', 0) or 0),
                Json(variants) if variants else '[]',
                Json(addons) if addons else '[]',
                stock_qty,
                stock_min,
                expiration,
                created_at,
                updated_at,
                data.get('ownerId') or data.get('owner_id'),
            ),
        )


class SalesService(ISalesService):
    """Processes a coffee-shop sale, deducting stock and recording the sale +
    its line items plus a stock movement per item, all in one transaction."""

    def process_sale(self, items: list, cashier_id: str, owner_id: str) -> dict:
        """items: list of dicts: {id, name, price, quantity, cupSize, productType}"""
        try:
            if not items:
                raise ValueError("No items in cart.")
            for item in items:
                if 'id' not in item or 'quantity' not in item:
                    raise ValueError("Invalid item data.")

            receipt_number = self._generate_receipt_number()
            now_ts = now()
            total_amount = 0.0
            sale_product_types = set()
            receipt_items = []
            product_cache = {}

            with transaction() as (conn, cur):
                # Validate stock and compute totals
                for item in items:
                    product = ProductRepository().find_by_id(item['id'], owner_id=owner_id)
                    if product is None:
                        raise ValueError(f"Product no longer exists: {item.get('name')}")
                    available = product.stock_quantity
                    requested = int(item['quantity'])
                    if available < requested:
                        raise ValueError(
                            f"Not enough stock for {product.name}. Available: {available}"
                        )
                    product_cache[item['id']] = product
                    sale_product_types.add(product.category if product.category else 'Coffee Drinks')
                    subtotal = round(float(item['price']) * requested, 2)
                    total_amount += subtotal
                    receipt_items.append({
                        'name': item['name'],
                        'price': float(item['price']),
                        'quantity': requested,
                        'cupSize': item.get('cupSize', 'S'),
                        'image': item.get('image', ''),
                        'addons': item.get('addons', []),
                        'subtotal': subtotal,
                    })

                product_type = 'Coffee' if 'Coffee Drinks' in sale_product_types else 'Pastry'

                sale_id = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO sales (id, receipt_no, cashier_id, owner_id, total, date, "
                    "product_type, status, receipt_printed, printed_at, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        sale_id, receipt_number, cashier_id, owner_id,
                        round(total_amount, 2), now_ts, product_type, 'Pending',
                        False, None, now_ts,
                    ),
                )

                for item in items:
                    product = product_cache[item['id']]
                    requested = int(item['quantity'])
                    new_stock = product.stock_quantity - requested
                    cur.execute(
                        "UPDATE products SET stock_quantity = %s, updated_at = %s WHERE id = %s",
                        (new_stock, now_ts, product.id),
                    )
                    cur.execute(
                        "INSERT INTO sale_items (sale_id, product_id, name, price, quantity, cup_size, image) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (
                            sale_id, product.id, item['name'],
                            float(item['price']), requested,
                            item.get('cupSize', 'S'), item.get('image', ''),
                        ),
                    )
                    cur.execute(
                        "INSERT INTO stock_movements (product_id, movement_type, quantity, reason, "
                        "timestamp, owner_id, receipt_no) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (
                            product.id, 'OUT', requested, 'Sale',
                            now_ts, owner_id, receipt_number,
                        ),
                    )

            return {
                'success': True,
                'receipt_number': receipt_number,
                'total': round(total_amount, 2),
                'tendered': 0,
                'change': 0,
                'items': receipt_items,
                'date': now_ts.strftime('%Y-%m-%d %H:%M:%S'),
            }
        except Exception as e:
            return {'success': False, 'message': str(e)}

    def mark_receipt_printed(self, receipt_number: str, owner_id: str) -> bool:
        try:
            row = query_one(
                "SELECT id, status FROM sales WHERE receipt_no = %s AND owner_id = %s "
                "AND status = 'Pending' FOR UPDATE", (receipt_number, owner_id)
            )
            if not row:
                return False
            execute(
                "UPDATE sales SET status = 'Completed', receipt_printed = TRUE, printed_at = %s "
                "WHERE id = %s", (now(), row['id'])
            )
            return True
        except Exception:
            return False

    def cancel_pending_sale(self, receipt_number: str, owner_id: str) -> bool:
        try:
            row = query_one(
                "SELECT id FROM sales WHERE receipt_no = %s AND owner_id = %s "
                "AND status = 'Pending'", (receipt_number, owner_id)
            )
            if not row:
                return False
            sale_id = row['id']

            with transaction() as (conn, cur):
                # Restore stock for each item
                cur.execute(
                    "SELECT product_id, quantity FROM sale_items WHERE sale_id = %s",
                    (sale_id,),
                )
                for si in cur.fetchall() or []:
                    cur.execute(
                        "UPDATE products SET stock_quantity = stock_quantity + %s, updated_at = %s "
                        "WHERE id = %s",
                        (int(si['quantity']), now(), si['product_id']),
                    )
                cur.execute("DELETE FROM sale_items WHERE sale_id = %s", (sale_id,))
                cur.execute("DELETE FROM sales WHERE id = %s", (sale_id,))

            execute(
                "INSERT INTO stock_movements (product_id, movement_type, quantity, reason, "
                "timestamp, owner_id, receipt_no) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                ('N/A', 'IN', 0, f'Cancelled pending sale {receipt_number}',
                 now(), owner_id, receipt_number),
            )
            return True
        except Exception:
            return False

    def _generate_receipt_number(self) -> str:
        return f"REC-{datetime.now().strftime('%Y%m%d')}-{random.randint(1000, 9999)}"
