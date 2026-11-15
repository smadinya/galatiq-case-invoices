PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inventory (
  item TEXT PRIMARY KEY, item_key TEXT NOT NULL UNIQUE,
  stock INTEGER NOT NULL CHECK (stock >= 0), unit_price TEXT NOT NULL, category TEXT);
CREATE TABLE IF NOT EXISTS vendors (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, normalized_name TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('approved','blocked')), address TEXT, payment_terms TEXT);
CREATE TABLE IF NOT EXISTS vendor_aliases (
  id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_id INTEGER NOT NULL REFERENCES vendors(id),
  alias TEXT NOT NULL, normalized_alias TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('previous_name','approved_rename')), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT NOT NULL UNIQUE,
  file_path TEXT NOT NULL, file_name TEXT NOT NULL, file_hash TEXT NOT NULL,
  fingerprint TEXT, vendor_id INTEGER REFERENCES vendors(id), vendor_key TEXT, vendor_name TEXT,
  invoice_number TEXT, revision TEXT, status TEXT NOT NULL, ceiling TEXT,
  amount TEXT, currency TEXT, amount_usd TEXT, invoice_date TEXT, error TEXT,
  result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_invoices_hash ON invoices(file_hash);
CREATE INDEX IF NOT EXISTS ix_invoices_fp ON invoices(fingerprint);
CREATE INDEX IF NOT EXISTS ix_invoices_vendor_number ON invoices(vendor_key, invoice_number);
CREATE TABLE IF NOT EXISTS stock_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, item TEXT NOT NULL REFERENCES inventory(item),
  delta INTEGER NOT NULL, invoice_id INTEGER REFERENCES invoices(id), reason TEXT NOT NULL, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key TEXT NOT NULL UNIQUE,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id), amount TEXT NOT NULL, currency TEXT NOT NULL,
  fx_rate TEXT, amount_usd TEXT, status TEXT NOT NULL CHECK (status IN ('SUCCESS','FAILED')),
  attempts INTEGER NOT NULL, error TEXT, receipt_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS budget_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, month TEXT NOT NULL, amount_usd TEXT NOT NULL,
  invoice_id INTEGER REFERENCES invoices(id), ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS human_queue (
  invoice_id INTEGER PRIMARY KEY REFERENCES invoices(id), trace_id TEXT NOT NULL,
  reason TEXT NOT NULL, recommendation_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('PENDING','RESOLVED','SUPERSEDED')),
  resolution TEXT, note TEXT, created_at TEXT NOT NULL, resolved_at TEXT);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT NOT NULL, ts TEXT NOT NULL,
  actor TEXT NOT NULL CHECK (actor IN ('system','vp','critic','human')), event TEXT NOT NULL,
  payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_audit_trace ON audit_log(trace_id);
