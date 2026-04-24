import os
import sqlite3
import smtplib
import time
import json
from email.mime.text import MIMEText
from functools import wraps
from datetime import datetime
from urllib.parse import urlencode

from flask import (
    Flask, request, redirect, url_for, render_template,
    session, flash, g, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

DATABASE = os.environ.get("DATABASE_URL", "microflip.db")
STRIPE_CLIENT_ID = os.environ.get("STRIPE_CLIENT_ID", "")
STRIPE_API_KEY = os.environ.get("STRIPE_API_KEY", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

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
        name TEXT NOT NULL,
        bio TEXT DEFAULT '',
        is_verified INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS listings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        title TEXT NOT NULL,
        description TEXT DEFAULT '',
        url TEXT DEFAULT '',
        asking_price_usd INTEGER DEFAULT 0,
        mrr_usd INTEGER DEFAULT 0,
        stack TEXT DEFAULT '',
        screenshot_url TEXT DEFAULT '',
        stripe_verified INTEGER DEFAULT 0,
        stripe_account_id TEXT DEFAULT '',
        status TEXT DEFAULT 'draft' CHECK(status IN ('draft','active','sold')),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS intents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        listing_id INTEGER NOT NULL REFERENCES listings(id),
        buyer_id INTEGER NOT NULL REFERENCES users(id),
        message TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    db.close()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def current_user():
    if "user_id" not in session:
        return None
    if not hasattr(g, "_user"):
        g._user = get_db().execute(
            "SELECT * FROM users WHERE id=?", (session["user_id"],)
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


@app.context_processor
def inject_user():
    return dict(current_user=current_user())


# ---------------------------------------------------------------------------
# Template filters
# ---------------------------------------------------------------------------

@app.template_filter("usd")
def format_usd(value):
    if value is None:
        return "$0"
    return f"${value:,}"


@app.template_filter("timeago")
def timeago(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    delta = datetime.utcnow() - value
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


# ---------------------------------------------------------------------------
# Email helper
# ---------------------------------------------------------------------------

def send_email(to, subject, body):
    if not SMTP_HOST:
        print(f"[EMAIL] To: {to} | Subject: {subject}\n{body}\n---")
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = to
    with smtplib.SMTP(SMTP_HOST, 587) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)


# ---------------------------------------------------------------------------
# Routes: Auth
# ---------------------------------------------------------------------------

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        name = request.form.get("name", "").strip()
        bio = request.form.get("bio", "").strip()
        if not email or not password or not name:
            flash("All fields are required.", "error")
            return render_template("signup.html")
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("signup.html")
        db = get_db()
        if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
            flash("Email already registered.", "error")
            return render_template("signup.html")
        cur = db.execute(
            "INSERT INTO users (email, password_hash, name, bio) VALUES (?,?,?,?)",
            (email, generate_password_hash(password), name, bio),
        )
        db.commit()
        session["user_id"] = cur.lastrowid
        flash("Account created!", "success")
        return redirect(url_for("dashboard"))
    return render_template("signup.html")


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


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Routes: Landing
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    db = get_db()
    listings = db.execute(
        "SELECT l.*, u.name as seller_name FROM listings l "
        "JOIN users u ON l.user_id = u.id "
        "WHERE l.status = 'active' AND l.stripe_verified = 1 "
        "ORDER BY l.created_at DESC LIMIT 6"
    ).fetchall()
    return render_template("index.html", listings=listings)


# ---------------------------------------------------------------------------
# Routes: Browse & Search
# ---------------------------------------------------------------------------

@app.route("/listings")
def browse():
    db = get_db()
    query = (
        "SELECT l.*, u.name as seller_name FROM listings l "
        "JOIN users u ON l.user_id = u.id WHERE l.status = 'active'"
    )
    params = []

    min_price = request.args.get("min_price", type=int)
    max_price = request.args.get("max_price", type=int)
    min_mrr = request.args.get("min_mrr", type=int)
    max_mrr = request.args.get("max_mrr", type=int)
    stack = request.args.get("stack", "").strip()
    sort = request.args.get("sort", "newest")

    if min_price is not None:
        query += " AND l.asking_price_usd >= ?"
        params.append(min_price)
    if max_price is not None:
        query += " AND l.asking_price_usd <= ?"
        params.append(max_price)
    if min_mrr is not None:
        query += " AND l.mrr_usd >= ?"
        params.append(min_mrr)
    if max_mrr is not None:
        query += " AND l.mrr_usd <= ?"
        params.append(max_mrr)
    if stack:
        query += " AND LOWER(l.stack) LIKE ?"
        params.append(f"%{stack.lower()}%")

    if sort == "price_asc":
        query += " ORDER BY l.asking_price_usd ASC"
    elif sort == "price_desc":
        query += " ORDER BY l.asking_price_usd DESC"
    elif sort == "mrr_desc":
        query += " ORDER BY l.mrr_usd DESC"
    else:
        query += " ORDER BY l.created_at DESC"

    listings = db.execute(query, params).fetchall()
    return render_template("listings.html", listings=listings)


# ---------------------------------------------------------------------------
# Routes: Listings CRUD
# ---------------------------------------------------------------------------

@app.route("/listings/new", methods=["GET", "POST"])
@login_required
def create_listing():
    if request.method == "POST":
        screenshot_url = request.form.get("screenshot_url", "").strip()
        f = request.files.get("screenshot")
        if f and f.filename:
            fname = secure_filename(f.filename)
            fname = f"{int(datetime.utcnow().timestamp())}_{fname}"
            f.save(os.path.join(UPLOAD_FOLDER, fname))
            screenshot_url = f"/static/uploads/{fname}"
        db = get_db()
        db.execute(
            "INSERT INTO listings (user_id, title, description, url, asking_price_usd, "
            "mrr_usd, stack, screenshot_url, status) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                session["user_id"],
                request.form.get("title", "").strip(),
                request.form.get("description", "").strip(),
                request.form.get("url", "").strip(),
                int(request.form.get("asking_price_usd", 0) or 0),
                int(request.form.get("mrr_usd", 0) or 0),
                request.form.get("stack", "").strip(),
                screenshot_url,
                request.form.get("status", "draft"),
            ),
        )
        db.commit()
        flash("Listing created!", "success")
        return redirect(url_for("dashboard"))
    return render_template("listing_form.html", listing=None)


@app.route("/listings/<int:id>")
def show_listing(id):
    db = get_db()
    listing = db.execute(
        "SELECT l.*, u.name as seller_name, u.email as seller_email, u.bio as seller_bio "
        "FROM listings l JOIN users u ON l.user_id = u.id WHERE l.id = ?", (id,)
    ).fetchone()
    if not listing:
        abort(404)
    already_interested = False
    if session.get("user_id"):
        already_interested = db.execute(
            "SELECT id FROM intents WHERE listing_id = ? AND buyer_id = ?",
            (id, session["user_id"])
        ).fetchone() is not None
    return render_template(
        "listing_detail.html", listing=listing, already_interested=already_interested
    )


@app.route("/listings/<int:id>/edit", methods=["GET", "POST"])
@login_required
def edit_listing(id):
    db = get_db()
    listing = db.execute(
        "SELECT * FROM listings WHERE id = ? AND user_id = ?",
        (id, session["user_id"]),
    ).fetchone()
    if not listing:
        abort(403)
    if request.method == "POST":
        screenshot_url = listing["screenshot_url"]
        f = request.files.get("screenshot")
        if f and f.filename:
            fname = secure_filename(f.filename)
            fname = f"{int(datetime.utcnow().timestamp())}_{fname}"
            f.save(os.path.join(UPLOAD_FOLDER, fname))
            screenshot_url = f"/static/uploads/{fname}"
        url_field = request.form.get("screenshot_url", "").strip()
        if url_field:
            screenshot_url = url_field
        db.execute(
            "UPDATE listings SET title=?, description=?, url=?, asking_price_usd=?, mrr_usd=?, "
            "stack=?, screenshot_url=?, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (
                request.form.get("title", "").strip(),
                request.form.get("description", "").strip(),
                request.form.get("url", "").strip(),
                int(request.form.get("asking_price_usd", 0) or 0),
                int(request.form.get("mrr_usd", 0) or 0),
                request.form.get("stack", "").strip(),
                screenshot_url,
                request.form.get("status", listing["status"]),
                id,
            ),
        )
        db.commit()
        flash("Listing updated!", "success")
        return redirect(url_for("show_listing", id=id))
    return render_template("listing_form.html", listing=listing)


# ---------------------------------------------------------------------------
# Routes: Contact Intent
# ---------------------------------------------------------------------------

@app.route("/listings/<int:id>/interest", methods=["POST"])
@login_required
def send_interest(id):
    db = get_db()
    listing = db.execute(
        "SELECT l.*, u.email as seller_email, u.name as seller_name "
        "FROM listings l JOIN users u ON l.user_id = u.id WHERE l.id = ?", (id,)
    ).fetchone()
    if not listing:
        abort(404)
    if listing["user_id"] == session["user_id"]:
        flash("You can't express interest in your own listing.", "error")
        return redirect(url_for("show_listing", id=id))
    existing = db.execute(
        "SELECT id FROM intents WHERE listing_id = ? AND buyer_id = ?",
        (id, session["user_id"]),
    ).fetchone()
    if existing:
        flash("You already expressed interest.", "warning")
        return redirect(url_for("show_listing", id=id))
    message = request.form.get("message", "").strip()
    db.execute(
        "INSERT INTO intents (listing_id, buyer_id, message) VALUES (?,?,?)",
        (id, session["user_id"], message),
    )
    db.commit()
    buyer = current_user()
    send_email(
        listing["seller_email"],
        f"New interest in \"{listing['title']}\" on MicroFlip",
        f"Hi {listing['seller_name']},\n\n"
        f"{buyer['name']} ({buyer['email']}) is interested in your listing "
        f"\"{listing['title']}\".\n\n"
        f"Message: {message or '(no message)'}\n\n"
        f"Reply directly to {buyer['email']} to continue the conversation.\n\n"
        f"-- MicroFlip",
    )
    flash("Interest sent! The seller will receive your contact info by email.", "success")
    return redirect(url_for("show_listing", id=id))


# ---------------------------------------------------------------------------
# Routes: Stripe Verify
# ---------------------------------------------------------------------------

@app.route("/connect/stripe")
@login_required
def stripe_oauth():
    listing_id = request.args.get("listing_id")
    if not listing_id:
        flash("Missing listing ID.", "error")
        return redirect(url_for("dashboard"))
    if not STRIPE_CLIENT_ID:
        flash("Stripe not configured. Set STRIPE_CLIENT_ID and STRIPE_API_KEY.", "error")
        return redirect(url_for("show_listing", id=listing_id))
    params = urlencode({
        "response_type": "code",
        "client_id": STRIPE_CLIENT_ID,
        "scope": "read_only",
        "state": f"{session['user_id']}:{listing_id}",
        "redirect_uri": url_for("stripe_callback", _external=True),
    })
    return redirect(f"https://connect.stripe.com/oauth/authorize?{params}")


@app.route("/connect/stripe/callback")
def stripe_callback():
    import urllib.request
    import urllib.parse

    code = request.args.get("code")
    state = request.args.get("state", "")
    error = request.args.get("error")
    if error or not code:
        flash(f"Stripe connection failed: {error or 'no code returned'}", "error")
        return redirect(url_for("dashboard"))
    parts = state.split(":")
    if len(parts) != 2:
        flash("Invalid state parameter.", "error")
        return redirect(url_for("dashboard"))
    user_id, listing_id = int(parts[0]), int(parts[1])
    if not current_user() or current_user()["id"] != user_id:
        abort(403)
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "client_secret": STRIPE_API_KEY,
    }).encode()
    req = urllib.request.Request(
        "https://connect.stripe.com/oauth/token", data=data, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            token_data = json.loads(resp.read())
    except Exception as e:
        flash(f"Stripe token exchange failed: {e}", "error")
        return redirect(url_for("show_listing", id=listing_id))
    stripe_account_id = token_data.get("stripe_user_id", "")
    access_token = token_data.get("access_token", "")
    verified_mrr = 0
    if access_token:
        verified_mrr = fetch_stripe_mrr(access_token)
    db = get_db()
    db.execute(
        "UPDATE listings SET stripe_verified = 1, stripe_account_id = ?, mrr_usd = ? "
        "WHERE id = ? AND user_id = ?",
        (stripe_account_id, verified_mrr, listing_id, user_id),
    )
    db.commit()
    flash(f"Stripe connected! Verified MRR: ${verified_mrr}/mo", "success")
    return redirect(url_for("show_listing", id=listing_id))


def fetch_stripe_mrr(access_token):
    import urllib.request
    now = int(time.time())
    thirty_days_ago = now - 30 * 86400
    url = (
        f"https://api.stripe.com/v1/charges"
        f"?created[gte]={thirty_days_ago}&limit=100"
    )
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {access_token}")
    try:
        with urllib.request.urlopen(req) as resp:
            charges = json.loads(resp.read())
        total_cents = sum(
            c["amount"] for c in charges.get("data", [])
            if c.get("paid") and not c.get("refunded")
        )
        return total_cents // 100
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Routes: Dashboard
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    uid = session["user_id"]
    listings = db.execute(
        "SELECT * FROM listings WHERE user_id = ? ORDER BY created_at DESC", (uid,)
    ).fetchall()
    intents = db.execute(
        "SELECT i.*, l.title as listing_title, u.name as buyer_name, u.email as buyer_email "
        "FROM intents i "
        "JOIN listings l ON i.listing_id = l.id "
        "JOIN users u ON i.buyer_id = u.id "
        "WHERE l.user_id = ? ORDER BY i.created_at DESC", (uid,)
    ).fetchall()
    return render_template("dashboard.html", listings=listings, intents=intents)


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
