import os
import sqlite3
import json
from datetime import datetime
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, g, abort
)
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
DATABASE = os.environ.get("DATABASE_URL", "microflip.db")

STRIPE_CLIENT_ID = os.environ.get("STRIPE_CLIENT_ID", "")
STRIPE_API_KEY = os.environ.get("STRIPE_API_KEY", "")
STRIPE_REDIRECT_URI = os.environ.get(
    "STRIPE_REDIRECT_URI", "http://localhost:5000/stripe/callback"
)

# ── DB ────────────────────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    db = sqlite3.connect(DATABASE)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'both'
             CHECK(role IN ('seller','buyer','both')),
        stripe_account_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS listings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        seller_id INTEGER NOT NULL REFERENCES users(id),
        title TEXT NOT NULL,
        url TEXT DEFAULT '',
        description TEXT DEFAULT '',
        asking_price_usd INTEGER NOT NULL DEFAULT 0,
        mrr_declared_usd INTEGER NOT NULL DEFAULT 0,
        mrr_verified_usd INTEGER,
        stack_tags TEXT DEFAULT '',
        screenshot_url TEXT,
        verified INTEGER DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'draft'
               CHECK(status IN ('draft','active','sold','expired')),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS revenue_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        listing_id INTEGER NOT NULL REFERENCES listings(id),
        month DATE NOT NULL,
        mrr_usd INTEGER NOT NULL,
        source TEXT NOT NULL DEFAULT 'stripe'
               CHECK(source IN ('stripe')),
        fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS offers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        listing_id INTEGER NOT NULL REFERENCES listings(id),
        buyer_id INTEGER NOT NULL REFERENCES users(id),
        amount_usd INTEGER NOT NULL,
        message TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending'
               CHECK(status IN ('pending','accepted','rejected','countered')),
        parent_offer_id INTEGER REFERENCES offers(id),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    db.close()

# ── Auth helpers ──────────────────────────────────────────────────────────

def current_user():
    uid = session.get("user_id")
    if uid is None:
        return None
    if not hasattr(g, "_user"):
        g._user = get_db().execute(
            "SELECT * FROM users WHERE id=?", (uid,)
        ).fetchone()
    return g._user


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if current_user() is None:
            flash("Please log in first.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def seller_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = current_user()
        if not user or user["role"] not in ("seller", "both"):
            flash("Seller account required.", "warning")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated


@app.context_processor
def inject_user():
    return dict(current_user=current_user())

# ── Filters ───────────────────────────────────────────────────────────────

@app.template_filter("usd")
def format_usd(value):
    if value is None:
        return "N/A"
    return f"${value:,}"


@app.template_filter("timeago")
def timeago(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except Exception:
            return value
    delta = datetime.utcnow() - value
    s = int(delta.total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"

# ── Routes: Auth ──────────────────────────────────────────────────────────

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "both")
        if not email or not password:
            flash("Email and password are required.", "error")
            return render_template("register.html")
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("register.html")
        if role not in ("seller", "buyer", "both"):
            role = "both"
        db = get_db()
        if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
            flash("Email already registered.", "error")
            return render_template("register.html")
        cur = db.execute(
            "INSERT INTO users (email, password_hash, role) VALUES (?,?,?)",
            (email, generate_password_hash(password), role),
        )
        db.commit()
        session["user_id"] = cur.lastrowid
        flash("Account created!", "success")
        return redirect(url_for("dashboard"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            flash("Welcome back!", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("index"))

# ── Routes: Landing ──────────────────────────────────────────────────────

@app.route("/")
def index():
    db = get_db()
    listings = db.execute(
        """SELECT l.*, u.email as seller_email FROM listings l
           JOIN users u ON l.seller_id = u.id
           WHERE l.status = 'active'
           ORDER BY l.verified DESC, l.created_at DESC LIMIT 9"""
    ).fetchall()
    return render_template("index.html", listings=listings)

# ── Routes: Browse ────────────────────────────────────────────────────────

@app.route("/listings")
def browse_listings():
    db = get_db()
    q = """SELECT l.*, u.email as seller_email FROM listings l
           JOIN users u ON l.seller_id = u.id
           WHERE l.status = 'active'"""
    params = []

    price_min = request.args.get("price_min", type=int)
    price_max = request.args.get("price_max", type=int)
    mrr_min = request.args.get("mrr_min", type=int)
    verified_only = request.args.get("verified")
    stack = request.args.get("stack", "").strip().lower()

    if price_min is not None:
        q += " AND l.asking_price_usd >= ?"
        params.append(price_min)
    if price_max is not None:
        q += " AND l.asking_price_usd <= ?"
        params.append(price_max)
    if mrr_min is not None:
        q += " AND l.mrr_declared_usd >= ?"
        params.append(mrr_min)
    if verified_only:
        q += " AND l.verified = 1"
    if stack:
        q += " AND LOWER(l.stack_tags) LIKE ?"
        params.append(f"%{stack}%")

    sort = request.args.get("sort", "newest")
    if sort == "price_asc":
        q += " ORDER BY l.asking_price_usd ASC"
    elif sort == "price_desc":
        q += " ORDER BY l.asking_price_usd DESC"
    elif sort == "mrr_desc":
        q += " ORDER BY l.mrr_declared_usd DESC"
    else:
        q += " ORDER BY l.verified DESC, l.created_at DESC"

    listings = db.execute(q, params).fetchall()
    return render_template("browse.html", listings=listings)

# ── Routes: Listings CRUD ─────────────────────────────────────────────────

@app.route("/listings/new", methods=["GET", "POST"])
@login_required
@seller_required
def create_listing():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        url = request.form.get("url", "").strip()
        description = request.form.get("description", "").strip()
        asking_price = int(request.form.get("asking_price", 0) or 0)
        mrr_declared = int(request.form.get("mrr_declared", 0) or 0)
        stack_tags = request.form.get("stack_tags", "").strip()
        screenshot_url = request.form.get("screenshot_url", "").strip()
        status = request.form.get("status", "active")
        if not title or asking_price <= 0:
            flash("Title and a positive asking price are required.", "error")
            return render_template("listing_form.html", listing=None)
        if status not in ("draft", "active"):
            status = "active"
        db = get_db()
        db.execute(
            """INSERT INTO listings
               (seller_id, title, url, description, asking_price_usd,
                mrr_declared_usd, stack_tags, screenshot_url, status)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (session["user_id"], title, url, description, asking_price,
             mrr_declared, stack_tags, screenshot_url or None, status),
        )
        db.commit()
        flash("Listing created!", "success")
        return redirect(url_for("dashboard"))
    return render_template("listing_form.html", listing=None)

# ── Routes: Listing detail ────────────────────────────────────────────────

@app.route("/listings/<int:id>")
def view_listing(id):
    db = get_db()
    listing = db.execute(
        """SELECT l.*, u.email as seller_email FROM listings l
           JOIN users u ON l.seller_id = u.id WHERE l.id = ?""",
        (id,),
    ).fetchone()
    if not listing:
        abort(404)
    snapshots = db.execute(
        "SELECT month, mrr_usd FROM revenue_snapshots WHERE listing_id=? ORDER BY month",
        (id,),
    ).fetchall()
    snapshot_data = [{"month": s["month"], "mrr": s["mrr_usd"]} for s in snapshots]

    offers = []
    user = current_user()
    if user:
        if user["id"] == listing["seller_id"]:
            offers = db.execute(
                """SELECT o.*, u.email as buyer_email FROM offers o
                   JOIN users u ON o.buyer_id = u.id
                   WHERE o.listing_id=? ORDER BY o.created_at DESC""",
                (id,),
            ).fetchall()
        else:
            offers = db.execute(
                """SELECT o.* FROM offers o
                   WHERE o.listing_id=? AND o.buyer_id=?
                   ORDER BY o.created_at DESC""",
                (id, user["id"]),
            ).fetchall()

    return render_template(
        "listing_detail.html",
        listing=listing,
        snapshots=snapshot_data,
        offers=offers,
    )

# ── Routes: Stripe verify ─────────────────────────────────────────────────

@app.route("/listings/<int:id>/verify", methods=["POST"])
@login_required
@seller_required
def start_stripe_verify(id):
    db = get_db()
    listing = db.execute(
        "SELECT * FROM listings WHERE id=? AND seller_id=?",
        (id, session["user_id"]),
    ).fetchone()
    if not listing:
        abort(404)
    if not STRIPE_CLIENT_ID:
        flash("Stripe not configured. Set STRIPE_CLIENT_ID env var.", "error")
        return redirect(url_for("view_listing", id=id))
    oauth_url = (
        "https://connect.stripe.com/oauth/authorize"
        f"?response_type=code&client_id={STRIPE_CLIENT_ID}"
        f"&scope=read_only&state={id}"
        f"&redirect_uri={STRIPE_REDIRECT_URI}"
    )
    return redirect(oauth_url)


@app.route("/stripe/callback")
@login_required
def stripe_oauth_callback():
    import urllib.request
    import urllib.parse

    code = request.args.get("code")
    listing_id = request.args.get("state", type=int)
    error = request.args.get("error")

    if error or not code or not listing_id:
        flash(f"Stripe auth failed: {error or 'missing code'}", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    listing = db.execute(
        "SELECT * FROM listings WHERE id=? AND seller_id=?",
        (listing_id, session["user_id"]),
    ).fetchone()
    if not listing:
        abort(404)

    try:
        # Exchange code for access token
        token_body = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "client_secret": STRIPE_API_KEY,
        }).encode()
        req = urllib.request.Request(
            "https://connect.stripe.com/oauth/token",
            data=token_body, method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())

        stripe_account_id = result.get("stripe_user_id")
        access_token = result.get("access_token", STRIPE_API_KEY)
        if not stripe_account_id:
            flash("Could not retrieve Stripe account ID.", "error")
            return redirect(url_for("view_listing", id=listing_id))

        db.execute(
            "UPDATE users SET stripe_account_id=? WHERE id=?",
            (stripe_account_id, session["user_id"]),
        )

        # Fetch balance transactions
        txn_url = (
            "https://api.stripe.com/v1/balance_transactions"
            "?limit=100&type=charge"
        )
        req2 = urllib.request.Request(txn_url)
        req2.add_header("Authorization", f"Bearer {access_token}")
        req2.add_header("Stripe-Account", stripe_account_id)
        with urllib.request.urlopen(req2) as resp2:
            txns = json.loads(resp2.read())

        monthly = {}
        for txn in txns.get("data", []):
            dt = datetime.fromtimestamp(txn["created"])
            key = dt.strftime("%Y-%m-01")
            monthly[key] = monthly.get(key, 0) + txn["amount"]

        for month, cents in sorted(monthly.items()):
            db.execute(
                """INSERT INTO revenue_snapshots
                   (listing_id, month, mrr_usd, source) VALUES (?,?,?,'stripe')""",
                (listing_id, month, cents // 100),
            )

        if monthly:
            recent = sorted(monthly.keys())[-6:]
            avg_mrr = sum(monthly[m] for m in recent) // (len(recent) * 100)
        else:
            avg_mrr = 0

        db.execute(
            """UPDATE listings SET verified=1, mrr_verified_usd=?,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (avg_mrr, listing_id),
        )
        db.commit()
        flash(f"Revenue verified! Average MRR: ${avg_mrr}/mo", "success")

    except Exception as e:
        flash(f"Stripe verification error: {e}", "error")

    return redirect(url_for("view_listing", id=listing_id))

# ── Routes: Offers ─────────────────────────────────────────────────────────

@app.route("/listings/<int:id>/offer", methods=["POST"])
@login_required
def send_offer(id):
    db = get_db()
    listing = db.execute(
        "SELECT * FROM listings WHERE id=? AND status='active'", (id,)
    ).fetchone()
    if not listing:
        abort(404)
    if listing["seller_id"] == session["user_id"]:
        flash("You cannot make an offer on your own listing.", "error")
        return redirect(url_for("view_listing", id=id))

    amount = int(request.form.get("amount", 0) or 0)
    message = request.form.get("message", "").strip()
    if amount <= 0:
        flash("Offer amount must be positive.", "error")
        return redirect(url_for("view_listing", id=id))

    db.execute(
        "INSERT INTO offers (listing_id, buyer_id, amount_usd, message) VALUES (?,?,?,?)",
        (id, session["user_id"], amount, message),
    )
    db.commit()
    flash("Offer sent!", "success")
    return redirect(url_for("view_listing", id=id))


@app.route("/offers/<int:id>/respond", methods=["POST"])
@login_required
def respond_offer(id):
    db = get_db()
    offer = db.execute(
        """SELECT o.*, l.seller_id, l.id as lid FROM offers o
           JOIN listings l ON o.listing_id = l.id WHERE o.id=?""",
        (id,),
    ).fetchone()
    if not offer or offer["seller_id"] != session["user_id"]:
        abort(404)

    action = request.form.get("action")

    if action == "accept":
        db.execute("UPDATE offers SET status='accepted' WHERE id=?", (id,))
        db.execute(
            "UPDATE listings SET status='sold', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (offer["lid"],),
        )
        db.commit()
        flash("Offer accepted! Listing marked as sold.", "success")

    elif action == "reject":
        db.execute("UPDATE offers SET status='rejected' WHERE id=?", (id,))
        db.commit()
        flash("Offer rejected.", "info")

    elif action == "counter":
        counter_amount = int(request.form.get("counter_amount", 0) or 0)
        counter_message = request.form.get("counter_message", "").strip()
        if counter_amount <= 0:
            flash("Counter-offer amount must be positive.", "error")
            return redirect(url_for("dashboard"))
        db.execute("UPDATE offers SET status='countered' WHERE id=?", (id,))
        db.execute(
            """INSERT INTO offers
               (listing_id, buyer_id, amount_usd, message, status, parent_offer_id)
               VALUES (?,?,?,?,'pending',?)""",
            (offer["lid"], offer["buyer_id"], counter_amount, counter_message, id),
        )
        db.commit()
        flash("Counter-offer sent!", "success")

    return redirect(url_for("dashboard"))

# ── Routes: Dashboard ─────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    user = current_user()
    my_listings = []
    received_offers = []
    sent_offers = []

    if user["role"] in ("seller", "both"):
        my_listings = db.execute(
            "SELECT * FROM listings WHERE seller_id=? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
        received_offers = db.execute(
            """SELECT o.*, l.title as listing_title, u.email as buyer_email
               FROM offers o
               JOIN listings l ON o.listing_id = l.id
               JOIN users u ON o.buyer_id = u.id
               WHERE l.seller_id=?
               ORDER BY o.created_at DESC""",
            (user["id"],),
        ).fetchall()

    if user["role"] in ("buyer", "both"):
        sent_offers = db.execute(
            """SELECT o.*, l.title as listing_title
               FROM offers o
               JOIN listings l ON o.listing_id = l.id
               WHERE o.buyer_id=?
               ORDER BY o.created_at DESC""",
            (user["id"],),
        ).fetchall()

    return render_template(
        "dashboard.html",
        my_listings=my_listings,
        received_offers=received_offers,
        sent_offers=sent_offers,
    )

# ── Boot ──────────────────────────────────────────────────────────────────

init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
