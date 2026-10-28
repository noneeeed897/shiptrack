-- ID_COL is replaced by app.py (SQLite locally, PostgreSQL on Render)
CREATE TABLE IF NOT EXISTS orders (
  id ID_COL,
  order_number TEXT UNIQUE NOT NULL,
  customer_name TEXT NOT NULL,
  email TEXT NOT NULL,
  phone TEXT,
  address TEXT NOT NULL,
  item_summary TEXT,
  amount REAL DEFAULT 0,
  carrier TEXT,
  tracking_number TEXT UNIQUE,
  status TEXT NOT NULL DEFAULT 'PLACED',
  eta TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id ID_COL,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  status TEXT NOT NULL,
  location TEXT,
  description TEXT,
  actor TEXT,
  ts TEXT NOT NULL,
  external_id TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_events_order ON events(order_id);
