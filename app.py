"""ShipTrack - order tracking & shipment lifecycle (Flask).
Local : pip install flask  ->  python app.py        (SQLite, no setup)
Render: set DATABASE_URL (PostgreSQL)  ->  gunicorn app:app
"""
import hashlib, hmac, os, secrets, sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_PG = DATABASE_URL.startswith("postgres")
SQLITE_PATH = os.environ.get("DB_PATH", os.path.join(BASE, "tracker.db"))
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "admin123")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me").encode()

# ---------- 1. Lifecycle (state machine) ----------
ALLOWED = {
    "PLACED": ["CONFIRMED", "CANCELLED"],
    "CONFIRMED": ["PACKED", "CANCELLED"],
    "PACKED": ["SHIPPED", "CANCELLED"],
    "SHIPPED": ["IN_TRANSIT", "ON_HOLD"],
    "IN_TRANSIT": ["OUT_FOR_DELIVERY", "ON_HOLD"],
    "ON_HOLD": ["IN_TRANSIT"],
    "OUT_FOR_DELIVERY": ["DELIVERED", "DELIVERY_FAILED"],
    "DELIVERY_FAILED": ["OUT_FOR_DELIVERY", "RETURN_INITIATED"],
    "DELIVERED": ["RETURN_INITIATED"],
    "RETURN_INITIATED": ["RETURNED"],
    "RETURNED": [], "CANCELLED": [],
}
HAPPY = ["PLACED", "CONFIRMED", "PACKED", "SHIPPED", "IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERED"]
LABEL = {"PLACED": "Order placed", "CONFIRMED": "Confirmed", "PACKED": "Packed", "SHIPPED": "Shipped",
         "IN_TRANSIT": "In transit", "OUT_FOR_DELIVERY": "Out for delivery", "DELIVERED": "Delivered",
         "DELIVERY_FAILED": "Delivery failed", "ON_HOLD": "On hold", "CANCELLED": "Cancelled",
         "RETURN_INITIATED": "Return requested", "RETURNED": "Returned"}
ICON = {"PLACED": "🛒", "CONFIRMED": "📋", "PACKED": "📦", "SHIPPED": "🚚", "IN_TRANSIT": "🛣️",
        "OUT_FOR_DELIVERY": "🏍️", "DELIVERED": "✅", "DELIVERY_FAILED": "⚠️", "ON_HOLD": "⏸️",
        "CANCELLED": "✖️", "RETURN_INITIATED": "↩️", "RETURNED": "📥"}
MSG = {"PLACED": "We've received your order.", "CONFIRMED": "Payment verified and order confirmed.",
       "PACKED": "Your items are packed and ready for pickup.", "SHIPPED": "Handed over to the courier.",
       "IN_TRANSIT": "Your package is on its way.", "OUT_FOR_DELIVERY": "Your package is out for delivery today.",
       "DELIVERED": "Delivered. Enjoy!", "DELIVERY_FAILED": "Delivery attempt failed. We will try again.",
       "ON_HOLD": "Shipment is temporarily on hold.", "CANCELLED": "Order cancelled.",
       "RETURN_INITIATED": "Return requested.", "RETURNED": "Return received at our warehouse."}
TONE = {"DELIVERED": "ok", "ON_HOLD": "warn", "DELIVERY_FAILED": "warn", "CANCELLED": "bad",
        "RETURN_INITIATED": "muted", "RETURNED": "muted"}
IST = timezone(timedelta(hours=5, minutes=30))

@app.context_processor
def inject():
    return dict(LABEL=LABEL, ICON=ICON, HAPPY=HAPPY, TONE=TONE, is_admin=bool(session.get("admin")))

# ---------- 2. Database (SQLite locally, PostgreSQL on Render) ----------
def db():
    if "db" not in g:
        if USE_PG:
            import psycopg
            from psycopg.rows import dict_row
            g.db = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        else:
            g.db = sqlite3.connect(SQLITE_PATH)
            g.db.row_factory = sqlite3.Row
    return g.db

def q(sql, args=()):
    return db().execute(sql.replace("?", "%s") if USE_PG else sql, args)

@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()

def init_db():
    with open(os.path.join(BASE, "schema.sql"), encoding="utf-8") as f:
        sql = f.read().replace("ID_COL", "SERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT")
    for stmt in sql.split(";"):
        if stmt.strip():
            q(stmt)
    db().commit()

def now():
    return datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")

# ---------- 3. Business logic ----------
def notify(order, status):
    """Stub: plug in email/SMS/WhatsApp here (SendGrid, MSG91, Twilio...)."""
    print(f"[NOTIFY] {order['email']}: {order['order_number']} -> {LABEL[status]}")

def get_order(number):
    return q("SELECT * FROM orders WHERE order_number=?", (number,)).fetchone()

def change_status(order, new, location="", note="", actor="admin", external_id=None):
    """Append an event and move the order. History is never edited."""
    if new not in ALLOWED:
        return False, "Unknown status."
    if external_id and q("SELECT 1 FROM events WHERE external_id=?", (external_id,)).fetchone():
        return True, "Duplicate event ignored."
    if new not in ALLOWED[order["status"]]:
        return False, f"Can't go from {LABEL[order['status']]} to {LABEL[new]}."
    q("INSERT INTO events(order_id,status,location,description,actor,ts,external_id) VALUES(?,?,?,?,?,?,?)",
      (order["id"], new, location, note or MSG[new], actor, now(), external_id))
    q("UPDATE orders SET status=? WHERE id=?", (new, order["id"]))
    db().commit()
    notify(order, new)
    return True, f"Status updated to {LABEL[new]}."

def create_order(f):
    while True:
        number = "ORD-" + str(secrets.randbelow(900000) + 100000)
        if not get_order(number):
            break
    try:
        amount = float(f.get("amount") or 0)
    except ValueError:
        amount = 0
    row = q("INSERT INTO orders(order_number,customer_name,email,phone,address,item_summary,amount,status,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?) RETURNING id",
            (number, f["name"].strip(), f["email"].strip().lower(), f.get("phone", ""), f["address"].strip(),
             f.get("items", ""), amount, "PLACED", now())).fetchone()
    q("INSERT INTO events(order_id,status,description,actor,ts) VALUES(?,?,?,?,?)",
      (row["id"], "PLACED", MSG["PLACED"], "system", now()))
    db().commit()
    return number

def same(a, b):
    return hmac.compare_digest(a.encode(), b.encode())

def verified_order(number):
    o = get_order(number)
    email = request.values.get("email", "").strip().lower()
    return o if o and same(o["email"], email) else None

def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper

# ---------- 4. Customer pages ----------
@app.get("/")
def home():
    return render_template("home.html")

@app.post("/track")
def track_form():
    return redirect(url_for("track", number=request.form["order_number"].strip().upper(),
                            email=request.form["email"].strip().lower()))

@app.get("/track/<number>")
def track(number):
    o = verified_order(number)
    if not o:
        flash("We couldn't find that order. Check the order number and email.", "bad")
        return redirect(url_for("home"))
    events = q("SELECT * FROM events WHERE order_id=? ORDER BY id DESC", (o["id"],)).fetchall()
    reached = max([HAPPY.index(e["status"]) for e in events if e["status"] in HAPPY] or [0])
    return render_template("track.html", o=o, events=events, reached=reached,
                           can_cancel="CANCELLED" in ALLOWED[o["status"]],
                           can_return=o["status"] == "DELIVERED",
                           place=o["address"].split(",")[-1].strip(), first=o["customer_name"].split()[0])

@app.post("/track/<number>/<action>")
def customer_action(number, action):
    o = verified_order(number)
    if not o or action not in ("cancel", "return"):
        abort(404)
    new = "CANCELLED" if action == "cancel" else "RETURN_INITIATED"
    ok, msg = change_status(o, new, "", "Requested by customer", "customer")
    flash(msg, "ok" if ok else "bad")
    return redirect(url_for("track", number=number, email=o["email"]))

# ---------- 5. Admin ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if same(request.form.get("username", ""), ADMIN_USER) and same(request.form.get("password", ""), ADMIN_PASS):
            session["admin"] = True
            return redirect(url_for("admin"))
        flash("Wrong username or password.", "bad")
    return render_template("login.html")

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

@app.get("/admin")
@admin_required
def admin():
    status, term = request.args.get("status", ""), request.args.get("q", "").strip().lower()
    sql, args = "SELECT * FROM orders WHERE 1=1", []
    if status:
        sql += " AND status=?"; args.append(status)
    if term:
        sql += " AND (LOWER(order_number) LIKE ? OR LOWER(customer_name) LIKE ? OR LOWER(email) LIKE ?)"
        args += [f"%{term}%"] * 3
    orders = q(sql + " ORDER BY id DESC", args).fetchall()
    c = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) AS n FROM orders GROUP BY status").fetchall()}
    stats = dict(total=sum(c.values()), active=sum(c.get(s, 0) for s in HAPPY[:-1]),
                 delivered=c.get("DELIVERED", 0), problems=c.get("ON_HOLD", 0) + c.get("DELIVERY_FAILED", 0))
    return render_template("admin.html", orders=orders, stats=stats, status=status, term=term)

@app.post("/admin/new")
@admin_required
def admin_new():
    flash(f"Order {create_order(request.form)} created.", "ok")
    return redirect(url_for("admin"))

@app.route("/admin/order/<number>", methods=["GET", "POST"])
@admin_required
def admin_order(number):
    o = get_order(number) or abort(404)
    if request.method == "POST":
        new = request.form["status"]
        ok, msg = change_status(o, new, request.form.get("location", "").strip(), request.form.get("note", "").strip())
        if ok and new == "SHIPPED":
            q("UPDATE orders SET carrier=?, tracking_number=?, eta=? WHERE id=?",
              (request.form.get("carrier") or "Local Courier", "TRK" + secrets.token_hex(5).upper(),
               request.form.get("eta", ""), o["id"]))
            db().commit()
        flash(msg, "ok" if ok else "bad")
        return redirect(url_for("admin_order", number=number))
    events = q("SELECT * FROM events WHERE order_id=? ORDER BY id DESC", (o["id"],)).fetchall()
    return render_template("admin_order.html", o=o, events=events, nxt=ALLOWED[o["status"]])

# ---------- 6. Carrier webhook + health check ----------
@app.post("/api/webhook/carrier")
def webhook():
    """JSON {event_id, tracking_number, status, location, description}; header X-Signature = HMAC-SHA256(body)."""
    sig = request.headers.get("X-Signature", "")
    if not same(sig, hmac.new(WEBHOOK_SECRET, request.get_data(), hashlib.sha256).hexdigest()):
        return jsonify(error="bad signature"), 401
    d = request.get_json(force=True)
    o = q("SELECT * FROM orders WHERE tracking_number=?", (d.get("tracking_number"),)).fetchone()
    if not o:
        return jsonify(error="unknown tracking number"), 404
    ok, msg = change_status(o, d.get("status", ""), d.get("location", ""), d.get("description", ""),
                            "carrier", d.get("event_id"))
    return jsonify(ok=ok, message=msg), (200 if ok else 409)

@app.get("/healthz")
def healthz():
    return "ok"

with app.app_context():
    init_db()

if __name__ == "__main__":      # local testing only; Render uses gunicorn
    app.run(debug=True)
