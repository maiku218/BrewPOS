# ☕ BrewPOS — Coffee Shop POS & Inventory System

A complete **Coffee Shop Point-of-Sale and Inventory Management System** built with
**Flask (Python)** and **PostgreSQL**. Pure coffee-focused — no medical/pharmacy references.
Authentication uses local email/password accounts with secure password hashing.

---

## Features

- **Local Authentication** — email/password accounts with `werkzeug` password hashing.
  The first registered account becomes the **admin**; subsequent accounts are **cashiers**.
  Admins can register additional cashiers via Staff Management.
- **POS / Cashier interface** — 3-column layout (sidebar · menu/cart · cart panel),
  product cards, **cup-size pricing** (S = base, M = +₱10, L = +₱20), quantity
  selector, checkout with payment modal, printable 58mm receipt.
- **Admin dashboard** — activity monitor, cashier logs, product catalog (card view),
  add/edit/delete products with image URLs, sales analytics (Chart.js), coffee vs
  pastry reports, low-stock & expiring alerts, staff management, receipt
  customization, and JSON backup/restore.
- **PostgreSQL data** — all application data lives in relational PostgreSQL tables.

## Database Collections (Firestore) → PostgreSQL Tables

| Firebase collection | PostgreSQL table |
|---------------------|------------------|
| `users` + `cashiers` | `users` (role distinguishes admin vs cashier) |
| `categories` | `categories` |
| `products` | `products` |
| `stock_movements` | `stock_movements` |
| `sales` + `sales/{id}/items` | `sales` + `sale_items` |
| `store_settings` | `store_settings` |
| `admin_activity` | `admin_activity` |
| `cashier_activity` | `cashier_activity` |

Multi-tenancy is enforced by `owner_id` on every tenant-scoped table.

---

## Setup

### 1. Install PostgreSQL

Install PostgreSQL (https://www.postgresql.org/download/) and create a database:

```sql
CREATE DATABASE brewpos;
CREATE USER brewpos_user WITH PASSWORD 'your_password';
GRANT ALL PRIVILEGES ON DATABASE brewpos TO brewpos_user;
```

### 2. Configure environment

Copy `.env.example` to `.env` and fill in your PostgreSQL credentials:

```bash
cp .env.example .env
# edit .env with your DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
```

### 3. Load the schema

```bash
psql -U postgres -d brewpos -f schema.sql
```

Or let the app initialize it automatically on first run (db.init_db() creates
tables and seeds default categories/settings).

### 4. Install & run

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000. Register your first account (becomes admin), then
sign in. Admins can register cashiers via Staff Management → Register Cashier.

### 5. Build Windows .exe (optional)

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --add-data "templates;templates" --add-data "static;static" app.py
```

---

## Migration from Firebase

A migration script is provided to copy existing Firestore data into PostgreSQL:

```bash
python migrate_firebase_to_postgres.py
```

Prerequisites:
- PostgreSQL running with the schema loaded (see step 3 above).
- Firebase service-account credentials available via `FIREBASE_CREDENTIALS_JSON`,
  `FIREBASE_CREDENTIALS_JSON_BASE64`, or `firebase_credentials.json`.

The script is read-only against Firestore and uses `ON CONFLICT DO NOTHING` so
it is safe to re-run without duplicating data.

See `schema.sql` for the full schema and `migrate_firebase_to_postgres.py` for
usage details.

---

## OOP Architecture

- **Encapsulation** — `Product._price`, `Product._stock`, `StockMovement._movement_type`
  with validated getters/setters (`models.py`).
- **Inheritance** — `User` → `Admin`, `Cashier` (adds `status`).
- **Polymorphism** — `StockMovementProcessor` / `StockInProcessor` / `StockOutProcessor`;
  `IAuthService` / `ISalesService` interfaces.
- **Abstraction** — `IEntity`, `IRepository` abstract bases; `ProductRepository` hides
  all PostgreSQL access behind `Product` objects; service classes abstract business logic.

---

## Project Structure

```
BrewPOS/
├── app.py                      # Flask application + routes
├── models.py                   # OOP domain models
├── services.py                 # AuthService, ProductRepository, SalesService (PostgreSQL)
├── db.py                       # PostgreSQL connection layer + schema init
├── schema.sql                  # PostgreSQL schema (tables, indexes, constraints)
├── migrate_firebase_to_postgres.py  # Firebase → PostgreSQL data migration script
├── requirements.txt
├── brewpos.spec                # PyInstaller config
├── .env.example                # PostgreSQL environment config example
├── static/{css,js,images/products}
└── templates/                  # login, admin_*, cashier_*, sales_*, inventory_*, etc.
```

## Security

- Database credentials are read from environment variables only (never hardcoded).
- All queries use parameterized statements (`%s` placeholders via psycopg2) — no SQL injection.
- Passwords are hashed with `werkzeug.security` (`generate_password_hash` /
  `check_password_hash`); plain-text passwords are never stored.
- Sessions use `HttpOnly` cookies with a 30-minute lifetime.
