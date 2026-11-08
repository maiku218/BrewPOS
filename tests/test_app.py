"""BrewPOS - Basic unit tests (no live PostgreSQL required).

These tests verify module imports, model behavior, and helper functions
without needing a live database connection. Database-dependent route tests
require a configured PostgreSQL instance (see README.md).
"""

import sys
import os
import uuid
from datetime import datetime

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import Product, StockMovement, User, Admin, Cashier


def test_product_creation():
    p = Product(
        product_id="p1",
        name="Latte",
        barcode="123456",
        category="Coffee",
        price=4.50,
        stock_quantity=10,
        stock_minimum=2,
        owner_id="owner1",
    )
    assert p.id == "p1"
    assert p.name == "Latte"
    assert p.price == 4.50
    assert p.stock_quantity == 10
    assert not p.is_low_stock
    assert not p.is_out_of_stock
    print("PASS: test_product_creation")


def test_product_low_stock():
    p = Product(
        product_id="p2", name="Tea", barcode="", category="Tea",
        price=3.0, stock_quantity=1, stock_minimum=5, owner_id="owner1",
    )
    assert p.is_low_stock
    assert not p.is_out_of_stock
    print("PASS: test_product_low_stock")


def test_product_out_of_stock():
    p = Product(
        product_id="p3", name="Water", barcode="", category="Others",
        price=2.0, stock_quantity=0, stock_minimum=0, owner_id="owner1",
    )
    assert p.is_out_of_stock
    print("PASS: test_product_out_of_stock")


def test_stock_movement():
    m = StockMovement(
        movement_id="m1", product_id="p1", movement_type="IN",
        quantity=5, reason="Restock",
    )
    assert m.movement_type == "IN"
    assert m.is_inbound()
    assert not m.is_outbound()
    d = m.to_dict()
    assert d["productId"] == "p1"
    assert d["quantity"] == 5
    print("PASS: test_stock_movement")


def test_user_admin_cashier():
    admin = Admin(uid="a1", email="admin@brewpos.local", display_name="Admin", role="admin")
    assert admin.role == "admin"
    assert admin.id == "a1"

    cashier = Cashier(uid="c1", email="cashier@brewpos.local", display_name="Cashier", status="active")
    assert cashier.role == "cashier"
    assert cashier.is_active
    cashier.status = "inactive"
    assert not cashier.is_active
    print("PASS: test_user_admin_cashier")


def test_product_to_dict():
    p = Product(
        product_id="p4", name="Cappuccino", barcode="", category="Coffee",
        price=5.0, stock_quantity=7, stock_minimum=3,
        variants=[{"name": "Small", "price": 4.0}, {"name": "Large", "price": 6.0}],
        addons=[{"name": "Extra Shot", "price": 1.0}],
        owner_id="owner1",
    )
    d = p.to_dict()
    assert d["name"] == "Cappuccino"
    assert d["price"] == 5.0
    assert len(d["variants"]) == 2
    assert len(d["addons"]) == 1
    assert d["ownerId"] == "owner1"
    print("PASS: test_product_to_dict")


def test_module_imports():
    import db
    import services
    import models
    assert hasattr(db, "init_db")
    assert hasattr(db, "get_connection")
    assert hasattr(services, "AuthService")
    assert hasattr(services, "ProductRepository")
    assert hasattr(services, "SalesService")
    assert hasattr(models, "Product")
    print("PASS: test_module_imports")


def test_uuid_generation():
    u1 = uuid.uuid4()
    u2 = uuid.uuid4()
    assert u1 != u2
    assert str(u1)  # non-empty string
    print("PASS: test_uuid_generation")


if __name__ == "__main__":
    test_product_creation()
    test_product_low_stock()
    test_product_out_of_stock()
    test_stock_movement()
    test_user_admin_cashier()
    test_product_to_dict()
    test_module_imports()
    test_uuid_generation()
    print("\nAll basic tests passed.")
