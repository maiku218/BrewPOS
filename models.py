"""BrewPOS - OOP domain models.

Demonstrates the four OOP pillars:
  - Encapsulation: private fields with validated getters/setters
  - Inheritance: User -> Admin, Cashier
  - Polymorphism: StockMovementProcessor hierarchy, IAuthService/ISalesService
  - Abstraction: IEntity / IRepository abstract base classes
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional


# ================================================
# PILLAR 4: ABSTRACTION
# ================================================

class IEntity(ABC):
    @property
    @abstractmethod
    def id(self):
        ...


class IRepository(ABC):
    @abstractmethod
    def get_all(self):
        ...

    @abstractmethod
    def get_by_id(self, entity_id):
        ...

    @abstractmethod
    def save(self, entity):
        ...

    @abstractmethod
    def delete(self, entity_id: str) -> bool:
        ...


# ================================================
# PILLAR 1: ENCAPSULATION
# ================================================

class StockMovement:
    """Encapsulation: _movement_type, _quantity and _reason are private and
    can only be set through the constructor with validation."""

    IN = 'IN'
    OUT = 'OUT'

    def __init__(self, movement_id: str, product_id: str, movement_type: str,
                 quantity: int, reason: str, timestamp: Optional[datetime] = None):
        if movement_type not in (self.IN, self.OUT):
            raise ValueError("movement_type must be 'IN' or 'OUT'")
        if quantity <= 0:
            raise ValueError("quantity must be greater than 0")

        self._id = movement_id
        self._product_id = product_id
        self._movement_type = movement_type
        self._quantity = int(quantity)
        self._reason = reason
        self._timestamp = timestamp or datetime.now()

    @property
    def id(self) -> str:
        return self._id

    @property
    def product_id(self) -> str:
        return self._product_id

    @property
    def movement_type(self) -> str:
        return self._movement_type

    @property
    def quantity(self) -> int:
        return self._quantity

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def timestamp(self) -> datetime:
        return self._timestamp

    def is_inbound(self) -> bool:
        return self._movement_type == self.IN

    def is_outbound(self) -> bool:
        return self._movement_type == self.OUT

    def to_dict(self) -> dict:
        return {
            'productId': self._product_id,
            'movementType': self._movement_type,
            'quantity': self._quantity,
            'reason': self._reason,
            'timestamp': self._timestamp,
        }

    def __repr__(self):
        return f"<StockMovement id={self._id} type={self._movement_type} qty={self._quantity}>"


class Product(IEntity):
    """Encapsulation: _price and _stock are private with validated setters."""

    def __init__(self, product_id: str, name: str, barcode: str, category: str,
                 description: str = '', image_url: str = '', status: str = 'Available',
                 pricing_type: str = 'single', price: float = 0,
                 variants: Optional[list] = None, addons: Optional[list] = None,
                 stock_quantity: int = 0, stock_minimum: int = 0,
                 expiration_date: Optional[datetime] = None,
                 created_at: Optional[datetime] = None, updated_at: Optional[datetime] = None,
                 owner_id: Optional[str] = None, image_path: str = ''):
        self._id = product_id
        self._name = name
        self._barcode = barcode
        self._category = category
        self._description = description
        self._image_url = image_url
        self._image_path = image_path
        self._status = status
        self._pricing_type = pricing_type
        self._price = float(price)
        self._variants = variants or []
        self._addons = addons or []
        self._stock_quantity = int(stock_quantity)
        self._stock_minimum = int(stock_minimum)
        self._expiration_date = expiration_date
        self._created_at = created_at or datetime.now()
        self._updated_at = updated_at or datetime.now()
        self._owner_id = owner_id

    @property
    def id(self) -> str:
        return self._id

    @property
    def barcode(self) -> str:
        return self._barcode

    @property
    def category(self) -> str:
        return self._category

    @property
    def description(self) -> str:
        return self._description

    @property
    def image_url(self) -> str:
        return self._image_url

    @property
    def image_path(self) -> str:
        return self._image_path

    @property
    def status(self) -> str:
        return self._status

    @property
    def pricing_type(self) -> str:
        return self._pricing_type

    @property
    def price(self) -> float:
        return self._price

    @property
    def variants(self) -> list:
        return self._variants

    @property
    def addons(self) -> list:
        return self._addons

    @property
    def stock_quantity(self) -> int:
        return self._stock_quantity

    @property
    def stock_minimum(self) -> int:
        return self._stock_minimum

    @property
    def expiration_date(self) -> Optional[datetime]:
        return self._expiration_date

    @property
    def created_at(self) -> datetime:
        return self._created_at

    @property
    def updated_at(self) -> datetime:
        return self._updated_at

    @property
    def owner_id(self) -> Optional[str]:
        return self._owner_id

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str):
        if not value or not value.strip():
            raise ValueError("Product name cannot be empty.")
        self._name = value.strip()

    @property
    def stock(self) -> int:
        return self._stock_quantity

    @stock.setter
    def stock(self, value):
        value = int(value)
        if value < 0:
            raise ValueError("Stock cannot be negative.")
        self._stock_quantity = value

    @property
    def is_low_stock(self) -> bool:
        return self._stock_quantity <= self._stock_minimum

    @property
    def is_out_of_stock(self) -> bool:
        return self._stock_quantity <= 0

    def apply_movement(self, quantity: int, movement_type: str = StockMovement.IN):
        if movement_type == StockMovement.IN:
            self._stock_quantity += quantity
        elif movement_type == StockMovement.OUT:
            if self._stock_quantity < quantity:
                raise ValueError(
                    f"Insufficient stock for '{self._name}'. "
                    f"Available: {self._stock_quantity}, Requested: {quantity}"
                )
            self._stock_quantity -= quantity
        else:
            raise ValueError(f"Invalid movement type: {movement_type}")

    def to_dict(self) -> dict:
        return {
            'barcode': self._barcode,
            'name': self._name,
            'category': self._category,
            'description': self._description,
            'imageUrl': self._image_url,
            'imagePath': self._image_path,
            'status': self._status,
            'pricingType': self._pricing_type,
            'price': self._price,
            'variants': self._variants,
            'addons': self._addons,
            'stock': {
                'quantity': self._stock_quantity,
                'minimum': self._stock_minimum,
            },
            'expirationDate': self._expiration_date,
            'createdAt': self._created_at,
            'updatedAt': self._updated_at,
            'ownerId': self._owner_id,
        }

    def __repr__(self):
        return f"<Product id={self._id} name='{self._name}' price={self._price} stock={self._stock_quantity}>"


# ================================================
# PILLAR 2: INHERITANCE
# ================================================

class User(IEntity):
    """Base user. Admin and Cashier inherit from this."""

    def __init__(self, uid: str, email: str, display_name: str, role: str):
        self._id = uid
        self._email = email
        self._display_name = display_name
        self._role = role

    @property
    def id(self) -> str:
        return self._id

    @property
    def email(self) -> str:
        return self._email

    @property
    def display_name(self) -> str:
        return self._display_name

    @property
    def role(self) -> str:
        return self._role

    def authenticate(self, id_token: str) -> bool:
        """Polymorphic authentication method (overridden by subclasses)."""
        return bool(id_token)


class Admin(User):
    """Inheritance: Admin extends User."""

    Role = 'admin'

    def authenticate(self, id_token: str) -> bool:
        # Admin verification is performed by the backend; here we simply
        # confirm a token is supplied.
        return bool(id_token)


class Cashier(User):
    """Inheritance: Cashier extends User with an additional 'status' field."""

    Role = 'cashier'

    def __init__(self, uid: str, email: str, display_name: str, status: str = 'active'):
        super().__init__(uid, email, display_name, 'cashier')
        self._status = status

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str):
        if value not in ('active', 'inactive'):
            raise ValueError("Status must be 'active' or 'inactive'.")
        self._status = value

    @property
    def is_active(self) -> bool:
        return self._status == 'active'

    def authenticate(self, id_token: str) -> bool:
        return bool(id_token) and self.is_active


# ================================================
# PILLAR 3: POLYMORPHISM
# ================================================

class StockMovementProcessor:
    """Abstract processor. Subclasses implement process()."""

    def __init__(self, movement: StockMovement, db):
        self.movement = movement
        self.db = db

    def process(self, product: Product):
        raise NotImplementedError


class StockInProcessor(StockMovementProcessor):
    def process(self, product: Product):
        product.apply_movement(self.movement.quantity, StockMovement.IN)
        self._record(product)

    def _record(self, product: Product):
        self.db.collection('products').document(product.id).update({
            'stock': {'quantity': product.stock_quantity, 'minimum': product.stock_minimum}
        })
        self.db.collection('stock_movements').add(self.movement.to_dict())


class StockOutProcessor(StockMovementProcessor):
    def process(self, product: Product):
        product.apply_movement(self.movement.quantity, StockMovement.OUT)
        self._record(product)

    def _record(self, product: Product):
        self.db.collection('products').document(product.id).update({
            'stock': {'quantity': product.stock_quantity, 'minimum': product.stock_minimum}
        })
        self.db.collection('stock_movements').add(self.movement.to_dict())


def get_movement_processor(movement: StockMovement, db) -> StockMovementProcessor:
    """Factory returning the correct polymorphic processor."""
    if movement.is_inbound():
        return StockInProcessor(movement, db)
    elif movement.is_outbound():
        return StockOutProcessor(movement, db)
    raise ValueError(f"Unknown movement type: {movement.movement_type}")
