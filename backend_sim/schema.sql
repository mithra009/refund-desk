-- Fake business backend for Refund Desk. Money is integer cents.
-- Portable between Postgres 16 and SQLite (tests); see backend_sim/db.py.
CREATE EXTENSION IF NOT EXISTS vector;  -- sqlite: stripped by db.py

CREATE TABLE IF NOT EXISTS customers (
  id TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL,
  tier TEXT NOT NULL DEFAULT 'standard',
  lifetime_orders INTEGER NOT NULL DEFAULT 0,
  lifetime_refunds INTEGER NOT NULL DEFAULT 0,
  payment_changes_30d INTEGER NOT NULL DEFAULT 0,
  address_reuse_accounts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL REFERENCES customers(id),
  placed_at TIMESTAMPTZ NOT NULL,
  total_cents INTEGER NOT NULL,
  shipping_cents INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_items (
  order_id TEXT NOT NULL REFERENCES orders(id),
  sku TEXT NOT NULL,
  price_cents INTEGER NOT NULL,
  final_sale BOOLEAN NOT NULL DEFAULT FALSE,
  note TEXT NOT NULL DEFAULT ''          -- free text; untrusted, labelled as data in prompts
);

CREATE TABLE IF NOT EXISTS shipments (
  order_id TEXT PRIMARY KEY REFERENCES orders(id),
  carrier TEXT NOT NULL,
  tracking_no TEXT NOT NULL UNIQUE,
  promised_at TIMESTAMPTZ NOT NULL,
  delivered_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS tracking_events (
  tracking_no TEXT NOT NULL,
  ts TIMESTAMPTZ NOT NULL,
  code TEXT NOT NULL CHECK (code IN ('IN_TRANSIT','DELIVERED','DAMAGED_SCAN','LOST'))
);

CREATE TABLE IF NOT EXISTS carrier_claims (
  tracking_no TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  opened_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS refunds (
  id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL REFERENCES orders(id),
  amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL,
  actor TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  customer_id TEXT NOT NULL,
  order_id TEXT,
  claim_type TEXT,
  status TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  final_decision JSONB
);

CREATE TABLE IF NOT EXISTS audit_log (
  case_id TEXT NOT NULL,
  ts TIMESTAMPTZ NOT NULL,
  actor TEXT NOT NULL,
  event TEXT NOT NULL,
  payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_chunks (
  clause_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  embedding vector(256)                  -- sqlite: stored as TEXT json
);

CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_events_tracking ON tracking_events(tracking_no);
CREATE INDEX IF NOT EXISTS idx_audit_case ON audit_log(case_id);
