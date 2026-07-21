# ☕ BrewPOS — Coffee Shop POS & Inventory System

A complete **Coffee Shop Point-of-Sale and Inventory Management System** built with
**Flask (Python)** and **Firebase Cloud Firestore**. Pure coffee-focused — no
medical/pharmacy references. Authentication uses **Google Sign-In**.

---

## Features

- **Google Sign-In** — users must sign in with Google before accessing the app.
  The first Google account becomes the **admin**; later accounts default to **cashier**.
  Sessions persist locally for auto-login on next launch.
- **POS / Cashier interface** — 3-column layout (sidebar · menu/cart · cart panel),
  product cards, **cup-size pricing** (S = base, M = +₱10, L = +₱20), quantity
  selector, checkout with payment modal, printable 58mm receipt.
- **Admin dashboard** — activity monitor, cashier logs, product catalog (card view),
  add/edit/delete products with image upload, sales analytics (Chart.js), coffee vs
  pastry reports, low-stock & expiring alerts, staff management, receipt
  customization, and JSON backup/restore.
- **Cloud data** — all collections live in Firestore (no local MySQL/XAMPP needed).

## Firebase Collections

`users`, `cashiers`, `cashier_activity`, `admin_activity`, `categories`, `products`,
`stock_movements`, `sales` (with `items` subcollection), `store_settings`.

Default categories: **Coffee Drinks**, **Pastries & Add-ons**.

---

## Setup

### 1. Create a Firebase project
- Go to https://console.firebase.google.com → **Add project** → name it `BrewPOS`.
- **Firestore Database** → Create database (test mode is fine for dev).
- **Authentication** → Sign-in method → enable **Google**.
- **Project settings** → **Your apps** → `</> Web app` → register `BrewPOS Web` →
  copy the `firebaseConfig` object.

### 2. Service account key (backend)
- **Project settings** → **Service accounts** → **Generate new private key**.
- Save the downloaded JSON as `firebase_credentials.json` in this folder
  (already git-ignored).
- Copy the web config values into `firebase_config.py` → `FIREBASE_WEB_CONFIG`
  (or set the `BREWPOS_FIREBASE_*` environment variables).

### 3. Install & run
```bash
pip install -r requirements.txt
python app.py
```
Open http://localhost:5000 and sign in with Google.

### 4. Build Windows .exe (optional)
```bash
pip install pyinstaller
pyinstaller --onefile --windowed --add-data "templates;templates" --add-data "static;static" --add-data "firebase_credentials.json;." app.py
```
Or use the provided `brewpos.spec`:
```bash
pyinstaller brewpos.spec
```

---

## OOP Architecture
- **Encapsulation** — `Product._price`, `Product._stock`, `StockMovement._movement_type`
  with validated getters/setters (`models.py`).
- **Inheritance** — `User` → `Admin`, `Cashier` (adds `status`).
- **Polymorphism** — `StockMovementProcessor` / `StockInProcessor` / `StockOutProcessor`;
  `IAuthService` / `ISalesService` interfaces with different behavior for admin vs cashier.
- **Abstraction** — `IEntity`, `IRepository` abstract bases; `ProductRepository` hides
  all Firestore access behind `Product` objects; service classes abstract business logic.

## Project Structure
```
BrewPOS/
├── app.py                 # Flask application + routes
├── models.py              # OOP domain models
├── services.py            # AuthService, ProductRepository, SalesService
├── firebase_config.py     # Firebase Admin SDK init + web config
├── firebase_credentials.json   # (git-ignored) service account key
├── requirements.txt
├── brewpos.spec           # PyInstaller config
├── static/{css,js,images/products}
└── templates/             # google_login, admin_*, cashier_*, sales_*, inventory_*, etc.
```
