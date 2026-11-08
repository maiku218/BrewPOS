-- ============================================================================
-- BrewPOS - PostgreSQL Schema (Firebase -> PostgreSQL migration)
-- ============================================================================
-- Run this script against an empty PostgreSQL database to create all tables,
-- indexes, constraints, and default data required by BrewPOS.
--
-- Usage:
--   psql -U postgres -d brewpos -f schema.sql
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Extensions
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ---------------------------------------------------------------------------
-- users
-- Replaces Firestore collections: users + cashiers (role distinguishes admin vs cashier)
-- ownerId multi-tenancy scoping: for an admin, owner_id = own id; for a cashier,
-- owner_id = the admin who created them.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              VARCHAR(255) PRIMARY KEY,
    email           VARCHAR(255) NOT NULL UNIQUE,
    username        VARCHAR(255) UNIQUE,
    password_hash   VARCHAR(255),
    display_name    VARCHAR(255) NOT NULL,
    role            VARCHAR(50) NOT NULL DEFAULT 'cashier' CHECK (role IN ('admin', 'cashier')),
    status          VARCHAR(50) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    owner_id        VARCHAR(255),
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_users_owner FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

-- ---------------------------------------------------------------------------
-- categories
-- Replaces Firestore collection: categories
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS categories (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(255) NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- products
-- Replaces Firestore collection: products
-- JSONB used for variants/addons (previously nested arrays/maps in Firestore).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS products (
    id              VARCHAR(255) PRIMARY KEY,
    name            VARCHAR(255) NOT NULL,
    barcode         VARCHAR(255),
    category        VARCHAR(255),
    description     TEXT,
    image_url       TEXT,
    image_path      TEXT,
    status          VARCHAR(50) NOT NULL DEFAULT 'Available',
    pricing_type    VARCHAR(50) NOT NULL DEFAULT 'single',
    price           NUMERIC(10, 2) NOT NULL DEFAULT 0,
    variants        JSONB NOT NULL DEFAULT '[]'::jsonb,
    addons          JSONB NOT NULL DEFAULT '[]'::jsonb,
    stock_quantity  INTEGER NOT NULL DEFAULT 0,
    stock_minimum   INTEGER NOT NULL DEFAULT 0,
    expiration_date DATE,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    owner_id        VARCHAR(255) NOT NULL,
    CONSTRAINT fk_products_owner FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- stock_movements
-- Replaces Firestore collection: stock_movements
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_movements (
    id             SERIAL PRIMARY KEY,
    product_id     VARCHAR(255) NOT NULL,
    movement_type  VARCHAR(10) NOT NULL CHECK (movement_type IN ('IN', 'OUT')),
    quantity       INTEGER NOT NULL CHECK (quantity > 0),
    reason         VARCHAR(255),
    timestamp      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    owner_id       VARCHAR(255) NOT NULL,
    receipt_no     VARCHAR(255),
    CONSTRAINT fk_stock_movements_product FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE SET NULL,
    CONSTRAINT fk_stock_movements_owner FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- sales
-- Replaces Firestore collection: sales (document fields)
-- Line items moved to sale_items (previously a subcollection).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sales (
    id               VARCHAR(255) PRIMARY KEY,
    receipt_no       VARCHAR(255) NOT NULL,
    cashier_id       VARCHAR(255) NOT NULL,
    owner_id         VARCHAR(255) NOT NULL,
    total            NUMERIC(10, 2) NOT NULL DEFAULT 0,
    date             TIMESTAMP NOT NULL,
    product_type     VARCHAR(50) NOT NULL DEFAULT 'Pastry',
    status           VARCHAR(50) NOT NULL DEFAULT 'Pending' CHECK (status IN ('Pending', 'Completed')),
    receipt_printed  BOOLEAN NOT NULL DEFAULT FALSE,
    printed_at       TIMESTAMP,
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_sales_cashier FOREIGN KEY (cashier_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_sales_owner FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- sale_items
-- Replaces Firestore subcollection: sales/{saleId}/items
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sale_items (
    id         SERIAL PRIMARY KEY,
    sale_id    VARCHAR(255) NOT NULL,
    product_id VARCHAR(255),
    name       VARCHAR(255) NOT NULL,
    price      NUMERIC(10, 2) NOT NULL DEFAULT 0,
    quantity   INTEGER NOT NULL CHECK (quantity > 0),
    cup_size   VARCHAR(10),
    image      TEXT,
    CONSTRAINT fk_sale_items_sale FOREIGN KEY (sale_id) REFERENCES sales(id) ON DELETE CASCADE,
    CONSTRAINT fk_sale_items_product FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE SET NULL
);

-- ---------------------------------------------------------------------------
-- store_settings
-- Replaces Firestore collection: store_settings (doc config_{ownerId} / config)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS store_settings (
    id           VARCHAR(255) PRIMARY KEY,
    store_name   VARCHAR(255) NOT NULL DEFAULT 'BrewPOS',
    subtitle     VARCHAR(255) NOT NULL DEFAULT 'Coffee Shop POS',
    address      TEXT,
    contact      TEXT,
    footer       TEXT NOT NULL DEFAULT E'Thank you for your purchase!\nPlease come again.',
    owner_id     VARCHAR(255),
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_store_settings_owner FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- admin_activity
-- Replaces Firestore collection: admin_activity
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admin_activity (
    id        SERIAL PRIMARY KEY,
    admin_id  VARCHAR(255) NOT NULL,
    action    VARCHAR(255),
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    details   TEXT,
    owner_id  VARCHAR(255) NOT NULL,
    CONSTRAINT fk_admin_activity_owner FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- cashier_activity
-- Replaces Firestore collection: cashier_activity
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cashier_activity (
    id        SERIAL PRIMARY KEY,
    cashier_id VARCHAR(255) NOT NULL,
    action    VARCHAR(255),
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    owner_id  VARCHAR(255) NOT NULL,
    CONSTRAINT fk_cashier_activity_owner FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);

CREATE INDEX IF NOT EXISTS idx_categories_name ON categories(name);

CREATE INDEX IF NOT EXISTS idx_products_owner_id ON products(owner_id);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode);
CREATE INDEX IF NOT EXISTS idx_products_stock ON products(stock_quantity);
CREATE INDEX IF NOT EXISTS idx_products_expiration ON products(expiration_date);

CREATE INDEX IF NOT EXISTS idx_stock_movements_owner_id ON stock_movements(owner_id);
CREATE INDEX IF NOT EXISTS idx_stock_movements_product_id ON stock_movements(product_id);
CREATE INDEX IF NOT EXISTS idx_stock_movements_timestamp ON stock_movements(timestamp);

CREATE INDEX IF NOT EXISTS idx_sales_owner_id ON sales(owner_id);
CREATE INDEX IF NOT EXISTS idx_sales_cashier_id ON sales(cashier_id);
CREATE INDEX IF NOT EXISTS idx_sales_receipt_no ON sales(receipt_no);
CREATE INDEX IF NOT EXISTS idx_sales_date ON sales(date);
CREATE INDEX IF NOT EXISTS idx_sales_status ON sales(status);
CREATE INDEX IF NOT EXISTS idx_sales_product_type ON sales(product_type);

CREATE INDEX IF NOT EXISTS idx_sale_items_sale_id ON sale_items(sale_id);
CREATE INDEX IF NOT EXISTS idx_sale_items_product_id ON sale_items(product_id);

CREATE INDEX IF NOT EXISTS idx_store_settings_owner_id ON store_settings(owner_id);

CREATE INDEX IF NOT EXISTS idx_admin_activity_owner_id ON admin_activity(owner_id);
CREATE INDEX IF NOT EXISTS idx_admin_activity_timestamp ON admin_activity(timestamp);

CREATE INDEX IF NOT EXISTS idx_cashier_activity_owner_id ON cashier_activity(owner_id);
CREATE INDEX IF NOT EXISTS idx_cashier_activity_timestamp ON cashier_activity(timestamp);

-- ---------------------------------------------------------------------------
-- otp_codes
-- One-time passwords for email verification (login/register/password_reset)
-- Stores OTP hash (never plaintext), verification status, failed attempts,
-- and resend cooldown. Integrates with Resend API for email delivery.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS otp_codes (
    id                SERIAL PRIMARY KEY,
    email             VARCHAR(255) NOT NULL,
    code_hash         VARCHAR(255) NOT NULL,
    purpose           VARCHAR(50) NOT NULL DEFAULT 'login' CHECK (purpose IN ('login', 'register', 'password_reset')),
    expires_at        TIMESTAMP NOT NULL,
    is_verified       BOOLEAN NOT NULL DEFAULT FALSE,
    failed_attempts   INTEGER NOT NULL DEFAULT 0,
    resend_cooldown_at TIMESTAMP,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    owner_id          VARCHAR(255),
    CONSTRAINT fk_otp_codes_owner FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_otp_codes_email ON otp_codes(email);
CREATE INDEX IF NOT EXISTS idx_otp_codes_expires_at ON otp_codes(expires_at);
CREATE INDEX IF NOT EXISTS idx_otp_codes_purpose ON otp_codes(purpose);

-- ============================================================================
-- End of schema
-- ============================================================================
