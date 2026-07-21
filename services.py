"""BrewPOS - Service layer for Firestore.

Implements the abstraction interfaces (IAuthService, ISalesService) and the
ProductRepository which hides all Firestore access behind OOP models.
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Dict
import random
from datetime import datetime, timedelta

from firebase_config import get_db, LOW_STOCK_THRESHOLD
from models import (
    User, Admin, Cashier, Product, StockMovement,
    StockInProcessor, StockOutProcessor, get_movement_processor,
)


# ================================================
# ABSTRACTION: Service interfaces
# ================================================

class IAuthService(ABC):
    @abstractmethod
    def verify_google_user(self, id_token: str) -> Optional[dict]:
        ...

    @abstractmethod
    def get_or_create_user(self, claims: dict) -> dict:
        ...

    @abstractmethod
    def get_session_data(self, user_doc: dict) -> dict:
        ...


class ISalesService(ABC):
    @abstractmethod
    def process_sale(self, items: list, cashier_id: str) -> dict:
        ...


# ================================================
# IMPLEMENTATIONS
# ================================================

class AuthService(IAuthService):
    """Handles Google Sign-In verification and user document management."""

    def verify_google_user(self, id_token: str) -> Optional[dict]:
        """Verify a Google ID token, returning decoded claims or None."""
        try:
            from firebase_config import verify_id_token
            return verify_id_token(id_token)
        except Exception as e:
            print(f"[AUTH] Token verification failed: {type(e).__name__}: {e}")
            return None

    def get_or_create_user(self, claims: dict) -> dict:
        """Create the user doc on first Google sign-in; each new Google account gets its own workspace."""
        db = get_db()
        uid = claims.get('uid') or claims.get('sub')
        email = claims.get('email', '')
        display_name = claims.get('name', email.split('@')[0] if email else 'User')

        user_ref = db.collection('users').document(uid)
        user_doc = user_ref.get()

        if user_doc.exists:
            existing = user_doc.to_dict()
            if 'ownerId' not in existing:
                user_ref.update({'ownerId': uid})
                existing['ownerId'] = uid
            return existing

        user_data = {
            'uid': uid,
            'email': email,
            'displayName': display_name,
            'role': 'admin',
            'status': 'active',
            'createdAt': datetime.now(),
            'ownerId': uid,
        }
        user_ref.set(user_data)
        return user_data

    def get_session_data(self, user_doc: dict) -> dict:
        if user_doc.get('role') == 'admin':
            return {
                'admin_user': user_doc.get('email'),
                'admin_id': user_doc.get('uid'),
                'role': 'admin',
            }
        return {
            'cashier_user': user_doc.get('email'),
            'cashier_id': user_doc.get('uid'),
            'role': 'cashier',
        }


class ProductRepository:
    """Hides all Firestore access; clients work with Product objects."""

    def __init__(self, db=None):
        self.db = db or get_db()

    def find_by_id(self, product_id: str, owner_id: Optional[str] = None) -> Optional[Product]:
        doc = self.db.collection('products').document(product_id).get()
        if not doc.exists:
            return None
        product = self._doc_to_product(doc)
        if owner_id and product.owner_id != owner_id:
            return None
        return product

    def find_by_barcode(self, barcode: str, owner_id: Optional[str] = None) -> Optional[Product]:
        query = self.db.collection('products').where('barcode', '==', barcode).limit(1).stream()
        for doc in query:
            product = self._doc_to_product(doc)
            if owner_id and product.owner_id != owner_id:
                continue
            return product
        return None

    def find_all(self, owner_id: Optional[str] = None) -> List[Product]:
        if not owner_id:
            raise ValueError("owner_id is required for find_all")
        docs = self.db.collection('products').where('ownerId', '==', owner_id).stream()
        products = [self._doc_to_product(d) for d in docs]
        products.sort(key=lambda p: p.name)
        return products

    def find_in_stock(self, owner_id: Optional[str] = None) -> List[Product]:
        return [p for p in self.find_all(owner_id) if p.stock_quantity > 0]

    def find_low_stock(self, threshold: int = LOW_STOCK_THRESHOLD, owner_id: Optional[str] = None) -> List[Product]:
        return [p for p in self.find_all(owner_id) if p.is_low_stock]

    def save(self, product: Product, product_id: Optional[str] = None):
        if product_id:
            self.db.collection('products').document(product_id).set(product.to_dict())
            return product_id
        _, ref = self.db.collection('products').add(product.to_dict())
        return ref.id

    def save_from_dict(self, data: dict, product_id: Optional[str] = None):
        if product_id:
            self.db.collection('products').document(product_id).set(data)
            return product_id
        _, ref = self.db.collection('products').add(data)
        return ref.id

    def update(self, product_id: str, data: dict):
        self.db.collection('products').document(product_id).update(data)

    def delete(self, product_id: str):
        self.db.collection('products').document(product_id).delete()

    def count_low_stock(self, threshold: int = LOW_STOCK_THRESHOLD, owner_id: Optional[str] = None) -> int:
        return len(self.find_low_stock(threshold, owner_id))

    def count_expiring(self, days: int, owner_id: Optional[str] = None) -> int:
        threshold = datetime.now() + timedelta(days=days)
        count = 0
        for doc in self.db.collection('products').stream():
            data = doc.to_dict()
            if owner_id and data.get('ownerId') != owner_id:
                continue
            exp = data.get('expirationDate')
            if isinstance(exp, datetime) and exp <= threshold:
                count += 1
            elif exp:
                try:
                    if datetime.strptime(str(exp), '%Y-%m-%d') <= threshold:
                        count += 1
                except ValueError:
                    pass
        return count

    def link_stock_movement(self, product: Product, movement: StockMovement):
        processor = get_movement_processor(movement, self.db)
        processor.process(product)

    def _doc_to_product(self, doc) -> Product:
        data = doc.to_dict()
        exp = data.get('expirationDate')
        if isinstance(exp, datetime):
            exp_date = exp
        elif exp:
            try:
                exp_date = datetime.strptime(str(exp), '%Y-%m-%d')
            except ValueError:
                exp_date = None
        else:
            exp_date = None
        stock = data.get('stock', {})
        if isinstance(stock, dict):
            stock_qty = int(stock.get('quantity', 0))
            stock_min = int(stock.get('minimum', 0))
        else:
            stock_qty = int(stock) if stock is not None else 0
            stock_min = 0
        variants = data.get('variants', [])
        if not isinstance(variants, list):
            variants = []
        addons = data.get('addons', [])
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
        image_url = data.get('imageUrl', '') or data.get('image', '')
        image_path = data.get('imagePath', '')
        return Product(
            product_id=doc.id,
            name=data.get('name', ''),
            barcode=data.get('barcode', ''),
            category=data.get('category', ''),
            description=data.get('description', ''),
            image_url=image_url,
            image_path=image_path,
            status=data.get('status', 'Available'),
            pricing_type=data.get('pricingType', 'single'),
            price=data.get('price', 0),
            variants=variants,
            addons=addons,
            stock_quantity=stock_qty,
            stock_minimum=stock_min,
            expiration_date=exp_date,
            created_at=data.get('createdAt'),
            updated_at=data.get('updatedAt'),
            owner_id=data.get('ownerId'),
        )


class SalesService(ISalesService):
    """Processes a coffee-shop sale, deducting stock and recording the sale +
    its line items (as a subcollection) plus a stock movement per item."""

    def __init__(self, db=None):
        self.db = db or get_db()

    def process_sale(self, items: list, cashier_id: str, owner_id: str) -> dict:
        """items: list of dicts: {id, name, price, quantity, cupSize, productType}"""
        try:
            if not items:
                raise ValueError("No items in cart.")
            for item in items:
                if 'id' not in item or 'quantity' not in item:
                    raise ValueError("Invalid item data.")

            products_col = self.db.collection('products')
            receipt_items = []
            total_amount = 0.0
            sale_product_types = set()

            # Validate stock using base product stock.
            product_cache = {}
            for item in items:
                product_doc = products_col.document(item['id']).get()
                if not product_doc.exists:
                    raise ValueError(f"Product no longer exists: {item.get('name')}")
                pdata = product_doc.to_dict()
                stock_data = pdata.get('stock', {})
                if isinstance(stock_data, dict):
                    available = int(stock_data.get('quantity', 0))
                else:
                    available = int(stock_data) if stock_data is not None else 0
                requested = int(item['quantity'])
                if available < requested:
                    raise ValueError(
                        f"Not enough stock for {pdata.get('name')}. "
                        f"Available: {available}"
                    )
                product_cache[item['id']] = (product_doc, pdata)
                sale_product_types.add(pdata.get('category', 'Coffee Drinks'))
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

            # Determine product type: Coffee if any coffee item, else Pastry.
            product_type = 'Coffee' if 'Coffee Drinks' in sale_product_types else 'Pastry'

            receipt_number = self._generate_receipt_number()
            now = datetime.now()

            sale_ref = self.db.collection('sales').document()
            sale_ref.set({
                'receiptNo': receipt_number,
                'cashierId': cashier_id,
                'ownerId': owner_id,
                'total': round(total_amount, 2),
                'date': now,
                'productType': product_type,
                'status': 'Pending',
                'receiptPrinted': False,
                'printedAt': None,
            })

            batch = self.db.batch()
            for item in items:
                product_doc, pdata = product_cache[item['id']]
                requested = int(item['quantity'])
                stock_data = pdata.get('stock', {})
                if isinstance(stock_data, dict):
                    new_stock = int(stock_data.get('quantity', 0)) - requested
                else:
                    new_stock = int(stock_data) - requested if stock_data is not None else -requested
                batch.update(product_doc.reference, {'stock': {'quantity': new_stock, 'minimum': stock_data.get('minimum', 0) if isinstance(stock_data, dict) else 0}})
                sale_ref.collection('items').add({
                    'productId': item['id'],
                    'name': item['name'],
                    'price': float(item['price']),
                    'quantity': requested,
                    'cupSize': item.get('cupSize', 'S'),
                    'image': item.get('image', ''),
                })
                self.db.collection('stock_movements').add({
                    'productId': item['id'],
                    'ownerId': owner_id,
                    'movementType': 'OUT',
                    'quantity': requested,
                    'reason': 'Sale',
                    'timestamp': now,
                    'receiptNo': receipt_number,
                })

            batch.commit()

            return {
                'success': True,
                'receipt_number': receipt_number,
                'total': round(total_amount, 2),
                'tendered': 0,
                'change': 0,
                'items': receipt_items,
                'date': now.strftime('%Y-%m-%d %H:%M:%S'),
            }
        except Exception as e:
            return {'success': False, 'message': str(e)}

    def mark_receipt_printed(self, receipt_number: str, owner_id: str) -> bool:
        try:
            sales = list(self.db.collection('sales').where('receiptNo', '==', receipt_number).stream())
            for sale in sales:
                sdata = sale.to_dict()
                if sdata.get('status') != 'Pending':
                    continue
                if sdata.get('ownerId') != owner_id:
                    continue
                sale.reference.update({
                    'status': 'Completed',
                    'receiptPrinted': True,
                    'printedAt': datetime.now(),
                })
                return True
            return False
        except Exception:
            return False

    def cancel_pending_sale(self, receipt_number: str, owner_id: str) -> bool:
        try:
            sales = list(self.db.collection('sales').where('receiptNo', '==', receipt_number).stream())
            sale = None
            for s in sales:
                sdata = s.to_dict()
                if sdata.get('status') == 'Pending' and sdata.get('ownerId') == owner_id:
                    sale = s
                    break
            if not sale:
                return False
            items = list(sale.reference.collection('items').stream())
            batch = self.db.batch()

            for item in items:
                item_data = item.to_dict()
                product_id = item_data.get('productId')
                qty = int(item_data.get('quantity', 0))
                if product_id:
                    product_doc = self.db.collection('products').document(product_id).get()
                    if product_doc.exists:
                        pdata = product_doc.to_dict()
                        stock_data = pdata.get('stock', {})
                        if isinstance(stock_data, dict):
                            current_stock = int(stock_data.get('quantity', 0))
                        else:
                            current_stock = int(stock_data) if stock_data is not None else 0
                        new_stock = current_stock + qty
                        batch.update(product_doc.reference, {'stock': {'quantity': new_stock, 'minimum': stock_data.get('minimum', 0) if isinstance(stock_data, dict) else 0}})

            for item in items:
                batch.delete(item.reference)

            batch.delete(sale.reference)
            batch.commit()

            self.db.collection('stock_movements').add({
                'productId': 'N/A',
                'ownerId': owner_id,
                'movementType': 'IN',
                'quantity': 0,
                'reason': f'Cancelled pending sale {receipt_number}',
                'timestamp': datetime.now(),
            })

            return True
        except Exception:
            return False

    def _generate_receipt_number(self) -> str:
        return f"REC-{datetime.now().strftime('%Y%m%d')}-{random.randint(1000, 9999)}"
