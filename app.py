import os
import json
import sqlite3
from datetime import datetime
from functools import wraps

from flask import (
    Flask, request, redirect, url_for, render_template,
    session, flash, g, abort, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
DATABASE = os.environ.get("DATABASE_URL", "microflip.db")
STRIPE_CLIENT_ID = os.environ.get("STRIPE_CLIENT_ID", "")
STRIPE_API_KEY = os.environ.get("STRIPE_API_KEY", "")
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# --------------- DB helpers ---------------

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
        role TEXT NOT NULL DEFAULT 'both' CHECK(role IN ('seller','buyer','both')),
        stripe_account_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS listings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        seller_id INTEGER NOT NULL REFERENCES users(id),
        title TEXT NOT NULL,
        description TEXT,
        url TEXT,
        asking_price_usd INTEGER NOT NULL,
        mrr_usd INTEGER NOT NULL DEFAULT 0,
        mrr_verified INTEGER NOT NULL DEFAULT 0,
        stripe_mrr_data TEXT,
        screenshot_url TEXT,
        status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','active','sold')),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS offers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        listing_id INTEGER NOT NULL REFERENCES listings(id),
        buyer_id INTEGER NOT NULL REFERENCES users(id),
        amount_usd INTEGER NOT NULL,
        message TEXT,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected','withdrawn')),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        offer_id INTEGER NOT NULL REFERENCES offers(id),
        sender_id INTEGER NOT NULL REFERENCES users(id),
        body TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    db.close()

# --------------- Auth helpers ---------------

def current_user():
    if "user_id" not in session:
        return None
    if not hasattr(g, "_user"):
        g._user = get_db().execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    return g._user

@app.context_processor
def inject_user():
    return dict(current_user=current_user())

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if current_user() is None:
            flash("Faça login para continuar.", "warning")
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return decorated

# --------------- Formatting helpers ---------------

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
        return "agora"
    if seconds < 3600:
        return f"{seconds // 60}min atrás"
    if seconds < 86400:
        return f"{seconds // 3600}h atrás"
    return f"{seconds // 86400}d atrás"

# --------------- Routes: Auth ---------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        name = request.form.get("name", "").strip()
        role = request.form.get("role", "both")
        if not email or not password or not name:
            flash("Preencha todos os campos.", "error")
            return redirect(url_for("register"))
        if len(password) < 6:
            flash("Senha deve ter pelo menos 6 caracteres.", "error")
            return redirect(url_for("register"))
        db = get_db()
        if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
            flash("Email já cadastrado.", "error")
            return redirect(url_for("register"))
        db.execute(
            "INSERT INTO users (email, password_hash, name, role) VALUES (?,?,?,?)",
            (email, generate_password_hash(password), name, role),
        )
        db.commit()
        user = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        session["user_id"] = user["id"]
        flash("Conta criada com sucesso!", "success")
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
            flash("Login realizado!", "success")
            next_url = request.args.get("next") or url_for("dashboard")
            return redirect(next_url)
        flash("Email ou senha inválidos.", "error")
    return render_template("login.html")

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Você saiu.", "info")
    return redirect(url_for("index"))

# --------------- Routes: Landing ---------------

@app.route("/")
def index():
    db = get_db()
    listings = db.execute(
        "SELECT l.*, u.name as seller_name FROM listings l JOIN users u ON l.seller_id=u.id "
        "WHERE l.status='active' ORDER BY l.created_at DESC LIMIT 6"
    ).fetchall()
    return render_template("index.html", listings=listings)

# --------------- Routes: Listings ---------------

@app.route("/listings")
def browse_listings():
    db = get_db()
    query = "SELECT l.*, u.name as seller_name FROM listings l JOIN users u ON l.seller_id=u.id WHERE l.status='active'"
    params = []
    price_min = request.args.get("price_min", type=int)
    price_max = request.args.get("price_max", type=int)
    mrr_min = request.args.get("mrr_min", type=int)
    mrr_max = request.args.get("mrr_max", type=int)
    search = request.args.get("q", "").strip()
    if price_min is not None:
        query += " AND l.asking_price_usd >= ?"
        params.append(price_min)
    if price_max is not None:
        query += " AND l.asking_price_usd <= ?"
        params.append(price_max)
    if mrr_min is not None:
        query += " AND l.mrr_usd >= ?"
        params.append(mrr_min)
    if mrr_max is not None:
        query += " AND l.mrr_usd <= ?"
        params.append(mrr_max)
    if search:
        query += " AND (l.title LIKE ? OR l.description LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    sort = request.args.get("sort", "recent")
    if sort == "price_asc":
        query += " ORDER BY l.asking_price_usd ASC"
    elif sort == "price_desc":
        query += " ORDER BY l.asking_price_usd DESC"
    else:
        query += " ORDER BY l.created_at DESC"
    listings = db.execute(query, params).fetchall()
    return render_template("listings.html", listings=listings)

@app.route("/listings/new", methods=["GET", "POST"])
@login_required
def create_listing():
    if request.method == "POST":
        db = get_db()
        screenshot_url = ""
        f = request.files.get("screenshot")
        if f and f.filename:
            fname = secure_filename(f.filename)
            fname = f"{int(datetime.utcnow().timestamp())}_{fname}"
            f.save(os.path.join(UPLOAD_FOLDER, fname))
            screenshot_url = f"/static/uploads/{fname}"
        db.execute(
            "INSERT INTO listings (seller_id, title, description, url, asking_price_usd, mrr_usd, screenshot_url, status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                session["user_id"],
                request.form["title"],
                request.form.get("description", ""),
                request.form.get("url", ""),
                int(request.form["asking_price"]),
                int(request.form.get("mrr", 0)),
                screenshot_url,
                request.form.get("status", "draft"),
            ),
        )
        db.commit()
        flash("Listing criado!", "success")
        return redirect(url_for("dashboard"))
    return render_template("listing_new.html")

@app.route("/listings/<int:id>")
def view_listing(id):
    db = get_db()
    listing = db.execute(
        "SELECT l.*, u.name as seller_name, u.email as seller_email FROM listings l "
        "JOIN users u ON l.seller_id=u.id WHERE l.id=?", (id,)
    ).fetchone()
    if not listing:
        abort(404)
    offers = []
    user = current_user()
    if user and (user["id"] == listing["seller_id"]):
        offers = db.execute(
            "SELECT o.*, u.name as buyer_name FROM offers o JOIN users u ON o.buyer_id=u.id "
            "WHERE o.listing_id=? ORDER BY o.created_at DESC", (id,)
        ).fetchall()
    mrr_data = None
    if listing["stripe_mrr_data"]:
        mrr_data = json.loads(listing["stripe_mrr_data"])
    return render_template("listing_detail.html", listing=listing, offers=offers, mrr_data=mrr_data)

@app.route("/listings/<int:id>/edit", methods=["GET", "POST"])
@login_required
def edit_listing(id):
    db = get_db()
    listing = db.execute("SELECT * FROM listings WHERE id=? AND seller_id=?", (id, session["user_id"])).fetchone()
    if not listing:
        abort(404)
    if request.method == "POST":
        screenshot_url = listing["screenshot_url"]
        f = request.files.get("screenshot")
        if f and f.filename:
            fname = secure_filename(f.filename)
            fname = f"{int(datetime.utcnow().timestamp())}_{fname}"
            f.save(os.path.join(UPLOAD_FOLDER, fname))
            screenshot_url = f"/static/uploads/{fname}"
        db.execute(
            "UPDATE listings SET title=?, description=?, url=?, asking_price_usd=?, mrr_usd=?, "
            "screenshot_url=?, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (
                request.form["title"],
                request.form.get("description", ""),
                request.form.get("url", ""),
                int(request.form["asking_price"]),
                int(request.form.get("mrr", 0)),
                screenshot_url,
                request.form.get("status", listing["status"]),
                id,
            ),
        )
        db.commit()
        flash("Listing atualizado!", "success")
        return redirect(url_for("view_listing", id=id))
    return render_template("listing_edit.html", listing=listing)

# --------------- Routes: Stripe Verify ---------------

@app.route("/listings/<int:id>/verify", methods=["POST"])
@login_required
def start_stripe_verify(id):
    db = get_db()
    listing = db.execute("SELECT * FROM listings WHERE id=? AND seller_id=?", (id, session["user_id"])).fetchone()
    if not listing:
        abort(404)
    if not STRIPE_CLIENT_ID:
        flash("Stripe não configurado. Defina STRIPE_CLIENT_ID e STRIPE_API_KEY.", "error")
        return redirect(url_for("view_listing", id=id))
    redirect_uri = url_for("stripe_callback", _external=True)
    state = f"{id}:{session['user_id']}"
    stripe_url = (
        f"https://connect.stripe.com/oauth/authorize?response_type=code"
        f"&client_id={STRIPE_CLIENT_ID}&scope=read_only"
        f"&redirect_uri={redirect_uri}&state={state}"
    )
    return redirect(stripe_url)

@app.route("/stripe/callback")
def stripe_callback():
    import urllib.request
    import urllib.parse

    code = request.args.get("code")
    state = request.args.get("state", "")
    error = request.args.get("error")
    if error:
        flash(f"Stripe recusado: {request.args.get('error_description', error)}", "error")
        return redirect(url_for("dashboard"))
    parts = state.split(":")
    if len(parts) != 2:
        abort(400)
    listing_id, user_id = int(parts[0]), int(parts[1])
    if not current_user() or current_user()["id"] != user_id:
        abort(403)
    # Exchange code for access token
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "client_secret": STRIPE_API_KEY,
    }).encode()
    req = urllib.request.Request("https://connect.stripe.com/oauth/token", data=data, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            token_data = json.loads(resp.read())
    except Exception as e:
        flash(f"Erro ao conectar Stripe: {e}", "error")
        return redirect(url_for("view_listing", id=listing_id))
    stripe_account_id = token_data.get("stripe_user_id", "")
    access_token = token_data.get("access_token", "")
    db = get_db()
    db.execute("UPDATE users SET stripe_account_id=? WHERE id=?", (stripe_account_id, user_id))
    # Fetch MRR data from last 6 months of charges
    mrr_data = fetch_stripe_mrr(access_token)
    db.execute(
        "UPDATE listings SET mrr_verified=1, stripe_mrr_data=? WHERE id=? AND seller_id=?",
        (json.dumps(mrr_data), listing_id, user_id),
    )
    db.commit()
    flash("MRR verificado com sucesso via Stripe!", "success")
    return redirect(url_for("view_listing", id=listing_id))

def fetch_stripe_mrr(access_token):
    """Fetch recurring revenue data from Stripe using the connected account's access token."""
    import urllib.request
    from datetime import timedelta
    months = []
    now = datetime.utcnow()
    for i in range(6):
        month_start = now.replace(day=1) - timedelta(days=30 * i)
        month_start = month_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if month_start.month == 12:
            month_end = month_start.replace(year=month_start.year + 1, month=1)
        else:
            month_end = month_start.replace(month=month_start.month + 1)
        url = (
            f"https://api.stripe.com/v1/charges?created[gte]={int(month_start.timestamp())}"
            f"&created[lt]={int(month_end.timestamp())}&limit=100"
        )
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {access_token}")
        try:
            with urllib.request.urlopen(req) as resp:
                charges = json.loads(resp.read())
            total = sum(c["amount"] for c in charges.get("data", []) if c["paid"]) // 100
        except Exception:
            total = 0
        months.append({"month": month_start.strftime("%Y-%m"), "revenue": total})
    months.reverse()
    return months

# --------------- Routes: Offers ---------------

@app.route("/listings/<int:id>/offer", methods=["POST"])
@login_required
def create_offer(id):
    db = get_db()
    listing = db.execute("SELECT * FROM listings WHERE id=? AND status='active'", (id,)).fetchone()
    if not listing:
        abort(404)
    if listing["seller_id"] == session["user_id"]:
        flash("Você não pode fazer oferta no próprio listing.", "error")
        return redirect(url_for("view_listing", id=id))
    amount = int(request.form["amount"])
    message = request.form.get("message", "")
    db.execute(
        "INSERT INTO offers (listing_id, buyer_id, amount_usd, message) VALUES (?,?,?,?)",
        (id, session["user_id"], amount, message),
    )
    db.commit()
    flash("Oferta enviada!", "success")
    return redirect(url_for("view_listing", id=id))

@app.route("/offers/<int:id>/respond", methods=["POST"])
@login_required
def respond_offer(id):
    db = get_db()
    offer = db.execute(
        "SELECT o.*, l.seller_id FROM offers o JOIN listings l ON o.listing_id=l.id WHERE o.id=?", (id,)
    ).fetchone()
    if not offer or offer["seller_id"] != session["user_id"]:
        abort(403)
    action = request.form.get("action")
    if action in ("accepted", "rejected"):
        db.execute("UPDATE offers SET status=? WHERE id=?", (action, id))
        db.commit()
        flash(f"Oferta {action}!", "success")
    return redirect(url_for("view_listing", id=offer["listing_id"]))

# --------------- Routes: Messages ---------------

@app.route("/offers/<int:id>/messages", methods=["GET", "POST"])
@login_required
def offer_messages(id):
    db = get_db()
    offer = db.execute(
        "SELECT o.*, l.seller_id, l.title as listing_title FROM offers o "
        "JOIN listings l ON o.listing_id=l.id WHERE o.id=?", (id,)
    ).fetchone()
    if not offer:
        abort(404)
    uid = session["user_id"]
    if uid != offer["buyer_id"] and uid != offer["seller_id"]:
        abort(403)
    if request.method == "POST":
        body = request.form.get("body", "").strip()
        if body:
            db.execute(
                "INSERT INTO messages (offer_id, sender_id, body) VALUES (?,?,?)",
                (id, uid, body),
            )
            db.commit()
        if request.headers.get("HX-Request"):
            msgs = db.execute(
                "SELECT m.*, u.name as sender_name FROM messages m JOIN users u ON m.sender_id=u.id "
                "WHERE m.offer_id=? ORDER BY m.created_at ASC", (id,)
            ).fetchall()
            return render_template("_messages.html", messages=msgs, current_user_id=uid)
        return redirect(url_for("offer_messages", id=id))
    msgs = db.execute(
        "SELECT m.*, u.name as sender_name FROM messages m JOIN users u ON m.sender_id=u.id "
        "WHERE m.offer_id=? ORDER BY m.created_at ASC", (id,)
    ).fetchall()
    return render_template("offer_messages.html", offer=offer, messages=msgs)

# --------------- Routes: Dashboard ---------------

@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    uid = session["user_id"]
    my_listings = db.execute(
        "SELECT * FROM listings WHERE seller_id=? ORDER BY created_at DESC", (uid,)
    ).fetchall()
    received_offers = db.execute(
        "SELECT o.*, l.title as listing_title, u.name as buyer_name "
        "FROM offers o JOIN listings l ON o.listing_id=l.id JOIN users u ON o.buyer_id=u.id "
        "WHERE l.seller_id=? ORDER BY o.created_at DESC", (uid,)
    ).fetchall()
    sent_offers = db.execute(
        "SELECT o.*, l.title as listing_title, u.name as seller_name "
        "FROM offers o JOIN listings l ON o.listing_id=l.id JOIN users u ON l.seller_id=u.id "
        "WHERE o.buyer_id=? ORDER BY o.created_at DESC", (uid,)
    ).fetchall()
    return render_template("dashboard.html", my_listings=my_listings,
                           received_offers=received_offers, sent_offers=sent_offers)

# --------------- Run ---------------

if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
