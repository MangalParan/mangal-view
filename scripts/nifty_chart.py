"""
Nifty Candlestick Chart Server
Serves an interactive TradingView-style candlestick chart for Nifty 50.
Supports 3min, 5min, 15min, 1hr, 1day timeframes.

Usage:
    python scripts/nifty_chart.py
    Then open http://localhost:5050 in your browser.
"""

import json
import math
import os
import random
import re
import string
import uuid
import sqlite3
import hashlib
import secrets
import functools
from datetime import datetime, timedelta

import numpy as np
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.preprocessing import StandardScaler
import websocket
import yfinance as yf
from curl_cffi import requests as cffi_requests

from flask import Flask, jsonify, request, Response, redirect, session, g

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# --- User Database ---
_default_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "users.db")
DB_PATH = os.environ.get("DB_PATH", _default_db)
try:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
except PermissionError:
    DB_PATH = _default_db
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            mobileno TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            place TEXT DEFAULT '',
            plan TEXT DEFAULT 'free',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS site_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    # Migrate: add columns if they don't exist (for existing DBs)
    cols = [r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()]
    if "username" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN username TEXT DEFAULT ''")
    if "place" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN place TEXT DEFAULT ''")
    if "plan" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'free'")
    # Seed default site settings
    defaults = {
        "maintenance_mode": "off",
        "settings_backtest": "on",
        "settings_datasource": "on",
        "settings_trade": "on",
        "settings_realtrade": "on",
        "menu_symbols": json.dumps(["NIFTY50","BANKNIFTY","SENSEX","GOLD","SILVER","XAUUSD","XAGUSD","GOLDTEN","SILVERBEES","BTC","ETH","DJI","NASDAQ","SP500","CRUDEOIL","NATURALGAS"]),
        "menu_timeframes": json.dumps(["1m","2m","3m","5m","10m","15m","30m","1h","2h","4h","1d","1w","1mo"]),
        "menu_indicators": json.dumps(["ST","SAR","SR","EMA","VWAP","BB","CPR","LP","FVG","BOS","CHoCH","CVD","VP","Signals"]),
        "menu_algos": json.dumps(["trend","mstreet","mfactor","sniper","orderflow","priceaction","breakout","momentum","scalping","smartmoney","quant","hybrid","statarb","institution","mpredict"]),
    }
    for k, v in defaults.items():
        db.execute("INSERT OR IGNORE INTO site_settings (key, value) VALUES (?, ?)", (k, v))
    # Migrate: ensure new algos are in menu_algos for existing DBs
    _all_algos = ["trend","mstreet","mfactor","sniper","orderflow","priceaction","breakout","momentum","scalping","smartmoney","quant","hybrid","statarb","institution","mpredict"]
    _row = db.execute("SELECT value FROM site_settings WHERE key = 'menu_algos'").fetchone()
    if _row:
        try:
            _existing = json.loads(_row[0])
            _missing = [a for a in _all_algos if a not in _existing]
            if _missing:
                _updated = _existing + _missing
                db.execute("UPDATE site_settings SET value = ? WHERE key = 'menu_algos'", (json.dumps(_updated),))
        except Exception:
            pass
    db.commit()
    db.close()


def get_site_setting(key, default="off"):
    db = get_db()
    row = db.execute("SELECT value FROM site_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_site_setting(key, value):
    db = get_db()
    db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)", (key, value))
    db.commit()


def hash_password(password):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200000)
    return salt + ":" + h.hex()


def verify_password(password, stored_hash):
    parts = stored_hash.split(":", 1)
    if len(parts) != 2:
        return False
    salt, expected = parts
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200000)
    return secrets.compare_digest(h.hex(), expected)


MAINTENANCE_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Maintenance - Mangal View</title>
<style>*{margin:0;padding:0;box-sizing:border-box}body{background:#131722;color:#d1d4dc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center}
.box{background:#1e222d;border-radius:16px;padding:48px 40px;max-width:480px;box-shadow:0 8px 32px rgba(0,0,0,.4)}
.icon{font-size:64px;margin-bottom:16px}h1{color:#ffd600;font-size:24px;margin-bottom:12px}
p{color:#787b86;font-size:15px;line-height:1.6}</style></head>
<body><div class="box"><div class="icon">&#128679;</div><h1>Under Maintenance</h1>
<p>Mangal View is currently undergoing scheduled maintenance.<br>We'll be back shortly. Thank you for your patience.</p></div></body></html>"""


def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect("/login")
        # Check maintenance mode (allow admin through)
        if not session.get("admin"):
            try:
                if get_site_setting("maintenance_mode", "off") == "on":
                    if request.path.startswith("/api/"):
                        return jsonify({"error": "Site is under maintenance"}), 503
                    return Response(MAINTENANCE_HTML, status=503, content_type="text/html")
            except Exception:
                pass
        return f(*args, **kwargs)
    return decorated


init_db()


@app.before_request
def check_maintenance_global():
    """Block entire site when maintenance mode is on, except admin panel access."""
    # Allow admin panel access (with key) and admin API routes
    if request.path.startswith("/admin"):
        return None
    # Allow static assets (if any)
    if request.path.startswith("/static"):
        return None
    # Check maintenance mode
    try:
        with sqlite3.connect(DB_PATH) as _db:
            _row = _db.execute("SELECT value FROM site_settings WHERE key = 'maintenance_mode'").fetchone()
            if _row and _row[0] == "on":
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Site is under maintenance"}), 503
                return Response(MAINTENANCE_HTML, status=503, content_type="text/html")
    except Exception:
        pass
    return None


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Login - Mangal View</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #131722; color: #d1d4dc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; }
  .login-box { background: #1e222d; border-radius: 12px; padding: 40px; width: 380px; box-shadow: 0 8px 32px rgba(0,0,0,0.5); }
  .login-box h1 { text-align: center; margin-bottom: 8px; color: #2962ff; font-size: 24px; }
  .login-box p.subtitle { text-align: center; color: #787b86; margin-bottom: 28px; font-size: 14px; }
  .form-group { margin-bottom: 20px; }
  .form-group label { display: block; margin-bottom: 6px; color: #787b86; font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; }
  .form-group input { width: 100%; padding: 12px 14px; background: #131722; border: 1px solid #363a45; border-radius: 6px; color: #d1d4dc; font-size: 15px; outline: none; transition: border-color 0.2s; }
  .form-group input:focus { border-color: #2962ff; }
  .btn { width: 100%; padding: 12px; background: #2962ff; color: #fff; border: none; border-radius: 6px; font-size: 15px; font-weight: 600; cursor: pointer; transition: background 0.2s; }
  .btn:hover { background: #1e53e5; }
  .error { background: #ff444422; border: 1px solid #ff4444; color: #ff6b6b; padding: 10px 14px; border-radius: 6px; margin-bottom: 20px; font-size: 13px; text-align: center; }
  .signup-link { text-align: center; margin-top: 20px; font-size: 13px; color: #787b86; }
  .signup-link a { color: #2962ff; text-decoration: none; }
  .signup-link a:hover { text-decoration: underline; }
</style>
</head>
<body>
<div class="login-box">
  <h1>Mangal View</h1>
  <p class="subtitle">Sign in to access the trading tool</p>
  {{ERROR}}
  <form method="POST" action="/login">
    <div class="form-group">
      <label>Mobile Number</label>
      <input type="tel" name="mobileno" placeholder="Enter 10-digit mobile" pattern="[0-9]{10}" maxlength="10" required autofocus>
    </div>
    <div class="form-group">
      <label>Password</label>
      <input type="password" name="password" placeholder="Enter password" required>
    </div>
    <button class="btn" type="submit">Sign In</button>
  </form>
  <div class="signup-link">Don't have an account? <a href="/register">Register</a></div>
</div>
</body>
</html>"""


REGISTER_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Register - Mangal View</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #131722; color: #d1d4dc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; }
  .login-box { background: #1e222d; border-radius: 12px; padding: 40px; width: 420px; box-shadow: 0 8px 32px rgba(0,0,0,0.5); }
  .login-box h1 { text-align: center; margin-bottom: 8px; color: #2962ff; font-size: 24px; }
  .login-box p.subtitle { text-align: center; color: #787b86; margin-bottom: 28px; font-size: 14px; }
  .form-group { margin-bottom: 16px; }
  .form-group label { display: block; margin-bottom: 6px; color: #787b86; font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; }
  .form-group input[type="text"], .form-group input[type="tel"], .form-group input[type="password"] { width: 100%; padding: 12px 14px; background: #131722; border: 1px solid #363a45; border-radius: 6px; color: #d1d4dc; font-size: 15px; outline: none; transition: border-color 0.2s; }
  .form-group input:focus { border-color: #2962ff; }
  .btn { width: 100%; padding: 12px; background: #2962ff; color: #fff; border: none; border-radius: 6px; font-size: 15px; font-weight: 600; cursor: pointer; transition: background 0.2s; }
  .btn:hover { background: #1e53e5; }
  .error { background: #ff444422; border: 1px solid #ff4444; color: #ff6b6b; padding: 10px 14px; border-radius: 6px; margin-bottom: 16px; font-size: 13px; text-align: center; }
  .success { background: #00c85322; border: 1px solid #00c853; color: #69f0ae; padding: 10px 14px; border-radius: 6px; margin-bottom: 16px; font-size: 13px; text-align: center; }
  .signup-link { text-align: center; margin-top: 20px; font-size: 13px; color: #787b86; }
  .signup-link a { color: #2962ff; text-decoration: none; }
  .signup-link a:hover { text-decoration: underline; }
  .plan-group { display: flex; gap: 20px; margin-top: 8px; }
  .plan-option { display: flex; align-items: center; gap: 8px; cursor: pointer; }
  .plan-option input[type="radio"] { accent-color: #2962ff; width: 16px; height: 16px; cursor: pointer; }
  .plan-option span { font-size: 14px; color: #d1d4dc; }
  .plan-info { margin-top: 10px; padding: 10px 14px; border-radius: 6px; font-size: 12px; line-height: 1.5; display: none; }
  .plan-info.free-info { background: #1b5e2022; border: 1px solid #43a047; color: #69f0ae; }
  .plan-info.paid-info { background: #ff6d0022; border: 1px solid #ff6d00; color: #ffab40; }
</style>
</head>
<body>
<div class="login-box">
  <h1>Mangal View</h1>
  <p class="subtitle">Create a new account</p>
  {{ERROR}}
  <form method="POST" action="/register">
    <div class="form-group">
      <label>Username</label>
      <input type="text" name="username" placeholder="Enter your name" required autofocus>
    </div>
    <div class="form-group">
      <label>Mobile Number</label>
      <input type="tel" name="mobileno" placeholder="Enter 10-digit mobile" pattern="[0-9]{10}" maxlength="10" required>
    </div>
    <div class="form-group">
      <label>Password</label>
      <input type="password" name="password" placeholder="Min 6 characters" minlength="6" required>
    </div>
    <div class="form-group">
      <label>Confirm Password</label>
      <input type="password" name="confirm_password" placeholder="Re-enter password" minlength="6" required>
    </div>
    <div class="form-group">
      <label>Place</label>
      <input type="text" name="place" placeholder="City / Town" required>
    </div>
    <div class="form-group">
      <label>Plan</label>
      <div class="plan-group">
        <label class="plan-option"><input type="radio" name="plan" value="free" checked onchange="document.getElementById('freeInfo').style.display='block';document.getElementById('paidInfo').style.display='none'"><span>Free Trial</span></label>
        <label class="plan-option"><input type="radio" name="plan" value="paid" onchange="document.getElementById('paidInfo').style.display='block';document.getElementById('freeInfo').style.display='none'"><span>Paid</span></label>
      </div>
      <div class="plan-info free-info" id="freeInfo" style="display:block">&#10003; 1 month free evaluation. No payment required.</div>
      <div class="plan-info paid-info" id="paidInfo">&#8377; 100/month &mdash; Contact <b>Mangal</b> at <b>95000 90975</b></div>
    </div>
    <button class="btn" type="submit">Register</button>
  </form>
  <div class="signup-link">Already have an account? <a href="/login">Sign In</a></div>
</div>
</body>
</html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return Response(LOGIN_PAGE.replace("{{ERROR}}", ""), content_type="text/html")
    mobileno = request.form.get("mobileno", "").strip()
    password = request.form.get("password", "")
    if not mobileno or not password:
        return Response(LOGIN_PAGE.replace("{{ERROR}}", '<div class="error">Please enter mobile number and password.</div>'), content_type="text/html")
    if not re.fullmatch(r"\d{10}", mobileno):
        return Response(LOGIN_PAGE.replace("{{ERROR}}", '<div class="error">Enter a valid 10-digit mobile number.</div>'), content_type="text/html")
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE mobileno = ?", (mobileno,)).fetchone()
    if user and verify_password(password, user["password_hash"]):
        # Check free tier expiry (30 days)
        if user["plan"] == "free":
            try:
                created = datetime.strptime(user["created_at"], "%Y-%m-%d %H:%M:%S")
            except Exception:
                created = datetime.utcnow()
            if datetime.utcnow() - created > timedelta(days=30):
                return Response(LOGIN_PAGE.replace("{{ERROR}}",
                    '<div class="error">Free Eval version over. <a href="/register">Re-register</a> with same name and mobile number for paid version.</div>'),
                    content_type="text/html")
        session["user_id"] = user["id"]
        session["mobileno"] = user["mobileno"]
        return redirect("/")
    return Response(LOGIN_PAGE.replace("{{ERROR}}", '<div class="error">Invalid mobile number or password.</div>'), content_type="text/html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return Response(REGISTER_PAGE.replace("{{ERROR}}", ""), content_type="text/html")
    username = request.form.get("username", "").strip()
    mobileno = request.form.get("mobileno", "").strip()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm_password", "")
    place = request.form.get("place", "").strip()
    plan = request.form.get("plan", "free").strip()
    if plan not in ("free", "paid"):
        plan = "free"
    if not username or not mobileno or not password or not confirm or not place:
        return Response(REGISTER_PAGE.replace("{{ERROR}}", '<div class="error">All fields are required.</div>'), content_type="text/html")
    if not re.fullmatch(r"\d{10}", mobileno):
        return Response(REGISTER_PAGE.replace("{{ERROR}}", '<div class="error">Enter a valid 10-digit mobile number.</div>'), content_type="text/html")
    if len(password) < 6:
        return Response(REGISTER_PAGE.replace("{{ERROR}}", '<div class="error">Password must be at least 6 characters.</div>'), content_type="text/html")
    if password != confirm:
        return Response(REGISTER_PAGE.replace("{{ERROR}}", '<div class="error">Passwords do not match.</div>'), content_type="text/html")
    db = get_db()
    existing = db.execute("SELECT id, plan FROM users WHERE mobileno = ?", (mobileno,)).fetchone()
    if existing:
        # Allow re-registration for paid upgrade after free expired
        if existing["plan"] == "free" and plan == "paid":
            pw_hash = hash_password(password)
            db.execute("UPDATE users SET username=?, password_hash=?, place=?, plan='paid', created_at=CURRENT_TIMESTAMP WHERE id=?",
                       (username, pw_hash, place, existing["id"]))
            db.commit()
            return Response(REGISTER_PAGE.replace("{{ERROR}}", '<div class="success">Upgraded to Paid! &#8377;100/month &mdash; Contact <b>Mangal</b> at <b>95000 90975</b>. <a href="/login">Sign in now</a></div>'), content_type="text/html")
        return Response(REGISTER_PAGE.replace("{{ERROR}}", '<div class="error">This mobile number is already registered.</div>'), content_type="text/html")
    pw_hash = hash_password(password)
    db.execute("INSERT INTO users (username, mobileno, password_hash, place, plan) VALUES (?, ?, ?, ?, ?)",
               (username, mobileno, pw_hash, place, plan))
    db.commit()
    if plan == "paid":
        return Response(REGISTER_PAGE.replace("{{ERROR}}", '<div class="success">Registration successful! &#8377;100/month &mdash; Contact <b>Mangal</b> at <b>95000 90975</b>. <a href="/login">Sign in now</a></div>'), content_type="text/html")
    return Response(REGISTER_PAGE.replace("{{ERROR}}", '<div class="success">Registration successful! 1 month free trial activated. <a href="/login">Sign in now</a></div>'), content_type="text/html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# --- Admin Panel ---
ADMIN_KEY = os.environ.get("ADMIN_KEY", "mangal2026")


@app.route("/admin", methods=["GET"])
def admin_page():
    key = request.args.get("key", "")
    if key != ADMIN_KEY:
        return Response('<h3 style="color:#ff4444;font-family:sans-serif;padding:40px">Unauthorized. Use /admin?key=YOUR_ADMIN_KEY</h3>', status=403, content_type="text/html")
    session["admin"] = True
    admin_html_path = os.path.join(os.path.dirname(__file__), "admin.html")
    with open(admin_html_path, "r", encoding="utf-8") as f:
        html = f.read()
    resp = Response(html, content_type="text/html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/admin/api/users", methods=["GET"])
def admin_list_users():
    if not session.get("admin"):
        return jsonify({"error": "Unauthorized"}), 401
    db = get_db()
    rows = db.execute("SELECT id, username, mobileno, place, plan, created_at FROM users ORDER BY id DESC").fetchall()
    return jsonify({"users": [dict(r) for r in rows]})


@app.route("/admin/api/users", methods=["POST"])
def admin_add_user():
    if not session.get("admin"):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    username = (data.get("username") or "").strip()
    mobileno = (data.get("mobileno") or "").strip()
    password = data.get("password") or ""
    place = (data.get("place") or "").strip()
    plan = data.get("plan", "free")
    if plan not in ("free", "paid"):
        plan = "free"
    if not username or not mobileno or not password or not place:
        return jsonify({"error": "All fields are required"}), 400
    if not re.fullmatch(r"\d{10}", mobileno):
        return jsonify({"error": "Enter a valid 10-digit mobile number"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE mobileno = ?", (mobileno,)).fetchone()
    if existing:
        return jsonify({"error": "Mobile number already registered"}), 409
    pw_hash = hash_password(password)
    db.execute("INSERT INTO users (username, mobileno, password_hash, place, plan) VALUES (?, ?, ?, ?, ?)",
               (username, mobileno, pw_hash, place, plan))
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/api/users", methods=["PUT"])
def admin_update_user():
    if not session.get("admin"):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    uid = data.get("id")
    if not uid:
        return jsonify({"error": "Missing user ID"}), 400
    db = get_db()
    user = db.execute("SELECT id FROM users WHERE id = ?", (uid,)).fetchone()
    if not user:
        return jsonify({"error": "User not found"}), 404
    db.execute("UPDATE users SET username=?, mobileno=?, place=?, plan=? WHERE id=?",
               (data.get("username", ""), data.get("mobileno", ""), data.get("place", ""), data.get("plan", "free"), uid))
    pwd = data.get("password", "")
    if pwd:
        pw_hash = hash_password(pwd)
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (pw_hash, uid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/api/users", methods=["DELETE"])
def admin_delete_user():
    if not session.get("admin"):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    uid = data.get("id")
    if not uid:
        return jsonify({"error": "Missing user ID"}), 400
    db = get_db()
    db.execute("DELETE FROM users WHERE id = ?", (uid,))
    db.commit()
    return jsonify({"ok": True})


# --- Admin Site Settings API ---
@app.route("/admin/api/settings", methods=["GET"])
def admin_get_settings():
    if not session.get("admin"):
        return jsonify({"error": "Unauthorized"}), 401
    db = get_db()
    rows = db.execute("SELECT key, value FROM site_settings").fetchall()
    settings = {r["key"]: r["value"] for r in rows}
    return jsonify({"ok": True, "settings": settings})


@app.route("/admin/api/settings", methods=["POST"])
def admin_update_settings():
    if not session.get("admin"):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    allowed_keys = {"maintenance_mode", "settings_backtest", "settings_datasource",
                    "settings_trade", "settings_realtrade",
                    "menu_symbols", "menu_timeframes", "menu_indicators", "menu_algos"}
    for key, value in data.items():
        if key in allowed_keys:
            if key.startswith("menu_"):
                # Menu config: value is a JSON array string
                if isinstance(value, list):
                    set_site_setting(key, json.dumps(value))
                elif isinstance(value, str):
                    set_site_setting(key, value)
            elif value in ("on", "off"):
                set_site_setting(key, value)
    return jsonify({"ok": True})


# --- User site settings API (for frontend to fetch visibility) ---
@app.route("/api/site-settings", methods=["GET"])
@login_required
def user_get_site_settings():
    db = get_db()
    rows = db.execute("SELECT key, value FROM site_settings").fetchall()
    settings = {r["key"]: r["value"] for r in rows}
    return jsonify(settings)


# --- Real Trade (Delta) State ---
delta_sessions = {}
delta_orders = {}

# --- Real Trade (Delta) API Stubs ---
@app.route('/api/realtrade/delta/login', methods=['POST'])
@login_required
def delta_login():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    # TODO: Integrate with Delta API
    if username and password:
        session_id = str(uuid.uuid4())
        delta_sessions[session_id] = {'username': username, 'token': 'mock_token'}
        return jsonify({'success': True, 'sessionId': session_id})
    return jsonify({'success': False, 'error': 'Missing credentials'}), 400

@app.route('/api/realtrade/delta/order', methods=['POST'])
@login_required
def delta_order():
    data = request.json
    session_id = data.get('sessionId')
    symbol = data.get('symbol')
    qty = data.get('qty')
    side = data.get('side')
    sl_pct = data.get('sl_pct')
    tgt_pct = data.get('tgt_pct')
    capital = data.get('capital')
    # TODO: Place real order via Delta API
    if session_id in delta_sessions:
        order_id = str(uuid.uuid4())
        delta_orders[order_id] = {
            'symbol': symbol, 'qty': qty, 'side': side, 'sl_pct': sl_pct, 'tgt_pct': tgt_pct, 'capital': capital,
            'status': 'placed', 'timestamp': datetime.utcnow().isoformat()
        }
        return jsonify({'success': True, 'orderId': order_id})
    return jsonify({'success': False, 'error': 'Invalid session'}), 403

@app.route('/api/realtrade/delta/status', methods=['GET'])
@login_required
def delta_status():
    session_id = request.args.get('sessionId')
    # TODO: Query real position/P&L from Delta API
    if session_id in delta_sessions:
        # Mock status
        return jsonify({'success': True, 'position': 'FLAT', 'pnl': 0, 'orders': list(delta_orders.values())})
    return jsonify({'success': False, 'error': 'Invalid session'}), 403

TICKER = "^NSEI"
IST_OFFSET = 19800  # UTC+5:30 in seconds

SYMBOL_MAP = {
    "NIFTY50":    {"ticker": "^NSEI",     "name": "NIFTY 50",    "exchange": "NSE"},
    "BANKNIFTY":  {"ticker": "^NSEBANK",  "name": "BANK NIFTY",  "exchange": "NSE"},
    "SENSEX":     {"ticker": "^BSESN",    "name": "SENSEX",      "exchange": "BSE"},
    "GOLD":       {"ticker": "GC=F",      "name": "Gold Futures", "exchange": "COMEX"},
    "SILVER":     {"ticker": "SI=F",      "name": "Silver Futures", "exchange": "COMEX"},
    "XAUUSD":     {"ticker": "XAUUSD=X",  "name": "XAU/USD",     "exchange": "FX"},
    "XAGUSD":     {"ticker": "XAGUSD=X",  "name": "XAG/USD",     "exchange": "FX"},
    "GOLDTEN":    {"ticker": "GOLDBEES.NS", "name": "Gold ETF",  "exchange": "NSE"},
    "SILVERBEES": {"ticker": "SILVERBEES.NS", "name": "Silver ETF", "exchange": "NSE"},
    "BTC":        {"ticker": "BTC-USD",        "name": "Bitcoin",    "exchange": "CRYPTO"},
    "ETH":        {"ticker": "ETH-USD",        "name": "Ethereum",   "exchange": "CRYPTO"},
    "DJI":        {"ticker": "^DJI",           "name": "Dow Jones",  "exchange": "NYSE"},
    "NASDAQ":     {"ticker": "^IXIC",          "name": "NASDAQ",     "exchange": "NASDAQ"},
    "SP500":      {"ticker": "^GSPC",          "name": "S&P 500",    "exchange": "NYSE"},
    "CRUDEOIL":  {"ticker": "CL=F",           "name": "Crude Oil",   "exchange": "NYMEX"},
    "NATURALGAS": {"ticker": "NG=F",           "name": "Natural Gas", "exchange": "NYMEX"},
}

INTERVAL_MAP = {
    "1m":  {"interval": "1m",  "period": "1d",  "label": "1 Min"},
    "2m":  {"interval": "2m",  "period": "1d",  "label": "2 Min"},
    "3m":  {"interval": "5m",  "period": "5d",  "label": "3 Min"},
    "5m":  {"interval": "5m",  "period": "5d",  "label": "5 Min"},
    "10m": {"interval": "15m", "period": "10d", "label": "10 Min"},
    "15m": {"interval": "15m", "period": "10d", "label": "15 Min"},
    "30m": {"interval": "30m", "period": "10d", "label": "30 Min"},
    "1h":  {"interval": "1h",  "period": "30d", "label": "1 Hour"},
    "2h":  {"interval": "1h",  "period": "60d", "label": "2 Hour"},
    "4h":  {"interval": "1h",  "period": "60d", "label": "4 Hour"},
    "1d":  {"interval": "1d",  "period": "1y",  "label": "1 Day"},
    "1w":  {"interval": "1wk", "period": "5y",  "label": "1 Week"},
    "1mo": {"interval": "1mo", "period": "max", "label": "1 Month"},
}

TV_SYMBOL_MAP = {
    "NIFTY50":    "NSE:NIFTY",
    "BANKNIFTY":  "NSE:BANKNIFTY",
    "SENSEX":     "BSE:SENSEX",
    "GOLD":       "COMEX:GC1!",
    "SILVER":     "COMEX:SI1!",
    "XAUUSD":     "FX_IDC:XAUUSD",
    "XAGUSD":     "FX_IDC:XAGUSD",
    "GOLDTEN":    "NSE:GOLDBEES",
    "SILVERBEES": "NSE:SILVERBEES",
    "BTC":        "BITSTAMP:BTCUSD",
    "ETH":        "BITSTAMP:ETHUSD",
    "DJI":        "DJ:DJI",
    "NASDAQ":     "NASDAQ:IXIC",
    "SP500":      "SP:SPX",
    "CRUDEOIL":  "NYMEX:CL1!",
    "NATURALGAS": "NYMEX:NG1!",
}

TV_INTERVAL_MAP = {
    "1m": "1", "2m": "2", "3m": "3", "5m": "5", "10m": "10", "15m": "15", "30m": "30",
    "1h": "60", "2h": "120", "4h": "240", "1d": "D", "1w": "W", "1mo": "M",
}

NSE_INDEX_MAP = {
    "NIFTY50":   {"index": "NIFTY 50",   "indices": True},
    "BANKNIFTY": {"index": "NIFTY BANK", "indices": True},
}


def fetch_nifty_data(interval_key, symbol_key="NIFTY50"):
    """Fetch OHLCV candlestick data from Yahoo Finance using the yfinance library.

    Resolves the symbol_key against SYMBOL_MAP for preset instruments, or uses
    the raw ticker string for user-searched symbols (e.g. 'RELIANCE.NS').
    Downloads historical data with period/interval from INTERVAL_MAP, converts
    each row's UTC timestamp to IST by adding IST_OFFSET, and returns a list
    of candle dicts with time, open, high, low, close, volume fields.

    Args:
        interval_key (str): Timeframe key ('3m', '5m', '15m', '1h', '1d').
        symbol_key (str): SYMBOL_MAP key (e.g. 'NIFTY50') or raw Yahoo ticker.

    Returns:
        list[dict]: List of OHLCV candle dicts with IST timestamps, or empty
            list if no data is available.
    """
    config = INTERVAL_MAP.get(interval_key, INTERVAL_MAP["5m"])
    sym = SYMBOL_MAP.get(symbol_key)
    if sym:
        yticker = sym["ticker"]
    else:
        yticker = symbol_key  # raw Yahoo Finance ticker
    ticker = yf.Ticker(yticker)
    df = ticker.history(period=config["period"], interval=config["interval"])

    if df.empty:
        return []

    candles = []
    for idx, row in df.iterrows():
        ts = int(idx.timestamp()) + IST_OFFSET
        candles.append({
            "time": ts,
            "open": round(row["Open"], 2),
            "high": round(row["High"], 2),
            "low": round(row["Low"], 2),
            "close": round(row["Close"], 2),
            "volume": int(row["Volume"]) if row["Volume"] == row["Volume"] else 0,
        })

    # Aggregate 1h candles into 2h/4h if needed
    if interval_key in ("2h", "4h"):
        n = 2 if interval_key == "2h" else 4
        agg = []
        for i in range(0, len(candles), n):
            group = candles[i:i + n]
            agg.append({
                "time": group[0]["time"],
                "open": group[0]["open"],
                "high": max(c["high"] for c in group),
                "low": min(c["low"] for c in group),
                "close": group[-1]["close"],
                "volume": sum(c["volume"] for c in group),
            })
        candles = agg

    return candles


def fetch_tradingview_data(interval_key, symbol_key="NIFTY50"):
    """Fetch OHLCV candlestick data from TradingView via their WebSocket API.

    Connects to wss://data.tradingview.com/socket.io/websocket using the
    websocket-client library. Creates a chart session, resolves the symbol
    (mapped via TV_SYMBOL_MAP for presets, or auto-prefixed with NSE:/BSE:
    for .NS/.BO tickers), and requests up to 300 bars at the specified
    interval. Parses candle data from the binary WebSocket response using
    regex extraction of {"i":N,"v":[timestamp,O,H,L,C,V]} patterns.

    This is an unofficial API using an unauthorized user token. Data is
    near real-time with no delay. Supports all symbols available on
    TradingView including NSE, BSE, COMEX, and crypto exchanges.

    Args:
        interval_key (str): Timeframe key ('3m', '5m', '15m', '1h', '1d').
        symbol_key (str): SYMBOL_MAP/TV_SYMBOL_MAP key or raw ticker.

    Returns:
        list[dict]: List of up to 300 OHLCV candle dicts with IST timestamps,
            or empty list if the connection or data parsing fails.
    """
    cs = "cs_" + "".join(random.choice(string.ascii_lowercase) for _ in range(12))

    def _prepend(s):
        return "~m~" + str(len(s)) + "~m~" + s

    def _msg(func, params):
        return _prepend(json.dumps({"m": func, "p": params}, separators=(",", ":")))

    tv_symbol = TV_SYMBOL_MAP.get(symbol_key)
    if not tv_symbol:
        raw = symbol_key.upper()
        if raw.endswith(".NS"):
            tv_symbol = "NSE:" + raw[:-3]
        elif raw.endswith(".BO"):
            tv_symbol = "BSE:" + raw[:-3]
        else:
            tv_symbol = raw

    tv_interval = TV_INTERVAL_MAP.get(interval_key, "5")

    try:
        ws = websocket.WebSocket()
        ws.settimeout(15)
        ws.connect(
            "wss://data.tradingview.com/socket.io/websocket",
            header={"Origin": "https://data.tradingview.com"},
        )
        ws.send(_msg("set_auth_token", ["unauthorized_user_token"]))
        ws.send(_msg("chart_create_session", [cs, ""]))
        sym_str = json.dumps(
            {"symbol": tv_symbol, "adjustment": "splits"}, separators=(",", ":")
        )
        ws.send(_msg("resolve_symbol", [cs, "sds_sym_1", "=" + sym_str]))
        ws.send(_msg("create_series", [cs, "sds_1", "s1", "sds_sym_1", tv_interval, 300]))

        raw_data = ""
        for _ in range(200):
            try:
                result = ws.recv()
                raw_data += result
                if "series_completed" in result:
                    break
            except Exception:
                break
        ws.close()
    except Exception:
        return []

    matches = re.findall(r'"i":(\d+),"v":\[([^\]]+)\]', raw_data)
    if not matches:
        return []

    candles = []
    for _, vals_str in matches:
        vals = vals_str.split(",")
        if len(vals) < 6:
            continue
        ts = int(float(vals[0])) + IST_OFFSET
        candles.append({
            "time": ts,
            "open": round(float(vals[1]), 2),
            "high": round(float(vals[2]), 2),
            "low": round(float(vals[3]), 2),
            "close": round(float(vals[4]), 2),
            "volume": int(float(vals[5])),
        })

    return candles


def fetch_nse_data(interval_key, symbol_key="NIFTY50"):
    """Fetch intraday tick data from NSE India and aggregate into OHLC candles.

    Uses curl_cffi with Chrome TLS impersonation to bypass NSE's bot detection.
    Fetches the chart-databyindex API which returns [timestamp_ms, price] tick
    pairs for the current trading day. Aggregates these ticks into OHLC candles
    at the requested interval by bucketing timestamps into fixed windows.

    Only supports NSE indices defined in NSE_INDEX_MAP (NIFTY 50, NIFTY BANK).
    Volume data is not available from this endpoint (always 0). Returns empty
    data after market hours (post 3:30 PM IST) as the NSE API responds with
    empty grapthData.

    Args:
        interval_key (str): Timeframe key ('3m', '5m', '15m', '1h', '1d').
        symbol_key (str): NSE_INDEX_MAP key (e.g. 'NIFTY50', 'BANKNIFTY').

    Returns:
        list[dict]: List of OHLC candle dicts (volume=0) with IST timestamps,
            or empty list if symbol not supported or API returns no data.
    """
    nse_info = NSE_INDEX_MAP.get(symbol_key)
    if not nse_info:
        return []

    try:
        session = cffi_requests.Session(impersonate="chrome")
        session.get("https://www.nseindia.com", timeout=10)

        index_name = nse_info["index"]
        url = (
            "https://www.nseindia.com/api/chart-databyindex"
            f"?index={index_name}"
            f"&indices={'true' if nse_info['indices'] else 'false'}"
        )
        resp = session.get(url, timeout=10)
        data = resp.json()
        graph_data = data.get("grapthData", [])
        if not graph_data:
            return []

        interval_secs = {"3m": 180, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}.get(interval_key, 300)

        candle_map = {}
        for tick in graph_data:
            ts_ms, price = tick[0], tick[1]
            ts_ist = ts_ms // 1000 + IST_OFFSET
            window = (ts_ist // interval_secs) * interval_secs
            if window not in candle_map:
                candle_map[window] = {"open": price, "high": price, "low": price, "close": price}
            else:
                entry = candle_map[window]
                entry["high"] = max(entry["high"], price)
                entry["low"] = min(entry["low"], price)
                entry["close"] = price

        candles = []
        for ts in sorted(candle_map.keys()):
            c = candle_map[ts]
            candles.append({
                "time": ts,
                "open": round(c["open"], 2),
                "high": round(c["high"], 2),
                "low": round(c["low"], 2),
                "close": round(c["close"], 2),
                "volume": 0,
            })

        return candles
    except Exception:
        return []


def compute_atr(candles, period):
    """Compute Average True Range (ATR) for each candle in the series.

    ATR measures market volatility by calculating the True Range (the greatest
    of: current high-low, |high - prev close|, |low - prev close|) for each
    bar, then smoothing it with a running average. Uses the Wilder smoothing
    method: initial ATR is the simple average of the first `period` TRs,
    subsequent values use ATR[i] = (ATR[i-1] * (period-1) + TR[i]) / period.

    Args:
        candles (list[dict]): OHLCV candle dicts with 'high', 'low', 'close'.
        period (int): Lookback period for ATR smoothing (typically 14).

    Returns:
        list[float]: ATR value for each candle index (first values are
            progressively calculated; fully valid from index >= period).
    """
    atr = [0.0] * len(candles)
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        if i < period:
            atr[i] = atr[i - 1] + tr / period if i > 0 else tr
        elif i == period:
            # Initial ATR = average of first `period` TRs
            s = tr
            for j in range(1, period):
                h = candles[j]["high"]
                l = candles[j]["low"]
                pc = candles[j - 1]["close"]
                s += max(h - l, abs(h - pc), abs(l - pc))
            atr[i] = s / period
        else:
            atr[i] = (atr[i - 1] * (period - 1) + tr) / period
    return atr


def compute_supertrend(candles, period=10, multiplier=3.0):
    """Compute the SuperTrend trend-following indicator.

    SuperTrend uses ATR-based upper and lower bands around the HL2 (midpoint)
    of each candle. When price closes above the upper band, trend flips bullish;
    when price closes below the lower band, trend flips bearish. Bands are
    clamped to prevent widening against the trend direction.

    The indicator line follows the lower band during uptrends (support) and
    the upper band during downtrends (resistance), making it useful for
    identifying trend direction and potential reversal points.

    Args:
        candles (list[dict]): OHLCV candle dicts.
        period (int): ATR lookback period (default 10).
        multiplier (float): ATR multiplier for band width (default 3.0).

    Returns:
        list[dict]: Dicts with 'time', 'value' (SuperTrend price level),
            and 'direction' (1=bullish, -1=bearish). Starts from index=period.
    """
    n = len(candles)
    if n < period + 1:
        return []

    atr = compute_atr(candles, period)
    st = [{"time": c["time"], "value": None, "direction": 1} for c in candles]

    upper_band = [0.0] * n
    lower_band = [0.0] * n
    supertrend = [0.0] * n
    direction = [1] * n  # 1 = up (bullish), -1 = down (bearish)

    for i in range(period, n):
        hl2 = (candles[i]["high"] + candles[i]["low"]) / 2
        upper_band[i] = hl2 + multiplier * atr[i]
        lower_band[i] = hl2 - multiplier * atr[i]

        # Clamp bands
        if i > period:
            if lower_band[i] > lower_band[i - 1] or candles[i - 1]["close"] < lower_band[i - 1]:
                pass
            else:
                lower_band[i] = lower_band[i - 1]

            if upper_band[i] < upper_band[i - 1] or candles[i - 1]["close"] > upper_band[i - 1]:
                pass
            else:
                upper_band[i] = upper_band[i - 1]

        # Direction
        if i == period:
            direction[i] = 1 if candles[i]["close"] > upper_band[i] else -1
        else:
            prev_st = supertrend[i - 1]
            if direction[i - 1] == 1:
                direction[i] = -1 if candles[i]["close"] < lower_band[i] else 1
            else:
                direction[i] = 1 if candles[i]["close"] > upper_band[i] else -1

        supertrend[i] = lower_band[i] if direction[i] == 1 else upper_band[i]

    result = []
    for i in range(period, n):
        result.append({
            "time": candles[i]["time"],
            "value": round(supertrend[i], 2),
            "direction": direction[i],
        })
    return result


def compute_parabolic_sar(candles, af_start=0.02, af_increment=0.02, af_max=0.2):
    """Compute the Parabolic Stop and Reverse (SAR) indicator.

    Parabolic SAR places dots above or below price to indicate trend direction
    and potential reversal points. The SAR value accelerates toward price using
    an Acceleration Factor (AF) that increases each time a new extreme point
    (EP) is made in the trend direction, up to a maximum AF value.

    During uptrends, SAR dots appear below candles (support). During downtrends,
    SAR dots appear above candles (resistance). A trend reversal occurs when
    price crosses the SAR value.

    Args:
        candles (list[dict]): OHLCV candle dicts.
        af_start (float): Initial acceleration factor (default 0.02).
        af_increment (float): AF step increase per new EP (default 0.02).
        af_max (float): Maximum acceleration factor cap (default 0.2).

    Returns:
        list[dict]: Dicts with 'time', 'value' (SAR price), and 'direction'
            (1=bullish/below price, -1=bearish/above price).
    """
    n = len(candles)
    if n < 2:
        return []

    sar = [0.0] * n
    ep = [0.0] * n   # extreme point
    af = [af_start] * n
    trend = [1] * n  # 1 = up, -1 = down

    # Initialize
    trend[0] = 1 if candles[1]["close"] >= candles[0]["close"] else -1
    if trend[0] == 1:
        sar[0] = candles[0]["low"]
        ep[0] = candles[0]["high"]
    else:
        sar[0] = candles[0]["high"]
        ep[0] = candles[0]["low"]

    for i in range(1, n):
        # Calculate SAR for current bar
        sar[i] = sar[i - 1] + af[i - 1] * (ep[i - 1] - sar[i - 1])

        # Ensure SAR is within prior bars
        if trend[i - 1] == 1:
            sar[i] = min(sar[i], candles[i - 1]["low"])
            if i >= 2:
                sar[i] = min(sar[i], candles[i - 2]["low"])
        else:
            sar[i] = max(sar[i], candles[i - 1]["high"])
            if i >= 2:
                sar[i] = max(sar[i], candles[i - 2]["high"])

        # Check for reversal
        reverse = False
        if trend[i - 1] == 1 and candles[i]["low"] < sar[i]:
            reverse = True
            trend[i] = -1
            sar[i] = ep[i - 1]
            ep[i] = candles[i]["low"]
            af[i] = af_start
        elif trend[i - 1] == -1 and candles[i]["high"] > sar[i]:
            reverse = True
            trend[i] = 1
            sar[i] = ep[i - 1]
            ep[i] = candles[i]["high"]
            af[i] = af_start
        else:
            trend[i] = trend[i - 1]
            af[i] = af[i - 1]
            ep[i] = ep[i - 1]

            if trend[i] == 1:
                if candles[i]["high"] > ep[i]:
                    ep[i] = candles[i]["high"]
                    af[i] = min(af[i] + af_increment, af_max)
            else:
                if candles[i]["low"] < ep[i]:
                    ep[i] = candles[i]["low"]
                    af[i] = min(af[i] + af_increment, af_max)

    result = []
    for i in range(1, n):
        result.append({
            "time": candles[i]["time"],
            "value": round(sar[i], 2),
            "bullish": trend[i] == 1,
        })
    return result


def compute_support_resistance(candles, num_levels=5):
    """Compute key support and resistance price levels using pivot-point clustering.

    Identifies swing highs and swing lows (local extrema with a 2-bar lookback/
    lookahead) across the candle series, then clusters nearby pivot prices that
    fall within 0.3% of each other. Clusters are ranked by strength (number of
    touches), with stronger levels representing more significant S/R zones.

    Pivots above the current price are classified as resistance; those below as
    support. Each level includes a strength count indicating confluence. The
    result is used to draw horizontal price lines on the chart.

    Args:
        candles (list[dict]): OHLCV candle dicts.
        num_levels (int): Maximum number of support/resistance levels to
            return (default 5 each).

    Returns:
        dict: Keys 'support' and 'resistance' (lists of {price, strength}
            sorted by proximity to current price), plus 'timeStart'/'timeEnd'
            for the price line time range.
    """
    if len(candles) < 5:
        return {"support": [], "resistance": []}

    # Find swing highs and swing lows (local extrema with lookback=2)
    pivots = []
    for i in range(2, len(candles) - 2):
        h = candles[i]["high"]
        l = candles[i]["low"]

        is_swing_high = (h >= candles[i-1]["high"] and h >= candles[i-2]["high"]
                         and h >= candles[i+1]["high"] and h >= candles[i+2]["high"])
        is_swing_low = (l <= candles[i-1]["low"] and l <= candles[i-2]["low"]
                        and l <= candles[i+1]["low"] and l <= candles[i+2]["low"])

        if is_swing_high:
            pivots.append({"price": h, "type": "high", "idx": i})
        if is_swing_low:
            pivots.append({"price": l, "type": "low", "idx": i})

    if not pivots:
        return {"support": [], "resistance": []}

    # Cluster nearby pivots (within 0.3% of each other)
    prices = sorted([p["price"] for p in pivots])
    clusters = []
    used = set()

    for i, p in enumerate(prices):
        if i in used:
            continue
        cluster = [p]
        used.add(i)
        for j in range(i + 1, len(prices)):
            if j in used:
                continue
            if abs(prices[j] - p) / p < 0.003:
                cluster.append(prices[j])
                used.add(j)
        clusters.append({"price": round(sum(cluster) / len(cluster), 2), "strength": len(cluster)})

    # Sort by strength (most touches first)
    clusters.sort(key=lambda x: -x["strength"])

    current_price = candles[-1]["close"]
    support = [c for c in clusters if c["price"] < current_price]
    resistance = [c for c in clusters if c["price"] >= current_price]

    support.sort(key=lambda x: -x["price"])  # closest first
    resistance.sort(key=lambda x: x["price"])  # closest first

    return {
        "support": support[:num_levels],
        "resistance": resistance[:num_levels],
        "timeStart": candles[0]["time"],
        "timeEnd": candles[-1]["time"],
    }


# ==================== ADDITIONAL INDICATORS ====================

def compute_ema(values, period):
    """Compute Exponential Moving Average (EMA) on a raw list of float values.

    Uses the standard EMA formula with smoothing constant k = 2/(period+1).
    The initial EMA value (at index period-1) is seeded with the Simple Moving
    Average of the first `period` values. Values before the seed index are 0.0.

    This is a low-level utility used internally by compute_rsi, compute_macd,
    and compute_ema_series. For time-series output, use compute_ema_series.

    Args:
        values (list[float]): Raw numeric values (e.g. closing prices).
        period (int): EMA lookback period.

    Returns:
        list[float]: EMA values aligned by index (0.0 for indices < period-1).
    """
    ema = [0.0] * len(values)
    if len(values) < period:
        return ema
    k = 2 / (period + 1)
    ema[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        ema[i] = values[i] * k + ema[i - 1] * (1 - k)
    return ema


def compute_sma(values, period):
    """Compute Simple Moving Average (SMA) on a raw list of float values.

    Calculates the arithmetic mean of the last `period` values at each index.
    Values before index period-1 are 0.0. Used internally by other indicator
    computations.

    Args:
        values (list[float]): Raw numeric values.
        period (int): SMA lookback window size.

    Returns:
        list[float]: SMA values aligned by index (0.0 for indices < period-1).
    """
    sma = [0.0] * len(values)
    for i in range(period - 1, len(values)):
        sma[i] = sum(values[i - period + 1:i + 1]) / period
    return sma


def compute_rsi(candles, period=14):
    """Compute the Relative Strength Index (RSI) momentum oscillator.

    RSI measures the speed and magnitude of price movements on a 0-100 scale.
    Uses Wilder's smoothing method: initial average gain/loss is a simple
    average, subsequent values use exponential smoothing with factor
    (period-1)/period. RSI > 70 indicates overbought; RSI < 30 indicates
    oversold. Also returns price momentum (change direction) for signal scoring.

    If insufficient data, returns RSI = 50 (neutral) for all candles.

    Args:
        candles (list[dict]): OHLCV candle dicts.
        period (int): RSI lookback period (default 14).

    Returns:
        list[dict]: Dicts with 'time', 'value' (RSI 0-100), and 'momentum'
            (1=rising, -1=falling, 0=flat).
    """
    n = len(candles)
    if n < period + 1:
        return [{"time": c["time"], "value": 50.0} for c in candles]

    closes = [c["close"] for c in candles]
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        delta = closes[i] - closes[i - 1]
        gains[i] = delta if delta > 0 else 0.0
        losses[i] = -delta if delta < 0 else 0.0

    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period

    rsi = [50.0] * n
    for i in range(period, n):
        if i == period:
            ag, al = avg_gain, avg_loss
        else:
            ag = (avg_gain * (period - 1) + gains[i]) / period
            al = (avg_loss * (period - 1) + losses[i]) / period
        avg_gain, avg_loss = ag, al
        if al == 0:
            rsi[i] = 100.0
        else:
            rs = ag / al
            rsi[i] = round(100 - 100 / (1 + rs), 2)

    return [{"time": candles[i]["time"], "value": rsi[i]} for i in range(n)]


def compute_macd(candles, fast=12, slow=26, signal_period=9):
    """Compute MACD (Moving Average Convergence Divergence) indicator.

    Calculates three components:
    - MACD Line: difference between fast EMA and slow EMA of closing prices.
    - Signal Line: EMA of the MACD line (used for crossover signals).
    - Histogram: MACD minus Signal (positive = bullish momentum, negative = bearish).

    MACD crossovers above/below the signal line generate buy/sell signals.
    The histogram's magnitude and direction indicate momentum strength.
    Output starts from index (slow-1 + signal_period-1) where all EMAs are valid.

    Args:
        candles (list[dict]): OHLCV candle dicts.
        fast (int): Fast EMA period (default 12).
        slow (int): Slow EMA period (default 26).
        signal_period (int): Signal line EMA period (default 9).

    Returns:
        list[dict]: Dicts with 'time', 'macd', 'signal', 'histogram' values.
    """
    closes = [c["close"] for c in candles]
    ema_fast = compute_ema(closes, fast)
    ema_slow = compute_ema(closes, slow)

    macd_line = [0.0] * len(closes)
    for i in range(slow - 1, len(closes)):
        macd_line[i] = ema_fast[i] - ema_slow[i]

    signal_values = macd_line[slow - 1:]
    sig = compute_ema(signal_values, signal_period)
    signal_line = [0.0] * (slow - 1) + sig

    histogram = [0.0] * len(closes)
    start = slow - 1 + signal_period - 1
    for i in range(start, len(closes)):
        histogram[i] = macd_line[i] - signal_line[i]

    result = []
    for i in range(start, len(closes)):
        result.append({
            "time": candles[i]["time"],
            "macd": round(macd_line[i], 2),
            "signal": round(signal_line[i], 2),
            "histogram": round(histogram[i], 2),
        })
    return result


def compute_vwap(candles):
    """Compute Volume Weighted Average Price (VWAP) with daily session reset.

    VWAP is the ratio of cumulative (typical price * volume) to cumulative
    volume, where typical price = (high + low + close) / 3. Resets the
    running totals at the start of each new trading day to provide a
    meaningful intraday benchmark. For zero-volume candles, a volume of 1
    is used to avoid division by zero.

    VWAP acts as an institutional benchmark — price above VWAP suggests
    bullish bias; below suggests bearish bias.

    Args:
        candles (list[dict]): OHLCV candle dicts with 'time' as IST timestamp.

    Returns:
        list[dict]: Dicts with 'time' and 'value' (VWAP price level).
    """
    n = len(candles)
    vwap = [0.0] * n
    cum_vol = 0.0
    cum_tp_vol = 0.0
    prev_date = None

    for i in range(n):
        tp = (candles[i]["high"] + candles[i]["low"] + candles[i]["close"]) / 3
        vol = candles[i]["volume"] if candles[i]["volume"] > 0 else 1

        # Reset at new trading day
        cur_date = datetime.fromtimestamp(candles[i]["time"], tz=None).date()
        if prev_date and cur_date != prev_date:
            cum_vol = 0.0
            cum_tp_vol = 0.0
        prev_date = cur_date

        cum_vol += vol
        cum_tp_vol += tp * vol
        vwap[i] = round(cum_tp_vol / cum_vol, 2) if cum_vol > 0 else tp

    return [{"time": candles[i]["time"], "value": vwap[i]} for i in range(n)]


def compute_ema_series(candles, period):
    """Compute EMA on closing prices and return as time-series dicts for charting.

    Wraps the low-level compute_ema() function, pairing each EMA value with
    its candle timestamp. Output begins at index (period-1) where the EMA
    becomes valid. Used for EMA 9 and EMA 21 overlay lines on the chart.

    Args:
        candles (list[dict]): OHLCV candle dicts.
        period (int): EMA lookback period.

    Returns:
        list[dict]: Dicts with 'time' and 'value' (rounded EMA price).
    """
    closes = [c["close"] for c in candles]
    ema = compute_ema(closes, period)
    return [{"time": candles[i]["time"], "value": round(ema[i], 2)}
            for i in range(period - 1, len(candles))]


def detect_candlestick_patterns(candles):
    """Detect key Japanese candlestick patterns for signal scoring.

    Scans the candle series (requiring at least 3 candles of context) and
    identifies the following reversal/continuation patterns:
    - Bullish Engulfing: bearish candle followed by larger bullish candle.
    - Bearish Engulfing: bullish candle followed by larger bearish candle.
    - Hammer: small body at top with long lower shadow (bullish reversal).
    - Shooting Star: small body at bottom with long upper shadow (bearish).
    - Morning Star: 3-candle bullish reversal (bear, small body, bull).
    - Evening Star: 3-candle bearish reversal (bull, small body, bear).
    - Doji: open equals close within 10% of range (indecision).

    Each pattern is scored +1 (bullish) or -1 (bearish) and used as an
    input to the composite signal engine with weight 1.0.

    Args:
        candles (list[dict]): OHLCV candle dicts.

    Returns:
        list[dict]: Dicts with 'time', 'pattern' (name), and 'score' (+1/-1).
    """
    patterns = []
    n = len(candles)
    for i in range(2, n):
        c = candles[i]
        p = candles[i - 1]
        pp = candles[i - 2]
        body = abs(c["close"] - c["open"])
        full_range = c["high"] - c["low"]
        if full_range == 0:
            continue

        prev_body = abs(p["close"] - p["open"])
        is_bull = c["close"] > c["open"]
        is_bear = c["close"] < c["open"]
        prev_bull = p["close"] > p["open"]
        prev_bear = p["close"] < p["open"]

        # Bullish Engulfing
        if prev_bear and is_bull and c["open"] <= p["close"] and c["close"] >= p["open"]:
            patterns.append({"time": c["time"], "type": "bullish_engulfing", "signal": 1})

        # Bearish Engulfing
        if prev_bull and is_bear and c["open"] >= p["close"] and c["close"] <= p["open"]:
            patterns.append({"time": c["time"], "type": "bearish_engulfing", "signal": -1})

        # Hammer (bullish reversal) — small body at top, long lower shadow
        lower_shadow = min(c["open"], c["close"]) - c["low"]
        upper_shadow = c["high"] - max(c["open"], c["close"])
        if lower_shadow > 2 * body and upper_shadow < body * 0.5 and body > 0:
            patterns.append({"time": c["time"], "type": "hammer", "signal": 1})

        # Shooting Star (bearish reversal)
        if upper_shadow > 2 * body and lower_shadow < body * 0.5 and body > 0:
            patterns.append({"time": c["time"], "type": "shooting_star", "signal": -1})

        # Morning Star (3-bar bullish reversal)
        if i >= 2:
            pp_bear = pp["close"] < pp["open"]
            pp_body = abs(pp["close"] - pp["open"])
            if pp_bear and prev_body < pp_body * 0.3 and is_bull and c["close"] > (pp["open"] + pp["close"]) / 2:
                patterns.append({"time": c["time"], "type": "morning_star", "signal": 1})

        # Evening Star (3-bar bearish reversal)
        if i >= 2:
            pp_bull = pp["close"] > pp["open"]
            pp_body = abs(pp["close"] - pp["open"])
            if pp_bull and prev_body < pp_body * 0.3 and is_bear and c["close"] < (pp["open"] + pp["close"]) / 2:
                patterns.append({"time": c["time"], "type": "evening_star", "signal": -1})

        # Doji (indecision)
        if body < full_range * 0.1:
            patterns.append({"time": c["time"], "type": "doji", "signal": 0})

    return patterns


def compute_cpr(candles):
    """Compute the Central Pivot Range (CPR) from the previous trading day's data.

    CPR consists of three levels derived from the prior day's High, Low, Close:
    - Pivot = (High + Low + Close) / 3
    - Bottom Central (BC) = (High + Low) / 2
    - Top Central (TC) = 2 * Pivot - BC

    CPR helps identify intraday support/resistance zones. A narrow CPR
    (TC close to BC) suggests a trending day; a wide CPR suggests
    range-bound trading. Groups candles by date to extract the previous
    day's H/L/C values.

    Args:
        candles (list[dict]): OHLCV candle dicts spanning at least 2 days.

    Returns:
        dict: Keys 'pivot', 'tc' (top central), 'bc' (bottom central),
            each a rounded float price level, or None if insufficient data.
    """
    if len(candles) < 2:
        return {"pivot": None, "tc": None, "bc": None}

    # Group candles by date to find previous day's H/L/C
    from collections import defaultdict
    daily = defaultdict(lambda: {"high": -float('inf'), "low": float('inf'), "close": 0})

    for c in candles:
        date = datetime.fromtimestamp(c["time"]).strftime("%Y-%m-%d")
        daily[date]["high"] = max(daily[date]["high"], c["high"])
        daily[date]["low"] = min(daily[date]["low"], c["low"])
        daily[date]["close"] = c["close"]

    dates = sorted(daily.keys())
    if len(dates) < 2:
        d = daily[dates[0]]
        prev_high, prev_low, prev_close = d["high"], d["low"], d["close"]
    else:
        prev_day = daily[dates[-2]]
        prev_high = prev_day["high"]
        prev_low = prev_day["low"]
        prev_close = prev_day["close"]

    pivot = round((prev_high + prev_low + prev_close) / 3, 2)
    bc = round((prev_high + prev_low) / 2, 2)
    tc = round(2 * pivot - bc, 2)

    return {"pivot": pivot, "tc": tc, "bc": bc}


def compute_bollinger_bands(candles, period=20, std_dev=2.0):
    """Compute Bollinger Bands — a volatility envelope around a moving average.

    Bollinger Bands consist of three lines:
    - Middle Band: Simple Moving Average (SMA) of closing prices.
    - Upper Band: SMA + (std_dev * population standard deviation).
    - Lower Band: SMA - (std_dev * population standard deviation).

    Bands expand during high volatility and contract during low volatility.
    Price touching the upper band suggests overbought; lower band suggests
    oversold. Band squeezes (narrow width) often precede breakout moves.

    Args:
        candles (list[dict]): OHLCV candle dicts.
        period (int): SMA/std dev lookback period (default 20).
        std_dev (float): Number of standard deviations for band width (default 2.0).

    Returns:
        list[dict]: Dicts with 'time', 'middle', 'upper', 'lower' price levels.
            Starts from index (period-1).
    """
    n = len(candles)
    if n < period:
        return []

    closes = [c["close"] for c in candles]
    result = []

    for i in range(period - 1, n):
        window = closes[i - period + 1:i + 1]
        sma = sum(window) / period
        variance = sum((x - sma) ** 2 for x in window) / period
        std = variance ** 0.5

        result.append({
            "time": candles[i]["time"],
            "middle": round(sma, 2),
            "upper": round(sma + std_dev * std, 2),
            "lower": round(sma - std_dev * std, 2),
        })

    return result


def compute_liquidity_pools(candles, lookback=10):
    """Detect liquidity pools (Smart Money Concept) — clusters of equal highs/lows.

    Liquidity pools form where multiple candles create equal highs (Buy-Side
    Liquidity / BSL) or equal lows (Sell-Side Liquidity / SSL) within a
    tolerance of 0.2%. These levels attract institutional stop hunts because
    retail traders place stop losses near obvious equal highs/lows.

    Scans each candle against the previous `lookback` candles to find price
    matches. Equal highs are labeled BSL (resistance); equal lows are labeled
    SSL (support). Drawn as dashed horizontal yellow lines on the chart.

    Args:
        candles (list[dict]): OHLCV candle dicts.
        lookback (int): Number of prior candles to check for price matches
            (default 10).

    Returns:
        list[dict]: Dicts with 'time', 'price', and 'type' ('BSL'/'SSL').
    """
    n = len(candles)
    if n < lookback + 2:
        return []

    pools = []
    threshold_pct = 0.002  # 0.2% tolerance for 'equal' highs/lows

    for i in range(lookback, n):
        high_i = candles[i]["high"]
        low_i = candles[i]["low"]

        # Check for equal highs (buy-side liquidity above)
        eq_high_count = 0
        for j in range(i - lookback, i):
            if abs(candles[j]["high"] - high_i) / high_i < threshold_pct:
                eq_high_count += 1
        if eq_high_count >= 2:
            pools.append({
                "time": candles[i]["time"],
                "price": round(high_i, 2),
                "type": "buyside",
                "strength": eq_high_count,
            })

        # Check for equal lows (sell-side liquidity below)
        eq_low_count = 0
        for j in range(i - lookback, i):
            if abs(candles[j]["low"] - low_i) / low_i < threshold_pct:
                eq_low_count += 1
        if eq_low_count >= 2:
            pools.append({
                "time": candles[i]["time"],
                "price": round(low_i, 2),
                "type": "sellside",
                "strength": eq_low_count,
            })

    return pools


def compute_fair_value_gaps(candles):
    """Detect Fair Value Gaps (FVG) — 3-candle price imbalance zones (SMC concept).

    An FVG occurs when there is a price gap between the first and third candles
    of a 3-candle sequence that the middle candle's range does not fill:
    - Bullish FVG: candle[i-2].high < candle[i].low — a gap up indicating
      unfilled buying pressure. Price tends to revisit this zone as support.
    - Bearish FVG: candle[i-2].low > candle[i].high — a gap down indicating
      unfilled selling pressure. Price tends to revisit this zone as resistance.

    FVGs represent areas where institutional orders may be waiting to fill.
    Drawn as paired horizontal lines (teal for bullish, red for bearish).

    Args:
        candles (list[dict]): OHLCV candle dicts.

    Returns:
        list[dict]: Dicts with 'time', 'timeEnd', 'high', 'low', and
            'type' ('bullish'/'bearish').
    """
    n = len(candles)
    if n < 3:
        return []

    fvgs = []
    for i in range(2, n):
        c0 = candles[i - 2]  # first candle
        c2 = candles[i]       # third candle

        # Bullish FVG: gap between c0 high and c2 low
        if c2["low"] > c0["high"]:
            fvgs.append({
                "time": candles[i - 1]["time"],  # middle candle time
                "timeEnd": c2["time"],
                "high": round(c2["low"], 2),
                "low": round(c0["high"], 2),
                "type": "bullish",
            })

        # Bearish FVG: gap between c0 low and c2 high
        if c2["high"] < c0["low"]:
            fvgs.append({
                "time": candles[i - 1]["time"],
                "timeEnd": c2["time"],
                "high": round(c0["low"], 2),
                "low": round(c2["high"], 2),
                "type": "bearish",
            })

    return fvgs


def _find_swing_points(candles, left=3, right=3):
    """Find swing high and swing low pivot points with configurable lookback.

    A swing high is a candle whose high is greater than or equal to the highs
    of all candles within `left` bars before and `right` bars after it.
    Similarly, a swing low has a low less than or equal to surrounding lows.

    This is a helper function used by compute_bos_choch to identify structural
    pivot points for Break of Structure and Change of Character detection.

    Args:
        candles (list[dict]): OHLCV candle dicts.
        left (int): Number of bars to the left to confirm pivot (default 3).
        right (int): Number of bars to the right to confirm pivot (default 3).

    Returns:
        list[dict]: Sorted list of swing point dicts with 'idx', 'time',
            'price', and 'type' ('high'/'low').
    """
    n = len(candles)
    swings = []
    for i in range(left, n - right):
        # Swing high
        is_sh = True
        for j in range(1, left + 1):
            if candles[i - j]["high"] > candles[i]["high"]:
                is_sh = False
                break
        if is_sh:
            for j in range(1, right + 1):
                if candles[i + j]["high"] > candles[i]["high"]:
                    is_sh = False
                    break
        if is_sh:
            swings.append({"idx": i, "time": candles[i]["time"],
                           "price": candles[i]["high"], "type": "high"})

        # Swing low
        is_sl = True
        for j in range(1, left + 1):
            if candles[i - j]["low"] < candles[i]["low"]:
                is_sl = False
                break
        if is_sl:
            for j in range(1, right + 1):
                if candles[i + j]["low"] < candles[i]["low"]:
                    is_sl = False
                    break
        if is_sl:
            swings.append({"idx": i, "time": candles[i]["time"],
                           "price": candles[i]["low"], "type": "low"})

    swings.sort(key=lambda s: s["idx"])
    return swings


def compute_bos_choch(candles, swing_lookback=3):
    """Detect Break of Structure (BOS) and Change of Character (CHoCH) — SMC concepts.

    Tracks the market's structural swing highs and lows, then identifies:
    - BOS (Break of Structure): Price breaks a previous swing high in a bullish
      trend or a previous swing low in a bearish trend. Confirms trend
      continuation. Shown as arrow markers on the chart.
    - CHoCH (Change of Character): Price breaks a swing point AGAINST the
      prevailing trend direction, signaling a potential trend reversal. Shown
      as circle markers on the chart.

    Uses _find_swing_points() to identify pivots, then iterates through them
    to track trend state and detect structural breaks at each swing point.

    Args:
        candles (list[dict]): OHLCV candle dicts.
        swing_lookback (int): Left/right bars for swing detection (default 3).

    Returns:
        dict: Keys 'bos' and 'choch', each a list of dicts with 'time',
            'price' (broken level), 'direction' ('bullish'/'bearish'),
            and 'swingPrice' (the structural level that was broken).
    """
    n = len(candles)
    if n < swing_lookback * 2 + 5:
        return {"bos": [], "choch": []}

    swings = _find_swing_points(candles, swing_lookback, swing_lookback)
    if len(swings) < 2:
        return {"bos": [], "choch": []}

    bos_list = []
    choch_list = []

    # Determine trend from first two swings
    trend = 1  # 1=bullish, -1=bearish
    last_sh = None  # last swing high
    last_sl = None  # last swing low

    for s in swings:
        if s["type"] == "high":
            last_sh = s
        else:
            last_sl = s
        if last_sh and last_sl:
            break

    for i in range(len(swings)):
        s = swings[i]

        if s["type"] == "high" and last_sh:
            if s["price"] > last_sh["price"]:
                # Higher high
                if trend == 1:
                    # BOS bullish — continuation
                    bos_list.append({
                        "time": s["time"], "price": round(s["price"], 2),
                        "type": "bullish", "broken": round(last_sh["price"], 2),
                    })
                else:
                    # CHoCH — bearish to bullish reversal
                    choch_list.append({
                        "time": s["time"], "price": round(s["price"], 2),
                        "type": "bullish", "broken": round(last_sh["price"], 2),
                    })
                    trend = 1
            last_sh = s

        elif s["type"] == "low" and last_sl:
            if s["price"] < last_sl["price"]:
                # Lower low
                if trend == -1:
                    # BOS bearish — continuation
                    bos_list.append({
                        "time": s["time"], "price": round(s["price"], 2),
                        "type": "bearish", "broken": round(last_sl["price"], 2),
                    })
                else:
                    # CHoCH — bullish to bearish reversal
                    choch_list.append({
                        "time": s["time"], "price": round(s["price"], 2),
                        "type": "bearish", "broken": round(last_sl["price"], 2),
                    })
                    trend = -1
            last_sl = s

    return {"bos": bos_list, "choch": choch_list}


def compute_cvd(candles):
    """Compute Cumulative Volume Delta (CVD) — buying vs selling pressure over time.

    Estimates the split between buying and selling volume for each candle using
    the close position ratio within the high-low range:
    - buy_ratio = (close - low) / (high - low)  (1.0 = all buying, 0.0 = all selling)
    - buy_volume = total_volume * buy_ratio
    - sell_volume = total_volume * (1 - buy_ratio)
    - delta = buy_volume - sell_volume

    The cumulative delta is the running total of per-bar deltas. Rising CVD with
    rising price confirms the uptrend; divergence warns of potential reversal.
    Shown as a histogram series below the main chart.

    Args:
        candles (list[dict]): OHLCV candle dicts.

    Returns:
        list[dict]: Dicts with 'time', 'delta' (per-bar), 'cumDelta' (running total).
    """
    n = len(candles)
    if n == 0:
        return []

    result = []
    cum_delta = 0.0

    for c in candles:
        hl_range = c["high"] - c["low"]
        vol = c["volume"]
        if hl_range > 0 and vol > 0:
            # Ratio: 1.0 = close at high (all buying), 0.0 = close at low (all selling)
            buy_ratio = (c["close"] - c["low"]) / hl_range
            buy_vol = vol * buy_ratio
            sell_vol = vol * (1 - buy_ratio)
            delta = buy_vol - sell_vol
        else:
            delta = 0.0

        cum_delta += delta
        result.append({
            "time": c["time"],
            "delta": round(delta, 0),
            "cumDelta": round(cum_delta, 0),
        })

    return result


def compute_volume_profile(candles, num_bins=24):
    """Compute Volume Profile — volume distributed across price levels.

    Divides the price range into equal bins and aggregates volume at each level.
    Identifies the Point of Control (POC) — the price level with highest volume,
    and the Value Area (70% of total volume around POC).

    Args:
        candles (list[dict]): OHLCV candle dicts.
        num_bins (int): Number of price bins (default 24).

    Returns:
        list[dict]: Dicts with 'price', 'volume', 'pct' (% of max), 'isPOC', 'isVA'.
    """
    n = len(candles)
    if n < 2:
        return []

    all_high = max(c["high"] for c in candles)
    all_low = min(c["low"] for c in candles)
    price_range = all_high - all_low
    if price_range <= 0:
        return []

    bin_size = price_range / num_bins
    bins = [0.0] * num_bins

    for c in candles:
        vol = c.get("volume", 0) or 0
        if vol <= 0:
            continue
        hl = c["high"] - c["low"]
        if hl <= 0:
            idx = int((c["close"] - all_low) / bin_size)
            idx = min(idx, num_bins - 1)
            bins[idx] += vol
        else:
            lo_bin = int((c["low"] - all_low) / bin_size)
            hi_bin = int((c["high"] - all_low) / bin_size)
            lo_bin = max(0, min(lo_bin, num_bins - 1))
            hi_bin = max(0, min(hi_bin, num_bins - 1))
            spread = hi_bin - lo_bin + 1
            per_bin = vol / spread
            for b in range(lo_bin, hi_bin + 1):
                bins[b] += per_bin

    max_vol = max(bins) if bins else 1
    if max_vol <= 0:
        return []

    poc_idx = bins.index(max_vol)
    total_vol = sum(bins)

    # Value Area: expand from POC until 70% of total volume
    va_set = {poc_idx}
    va_vol = bins[poc_idx]
    lo_ptr, hi_ptr = poc_idx - 1, poc_idx + 1
    while va_vol < total_vol * 0.7 and (lo_ptr >= 0 or hi_ptr < num_bins):
        lo_v = bins[lo_ptr] if lo_ptr >= 0 else 0
        hi_v = bins[hi_ptr] if hi_ptr < num_bins else 0
        if lo_v >= hi_v and lo_ptr >= 0:
            va_set.add(lo_ptr)
            va_vol += lo_v
            lo_ptr -= 1
        elif hi_ptr < num_bins:
            va_set.add(hi_ptr)
            va_vol += hi_v
            hi_ptr += 1
        else:
            break

    va_high_idx = max(va_set)
    va_low_idx = min(va_set)

    result = []
    for i in range(num_bins):
        price = round(all_low + (i + 0.5) * bin_size, 2)
        result.append({
            "price": price,
            "volume": round(bins[i], 0),
            "pct": round(bins[i] / max_vol * 100, 1),
            "isPOC": i == poc_idx,
            "isVA": i in va_set,
            "isVAH": i == va_high_idx,
            "isVAL": i == va_low_idx,
        })
    return result


def generate_signals(candles, supertrend, psar, rsi_data, macd_data, vwap_data,
                     ema9, ema21, patterns, sr):
    """Institutional-grade composite signal engine using weighted multi-indicator scoring.

    Combines 9 technical indicators into a single weighted score per candle to
    generate actionable BUY/SELL signals. Each indicator contributes a directional
    score scaled by its assigned weight:

    Indicator breakdown (total possible = ~10):
      SuperTrend direction:     weight 1.5  (trend state: bullish +1, bearish -1)
      PSAR direction:           weight 1.0  (trend state: bullish +1, bearish -1)
      RSI zone + momentum:     weight 1.5  (overbought/oversold zones + direction)
      MACD crossover + hist:   weight 2.0  (signal line cross + histogram direction)
      EMA 9/21 crossover:      weight 1.5  (fast above slow = bullish)
      VWAP position:           weight 1.0  (price above VWAP = bullish)
      Volume confirmation:     weight 0.5  (above-average volume confirms move)
      Candlestick patterns:    weight 1.0  (engulfing, hammer, star patterns)
      S/R proximity boost:     weight 0.5  (contextual: near support = bullish boost)

    Signal thresholds:
      score >= 3.5 → BUY,  score >= 5.5 → STRONG BUY
      score <= -3.5 → SELL, score <= -5.5 → STRONG SELL

    Also generates a summary with the latest signal verdict, composite score,
    individual indicator statuses, and signal counts for the panel UI.

    Args:
        candles (list[dict]): OHLCV candle dicts.
        supertrend (list[dict]): SuperTrend output (time, value, direction).
        psar (list[dict]): Parabolic SAR output (time, value, direction).
        rsi_data (list[dict]): RSI output (time, value, momentum).
        macd_data (list[dict]): MACD output (time, macd, signal, histogram).
        vwap_data (list[dict]): VWAP output (time, value).
        ema9 (list[dict]): EMA 9 series (time, value).
        ema21 (list[dict]): EMA 21 series (time, value).
        patterns (list[dict]): Candlestick patterns (time, pattern, score).
        sr (dict): Support/resistance levels (support, resistance lists).

    Returns:
        tuple: (signals, summary) where signals is a list of dicts with
            'time', 'signal', 'score', 'price'; and summary is a dict with
            'verdict', 'score', 'indicators', 'buyCount', 'sellCount'.
    """
    n = len(candles)
    if n < 30:
        return [], {}

    # Build lookup maps (time → value)
    st_map = {s["time"]: s for s in supertrend}
    psar_map = {p["time"]: p for p in psar}
    rsi_map = {r["time"]: r["value"] for r in rsi_data}
    macd_map = {m["time"]: m for m in macd_data}
    vwap_map = {v["time"]: v["value"] for v in vwap_data}
    ema9_map = {e["time"]: e["value"] for e in ema9}
    ema21_map = {e["time"]: e["value"] for e in ema21}
    pat_map = {}
    for p in patterns:
        pat_map.setdefault(p["time"], []).append(p)

    # Average volume for volume confirmation
    volumes = [c["volume"] for c in candles]
    avg_vol_20 = compute_sma(volumes, 20)

    # Support/resistance levels for proximity check
    sup_levels = [s["price"] for s in sr.get("support", [])]
    res_levels = [r["price"] for r in sr.get("resistance", [])]

    signals = []
    prev_score = 0

    for i in range(1, n):
        t = candles[i]["time"]
        t_prev = candles[i - 1]["time"]
        close = candles[i]["close"]
        score = 0.0
        reasons = []

        # --- 1. SuperTrend (weight 1.5) ---
        st = st_map.get(t)
        st_prev = st_map.get(t_prev)
        if st:
            if st["direction"] == 1:
                score += 1.5
                reasons.append("ST Bullish")
            else:
                score -= 1.5
                reasons.append("ST Bearish")
            # Bonus for direction flip
            if st_prev and st["direction"] != st_prev["direction"]:
                score += 0.5 * st["direction"]
                reasons.append("ST Flip")

        # --- 2. Parabolic SAR (weight 1.0) ---
        ps = psar_map.get(t)
        ps_prev = psar_map.get(t_prev)
        if ps:
            if ps["bullish"]:
                score += 1.0
                reasons.append("PSAR Bull")
            else:
                score -= 1.0
                reasons.append("PSAR Bear")
            if ps_prev and ps["bullish"] != ps_prev["bullish"]:
                score += 0.5 if ps["bullish"] else -0.5
                reasons.append("PSAR Flip")

        # --- 3. RSI (weight 1.5) ---
        rsi_val = rsi_map.get(t, 50)
        rsi_prev = rsi_map.get(t_prev, 50)
        if rsi_val < 30:
            score += 1.5
            reasons.append(f"RSI Oversold({rsi_val})")
        elif rsi_val < 40:
            score += 0.5
            reasons.append(f"RSI Low({rsi_val})")
        elif rsi_val > 70:
            score -= 1.5
            reasons.append(f"RSI Overbought({rsi_val})")
        elif rsi_val > 60:
            score -= 0.5
            reasons.append(f"RSI High({rsi_val})")
        # RSI momentum (crossing 50)
        if rsi_prev <= 50 < rsi_val:
            score += 0.5
            reasons.append("RSI Cross 50↑")
        elif rsi_prev >= 50 > rsi_val:
            score -= 0.5
            reasons.append("RSI Cross 50↓")

        # --- 4. MACD (weight 2.0) ---
        mc = macd_map.get(t)
        mc_prev = macd_map.get(t_prev)
        if mc and mc_prev:
            # Signal line crossover
            if mc_prev["macd"] <= mc_prev["signal"] and mc["macd"] > mc["signal"]:
                score += 2.0
                reasons.append("MACD Bull Cross")
            elif mc_prev["macd"] >= mc_prev["signal"] and mc["macd"] < mc["signal"]:
                score -= 2.0
                reasons.append("MACD Bear Cross")
            # Histogram direction
            if mc["histogram"] > 0 and mc["histogram"] > mc_prev["histogram"]:
                score += 0.5
                reasons.append("MACD Hist↑")
            elif mc["histogram"] < 0 and mc["histogram"] < mc_prev["histogram"]:
                score -= 0.5
                reasons.append("MACD Hist↓")

        # --- 5. EMA 9/21 Crossover (weight 1.5) ---
        e9 = ema9_map.get(t)
        e21 = ema21_map.get(t)
        e9p = ema9_map.get(t_prev)
        e21p = ema21_map.get(t_prev)
        if e9 and e21 and e9p and e21p:
            if e9p <= e21p and e9 > e21:
                score += 1.5
                reasons.append("EMA 9/21 Bull Cross")
            elif e9p >= e21p and e9 < e21:
                score -= 1.5
                reasons.append("EMA 9/21 Bear Cross")
            elif e9 > e21:
                score += 0.3
            else:
                score -= 0.3

        # --- 6. VWAP (weight 1.0) ---
        vw = vwap_map.get(t)
        if vw:
            if close > vw * 1.001:
                score += 1.0
                reasons.append("Above VWAP")
            elif close < vw * 0.999:
                score -= 1.0
                reasons.append("Below VWAP")

        # --- 7. Volume confirmation (weight 0.5) ---
        if i < len(avg_vol_20) and avg_vol_20[i] > 0:
            vol_ratio = candles[i]["volume"] / avg_vol_20[i]
            if vol_ratio > 1.5:
                # Amplify in direction of move
                vol_dir = 0.5 if candles[i]["close"] > candles[i]["open"] else -0.5
                score += vol_dir
                reasons.append(f"High Vol({vol_ratio:.1f}x)")

        # --- 8. Candlestick patterns (weight 1.0) ---
        pats = pat_map.get(t, [])
        for pat in pats:
            if pat["signal"] == 1:
                score += 1.0
                reasons.append(pat["type"].replace("_", " ").title())
            elif pat["signal"] == -1:
                score -= 1.0
                reasons.append(pat["type"].replace("_", " ").title())

        # --- 9. S/R proximity boost (weight 0.5) ---
        for sl in sup_levels:
            if 0 < (close - sl) / close < 0.005:  # within 0.5% of support
                score += 0.5
                reasons.append(f"Near Support {sl}")
                break
        for rl in res_levels:
            if 0 < (rl - close) / close < 0.005:
                score -= 0.5
                reasons.append(f"Near Resistance {rl}")
                break

        # --- Generate signal if threshold met ---
        score = round(score, 2)
        if score >= 5.0:
            signals.append({"time": t, "type": "STRONG_BUY", "score": score, "reasons": reasons,
                            "price": candles[i]["low"]})
        elif score >= 3.5:
            signals.append({"time": t, "type": "BUY", "score": score, "reasons": reasons,
                            "price": candles[i]["low"]})
        elif score <= -5.0:
            signals.append({"time": t, "type": "STRONG_SELL", "score": score, "reasons": reasons,
                            "price": candles[i]["high"]})
        elif score <= -3.5:
            signals.append({"time": t, "type": "SELL", "score": score, "reasons": reasons,
                            "price": candles[i]["high"]})

        prev_score = score

    # Current analysis summary (latest bar)
    last_i = n - 1
    t_last = candles[last_i]["time"]
    summary_score = 0
    summary_reasons = []

    st_last = st_map.get(t_last)
    if st_last:
        d = 1.5 if st_last["direction"] == 1 else -1.5
        summary_score += d
        summary_reasons.append(("SuperTrend", "Bullish" if d > 0 else "Bearish", d))

    ps_last = psar_map.get(t_last)
    if ps_last:
        d = 1.0 if ps_last["bullish"] else -1.0
        summary_score += d
        summary_reasons.append(("PSAR", "Bullish" if d > 0 else "Bearish", d))

    rsi_last = rsi_map.get(t_last, 50)
    rsi_dir = 1.5 if rsi_last < 30 else (-1.5 if rsi_last > 70 else (0.5 if rsi_last < 40 else (-0.5 if rsi_last > 60 else 0)))
    summary_score += rsi_dir
    summary_reasons.append(("RSI", f"{rsi_last:.1f}", rsi_dir))

    mc_last = macd_map.get(t_last)
    if mc_last:
        d = 1.0 if mc_last["histogram"] > 0 else -1.0
        summary_score += d
        summary_reasons.append(("MACD", "Bullish" if d > 0 else "Bearish", d))

    e9_last = ema9_map.get(t_last)
    e21_last = ema21_map.get(t_last)
    if e9_last and e21_last:
        d = 1.0 if e9_last > e21_last else -1.0
        summary_score += d
        summary_reasons.append(("EMA 9/21", "Bull" if d > 0 else "Bear", d))

    vw_last = vwap_map.get(t_last)
    if vw_last:
        d = 1.0 if candles[last_i]["close"] > vw_last else -1.0
        summary_score += d
        summary_reasons.append(("VWAP", "Above" if d > 0 else "Below", d))

    if summary_score >= 5:
        verdict = "STRONG BUY"
    elif summary_score >= 3.5:
        verdict = "BUY"
    elif summary_score <= -5:
        verdict = "STRONG SELL"
    elif summary_score <= -3.5:
        verdict = "SELL"
    else:
        verdict = "NEUTRAL"

    summary = {
        "score": round(summary_score, 2),
        "verdict": verdict,
        "indicators": [{"name": r[0], "status": r[1], "weight": r[2]} for r in summary_reasons],
        "rsi": rsi_last,
        "macd": mc_last,
        "vwap": vwap_map.get(t_last),
    }

    return signals, summary


def generate_janestreet_signals(candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr):
    """Janestreet-style quantitative signal engine using statistical mean-reversion
    and momentum breakout strategies.

    Uses a weighted composite of 7 quant-focused indicators:
      Z-Score mean reversion:     weight 2.0  (20-bar z-score of close price)
      Bollinger Band squeeze:     weight 1.5  (price at/beyond bands = reversion signal)
      RSI divergence:             weight 1.5  (extreme RSI + price divergence)
      Volume-weighted momentum:   weight 1.5  (VWAP deviation + price acceleration)
      MACD histogram momentum:    weight 1.5  (histogram acceleration / deceleration)
      EMA spread z-score:         weight 1.0  (normalized EMA9-EMA21 spread)
      S/R mean reversion:         weight 0.5  (price near S/R = reversion opportunity)

    Signal thresholds: score >= 3.5 → BUY, >= 5.0 → STRONG BUY
                       score <= -3.5 → SELL, <= -5.0 → STRONG SELL

    Args:
        candles: OHLCV candle dicts.
        bb: Bollinger Bands data (upper, middle, lower lists).
        rsi_data: RSI output (time, value).
        macd_data: MACD output (time, macd, signal, histogram).
        vwap_data: VWAP output (time, value).
        ema9: EMA 9 series.
        ema21: EMA 21 series.
        sr: Support/resistance levels.

    Returns:
        tuple: (signals, summary)
    """
    n = len(candles)
    if n < 30:
        return [], {}

    # Build lookup maps
    rsi_map = {r["time"]: r["value"] for r in rsi_data}
    macd_map = {m["time"]: m for m in macd_data}
    vwap_map = {v["time"]: v["value"] for v in vwap_data}
    ema9_map = {e["time"]: e["value"] for e in ema9}
    ema21_map = {e["time"]: e["value"] for e in ema21}

    # BB lookup
    bb_upper_map, bb_lower_map, bb_mid_map = {}, {}, {}
    for b in bb:
        bb_upper_map[b["time"]] = b["upper"]
        bb_lower_map[b["time"]] = b["lower"]
        bb_mid_map[b["time"]] = b["middle"]

    sup_levels = [s["price"] for s in sr.get("support", [])]
    res_levels = [r["price"] for r in sr.get("resistance", [])]

    # Precompute 20-bar rolling mean and std for z-score
    closes = [c["close"] for c in candles]
    window = 20

    signals = []

    for i in range(window, n):
        t = candles[i]["time"]
        t_prev = candles[i - 1]["time"]
        close = candles[i]["close"]
        score = 0.0
        reasons = []

        # --- 1. Z-Score Mean Reversion (weight 2.0) ---
        segment = closes[i - window:i]
        mean = sum(segment) / window
        std = (sum((x - mean) ** 2 for x in segment) / window) ** 0.5
        if std > 0:
            zscore = (close - mean) / std
            if zscore < -2.0:
                score += 2.0
                reasons.append(f"Z-Score Oversold({zscore:.2f})")
            elif zscore < -1.0:
                score += 1.0
                reasons.append(f"Z-Score Low({zscore:.2f})")
            elif zscore > 2.0:
                score -= 2.0
                reasons.append(f"Z-Score Overbought({zscore:.2f})")
            elif zscore > 1.0:
                score -= 1.0
                reasons.append(f"Z-Score High({zscore:.2f})")

        # --- 2. Bollinger Band Squeeze (weight 1.5) ---
        bb_u = bb_upper_map.get(t)
        bb_l = bb_lower_map.get(t)
        bb_m = bb_mid_map.get(t)
        if bb_u and bb_l and bb_m:
            bb_width = (bb_u - bb_l) / bb_m if bb_m > 0 else 0
            if close <= bb_l:
                score += 1.5
                reasons.append(f"BB Lower Touch(w={bb_width:.3f})")
            elif close >= bb_u:
                score -= 1.5
                reasons.append(f"BB Upper Touch(w={bb_width:.3f})")
            # Squeeze detection (narrow bands = breakout imminent)
            if bb_width < 0.02:
                # Direction based on close vs mid
                if close > bb_m:
                    score += 0.5
                    reasons.append("BB Squeeze Bullish")
                else:
                    score -= 0.5
                    reasons.append("BB Squeeze Bearish")

        # --- 3. RSI Divergence (weight 1.5) ---
        rsi_val = rsi_map.get(t, 50)
        rsi_prev = rsi_map.get(t_prev, 50)
        if rsi_val < 25:
            score += 1.5
            reasons.append(f"RSI Extreme Oversold({rsi_val:.0f})")
        elif rsi_val < 35 and close < closes[i - 1]:
            # Bullish divergence: price falls but RSI not making new low
            if rsi_val > rsi_prev:
                score += 1.0
                reasons.append(f"RSI Bull Divergence({rsi_val:.0f})")
        elif rsi_val > 75:
            score -= 1.5
            reasons.append(f"RSI Extreme Overbought({rsi_val:.0f})")
        elif rsi_val > 65 and close > closes[i - 1]:
            if rsi_val < rsi_prev:
                score -= 1.0
                reasons.append(f"RSI Bear Divergence({rsi_val:.0f})")

        # --- 4. Volume-Weighted Momentum (weight 1.5) ---
        vw = vwap_map.get(t)
        if vw and vw > 0:
            vwap_dev = (close - vw) / vw
            if vwap_dev < -0.005:
                score += 1.5
                reasons.append(f"Below VWAP({vwap_dev:.3f})")
            elif vwap_dev > 0.005:
                score -= 0.5  # Momentum, not reversion
                reasons.append(f"Above VWAP({vwap_dev:.3f})")
            # Price acceleration
            if i >= 2:
                accel = (closes[i] - closes[i-1]) - (closes[i-1] - closes[i-2])
                if accel > 0 and vwap_dev < 0:
                    score += 0.5
                    reasons.append("Price Accelerating Up")
                elif accel < 0 and vwap_dev > 0:
                    score -= 0.5
                    reasons.append("Price Decelerating")

        # --- 5. MACD Histogram Momentum (weight 1.5) ---
        mc = macd_map.get(t)
        mc_prev = macd_map.get(t_prev)
        if mc and mc_prev:
            hist_delta = mc["histogram"] - mc_prev["histogram"]
            if mc["histogram"] < 0 and hist_delta > 0:
                score += 1.5
                reasons.append("MACD Hist Reversing Up")
            elif mc["histogram"] > 0 and hist_delta < 0:
                score -= 1.5
                reasons.append("MACD Hist Reversing Down")
            elif mc["histogram"] > 0 and hist_delta > 0:
                score += 0.5
                reasons.append("MACD Hist Expanding Up")
            elif mc["histogram"] < 0 and hist_delta < 0:
                score -= 0.5
                reasons.append("MACD Hist Expanding Down")

        # --- 6. EMA Spread Z-Score (weight 1.0) ---
        e9 = ema9_map.get(t)
        e21 = ema21_map.get(t)
        if e9 and e21 and e21 > 0:
            spread = (e9 - e21) / e21
            if spread < -0.003:
                score += 1.0
                reasons.append(f"EMA Spread Negative({spread:.4f})")
            elif spread > 0.003:
                score -= 0.3
                reasons.append(f"EMA Spread Positive({spread:.4f})")
            # Spread convergence (mean reversion)
            e9p = ema9_map.get(t_prev)
            e21p = ema21_map.get(t_prev)
            if e9p and e21p and e21p > 0:
                prev_spread = (e9p - e21p) / e21p
                if spread < 0 and spread > prev_spread:
                    score += 0.5
                    reasons.append("EMA Converging Up")
                elif spread > 0 and spread < prev_spread:
                    score -= 0.5
                    reasons.append("EMA Converging Down")

        # --- 7. S/R Mean Reversion (weight 0.5) ---
        for sl in sup_levels:
            if 0 < (close - sl) / close < 0.003:
                score += 0.5
                reasons.append(f"At Support {sl:.0f}")
                break
        for rl in res_levels:
            if 0 < (rl - close) / close < 0.003:
                score -= 0.5
                reasons.append(f"At Resistance {rl:.0f}")
                break

        # --- Generate signal ---
        score = round(score, 2)
        if score >= 5.0:
            signals.append({"time": t, "type": "STRONG_BUY", "score": score,
                            "reasons": reasons, "price": candles[i]["low"]})
        elif score >= 3.5:
            signals.append({"time": t, "type": "BUY", "score": score,
                            "reasons": reasons, "price": candles[i]["low"]})
        elif score <= -5.0:
            signals.append({"time": t, "type": "STRONG_SELL", "score": score,
                            "reasons": reasons, "price": candles[i]["high"]})
        elif score <= -3.5:
            signals.append({"time": t, "type": "SELL", "score": score,
                            "reasons": reasons, "price": candles[i]["high"]})

    # Summary for latest bar
    last_i = n - 1
    t_last = candles[last_i]["time"]
    summary_score = 0
    summary_reasons = []

    # Z-score
    if n >= window:
        seg = closes[n - window:n]
        m = sum(seg) / window
        s = (sum((x - m) ** 2 for x in seg) / window) ** 0.5
        zs = (closes[-1] - m) / s if s > 0 else 0
        d = 1.0 if zs < -1 else (-1.0 if zs > 1 else 0)
        summary_score += d
        summary_reasons.append(("Z-Score", f"{zs:.2f}", d))

    # BB
    bb_u = bb_upper_map.get(t_last)
    bb_l = bb_lower_map.get(t_last)
    if bb_u and bb_l:
        if closes[-1] <= bb_l:
            d = 1.5
        elif closes[-1] >= bb_u:
            d = -1.5
        else:
            d = 0
        summary_score += d
        summary_reasons.append(("Bollinger", "Lower" if d > 0 else ("Upper" if d < 0 else "Mid"), d))

    rsi_last = rsi_map.get(t_last, 50)
    d = 1.5 if rsi_last < 25 else (-1.5 if rsi_last > 75 else 0)
    summary_score += d
    summary_reasons.append(("RSI", f"{rsi_last:.1f}", d))

    mc_last = macd_map.get(t_last)
    mc_prev2 = macd_map.get(candles[last_i - 1]["time"]) if last_i > 0 else None
    if mc_last and mc_prev2:
        hd = mc_last["histogram"] - mc_prev2["histogram"]
        d = 1.0 if (mc_last["histogram"] < 0 and hd > 0) else (-1.0 if (mc_last["histogram"] > 0 and hd < 0) else 0)
        summary_score += d
        summary_reasons.append(("MACD Hist", "Reversing Up" if d > 0 else ("Reversing Down" if d < 0 else "Flat"), d))

    vw_last = vwap_map.get(t_last)
    if vw_last:
        d = 1.0 if closes[-1] < vw_last * 0.995 else (-0.5 if closes[-1] > vw_last * 1.005 else 0)
        summary_score += d
        summary_reasons.append(("VWAP Dev", "Below" if d > 0 else ("Above" if d < 0 else "Neutral"), d))

    e9l = ema9_map.get(t_last)
    e21l = ema21_map.get(t_last)
    if e9l and e21l:
        d = 1.0 if e9l < e21l else -0.3
        summary_score += d
        summary_reasons.append(("EMA Spread", "Negative" if d > 0 else "Positive", d))

    if summary_score >= 5:
        verdict = "STRONG BUY"
    elif summary_score >= 3.5:
        verdict = "BUY"
    elif summary_score <= -5:
        verdict = "STRONG SELL"
    elif summary_score <= -3.5:
        verdict = "SELL"
    else:
        verdict = "NEUTRAL"

    summary = {
        "score": round(summary_score, 2),
        "verdict": verdict,
        "indicators": [{"name": r[0], "status": r[1], "weight": r[2]} for r in summary_reasons],
        "rsi": rsi_last,
        "macd": mc_last,
        "vwap": vw_last,
    }

    return signals, summary


def generate_accurate_signals(candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr):
    """Accurate strategy: ultra-precise alternating buy/sell signals using an
    ensemble of 12+ weighted indicators and mathematical models.

    Combines:
      1. Z-Score mean reversion       (weight 2.0)
      2. Bollinger Band position       (weight 1.5)
      3. RSI with Stochastic RSI       (weight 2.0)
      4. MACD histogram + crossover    (weight 2.0)
      5. VWAP deviation                (weight 1.5)
      6. EMA 9/21 spread & crossover   (weight 1.5)
      7. ATR volatility regime         (weight 1.0)
      8. S/R proximity                 (weight 1.0)
      9. Candle body ratio analysis    (weight 1.0)
     10. Price momentum (ROC)          (weight 1.5)
     11. Heikin-Ashi trend filter      (weight 1.0)
     12. Volume pressure (OBV delta)   (weight 1.0)

    Enforces strict alternating BUY→SELL→BUY pattern so every signal is
    actionable as a complete entry/exit pair.

    Returns:
        tuple: (signals, summary)
    """
    n = len(candles)
    if n < 30:
        return [], {}

    # Build lookup maps
    rsi_map = {r["time"]: r["value"] for r in rsi_data}
    macd_map = {m["time"]: m for m in macd_data}
    vwap_map = {v["time"]: v["value"] for v in vwap_data}
    ema9_map = {e["time"]: e["value"] for e in ema9}
    ema21_map = {e["time"]: e["value"] for e in ema21}

    bb_upper_map, bb_lower_map, bb_mid_map = {}, {}, {}
    for b in bb:
        bb_upper_map[b["time"]] = b["upper"]
        bb_lower_map[b["time"]] = b["lower"]
        bb_mid_map[b["time"]] = b["middle"]

    sup_levels = [s["price"] for s in sr.get("support", [])]
    res_levels = [r["price"] for r in sr.get("resistance", [])]

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    opens = [c["open"] for c in candles]
    volumes = [c.get("volume", 0) for c in candles]

    window = 20

    # Precompute ATR (14-period)
    atr_period = 14
    atr_vals = [0.0] * n
    for i in range(1, n):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        if i < atr_period:
            atr_vals[i] = tr
        else:
            atr_vals[i] = (atr_vals[i - 1] * (atr_period - 1) + tr) / atr_period

    # Precompute Heikin-Ashi
    ha_close = [0.0] * n
    ha_open = [0.0] * n
    ha_close[0] = (opens[0] + highs[0] + lows[0] + closes[0]) / 4
    ha_open[0] = (opens[0] + closes[0]) / 2
    for i in range(1, n):
        ha_close[i] = (opens[i] + highs[i] + lows[i] + closes[i]) / 4
        ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2

    # Precompute OBV
    obv = [0.0] * n
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            obv[i] = obv[i - 1] + volumes[i]
        elif closes[i] < closes[i - 1]:
            obv[i] = obv[i - 1] - volumes[i]
        else:
            obv[i] = obv[i - 1]

    # Precompute Stochastic RSI (14-period)
    rsi_list = [rsi_map.get(candles[i]["time"], 50) for i in range(n)]
    stoch_rsi = [50.0] * n
    stoch_period = 14
    for i in range(stoch_period, n):
        rsi_window = rsi_list[i - stoch_period:i + 1]
        rsi_min = min(rsi_window)
        rsi_max = max(rsi_window)
        if rsi_max - rsi_min > 0:
            stoch_rsi[i] = (rsi_list[i] - rsi_min) / (rsi_max - rsi_min) * 100
        else:
            stoch_rsi[i] = 50.0

    raw_signals = []  # (index, net_score, reasons, buy_score, sell_score)

    for i in range(window, n):
        t = candles[i]["time"]
        t_prev = candles[i - 1]["time"]
        close = closes[i]
        buy_score = 0.0
        sell_score = 0.0
        buy_count = 0   # number of indicators voting buy
        sell_count = 0   # number of indicators voting sell
        reasons = []

        # --- 1. Z-Score Mean Reversion (weight 2.0) ---
        segment = closes[i - window:i]
        mean = sum(segment) / window
        std = (sum((x - mean) ** 2 for x in segment) / window) ** 0.5
        zscore = (close - mean) / std if std > 0 else 0
        if zscore < -1.5:
            buy_score += 2.0; buy_count += 1
            reasons.append(f"Z-Score Oversold({zscore:.2f})")
        elif zscore < -0.8:
            buy_score += 1.0; buy_count += 1
            reasons.append(f"Z-Score Low({zscore:.2f})")
        elif zscore > 1.5:
            sell_score += 2.0; sell_count += 1
            reasons.append(f"Z-Score Overbought({zscore:.2f})")
        elif zscore > 0.8:
            sell_score += 1.0; sell_count += 1
            reasons.append(f"Z-Score High({zscore:.2f})")

        # --- 2. Bollinger Band Position (weight 1.5) ---
        bb_u = bb_upper_map.get(t)
        bb_l = bb_lower_map.get(t)
        bb_m = bb_mid_map.get(t)
        if bb_u and bb_l and bb_m and bb_m > 0:
            bb_pct = (close - bb_l) / (bb_u - bb_l) if (bb_u - bb_l) > 0 else 0.5
            if bb_pct <= 0.1:
                buy_score += 1.5; buy_count += 1
                reasons.append(f"BB%B Extreme Low({bb_pct:.2f})")
            elif bb_pct <= 0.25:
                buy_score += 0.8; buy_count += 1
                reasons.append(f"BB%B Low({bb_pct:.2f})")
            elif bb_pct >= 0.9:
                sell_score += 1.5; sell_count += 1
                reasons.append(f"BB%B Extreme High({bb_pct:.2f})")
            elif bb_pct >= 0.75:
                sell_score += 0.8; sell_count += 1
                reasons.append(f"BB%B High({bb_pct:.2f})")

        # --- 3. RSI + Stochastic RSI (weight 2.0) ---
        rsi_val = rsi_map.get(t, 50)
        srsi = stoch_rsi[i]
        if rsi_val < 30 and srsi < 20:
            buy_score += 2.0; buy_count += 1
            reasons.append(f"RSI+StochRSI Oversold({rsi_val:.0f},{srsi:.0f})")
        elif rsi_val < 40 and srsi < 30:
            buy_score += 1.0; buy_count += 1
            reasons.append(f"RSI+StochRSI Low({rsi_val:.0f},{srsi:.0f})")
        elif rsi_val > 70 and srsi > 80:
            sell_score += 2.0; sell_count += 1
            reasons.append(f"RSI+StochRSI Overbought({rsi_val:.0f},{srsi:.0f})")
        elif rsi_val > 60 and srsi > 70:
            sell_score += 1.0; sell_count += 1
            reasons.append(f"RSI+StochRSI High({rsi_val:.0f},{srsi:.0f})")

        # --- 4. MACD Histogram + Crossover (weight 2.0) ---
        mc = macd_map.get(t)
        mc_prev = macd_map.get(t_prev)
        if mc and mc_prev:
            hist = mc["histogram"]
            hist_prev = mc_prev["histogram"]
            hist_delta = hist - hist_prev
            # Bullish crossover
            if mc_prev["macd"] < mc_prev["signal"] and mc["macd"] >= mc["signal"]:
                buy_score += 2.0; buy_count += 1
                reasons.append("MACD Bullish Cross")
            elif mc_prev["macd"] > mc_prev["signal"] and mc["macd"] <= mc["signal"]:
                sell_score += 2.0; sell_count += 1
                reasons.append("MACD Bearish Cross")
            # Histogram acceleration
            if hist < 0 and hist_delta > 0:
                buy_score += 1.0; buy_count += 1
                reasons.append("MACD Hist Recovering")
            elif hist > 0 and hist_delta < 0:
                sell_score += 1.0; sell_count += 1
                reasons.append("MACD Hist Weakening")

        # --- 5. VWAP Deviation (weight 1.5) ---
        vw = vwap_map.get(t)
        if vw and vw > 0:
            vwap_dev = (close - vw) / vw
            if vwap_dev < -0.004:
                buy_score += 1.5; buy_count += 1
                reasons.append(f"Below VWAP({vwap_dev:.4f})")
            elif vwap_dev < -0.001:
                buy_score += 0.5
                reasons.append(f"Slightly Below VWAP({vwap_dev:.4f})")
            elif vwap_dev > 0.004:
                sell_score += 1.5; sell_count += 1
                reasons.append(f"Above VWAP({vwap_dev:.4f})")
            elif vwap_dev > 0.001:
                sell_score += 0.5
                reasons.append(f"Slightly Above VWAP({vwap_dev:.4f})")

        # --- 6. EMA 9/21 Spread & Crossover (weight 1.5) ---
        e9 = ema9_map.get(t)
        e21 = ema21_map.get(t)
        e9p = ema9_map.get(t_prev)
        e21p = ema21_map.get(t_prev)
        if e9 and e21 and e21 > 0:
            spread = (e9 - e21) / e21
            if e9p and e21p:
                # Bullish crossover
                if e9p <= e21p and e9 > e21:
                    buy_score += 1.5; buy_count += 1
                    reasons.append("EMA Bullish Cross")
                elif e9p >= e21p and e9 < e21:
                    sell_score += 1.5; sell_count += 1
                    reasons.append("EMA Bearish Cross")
                else:
                    if spread < -0.002:
                        buy_score += 0.5; buy_count += 1
                        reasons.append(f"EMA Spread Neg({spread:.4f})")
                    elif spread > 0.002:
                        sell_score += 0.5; sell_count += 1
                        reasons.append(f"EMA Spread Pos({spread:.4f})")

        # --- 7. ATR Volatility Regime (weight 1.0) ---
        atr = atr_vals[i]
        if atr > 0 and close > 0:
            atr_pct = atr / close
            # High volatility favors mean-reversion signals
            if atr_pct > 0.01:
                if zscore < -0.5:
                    buy_score += 1.0; buy_count += 1
                    reasons.append(f"High Vol Reversal Up(ATR%={atr_pct:.3f})")
                elif zscore > 0.5:
                    sell_score += 1.0; sell_count += 1
                    reasons.append(f"High Vol Reversal Dn(ATR%={atr_pct:.3f})")

        # --- 8. S/R Proximity (weight 1.0) ---
        for sl in sup_levels:
            if close > 0 and 0 < (close - sl) / close < 0.004:
                buy_score += 1.0; buy_count += 1
                reasons.append(f"Near Support {sl:.0f}")
                break
        for rl in res_levels:
            if close > 0 and 0 < (rl - close) / close < 0.004:
                sell_score += 1.0; sell_count += 1
                reasons.append(f"Near Resistance {rl:.0f}")
                break

        # --- 9. Candle Body Ratio Analysis (weight 1.0) ---
        body = abs(close - opens[i])
        wick_range = highs[i] - lows[i]
        if wick_range > 0:
            body_ratio = body / wick_range
            # Strong bullish candle (large body, close > open)
            if close > opens[i] and body_ratio > 0.65:
                buy_score += 1.0; buy_count += 1
                reasons.append(f"Strong Bullish Candle(r={body_ratio:.2f})")
            elif close < opens[i] and body_ratio > 0.65:
                sell_score += 1.0; sell_count += 1
                reasons.append(f"Strong Bearish Candle(r={body_ratio:.2f})")
            # Hammer/Shooting star
            lower_wick = min(close, opens[i]) - lows[i]
            upper_wick = highs[i] - max(close, opens[i])
            if lower_wick > body * 2 and upper_wick < body * 0.5:
                buy_score += 0.5; buy_count += 1
                reasons.append("Hammer Pattern")
            elif upper_wick > body * 2 and lower_wick < body * 0.5:
                sell_score += 0.5; sell_count += 1
                reasons.append("Shooting Star")

        # --- 10. Price Momentum ROC (weight 1.5) ---
        roc_period = min(10, i)
        if roc_period > 0 and closes[i - roc_period] > 0:
            roc = (close - closes[i - roc_period]) / closes[i - roc_period]
            if roc < -0.008:
                buy_score += 1.5; buy_count += 1
                reasons.append(f"ROC Reversal Up({roc:.4f})")
            elif roc < -0.003:
                buy_score += 0.5; buy_count += 1
                reasons.append(f"ROC Negative({roc:.4f})")
            elif roc > 0.008:
                sell_score += 1.5; sell_count += 1
                reasons.append(f"ROC Reversal Dn({roc:.4f})")
            elif roc > 0.003:
                sell_score += 0.5; sell_count += 1
                reasons.append(f"ROC Positive({roc:.4f})")

        # --- 11. Heikin-Ashi Trend Filter (weight 1.0) ---
        if ha_close[i] > ha_open[i]:
            # HA bullish
            if i >= 2 and ha_close[i - 1] <= ha_open[i - 1]:
                buy_score += 1.0; buy_count += 1
                reasons.append("HA Trend Reversal Bullish")
            else:
                buy_score += 0.3
        else:
            # HA bearish
            if i >= 2 and ha_close[i - 1] >= ha_open[i - 1]:
                sell_score += 1.0; sell_count += 1
                reasons.append("HA Trend Reversal Bearish")
            else:
                sell_score += 0.3

        # --- 12. Volume Pressure OBV Delta (weight 1.0) ---
        if i >= 3:
            obv_delta = obv[i] - obv[i - 3]
            if obv_delta > 0 and close > closes[i - 1]:
                buy_score += 1.0; buy_count += 1
                reasons.append("OBV Rising + Price Up")
            elif obv_delta < 0 and close < closes[i - 1]:
                sell_score += 1.0; sell_count += 1
                reasons.append("OBV Falling + Price Down")

        # Net score: positive = buy bias, negative = sell bias
        net_score = round(buy_score - sell_score, 2)
        raw_signals.append((i, net_score, reasons, buy_score, sell_score, buy_count, sell_count))

    # --- Score-based signal generation ---
    # Emit BUY/SELL signals when indicators agree on a direction.
    # Require: sufficient net score, minimum indicator consensus, and cooldown.
    signals = []
    last_signal_idx = -10    # index into raw_signals
    min_cooldown = 2         # minimum bars between signals

    # Fixed thresholds: BUY >= 3.5, STRONG_BUY >= 5.0, SELL <= -3.5, STRONG_SELL <= -5.0
    buy_threshold = 3.5
    strong_buy_threshold = 5.0
    sell_threshold = 3.5
    strong_sell_threshold = 5.0

    for sig_idx, (i, net_score, reasons, buy_sc, sell_sc, b_cnt, s_cnt) in enumerate(raw_signals):
        # Enforce cooldown
        if sig_idx - last_signal_idx < min_cooldown:
            continue

        t = candles[i]["time"]

        # BUY signal
        if net_score >= buy_threshold:
            sig_type = "STRONG_BUY" if net_score >= strong_buy_threshold else "BUY"
            signals.append({
                "time": t,
                "type": sig_type,
                "score": round(net_score, 2),
                "reasons": reasons,
                "price": candles[i]["low"],
            })
            last_signal_idx = sig_idx

        # SELL signal
        elif net_score <= -sell_threshold:
            sig_type = "STRONG_SELL" if net_score <= -strong_sell_threshold else "SELL"
            signals.append({
                "time": t,
                "type": sig_type,
                "score": round(abs(net_score), 2),
                "reasons": reasons,
                "price": candles[i]["high"],
            })
            last_signal_idx = sig_idx

    # --- Summary for latest bar ---
    last_i = n - 1
    t_last = candles[last_i]["time"]
    summary_indicators = []
    summary_score = 0.0

    # Z-score summary
    if n >= window:
        seg = closes[n - window:n]
        m = sum(seg) / window
        s = (sum((x - m) ** 2 for x in seg) / window) ** 0.5
        zs = (closes[-1] - m) / s if s > 0 else 0
        d = 1.5 if zs < -1 else (-1.5 if zs > 1 else 0)
        summary_score += d
        summary_indicators.append(("Z-Score", f"{zs:.2f}", d))

    # BB summary
    bb_u = bb_upper_map.get(t_last)
    bb_l = bb_lower_map.get(t_last)
    if bb_u and bb_l and (bb_u - bb_l) > 0:
        bb_pct = (closes[-1] - bb_l) / (bb_u - bb_l)
        d = 1.5 if bb_pct <= 0.2 else (-1.5 if bb_pct >= 0.8 else 0)
        summary_score += d
        summary_indicators.append(("Bollinger %B", f"{bb_pct:.2f}", d))

    # RSI + StochRSI summary
    rsi_last = rsi_map.get(t_last, 50)
    srsi_last = stoch_rsi[last_i] if last_i < n else 50
    d = 2.0 if (rsi_last < 30 and srsi_last < 20) else (-2.0 if (rsi_last > 70 and srsi_last > 80) else 0)
    summary_score += d
    summary_indicators.append(("RSI+StochRSI", f"{rsi_last:.1f}/{srsi_last:.0f}", d))

    # MACD summary
    mc_last = macd_map.get(t_last)
    mc_prev2 = macd_map.get(candles[last_i - 1]["time"]) if last_i > 0 else None
    if mc_last and mc_prev2:
        if mc_prev2["macd"] < mc_prev2["signal"] and mc_last["macd"] >= mc_last["signal"]:
            d = 2.0
            status = "Bullish Cross"
        elif mc_prev2["macd"] > mc_prev2["signal"] and mc_last["macd"] <= mc_last["signal"]:
            d = -2.0
            status = "Bearish Cross"
        else:
            hd = mc_last["histogram"] - mc_prev2["histogram"]
            d = 1.0 if (mc_last["histogram"] < 0 and hd > 0) else (-1.0 if (mc_last["histogram"] > 0 and hd < 0) else 0)
            status = "Hist+" if d > 0 else ("Hist-" if d < 0 else "Flat")
        summary_score += d
        summary_indicators.append(("MACD", status, d))

    # VWAP summary
    vw_last = vwap_map.get(t_last)
    if vw_last and vw_last > 0:
        dev = (closes[-1] - vw_last) / vw_last
        d = 1.5 if dev < -0.004 else (-1.5 if dev > 0.004 else 0)
        summary_score += d
        summary_indicators.append(("VWAP Dev", f"{dev:.4f}", d))

    # EMA summary
    e9l = ema9_map.get(t_last)
    e21l = ema21_map.get(t_last)
    if e9l and e21l:
        d = 1.0 if e9l > e21l else -1.0
        summary_score += d
        summary_indicators.append(("EMA 9/21", "Bullish" if d > 0 else "Bearish", d))

    # ATR summary
    atr_last = atr_vals[last_i]
    if closes[-1] > 0:
        atr_pct_last = atr_last / closes[-1]
        summary_indicators.append(("ATR%", f"{atr_pct_last:.3f}", 0))

    # HA summary
    ha_d = 0.5 if ha_close[last_i] > ha_open[last_i] else -0.5
    summary_score += ha_d
    summary_indicators.append(("Heikin-Ashi", "Bullish" if ha_d > 0 else "Bearish", ha_d))

    # ROC summary
    roc_p = min(10, last_i)
    if roc_p > 0 and closes[last_i - roc_p] > 0:
        roc_v = (closes[-1] - closes[last_i - roc_p]) / closes[last_i - roc_p]
        d = 1.0 if roc_v < -0.005 else (-1.0 if roc_v > 0.005 else 0)
        summary_score += d
        summary_indicators.append(("ROC", f"{roc_v:.4f}", d))

    summary_score = round(summary_score, 2)
    if summary_score >= 5:
        verdict = "STRONG BUY"
    elif summary_score >= 3.5:
        verdict = "BUY"
    elif summary_score <= -5:
        verdict = "STRONG SELL"
    elif summary_score <= -3.5:
        verdict = "SELL"
    else:
        verdict = "NEUTRAL"

    summary = {
        "score": summary_score,
        "verdict": verdict,
        "indicators": [{"name": r[0], "status": r[1], "weight": r[2]} for r in summary_indicators],
        "rsi": rsi_last,
        "macd": mc_last,
        "vwap": vw_last,
    }

    return signals, summary


# ---------------------------------------------------------------------------
# Strategy: Sniper Entry (Breakout Detection)
# ---------------------------------------------------------------------------
def generate_sniper_signals(candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr):
    """Sniper Entry Strategy — high-precision breakout detection engine.

    Identifies exact breakout moments by combining consolidation detection,
    volume explosion, and multi-indicator confirmation. Only fires when
    price breaks out of a tight range with strong momentum confirmation.

    Composite scoring (total ~15):
      1. Consolidation squeeze detection     (weight 2.0)
      2. Bollinger Band breakout             (weight 2.0)
      3. Volume explosion (>2x avg)          (weight 2.0)
      4. EMA 9/21 alignment + crossover      (weight 1.5)
      5. RSI momentum thrust (>60 or <40)    (weight 1.5)
      6. MACD histogram acceleration         (weight 1.5)
      7. VWAP breakout confirmation          (weight 1.5)
      8. S/R level breakout                  (weight 1.5)
      9. Candle body strength (>70% body)    (weight 1.0)

    Thresholds: BUY >= 5.0, STRONG BUY >= 7.0
                SELL <= -5.0, STRONG SELL <= -7.0

    Enforces strict alternating BUY→SELL→BUY for clean entry/exit pairs.
    """
    n = len(candles)
    if n < 30:
        return [], {}

    rsi_map = {r["time"]: r["value"] for r in rsi_data}
    macd_map = {m["time"]: m for m in macd_data}
    vwap_map = {v["time"]: v["value"] for v in vwap_data}
    ema9_map = {e["time"]: e["value"] for e in ema9}
    ema21_map = {e["time"]: e["value"] for e in ema21}

    bb_upper_map, bb_lower_map, bb_mid_map = {}, {}, {}
    for b in bb:
        bb_upper_map[b["time"]] = b["upper"]
        bb_lower_map[b["time"]] = b["lower"]
        bb_mid_map[b["time"]] = b["middle"]

    sup_levels = [s["price"] for s in sr.get("support", [])]
    res_levels = [r["price"] for r in sr.get("resistance", [])]

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    opens = [c["open"] for c in candles]
    volumes = [c.get("volume", 0) for c in candles]

    signals = []
    last_signal_type = None  # enforce alternating
    lookback = 20

    for i in range(lookback, n):
        t = candles[i]["time"]
        close = closes[i]
        high = highs[i]
        low = lows[i]
        opn = opens[i]
        vol = volumes[i]
        score = 0.0
        reasons = []

        # --- 1. Consolidation Squeeze Detection (weight 2.0) ---
        # Measure range contraction over lookback period
        recent_highs = highs[i - lookback:i]
        recent_lows = lows[i - lookback:i]
        range_width = max(recent_highs) - min(recent_lows)
        avg_candle_range = sum(highs[j] - lows[j] for j in range(i - lookback, i)) / lookback
        current_range = high - low

        # Squeeze: range is tightening (last 5 bars narrower than lookback avg)
        last5_range = sum(highs[j] - lows[j] for j in range(i - 5, i)) / 5
        squeeze_ratio = last5_range / avg_candle_range if avg_candle_range > 0 else 1
        is_squeeze = squeeze_ratio < 0.7

        # Breakout: current candle breaks the consolidation range
        consolidation_high = max(highs[i - 5:i])
        consolidation_low = min(lows[i - 5:i])
        breakout_up = close > consolidation_high and current_range > avg_candle_range * 1.2
        breakout_down = close < consolidation_low and current_range > avg_candle_range * 1.2

        if is_squeeze and breakout_up:
            score += 2.0
            reasons.append(f"Squeeze Breakout UP (ratio={squeeze_ratio:.2f})")
        elif is_squeeze and breakout_down:
            score -= 2.0
            reasons.append(f"Squeeze Breakout DOWN (ratio={squeeze_ratio:.2f})")
        elif breakout_up:
            score += 1.0
            reasons.append("Range Breakout UP")
        elif breakout_down:
            score -= 1.0
            reasons.append("Range Breakout DOWN")

        # --- 2. Bollinger Band Breakout (weight 2.0) ---
        bb_u = bb_upper_map.get(t)
        bb_l = bb_lower_map.get(t)
        bb_m = bb_mid_map.get(t)
        if bb_u and bb_l and bb_m:
            bb_width = bb_u - bb_l
            # Check previous BB width for squeeze detection
            t_prev5 = candles[i - 5]["time"]
            bb_u_p5 = bb_upper_map.get(t_prev5, bb_u)
            bb_l_p5 = bb_lower_map.get(t_prev5, bb_l)
            prev_bb_width = bb_u_p5 - bb_l_p5

            if close > bb_u:
                # Breakout above upper band
                if bb_width < prev_bb_width * 0.85:  # band was squeezing
                    score += 2.0
                    reasons.append("BB Squeeze Breakout UP")
                else:
                    score += 1.0
                    reasons.append("BB Upper Breakout")
            elif close < bb_l:
                if bb_width < prev_bb_width * 0.85:
                    score -= 2.0
                    reasons.append("BB Squeeze Breakout DOWN")
                else:
                    score -= 1.0
                    reasons.append("BB Lower Breakout")

        # --- 3. Volume Explosion (weight 2.0) ---
        vol_avg = sum(volumes[i - lookback:i]) / lookback if lookback > 0 else 1
        if vol_avg > 0 and vol > 0:
            vol_ratio = vol / vol_avg
            if vol_ratio >= 3.0:
                v_score = 2.0
            elif vol_ratio >= 2.0:
                v_score = 1.5
            elif vol_ratio >= 1.5:
                v_score = 0.8
            else:
                v_score = 0

            if v_score > 0:
                if close > opn:
                    score += v_score
                    reasons.append(f"Volume Explosion {vol_ratio:.1f}x (Bullish)")
                elif close < opn:
                    score -= v_score
                    reasons.append(f"Volume Explosion {vol_ratio:.1f}x (Bearish)")

        # --- 4. EMA 9/21 Alignment + Crossover (weight 1.5) ---
        e9 = ema9_map.get(t)
        e21 = ema21_map.get(t)
        t_prev = candles[i - 1]["time"]
        e9p = ema9_map.get(t_prev)
        e21p = ema21_map.get(t_prev)
        if e9 and e21 and e9p and e21p:
            # Fresh crossover is strongest signal
            if e9p <= e21p and e9 > e21:
                score += 1.5
                reasons.append("EMA 9/21 Bullish Cross")
            elif e9p >= e21p and e9 < e21:
                score -= 1.5
                reasons.append("EMA 9/21 Bearish Cross")
            elif e9 > e21 and close > e9:
                score += 0.5
                reasons.append("EMA Aligned Bullish")
            elif e9 < e21 and close < e9:
                score -= 0.5
                reasons.append("EMA Aligned Bearish")

        # --- 5. RSI Momentum Thrust (weight 1.5) ---
        rsi_val = rsi_map.get(t, 50)
        rsi_prev = rsi_map.get(t_prev, 50)
        if rsi_val > 65 and rsi_prev <= 65:
            score += 1.5
            reasons.append(f"RSI Thrust UP ({rsi_val:.0f})")
        elif rsi_val > 60 and rsi_val > rsi_prev:
            score += 0.8
            reasons.append(f"RSI Momentum UP ({rsi_val:.0f})")
        elif rsi_val < 35 and rsi_prev >= 35:
            score -= 1.5
            reasons.append(f"RSI Thrust DOWN ({rsi_val:.0f})")
        elif rsi_val < 40 and rsi_val < rsi_prev:
            score -= 0.8
            reasons.append(f"RSI Momentum DOWN ({rsi_val:.0f})")

        # --- 6. MACD Histogram Acceleration (weight 1.5) ---
        mc = macd_map.get(t)
        mc_prev = macd_map.get(t_prev)
        t_prev2 = candles[i - 2]["time"] if i >= 2 else t_prev
        mc_prev2 = macd_map.get(t_prev2)
        if mc and mc_prev and mc_prev2:
            hist = mc["histogram"]
            hist_p = mc_prev["histogram"]
            hist_p2 = mc_prev2["histogram"]
            accel = hist - hist_p
            prev_accel = hist_p - hist_p2

            # Histogram turning positive from negative with acceleration
            if hist > 0 and hist_p <= 0 and accel > 0:
                score += 1.5
                reasons.append("MACD Hist Flip Bullish")
            elif hist < 0 and hist_p >= 0 and accel < 0:
                score -= 1.5
                reasons.append("MACD Hist Flip Bearish")
            elif hist > 0 and accel > prev_accel and accel > 0:
                score += 0.5
                reasons.append("MACD Accelerating UP")
            elif hist < 0 and accel < prev_accel and accel < 0:
                score -= 0.5
                reasons.append("MACD Accelerating DOWN")

        # --- 7. VWAP Breakout Confirmation (weight 1.5) ---
        vw = vwap_map.get(t)
        vw_prev = vwap_map.get(t_prev)
        if vw and vw_prev:
            # Price crossing above VWAP
            if closes[i - 1] <= vw_prev and close > vw:
                score += 1.5
                reasons.append("VWAP Breakout UP")
            elif closes[i - 1] >= vw_prev and close < vw:
                score -= 1.5
                reasons.append("VWAP Breakdown")
            elif close > vw * 1.003:
                score += 0.3
                reasons.append("Above VWAP")
            elif close < vw * 0.997:
                score -= 0.3
                reasons.append("Below VWAP")

        # --- 8. S/R Level Breakout (weight 1.5) ---
        for rl in res_levels:
            prev_close = closes[i - 1]
            if prev_close < rl and close > rl and (close - rl) / rl > 0.001:
                score += 1.5
                reasons.append(f"Resistance Breakout {rl:.0f}")
                break
        for sl in sup_levels:
            prev_close = closes[i - 1]
            if prev_close > sl and close < sl and (sl - close) / sl > 0.001:
                score -= 1.5
                reasons.append(f"Support Breakdown {sl:.0f}")
                break

        # --- 9. Candle Body Strength (weight 1.0) ---
        body = abs(close - opn)
        full_range = high - low
        if full_range > 0:
            body_ratio = body / full_range
            if body_ratio > 0.75:
                if close > opn:
                    score += 1.0
                    reasons.append(f"Strong Bullish Candle ({body_ratio:.0%})")
                else:
                    score -= 1.0
                    reasons.append(f"Strong Bearish Candle ({body_ratio:.0%})")

        # --- Generate signal with alternating enforcement ---
        score = round(score, 2)
        if score >= 5.0 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "STRONG_BUY", "score": score,
                            "reasons": reasons, "price": close})
            last_signal_type = "BUY"
        elif score >= 3.5 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "BUY", "score": score,
                            "reasons": reasons, "price": close})
            last_signal_type = "BUY"
        elif score <= -3.5 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "STRONG_SELL", "score": score,
                            "reasons": reasons, "price": close})
            last_signal_type = "SELL"
        elif score <= -3.5 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "SELL", "score": score,
                            "reasons": reasons, "price": close})
            last_signal_type = "SELL"

    # --- Summary for latest bar ---
    last_i = n - 1
    t_last = candles[last_i]["time"]
    summary_score = 0
    summary_reasons = []

    # Squeeze status
    if n >= lookback + 1:
        last5_r = sum(highs[j] - lows[j] for j in range(last_i - 5, last_i)) / 5
        avg_r = sum(highs[j] - lows[j] for j in range(last_i - lookback, last_i)) / lookback
        sq = last5_r / avg_r if avg_r > 0 else 1
        d = 1.5 if sq < 0.7 else (0 if sq < 1.0 else -0.5)
        summary_score += d
        summary_reasons.append(("Squeeze", f"{sq:.2f}", d))

    bb_u = bb_upper_map.get(t_last)
    bb_l = bb_lower_map.get(t_last)
    if bb_u and bb_l:
        if closes[-1] > bb_u:
            d = 2.0
        elif closes[-1] < bb_l:
            d = -2.0
        else:
            d = 0
        summary_score += d
        summary_reasons.append(("BB Position", "Above Upper" if d > 0 else ("Below Lower" if d < 0 else "Inside"), d))

    vol_avg_s = sum(volumes[last_i - lookback:last_i]) / lookback if lookback > 0 else 1
    vol_r = volumes[last_i] / vol_avg_s if vol_avg_s > 0 else 0
    d = 1.5 if vol_r >= 2.0 else (0.5 if vol_r >= 1.5 else 0)
    if closes[-1] < opens[-1]:
        d = -d
    summary_score += d
    summary_reasons.append(("Volume", f"{vol_r:.1f}x", d))

    rsi_last = rsi_map.get(t_last, 50)
    d = 1.0 if rsi_last > 60 else (-1.0 if rsi_last < 40 else 0)
    summary_score += d
    summary_reasons.append(("RSI Thrust", f"{rsi_last:.0f}", d))

    mc_last = macd_map.get(t_last)
    mc_prev_s = macd_map.get(candles[last_i - 1]["time"]) if last_i > 0 else None
    if mc_last and mc_prev_s:
        hist_d = mc_last["histogram"] - mc_prev_s["histogram"]
        d = 1.0 if (mc_last["histogram"] > 0 and hist_d > 0) else (-1.0 if (mc_last["histogram"] < 0 and hist_d < 0) else 0)
        summary_score += d
        summary_reasons.append(("MACD Accel", "Bullish" if d > 0 else ("Bearish" if d < 0 else "Flat"), d))

    vw_last = vwap_map.get(t_last)
    if vw_last:
        d = 1.0 if closes[-1] > vw_last else -1.0
        summary_score += d
        summary_reasons.append(("VWAP", "Above" if d > 0 else "Below", d))

    if summary_score >= 5:
        verdict = "STRONG BUY"
    elif summary_score >= 3.5:
        verdict = "BUY"
    elif summary_score <= -5:
        verdict = "STRONG SELL"
    elif summary_score <= -3.5:
        verdict = "SELL"
    else:
        verdict = "NEUTRAL"

    summary = {
        "score": round(summary_score, 2),
        "verdict": verdict,
        "indicators": [{"name": r[0], "status": r[1], "weight": r[2]} for r in summary_reasons],
        "rsi": rsi_last,
        "macd": mc_last,
        "vwap": vw_last,
    }

    return signals, summary


# ---------------------------------------------------------------------------
# Strategy: Order Flow Analysis
# ---------------------------------------------------------------------------
def generate_orderflow_signals(candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr):
    """Order Flow Strategy — volume-price action analysis for institutional flow detection.

    Detects institutional buying/selling pressure using volume delta analysis,
    cumulative volume delta divergences, absorption patterns, and aggressive
    order detection. Only fires when clear order flow imbalance is confirmed.

    Composite scoring (total ~16):
      1. Volume Delta (buy vs sell pressure)      (weight 2.0)
      2. CVD trend & divergence                   (weight 2.0)
      3. Absorption detection (wick + volume)     (weight 2.0)
      4. Aggressive iceberg detection             (weight 1.5)
      5. VWAP institutional level                 (weight 1.5)
      6. Volume Profile POC proximity             (weight 1.5)
      7. RSI with volume confirmation             (weight 1.5)
      8. MACD with volume filter                  (weight 1.5)
      9. Price rejection (wicks at levels)        (weight 1.0)
     10. EMA trend alignment                      (weight 1.0)

    Thresholds: BUY >= 5.0, STRONG BUY >= 7.0
                SELL <= -5.0, STRONG SELL <= -7.0

    Enforces strict alternating BUY→SELL→BUY for clean entry/exit pairs.
    """
    n = len(candles)
    if n < 30:
        return [], {}

    rsi_map = {r["time"]: r["value"] for r in rsi_data}
    macd_map = {m["time"]: m for m in macd_data}
    vwap_map = {v["time"]: v["value"] for v in vwap_data}
    ema9_map = {e["time"]: e["value"] for e in ema9}
    ema21_map = {e["time"]: e["value"] for e in ema21}

    bb_upper_map, bb_lower_map, bb_mid_map = {}, {}, {}
    for b in bb:
        bb_upper_map[b["time"]] = b["upper"]
        bb_lower_map[b["time"]] = b["lower"]
        bb_mid_map[b["time"]] = b["middle"]

    sup_levels = [s["price"] for s in sr.get("support", [])]
    res_levels = [r["price"] for r in sr.get("resistance", [])]

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    opens = [c["open"] for c in candles]
    volumes = [c.get("volume", 0) for c in candles]

    # Precompute volume delta and CVD
    buy_volumes = []
    sell_volumes = []
    deltas = []
    cvd = [0.0]
    for i in range(n):
        rng = highs[i] - lows[i]
        if rng > 0 and volumes[i] > 0:
            buy_pct = (closes[i] - lows[i]) / rng
            sell_pct = (highs[i] - closes[i]) / rng
            bv = volumes[i] * buy_pct
            sv = volumes[i] * sell_pct
        else:
            bv = volumes[i] * 0.5
            sv = volumes[i] * 0.5
        buy_volumes.append(bv)
        sell_volumes.append(sv)
        delta = bv - sv
        deltas.append(delta)
        if i > 0:
            cvd.append(cvd[-1] + delta)

    # Precompute volume profile (POC = price with highest volume in lookback)
    lookback = 20

    signals = []
    last_signal_type = None  # enforce alternating

    for i in range(lookback, n):
        t = candles[i]["time"]
        close = closes[i]
        high = highs[i]
        low = lows[i]
        opn = opens[i]
        vol = volumes[i]
        score = 0.0
        reasons = []

        t_prev = candles[i - 1]["time"]
        t_prev2 = candles[i - 2]["time"] if i >= 2 else t_prev

        # --- 1. Volume Delta Analysis (weight 2.0) ---
        delta = deltas[i]
        delta_prev = deltas[i - 1]
        vol_avg = sum(volumes[i - lookback:i]) / lookback if lookback > 0 else 1
        delta_avg = sum(abs(deltas[j]) for j in range(i - lookback, i)) / lookback if lookback > 0 else 1

        if delta_avg > 0:
            delta_ratio = abs(delta) / delta_avg
            if delta > 0 and delta_ratio >= 2.0:
                score += 2.0
                reasons.append(f"Strong Buy Delta ({delta_ratio:.1f}x)")
            elif delta > 0 and delta_ratio >= 1.3:
                score += 1.0
                reasons.append(f"Buy Delta ({delta_ratio:.1f}x)")
            elif delta < 0 and delta_ratio >= 2.0:
                score -= 2.0
                reasons.append(f"Strong Sell Delta ({delta_ratio:.1f}x)")
            elif delta < 0 and delta_ratio >= 1.3:
                score -= 1.0
                reasons.append(f"Sell Delta ({delta_ratio:.1f}x)")

        # --- 2. CVD Trend & Divergence (weight 2.0) ---
        cvd_now = cvd[i]
        cvd_prev5 = cvd[i - 5] if i >= 5 else cvd[0]
        cvd_slope = cvd_now - cvd_prev5
        price_slope = closes[i] - closes[i - 5] if i >= 5 else 0

        # CVD divergence: price down but CVD up = hidden buying
        if price_slope < 0 and cvd_slope > 0 and abs(cvd_slope) > delta_avg * 2:
            score += 2.0
            reasons.append("CVD Bullish Divergence (Hidden Buying)")
        elif price_slope > 0 and cvd_slope < 0 and abs(cvd_slope) > delta_avg * 2:
            score -= 2.0
            reasons.append("CVD Bearish Divergence (Hidden Selling)")
        elif cvd_slope > 0 and price_slope > 0:
            score += 0.5
            reasons.append("CVD Confirms Uptrend")
        elif cvd_slope < 0 and price_slope < 0:
            score -= 0.5
            reasons.append("CVD Confirms Downtrend")

        # --- 3. Absorption Detection (weight 2.0) ---
        # High volume + long wick + small body = absorption (institutional limit orders)
        body = abs(close - opn)
        full_range = high - low
        upper_wick = high - max(close, opn)
        lower_wick = min(close, opn) - low

        if full_range > 0 and vol_avg > 0:
            body_ratio = body / full_range
            vol_ratio = vol / vol_avg

            # Bullish absorption: long lower wick, high volume, at support area
            if lower_wick > body * 2 and vol_ratio >= 1.5 and body_ratio < 0.4:
                score += 2.0
                reasons.append(f"Bullish Absorption (wick={lower_wick:.0f}, vol={vol_ratio:.1f}x)")
            # Bearish absorption: long upper wick, high volume, at resistance area
            elif upper_wick > body * 2 and vol_ratio >= 1.5 and body_ratio < 0.4:
                score -= 2.0
                reasons.append(f"Bearish Absorption (wick={upper_wick:.0f}, vol={vol_ratio:.1f}x)")

        # --- 4. Aggressive Iceberg Detection (weight 1.5) ---
        # Consecutive candles with high volume in same direction = iceberg order
        if i >= 3:
            consec_buy = all(deltas[i - j] > 0 and volumes[i - j] > vol_avg * 1.2 for j in range(3))
            consec_sell = all(deltas[i - j] < 0 and volumes[i - j] > vol_avg * 1.2 for j in range(3))
            if consec_buy:
                score += 1.5
                reasons.append("Iceberg Buy Detected (3-bar)")
            elif consec_sell:
                score -= 1.5
                reasons.append("Iceberg Sell Detected (3-bar)")

        # --- 5. VWAP Institutional Level (weight 1.5) ---
        vw = vwap_map.get(t)
        vw_prev = vwap_map.get(t_prev)
        if vw and vw_prev:
            # Price bouncing off VWAP with volume = institutional interest
            if closes[i - 1] <= vw_prev * 1.001 and close > vw * 1.002 and vol > vol_avg * 1.3:
                score += 1.5
                reasons.append("VWAP Bounce (Institutional Buy)")
            elif closes[i - 1] >= vw_prev * 0.999 and close < vw * 0.998 and vol > vol_avg * 1.3:
                score -= 1.5
                reasons.append("VWAP Rejection (Institutional Sell)")
            elif close > vw:
                score += 0.3
                reasons.append("Above VWAP")
            elif close < vw:
                score -= 0.3
                reasons.append("Below VWAP")

        # --- 6. Volume Profile POC Proximity (weight 1.5) ---
        # Build a simple volume profile from lookback period
        price_vol = {}
        for j in range(i - lookback, i):
            rounded_price = round(closes[j] / 10) * 10  # bin prices
            price_vol[rounded_price] = price_vol.get(rounded_price, 0) + volumes[j]
        if price_vol:
            poc_price = max(price_vol, key=price_vol.get)
            poc_dist = (close - poc_price) / close if close > 0 else 0
            if abs(poc_dist) < 0.002:
                # At POC — look at delta for direction
                if delta > 0:
                    score += 1.5
                    reasons.append(f"At POC {poc_price:.0f} + Buy Delta")
                elif delta < 0:
                    score -= 1.5
                    reasons.append(f"At POC {poc_price:.0f} + Sell Delta")
            elif poc_dist > 0.005 and delta > 0:
                score += 0.5
                reasons.append(f"Above POC {poc_price:.0f}")
            elif poc_dist < -0.005 and delta < 0:
                score -= 0.5
                reasons.append(f"Below POC {poc_price:.0f}")

        # --- 7. RSI with Volume Confirmation (weight 1.5) ---
        rsi_val = rsi_map.get(t, 50)
        rsi_prev = rsi_map.get(t_prev, 50)
        if rsi_val > 55 and rsi_val > rsi_prev and delta > 0:
            score += 1.5
            reasons.append(f"RSI Rising + Buy Volume ({rsi_val:.0f})")
        elif rsi_val < 45 and rsi_val < rsi_prev and delta < 0:
            score -= 1.5
            reasons.append(f"RSI Falling + Sell Volume ({rsi_val:.0f})")
        elif rsi_val < 30 and delta > 0:
            # RSI oversold but buy volume = accumulation
            score += 1.0
            reasons.append(f"RSI Oversold + Accumulation ({rsi_val:.0f})")
        elif rsi_val > 70 and delta < 0:
            # RSI overbought but sell volume = distribution
            score -= 1.0
            reasons.append(f"RSI Overbought + Distribution ({rsi_val:.0f})")

        # --- 8. MACD with Volume Filter (weight 1.5) ---
        mc = macd_map.get(t)
        mc_prev = macd_map.get(t_prev)
        if mc and mc_prev:
            hist = mc["histogram"]
            hist_p = mc_prev["histogram"]
            # MACD cross confirmed by volume
            if hist > 0 and hist_p <= 0 and vol > vol_avg * 1.3:
                score += 1.5
                reasons.append("MACD Cross UP + Volume")
            elif hist < 0 and hist_p >= 0 and vol > vol_avg * 1.3:
                score -= 1.5
                reasons.append("MACD Cross DOWN + Volume")
            elif hist > hist_p and delta > 0:
                score += 0.3
                reasons.append("MACD Rising + Buy Flow")
            elif hist < hist_p and delta < 0:
                score -= 0.3
                reasons.append("MACD Falling + Sell Flow")

        # --- 9. Price Rejection at Levels (weight 1.0) ---
        for sl in sup_levels:
            if low <= sl * 1.002 and close > sl and lower_wick > body:
                score += 1.0
                reasons.append(f"Rejection at Support {sl:.0f}")
                break
        for rl in res_levels:
            if high >= rl * 0.998 and close < rl and upper_wick > body:
                score -= 1.0
                reasons.append(f"Rejection at Resistance {rl:.0f}")
                break

        # --- 10. EMA Trend Alignment (weight 1.0) ---
        e9 = ema9_map.get(t)
        e21 = ema21_map.get(t)
        if e9 and e21:
            if e9 > e21 and close > e9 and delta > 0:
                score += 1.0
                reasons.append("EMA Bullish + Buy Flow")
            elif e9 < e21 and close < e9 and delta < 0:
                score -= 1.0
                reasons.append("EMA Bearish + Sell Flow")

        # --- Generate signal with alternating enforcement ---
        score = round(score, 2)
        if score >= 5.0 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "STRONG_BUY", "score": score,
                            "reasons": reasons, "price": close})
            last_signal_type = "BUY"
        elif score >= 3.5 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "BUY", "score": score,
                            "reasons": reasons, "price": close})
            last_signal_type = "BUY"
        elif score <= -3.5 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "STRONG_SELL", "score": score,
                            "reasons": reasons, "price": close})
            last_signal_type = "SELL"
        elif score <= -3.5 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "SELL", "score": score,
                            "reasons": reasons, "price": close})
            last_signal_type = "SELL"

    # --- Summary for latest bar ---
    last_i = n - 1
    t_last = candles[last_i]["time"]
    summary_score = 0
    summary_reasons = []

    # Delta
    d_last = deltas[last_i]
    d_avg = sum(abs(deltas[j]) for j in range(last_i - lookback, last_i)) / lookback if lookback > 0 else 1
    d_r = abs(d_last) / d_avg if d_avg > 0 else 0
    d = 2.0 if (d_last > 0 and d_r >= 2) else (1.0 if d_last > 0 else (-2.0 if (d_last < 0 and d_r >= 2) else (-1.0 if d_last < 0 else 0)))
    summary_score += d
    summary_reasons.append(("Delta", f"{d_last:.0f} ({d_r:.1f}x)", d))

    # CVD
    cvd_s = cvd[last_i] - cvd[last_i - 5] if last_i >= 5 else 0
    d = 1.5 if cvd_s > 0 else (-1.5 if cvd_s < 0 else 0)
    summary_score += d
    summary_reasons.append(("CVD Trend", "Rising" if d > 0 else ("Falling" if d < 0 else "Flat"), d))

    # Volume
    vol_r = volumes[last_i] / (sum(volumes[last_i - lookback:last_i]) / lookback) if sum(volumes[last_i - lookback:last_i]) > 0 else 0
    d = 1.0 if vol_r >= 1.5 else 0
    if closes[-1] < opens[-1]:
        d = -d
    summary_score += d
    summary_reasons.append(("Volume", f"{vol_r:.1f}x", d))

    rsi_last = rsi_map.get(t_last, 50)
    d = 1.0 if (rsi_last > 55 and deltas[last_i] > 0) else (-1.0 if (rsi_last < 45 and deltas[last_i] < 0) else 0)
    summary_score += d
    summary_reasons.append(("RSI+Flow", f"{rsi_last:.0f}", d))

    mc_last = macd_map.get(t_last)
    vw_last = vwap_map.get(t_last)
    if vw_last:
        d = 1.0 if closes[-1] > vw_last else -1.0
        summary_score += d
        summary_reasons.append(("VWAP", "Above" if d > 0 else "Below", d))

    if summary_score >= 5:
        verdict = "STRONG BUY"
    elif summary_score >= 3.5:
        verdict = "BUY"
    elif summary_score <= -5:
        verdict = "STRONG SELL"
    elif summary_score <= -3.5:
        verdict = "SELL"
    else:
        verdict = "NEUTRAL"

    summary = {
        "score": round(summary_score, 2),
        "verdict": verdict,
        "indicators": [{"name": r[0], "status": r[1], "weight": r[2]} for r in summary_reasons],
        "rsi": rsi_last,
        "macd": mc_last,
        "vwap": vw_last,
    }

    return signals, summary


# ---------------------------------------------------------------------------
# Strategy: Price Action (Pure Chart Structure Analysis)
# ---------------------------------------------------------------------------
def generate_priceaction_signals(candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr):
    """Price Action Strategy — pure price structure analysis without lagging indicators.

    Reads raw candle formations, swing structure, trend structure, and key levels
    to identify high-probability entries based on what price is actually doing.

    Composite scoring (total ~16):
      1. Trend Structure (HH/HL or LH/LL)          (weight 2.0)
      2. Candlestick Reversal Patterns               (weight 2.0)
      3. Pin Bar / Rejection at Key Levels            (weight 2.0)
      4. Inside Bar Breakout                          (weight 1.5)
      5. Engulfing with Momentum                      (weight 1.5)
      6. Support / Resistance Reaction                (weight 1.5)
      7. Higher Timeframe Candle Context              (weight 1.5)
      8. Consecutive Candle Momentum                  (weight 1.0)
      9. Range Contraction then Expansion             (weight 1.0)
     10. Gap / Window Analysis                        (weight 1.0)
     11. Swing Failure Pattern (SFP)                  (weight 1.5)

    Thresholds: BUY >= 5.0, STRONG BUY >= 7.0
                SELL <= -5.0, STRONG SELL <= -7.0

    Enforces strict alternating BUY→SELL→BUY for clean entry/exit pairs.
    """
    n = len(candles)
    if n < 30:
        return [], {}

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    opens = [c["open"] for c in candles]
    volumes = [c.get("volume", 0) for c in candles]

    vwap_map = {v["time"]: v["value"] for v in vwap_data}
    ema9_map = {e["time"]: e["value"] for e in ema9}
    ema21_map = {e["time"]: e["value"] for e in ema21}

    bb_upper_map, bb_lower_map = {}, {}
    for b in bb:
        bb_upper_map[b["time"]] = b["upper"]
        bb_lower_map[b["time"]] = b["lower"]

    sup_levels = [s["price"] for s in sr.get("support", [])]
    res_levels = [r["price"] for r in sr.get("resistance", [])]

    # --- Precompute swing highs and swing lows (5-bar lookback/forward) ---
    swing_highs = []  # (index, price)
    swing_lows = []
    swing_lb = 5
    for i in range(swing_lb, n - swing_lb):
        if all(highs[i] >= highs[i - j] for j in range(1, swing_lb + 1)) and \
           all(highs[i] >= highs[i + j] for j in range(1, swing_lb + 1)):
            swing_highs.append((i, highs[i]))
        if all(lows[i] <= lows[i - j] for j in range(1, swing_lb + 1)) and \
           all(lows[i] <= lows[i + j] for j in range(1, swing_lb + 1)):
            swing_lows.append((i, lows[i]))

    signals = []
    last_signal_type = None
    lookback = 20

    for i in range(lookback, n):
        t = candles[i]["time"]
        close = closes[i]
        high = highs[i]
        low = lows[i]
        opn = opens[i]
        vol = volumes[i]
        score = 0.0
        reasons = []

        body = abs(close - opn)
        full_range = high - low
        upper_wick = high - max(close, opn)
        lower_wick = min(close, opn) - low
        is_bullish = close > opn
        is_bearish = close < opn
        body_ratio = body / full_range if full_range > 0 else 0

        prev_close = closes[i - 1]
        prev_open = opens[i - 1]
        prev_high = highs[i - 1]
        prev_low = lows[i - 1]
        prev_body = abs(prev_close - prev_open)
        prev_range = prev_high - prev_low

        # --- 1. Trend Structure: HH/HL or LH/LL (weight 2.0) ---
        recent_sh = [p for idx, p in swing_highs if i - 20 <= idx < i]
        recent_sl = [p for idx, p in swing_lows if i - 20 <= idx < i]
        if len(recent_sh) >= 2 and len(recent_sl) >= 2:
            # Higher Highs + Higher Lows = uptrend
            hh = recent_sh[-1] > recent_sh[-2]
            hl = recent_sl[-1] > recent_sl[-2]
            lh = recent_sh[-1] < recent_sh[-2]
            ll = recent_sl[-1] < recent_sl[-2]

            if hh and hl:
                score += 2.0
                reasons.append("Uptrend Structure (HH+HL)")
            elif lh and ll:
                score -= 2.0
                reasons.append("Downtrend Structure (LH+LL)")
            elif hh and not hl:
                score += 0.5
                reasons.append("Weak Uptrend (HH only)")
            elif ll and not lh:
                score -= 0.5
                reasons.append("Weak Downtrend (LL only)")

        # --- 2. Candlestick Reversal Patterns (weight 2.0) ---
        # Morning Star (3-candle bullish reversal)
        if i >= 2:
            c0_bear = closes[i - 2] < opens[i - 2]
            c1_small = abs(closes[i - 1] - opens[i - 1]) < (highs[i - 1] - lows[i - 1]) * 0.3
            c2_bull = is_bullish and body > prev_body
            if c0_bear and c1_small and c2_bull and close > (opens[i - 2] + closes[i - 2]) / 2:
                score += 2.0
                reasons.append("Morning Star Reversal")

            # Evening Star (3-candle bearish reversal)
            c0_bull = closes[i - 2] > opens[i - 2]
            c2_bear = is_bearish and body > prev_body
            if c0_bull and c1_small and c2_bear and close < (opens[i - 2] + closes[i - 2]) / 2:
                score -= 2.0
                reasons.append("Evening Star Reversal")

        # Hammer at lows (bullish)
        if lower_wick > body * 2 and upper_wick < body * 0.5 and full_range > 0:
            # Check if at recent lows
            recent_low = min(lows[i - 10:i])
            if low <= recent_low * 1.002:
                score += 1.5
                reasons.append("Hammer at Lows")

        # Shooting Star at highs (bearish)
        if upper_wick > body * 2 and lower_wick < body * 0.5 and full_range > 0:
            recent_high = max(highs[i - 10:i])
            if high >= recent_high * 0.998:
                score -= 1.5
                reasons.append("Shooting Star at Highs")

        # --- 3. Pin Bar / Rejection at Key Levels (weight 2.0) ---
        for sl in sup_levels:
            if low <= sl * 1.003 and close > sl:
                if lower_wick > body * 1.5:
                    score += 2.0
                    reasons.append(f"Pin Bar Rejection at Support {sl:.0f}")
                    break
                elif is_bullish:
                    score += 0.5
                    reasons.append(f"Bullish Close at Support {sl:.0f}")
                    break

        for rl in res_levels:
            if high >= rl * 0.997 and close < rl:
                if upper_wick > body * 1.5:
                    score -= 2.0
                    reasons.append(f"Pin Bar Rejection at Resistance {rl:.0f}")
                    break
                elif is_bearish:
                    score -= 0.5
                    reasons.append(f"Bearish Close at Resistance {rl:.0f}")
                    break

        # --- 4. Inside Bar Breakout (weight 1.5) ---
        if i >= 1:
            is_inside = high <= prev_high and low >= prev_low
            if not is_inside and i >= 2:
                # Check if previous was inside bar, and current breaks out
                was_inside = highs[i - 1] <= highs[i - 2] and lows[i - 1] >= lows[i - 2]
                if was_inside:
                    if close > highs[i - 2]:
                        score += 1.5
                        reasons.append("Inside Bar Bullish Breakout")
                    elif close < lows[i - 2]:
                        score -= 1.5
                        reasons.append("Inside Bar Bearish Breakout")

        # --- 5. Engulfing with Momentum (weight 1.5) ---
        if is_bullish and prev_close < prev_open:
            # Bullish engulfing: current body completely covers previous
            if opn <= prev_close and close >= prev_open and body > prev_body * 1.2:
                score += 1.5
                reasons.append("Bullish Engulfing")
        elif is_bearish and prev_close > prev_open:
            # Bearish engulfing
            if opn >= prev_close and close <= prev_open and body > prev_body * 1.2:
                score -= 1.5
                reasons.append("Bearish Engulfing")

        # --- 6. Support / Resistance Reaction (weight 1.5) ---
        # Price touching and bouncing from S/R with strong candle body
        for sl in sup_levels:
            if abs(low - sl) / close < 0.003 and is_bullish and body_ratio > 0.6:
                score += 1.5
                reasons.append(f"Strong Bounce off Support {sl:.0f}")
                break
        for rl in res_levels:
            if abs(high - rl) / close < 0.003 and is_bearish and body_ratio > 0.6:
                score -= 1.5
                reasons.append(f"Strong Rejection at Resistance {rl:.0f}")
                break

        # --- 7. Higher Timeframe Candle Context (weight 1.5) ---
        # Use 5-bar aggregate as proxy for higher timeframe
        if i >= 5:
            htf_open = opens[i - 4]
            htf_close = close
            htf_high = max(highs[i - 4:i + 1])
            htf_low = min(lows[i - 4:i + 1])
            htf_body = abs(htf_close - htf_open)
            htf_range = htf_high - htf_low
            htf_ratio = htf_body / htf_range if htf_range > 0 else 0

            if htf_close > htf_open and htf_ratio > 0.6:
                score += 1.5
                reasons.append("HTF Bullish Structure")
            elif htf_close < htf_open and htf_ratio > 0.6:
                score -= 1.5
                reasons.append("HTF Bearish Structure")

        # --- 8. Consecutive Candle Momentum (weight 1.0) ---
        if i >= 3:
            consec_bull = all(closes[i - j] > opens[i - j] for j in range(3))
            consec_bear = all(closes[i - j] < opens[i - j] for j in range(3))
            # 3 consecutive same-direction with increasing bodies
            if consec_bull and body > abs(closes[i - 1] - opens[i - 1]):
                score += 1.0
                reasons.append("3-Bar Bullish Momentum")
            elif consec_bear and body > abs(closes[i - 1] - opens[i - 1]):
                score -= 1.0
                reasons.append("3-Bar Bearish Momentum")

        # --- 9. Range Contraction then Expansion (weight 1.0) ---
        if i >= 5:
            avg_range_5 = sum(highs[j] - lows[j] for j in range(i - 5, i)) / 5
            if full_range > avg_range_5 * 1.8 and body_ratio > 0.6:
                if is_bullish:
                    score += 1.0
                    reasons.append(f"Range Expansion Bullish ({full_range / avg_range_5:.1f}x)")
                elif is_bearish:
                    score -= 1.0
                    reasons.append(f"Range Expansion Bearish ({full_range / avg_range_5:.1f}x)")

        # --- 10. Gap / Window Analysis (weight 1.0) ---
        if i >= 1:
            gap_up = low > prev_high  # gap up
            gap_down = high < prev_low  # gap down
            if gap_up and is_bullish:
                score += 1.0
                reasons.append(f"Gap Up + Bullish Follow ({low - prev_high:.0f} pts)")
            elif gap_down and is_bearish:
                score -= 1.0
                reasons.append(f"Gap Down + Bearish Follow ({prev_low - high:.0f} pts)")
            # Gap fill rejection (price fills gap then reverses)
            elif gap_up and is_bearish and close < prev_high:
                score -= 0.5
                reasons.append("Gap Fill Rejection (Bearish)")
            elif gap_down and is_bullish and close > prev_low:
                score += 0.5
                reasons.append("Gap Fill Rejection (Bullish)")

        # --- 11. Swing Failure Pattern - SFP (weight 1.5) ---
        # Price takes out a prior swing high/low then closes back inside = trap
        if len(recent_sh) >= 1 and high > recent_sh[-1] and close < recent_sh[-1]:
            score -= 1.5
            reasons.append(f"SFP Bearish (false break {recent_sh[-1]:.0f})")
        if len(recent_sl) >= 1 and low < recent_sl[-1] and close > recent_sl[-1]:
            score += 1.5
            reasons.append(f"SFP Bullish (false break {recent_sl[-1]:.0f})")

        # --- Generate signal with alternating enforcement ---
        score = round(score, 2)
        if score >= 5.0 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "STRONG_BUY", "score": score,
                            "reasons": reasons, "price": close})
            last_signal_type = "BUY"
        elif score >= 3.5 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "BUY", "score": score,
                            "reasons": reasons, "price": close})
            last_signal_type = "BUY"
        elif score <= -3.5 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "STRONG_SELL", "score": score,
                            "reasons": reasons, "price": close})
            last_signal_type = "SELL"
        elif score <= -3.5 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "SELL", "score": score,
                            "reasons": reasons, "price": close})
            last_signal_type = "SELL"

    # --- Summary for latest bar ---
    last_i = n - 1
    t_last = candles[last_i]["time"]
    summary_score = 0
    summary_reasons = []

    # Trend structure
    recent_sh_s = [p for idx, p in swing_highs if last_i - 20 <= idx < last_i]
    recent_sl_s = [p for idx, p in swing_lows if last_i - 20 <= idx < last_i]
    if len(recent_sh_s) >= 2 and len(recent_sl_s) >= 2:
        hh = recent_sh_s[-1] > recent_sh_s[-2]
        hl = recent_sl_s[-1] > recent_sl_s[-2]
        if hh and hl:
            d = 2.0
            summary_reasons.append(("Trend", "Uptrend (HH+HL)", d))
        elif not hh and not hl:
            d = -2.0
            summary_reasons.append(("Trend", "Downtrend (LH+LL)", d))
        else:
            d = 0
            summary_reasons.append(("Trend", "Ranging", d))
        summary_score += d

    # Last candle type
    last_body = abs(closes[-1] - opens[-1])
    last_range = highs[-1] - lows[-1]
    last_ratio = last_body / last_range if last_range > 0 else 0
    last_bull = closes[-1] > opens[-1]
    d = 1.5 if (last_bull and last_ratio > 0.6) else (-1.5 if (not last_bull and last_ratio > 0.6) else 0)
    summary_score += d
    summary_reasons.append(("Candle", f"{'Bullish' if last_bull else 'Bearish'} ({last_ratio:.0%})", d))

    # S/R proximity
    near_sup = any(abs(lows[-1] - sl) / closes[-1] < 0.005 for sl in sup_levels)
    near_res = any(abs(highs[-1] - rl) / closes[-1] < 0.005 for rl in res_levels)
    if near_sup and last_bull:
        d = 1.5
    elif near_res and not last_bull:
        d = -1.5
    else:
        d = 0
    summary_score += d
    summary_reasons.append(("S/R", "At Support" if near_sup else ("At Resistance" if near_res else "Clear"), d))

    # Momentum (3-bar)
    if n >= 3:
        mom_bull = all(closes[-1 - j] > opens[-1 - j] for j in range(3))
        mom_bear = all(closes[-1 - j] < opens[-1 - j] for j in range(3))
        d = 1.0 if mom_bull else (-1.0 if mom_bear else 0)
        summary_score += d
        summary_reasons.append(("Momentum", "Bullish" if d > 0 else ("Bearish" if d < 0 else "Mixed"), d))

    # EMA context
    e9 = ema9_map.get(t_last)
    e21 = ema21_map.get(t_last)
    if e9 and e21:
        d = 1.0 if (closes[-1] > e9 > e21) else (-1.0 if (closes[-1] < e9 < e21) else 0)
        summary_score += d
        summary_reasons.append(("EMA Context", "Bullish" if d > 0 else ("Bearish" if d < 0 else "Neutral"), d))

    rsi_map = {r["time"]: r["value"] for r in rsi_data}
    rsi_last = rsi_map.get(t_last, 50)
    macd_map = {m["time"]: m for m in macd_data}
    mc_last = macd_map.get(t_last)
    vw_last = vwap_map.get(t_last)

    if summary_score >= 5:
        verdict = "STRONG BUY"
    elif summary_score >= 3.5:
        verdict = "BUY"
    elif summary_score <= -5:
        verdict = "STRONG SELL"
    elif summary_score <= -3.5:
        verdict = "SELL"
    else:
        verdict = "NEUTRAL"

    summary = {
        "score": round(summary_score, 2),
        "verdict": verdict,
        "indicators": [{"name": r[0], "status": r[1], "weight": r[2]} for r in summary_reasons],
        "rsi": rsi_last,
        "macd": mc_last,
        "vwap": vw_last,
    }

    return signals, summary


# ---------------------------------------------------------------------------
# Strategy: Breakout (Range/Channel Breakout Detection)
# ---------------------------------------------------------------------------
def generate_breakout_signals(candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr):
    n = len(candles)
    if n < 30:
        return [], {}
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    opens = [c["open"] for c in candles]
    volumes = [c.get("volume", 0) for c in candles]

    rsi_map = {r["time"]: r["value"] for r in rsi_data}
    macd_map = {m["time"]: m for m in macd_data}
    vwap_map = {v["time"]: v["value"] for v in vwap_data}
    ema9_map = {e["time"]: e["value"] for e in ema9}
    ema21_map = {e["time"]: e["value"] for e in ema21}
    bb_upper_map, bb_lower_map = {}, {}
    for b in bb:
        bb_upper_map[b["time"]] = b["upper"]
        bb_lower_map[b["time"]] = b["lower"]
    sup_levels = [s["price"] for s in sr.get("support", [])]
    res_levels = [r["price"] for r in sr.get("resistance", [])]

    lookback = 20
    signals = []
    last_signal_type = None

    for i in range(lookback, n):
        t = candles[i]["time"]
        close = closes[i]
        high = highs[i]
        low = lows[i]
        opn = opens[i]
        vol = volumes[i]
        score = 0.0
        reasons = []
        t_prev = candles[i - 1]["time"]
        body = abs(close - opn)
        full_range = high - low

        # Donchian Channel breakout
        dc_high = max(highs[i - lookback:i])
        dc_low = min(lows[i - lookback:i])
        if close > dc_high:
            score += 2.0
            reasons.append(f"Donchian Breakout UP ({dc_high:.0f})")
        elif close < dc_low:
            score -= 2.0
            reasons.append(f"Donchian Breakout DOWN ({dc_low:.0f})")

        # BB expansion breakout
        bb_u = bb_upper_map.get(t)
        bb_l = bb_lower_map.get(t)
        t_p5 = candles[i - 5]["time"] if i >= 5 else candles[0]["time"]
        bb_u5 = bb_upper_map.get(t_p5, bb_u)
        bb_l5 = bb_lower_map.get(t_p5, bb_l)
        if bb_u and bb_l and bb_u5 and bb_l5:
            curr_w = bb_u - bb_l
            prev_w = bb_u5 - bb_l5
            expanding = curr_w > prev_w * 1.2
            if close > bb_u and expanding:
                score += 2.0
                reasons.append("BB Expansion Breakout UP")
            elif close < bb_l and expanding:
                score -= 2.0
                reasons.append("BB Expansion Breakout DOWN")

        # Volume surge
        vol_avg = sum(volumes[i - lookback:i]) / lookback
        if vol_avg > 0 and vol > 0:
            vr = vol / vol_avg
            if vr >= 2.5:
                vs = 2.0
            elif vr >= 1.8:
                vs = 1.2
            else:
                vs = 0
            if vs > 0:
                if close > opn:
                    score += vs
                    reasons.append(f"Volume Surge {vr:.1f}x (Bull)")
                else:
                    score -= vs
                    reasons.append(f"Volume Surge {vr:.1f}x (Bear)")

        # ATR expansion
        atr_vals = []
        for j in range(max(1, i - 14), i + 1):
            tr = max(highs[j] - lows[j], abs(highs[j] - closes[j - 1]), abs(lows[j] - closes[j - 1]))
            atr_vals.append(tr)
        if len(atr_vals) >= 2:
            cur_atr = atr_vals[-1]
            avg_atr = sum(atr_vals[:-1]) / len(atr_vals[:-1])
            if avg_atr > 0 and cur_atr > avg_atr * 1.5:
                d = 1.5 if close > opn else -1.5
                score += d
                reasons.append(f"ATR Expansion ({cur_atr / avg_atr:.1f}x)")

        # EMA alignment
        e9 = ema9_map.get(t)
        e21 = ema21_map.get(t)
        if e9 and e21:
            if close > e9 > e21:
                score += 1.5
                reasons.append("EMA Bullish Aligned")
            elif close < e9 < e21:
                score -= 1.5
                reasons.append("EMA Bearish Aligned")

        # RSI thrust
        rsi_val = rsi_map.get(t, 50)
        if rsi_val > 65:
            score += 1.5
            reasons.append(f"RSI Thrust UP ({rsi_val:.0f})")
        elif rsi_val < 35:
            score -= 1.5
            reasons.append(f"RSI Thrust DOWN ({rsi_val:.0f})")

        # MACD acceleration
        mc = macd_map.get(t)
        mc_p = macd_map.get(t_prev)
        if mc and mc_p:
            if mc["histogram"] > 0 and mc["histogram"] > mc_p["histogram"]:
                score += 1.5
                reasons.append("MACD Accel UP")
            elif mc["histogram"] < 0 and mc["histogram"] < mc_p["histogram"]:
                score -= 1.5
                reasons.append("MACD Accel DOWN")

        # S/R pierce
        for rl in res_levels:
            if closes[i - 1] < rl and close > rl:
                score += 1.5
                reasons.append(f"Resistance Pierce {rl:.0f}")
                break
        for sl in sup_levels:
            if closes[i - 1] > sl and close < sl:
                score -= 1.5
                reasons.append(f"Support Pierce {sl:.0f}")
                break

        # Candle body strength
        if full_range > 0 and body / full_range > 0.65:
            d = 1.0 if close > opn else -1.0
            score += d
            reasons.append(f"Strong Body ({body / full_range:.0%})")

        score = round(score, 2)
        if score >= 5.0 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "STRONG_BUY", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "BUY"
        elif score >= 3.5 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "BUY", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "BUY"
        elif score <= -3.5 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "STRONG_SELL", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "SELL"
        elif score <= -3.5 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "SELL", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "SELL"

    li = n - 1
    tl = candles[li]["time"]
    ss = 0
    sr_list = []
    dc_h = max(highs[li - lookback:li])
    dc_l = min(lows[li - lookback:li])
    d = 2.0 if closes[-1] > dc_h else (-2.0 if closes[-1] < dc_l else 0)
    ss += d
    sr_list.append(("Donchian", f"{'Above' if d > 0 else 'Below' if d < 0 else 'Inside'}", d))
    vol_avg_s = sum(volumes[li - lookback:li]) / lookback
    vr_s = volumes[li] / vol_avg_s if vol_avg_s > 0 else 0
    d = 1.5 if vr_s >= 1.8 else 0
    if closes[-1] < opens[-1]:
        d = -d
    ss += d
    sr_list.append(("Volume", f"{vr_s:.1f}x", d))
    rsi_l = rsi_map.get(tl, 50)
    d = 1.0 if rsi_l > 60 else (-1.0 if rsi_l < 40 else 0)
    ss += d
    sr_list.append(("RSI", f"{rsi_l:.0f}", d))
    mc_l = macd_map.get(tl)
    vw_l = vwap_map.get(tl)
    verdict = "STRONG BUY" if ss >= 5 else ("BUY" if ss >= 3.5 else ("STRONG SELL" if ss <= -5 else ("SELL" if ss <= -3.5 else "NEUTRAL")))
    summary = {"score": round(ss, 2), "verdict": verdict, "indicators": [{"name": r[0], "status": r[1], "weight": r[2]} for r in sr_list], "rsi": rsi_l, "macd": mc_l, "vwap": vw_l}
    return signals, summary


# ---------------------------------------------------------------------------
# Strategy: Momentum (Rate of Change + Acceleration)
# ---------------------------------------------------------------------------
def generate_momentum_signals(candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr):
    n = len(candles)
    if n < 30:
        return [], {}
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    opens = [c["open"] for c in candles]
    volumes = [c.get("volume", 0) for c in candles]

    rsi_map = {r["time"]: r["value"] for r in rsi_data}
    macd_map = {m["time"]: m for m in macd_data}
    vwap_map = {v["time"]: v["value"] for v in vwap_data}
    ema9_map = {e["time"]: e["value"] for e in ema9}
    ema21_map = {e["time"]: e["value"] for e in ema21}

    lookback = 20
    signals = []
    last_signal_type = None

    roc = [0.0] * n
    roc_roc = [0.0] * n
    for i in range(10, n):
        roc[i] = (closes[i] - closes[i - 10]) / closes[i - 10] * 100 if closes[i - 10] > 0 else 0
    for i in range(11, n):
        roc_roc[i] = roc[i] - roc[i - 1]

    for i in range(lookback, n):
        t = candles[i]["time"]
        close = closes[i]
        opn = opens[i]
        vol = volumes[i]
        score = 0.0
        reasons = []
        t_prev = candles[i - 1]["time"]

        # ROC
        if roc[i] > 1.0:
            score += 2.0
            reasons.append(f"ROC Strong UP ({roc[i]:.2f}%)")
        elif roc[i] > 0.3:
            score += 1.0
            reasons.append(f"ROC UP ({roc[i]:.2f}%)")
        elif roc[i] < -1.0:
            score -= 2.0
            reasons.append(f"ROC Strong DOWN ({roc[i]:.2f}%)")
        elif roc[i] < -0.3:
            score -= 1.0
            reasons.append(f"ROC DOWN ({roc[i]:.2f}%)")

        # ROC acceleration
        if roc_roc[i] > 0.3 and roc[i] > 0:
            score += 1.5
            reasons.append("Momentum Accelerating UP")
        elif roc_roc[i] < -0.3 and roc[i] < 0:
            score -= 1.5
            reasons.append("Momentum Accelerating DOWN")

        # RSI momentum crossing 50
        rsi_val = rsi_map.get(t, 50)
        rsi_prev = rsi_map.get(t_prev, 50)
        if rsi_prev <= 50 and rsi_val > 55:
            score += 2.0
            reasons.append(f"RSI Cross Above 50 ({rsi_val:.0f})")
        elif rsi_prev >= 50 and rsi_val < 45:
            score -= 2.0
            reasons.append(f"RSI Cross Below 50 ({rsi_val:.0f})")

        # MACD histogram expansion
        mc = macd_map.get(t)
        mc_p = macd_map.get(t_prev)
        if mc and mc_p:
            h_delta = mc["histogram"] - mc_p["histogram"]
            if mc["histogram"] > 0 and h_delta > 0:
                score += 2.0
                reasons.append("MACD Expanding Bullish")
            elif mc["histogram"] < 0 and h_delta < 0:
                score -= 2.0
                reasons.append("MACD Expanding Bearish")

        # EMA spread widening
        e9 = ema9_map.get(t)
        e21 = ema21_map.get(t)
        e9p = ema9_map.get(t_prev)
        e21p = ema21_map.get(t_prev)
        if e9 and e21 and e9p and e21p:
            spread = e9 - e21
            prev_spread = e9p - e21p
            if spread > 0 and spread > prev_spread:
                score += 1.5
                reasons.append("EMA Spread Widening Bullish")
            elif spread < 0 and spread < prev_spread:
                score -= 1.5
                reasons.append("EMA Spread Widening Bearish")

        # ADX-like directional strength
        if i >= 14:
            up_moves = sum(max(0, highs[j] - highs[j - 1]) for j in range(i - 13, i + 1))
            down_moves = sum(max(0, lows[j - 1] - lows[j]) for j in range(i - 13, i + 1))
            total = up_moves + down_moves
            if total > 0:
                di_diff = abs(up_moves - down_moves) / total
                if di_diff > 0.4:
                    d = 1.5 if up_moves > down_moves else -1.5
                    score += d
                    reasons.append(f"Strong Directional Move ({di_diff:.2f})")

        # VWAP momentum
        vw = vwap_map.get(t)
        if vw and vw > 0:
            dev = (close - vw) / vw
            if dev > 0.005:
                score += 1.5
                reasons.append(f"Above VWAP +{dev:.3f}")
            elif dev < -0.005:
                score -= 1.5
                reasons.append(f"Below VWAP {dev:.3f}")

        # Volume momentum
        vol_avg = sum(volumes[i - lookback:i]) / lookback if lookback > 0 else 1
        if vol_avg > 0 and vol > vol_avg * 1.5:
            if close > opn:
                score += 1.5
                reasons.append(f"Rising Volume Bullish ({vol / vol_avg:.1f}x)")
            else:
                score -= 1.5
                reasons.append(f"Rising Volume Bearish ({vol / vol_avg:.1f}x)")

        # Consecutive closes
        if i >= 3:
            if all(closes[i - j] > opens[i - j] for j in range(3)):
                score += 1.0
                reasons.append("3-Bar Bull Run")
            elif all(closes[i - j] < opens[i - j] for j in range(3)):
                score -= 1.0
                reasons.append("3-Bar Bear Run")

        score = round(score, 2)
        if score >= 5.0 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "STRONG_BUY", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "BUY"
        elif score >= 3.5 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "BUY", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "BUY"
        elif score <= -3.5 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "STRONG_SELL", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "SELL"
        elif score <= -3.5 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "SELL", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "SELL"

    li = n - 1
    tl = candles[li]["time"]
    ss = 0
    sr_list = []
    d = 1.5 if roc[li] > 0.5 else (-1.5 if roc[li] < -0.5 else 0)
    ss += d
    sr_list.append(("ROC", f"{roc[li]:.2f}%", d))
    rsi_l = rsi_map.get(tl, 50)
    d = 1.5 if rsi_l > 55 else (-1.5 if rsi_l < 45 else 0)
    ss += d
    sr_list.append(("RSI", f"{rsi_l:.0f}", d))
    mc_l = macd_map.get(tl)
    if mc_l:
        d = 1.5 if mc_l["histogram"] > 0 else -1.5
        ss += d
        sr_list.append(("MACD", "Bullish" if d > 0 else "Bearish", d))
    vw_l = vwap_map.get(tl)
    verdict = "STRONG BUY" if ss >= 5 else ("BUY" if ss >= 3.5 else ("STRONG SELL" if ss <= -5 else ("SELL" if ss <= -3.5 else "NEUTRAL")))
    summary = {"score": round(ss, 2), "verdict": verdict, "indicators": [{"name": r[0], "status": r[1], "weight": r[2]} for r in sr_list], "rsi": rsi_l, "macd": mc_l, "vwap": vw_l}
    return signals, summary


# ---------------------------------------------------------------------------
# Strategy: Scalping (Ultra Short-Term Mean Reversion + Micro Momentum)
# ---------------------------------------------------------------------------
def generate_scalping_signals(candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr):
    n = len(candles)
    if n < 20:
        return [], {}
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    opens = [c["open"] for c in candles]
    volumes = [c.get("volume", 0) for c in candles]

    rsi_map = {r["time"]: r["value"] for r in rsi_data}
    macd_map = {m["time"]: m for m in macd_data}
    vwap_map = {v["time"]: v["value"] for v in vwap_data}
    ema9_map = {e["time"]: e["value"] for e in ema9}
    bb_upper_map, bb_lower_map, bb_mid_map = {}, {}, {}
    for b in bb:
        bb_upper_map[b["time"]] = b["upper"]
        bb_lower_map[b["time"]] = b["lower"]
        bb_mid_map[b["time"]] = b["middle"]

    # Fast EMA5
    k5 = 2.0 / 6
    ema5 = [0.0] * n
    ema5[4] = sum(closes[:5]) / 5 if n >= 5 else closes[0]
    for i in range(5, n):
        ema5[i] = closes[i] * k5 + ema5[i - 1] * (1 - k5)

    lookback = 15
    signals = []
    last_signal_type = None

    for i in range(lookback, n):
        t = candles[i]["time"]
        close = closes[i]
        high = highs[i]
        low = lows[i]
        opn = opens[i]
        vol = volumes[i]
        score = 0.0
        reasons = []
        t_prev = candles[i - 1]["time"]
        body = abs(close - opn)
        full_range = high - low
        upper_wick = high - max(close, opn)
        lower_wick = min(close, opn) - low

        # BB bounce
        bb_l = bb_lower_map.get(t)
        bb_u = bb_upper_map.get(t)
        if bb_l and bb_u:
            if low <= bb_l and close > opn:
                score += 2.0
                reasons.append("BB Lower Bounce (Buy)")
            elif high >= bb_u and close < opn:
                score -= 2.0
                reasons.append("BB Upper Bounce (Sell)")

        # RSI extreme reversal
        rsi_val = rsi_map.get(t, 50)
        rsi_prev = rsi_map.get(t_prev, 50)
        if rsi_prev < 25 and rsi_val > rsi_prev:
            score += 2.0
            reasons.append(f"RSI Oversold Reversal ({rsi_val:.0f})")
        elif rsi_prev > 75 and rsi_val < rsi_prev:
            score -= 2.0
            reasons.append(f"RSI Overbought Reversal ({rsi_val:.0f})")

        # VWAP mean reversion
        vw = vwap_map.get(t)
        if vw and vw > 0:
            dev = (close - vw) / vw
            prev_dev = (closes[i - 1] - vw) / vw
            if prev_dev < -0.003 and dev > prev_dev:
                score += 2.0
                reasons.append(f"VWAP Mean Revert UP ({dev:.3f})")
            elif prev_dev > 0.003 and dev < prev_dev:
                score -= 2.0
                reasons.append(f"VWAP Mean Revert DOWN ({dev:.3f})")

        # Micro EMA cross: EMA5 vs EMA9
        e9 = ema9_map.get(t)
        e9p = ema9_map.get(t_prev)
        if e9 and e9p and i >= 5:
            if ema5[i - 1] <= e9p and ema5[i] > e9:
                score += 1.5
                reasons.append("EMA5/9 Bull Cross")
            elif ema5[i - 1] >= e9p and ema5[i] < e9:
                score -= 1.5
                reasons.append("EMA5/9 Bear Cross")

        # Volume spike on reversal
        vol_avg = sum(volumes[i - lookback:i]) / lookback if lookback > 0 else 1
        if vol_avg > 0 and vol > vol_avg * 1.8:
            if closes[i - 1] < opens[i - 1] and close > opn:
                score += 1.5
                reasons.append(f"Vol Spike + Reversal UP ({vol / vol_avg:.1f}x)")
            elif closes[i - 1] > opens[i - 1] and close < opn:
                score -= 1.5
                reasons.append(f"Vol Spike + Reversal DOWN ({vol / vol_avg:.1f}x)")

        # Wick rejection
        if full_range > 0:
            if lower_wick > body * 2 and lower_wick > full_range * 0.5:
                score += 1.5
                reasons.append("Pin Bar Rejection (Bull)")
            elif upper_wick > body * 2 and upper_wick > full_range * 0.5:
                score -= 1.5
                reasons.append("Pin Bar Rejection (Bear)")

        # MACD zero-line cross
        mc = macd_map.get(t)
        mc_p = macd_map.get(t_prev)
        if mc and mc_p:
            if mc_p["macd"] <= 0 and mc["macd"] > 0:
                score += 1.5
                reasons.append("MACD Zero Cross UP")
            elif mc_p["macd"] >= 0 and mc["macd"] < 0:
                score -= 1.5
                reasons.append("MACD Zero Cross DOWN")

        # Candle body reversal
        if closes[i - 1] < opens[i - 1] and close > opn and body > abs(closes[i - 1] - opens[i - 1]):
            score += 1.0
            reasons.append("Body Reversal Bull")
        elif closes[i - 1] > opens[i - 1] and close < opn and body > abs(closes[i - 1] - opens[i - 1]):
            score -= 1.0
            reasons.append("Body Reversal Bear")

        # Tight range breakout NR4
        if i >= 4:
            ranges = [highs[i - j] - lows[i - j] for j in range(1, 5)]
            if full_range > max(ranges) and body > full_range * 0.5:
                d = 1.0 if close > opn else -1.0
                score += d
                reasons.append("NR4 Breakout")

        score = round(score, 2)
        if score >= 5.0 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "STRONG_BUY", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "BUY"
        elif score >= 3.5 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "BUY", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "BUY"
        elif score <= -3.5 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "STRONG_SELL", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "SELL"
        elif score <= -3.5 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "SELL", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "SELL"

    li = n - 1
    tl = candles[li]["time"]
    ss = 0
    sr_list = []
    rsi_l = rsi_map.get(tl, 50)
    d = 1.5 if rsi_l < 30 else (-1.5 if rsi_l > 70 else 0)
    ss += d
    sr_list.append(("RSI", f"{rsi_l:.0f}", d))
    vw_l = vwap_map.get(tl)
    if vw_l and vw_l > 0:
        dv = (closes[-1] - vw_l) / vw_l
        d = 1.5 if dv < -0.003 else (-1.5 if dv > 0.003 else 0)
        ss += d
        sr_list.append(("VWAP Dev", f"{dv:.3f}", d))
    mc_l = macd_map.get(tl)
    verdict = "STRONG BUY" if ss >= 5 else ("BUY" if ss >= 3.5 else ("STRONG SELL" if ss <= -5 else ("SELL" if ss <= -3.5 else "NEUTRAL")))
    summary = {"score": round(ss, 2), "verdict": verdict, "indicators": [{"name": r[0], "status": r[1], "weight": r[2]} for r in sr_list], "rsi": rsi_l, "macd": mc_l, "vwap": vw_l}
    return signals, summary


# ---------------------------------------------------------------------------
# Strategy: Smart Money (Institutional Footprint Detection)
# ---------------------------------------------------------------------------
def generate_smartmoney_signals(candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr):
    n = len(candles)
    if n < 30:
        return [], {}
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    opens = [c["open"] for c in candles]
    volumes = [c.get("volume", 0) for c in candles]

    rsi_map = {r["time"]: r["value"] for r in rsi_data}
    macd_map = {m["time"]: m for m in macd_data}
    vwap_map = {v["time"]: v["value"] for v in vwap_data}
    ema9_map = {e["time"]: e["value"] for e in ema9}
    ema21_map = {e["time"]: e["value"] for e in ema21}

    # Swing points for structure
    swing_lb = 5
    swing_highs = []
    swing_lows = []
    for i in range(swing_lb, n - min(swing_lb, n - 1)):
        end = min(i + swing_lb + 1, n)
        if all(highs[i] >= highs[i - j] for j in range(1, swing_lb + 1)) and \
           all(highs[i] >= highs[j] for j in range(i + 1, end)):
            swing_highs.append((i, highs[i]))
        if all(lows[i] <= lows[i - j] for j in range(1, swing_lb + 1)) and \
           all(lows[i] <= lows[j] for j in range(i + 1, end)):
            swing_lows.append((i, lows[i]))

    lookback = 20
    signals = []
    last_signal_type = None

    for i in range(lookback, n):
        t = candles[i]["time"]
        close = closes[i]
        high = highs[i]
        low = lows[i]
        opn = opens[i]
        vol = volumes[i]
        score = 0.0
        reasons = []
        body = abs(close - opn)
        full_range = high - low
        vol_avg = sum(volumes[i - lookback:i]) / lookback if lookback > 0 else 1

        # Order Block
        if i >= 3:
            if closes[i - 2] < opens[i - 2] and closes[i - 1] > opens[i - 1] and close > opn:
                disp = close - opens[i - 2]
                avg_rng = sum(highs[j] - lows[j] for j in range(i - 10, i)) / 10
                if avg_rng > 0 and disp > avg_rng * 2:
                    score += 2.0
                    reasons.append("Bullish Order Block")
            if closes[i - 2] > opens[i - 2] and closes[i - 1] < opens[i - 1] and close < opn:
                disp = opens[i - 2] - close
                avg_rng = sum(highs[j] - lows[j] for j in range(i - 10, i)) / 10
                if avg_rng > 0 and disp > avg_rng * 2:
                    score -= 2.0
                    reasons.append("Bearish Order Block")

        # Fair Value Gap
        if i >= 2:
            if lows[i] > highs[i - 2] and close > opn:
                score += 2.0
                reasons.append(f"Bullish FVG ({lows[i] - highs[i - 2]:.0f} pts)")
            elif highs[i] < lows[i - 2] and close < opn:
                score -= 2.0
                reasons.append(f"Bearish FVG ({lows[i - 2] - highs[i]:.0f} pts)")

        # Liquidity Sweep + Reversal
        recent_sh = [p for idx, p in swing_highs if i - 15 <= idx < i]
        recent_sl = [p for idx, p in swing_lows if i - 15 <= idx < i]
        if recent_sl and low < min(recent_sl) and close > opn:
            score += 2.0
            reasons.append("Liquidity Sweep Below + Reversal")
        if recent_sh and high > max(recent_sh) and close < opn:
            score -= 2.0
            reasons.append("Liquidity Sweep Above + Reversal")

        # Displacement candle
        if full_range > 0 and vol_avg > 0:
            body_ratio = body / full_range
            vol_ratio = vol / vol_avg
            if body_ratio > 0.75 and vol_ratio > 1.5:
                d = 1.5 if close > opn else -1.5
                score += d
                reasons.append(f"Displacement {'UP' if d > 0 else 'DOWN'}")

        # Break of Structure
        if recent_sh and close > max(recent_sh) and close > opn:
            score += 1.5
            reasons.append("BOS Bullish")
        if recent_sl and close < min(recent_sl) and close < opn:
            score -= 1.5
            reasons.append("BOS Bearish")

        # Change of Character
        if len(recent_sh) >= 2 and len(recent_sl) >= 2:
            if recent_sh[-2] < recent_sh[-1] and close < min(recent_sl):
                score -= 1.5
                reasons.append("CHoCH Bearish")
            if recent_sl[-2] > recent_sl[-1] and close > max(recent_sh):
                score += 1.5
                reasons.append("CHoCH Bullish")

        # VWAP institutional level
        vw = vwap_map.get(t)
        if vw:
            if close > vw and low <= vw * 1.001:
                score += 1.0
                reasons.append("VWAP Institutional Hold")
            elif close < vw and high >= vw * 0.999:
                score -= 1.0
                reasons.append("VWAP Institutional Reject")

        # Volume imbalance
        if full_range > 0 and vol > 0:
            buy_pct = (close - low) / full_range
            if buy_pct > 0.7 and vol > vol_avg * 1.3:
                score += 1.5
                reasons.append("Buy Imbalance")
            elif buy_pct < 0.3 and vol > vol_avg * 1.3:
                score -= 1.5
                reasons.append("Sell Imbalance")

        # EMA reclaim after sweep
        e9 = ema9_map.get(t)
        e21 = ema21_map.get(t)
        if e9 and e21:
            if closes[i - 1] < e21 and close > e9:
                score += 1.0
                reasons.append("EMA Reclaim After Sweep")
            elif closes[i - 1] > e21 and close < e9:
                score -= 1.0
                reasons.append("EMA Lost After Sweep")

        score = round(score, 2)
        if score >= 5.0 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "STRONG_BUY", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "BUY"
        elif score >= 3.5 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "BUY", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "BUY"
        elif score <= -3.5 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "STRONG_SELL", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "SELL"
        elif score <= -3.5 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "SELL", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "SELL"

    li = n - 1
    tl = candles[li]["time"]
    ss = 0
    sr_list = []
    rsi_l = rsi_map.get(tl, 50)
    mc_l = macd_map.get(tl)
    vw_l = vwap_map.get(tl)
    rng = highs[li] - lows[li]
    bp = (closes[-1] - lows[-1]) / rng if rng > 0 else 0.5
    d = 1.5 if bp > 0.65 else (-1.5 if bp < 0.35 else 0)
    ss += d
    sr_list.append(("Buy Pressure", f"{bp:.0%}", d))
    r_sh = [p for idx, p in swing_highs if li - 20 <= idx < li]
    r_sl = [p for idx, p in swing_lows if li - 20 <= idx < li]
    if r_sh and closes[-1] > max(r_sh):
        d = 2.0
    elif r_sl and closes[-1] < min(r_sl):
        d = -2.0
    else:
        d = 0
    ss += d
    sr_list.append(("Structure", "BOS Bull" if d > 0 else ("BOS Bear" if d < 0 else "Range"), d))
    verdict = "STRONG BUY" if ss >= 5 else ("BUY" if ss >= 3.5 else ("STRONG SELL" if ss <= -5 else ("SELL" if ss <= -3.5 else "NEUTRAL")))
    summary = {"score": round(ss, 2), "verdict": verdict, "indicators": [{"name": r[0], "status": r[1], "weight": r[2]} for r in sr_list], "rsi": rsi_l, "macd": mc_l, "vwap": vw_l}
    return signals, summary


# ---------------------------------------------------------------------------
# Strategy: Quant (Statistical / Mathematical Model)
# ---------------------------------------------------------------------------
def generate_quant_signals(candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr):
    n = len(candles)
    if n < 50:
        return [], {}
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    opens = [c["open"] for c in candles]
    volumes = [c.get("volume", 0) for c in candles]

    rsi_map = {r["time"]: r["value"] for r in rsi_data}
    macd_map = {m["time"]: m for m in macd_data}
    vwap_map = {v["time"]: v["value"] for v in vwap_data}
    ema21_map = {e["time"]: e["value"] for e in ema21}
    bb_upper_map, bb_lower_map = {}, {}
    for b in bb:
        bb_upper_map[b["time"]] = b["upper"]
        bb_lower_map[b["time"]] = b["lower"]

    lookback = 20
    signals = []
    last_signal_type = None

    for i in range(50, n):
        t = candles[i]["time"]
        close = closes[i]
        score = 0.0
        reasons = []
        t_prev = candles[i - 1]["time"]

        # Z-Score
        seg = closes[i - lookback:i + 1]
        mu = sum(seg) / len(seg)
        std = (sum((x - mu) ** 2 for x in seg) / len(seg)) ** 0.5
        z = (close - mu) / std if std > 0 else 0
        if z < -2.0:
            score += 2.0
            reasons.append(f"Z-Score Extreme Low ({z:.2f})")
        elif z < -1.0:
            score += 1.0
            reasons.append(f"Z-Score Low ({z:.2f})")
        elif z > 2.0:
            score -= 2.0
            reasons.append(f"Z-Score Extreme High ({z:.2f})")
        elif z > 1.0:
            score -= 1.0
            reasons.append(f"Z-Score High ({z:.2f})")

        # Linear regression deviation
        xs = list(range(lookback + 1))
        ys = seg
        x_mean = sum(xs) / len(xs)
        y_mean = sum(ys) / len(ys)
        num = sum((xs[j] - x_mean) * (ys[j] - y_mean) for j in range(len(xs)))
        den = sum((xs[j] - x_mean) ** 2 for j in range(len(xs)))
        slope = num / den if den > 0 else 0
        intercept = y_mean - slope * x_mean
        reg_val = slope * lookback + intercept
        reg_dev = (close - reg_val) / std if std > 0 else 0
        if reg_dev < -1.5:
            score += 2.0
            reasons.append(f"Below Regression ({reg_dev:.2f}\u03c3)")
        elif reg_dev > 1.5:
            score -= 2.0
            reasons.append(f"Above Regression ({reg_dev:.2f}\u03c3)")
        elif slope > 0 and reg_dev > -0.5:
            score += 0.5
            reasons.append("Regression Uptrend")
        elif slope < 0 and reg_dev < 0.5:
            score -= 0.5
            reasons.append("Regression Downtrend")

        # Bollinger %B
        bb_u = bb_upper_map.get(t)
        bb_l = bb_lower_map.get(t)
        if bb_u and bb_l and bb_u > bb_l:
            pct_b = (close - bb_l) / (bb_u - bb_l)
            if pct_b < 0.05:
                score += 1.5
                reasons.append(f"%B Oversold ({pct_b:.2f})")
            elif pct_b > 0.95:
                score -= 1.5
                reasons.append(f"%B Overbought ({pct_b:.2f})")

        # Stochastic RSI
        rsi_val = rsi_map.get(t, 50)
        rsi_window = [rsi_map.get(candles[j]["time"], 50) for j in range(max(0, i - 14), i + 1)]
        if len(rsi_window) >= 2:
            rsi_min = min(rsi_window)
            rsi_max = max(rsi_window)
            stoch_rsi = (rsi_val - rsi_min) / (rsi_max - rsi_min) * 100 if rsi_max > rsi_min else 50
            stoch_prev_w = [rsi_map.get(candles[j]["time"], 50) for j in range(max(0, i - 15), i)]
            stoch_prev = 50
            if len(stoch_prev_w) >= 2:
                pm, px = min(stoch_prev_w), max(stoch_prev_w)
                rsi_p = rsi_map.get(t_prev, 50)
                stoch_prev = (rsi_p - pm) / (px - pm) * 100 if px > pm else 50
            if stoch_prev < 20 and stoch_rsi > 20:
                score += 2.0
                reasons.append(f"StochRSI Cross Up ({stoch_rsi:.0f})")
            elif stoch_prev > 80 and stoch_rsi < 80:
                score -= 2.0
                reasons.append(f"StochRSI Cross Down ({stoch_rsi:.0f})")

        # Keltner Channel position
        atr_14 = []
        for j in range(max(1, i - 14), i + 1):
            tr = max(highs[j] - lows[j], abs(highs[j] - closes[j - 1]), abs(lows[j] - closes[j - 1]))
            atr_14.append(tr)
        avg_atr = sum(atr_14) / len(atr_14) if atr_14 else 0
        e21 = ema21_map.get(t)
        if e21 and avg_atr > 0:
            kc_upper = e21 + 2 * avg_atr
            kc_lower = e21 - 2 * avg_atr
            if close < kc_lower:
                score += 1.5
                reasons.append("Below Keltner Lower")
            elif close > kc_upper:
                score -= 1.5
                reasons.append("Above Keltner Upper")

        # Hurst exponent proxy
        returns = [closes[j] / closes[j - 1] - 1 for j in range(i - 20, i + 1) if closes[j - 1] > 0]
        if len(returns) >= 10:
            r_mean = sum(returns) / len(returns)
            cum_dev = []
            cum = 0
            for r in returns:
                cum += r - r_mean
                cum_dev.append(cum)
            r_std = (sum((r - r_mean) ** 2 for r in returns) / len(returns)) ** 0.5
            rs = (max(cum_dev) - min(cum_dev)) / max(0.0001, r_std)
            h_proxy = math.log(max(1, rs)) / math.log(len(returns)) if len(returns) > 1 else 0.5
            if h_proxy < 0.4 and z < -1:
                score += 1.5
                reasons.append(f"Mean-Reverting + Oversold (H={h_proxy:.2f})")
            elif h_proxy < 0.4 and z > 1:
                score -= 1.5
                reasons.append(f"Mean-Reverting + Overbought (H={h_proxy:.2f})")
            elif h_proxy > 0.6:
                d = 1.0 if closes[i] > closes[i - 1] else -1.0
                score += d
                reasons.append(f"Trending (H={h_proxy:.2f})")

        # Variance ratio
        if len(returns) >= 10:
            var_1 = sum(r ** 2 for r in returns) / len(returns)
            returns_2 = [closes[j] / closes[j - 2] - 1 for j in range(i - 18, i + 1, 2) if j >= 2 and closes[j - 2] > 0]
            var_2 = sum(r ** 2 for r in returns_2) / len(returns_2) if returns_2 else var_1
            vr = var_2 / (2 * var_1) if var_1 > 0 else 1
            if vr < 0.7 and z < -0.5:
                score += 1.5
                reasons.append(f"VR Mean Revert Buy ({vr:.2f})")
            elif vr < 0.7 and z > 0.5:
                score -= 1.5
                reasons.append(f"VR Mean Revert Sell ({vr:.2f})")

        # Price percentile rank
        window_50 = closes[max(0, i - 50):i + 1]
        rank = sum(1 for p in window_50 if p <= close) / len(window_50) * 100
        if rank < 10:
            score += 1.0
            reasons.append(f"Percentile Low ({rank:.0f}%)")
        elif rank > 90:
            score -= 1.0
            reasons.append(f"Percentile High ({rank:.0f}%)")

        # Return distribution skew
        if len(returns) >= 10:
            r_mean = sum(returns) / len(returns)
            r_std = (sum((r - r_mean) ** 2 for r in returns) / len(returns)) ** 0.5
            if r_std > 0:
                skew = sum((r - r_mean) ** 3 for r in returns) / (len(returns) * r_std ** 3)
                if skew > 0.5:
                    score += 1.0
                    reasons.append(f"Positive Skew ({skew:.2f})")
                elif skew < -0.5:
                    score -= 1.0
                    reasons.append(f"Negative Skew ({skew:.2f})")

        score = round(score, 2)
        if score >= 5.0 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "STRONG_BUY", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "BUY"
        elif score >= 3.5 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "BUY", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "BUY"
        elif score <= -3.5 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "STRONG_SELL", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "SELL"
        elif score <= -3.5 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "SELL", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "SELL"

    li = n - 1
    tl = candles[li]["time"]
    seg = closes[li - lookback:li + 1]
    mu = sum(seg) / len(seg)
    std = (sum((x - mu) ** 2 for x in seg) / len(seg)) ** 0.5
    z_l = (closes[-1] - mu) / std if std > 0 else 0
    ss = 0
    sr_list = []
    d = 2.0 if z_l < -1.5 else (1.0 if z_l < -0.5 else (-2.0 if z_l > 1.5 else (-1.0 if z_l > 0.5 else 0)))
    ss += d
    sr_list.append(("Z-Score", f"{z_l:.2f}", d))
    rsi_l = rsi_map.get(tl, 50)
    d = 1.0 if rsi_l < 35 else (-1.0 if rsi_l > 65 else 0)
    ss += d
    sr_list.append(("RSI", f"{rsi_l:.0f}", d))
    mc_l = macd_map.get(tl)
    vw_l = vwap_map.get(tl)
    verdict = "STRONG BUY" if ss >= 5 else ("BUY" if ss >= 3.5 else ("STRONG SELL" if ss <= -5 else ("SELL" if ss <= -3.5 else "NEUTRAL")))
    summary = {"score": round(ss, 2), "verdict": verdict, "indicators": [{"name": r[0], "status": r[1], "weight": r[2]} for r in sr_list], "rsi": rsi_l, "macd": mc_l, "vwap": vw_l}
    return signals, summary


# ---------------------------------------------------------------------------
# Strategy: Hybrid (Multi-Strategy Consensus Voting)
# ---------------------------------------------------------------------------
def generate_hybrid_signals(candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr):
    n = len(candles)
    if n < 30:
        return [], {}
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    opens = [c["open"] for c in candles]
    volumes = [c.get("volume", 0) for c in candles]

    rsi_map = {r["time"]: r["value"] for r in rsi_data}
    macd_map = {m["time"]: m for m in macd_data}
    vwap_map = {v["time"]: v["value"] for v in vwap_data}
    ema9_map = {e["time"]: e["value"] for e in ema9}
    ema21_map = {e["time"]: e["value"] for e in ema21}
    bb_upper_map, bb_lower_map = {}, {}
    for b in bb:
        bb_upper_map[b["time"]] = b["upper"]
        bb_lower_map[b["time"]] = b["lower"]

    lookback = 20
    signals = []
    last_signal_type = None

    for i in range(lookback, n):
        t = candles[i]["time"]
        close = closes[i]
        high = highs[i]
        low = lows[i]
        opn = opens[i]
        vol = volumes[i]
        t_prev = candles[i - 1]["time"]

        votes = {}
        reasons = []
        vol_avg = sum(volumes[i - lookback:i]) / lookback if lookback > 0 else 1

        # A. Trend vote
        e9 = ema9_map.get(t)
        e21 = ema21_map.get(t)
        vw = vwap_map.get(t)
        if e9 and e21:
            if close > e9 > e21 and (vw is None or close > vw):
                votes["trend"] = 1
                reasons.append("Trend: Bullish")
            elif close < e9 < e21 and (vw is None or close < vw):
                votes["trend"] = -1
                reasons.append("Trend: Bearish")
            else:
                votes["trend"] = 0
        else:
            votes["trend"] = 0

        # B. Mean Reversion vote
        seg = closes[i - lookback:i + 1]
        mu = sum(seg) / len(seg)
        std = (sum((x - mu) ** 2 for x in seg) / len(seg)) ** 0.5
        z = (close - mu) / std if std > 0 else 0
        bb_u = bb_upper_map.get(t)
        bb_l = bb_lower_map.get(t)
        if z < -1.5 or (bb_l and close <= bb_l):
            votes["meanrev"] = 1
            reasons.append(f"MeanRev: Buy (z={z:.2f})")
        elif z > 1.5 or (bb_u and close >= bb_u):
            votes["meanrev"] = -1
            reasons.append(f"MeanRev: Sell (z={z:.2f})")
        else:
            votes["meanrev"] = 0

        # C. Momentum vote
        mc = macd_map.get(t)
        mc_p = macd_map.get(t_prev)
        roc_val = (close - closes[i - 10]) / closes[i - 10] * 100 if i >= 10 and closes[i - 10] > 0 else 0
        mom_vote = 0
        if mc and mc_p:
            if mc["histogram"] > 0 and mc["histogram"] > mc_p["histogram"] and roc_val > 0.3:
                mom_vote = 1
                reasons.append(f"Momentum: Bullish (ROC={roc_val:.2f}%)")
            elif mc["histogram"] < 0 and mc["histogram"] < mc_p["histogram"] and roc_val < -0.3:
                mom_vote = -1
                reasons.append(f"Momentum: Bearish (ROC={roc_val:.2f}%)")
        votes["momentum"] = mom_vote

        # D. Volume vote
        full_range = high - low
        if vol_avg > 0 and vol > 0 and full_range > 0:
            buy_pct = (close - low) / full_range
            delta = vol * (buy_pct - 0.5) * 2
            if delta > 0 and vol > vol_avg * 1.3:
                votes["volume"] = 1
                reasons.append(f"Volume: Buy Pressure ({vol / vol_avg:.1f}x)")
            elif delta < 0 and vol > vol_avg * 1.3:
                votes["volume"] = -1
                reasons.append(f"Volume: Sell Pressure ({vol / vol_avg:.1f}x)")
            else:
                votes["volume"] = 0
        else:
            votes["volume"] = 0

        # E. Price Action vote
        body = abs(close - opn)
        pa_vote = 0
        if full_range > 0:
            lower_wick = min(close, opn) - low
            upper_wick = high - max(close, opn)
            if close > opn and body > abs(closes[i - 1] - opens[i - 1]) * 1.2 and closes[i - 1] < opens[i - 1]:
                pa_vote = 1
                reasons.append("PA: Bullish Engulfing")
            elif close < opn and body > abs(closes[i - 1] - opens[i - 1]) * 1.2 and closes[i - 1] > opens[i - 1]:
                pa_vote = -1
                reasons.append("PA: Bearish Engulfing")
            elif lower_wick > body * 2:
                pa_vote = 1
                reasons.append("PA: Hammer")
            elif upper_wick > body * 2:
                pa_vote = -1
                reasons.append("PA: Shooting Star")
        votes["priceaction"] = pa_vote

        # Count consensus
        bull_count = sum(1 for v in votes.values() if v == 1)
        bear_count = sum(1 for v in votes.values() if v == -1)
        total_votes = len(votes)

        score = 0.0
        if bull_count >= 4:
            score = bull_count * 2.0
            reasons.insert(0, f"CONSENSUS: {bull_count}/{total_votes} Bullish")
        elif bull_count >= 3:
            score = bull_count * 1.8
            reasons.insert(0, f"CONSENSUS: {bull_count}/{total_votes} Bullish")
        elif bear_count >= 4:
            score = -bear_count * 2.0
            reasons.insert(0, f"CONSENSUS: {bear_count}/{total_votes} Bearish")
        elif bear_count >= 3:
            score = -bear_count * 1.8
            reasons.insert(0, f"CONSENSUS: {bear_count}/{total_votes} Bearish")

        score = round(score, 2)
        if score >= 5.0 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "STRONG_BUY", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "BUY"
        elif score >= 3.5 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "BUY", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "BUY"
        elif score <= -3.5 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "STRONG_SELL", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "SELL"
        elif score <= -3.5 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "SELL", "score": score, "reasons": reasons, "price": close})
            last_signal_type = "SELL"

    li = n - 1
    tl = candles[li]["time"]
    ss = 0
    sr_list = []
    e9 = ema9_map.get(tl)
    e21 = ema21_map.get(tl)
    trend_v = 1 if (e9 and e21 and closes[-1] > e9 > e21) else (-1 if (e9 and e21 and closes[-1] < e9 < e21) else 0)
    seg = closes[li - lookback:li + 1]
    mu = sum(seg) / len(seg)
    std = (sum((x - mu) ** 2 for x in seg) / len(seg)) ** 0.5
    z_v = (closes[-1] - mu) / std if std > 0 else 0
    mr_v = 1 if z_v < -1.5 else (-1 if z_v > 1.5 else 0)
    mc_l = macd_map.get(tl)
    mom_v = 1 if (mc_l and mc_l["histogram"] > 0) else (-1 if (mc_l and mc_l["histogram"] < 0) else 0)
    bc = sum(1 for v in [trend_v, mr_v, mom_v] if v == 1)
    sc = sum(1 for v in [trend_v, mr_v, mom_v] if v == -1)
    ss = bc * 2 - sc * 2
    sr_list.append(("Trend", "Bull" if trend_v > 0 else ("Bear" if trend_v < 0 else "Flat"), trend_v * 2))
    sr_list.append(("MeanRev", f"z={z_v:.2f}", mr_v * 2))
    sr_list.append(("Momentum", "Bull" if mom_v > 0 else ("Bear" if mom_v < 0 else "Flat"), mom_v * 2))
    rsi_l = rsi_map.get(tl, 50)
    vw_l = vwap_map.get(tl)
    verdict = "STRONG BUY" if ss >= 5 else ("BUY" if ss >= 3.5 else ("STRONG SELL" if ss <= -5 else ("SELL" if ss <= -3.5 else "NEUTRAL")))
    summary = {"score": round(ss, 2), "verdict": verdict, "indicators": [{"name": r[0], "status": r[1], "weight": r[2]} for r in sr_list], "rsi": rsi_l, "macd": mc_l, "vwap": vw_l}
    return signals, summary


def generate_statarb_signals(candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr):
    """Statistical Arbitrage strategy — pairs-style mean-reversion using z-score,
    Bollinger %B, RSI divergence, and correlation spread analysis.
    Thresholds: BUY >= 3.5 | STRONG BUY >= 5.0 | SELL <= -3.5 | STRONG SELL <= -5.0
    """
    n = len(candles)
    if n < 40:
        return [], {}
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    opens = [c["open"] for c in candles]
    volumes = [c.get("volume", 0) for c in candles]

    rsi_map = {r["time"]: r["value"] for r in rsi_data}
    macd_map = {m["time"]: m for m in macd_data}
    vwap_map = {v["time"]: v["value"] for v in vwap_data}
    ema9_map = {e["time"]: e["value"] for e in ema9}
    ema21_map = {e["time"]: e["value"] for e in ema21}
    bb_upper_map, bb_lower_map, bb_mid_map = {}, {}, {}
    for b in bb:
        bb_upper_map[b["time"]] = b["upper"]
        bb_lower_map[b["time"]] = b["lower"]
        bb_mid_map[b["time"]] = b.get("middle", (b["upper"] + b["lower"]) / 2)

    lookback = 20
    signals = []
    last_signal_type = None

    for i in range(lookback + 10, n):
        t = candles[i]["time"]
        close = closes[i]
        high = highs[i]
        low = lows[i]
        opn = opens[i]
        vol = volumes[i]

        score = 0.0
        reasons = []
        vol_avg = sum(volumes[i - lookback:i]) / lookback if lookback > 0 else 1

        # 1. Z-Score mean reversion (weight 2.5)
        seg = closes[i - lookback:i + 1]
        mu = sum(seg) / len(seg)
        std = (sum((x - mu) ** 2 for x in seg) / len(seg)) ** 0.5
        z = (close - mu) / std if std > 0 else 0
        if z < -2.0:
            score += 2.5
            reasons.append(f"Z-Score: Deep Oversold (z={z:.2f})")
        elif z < -1.2:
            score += 1.5
            reasons.append(f"Z-Score: Oversold (z={z:.2f})")
        elif z > 2.0:
            score -= 2.5
            reasons.append(f"Z-Score: Deep Overbought (z={z:.2f})")
        elif z > 1.2:
            score -= 1.5
            reasons.append(f"Z-Score: Overbought (z={z:.2f})")

        # 2. Bollinger %B spread (weight 2.0)
        bb_u = bb_upper_map.get(t)
        bb_l = bb_lower_map.get(t)
        if bb_u and bb_l and bb_u > bb_l:
            pctB = (close - bb_l) / (bb_u - bb_l)
            if pctB < 0.05:
                score += 2.0
                reasons.append(f"BB %B: Below lower band ({pctB:.2f})")
            elif pctB < 0.2:
                score += 1.0
                reasons.append(f"BB %B: Near lower band ({pctB:.2f})")
            elif pctB > 0.95:
                score -= 2.0
                reasons.append(f"BB %B: Above upper band ({pctB:.2f})")
            elif pctB > 0.8:
                score -= 1.0
                reasons.append(f"BB %B: Near upper band ({pctB:.2f})")

        # 3. Spread velocity — rate of deviation change (weight 1.5)
        if i >= 5:
            z_prev = 0
            seg_p = closes[i - lookback - 5:i - 5 + 1]
            if len(seg_p) >= lookback:
                mu_p = sum(seg_p) / len(seg_p)
                std_p = (sum((x - mu_p) ** 2 for x in seg_p) / len(seg_p)) ** 0.5
                z_prev = (closes[i - 5] - mu_p) / std_p if std_p > 0 else 0
            dz = z - z_prev
            if dz < -1.0:
                score += 1.5
                reasons.append(f"Spread Velocity: Accelerating down (dz={dz:.2f})")
            elif dz > 1.0:
                score -= 1.5
                reasons.append(f"Spread Velocity: Accelerating up (dz={dz:.2f})")

        # 4. RSI divergence from z-score (weight 1.5)
        rsi_val = rsi_map.get(t, 50)
        if z < -1.0 and rsi_val > 40:
            score += 1.5
            reasons.append(f"RSI Divergence: Price low but RSI stable ({rsi_val:.0f})")
        elif z > 1.0 and rsi_val < 60:
            score -= 1.5
            reasons.append(f"RSI Divergence: Price high but RSI weak ({rsi_val:.0f})")

        # 5. EMA spread z-score (weight 1.5)
        e9 = ema9_map.get(t)
        e21 = ema21_map.get(t)
        if e9 and e21 and e21 > 0:
            ema_spread = (e9 - e21) / e21 * 100
            ema_spreads = []
            for j in range(max(0, i - lookback), i):
                _e9 = ema9_map.get(candles[j]["time"])
                _e21 = ema21_map.get(candles[j]["time"])
                if _e9 and _e21 and _e21 > 0:
                    ema_spreads.append((_e9 - _e21) / _e21 * 100)
            if len(ema_spreads) >= 5:
                es_mu = sum(ema_spreads) / len(ema_spreads)
                es_std = (sum((x - es_mu) ** 2 for x in ema_spreads) / len(ema_spreads)) ** 0.5
                es_z = (ema_spread - es_mu) / es_std if es_std > 0 else 0
                if es_z < -1.5:
                    score += 1.5
                    reasons.append(f"EMA Spread: Compressed (z={es_z:.2f})")
                elif es_z > 1.5:
                    score -= 1.5
                    reasons.append(f"EMA Spread: Extended (z={es_z:.2f})")

        # 6. Volume confirmation (weight 1.0)
        if vol_avg > 0 and vol > vol_avg * 1.5:
            if close < opn:
                score += 1.0
                reasons.append(f"Volume: Capitulation selling ({vol / vol_avg:.1f}x)")
            elif close > opn:
                score -= 1.0
                reasons.append(f"Volume: Euphoric buying ({vol / vol_avg:.1f}x)")

        # 7. MACD histogram reversal (weight 1.5)
        mc = macd_map.get(t)
        mc_p = macd_map.get(candles[i - 1]["time"]) if i > 0 else None
        if mc and mc_p:
            if mc["histogram"] > mc_p["histogram"] and mc["histogram"] < 0:
                score += 1.5
                reasons.append("MACD: Histogram reversing up from negative")
            elif mc["histogram"] < mc_p["histogram"] and mc["histogram"] > 0:
                score -= 1.5
                reasons.append("MACD: Histogram reversing down from positive")

        score = round(score, 2)
        if score >= 5.0 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "STRONG_BUY", "score": score, "reasons": reasons, "price": low})
            last_signal_type = "BUY"
        elif score >= 3.5 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "BUY", "score": score, "reasons": reasons, "price": low})
            last_signal_type = "BUY"
        elif score <= -5.0 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "STRONG_SELL", "score": score, "reasons": reasons, "price": high})
            last_signal_type = "SELL"
        elif score <= -3.5 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "SELL", "score": score, "reasons": reasons, "price": high})
            last_signal_type = "SELL"

    # Summary
    li = n - 1
    tl = candles[li]["time"]
    seg = closes[li - lookback:li + 1]
    mu = sum(seg) / len(seg)
    std = (sum((x - mu) ** 2 for x in seg) / len(seg)) ** 0.5
    z_l = (closes[-1] - mu) / std if std > 0 else 0
    rsi_l = rsi_map.get(tl, 50)
    mc_l = macd_map.get(tl)
    vw_l = vwap_map.get(tl)
    sr_list = [
        ("Z-Score", f"{z_l:.2f}", 2.5 if abs(z_l) > 1.2 else 0),
        ("RSI", f"{rsi_l:.0f}", 1.5 if rsi_l < 35 or rsi_l > 65 else 0),
        ("MACD Hist", "Pos" if mc_l and mc_l["histogram"] > 0 else "Neg", 1.5),
    ]
    ss = sum(r[2] for r in sr_list) if z_l < -1.2 else -sum(r[2] for r in sr_list) if z_l > 1.2 else 0
    verdict = "STRONG BUY" if ss >= 5 else ("BUY" if ss >= 3.5 else ("STRONG SELL" if ss <= -5 else ("SELL" if ss <= -3.5 else "NEUTRAL")))
    summary = {"score": round(ss, 2), "verdict": verdict, "indicators": [{"name": r[0], "status": r[1], "weight": r[2]} for r in sr_list], "rsi": rsi_l, "macd": mc_l, "vwap": vw_l}
    return signals, summary


def generate_institution_signals(candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr):
    """Institutional Algo — detects institutional accumulation/distribution patterns
    using volume analysis, order block detection, VWAP anchoring, and dark pool footprints.
    Thresholds: BUY >= 3.5 | STRONG BUY >= 5.0 | SELL <= -3.5 | STRONG SELL <= -5.0
    """
    n = len(candles)
    if n < 40:
        return [], {}
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    opens = [c["open"] for c in candles]
    volumes = [c.get("volume", 0) for c in candles]

    rsi_map = {r["time"]: r["value"] for r in rsi_data}
    macd_map = {m["time"]: m for m in macd_data}
    vwap_map = {v["time"]: v["value"] for v in vwap_data}
    ema9_map = {e["time"]: e["value"] for e in ema9}
    ema21_map = {e["time"]: e["value"] for e in ema21}
    bb_upper_map, bb_lower_map = {}, {}
    for b in bb:
        bb_upper_map[b["time"]] = b["upper"]
        bb_lower_map[b["time"]] = b["lower"]

    lookback = 20
    signals = []
    last_signal_type = None

    # Pre-compute OBV
    obv = [0.0] * n
    for j in range(1, n):
        if closes[j] > closes[j - 1]:
            obv[j] = obv[j - 1] + volumes[j]
        elif closes[j] < closes[j - 1]:
            obv[j] = obv[j - 1] - volumes[j]
        else:
            obv[j] = obv[j - 1]

    for i in range(lookback + 5, n):
        t = candles[i]["time"]
        close = closes[i]
        high = highs[i]
        low = lows[i]
        opn = opens[i]
        vol = volumes[i]

        score = 0.0
        reasons = []
        vol_avg = sum(volumes[i - lookback:i]) / lookback if lookback > 0 else 1

        # 1. Institutional volume detection (weight 2.5)
        # Large volume + small body = absorption (institutions accumulating/distributing)
        body = abs(close - opn)
        full_range = high - low
        if full_range > 0 and vol_avg > 0:
            body_ratio = body / full_range
            vol_ratio = vol / vol_avg
            if vol_ratio > 2.0 and body_ratio < 0.3:
                # Absorption candle — direction from wick analysis
                lower_wick = min(close, opn) - low
                upper_wick = high - max(close, opn)
                if lower_wick > upper_wick:
                    score += 2.5
                    reasons.append(f"Institutional Absorption: Buy ({vol_ratio:.1f}x vol, {body_ratio:.1%} body)")
                else:
                    score -= 2.5
                    reasons.append(f"Institutional Distribution: Sell ({vol_ratio:.1f}x vol, {body_ratio:.1%} body)")
            elif vol_ratio > 2.5 and close > opn:
                score += 1.5
                reasons.append(f"Aggressive Institutional Buying ({vol_ratio:.1f}x)")
            elif vol_ratio > 2.5 and close < opn:
                score -= 1.5
                reasons.append(f"Aggressive Institutional Selling ({vol_ratio:.1f}x)")

        # 2. Order Block detection (weight 2.0)
        # Last opposite candle before impulsive move
        if i >= 3:
            move = closes[i] - closes[i - 3]
            atr_seg = [highs[j] - lows[j] for j in range(i - lookback, i)]
            avg_atr = sum(atr_seg) / len(atr_seg) if atr_seg else 1
            if avg_atr > 0 and abs(move) > avg_atr * 2:
                # Strong impulsive move — check for order block
                if move > 0 and closes[i - 3] < opens[i - 3]:
                    score += 2.0
                    reasons.append("Order Block: Bullish (bearish candle before rally)")
                elif move < 0 and closes[i - 3] > opens[i - 3]:
                    score -= 2.0
                    reasons.append("Order Block: Bearish (bullish candle before drop)")

        # 3. VWAP institutional anchoring (weight 2.0)
        vw = vwap_map.get(t)
        if vw and vw > 0:
            vwap_dev = (close - vw) / vw * 100
            if vwap_dev < -0.5 and vol > vol_avg * 1.5 and close > opn:
                score += 2.0
                reasons.append(f"VWAP: Institutional buy below VWAP ({vwap_dev:.2f}%)")
            elif vwap_dev > 0.5 and vol > vol_avg * 1.5 and close < opn:
                score -= 2.0
                reasons.append(f"VWAP: Institutional sell above VWAP ({vwap_dev:.2f}%)")

        # 4. OBV divergence (weight 1.5)
        if i >= 10:
            price_change = closes[i] - closes[i - 10]
            obv_change = obv[i] - obv[i - 10]
            if price_change < 0 and obv_change > 0:
                score += 1.5
                reasons.append("OBV Divergence: Hidden accumulation")
            elif price_change > 0 and obv_change < 0:
                score -= 1.5
                reasons.append("OBV Divergence: Hidden distribution")

        # 5. Dark pool footprint — high volume at same price level (weight 1.5)
        if i >= 5:
            price_cluster = 0
            for j in range(i - 5, i):
                if abs(closes[j] - close) / close < 0.002 and volumes[j] > vol_avg * 1.3:
                    price_cluster += 1
            if price_cluster >= 3:
                if close > opens[i]:
                    score += 1.5
                    reasons.append(f"Dark Pool: Repeated institutional interest at {close:.2f}")
                else:
                    score -= 1.5
                    reasons.append(f"Dark Pool: Distribution at {close:.2f}")

        # 6. EMA trend alignment filter (weight 1.0)
        e9 = ema9_map.get(t)
        e21 = ema21_map.get(t)
        if e9 and e21:
            if close > e9 > e21:
                score += 1.0
                reasons.append("Trend: Bullish alignment")
            elif close < e9 < e21:
                score -= 1.0
                reasons.append("Trend: Bearish alignment")

        # 7. RSI + Volume confirmation (weight 1.5)
        rsi_val = rsi_map.get(t, 50)
        if rsi_val < 30 and vol > vol_avg * 1.5:
            score += 1.5
            reasons.append(f"RSI: Oversold with volume ({rsi_val:.0f})")
        elif rsi_val > 70 and vol > vol_avg * 1.5:
            score -= 1.5
            reasons.append(f"RSI: Overbought with volume ({rsi_val:.0f})")

        # 8. S/R level reaction with volume (weight 1.0)
        for lvl in sr:
            if abs(close - lvl["price"]) / close < 0.003:
                if lvl["type"] == "support" and close > opn and vol > vol_avg:
                    score += 1.0
                    reasons.append(f"S/R: Institutional support hold at {lvl['price']:.2f}")
                elif lvl["type"] == "resistance" and close < opn and vol > vol_avg:
                    score -= 1.0
                    reasons.append(f"S/R: Institutional rejection at {lvl['price']:.2f}")
                break

        score = round(score, 2)
        if score >= 5.0 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "STRONG_BUY", "score": score, "reasons": reasons, "price": low})
            last_signal_type = "BUY"
        elif score >= 3.5 and last_signal_type != "BUY":
            signals.append({"time": t, "type": "BUY", "score": score, "reasons": reasons, "price": low})
            last_signal_type = "BUY"
        elif score <= -5.0 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "STRONG_SELL", "score": score, "reasons": reasons, "price": high})
            last_signal_type = "SELL"
        elif score <= -3.5 and last_signal_type != "SELL":
            signals.append({"time": t, "type": "SELL", "score": score, "reasons": reasons, "price": high})
            last_signal_type = "SELL"

    # Summary
    li = n - 1
    tl = candles[li]["time"]
    rsi_l = rsi_map.get(tl, 50)
    mc_l = macd_map.get(tl)
    vw_l = vwap_map.get(tl)
    vol_ratio_l = volumes[-1] / (sum(volumes[-lookback:]) / lookback) if sum(volumes[-lookback:]) > 0 else 1
    obv_trend = "Rising" if obv[-1] > obv[-10] else "Falling"
    sr_list = [
        ("Volume Ratio", f"{vol_ratio_l:.1f}x", 2.5 if vol_ratio_l > 2 else 0),
        ("OBV", obv_trend, 1.5 if obv_trend == "Rising" else -1.5),
        ("RSI", f"{rsi_l:.0f}", 1.5 if rsi_l < 35 else (-1.5 if rsi_l > 65 else 0)),
    ]
    ss = sum(r[2] for r in sr_list)
    verdict = "STRONG BUY" if ss >= 5 else ("BUY" if ss >= 3.5 else ("STRONG SELL" if ss <= -5 else ("SELL" if ss <= -3.5 else "NEUTRAL")))
    summary = {"score": round(ss, 2), "verdict": verdict, "indicators": [{"name": r[0], "status": r[1], "weight": r[2]} for r in sr_list], "rsi": rsi_l, "macd": mc_l, "vwap": vw_l}
    return signals, summary


INTERVAL_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900,
    "1h": 3600, "1d": 86400, "1w": 604800, "1mo": 2592000,
}


def predict_next_candles(candles, interval="5m", n_predict=5):
    """Predict next n candles using Gradient Boosting on engineered features.

    Features per candle:
      - Lagged returns (close-to-close % change) for 1..10 bars
      - Lagged body ratio (body / range)
      - Lagged upper/lower wick ratios
      - Rolling mean & std of returns (5, 10, 20 bar)
      - RSI-like momentum (avg up / avg down over 14 bars)
      - High-Low range as % of close
      - Volume change ratio

    Trains 4 separate GBR models (open, high, low, close offsets from
    previous close) and predicts iteratively.

    Returns:
        list[dict]: Predicted candle dicts with time, open, high, low, close.
    """
    n = len(candles)
    if n < 50:
        return []

    closes = np.array([c["close"] for c in candles], dtype=np.float64)
    opens = np.array([c["open"] for c in candles], dtype=np.float64)
    highs = np.array([c["high"] for c in candles], dtype=np.float64)
    lows = np.array([c["low"] for c in candles], dtype=np.float64)
    volumes = np.array([c.get("volume", 0) for c in candles], dtype=np.float64)

    # Returns
    returns = np.zeros(n)
    returns[1:] = (closes[1:] - closes[:-1]) / np.where(closes[:-1] == 0, 1, closes[:-1])

    # Feature engineering
    lookback = 20
    feature_start = lookback
    X, Y_open, Y_high, Y_low, Y_close = [], [], [], [], []

    for i in range(feature_start, n):
        feat = []
        # Lagged returns (1..10)
        for lag in range(1, 11):
            feat.append(returns[i - lag] if i - lag >= 0 else 0)

        # Body ratio and wick ratios
        rng = highs[i - 1] - lows[i - 1]
        body = abs(closes[i - 1] - opens[i - 1])
        feat.append(body / rng if rng > 0 else 0)
        feat.append((highs[i - 1] - max(opens[i - 1], closes[i - 1])) / rng if rng > 0 else 0)
        feat.append((min(opens[i - 1], closes[i - 1]) - lows[i - 1]) / rng if rng > 0 else 0)

        # Rolling stats
        for w in [5, 10, 20]:
            seg = returns[max(0, i - w):i]
            feat.append(float(np.mean(seg)) if len(seg) > 0 else 0)
            feat.append(float(np.std(seg)) if len(seg) > 0 else 0)

        # RSI-like momentum (14 bar)
        rsi_seg = returns[max(0, i - 14):i]
        ups = float(np.mean(rsi_seg[rsi_seg > 0])) if np.any(rsi_seg > 0) else 0
        dns = float(np.mean(np.abs(rsi_seg[rsi_seg < 0]))) if np.any(rsi_seg < 0) else 0
        feat.append(ups / (dns + 1e-10))

        # Range as % of close
        feat.append(rng / closes[i - 1] if closes[i - 1] > 0 else 0)

        # Volume change
        feat.append((volumes[i - 1] - volumes[i - 2]) / (volumes[i - 2] + 1e-10) if i >= 2 else 0)

        X.append(feat)

        # Targets: offsets from previous close (as % of prev close)
        pc = closes[i - 1] if closes[i - 1] > 0 else 1
        Y_open.append((opens[i] - pc) / pc)
        Y_high.append((highs[i] - pc) / pc)
        Y_low.append((lows[i] - pc) / pc)
        Y_close.append((closes[i] - pc) / pc)

    X = np.array(X, dtype=np.float64)
    Y_open = np.array(Y_open, dtype=np.float64)
    Y_high = np.array(Y_high, dtype=np.float64)
    Y_low = np.array(Y_low, dtype=np.float64)
    Y_close = np.array(Y_close, dtype=np.float64)

    # Handle NaN/Inf
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Train 4 GBR models
    gbr_params = dict(n_estimators=100, max_depth=4, learning_rate=0.1,
                      subsample=0.8, random_state=42)
    model_open = GradientBoostingRegressor(**gbr_params).fit(X_scaled, Y_open)
    model_high = GradientBoostingRegressor(**gbr_params).fit(X_scaled, Y_high)
    model_low = GradientBoostingRegressor(**gbr_params).fit(X_scaled, Y_low)
    model_close = GradientBoostingRegressor(**gbr_params).fit(X_scaled, Y_close)

    # Iteratively predict next candles
    interval_sec = INTERVAL_SECONDS.get(interval, 300)
    last_time = candles[-1]["time"]
    predictions = []

    # Working copies of recent data for rolling feature computation
    ext_returns = list(returns)
    ext_closes = list(closes)
    ext_opens = list(opens)
    ext_highs = list(highs)
    ext_lows = list(lows)
    ext_volumes = list(volumes)

    for step in range(n_predict):
        cur_n = len(ext_closes)
        feat = []

        # Lagged returns
        for lag in range(1, 11):
            idx = cur_n - lag
            feat.append(ext_returns[idx] if idx >= 0 else 0)

        # Body/wick ratios of last bar
        rng = ext_highs[-1] - ext_lows[-1]
        body = abs(ext_closes[-1] - ext_opens[-1])
        feat.append(body / rng if rng > 0 else 0)
        feat.append((ext_highs[-1] - max(ext_opens[-1], ext_closes[-1])) / rng if rng > 0 else 0)
        feat.append((min(ext_opens[-1], ext_closes[-1]) - ext_lows[-1]) / rng if rng > 0 else 0)

        # Rolling stats
        for w in [5, 10, 20]:
            seg = ext_returns[max(0, cur_n - w):cur_n]
            feat.append(float(np.mean(seg)) if len(seg) > 0 else 0)
            feat.append(float(np.std(seg)) if len(seg) > 0 else 0)

        # RSI momentum
        rsi_seg = np.array(ext_returns[max(0, cur_n - 14):cur_n])
        ups = float(np.mean(rsi_seg[rsi_seg > 0])) if np.any(rsi_seg > 0) else 0
        dns = float(np.mean(np.abs(rsi_seg[rsi_seg < 0]))) if np.any(rsi_seg < 0) else 0
        feat.append(ups / (dns + 1e-10))

        # Range %
        feat.append(rng / ext_closes[-1] if ext_closes[-1] > 0 else 0)

        # Volume change
        feat.append((ext_volumes[-1] - ext_volumes[-2]) / (ext_volumes[-2] + 1e-10) if len(ext_volumes) >= 2 else 0)

        feat = np.nan_to_num(np.array([feat], dtype=np.float64), nan=0.0, posinf=0.0, neginf=0.0)
        feat_scaled = scaler.transform(feat)

        pc = ext_closes[-1]
        pred_open = round(pc * (1 + float(model_open.predict(feat_scaled)[0])), 2)
        pred_high = round(pc * (1 + float(model_high.predict(feat_scaled)[0])), 2)
        pred_low = round(pc * (1 + float(model_low.predict(feat_scaled)[0])), 2)
        pred_close = round(pc * (1 + float(model_close.predict(feat_scaled)[0])), 2)

        # Enforce high >= max(open,close) and low <= min(open,close)
        pred_high = max(pred_high, pred_open, pred_close)
        pred_low = min(pred_low, pred_open, pred_close)

        pred_time = last_time + interval_sec * (step + 1)

        predictions.append({
            "time": pred_time,
            "open": pred_open,
            "high": pred_high,
            "low": pred_low,
            "close": pred_close,
        })

        # Update rolling arrays for next iteration
        ret = (pred_close - pc) / pc if pc > 0 else 0
        ext_returns.append(ret)
        ext_closes.append(pred_close)
        ext_opens.append(pred_open)
        ext_highs.append(pred_high)
        ext_lows.append(pred_low)
        ext_volumes.append(ext_volumes[-1])  # carry forward volume

    return predictions


def run_backtest(candles, signals, trade_qty=0):
    """Run a historical strategy backtest using the composite signal engine's output.

    Simulates trading with an initial capital of ₹1,00,000. BUY signals enter
    long positions; SELL signals exit. Computes comprehensive TradingView-style
    performance metrics including:

    - Net profit, gross profit/loss, profit factor
    - Win rate, win/loss counts, avg trade P&L, payoff ratio
    - Max drawdown, max consecutive wins/losses
    - Sharpe ratio (annualized, assuming 252 trading days)
    - Expectancy (expected value per trade)
    - Buy & hold comparison return
    - Full trade list with entry/exit times, prices, quantity, P&L

    Supports fixed quantity mode (trade_qty > 0) or auto-sizing from available
    capital (trade_qty = 0, buys max affordable shares per signal). Open
    positions at the end are marked with the last candle price.

    Args:
        candles (list[dict]): OHLCV candle dicts.
        signals (list[dict]): Signal dicts from generate_signals with
            'time', 'signal' ('BUY'/'STRONG BUY'/'SELL'/'STRONG SELL').
        trade_qty (int): Fixed lot size per trade (0 = auto-size from capital).

    Returns:
        dict: Keys 'summary' (performance metrics dict) and 'trades'
            (list of trade dicts with entry/exit details). Empty dict if
            no candles or signals provided.
    """
    if not candles or not signals:
        return {}

    initial_capital = 100000.0
    capital = initial_capital
    position = 0  # 0 = flat, 1 = long
    entry_price = 0
    entry_time = 0
    qty = 0
    fixed_qty = max(0, int(trade_qty))

    trades = []
    equity_curve = []
    peak_equity = initial_capital

    # Build candle lookup
    candle_map = {c["time"]: c for c in candles}
    first_price = candles[0]["close"]
    last_price = candles[-1]["close"]

    for sig in signals:
        t = sig["time"]
        c = candle_map.get(t)
        if not c:
            continue
        price = c["close"]

        if sig["type"] in ("BUY", "STRONG_BUY") and position == 0:
            # Enter long
            qty = fixed_qty if fixed_qty > 0 else int(capital / price)
            if qty <= 0:
                continue
            entry_price = price
            entry_time = t
            position = 1

        elif sig["type"] in ("SELL", "STRONG_SELL") and position == 1:
            # Exit long
            pnl = (price - entry_price) * qty
            pnl_pct = ((price - entry_price) / entry_price) * 100
            capital += pnl
            trades.append({
                "entryTime": entry_time,
                "exitTime": t,
                "entryPrice": round(entry_price, 2),
                "exitPrice": round(price, 2),
                "qty": qty,
                "pnl": round(pnl, 2),
                "pnlPct": round(pnl_pct, 2),
                "capital": round(capital, 2),
            })
            equity_curve.append({"time": t, "value": round(capital, 2)})
            peak_equity = max(peak_equity, capital)
            position = 0
            qty = 0

    # Close open position at last candle price
    if position == 1:
        price = last_price
        pnl = (price - entry_price) * qty
        pnl_pct = ((price - entry_price) / entry_price) * 100
        capital += pnl
        trades.append({
            "entryTime": entry_time,
            "exitTime": candles[-1]["time"],
            "entryPrice": round(entry_price, 2),
            "exitPrice": round(price, 2),
            "qty": qty,
            "pnl": round(pnl, 2),
            "pnlPct": round(pnl_pct, 2),
            "capital": round(capital, 2),
            "open": True,
        })
        equity_curve.append({"time": candles[-1]["time"], "value": round(capital, 2)})
        peak_equity = max(peak_equity, capital)

    if not trades:
        return {"trades": [], "summary": {}}

    # --- Compute strategy metrics ---
    total_trades = len(trades)
    winners = [t for t in trades if t["pnl"] > 0]
    losers = [t for t in trades if t["pnl"] < 0]
    breakeven = [t for t in trades if t["pnl"] == 0]

    gross_profit = sum(t["pnl"] for t in winners) if winners else 0
    gross_loss = abs(sum(t["pnl"] for t in losers)) if losers else 0
    net_profit = capital - initial_capital
    net_profit_pct = (net_profit / initial_capital) * 100

    win_rate = (len(winners) / total_trades * 100) if total_trades else 0
    loss_rate = (len(losers) / total_trades * 100) if total_trades else 0

    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    avg_trade = net_profit / total_trades if total_trades else 0
    avg_win = gross_profit / len(winners) if winners else 0
    avg_loss = gross_loss / len(losers) if losers else 0

    largest_win = max((t["pnl"] for t in winners), default=0)
    largest_loss = min((t["pnl"] for t in losers), default=0)

    # Max consecutive wins/losses
    max_consec_wins = 0
    max_consec_losses = 0
    cw = 0
    cl = 0
    for t in trades:
        if t["pnl"] > 0:
            cw += 1
            cl = 0
            max_consec_wins = max(max_consec_wins, cw)
        elif t["pnl"] < 0:
            cl += 1
            cw = 0
            max_consec_losses = max(max_consec_losses, cl)
        else:
            cw = 0
            cl = 0

    # Max drawdown
    peak = initial_capital
    max_dd = 0
    max_dd_pct = 0
    running_cap = initial_capital
    for t in trades:
        running_cap = t["capital"]
        peak = max(peak, running_cap)
        dd = peak - running_cap
        dd_pct = (dd / peak * 100) if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd_pct

    # Payoff ratio (avg win / avg loss)
    payoff_ratio = (avg_win / avg_loss) if avg_loss > 0 else float("inf")

    # Expectancy = (winRate * avgWin) - (lossRate * avgLoss)
    expectancy = (win_rate / 100 * avg_win) - (loss_rate / 100 * avg_loss)

    # Sharpe-like ratio (simplified: avg return / std dev of returns)
    returns = [t["pnlPct"] for t in trades]
    avg_ret = sum(returns) / len(returns) if returns else 0
    variance = sum((r - avg_ret) ** 2 for r in returns) / len(returns) if returns else 0
    std_dev = variance ** 0.5
    sharpe = (avg_ret / std_dev) if std_dev > 0 else 0

    # Buy & hold comparison
    buy_hold_pnl = ((last_price - first_price) / first_price) * initial_capital
    buy_hold_pct = ((last_price - first_price) / first_price) * 100

    summary = {
        "netProfit": round(net_profit, 2),
        "netProfitPct": round(net_profit_pct, 2),
        "grossProfit": round(gross_profit, 2),
        "grossLoss": round(gross_loss, 2),
        "profitFactor": round(profit_factor, 2) if profit_factor != float("inf") else "∞",
        "totalTrades": total_trades,
        "winningTrades": len(winners),
        "losingTrades": len(losers),
        "breakevenTrades": len(breakeven),
        "winRate": round(win_rate, 2),
        "lossRate": round(loss_rate, 2),
        "avgTrade": round(avg_trade, 2),
        "avgWin": round(avg_win, 2),
        "avgLoss": round(avg_loss, 2),
        "largestWin": round(largest_win, 2),
        "largestLoss": round(largest_loss, 2),
        "maxConsecWins": max_consec_wins,
        "maxConsecLosses": max_consec_losses,
        "maxDrawdown": round(max_dd, 2),
        "maxDrawdownPct": round(max_dd_pct, 2),
        "payoffRatio": round(payoff_ratio, 2) if payoff_ratio != float("inf") else "∞",
        "expectancy": round(expectancy, 2),
        "sharpeRatio": round(sharpe, 2),
        "buyHoldPnl": round(buy_hold_pnl, 2),
        "buyHoldPct": round(buy_hold_pct, 2),
        "initialCapital": initial_capital,
        "finalCapital": round(capital, 2),
    }

    return {"trades": trades, "summary": summary, "equityCurve": equity_curve}


EXCHANGE_SUFFIX_MAP = {
    "NSI": ".NS", "NSE": ".NS",
    "BOM": ".BO", "BSE": ".BO",
}


@app.route("/api/search")
@login_required
def api_search():
    """Search for a stock/index ticker by name or symbol via Yahoo Finance search API.

    Accepts a query string via ?q= parameter. Uses Yahoo Finance's search
    endpoint to find matches by company name or ticker symbol. Returns up to
    6 results with properly suffixed tickers for data fetching.

    Returns:
        JSON array: List of {ticker, name, exchange} dicts, or empty [].
    """
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    try:
        resp = cffi_requests.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": q, "quotesCount": 6, "newsCount": 0,
                    "enableFuzzyQuery": True, "quotesQueryId": "tss_match_phrase_query"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5,
            impersonate="chrome",
        )
        data = resp.json()
        quotes = data.get("quotes", [])
        results = []
        for qt in quotes:
            qt_type = qt.get("quoteType", "")
            if qt_type not in ("EQUITY", "INDEX", "ETF", "MUTUALFUND", "FUTURE", "CRYPTOCURRENCY", "CURRENCY"):
                continue
            symbol = qt.get("symbol", "")
            name = qt.get("shortname") or qt.get("longname") or symbol
            exchange = qt.get("exchange", "")
            # Apply exchange suffix for Indian stocks
            if "." not in symbol:
                suffix = EXCHANGE_SUFFIX_MAP.get(exchange, "")
                ticker = symbol + suffix
            else:
                ticker = symbol
            results.append({"ticker": ticker, "name": name, "exchange": exchange})
        if results:
            return jsonify(results)
    except Exception:
        pass
    # Fallback: try direct ticker lookup
    candidates = [q]
    if "." not in q:
        candidates.append(q + ".NS")
        candidates.append(q + ".BO")
    for cand in candidates:
        try:
            t = yf.Ticker(cand)
            info = t.info or {}
            short_name = info.get("shortName") or info.get("longName")
            if not short_name:
                continue
            exchange = info.get("exchange", "")
            raw_symbol = info.get("symbol", cand.upper())
            if "." in raw_symbol:
                ticker = raw_symbol
            elif "." in cand:
                ticker = cand.upper()
            else:
                suffix = EXCHANGE_SUFFIX_MAP.get(exchange, "")
                ticker = raw_symbol + suffix
            return jsonify([{"ticker": ticker, "name": short_name, "exchange": exchange}])
        except Exception:
            continue
    return jsonify([])


# ---------------------------------------------------------------------------
# Paper Trading State (in-memory)
# ---------------------------------------------------------------------------
paper_trades = {}  # session_id -> session dict


@app.route("/api/trade/start", methods=["POST"])
@login_required
def api_trade_start():
    data = request.get_json(force=True)
    symbol = data.get("symbol", "NIFTY50")
    capital = float(data.get("capital", 100000))
    algo = data.get("algo", "mstreet")
    sid = uuid.uuid4().hex[:12]
    paper_trades[sid] = {
        "symbol": symbol,
        "algo": algo,
        "initialCapital": capital,
        "capital": capital,
        "position": 0,
        "entryPrice": 0,
        "entryTime": 0,
        "qty": 0,
        "trades": [],
        "equityCurve": [{"time": 0, "value": capital}],
        "peakEquity": capital,
        "maxDrawdown": 0,
        "maxDrawdownPct": 0,
        "active": True,
        "lastSignalTime": 0,
    }
    return jsonify({"sessionId": sid, "status": "started"})


@app.route("/api/trade/execute", methods=["POST"])
@login_required
def api_trade_execute():
    data = request.get_json(force=True)
    sid = data.get("sessionId", "")
    session = paper_trades.get(sid)
    if not session or not session["active"]:
        return jsonify({"error": "Invalid or inactive session"}), 400

    sig_type = data.get("signalType", "")
    price = float(data.get("price", 0))
    sig_time = data.get("time", 0)

    if sig_time <= session["lastSignalTime"]:
        return jsonify({"status": "duplicate", "trade": None})

    session["lastSignalTime"] = sig_time
    trade = None

    if sig_type in ("BUY", "STRONG_BUY") and session["position"] == 0:
        qty = int(session["capital"] / price) if price > 0 else 0
        if qty <= 0:
            return jsonify({"status": "insufficient_capital", "trade": None})
        session["position"] = 1
        session["entryPrice"] = price
        session["entryTime"] = sig_time
        session["qty"] = qty
        trade = {"action": "BUY", "price": round(price, 2), "qty": qty,
                 "time": sig_time, "capital": round(session["capital"], 2)}

    elif sig_type in ("SELL", "STRONG_SELL") and session["position"] == 1:
        pnl = (price - session["entryPrice"]) * session["qty"]
        pnl_pct = ((price - session["entryPrice"]) / session["entryPrice"]) * 100 if session["entryPrice"] else 0
        session["capital"] += pnl
        trade_rec = {
            "entryTime": session["entryTime"],
            "exitTime": sig_time,
            "entryPrice": round(session["entryPrice"], 2),
            "exitPrice": round(price, 2),
            "qty": session["qty"],
            "pnl": round(pnl, 2),
            "pnlPct": round(pnl_pct, 2),
            "capital": round(session["capital"], 2),
        }
        session["trades"].append(trade_rec)
        session["equityCurve"].append({"time": sig_time, "value": round(session["capital"], 2)})
        session["peakEquity"] = max(session["peakEquity"], session["capital"])
        dd = session["peakEquity"] - session["capital"]
        dd_pct = (dd / session["peakEquity"] * 100) if session["peakEquity"] else 0
        session["maxDrawdown"] = max(session["maxDrawdown"], dd)
        session["maxDrawdownPct"] = max(session["maxDrawdownPct"], dd_pct)
        session["position"] = 0
        session["qty"] = 0
        session["entryPrice"] = 0
        session["entryTime"] = 0
        trade = {"action": "SELL", "price": round(price, 2), "qty": trade_rec["qty"],
                 "time": sig_time, "pnl": trade_rec["pnl"], "capital": trade_rec["capital"]}

    return jsonify({"status": "ok", "trade": trade, "summary": _trade_summary(session)})


@app.route("/api/trade/stop", methods=["POST"])
@login_required
def api_trade_stop():
    data = request.get_json(force=True)
    sid = data.get("sessionId", "")
    price = float(data.get("price", 0))
    session = paper_trades.get(sid)
    if not session:
        return jsonify({"error": "Invalid session"}), 400

    # Close open position at provided price
    if session["position"] == 1 and price > 0:
        pnl = (price - session["entryPrice"]) * session["qty"]
        pnl_pct = ((price - session["entryPrice"]) / session["entryPrice"]) * 100 if session["entryPrice"] else 0
        session["capital"] += pnl
        trade_rec = {
            "entryTime": session["entryTime"],
            "exitTime": int(datetime.now().timestamp()),
            "entryPrice": round(session["entryPrice"], 2),
            "exitPrice": round(price, 2),
            "qty": session["qty"],
            "pnl": round(pnl, 2),
            "pnlPct": round(pnl_pct, 2),
            "capital": round(session["capital"], 2),
            "forced": True,
        }
        session["trades"].append(trade_rec)
        session["equityCurve"].append({"time": trade_rec["exitTime"], "value": round(session["capital"], 2)})
        session["peakEquity"] = max(session["peakEquity"], session["capital"])
        dd = session["peakEquity"] - session["capital"]
        dd_pct = (dd / session["peakEquity"] * 100) if session["peakEquity"] else 0
        session["maxDrawdown"] = max(session["maxDrawdown"], dd)
        session["maxDrawdownPct"] = max(session["maxDrawdownPct"], dd_pct)
        session["position"] = 0

    session["active"] = False
    return jsonify({"status": "stopped", "summary": _trade_summary(session)})


@app.route("/api/trade/status")
@login_required
def api_trade_status():
    sid = request.args.get("session_id", "")
    session = paper_trades.get(sid)
    if not session:
        return jsonify({"error": "Invalid session"}), 400
    return jsonify({
        "active": session["active"],
        "symbol": session["symbol"],
        "algo": session["algo"],
        "position": session["position"],
        "entryPrice": round(session["entryPrice"], 2),
        "qty": session["qty"],
        "capital": round(session["capital"], 2),
        "trades": session["trades"],
        "equityCurve": session["equityCurve"],
        "summary": _trade_summary(session),
    })


def _trade_summary(session):
    trades = session["trades"]
    initial = session["initialCapital"]
    capital = session["capital"]
    net = capital - initial
    net_pct = (net / initial * 100) if initial else 0
    total = len(trades)
    if total == 0:
        return {"totalTrades": 0, "netProfit": 0, "netProfitPct": 0,
                "initialCapital": initial, "finalCapital": round(capital, 2),
                "maxDrawdown": 0, "maxDrawdownPct": 0}
    winners = [t for t in trades if t["pnl"] > 0]
    losers = [t for t in trades if t["pnl"] < 0]
    gross_profit = sum(t["pnl"] for t in winners)
    gross_loss = abs(sum(t["pnl"] for t in losers))
    pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else ("∞" if gross_profit > 0 else 0)
    win_rate = round(len(winners) / total * 100, 2) if total else 0
    avg_trade = round(net / total, 2)
    avg_win = round(gross_profit / len(winners), 2) if winners else 0
    avg_loss = round(-gross_loss / len(losers), 2) if losers else 0
    largest_win = round(max((t["pnl"] for t in winners), default=0), 2)
    largest_loss = round(min((t["pnl"] for t in losers), default=0), 2)
    return {
        "totalTrades": total,
        "winningTrades": len(winners),
        "losingTrades": len(losers),
        "netProfit": round(net, 2),
        "netProfitPct": round(net_pct, 2),
        "grossProfit": round(gross_profit, 2),
        "grossLoss": round(gross_loss, 2),
        "profitFactor": pf,
        "winRate": win_rate,
        "avgTrade": avg_trade,
        "avgWin": avg_win,
        "avgLoss": avg_loss,
        "largestWin": largest_win,
        "largestLoss": largest_loss,
        "maxDrawdown": round(session["maxDrawdown"], 2),
        "maxDrawdownPct": round(session["maxDrawdownPct"], 2),
        "initialCapital": initial,
        "finalCapital": round(capital, 2),
    }


# ---------------------------------------------------------------------------
# Help Pages
# ---------------------------------------------------------------------------
HELP_PAGE_STYLE = r"""
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#131722; color:#d1d4dc; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; padding:0; }
  .help-header { background:#1e222d; padding:16px 32px; border-bottom:1px solid #2a2e39; display:flex; justify-content:space-between; align-items:center; position:sticky; top:0; z-index:10; }
  .help-header h1 { font-size:22px; color:#fff; }
  .help-header a { color:#2962ff; text-decoration:none; font-size:14px; }
  .help-header a:hover { text-decoration:underline; }
  .download-btn { background:#2962ff; color:#fff; border:none; padding:8px 18px; border-radius:4px; cursor:pointer; font-size:13px; font-weight:600; }
  .download-btn:hover { background:#1e53e5; }
  .help-body { max-width:960px; margin:0 auto; padding:32px 24px 80px; }
  h2 { color:#fff; font-size:20px; margin:32px 0 12px; padding-bottom:8px; border-bottom:1px solid #2a2e39; }
  h3 { color:#ff9100; font-size:16px; margin:20px 0 8px; }
  h4 { color:#2196f3; font-size:14px; margin:14px 0 6px; }
  p, li { font-size:14px; line-height:1.7; color:#b2b5be; }
  ul, ol { padding-left:24px; margin:8px 0; }
  table { width:100%; border-collapse:collapse; margin:12px 0; font-size:13px; }
  th { background:#1e222d; color:#d1d4dc; padding:10px 12px; text-align:left; border:1px solid #2a2e39; }
  td { padding:8px 12px; border:1px solid #2a2e39; color:#b2b5be; }
  tr:nth-child(even) td { background:#1a1e2a; }
  code { background:#1e222d; color:#ff9100; padding:2px 6px; border-radius:3px; font-size:13px; }
  .tag { display:inline-block; padding:2px 8px; border-radius:3px; font-size:11px; font-weight:600; margin-right:4px; }
  .tag-buy { background:rgba(38,166,154,0.2); color:#26a69a; }
  .tag-sell { background:rgba(239,83,80,0.2); color:#ef5350; }
  .tag-weight { background:rgba(41,98,255,0.15); color:#5b8def; }
  .card { background:#1e222d; border:1px solid #2a2e39; border-radius:8px; padding:16px 20px; margin:12px 0; }
  .score-bar { display:flex; align-items:center; gap:8px; margin:6px 0; }
  .score-fill { height:6px; border-radius:3px; }
</style>
<script>
function downloadPDF(){
  const el = document.querySelector('.help-body');
  const title = document.title;
  const win = window.open('','','width=900,height=700');
  win.document.write('<html><head><title>'+title+'</title><style>');
  win.document.write('body{font-family:Arial,sans-serif;padding:24px;color:#222;font-size:13px}');
  win.document.write('h1{font-size:22px;margin-bottom:16px}h2{font-size:18px;margin:20px 0 8px;border-bottom:1px solid #ccc;padding-bottom:4px}');
  win.document.write('h3{font-size:15px;color:#e65100;margin:14px 0 6px}h4{font-size:13px;color:#1565c0;margin:10px 0 4px}');
  win.document.write('p,li{line-height:1.6;font-size:13px}ul,ol{padding-left:20px}');
  win.document.write('table{width:100%;border-collapse:collapse;margin:8px 0;font-size:12px}th{background:#f5f5f5;padding:6px 8px;border:1px solid #ddd;text-align:left}td{padding:6px 8px;border:1px solid #ddd}');
  win.document.write('code{background:#f5f5f5;padding:1px 4px;font-size:12px;border-radius:2px}');
  win.document.write('.tag{display:inline-block;padding:1px 6px;border-radius:2px;font-size:10px;font-weight:600;margin-right:3px}');
  win.document.write('.tag-buy{background:#e8f5e9;color:#2e7d32}.tag-sell{background:#ffebee;color:#c62828}.tag-weight{background:#e3f2fd;color:#1565c0}');
  win.document.write('.card{border:1px solid #ddd;border-radius:6px;padding:12px 16px;margin:8px 0;background:#fafafa}');
  win.document.write('.download-btn,.help-header{display:none}');
  win.document.write('</style></head><body>');
  win.document.write('<h1>'+title+'</h1>');
  win.document.write(el.innerHTML);
  win.document.write('</body></html>');
  win.document.close();
  setTimeout(function(){ win.print(); }, 500);
}
</script>
"""

HELP_ALGOS_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Mangal View - Algo Documentation</title>""" + HELP_PAGE_STYLE + r"""
</head><body>
<div class="help-header">
  <div><h1>&#128202; Algorithm Documentation</h1><a href="/">&larr; Back to Chart</a></div>
  <button class="download-btn" onclick="downloadPDF()">&#128196; Download PDF</button>
</div>
<div class="help-body">

<p>Mangal View provides <strong>14 algorithmic signal engines</strong> plus 1 ML prediction model. Each algorithm uses a weighted scoring system — multiple technical indicators contribute directional scores that are summed into a final composite score. When the score exceeds the threshold, a BUY or SELL signal is generated.</p>

<h2>Signal Scoring System</h2>
<div class="card">
<p>Each indicator contributes a score between <code>-weight</code> and <code>+weight</code>. The total score determines the signal:</p>
<table>
<tr><th>Signal</th><th>Condition</th><th>Meaning</th></tr>
<tr><td><span class="tag tag-buy">STRONG BUY</span></td><td>Score &ge; Strong threshold</td><td>High-confidence bullish setup, multiple confirmations aligned</td></tr>
<tr><td><span class="tag tag-buy">BUY</span></td><td>Score &ge; Buy threshold</td><td>Moderate bullish setup</td></tr>
<tr><td><span class="tag tag-sell">SELL</span></td><td>Score &le; -Sell threshold</td><td>Moderate bearish setup</td></tr>
<tr><td><span class="tag tag-sell">STRONG SELL</span></td><td>Score &le; -Strong threshold</td><td>High-confidence bearish setup, multiple confirmations aligned</td></tr>
</table>
</div>

<h2>1. Trend</h2>
<div class="card">
<p><strong>Style:</strong> Classic multi-indicator trend following<br>
<strong>Thresholds:</strong> BUY &ge; 3.5 | STRONG BUY &ge; 5.5 | SELL &le; -3.5 | STRONG SELL &le; -5.5<br>
<strong>Best for:</strong> Swing trading, position trading on trending markets</p>
<h4>Indicators &amp; Weights</h4>
<table>
<tr><th>Indicator</th><th>Weight</th><th>Logic</th></tr>
<tr><td>SuperTrend</td><td><span class="tag tag-weight">1.5</span></td><td>+1 if bullish (price above band), -1 if bearish</td></tr>
<tr><td>Parabolic SAR</td><td><span class="tag tag-weight">1.0</span></td><td>+1 if SAR below price (bullish), -1 if above</td></tr>
<tr><td>RSI (14)</td><td><span class="tag tag-weight">1.5</span></td><td>Overbought/oversold zones + momentum direction</td></tr>
<tr><td>MACD</td><td><span class="tag tag-weight">2.0</span></td><td>Signal line crossover + histogram direction</td></tr>
<tr><td>EMA 9/21</td><td><span class="tag tag-weight">1.5</span></td><td>+1 if EMA9 &gt; EMA21 (golden cross), -1 if death cross</td></tr>
<tr><td>VWAP</td><td><span class="tag tag-weight">1.0</span></td><td>+1 if price above VWAP, -1 if below</td></tr>
<tr><td>Volume</td><td><span class="tag tag-weight">0.5</span></td><td>Confirms signal if volume above average</td></tr>
<tr><td>Candlestick Patterns</td><td><span class="tag tag-weight">1.0</span></td><td>Engulfing, Hammer, Shooting Star, Morning/Evening Star</td></tr>
<tr><td>S/R Proximity</td><td><span class="tag tag-weight">0.5</span></td><td>Extra weight near support (buy) or resistance (sell)</td></tr>
</table>
<p><strong>Max possible score:</strong> &plusmn;10.5</p>
</div>

<h2>2. MStreet</h2>
<div class="card">
<p><strong>Style:</strong> Statistical mean-reversion + momentum breakout (quantitative)<br>
<strong>Thresholds:</strong> BUY &ge; 3.0 | STRONG BUY &ge; 5.0 | SELL &le; -3.0 | STRONG SELL &le; -5.0<br>
<strong>Best for:</strong> Range-bound markets, mean-reversion entries</p>
<h4>Indicators &amp; Weights</h4>
<table>
<tr><th>Indicator</th><th>Weight</th><th>Logic</th></tr>
<tr><td>Z-Score (20-bar)</td><td><span class="tag tag-weight">2.0</span></td><td>Buy when z-score &lt; -1.5 (oversold), sell when &gt; 1.5 (overbought)</td></tr>
<tr><td>BB Squeeze</td><td><span class="tag tag-weight">1.5</span></td><td>Detects Bollinger Band squeeze + expansion direction</td></tr>
<tr><td>RSI Divergence</td><td><span class="tag tag-weight">1.5</span></td><td>Bullish divergence (price lower, RSI higher) and vice versa</td></tr>
<tr><td>Volume-Weighted Momentum</td><td><span class="tag tag-weight">1.5</span></td><td>Price momentum weighted by relative volume</td></tr>
<tr><td>MACD Histogram Momentum</td><td><span class="tag tag-weight">1.5</span></td><td>Rate of change of MACD histogram</td></tr>
<tr><td>EMA Spread Z-Score</td><td><span class="tag tag-weight">1.0</span></td><td>Statistical deviation of EMA9-EMA21 spread</td></tr>
<tr><td>S/R Mean Reversion</td><td><span class="tag tag-weight">0.5</span></td><td>Bounce probability near support/resistance levels</td></tr>
</table>
</div>

<h2>3. MFactor</h2>
<div class="card">
<p><strong>Style:</strong> High-accuracy multi-factor model with 12 indicators<br>
<strong>Thresholds:</strong> BUY &ge; 4.0 | STRONG BUY &ge; 6.5 | SELL &le; -4.0 | STRONG SELL &le; -6.5<br>
<strong>Best for:</strong> Precision entries, reducing false signals<br>
<strong>Unique:</strong> Enforces strict alternating BUY→SELL→BUY pattern to avoid repeated same-direction signals</p>
<h4>Indicators &amp; Weights</h4>
<table>
<tr><th>Indicator</th><th>Weight</th><th>Logic</th></tr>
<tr><td>Z-Score</td><td><span class="tag tag-weight">2.0</span></td><td>20-bar statistical deviation from mean</td></tr>
<tr><td>BB Position</td><td><span class="tag tag-weight">1.5</span></td><td>%B position within Bollinger Bands</td></tr>
<tr><td>RSI + Stochastic RSI</td><td><span class="tag tag-weight">2.0</span></td><td>Double-smoothed RSI for extreme zones</td></tr>
<tr><td>MACD Cross + Histogram</td><td><span class="tag tag-weight">2.0</span></td><td>Signal crossover + histogram direction</td></tr>
<tr><td>VWAP Deviation</td><td><span class="tag tag-weight">1.5</span></td><td>Distance from VWAP as % of price</td></tr>
<tr><td>EMA 9/21 Spread + Cross</td><td><span class="tag tag-weight">1.5</span></td><td>Spread magnitude + crossover detection</td></tr>
<tr><td>ATR Volatility Regime</td><td><span class="tag tag-weight">1.0</span></td><td>High/low volatility regime filter</td></tr>
<tr><td>S/R Proximity</td><td><span class="tag tag-weight">1.0</span></td><td>Proximity to key support/resistance levels</td></tr>
<tr><td>Candle Body Ratio</td><td><span class="tag tag-weight">1.0</span></td><td>Strong body (&gt;70%) confirms conviction</td></tr>
<tr><td>Price Momentum (ROC)</td><td><span class="tag tag-weight">1.5</span></td><td>10-bar Rate of Change</td></tr>
<tr><td>Heikin-Ashi Trend</td><td><span class="tag tag-weight">1.0</span></td><td>HA candle direction filter</td></tr>
<tr><td>OBV Volume Pressure</td><td><span class="tag tag-weight">1.0</span></td><td>On-Balance Volume delta direction</td></tr>
</table>
</div>

<h2>4. Sniper</h2>
<div class="card">
<p><strong>Style:</strong> High-precision breakout detection (few but accurate signals)<br>
<strong>Thresholds:</strong> BUY &ge; 5.0 | STRONG BUY &ge; 7.0 | SELL &le; -5.0 | STRONG SELL &le; -7.0<br>
<strong>Best for:</strong> Breakout trading after consolidation, high-conviction entries</p>
<h4>Indicators &amp; Weights</h4>
<table>
<tr><th>Indicator</th><th>Weight</th><th>Logic</th></tr>
<tr><td>Consolidation Squeeze</td><td><span class="tag tag-weight">2.0</span></td><td>Detects tight range compression before breakout</td></tr>
<tr><td>BB Breakout</td><td><span class="tag tag-weight">2.0</span></td><td>Price breaking above upper / below lower BB</td></tr>
<tr><td>Volume Explosion</td><td><span class="tag tag-weight">2.0</span></td><td>Volume &gt; 2x 20-bar average (institutional interest)</td></tr>
<tr><td>EMA 9/21 Alignment</td><td><span class="tag tag-weight">1.5</span></td><td>Trend alignment + crossover confirmation</td></tr>
<tr><td>RSI Momentum Thrust</td><td><span class="tag tag-weight">1.5</span></td><td>RSI breaking through 50-level with momentum</td></tr>
<tr><td>MACD Histogram Accel.</td><td><span class="tag tag-weight">1.5</span></td><td>Increasing histogram bars (accelerating momentum)</td></tr>
<tr><td>VWAP Breakout</td><td><span class="tag tag-weight">1.5</span></td><td>Price breaking above/below VWAP with volume</td></tr>
<tr><td>S/R Level Breakout</td><td><span class="tag tag-weight">1.5</span></td><td>Breaking through key support/resistance levels</td></tr>
<tr><td>Candle Body Strength</td><td><span class="tag tag-weight">1.0</span></td><td>Body &gt; 70% of total range (strong conviction)</td></tr>
</table>
</div>

<h2>5. OrderFlow</h2>
<div class="card">
<p><strong>Style:</strong> Order flow analysis — buying vs selling pressure<br>
<strong>Thresholds:</strong> BUY &ge; 4.0 | STRONG BUY &ge; 6.0 | SELL &le; -4.0 | STRONG SELL &le; -6.0<br>
<strong>Best for:</strong> Reading institutional activity, volume-based entries</p>
<h4>Indicators &amp; Weights</h4>
<table>
<tr><th>Indicator</th><th>Weight</th><th>Logic</th></tr>
<tr><td>Volume Delta</td><td><span class="tag tag-weight">2.0</span></td><td>Buy volume vs sell volume ratio per candle</td></tr>
<tr><td>CVD Trend + Divergence</td><td><span class="tag tag-weight">2.0</span></td><td>Cumulative Volume Delta direction + price divergence</td></tr>
<tr><td>Absorption Detection</td><td><span class="tag tag-weight">2.0</span></td><td>Large wick + high volume = institutional absorption</td></tr>
<tr><td>Iceberg Orders</td><td><span class="tag tag-weight">1.5</span></td><td>Aggressive hidden order detection</td></tr>
<tr><td>VWAP Institutional</td><td><span class="tag tag-weight">1.5</span></td><td>Institutional buying/selling around VWAP</td></tr>
<tr><td>Volume Profile POC</td><td><span class="tag tag-weight">1.5</span></td><td>Activity near Point of Control</td></tr>
<tr><td>RSI + Volume</td><td><span class="tag tag-weight">1.5</span></td><td>RSI extreme confirmed by volume surge</td></tr>
<tr><td>MACD + Volume</td><td><span class="tag tag-weight">1.5</span></td><td>MACD crossover with volume confirmation</td></tr>
<tr><td>Price Rejection</td><td><span class="tag tag-weight">1.0</span></td><td>Long wicks at key levels (rejection candles)</td></tr>
<tr><td>EMA Trend Alignment</td><td><span class="tag tag-weight">1.0</span></td><td>Trend direction filter</td></tr>
</table>
</div>

<h2>6. PriceAction</h2>
<div class="card">
<p><strong>Style:</strong> Pure price structure analysis — no lagging indicators<br>
<strong>Thresholds:</strong> BUY &ge; 4.0 | STRONG BUY &ge; 6.0 | SELL &le; -4.0 | STRONG SELL &le; -6.0<br>
<strong>Best for:</strong> Clean chart traders, price structure believers</p>
<h4>Indicators &amp; Weights</h4>
<table>
<tr><th>Indicator</th><th>Weight</th><th>Logic</th></tr>
<tr><td>Trend Structure</td><td><span class="tag tag-weight">2.0</span></td><td>Higher Highs/Higher Lows vs Lower Highs/Lower Lows</td></tr>
<tr><td>Candlestick Reversals</td><td><span class="tag tag-weight">2.0</span></td><td>Pin bars, engulfing, hammer, shooting star patterns</td></tr>
<tr><td>Pin Bar Rejection</td><td><span class="tag tag-weight">2.0</span></td><td>Pin bars at key support/resistance levels</td></tr>
<tr><td>Inside Bar Breakout</td><td><span class="tag tag-weight">1.5</span></td><td>Breakout from inside bar pattern (contraction→expansion)</td></tr>
<tr><td>Engulfing + Momentum</td><td><span class="tag tag-weight">1.5</span></td><td>Engulfing pattern with follow-through momentum</td></tr>
<tr><td>S/R Reaction</td><td><span class="tag tag-weight">1.5</span></td><td>Price reaction at support/resistance zones</td></tr>
<tr><td>Higher TF Context</td><td><span class="tag tag-weight">1.5</span></td><td>Multi-candle context for trend direction</td></tr>
<tr><td>Consecutive Momentum</td><td><span class="tag tag-weight">1.0</span></td><td>3+ consecutive directional candles</td></tr>
<tr><td>Range Contraction→Expansion</td><td><span class="tag tag-weight">1.0</span></td><td>Volatility contraction followed by expansion</td></tr>
<tr><td>Gap Analysis</td><td><span class="tag tag-weight">1.0</span></td><td>Gap up/down with continuation potential</td></tr>
<tr><td>Swing Failure (SFP)</td><td><span class="tag tag-weight">1.5</span></td><td>Failed breakout beyond swing high/low = reversal signal</td></tr>
</table>
</div>

<h2>7. Breakout</h2>
<div class="card">
<p><strong>Style:</strong> Channel breakout with volatility expansion<br>
<strong>Thresholds:</strong> BUY &ge; 4.0 | STRONG BUY &ge; 6.0 | SELL &le; -4.0 | STRONG SELL &le; -6.0<br>
<strong>Best for:</strong> Trending markets, catching new directional moves</p>
<h4>Key Techniques</h4>
<ul>
<li><strong>Donchian Channel Breakout</strong> <span class="tag tag-weight">2.0</span> — Price breaking above 20-bar high / below 20-bar low</li>
<li><strong>BB Expansion Breakout</strong> <span class="tag tag-weight">2.0</span> — Bollinger Band expansion after squeeze</li>
<li>Standard confirmations: RSI, MACD, EMA, VWAP, Volume, S/R</li>
</ul>
</div>

<h2>8. Momentum</h2>
<div class="card">
<p><strong>Style:</strong> Rate of change and momentum acceleration<br>
<strong>Thresholds:</strong> BUY &ge; 4.0 | STRONG BUY &ge; 6.0 | SELL &le; -4.0 | STRONG SELL &le; -6.0<br>
<strong>Best for:</strong> Fast-moving markets, riding momentum waves</p>
<h4>Key Techniques</h4>
<ul>
<li><strong>ROC (10-bar)</strong> <span class="tag tag-weight">2.0</span> — Strong Rate of Change (&gt;1.5% strong, &gt;0.5% weak)</li>
<li><strong>ROC Acceleration</strong> <span class="tag tag-weight">1.5</span> — ROC-of-ROC (momentum speeding up)</li>
<li><strong>RSI 50-Cross</strong> <span class="tag tag-weight">2.0</span> — RSI crossing above/below 50 level</li>
<li>Confirmations: MACD, EMA alignment, VWAP, Volume surge</li>
</ul>
</div>

<h2>9. Scalping</h2>
<div class="card">
<p><strong>Style:</strong> Quick mean-reversion trades on extreme levels<br>
<strong>Thresholds:</strong> BUY &ge; 3.5 | STRONG BUY &ge; 5.5 | SELL &le; -3.5 | STRONG SELL &le; -5.5<br>
<strong>Best for:</strong> 1-5 min timeframes, rapid entries and exits</p>
<h4>Key Techniques</h4>
<ul>
<li><strong>BB Bounce</strong> <span class="tag tag-weight">2.0</span> — Buy at lower BB, sell at upper BB</li>
<li><strong>RSI Extreme Reversal</strong> <span class="tag tag-weight">2.0</span> — Buy when RSI &lt; 25, sell when RSI &gt; 75</li>
<li><strong>VWAP Mean Reversion</strong> <span class="tag tag-weight">2.0</span> — Price returning to VWAP from extremes</li>
<li><strong>Micro EMA Cross</strong> — Fast EMA5 vs EMA9 for quick momentum shifts</li>
<li>Shorter lookback (15 bars), minimum 20 candles required</li>
</ul>
</div>

<h2>10. SmartMoney</h2>
<div class="card">
<p><strong>Style:</strong> Smart Money Concepts (SMC) — institutional order flow<br>
<strong>Thresholds:</strong> BUY &ge; 4.0 | STRONG BUY &ge; 6.0 | SELL &le; -4.0 | STRONG SELL &le; -6.0<br>
<strong>Best for:</strong> Traders following institutional footprints</p>
<h4>Key Techniques</h4>
<ul>
<li><strong>Order Block Detection</strong> <span class="tag tag-weight">2.0</span> — Identifying institutional order blocks (last opposite candle before impulsive move)</li>
<li><strong>Fair Value Gap</strong> <span class="tag tag-weight">2.0</span> — 3-candle price imbalance zones where institutions left gaps</li>
<li><strong>Liquidity Sweep + Reversal</strong> <span class="tag tag-weight">2.0</span> — Stop hunt beyond swing H/L followed by reversal</li>
<li><strong>Displacement Candle</strong> <span class="tag tag-weight">1.5</span> — Large-body candle showing strong institutional intent</li>
<li>Uses swing highs/lows computed with 5-bar lookback for structure</li>
</ul>
</div>

<h2>11. Quant</h2>
<div class="card">
<p><strong>Style:</strong> Statistical/quantitative models<br>
<strong>Thresholds:</strong> BUY &ge; 3.5 | STRONG BUY &ge; 5.5 | SELL &le; -3.5 | STRONG SELL &le; -5.5<br>
<strong>Best for:</strong> Statistically-driven traders, mean reversion<br>
<strong>Requires:</strong> Minimum 50 candles</p>
<h4>Key Techniques</h4>
<ul>
<li><strong>Z-Score</strong> <span class="tag tag-weight">2.0</span> — Standard deviation from 20-bar mean</li>
<li><strong>Linear Regression Deviation</strong> <span class="tag tag-weight">2.0</span> — Price deviation from regression line in sigma units</li>
<li><strong>Bollinger %B</strong> <span class="tag tag-weight">1.5</span> — Position within BB (0 = lower, 1 = upper)</li>
<li><strong>Stochastic RSI</strong> — Double-smoothed RSI for precise overbought/oversold zones</li>
<li>Regression slope + deviation analysis for trend + mean-reversion combo</li>
</ul>
</div>

<h2>12. Hybrid</h2>
<div class="card">
<p><strong>Style:</strong> Multi-strategy voting consensus<br>
<strong>Thresholds:</strong> BUY &ge; 2.5 | STRONG BUY &ge; 3.5 | SELL &le; -2.5 | STRONG SELL &le; -3.5<br>
<strong>Best for:</strong> Balanced approach, reducing false signals through consensus</p>
<h4>Voting System</h4>
<p>Four independent sub-strategies each cast a vote (+1, -1, or 0). The final signal = sum of all votes:</p>
<table>
<tr><th>Sub-Strategy</th><th>Vote Logic</th></tr>
<tr><td><strong>Trend Vote</strong></td><td>EMA 9/21 alignment + price vs VWAP position</td></tr>
<tr><td><strong>Mean Reversion Vote</strong></td><td>Z-score extreme + Bollinger Band position</td></tr>
<tr><td><strong>Momentum Vote</strong></td><td>MACD histogram direction + Rate of Change</td></tr>
<tr><td><strong>Volume Vote</strong></td><td>Volume delta direction + buying/selling pressure</td></tr>
</table>
</div>

<h2>13. StatArb (Statistical Arbitrage)</h2>
<div class="card">
<p><strong>Style:</strong> Statistical mean-reversion using spread analysis and z-score deviation<br>
<strong>Thresholds:</strong> BUY &ge; 3.5 | STRONG BUY &ge; 5.0 | SELL &le; -3.5 | STRONG SELL &le; -5.0<br>
<strong>Best for:</strong> Mean-reversion in range-bound markets, statistical edge trading<br>
<strong>Requires:</strong> Minimum 40 candles</p>
<h4>Indicators &amp; Weights</h4>
<table>
<tr><th>Indicator</th><th>Weight</th><th>Logic</th></tr>
<tr><td>Z-Score (20-bar)</td><td><span class="tag tag-weight">2.5</span></td><td>Deep oversold z &lt; -2.0 or overbought z &gt; 2.0 for max weight</td></tr>
<tr><td>Bollinger %B</td><td><span class="tag tag-weight">2.0</span></td><td>Position within BB bands (%B &lt; 0.05 or &gt; 0.95 for extremes)</td></tr>
<tr><td>Spread Velocity</td><td><span class="tag tag-weight">1.5</span></td><td>Rate of z-score change over 5 bars — accelerating deviation</td></tr>
<tr><td>RSI Divergence</td><td><span class="tag tag-weight">1.5</span></td><td>Price at extreme z-score but RSI stable = reversal signal</td></tr>
<tr><td>EMA Spread Z-Score</td><td><span class="tag tag-weight">1.5</span></td><td>EMA9-EMA21 spread deviation from its own mean</td></tr>
<tr><td>MACD Histogram Reversal</td><td><span class="tag tag-weight">1.5</span></td><td>Histogram reversing direction from extreme = momentum shift</td></tr>
<tr><td>Volume Confirmation</td><td><span class="tag tag-weight">1.0</span></td><td>Capitulation selling or euphoric buying at extremes</td></tr>
</table>
<p><strong>Max possible score:</strong> &plusmn;12.0</p>
</div>

<h2>14. Institution (Institutional Algo)</h2>
<div class="card">
<p><strong>Style:</strong> Institutional accumulation/distribution detection using volume footprint analysis<br>
<strong>Thresholds:</strong> BUY &ge; 3.5 | STRONG BUY &ge; 5.0 | SELL &le; -3.5 | STRONG SELL &le; -5.0<br>
<strong>Best for:</strong> Following institutional money flow, detecting smart money activity<br>
<strong>Requires:</strong> Minimum 40 candles</p>
<h4>Indicators &amp; Weights</h4>
<table>
<tr><th>Indicator</th><th>Weight</th><th>Logic</th></tr>
<tr><td>Institutional Volume</td><td><span class="tag tag-weight">2.5</span></td><td>Absorption candles (high vol + small body) = institutional accumulation/distribution</td></tr>
<tr><td>Order Block Detection</td><td><span class="tag tag-weight">2.0</span></td><td>Last opposite candle before impulsive move (3-bar lookback)</td></tr>
<tr><td>VWAP Institutional Anchor</td><td><span class="tag tag-weight">2.0</span></td><td>Institutional buying below VWAP / selling above VWAP with volume</td></tr>
<tr><td>OBV Divergence</td><td><span class="tag tag-weight">1.5</span></td><td>Price vs OBV divergence = hidden accumulation or distribution</td></tr>
<tr><td>Dark Pool Footprint</td><td><span class="tag tag-weight">1.5</span></td><td>Repeated high-volume activity at same price level (5-bar cluster)</td></tr>
<tr><td>RSI + Volume</td><td><span class="tag tag-weight">1.5</span></td><td>RSI extreme zones confirmed by institutional volume</td></tr>
<tr><td>EMA Trend Alignment</td><td><span class="tag tag-weight">1.0</span></td><td>Trend direction confirmation filter</td></tr>
<tr><td>S/R Level Reaction</td><td><span class="tag tag-weight">1.0</span></td><td>Institutional support holds or resistance rejections with volume</td></tr>
</table>
<p><strong>Max possible score:</strong> &plusmn;13.5</p>
</div>

<h2>15. MPredict (ML Model)</h2>
<div class="card">
<p><strong>Style:</strong> Machine Learning prediction (not a signal engine)<br>
<strong>Model:</strong> GradientBoostingRegressor (sklearn)<br>
<strong>Output:</strong> Predicts next 5 candles displayed as semi-transparent overlay</p>
<h4>Features Used</h4>
<ul>
<li>10 lagged returns (price changes over past 10 candles)</li>
<li>Body/wick ratios (candle morphology)</li>
<li>Rolling mean &amp; standard deviation (5, 10, 20 periods)</li>
<li>RSI-like momentum indicator</li>
<li>Range % and volume change</li>
</ul>
<p>4 separate models predict open/high/low/close offsets from the last real candle. Predictions are iterative — each predicted candle feeds into the next prediction.</p>
</div>

<h2>Choosing the Right Algorithm</h2>
<div class="card">
<table>
<tr><th>Market Condition</th><th>Recommended Algos</th></tr>
<tr><td>Strong Trending</td><td>Trend, Momentum, Breakout</td></tr>
<tr><td>Range-Bound / Sideways</td><td>MStreet, Scalping, Quant, StatArb</td></tr>
<tr><td>High Volatility</td><td>Sniper, SmartMoney</td></tr>
<tr><td>Volume-Driven</td><td>OrderFlow, SmartMoney, Institution</td></tr>
<tr><td>Clean Charts</td><td>PriceAction</td></tr>
<tr><td>Maximum Accuracy</td><td>MFactor, Hybrid</td></tr>
<tr><td>Quick Scalps</td><td>Scalping</td></tr>
<tr><td>Mean Reversion</td><td>StatArb, MStreet, Quant</td></tr>
<tr><td>Institutional Flow</td><td>Institution, OrderFlow, SmartMoney</td></tr>
</table>
<p><strong>Tip:</strong> You can enable multiple algos simultaneously. Signals are deduplicated by time — the signal with the highest absolute score is kept.</p>
</div>

</div></body></html>"""


HELP_INDICATORS_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Mangal View - Indicator Documentation</title>""" + HELP_PAGE_STYLE + r"""
</head><body>
<div class="help-header">
  <div><h1>&#128200; Indicator Documentation</h1><a href="/">&larr; Back to Chart</a></div>
  <button class="download-btn" onclick="downloadPDF()">&#128196; Download PDF</button>
</div>
<div class="help-body">

<p>Mangal View provides <strong>14 technical indicators</strong> that can be toggled individually from the Indicators dropdown. Each indicator is computed server-side and rendered on the chart.</p>

<h2>1. SuperTrend</h2>
<div class="card">
<p><strong>Toggle:</strong> Indicators &rarr; SuperTrend &nbsp;|&nbsp; <strong>Configurable:</strong> Period (default 10), Multiplier (default 3.0)</p>
<h4>How It Works</h4>
<p>SuperTrend uses Average True Range (ATR) to create dynamic support/resistance bands around price:</p>
<ul>
<li><strong>Upper Band</strong> = (High + Low) / 2 + Multiplier &times; ATR</li>
<li><strong>Lower Band</strong> = (High + Low) / 2 - Multiplier &times; ATR</li>
<li>When price closes <strong>above</strong> the upper band → trend flips <span class="tag tag-buy">BULLISH</span> (green line)</li>
<li>When price closes <strong>below</strong> the lower band → trend flips <span class="tag tag-sell">BEARISH</span> (red line)</li>
</ul>
<h4>Usage</h4>
<p>Trade in the direction of the SuperTrend. Green = buy bias, Red = sell bias. Higher period/multiplier = fewer whipsaws but slower signals.</p>
</div>

<h2>2. Parabolic SAR (PSAR)</h2>
<div class="card">
<p><strong>Toggle:</strong> Indicators &rarr; PSAR &nbsp;|&nbsp; <strong>Configurable:</strong> AF Start (0.02), AF Increment (0.02), AF Max (0.2)</p>
<h4>How It Works</h4>
<p>PSAR places dots above or below price that accelerate toward the price over time:</p>
<ul>
<li><strong>Dots below price</strong> = <span class="tag tag-buy">BULLISH</span> — trend is up, SAR acts as trailing stop</li>
<li><strong>Dots above price</strong> = <span class="tag tag-sell">BEARISH</span> — trend is down</li>
<li>Acceleration Factor (AF) starts at 0.02 and increases by 0.02 each bar the extreme price makes a new high/low, capped at 0.2</li>
</ul>
<h4>Usage</h4>
<p>Use PSAR as a trailing stop-loss. When dots flip from above to below = potential buy entry. Best in trending markets; generates many false signals in ranges.</p>
</div>

<h2>3. Support / Resistance (S/R) Levels</h2>
<div class="card">
<p><strong>Toggle:</strong> Indicators &rarr; S/R Levels</p>
<h4>How It Works</h4>
<ul>
<li>Identifies <strong>swing highs</strong> (pivot highs) and <strong>swing lows</strong> (pivot lows) using 2-bar lookback</li>
<li>Clusters nearby levels within <strong>0.3% tolerance</strong> into single zones</li>
<li>Displays horizontal lines at clustered support and resistance levels</li>
</ul>
<h4>Usage</h4>
<p>Support levels = potential bounce zones (buy). Resistance levels = potential rejection zones (sell). Breakouts through S/R levels often lead to strong moves.</p>
</div>

<h2>4. EMA 9/21</h2>
<div class="card">
<p><strong>Toggle:</strong> Indicators &rarr; EMA 9/21</p>
<h4>How It Works</h4>
<ul>
<li><strong>EMA 9</strong> (yellow) = fast moving average, reacts quickly to price changes</li>
<li><strong>EMA 21</strong> (yellow, darker) = slow moving average, smooths out noise</li>
<li>EMA formula: k = 2/(period+1); EMA = Close &times; k + EMA_prev &times; (1-k)</li>
</ul>
<h4>Signals</h4>
<ul>
<li><strong>Golden Cross</strong>: EMA 9 crosses above EMA 21 = <span class="tag tag-buy">BULLISH</span></li>
<li><strong>Death Cross</strong>: EMA 9 crosses below EMA 21 = <span class="tag tag-sell">BEARISH</span></li>
<li>When both EMAs are sloping up with EMA9 &gt; EMA21 = strong uptrend</li>
</ul>
</div>

<h2>5. VWAP (Volume Weighted Average Price)</h2>
<div class="card">
<p><strong>Toggle:</strong> Indicators &rarr; VWAP</p>
<h4>How It Works</h4>
<ul>
<li>VWAP = Cumulative (Typical Price &times; Volume) / Cumulative Volume</li>
<li>Typical Price = (High + Low + Close) / 3</li>
<li>Resets at the start of each trading day session</li>
</ul>
<h4>Usage</h4>
<ul>
<li>Price <strong>above VWAP</strong> = buyers in control, bullish bias</li>
<li>Price <strong>below VWAP</strong> = sellers in control, bearish bias</li>
<li>VWAP acts as a magnet — price tends to return to VWAP</li>
<li>Institutional traders use VWAP as benchmark for execution quality</li>
</ul>
</div>

<h2>6. Bollinger Bands (BB)</h2>
<div class="card">
<p><strong>Toggle:</strong> Indicators &rarr; Bollinger Bands &nbsp;|&nbsp; <strong>Configurable:</strong> Period (20), Std Dev (2.0)</p>
<h4>How It Works</h4>
<ul>
<li><strong>Middle Band</strong> = 20-period SMA</li>
<li><strong>Upper Band</strong> = SMA + 2 &times; Standard Deviation</li>
<li><strong>Lower Band</strong> = SMA - 2 &times; Standard Deviation</li>
<li>~95% of price action stays within the bands</li>
</ul>
<h4>Signals</h4>
<ul>
<li><strong>BB Squeeze</strong>: Bands narrowing = low volatility, breakout imminent</li>
<li><strong>BB Walk</strong>: Price riding upper/lower band = strong trend</li>
<li><strong>BB Bounce</strong>: Price touching lower band then reversing = potential buy</li>
</ul>
</div>

<h2>7. CPR (Central Pivot Range)</h2>
<div class="card">
<p><strong>Toggle:</strong> Indicators &rarr; CPR</p>
<h4>How It Works</h4>
<ul>
<li><strong>Pivot</strong> = (Previous High + Previous Low + Previous Close) / 3</li>
<li><strong>BC (Bottom Central)</strong> = (Previous High + Previous Low) / 2</li>
<li><strong>TC (Top Central)</strong> = 2 &times; Pivot - BC</li>
</ul>
<h4>Usage</h4>
<ul>
<li><strong>Narrow CPR</strong> = trending day expected (price will break out)</li>
<li><strong>Wide CPR</strong> = range-bound day expected</li>
<li>Price above TC = bullish; Price below BC = bearish; Between TC and BC = neutral</li>
</ul>
</div>

<h2>8. Liquidity Pools</h2>
<div class="card">
<p><strong>Toggle:</strong> Indicators &rarr; Liquidity Pools</p>
<h4>How It Works (Smart Money Concept)</h4>
<ul>
<li>Scans for <strong>equal highs</strong> (Buy-Side Liquidity / BSL) and <strong>equal lows</strong> (Sell-Side Liquidity / SSL)</li>
<li>Equal = within 0.2% tolerance over 10-bar lookback</li>
<li>&ge;2 equal highs/lows forms a liquidity pool</li>
</ul>
<h4>Usage</h4>
<ul>
<li><strong>BSL</strong> (yellow dashed above) = Retail stop losses above equal highs — institutions sweep these before reversing down</li>
<li><strong>SSL</strong> (yellow dashed below) = Retail stop losses below equal lows — institutions sweep these before reversing up</li>
</ul>
</div>

<h2>9. Fair Value Gap (FVG)</h2>
<div class="card">
<p><strong>Toggle:</strong> Indicators &rarr; Fair Value Gap</p>
<h4>How It Works (Smart Money Concept)</h4>
<ul>
<li>A 3-candle pattern where the middle candle creates a gap between the first and third candle ranges</li>
<li><strong>Bullish FVG</strong>: Candle[i-2].high &lt; Candle[i].low — gap up, institutions buying</li>
<li><strong>Bearish FVG</strong>: Candle[i-2].low &gt; Candle[i].high — gap down, institutions selling</li>
</ul>
<h4>Usage</h4>
<p>Price tends to return to fill FVGs. Bullish FVG = potential buy zone when price retraces. Bearish FVG = potential sell zone.</p>
</div>

<h2>10. Break of Structure (BOS)</h2>
<div class="card">
<p><strong>Toggle:</strong> Indicators &rarr; Break of Structure</p>
<h4>How It Works (Smart Money Concept)</h4>
<ul>
<li>Tracks swing highs and swing lows to determine market structure</li>
<li><strong>Bullish BOS</strong>: Price breaks above a previous swing high → uptrend continuation</li>
<li><strong>Bearish BOS</strong>: Price breaks below a previous swing low → downtrend continuation</li>
</ul>
<h4>Usage</h4>
<p>BOS confirms the trend direction. Look for entries in pullbacks after a BOS in the trend direction.</p>
</div>

<h2>11. Change of Character (CHoCH)</h2>
<div class="card">
<p><strong>Toggle:</strong> Indicators &rarr; Change of Character</p>
<h4>How It Works (Smart Money Concept)</h4>
<ul>
<li>Detects when market structure <strong>reverses</strong> (opposite of BOS)</li>
<li><strong>Bullish CHoCH</strong>: In a downtrend, price breaks above a swing high → potential reversal to uptrend</li>
<li><strong>Bearish CHoCH</strong>: In an uptrend, price breaks below a swing low → potential reversal to downtrend</li>
</ul>
<h4>Usage</h4>
<p>CHoCH is an early reversal signal. Wait for confirmation (pullback + continuation) before entering.</p>
</div>

<h2>12. Cumulative Volume Delta (CVD)</h2>
<div class="card">
<p><strong>Toggle:</strong> Indicators &rarr; Cum. Volume Delta</p>
<h4>How It Works</h4>
<ul>
<li>Estimates buying vs selling volume per candle: buy_ratio = (Close - Low) / (High - Low)</li>
<li>Buy Volume = Total Volume &times; buy_ratio; Sell Volume = Total Volume &times; (1 - buy_ratio)</li>
<li>Delta = Buy Volume - Sell Volume; CVD = Running total of deltas</li>
</ul>
<h4>Usage</h4>
<ul>
<li><strong>Rising CVD + Rising Price</strong> = Healthy uptrend (buyers in control)</li>
<li><strong>Falling CVD + Rising Price</strong> = Bearish divergence (hidden selling, potential reversal)</li>
<li><strong>Rising CVD + Falling Price</strong> = Bullish divergence (hidden buying, potential reversal)</li>
</ul>
</div>

<h2>13. Volume Profile</h2>
<div class="card">
<p><strong>Toggle:</strong> Indicators &rarr; Volume Profile</p>
<h4>How It Works</h4>
<ul>
<li>Distributes total volume across 24 price bins covering the visible price range</li>
<li><strong>POC (Point of Control)</strong> = Price level with the highest traded volume (solid orange line, labeled)</li>
<li><strong>VAH (Value Area High)</strong> = Upper boundary of the value area (solid green line, labeled)</li>
<li><strong>VAL (Value Area Low)</strong> = Lower boundary of the value area (solid red line, labeled)</li>
<li><strong>Value Area</strong> = Range of prices containing 70% of total volume (semi-transparent orange)</li>
</ul>
<h4>Usage</h4>
<ul>
<li>POC acts as a magnet — price tends to spend time around the POC</li>
<li><strong>VAH</strong> acts as resistance — price breaking above VAH often leads to upside continuation</li>
<li><strong>VAL</strong> acts as support — price breaking below VAL often leads to downside continuation</li>
<li><strong>High Volume Nodes</strong> = Support/resistance zones (price consolidation areas)</li>
<li><strong>Low Volume Nodes</strong> = Price moves quickly through these levels</li>
<li>Breakout from Value Area (above VAH or below VAL) often leads to trending moves</li>
</ul>
</div>

<h2>14. Signals</h2>
<div class="card">
<p><strong>Toggle:</strong> Indicators &rarr; Signals (ON by default)</p>
<h4>How It Works</h4>
<ul>
<li>Displays BUY/SELL markers on the chart generated by the selected algorithm(s)</li>
<li>Green arrows up = BUY signals; Red arrows down = SELL signals</li>
<li>Larger arrows = STRONG signals (higher confidence)</li>
<li>Hover over markers to see signal details: score, type, and contributing reasons</li>
</ul>
</div>

<h2>Indicator Settings</h2>
<div class="card">
<p>Click <strong>&ldquo;&#9881; Indicator Settings&rdquo;</strong> at the bottom of the Indicators dropdown to configure:</p>
<table>
<tr><th>Indicator</th><th>Parameter</th><th>Default</th><th>Range</th></tr>
<tr><td>SuperTrend</td><td>Period</td><td>10</td><td>1 - 50</td></tr>
<tr><td>SuperTrend</td><td>Multiplier</td><td>3.0</td><td>0.1 - 10</td></tr>
<tr><td>Parabolic SAR</td><td>AF Start</td><td>0.02</td><td>0.001 - 0.1</td></tr>
<tr><td>Parabolic SAR</td><td>AF Increment</td><td>0.02</td><td>0.001 - 0.1</td></tr>
<tr><td>Parabolic SAR</td><td>AF Max</td><td>0.2</td><td>0.01 - 0.5</td></tr>
<tr><td>Bollinger Bands</td><td>Period</td><td>20</td><td>5 - 100</td></tr>
<tr><td>Bollinger Bands</td><td>Std Dev</td><td>2.0</td><td>0.5 - 5.0</td></tr>
</table>
<p>Click <strong>Apply</strong> to reload the chart with new settings. Click <strong>Restore Defaults</strong> to reset all parameters.</p>
</div>

</div></body></html>"""


HELP_MANUAL_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Mangal View - User Manual</title>""" + HELP_PAGE_STYLE + r"""
</head><body>
<div class="help-header">
  <div><h1>&#128214; User Manual</h1><a href="/">&larr; Back to Chart</a></div>
  <button class="download-btn" onclick="downloadPDF()">&#128196; Download PDF</button>
</div>
<div class="help-body">

<h2>Overview</h2>
<div class="card">
<p><strong>Mangal View</strong> is a professional-grade, real-time charting and algorithmic signal platform for Indian and global markets. It provides:</p>
<ul>
<li>Interactive candlestick charts with 13 timeframes</li>
<li>14 technical indicators (trend, SMC, volume, statistical)</li>
<li>14 algorithmic signal engines + 1 ML prediction model</li>
<li>Strategy backtesting with comprehensive performance metrics</li>
<li>Paper trading simulator</li>
<li>Real trading integration (Delta Exchange)</li>
<li>3 data sources: Yahoo Finance, TradingView, NSE India</li>
</ul>
</div>

<h2>Getting Started</h2>
<h3>Registration &amp; Login</h3>
<div class="card">
<ol>
<li>Visit the platform URL and click <strong>Register</strong></li>
<li>Enter your Username, Mobile Number (10 digits), Password, Place, and select a Plan</li>
<li>Plans: <strong>Free Trial</strong> (1 month evaluation) or <strong>Paid</strong> (&#8377;100/month)</li>
<li>After registration, log in with your Mobile Number and Password</li>
</ol>
</div>

<h2>Chart Interface</h2>
<h3>Symbol Selection</h3>
<div class="card">
<p>The symbol dropdown (top-left) provides 16 preset symbols:</p>
<table>
<tr><th>Category</th><th>Symbols</th></tr>
<tr><td>Indian Indices</td><td>NIFTY 50, BANK NIFTY, SENSEX</td></tr>
<tr><td>Precious Metals</td><td>Gold Futures, Silver Futures, XAU/USD, XAG/USD, Gold ETF, Silver ETF</td></tr>
<tr><td>Energy</td><td>Crude Oil, Natural Gas</td></tr>
<tr><td>Crypto</td><td>Bitcoin, Ethereum</td></tr>
<tr><td>US Indices</td><td>Dow Jones, NASDAQ, S&amp;P 500</td></tr>
</table>
</div>

<h3>Search</h3>
<div class="card">
<p>Use the search box to find any stock, index, or ETF by <strong>company name or ticker</strong>:</p>
<ul>
<li>Type a company name (e.g., "Reliance", "Tata Motors", "Infosys")</li>
<li>Or type a ticker directly (e.g., "RELIANCE.NS", "TCS.BO")</li>
<li>Results show up to 6 matches with ticker, name, and exchange</li>
<li>Click a result or press Enter to load the chart</li>
<li>Indian stocks automatically get .NS (NSE) or .BO (BSE) suffix</li>
</ul>
</div>

<h3>Timeframes</h3>
<div class="card">
<p>Click the <strong>Period</strong> dropdown to select from 13 timeframes:</p>
<table>
<tr><th>Timeframe</th><th>Data Period</th><th>Best For</th></tr>
<tr><td>1m, 2m, 3m</td><td>1-5 days</td><td>Scalping</td></tr>
<tr><td>5m, 10m, 15m</td><td>5-10 days</td><td>Intraday trading</td></tr>
<tr><td>30m, 1H, 2H, 4H</td><td>10-60 days</td><td>Swing trading</td></tr>
<tr><td>1D, 1W, 1M</td><td>1 year - max</td><td>Position trading / investing</td></tr>
</table>
</div>

<h3>Chart Interactions</h3>
<div class="card">
<ul>
<li><strong>Crosshair:</strong> Move mouse over chart to see OHLCV values in the legend</li>
<li><strong>Zoom:</strong> Use the Zoom dropdown or mouse scroll wheel</li>
<li><strong>Pan:</strong> Click and drag to move the chart</li>
<li><strong>LIVE mode:</strong> Click the LIVE button to enable continuous auto-refresh (every few seconds)</li>
</ul>
</div>

<h2>Indicators</h2>
<div class="card">
<p>Click the <strong>Indicators</strong> dropdown to toggle any of the 14 indicators:</p>
<ul>
<li><strong>Trend:</strong> SuperTrend, PSAR, EMA 9/21, VWAP</li>
<li><strong>Volatility:</strong> Bollinger Bands</li>
<li><strong>Levels:</strong> S/R Levels, CPR</li>
<li><strong>Smart Money (SMC):</strong> Liquidity Pools, Fair Value Gap, Break of Structure, Change of Character</li>
<li><strong>Volume:</strong> Cum. Volume Delta, Volume Profile</li>
<li><strong>Signals:</strong> Buy/Sell markers from selected algo(s)</li>
</ul>
<p>Open <strong>&#9881; Indicator Settings</strong> to customize SuperTrend, PSAR, and Bollinger Bands parameters. Click <strong>Restore Defaults</strong> to reset.</p>
<p>See <a href="/help/indicators">Indicator Documentation</a> for detailed explanations.</p>
</div>

<h2>Algorithms (Algos)</h2>
<div class="card">
<p>Click the <strong>Algo</strong> dropdown to select one or more signal algorithms. Multi-select is supported — click multiple algos to combine them.</p>
<p>Available: Trend, MStreet, MFactor, Sniper, OrderFlow, PriceAction, Breakout, Momentum, Scalping, SmartMoney, Quant, Hybrid, StatArb, Institution, MPredict</p>
<p>When multiple algos are selected, signals are combined and deduplicated — the signal with the highest absolute score is kept for each time bar.</p>
<p>See <a href="/help/algos">Algo Documentation</a> for detailed explanations of each algorithm.</p>
</div>

<h3>Signal Analysis</h3>
<div class="card">
<p>Click <strong>&#9889; Signal Analysis</strong> in the Algo dropdown to open the Signal Analysis panel:</p>
<ul>
<li><strong>Verdict:</strong> Overall BUY / SELL / NEUTRAL recommendation</li>
<li><strong>Score:</strong> Composite score from all contributing indicators</li>
<li><strong>Per-Indicator Status:</strong> Individual indicator readings</li>
<li><strong>Signal Count:</strong> Total buy/sell signals generated</li>
</ul>
</div>

<h2>Data Sources</h2>
<div class="card">
<p>Click the <strong>&#9881; Settings</strong> gear icon, then expand <strong>Data Source</strong>:</p>
<table>
<tr><th>Source</th><th>Description</th><th>Pros</th><th>Cons</th></tr>
<tr><td><strong>TradingView</strong> (default)</td><td>Near real-time via WebSocket</td><td>Fast, reliable, all symbols</td><td>Unofficial API</td></tr>
<tr><td><strong>Yahoo Finance</strong></td><td>Official yfinance library</td><td>Official, all symbols</td><td>Slight delay, rate limits</td></tr>
<tr><td><strong>NSE India</strong></td><td>Direct NSE API</td><td>Official Indian exchange data</td><td>Only NIFTY 50 &amp; BANK NIFTY, empty after 3:30 PM</td></tr>
</table>
</div>

<h2>Backtesting</h2>
<div class="card">
<p>Test any algorithm's historical performance:</p>
<ol>
<li>Open <strong>&#9881; Settings</strong> → toggle <strong>Backtest</strong> ON</li>
<li>Select an algorithm from the backtest list</li>
<li>The Strategy Tester panel opens with 3 tabs:</li>
</ol>
<h4>Overview Tab</h4>
<ul>
<li>Net Profit / Loss, Total Trades, Win Rate</li>
<li>Profit Factor, Sharpe Ratio, Max Drawdown</li>
<li>Average Win / Loss, Payoff Ratio</li>
</ul>
<h4>Performance Tab</h4>
<p>Visual equity curve showing capital growth over time</p>
<h4>Trade List Tab</h4>
<p>Detailed list of every trade: entry/exit time, price, P&amp;L, type</p>
<p><strong>Qty Setting:</strong> Adjust trade quantity per signal (0 = auto-size from &#8377;1,00,000 initial capital)</p>
</div>

<h2>Paper Trading</h2>
<div class="card">
<p>Practice trading without real money:</p>
<ol>
<li>Open <strong>&#9881; Settings</strong> → expand <strong>Trade</strong> → click <strong>Futures</strong></li>
<li>The Trading Panel opens:</li>
</ol>
<ul>
<li><strong>Symbol:</strong> Select from dropdown (all 16 preset symbols)</li>
<li><strong>Capital:</strong> Starting virtual capital</li>
<li><strong>Algorithm:</strong> Select the signal algorithm to follow</li>
<li><strong>Start/Stop:</strong> Begin or end the paper trading session</li>
<li><strong>Live P&amp;L:</strong> Real-time profit/loss display</li>
<li><strong>Positions:</strong> Current open positions</li>
<li><strong>Trade Log:</strong> Historical trade list</li>
</ul>
</div>

<h2>Real Trading (Delta Exchange)</h2>
<div class="card">
<p>Connect to Delta Exchange for automated real trading:</p>
<ol>
<li>Open <strong>&#9881; Settings</strong> → expand <strong>Real Trade</strong> → click <strong>Delta</strong></li>
<li>Enter your Delta Exchange credentials:</li>
</ol>
<ul>
<li><strong>Username &amp; Password:</strong> Your Delta Exchange login</li>
<li><strong>Capital:</strong> Trading capital allocation</li>
<li><strong>Qty:</strong> Position size per trade (0 = auto)</li>
<li><strong>Symbol:</strong> Trading instrument</li>
<li><strong>SL %:</strong> Stop loss percentage</li>
<li><strong>Target %:</strong> Take profit percentage</li>
<li><strong>Mode:</strong> Signals (auto-follow algo signals) or Manual</li>
</ul>
<p>&#9888; <strong>Warning:</strong> Real trading involves actual money. Use paper trading first to validate your strategy.</p>
</div>

<h2>Zoom Controls</h2>
<div class="card">
<ul>
<li><strong>H+</strong> / <strong>H-</strong>: Horizontal zoom (time axis)</li>
<li><strong>V+</strong> / <strong>V-</strong>: Vertical zoom (price axis)</li>
<li><strong>Reset / Fit All</strong>: Reset zoom to show all data</li>
<li>Mouse scroll wheel also zooms horizontally</li>
</ul>
</div>

<h2>OHLC Legend</h2>
<div class="card">
<p>The top-left overlay shows real-time values as you move the crosshair:</p>
<ul>
<li><strong>O</strong> = Open price</li>
<li><strong>H</strong> = High price</li>
<li><strong>L</strong> = Low price</li>
<li><strong>C</strong> = Close price</li>
<li><strong>Vol</strong> = Volume</li>
<li><strong>ST</strong> = SuperTrend value (when enabled)</li>
<li><strong>PSAR</strong> = Parabolic SAR value (when enabled)</li>
</ul>
</div>

<h2>Keyboard &amp; Mouse</h2>
<div class="card">
<table>
<tr><th>Action</th><th>How</th></tr>
<tr><td>Pan chart</td><td>Click + drag</td></tr>
<tr><td>Zoom in/out</td><td>Mouse scroll wheel</td></tr>
<tr><td>Search symbol</td><td>Type in search box + Enter</td></tr>
<tr><td>Signal tooltip</td><td>Hover over buy/sell arrow markers</td></tr>
</table>
</div>

<h2>Tips &amp; Best Practices</h2>
<div class="card">
<ol>
<li><strong>Start with defaults:</strong> Trend + MStreet on 5m TradingView gives a good starting point</li>
<li><strong>Multi-algo:</strong> Enable 2-3 complementary algos for stronger confirmation</li>
<li><strong>Backtest first:</strong> Always backtest an algorithm on your target symbol before trading</li>
<li><strong>Match algo to market:</strong> Use Trend/Momentum in trending markets, MStreet/Scalping in ranges</li>
<li><strong>Use indicators wisely:</strong> Don't enable all indicators at once — pick 2-3 relevant ones</li>
<li><strong>Signal Analysis:</strong> Check the Signal Analysis panel for a quick verdict before taking a trade</li>
<li><strong>Paper trade:</strong> Practice with the paper trading feature before going live</li>
</ol>
</div>

</div></body></html>"""


@app.route("/help/algos")
@login_required
def help_algos():
    return Response(HELP_ALGOS_PAGE, content_type="text/html")


@app.route("/help/indicators")
@login_required
def help_indicators():
    return Response(HELP_INDICATORS_PAGE, content_type="text/html")


@app.route("/help/manual")
@login_required
def help_manual():
    return Response(HELP_MANUAL_PAGE, content_type="text/html")


@app.route("/")
@login_required
def index():
    """Serve the main HTML page containing the interactive TradingView-style chart.

    Returns the full single-page application including embedded CSS, HTML layout
    (toolbar, chart container, panels), and JavaScript (chart initialization,
    indicator rendering, signal engine UI, backtest panel, live data feed).
    The HTML is stored in the HTML_PAGE raw string constant.

    Returns:
        Response: HTML page with content-type text/html.
    """
    return Response(HTML_PAGE, content_type="text/html")


@app.route("/api/candles")
@login_required
def api_candles():
    """Main API endpoint — fetch OHLCV data, compute all indicators, and return JSON.

    Accepts query parameters for timeframe, symbol, data source, indicator
    settings, and backtest configuration. Fetches candle data from the selected
    source (Yahoo Finance, TradingView, or NSE), computes all technical
    indicators (SuperTrend, PSAR, S/R, EMA, VWAP, RSI, MACD, Bollinger Bands,
    CPR, Liquidity Pools, FVG, BOS/CHoCH, CVD), generates composite signals,
    and runs a strategy backtest.

    Query Parameters:
        interval (str): Timeframe ('3m','5m','15m','1h','1d'). Default '5m'.
        symbol (str): SYMBOL_MAP key or raw ticker. Default 'NIFTY50'.
        source (str): Data source ('yahoo','tradingview','nse'). Default 'yahoo'.
        st_period (int): SuperTrend ATR period (1-50). Default 10.
        st_multiplier (float): SuperTrend multiplier (0.1-10). Default 3.0.
        sar_start (float): PSAR initial AF (0.001-0.1). Default 0.02.
        sar_inc (float): PSAR AF increment (0.001-0.1). Default 0.02.
        sar_max (float): PSAR max AF (0.01-0.5). Default 0.2.
        bb_period (int): Bollinger Bands period (5-100). Default 20.
        bb_stddev (float): Bollinger Bands std dev (0.5-5). Default 2.0.
        bt_qty (int): Backtest trade quantity (0=auto). Default 0.

    Returns:
        JSON: Object with candles, supertrend, parabolicSAR, supportResistance,
            ema9, ema21, vwap, rsi, macd, patterns, signals, signalSummary,
            cpr, bollingerBands, liquidityPools, fairValueGaps, bosChoch,
            cvd, backtest.
    """
    interval = request.args.get("interval", "5m")
    if interval not in INTERVAL_MAP:
        interval = "5m"

    symbol = request.args.get("symbol", "NIFTY50")

    # SuperTrend params
    st_period = request.args.get("st_period", 10, type=int)
    st_multiplier = request.args.get("st_multiplier", 3.0, type=float)
    st_period = max(1, min(st_period, 50))
    st_multiplier = max(0.1, min(st_multiplier, 10.0))

    # Parabolic SAR params
    sar_start = request.args.get("sar_start", 0.02, type=float)
    sar_inc = request.args.get("sar_inc", 0.02, type=float)
    sar_max = request.args.get("sar_max", 0.2, type=float)
    sar_start = max(0.001, min(sar_start, 0.1))
    sar_inc = max(0.001, min(sar_inc, 0.1))
    sar_max = max(0.01, min(sar_max, 0.5))

    # Bollinger Bands params
    bb_period = request.args.get("bb_period", 20, type=int)
    bb_stddev = request.args.get("bb_stddev", 2.0, type=float)
    bb_period = max(5, min(bb_period, 100))
    bb_stddev = max(0.5, min(bb_stddev, 5.0))

    # Data source
    source = request.args.get("source", "yahoo")
    if source == "tradingview":
        candles = fetch_tradingview_data(interval, symbol)
    elif source == "nse":
        candles = fetch_nse_data(interval, symbol)
    else:
        candles = fetch_nifty_data(interval, symbol)

    supertrend = compute_supertrend(candles, st_period, st_multiplier)
    psar = compute_parabolic_sar(candles, sar_start, sar_inc, sar_max)
    sr = compute_support_resistance(candles)
    rsi_data = compute_rsi(candles)
    macd_data = compute_macd(candles)
    vwap_data = compute_vwap(candles)
    ema9 = compute_ema_series(candles, 9)
    ema21 = compute_ema_series(candles, 21)
    patterns = detect_candlestick_patterns(candles)
    cpr = compute_cpr(candles)
    bb = compute_bollinger_bands(candles, bb_period, bb_stddev)
    liquidity_pools = compute_liquidity_pools(candles)
    fvg = compute_fair_value_gaps(candles)
    bos_choch = compute_bos_choch(candles)
    cvd = compute_cvd(candles)
    volume_profile = compute_volume_profile(candles)

    algo_param = request.args.get("algo", "trend")
    algos = [a.strip() for a in algo_param.split(",") if a.strip()]
    # Remove mpredict from signal algos (it only controls predictions)
    signal_algos = [a for a in algos if a != "mpredict"]

    all_signals = []
    summaries = {}
    for algo in signal_algos:
        if algo == "mstreet":
            sigs, summ = generate_janestreet_signals(
                candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr
            )
        elif algo == "mfactor":
            sigs, summ = generate_accurate_signals(
                candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr
            )
        elif algo == "sniper":
            sigs, summ = generate_sniper_signals(
                candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr
            )
        elif algo == "orderflow":
            sigs, summ = generate_orderflow_signals(
                candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr
            )
        elif algo == "priceaction":
            sigs, summ = generate_priceaction_signals(
                candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr
            )
        elif algo == "breakout":
            sigs, summ = generate_breakout_signals(
                candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr
            )
        elif algo == "momentum":
            sigs, summ = generate_momentum_signals(
                candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr
            )
        elif algo == "scalping":
            sigs, summ = generate_scalping_signals(
                candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr
            )
        elif algo == "smartmoney":
            sigs, summ = generate_smartmoney_signals(
                candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr
            )
        elif algo == "quant":
            sigs, summ = generate_quant_signals(
                candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr
            )
        elif algo == "hybrid":
            sigs, summ = generate_hybrid_signals(
                candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr
            )
        elif algo == "statarb":
            sigs, summ = generate_statarb_signals(
                candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr
            )
        elif algo == "institution":
            sigs, summ = generate_institution_signals(
                candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr
            )
        else:  # trend (default)
            sigs, summ = generate_signals(
                candles, supertrend, psar, rsi_data, macd_data,
                vwap_data, ema9, ema21, patterns, sr
            )
        for _sig in sigs:
            _sig["algo"] = algo
        all_signals.extend(sigs)
        summaries[algo] = summ

    # Deduplicate signals by time — keep the one with highest absolute score
    seen = {}
    for s in all_signals:
        t = s["time"]
        if t not in seen or abs(s.get("score", 0)) > abs(seen[t].get("score", 0)):
            seen[t] = s
    signals = sorted(seen.values(), key=lambda x: x["time"])

    bt_qty = request.args.get("bt_qty", 0, type=int)
    backtest = run_backtest(candles, signals, bt_qty)

    # ML Predictions — only if mpredict is selected
    predictions = []
    if "mpredict" in algos:
        try:
            predictions = predict_next_candles(candles, interval, n_predict=5)
        except Exception:
            predictions = []

    return jsonify({
        "candles": candles,
        "supertrend": supertrend,
        "parabolicSAR": psar,
        "supportResistance": sr,
        "ema9": ema9,
        "ema21": ema21,
        "vwap": vwap_data,
        "rsi": rsi_data,
        "macd": macd_data,
        "patterns": patterns,
        "signals": signals,
        "signalSummary": summaries,
        "cpr": cpr,
        "bollingerBands": bb,
        "liquidityPools": liquidity_pools,
        "fairValueGaps": fvg,
        "bosChoch": bos_choch,
        "cvd": cvd,
        "volumeProfile": volume_profile,
        "backtest": backtest,
        "predictions": predictions,
    })


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Nifty 50 - Live Chart</title>
<style>
  :root {
    --bg-primary: #131722;
    --bg-secondary: #1e222d;
    --bg-tertiary: #2a2e39;
    --text-primary: #d1d4dc;
    --text-secondary: #787b86;
    --text-white: #fff;
    --border-color: #2a2e39;
    --accent: #2962ff;
    --chart-bg: #131722;
    --input-bg: #131722;
    --panel-bg: #1e222d;
    --hover-bg: #252a37;
  }
  html.light-theme {
    --bg-primary: #ffffff;
    --bg-secondary: #f0f3fa;
    --bg-tertiary: #e0e3eb;
    --text-primary: #131722;
    --text-secondary: #787b86;
    --text-white: #131722;
    --border-color: #e0e3eb;
    --accent: #2962ff;
    --chart-bg: #ffffff;
    --input-bg: #f0f3fa;
    --panel-bg: #f0f3fa;
    --hover-bg: #e8ebf2;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: var(--bg-primary);
    color: var(--text-primary);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif;
    overflow: hidden;
    height: 100vh;
  }
  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 20px;
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border-color);
  }
  .header-left { display: flex; align-items: center; gap: 16px; }
  .ticker-name { font-size: 20px; font-weight: 700; color: var(--text-white); letter-spacing: 0.5px; }
  .ticker-exchange { font-size: 12px; color: var(--text-secondary); font-weight: 400; }
  /* Symbol Selector */
  .symbol-select {
    padding: 6px 12px; background: var(--input-bg); border: 1px solid var(--border-color);
    border-radius: 4px; color: var(--text-white); font-size: 14px; font-weight: 600;
    cursor: pointer; outline: none; appearance: none;
    -webkit-appearance: none; -moz-appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%23787b86'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: right 10px center;
    padding-right: 28px; min-width: 150px;
  }
  .symbol-select:hover { border-color: #2962ff; }
  .symbol-select:focus { border-color: #2962ff; }
  .symbol-select option { background: var(--bg-secondary); color: var(--text-primary); }
  /* Search Box */
  .search-wrap {
    position: relative;
  }
  .search-input {
    padding: 6px 12px; background: var(--input-bg); border: 1px solid var(--border-color);
    border-radius: 4px; color: var(--text-white); font-size: 13px; font-weight: 500;
    outline: none; width: 180px;
  }
  .search-input::placeholder { color: #555; }
  .search-input:focus { border-color: #2962ff; }
  .search-result {
    position: absolute; top: 100%; left: 0; width: 280px; max-height: 200px;
    overflow-y: auto; background: var(--bg-secondary); border: 1px solid var(--border-color);
    border-radius: 4px; z-index: 1000; margin-top: 2px; display: none;
  }
  .search-result-item {
    padding: 8px 12px; cursor: pointer; font-size: 13px; color: var(--text-primary);
    border-bottom: 1px solid var(--border-color);
  }
  .search-result-item:hover { background: var(--bg-tertiary); }
  .search-result-item .sr-ticker { font-weight: 700; color: var(--text-white); }
  .search-result-item .sr-name { color: var(--text-secondary); font-size: 11px; margin-left: 8px; }
  .search-result-item .sr-exch { color: var(--text-secondary); font-size: 10px; float: right; }
  .price-info { display: flex; align-items: baseline; gap: 10px; }
  .current-price { font-size: 22px; font-weight: 700; }
  .price-change { font-size: 14px; font-weight: 500; }
  .positive { color: #26a69a; }
  .negative { color: #ef5350; }
  .toolbar {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 6px 20px;
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border-color);
    flex-wrap: wrap;
  }
  .tf-btn, .ind-btn {
    padding: 6px 14px;
    border: none;
    background: transparent;
    color: var(--text-secondary);
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    border-radius: 4px;
    transition: all 0.15s;
    letter-spacing: 0.3px;
  }
  .tf-btn:hover, .ind-btn:hover { background: var(--bg-tertiary); color: var(--text-primary); }
  .tf-btn.active { background: #2962ff; color: #fff; }
  /* Period Dropdown */
  .period-dropdown-wrapper { position: relative; }
  .period-dropdown {
    position: absolute; top: 100%; left: 0; background: var(--bg-secondary); border: 1px solid var(--border-color);
    border-radius: 6px; padding: 4px 0; min-width: 140px; z-index: 300;
    box-shadow: 0 8px 24px rgba(0,0,0,0.5); display: none;
  }
  .period-dropdown.open { display: block; }
  .period-item {
    display: block; padding: 8px 16px; color: var(--text-primary); font-size: 13px;
    cursor: pointer; transition: background 0.1s; border: none; background: none; width: 100%; text-align: left;
  }
  .period-item:hover { background: var(--bg-tertiary); }
  .period-item.active { color: #2962ff; font-weight: 600; }
  .ind-btn.active { background: #363a45; color: #fff; }
  .ind-btn .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; vertical-align: middle; }
  .separator { width: 1px; height: 20px; background: var(--border-color); margin: 0 8px; }
  /* Indicators Dropdown */
  .indicators-dropdown-wrapper { position: relative; }
  .indicators-dropdown {
    position: absolute; top: 100%; left: 0; z-index: 300;
    background: var(--bg-secondary); border: 1px solid var(--border-color); border-radius: 8px;
    padding: 6px 0; min-width: 200px; display: none;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5); margin-top: 4px;
  }
  .indicators-dropdown.open { display: block; }
  .ind-item {
    display: flex; align-items: center; gap: 8px; padding: 8px 14px;
    cursor: pointer; font-size: 13px; color: var(--text-primary); transition: background 0.12s;
    user-select: none;
  }
  .ind-item:hover { background: var(--bg-tertiary); }
  .ind-item .dot { flex-shrink: 0; display: inline-block; width: 8px; height: 8px; border-radius: 50%; }
  .ind-item span:nth-child(2) { flex: 1; }
  .ind-item input[type="checkbox"] {
    accent-color: #2962ff; width: 15px; height: 15px; cursor: pointer;
  }
  #chart-container {
    width: 100%;
    height: calc(100vh - 90px);
    position: relative;
  }
  .loading-overlay {
    position: absolute; top: 0; left: 0; right: 0; bottom: 0;
    background: var(--bg-primary, #131722);
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    z-index: 100; transition: opacity 0.3s;
  }
  .loading-overlay.hidden { opacity: 0; pointer-events: none; }
  .loading-text { color: var(--text-secondary, #787b86); font-size: 14px; margin-top: 16px; letter-spacing: 1px; }
  .loading-brand { color: var(--accent, #2962ff); font-size: 22px; font-weight: 700; margin-bottom: 12px; letter-spacing: 1px; }
  .spinner { width: 36px; height: 36px; border: 3px solid var(--bg-tertiary, #2a2e39); border-top-color: #2962ff; border-radius: 50%; animation: spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .ohlc-legend {
    position: absolute; top: 8px; left: 12px; z-index: 10;
    font-size: 12px; display: flex; gap: 12px; color: #787b86; pointer-events: none;
  }
  .ohlc-legend span { font-weight: 500; }
  .ohlc-val { color: #d1d4dc; }
  .chart-title {
    flex: 1; text-align: center;
    font-size: 16px; font-weight: 700; color: #d1d4dc;
    letter-spacing: 2px; text-transform: uppercase;
    white-space: nowrap;
  }
  .signal-tooltip {
    position: absolute; display: none; z-index: 200;
    background: var(--bg-secondary); border: 1px solid var(--border-color); border-radius: 6px;
    padding: 10px 14px; min-width: 200px; max-width: 300px;
    color: var(--text-primary); font-size: 12px; pointer-events: none;
    box-shadow: 0 4px 16px rgba(0,0,0,0.5);
  }
  .signal-tooltip .st-header {
    font-size: 13px; font-weight: 700; margin-bottom: 6px; padding-bottom: 4px;
    border-bottom: 1px solid #2a2e39;
  }
  .signal-tooltip .st-header.buy { color: #26a69a; }
  .signal-tooltip .st-header.sell { color: #ef5350; }
  .signal-tooltip .st-score { font-weight: 400; opacity: 0.8; }
  .signal-tooltip .st-row {
    display: flex; justify-content: space-between; padding: 2px 0;
    font-size: 11px; color: #787b86;
  }
  .signal-tooltip .st-row .st-reason { color: #d1d4dc; }
  .watermark {
    position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
    font-size: 48px; font-weight: 700; color: rgba(42, 46, 57, 0.5);
    pointer-events: none; z-index: 1; letter-spacing: 2px;
  }
  .indicator-legend {
    position: absolute; top: 24px; left: 12px; z-index: 10;
    font-size: 11px; display: flex; gap: 16px; color: #787b86; pointer-events: none;
  }
  .indicator-legend .il-st { color: #ff9800; }
  .indicator-legend .il-sar { color: #e040fb; }
  /* Settings Panel */
  .settings-panel {
    position: absolute; top: 44px; right: 12px; z-index: 200;
    background: var(--bg-secondary); border: 1px solid var(--border-color); border-radius: 8px;
    padding: 16px; width: 280px; display: none;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5);
  }
  .settings-panel.open { display: block; }
  .settings-panel h3 {
    font-size: 14px; color: var(--text-white); margin-bottom: 12px;
    border-bottom: 1px solid var(--border-color); padding-bottom: 8px;
  }
  .settings-panel label {
    display: flex; justify-content: space-between; align-items: center;
    font-size: 12px; color: var(--text-secondary); margin-bottom: 8px;
  }
  .settings-panel input[type="number"] {
    width: 70px; padding: 4px 8px; background: var(--input-bg); border: 1px solid var(--border-color);
    border-radius: 4px; color: var(--text-primary); font-size: 12px; text-align: right;
  }
  .settings-panel input[type="number"]:focus { outline: none; border-color: #2962ff; }
  .settings-panel .apply-btn {
    width: 100%; padding: 8px; background: #2962ff; color: #fff; border: none;
    border-radius: 4px; font-size: 13px; font-weight: 600; cursor: pointer;
    margin-top: 8px; transition: background 0.15s;
  }
  .settings-panel .apply-btn:hover { background: #1e53e5; }
  .settings-panel .section-title {
    font-size: 12px; font-weight: 600; color: var(--text-primary); margin: 10px 0 6px 0;
  }
  .gear-btn {
    padding: 6px 10px; border: none; background: transparent; color: var(--text-secondary);
    font-size: 16px; cursor: pointer; border-radius: 4px; transition: all 0.15s;
  }
  .gear-btn:hover { background: #2a2e39; color: #d1d4dc; }
  /* Settings Config Panel */
  .cfg-panel {
    position: absolute; top: 44px; right: 60px; z-index: 250;
    background: var(--bg-secondary); border: 1px solid var(--border-color); border-radius: 8px;
    width: 280px; display: none; box-shadow: 0 8px 32px rgba(0,0,0,0.6);
    max-height: calc(100vh - 100px); overflow-y: auto;
  }
  .cfg-panel.open { display: block; }
  .cfg-header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 12px 16px; border-bottom: 1px solid var(--border-color); position: sticky; top: 0;
    background: var(--bg-secondary); z-index: 1;
  }
  .cfg-header h3 { margin: 0; font-size: 14px; color: var(--text-primary); font-weight: 600; }
  .cfg-close {
    background: none; border: none; color: var(--text-secondary); font-size: 18px; cursor: pointer;
    padding: 0 4px; line-height: 1;
  }
  .cfg-close:hover { color: #ef5350; }
  .cfg-section { border-bottom: 1px solid var(--border-color); }
  .cfg-section:last-child { border-bottom: none; }
  .cfg-section-header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px 16px; cursor: default;
  }
  .cfg-section-header span { color: var(--text-primary); font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
  .cfg-toggle { position: relative; width: 36px; height: 20px; display: inline-block; flex-shrink: 0; }
  .cfg-toggle input { opacity: 0; width: 0; height: 0; }
  .cfg-slider {
    position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
    background: #3a3e4a; border-radius: 20px; transition: 0.2s;
  }
  .cfg-slider::before {
    content: ''; position: absolute; height: 14px; width: 14px; left: 3px; bottom: 3px;
    background: #787b86; border-radius: 50%; transition: 0.2s;
  }
  .cfg-toggle input:checked + .cfg-slider { background: #2962ff; }
  .cfg-toggle input:checked + .cfg-slider::before { transform: translateX(16px); background: #fff; }
  .cfg-section-body { display: none; padding: 4px 0 8px 0; }
  .cfg-section-body.open { display: block; }
  .cfg-item {
    display: block; padding: 7px 24px; color: var(--text-primary); font-size: 13px;
    cursor: pointer; transition: background 0.1s; border: none; background: none; width: 100%; text-align: left;
  }
  .cfg-item:hover:not(.disabled) { background: var(--bg-tertiary); }
  .cfg-item.disabled { color: #555; cursor: default; }
  .cfg-item.active { color: #2962ff; font-weight: 600; }
  .cfg-item.has-sub::after { content: '\25B6'; float: right; font-size: 10px; margin-top: 2px; }
  .cfg-item.has-sub.expanded::after { content: '\25BC'; }
  .cfg-sub { display: none; padding-left: 16px; background: #181c27; border-left: 2px solid #2962ff; margin-left: 16px; }
  .cfg-sub.open { display: block; }
  .cfg-sub-item {
    display: block; padding: 7px 16px; color: var(--text-primary); font-size: 13px;
    cursor: pointer; transition: background 0.1s; border: none; background: none; width: 100%; text-align: left;
  }
  .cfg-sub-item:hover { background: var(--bg-tertiary); }
  /* Live Data Button */
  .live-btn {
    padding: 6px 14px; border: 1px solid #2a2e39; background: transparent;
    color: #787b86; font-size: 13px; font-weight: 600; cursor: pointer;
    border-radius: 4px; transition: all 0.2s; display: flex; align-items: center; gap: 6px;
  }
  .live-btn:hover { background: #2a2e39; color: #d1d4dc; }
  .live-btn.active { background: rgba(239,83,80,0.15); color: #ef5350; border-color: #ef5350; }
  .live-dot {
    width: 8px; height: 8px; border-radius: 50%; background: #787b86; transition: background 0.2s;
  }
  .live-btn.active .live-dot { background: #ef5350; animation: livePulse 1s ease-in-out infinite; }
  @keyframes livePulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
  /* Zoom Controls */
  .zoom-group { display: flex; align-items: center; gap: 2px; }
  .zoom-label { font-size: 10px; color: #787b86; margin-right: 2px; letter-spacing: 0.5px; }
  .zoom-btn {
    width: 28px; height: 28px; border: 1px solid #2a2e39; background: transparent;
    color: #787b86; font-size: 16px; font-weight: 700; cursor: pointer;
    border-radius: 4px; transition: all 0.15s; display: flex; align-items: center; justify-content: center;
    line-height: 1;
  }
  .zoom-btn:hover { background: #2a2e39; color: #d1d4dc; }
  .zoom-btn:active { background: #363a45; }
  /* Zoom Dropdown */
  .zoom-dropdown-wrapper { position: relative; }
  .zoom-dropdown {
    position: absolute; top: 100%; left: 0; background: #1e222d; border: 1px solid #2a2e39;
    border-radius: 6px; padding: 4px 0; min-width: 200px; z-index: 300;
    box-shadow: 0 8px 24px rgba(0,0,0,0.5); display: none;
  }
  .zoom-dropdown.open { display: block; }
  .zm-item {
    display: block; padding: 8px 16px; color: #d1d4dc; font-size: 13px;
    cursor: pointer; transition: background 0.1s; border: none; background: none; width: 100%; text-align: left;
  }
  .zm-item:hover { background: #2a2e39; }

  /* Help Dropdown */
  .help-dropdown-wrapper { position: relative; }
  .help-dropdown {
    position: absolute; top: 100%; right: 0; background: #1e222d; border: 1px solid #2a2e39;
    border-radius: 6px; padding: 4px 0; min-width: 180px; z-index: 300;
    box-shadow: 0 8px 24px rgba(0,0,0,0.5); display: none;
  }
  .help-dropdown.open { display: block; }
  .help-item {
    display: block; padding: 8px 16px; color: #d1d4dc; font-size: 13px;
    cursor: pointer; transition: background 0.1s; text-decoration: none;
  }
  .help-item:hover { background: #2a2e39; }

  /* Trade Dropdown */
  .trade-dropdown-wrapper { position: relative; }
  .trade-dropdown {
    position: absolute; top: 100%; left: 0; background: #1e222d; border: 1px solid #2a2e39;
    border-radius: 6px; padding: 4px 0; min-width: 160px; z-index: 300;
    box-shadow: 0 8px 24px rgba(0,0,0,0.5); display: none;
  }
  .trade-dropdown.open { display: block; }
  .trade-item {
    display: block; padding: 8px 16px; color: #d1d4dc; font-size: 13px;
    cursor: pointer; transition: background 0.1s; border: none; background: none; width: 100%; text-align: left;
    position: relative;
  }
  .trade-item:hover { background: #2a2e39; }
  .trade-item.disabled { color: #555; cursor: default; }
  .trade-item.disabled:hover { background: none; }
  .trade-item.has-sub::after { content: '\25B6'; float: right; font-size: 10px; margin-top: 2px; }
  .trade-item.has-sub.expanded::after { content: '\25BC'; }
  .trade-sub {
    display: none; padding-left: 12px; background: #181c27;
    border-left: 2px solid #2962ff;
  }
  .trade-sub.open { display: block; }
  .trade-sub-item {
    display: block; padding: 8px 16px; color: #d1d4dc; font-size: 13px;
    cursor: pointer; transition: background 0.1s; border: none; background: none; width: 100%; text-align: left;
  }
  .trade-sub-item:hover { background: #2a2e39; }

  /* Trade Panels */
  .trade-panel, .trade-log-panel {
    position: absolute; top: 44px; right: 12px; z-index: 200;
    background: #1e222d; border: 1px solid #2a2e39; border-radius: 8px;
    padding: 0; width: 420px; display: none;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5); max-height: calc(100vh - 100px); overflow-y: auto;
  }
  .trade-panel.open, .trade-log-panel.open { display: block; }
  .tp-header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 12px 16px; border-bottom: 1px solid #2a2e39; cursor: move; user-select: none;
  }
  .tp-header h3 { font-size: 14px; color: #fff; margin: 0; }
  .tp-close { background: none; border: none; color: #787b86; font-size: 20px; cursor: pointer; }
  .tp-close:hover { color: #fff; }
  .tp-body { padding: 16px; }
  .tp-row { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
  .tp-row label { color: #787b86; font-size: 12px; min-width: 60px; }
  .tp-row select, .tp-row input[type=number] {
    flex: 1; padding: 6px 10px; background: #131722; border: 1px solid #2a2e39;
    border-radius: 4px; color: #d1d4dc; font-size: 13px;
  }
  .tp-algo { color: #787b86; font-size: 11px; margin-bottom: 12px; }
  .tp-start-btn {
    width: 100%; padding: 10px; border: none; border-radius: 6px; font-size: 14px;
    font-weight: 700; cursor: pointer; transition: background 0.2s;
  }
  .tp-start-btn.start { background: #26a69a; color: #fff; }
  .tp-start-btn.start:hover { background: #2bbd8e; }
  .tp-start-btn.stop { background: #ef5350; color: #fff; }
  .tp-start-btn.stop:hover { background: #ff6b68; }
  .tp-status {
    margin-top: 16px; padding: 12px; background: #131722; border-radius: 6px;
    border: 1px solid #2a2e39; display: none;
  }
  .tp-status.visible { display: block; }
  .tp-status-row {
    display: flex; justify-content: space-between; padding: 4px 0;
    font-size: 12px; color: #787b86;
  }
  .tp-status-row .val { color: #d1d4dc; font-weight: 600; }
  .tp-status-row .val.positive { color: #26a69a; }
  .tp-status-row .val.negative { color: #ef5350; }

  /* Real Trade Dropdown */
  .realtrade-dropdown-wrapper { position: relative; display: inline-block; }
  .realtrade-dropdown {
    position: absolute; top: 36px; left: 0; background: #23273a; border: 1px solid #2a2e39; border-radius: 8px;
    min-width: 160px; z-index: 210; display: none; box-shadow: 0 8px 32px rgba(0,0,0,0.5);
  }
  .realtrade-dropdown.open { display: block; }
  .realtrade-item { width: 100%; background: none; border: none; color: #d1d4dc; padding: 10px 18px; text-align: left; font-size: 14px; cursor: pointer; transition: background 0.2s; }
  .realtrade-item:hover:not(.disabled) { background: #2a2e39; }
  .realtrade-item.disabled { color: #787b86; cursor: not-allowed; }

  /* Real Trade Panel */
  .realtrade-panel {
    position: absolute; top: 80px; right: 60px; z-index: 220;
    background: #1e222d; border: 1px solid #2a2e39; border-radius: 8px;
    padding: 0; width: 420px; display: none;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5); max-height: calc(100vh - 100px); overflow-y: auto;
  }
  .realtrade-panel.open { display: block; }
  .rt-header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 12px 16px; border-bottom: 1px solid #2a2e39; cursor: move; user-select: none;
  }
  .rt-header h3 { font-size: 14px; color: #fff; margin: 0; }
  .rt-close { background: none; border: none; color: #787b86; font-size: 20px; cursor: pointer; }
  .rt-close:hover { color: #fff; }
  .rt-body { padding: 16px; }
  .rt-row { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
  .rt-row label { color: #787b86; font-size: 12px; min-width: 60px; }
  .rt-row input[type=text], .rt-row input[type=password], .rt-row input[type=number] {
    flex: 1; padding: 6px 10px; background: #131722; border: 1px solid #2a2e39;
    border-radius: 4px; color: #d1d4dc; font-size: 13px;
  }
  .rt-start-btn {
    width: 100%; padding: 10px; border: none; border-radius: 6px; font-size: 14px;
    font-weight: 700; cursor: pointer; transition: background 0.2s;
    background: #43a047; color: #fff;
  }
  .rt-start-btn:hover { background: #388e3c; }
  .rt-status { margin-top: 16px; padding: 12px; background: #131722; border-radius: 6px; border: 1px solid #2a2e39; color: #d1d4dc; font-size: 13px; }

  /* Signal Panel */
  .signal-panel {
    position: absolute; top: 44px; right: 300px; z-index: 200;
    background: #1e222d; border: 1px solid #2a2e39; border-radius: 8px;
    padding: 16px; width: 320px; display: none;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5); max-height: calc(100vh - 160px); overflow-y: auto;
  }
  .signal-panel.open { display: block; }
  .signal-panel h3 { font-size: 14px; color: #fff; margin-bottom: 10px; border-bottom: 1px solid #2a2e39; padding-bottom: 8px; }
  .verdict-box {
    text-align: center; padding: 12px; border-radius: 8px; margin-bottom: 12px;
    font-size: 20px; font-weight: 800; letter-spacing: 1px;
  }
  .verdict-box.buy { background: rgba(38,166,154,0.15); color: #26a69a; border: 1px solid #26a69a44; }
  .verdict-box.sell { background: rgba(239,83,80,0.15); color: #ef5350; border: 1px solid #ef535044; }
  .verdict-box.neutral { background: rgba(120,123,134,0.15); color: #787b86; border: 1px solid #787b8644; }
  .verdict-score { font-size: 12px; font-weight: 400; margin-top: 4px; }
  .ind-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 6px 0; border-bottom: 1px solid #2a2e3922; font-size: 12px;
  }
  .ind-row .ind-name { color: #d1d4dc; font-weight: 500; }
  .ind-row .ind-status { font-weight: 600; padding: 2px 8px; border-radius: 3px; font-size: 11px; }
  .ind-row .ind-status.bull { background: rgba(38,166,154,0.15); color: #26a69a; }
  .ind-row .ind-status.bear { background: rgba(239,83,80,0.15); color: #ef5350; }
  .ind-row .ind-status.neut { background: rgba(120,123,134,0.15); color: #787b86; }
  .ind-row .ind-weight { color: #787b86; font-size: 10px; min-width: 36px; text-align: right; }
  .signal-count { font-size: 11px; color: #787b86; margin-top: 10px; }
  .signal-count span { font-weight: 700; }
  .disclaimer { font-size: 9px; color: #555; margin-top: 10px; line-height: 1.4; }
  /* Backtest Dropdown */
  .backtest-dropdown-wrapper { position: relative; }
  .backtest-dropdown {
    position: absolute; top: 100%; left: 0; background: #1e222d; border: 1px solid #2a2e39;
    border-radius: 6px; padding: 4px 0; min-width: 160px; z-index: 300;
    box-shadow: 0 8px 24px rgba(0,0,0,0.5); display: none;
  }
  .backtest-dropdown.open { display: block; }
  .backtest-dropdown .bt-item {
    display: block; padding: 8px 16px; color: #d1d4dc; font-size: 13px;
    cursor: pointer; transition: background 0.1s; border: none; background: none; width: 100%; text-align: left;
  }
  .backtest-dropdown .bt-item:hover { background: #2a2e39; }
  /* Data Source Dropdown */
  .datasource-dropdown-wrapper { position: relative; }
  .datasource-dropdown {
    position: absolute; top: 100%; left: 0; background: #1e222d; border: 1px solid #2a2e39;
    border-radius: 6px; padding: 4px 0; min-width: 180px; z-index: 300;
    box-shadow: 0 8px 24px rgba(0,0,0,0.5); display: none;
  }
  .datasource-dropdown.open { display: block; }
  .ds-item {
    display: block; padding: 8px 16px; color: #d1d4dc; font-size: 13px;
    cursor: pointer; transition: background 0.1s; border: none; background: none; width: 100%; text-align: left;
  }
  .ds-item:hover { background: #2a2e39; }
  .ds-item.active { color: #2962ff; font-weight: 600; }
  /* Algo Dropdown */
  .algo-dropdown-wrapper { position: relative; }
  .algo-dropdown {
    position: absolute; top: 100%; left: 0; background: #1e222d; border: 1px solid #2a2e39;
    border-radius: 6px; padding: 4px 0; min-width: 180px; z-index: 300;
    box-shadow: 0 8px 24px rgba(0,0,0,0.5); display: none;
  }
  .algo-dropdown.open { display: block; }
  .algo-item {
    display: block; padding: 8px 16px; color: #d1d4dc; font-size: 13px;
    cursor: pointer; transition: background 0.1s; border: none; background: none; width: 100%; text-align: left;
  }
  .algo-item:hover { background: #2a2e39; }
  .algo-item.active { color: #2962ff; font-weight: 600; }
  /* Backtest Panel */
  .backtest-panel {
    position: absolute; top: 44px; right: 12px; z-index: 200;
    background: #1e222d; border: 1px solid #2a2e39; border-radius: 8px;
    padding: 0; width: 420px; display: none;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5); max-height: calc(100vh - 160px); overflow-y: auto;
  }
  .backtest-panel.open { display: block; }
  .bt-header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 12px 16px; border-bottom: 1px solid #2a2e39; position: sticky; top: 0;
    background: #1e222d; z-index: 1;
  }
  .bt-header h3 { font-size: 14px; color: #fff; margin: 0; }
  .bt-close { background: none; border: none; color: #787b86; font-size: 18px; cursor: pointer; padding: 0 4px; }
  .bt-close:hover { color: #fff; }
  .bt-tabs {
    display: flex; border-bottom: 1px solid #2a2e39; background: #181c27;
  }
  .bt-tab {
    flex: 1; padding: 10px; text-align: center; font-size: 12px; font-weight: 600;
    color: #787b86; cursor: pointer; border: none; background: none;
    border-bottom: 2px solid transparent; transition: all 0.15s;
  }
  .bt-tab.active { color: #2962ff; border-bottom-color: #2962ff; }
  .bt-tab:hover { color: #d1d4dc; }
  .bt-content { padding: 16px; }
  .bt-content.hidden { display: none; }
  .bt-stat-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 0;
  }
  .bt-stat {
    padding: 10px 12px; border-bottom: 1px solid #2a2e3933;
    display: flex; flex-direction: column; gap: 2px;
  }
  .bt-stat-label { font-size: 10px; color: #787b86; text-transform: uppercase; letter-spacing: 0.5px; }
  .bt-stat-value { font-size: 14px; font-weight: 700; color: #d1d4dc; }
  .bt-stat-value.positive { color: #26a69a; }
  .bt-stat-value.negative { color: #ef5350; }
  .bt-stat.full { grid-column: 1 / -1; }
  .bt-section-title {
    font-size: 11px; font-weight: 700; color: #787b86; text-transform: uppercase;
    letter-spacing: 1px; padding: 12px 12px 6px; border-top: 1px solid #2a2e39;
  }
  .bt-trade-table {
    width: 100%; border-collapse: collapse; font-size: 11px;
  }
  .bt-trade-table th {
    padding: 8px 6px; text-align: left; color: #787b86; font-weight: 600;
    border-bottom: 1px solid #2a2e39; font-size: 10px; text-transform: uppercase;
    position: sticky; top: 0; background: #1e222d;
  }
  .bt-trade-table td {
    padding: 6px; color: #d1d4dc; border-bottom: 1px solid #2a2e3933;
  }
  .bt-trade-table tr:hover td { background: #2a2e3944; }
  .bt-pnl-bar {
    display: inline-block; height: 4px; border-radius: 2px; min-width: 4px; vertical-align: middle;
  }
  .bt-equity-box {
    background: #131722; border-radius: 6px; padding: 12px; margin-bottom: 8px;
  }
  .bt-equity-row {
    display: flex; justify-content: space-between; padding: 4px 0; font-size: 12px;
  }
  .bt-equity-row .label { color: #787b86; }
  .bt-equity-row .val { color: #d1d4dc; font-weight: 600; }
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <div>
      <select class="symbol-select" id="symbolSelect">
        <option value="NIFTY50" selected>NIFTY 50</option>
        <option value="BANKNIFTY">BANK NIFTY</option>
        <option value="SENSEX">SENSEX</option>
        <option value="GOLD">Gold Futures</option>
        <option value="SILVER">Silver Futures</option>
        <option value="XAUUSD">XAU/USD</option>
        <option value="XAGUSD">XAG/USD</option>
        <option value="GOLDTEN">Gold ETF (10g)</option>
        <option value="SILVERBEES">Silver ETF</option>
        <option value="BTC">Bitcoin</option>
        <option value="ETH">Ethereum</option>
        <option value="DJI">Dow Jones</option>
        <option value="NASDAQ">NASDAQ</option>
        <option value="SP500">S&P 500</option>
        <option value="CRUDEOIL">Crude Oil</option>
        <option value="NATURALGAS">Natural Gas</option>
      </select>
      <span class="ticker-exchange" id="tickerExchange"> &middot; NSE</span>
    </div>
    <div class="search-wrap">
      <input class="search-input" id="searchInput" type="text" placeholder="Search by name or ticker (e.g. Reliance, TCS)" autocomplete="off">
      <div class="search-result" id="searchResult"></div>
    </div>
  </div>
  <div class="chart-title">Mangal View</div>
  <div class="price-info">
    <span class="current-price" id="currentPrice">--</span>
    <span class="price-change" id="priceChange">--</span>
  </div>
</div>

<div class="toolbar">
  <div class="period-dropdown-wrapper">
    <button class="ind-btn" id="btnPeriod"><span class="dot" style="background:#4caf50"></span>5m &#9662;</button>
    <div class="period-dropdown" id="periodDropdown">
      <button class="period-item" data-tf="1m" data-label="1m" data-name="1 Min">&#8203; 1 Min</button>
      <button class="period-item" data-tf="2m" data-label="2m" data-name="2 Min">&#8203; 2 Min</button>
      <button class="period-item" data-tf="3m" data-label="3m" data-name="3 Min">&#8203; 3 Min</button>
      <button class="period-item active" data-tf="5m" data-label="5m" data-name="5 Min">&#10004; 5 Min</button>
      <button class="period-item" data-tf="10m" data-label="10m" data-name="10 Min">&#8203; 10 Min</button>
      <button class="period-item" data-tf="15m" data-label="15m" data-name="15 Min">&#8203; 15 Min</button>
      <button class="period-item" data-tf="30m" data-label="30m" data-name="30 Min">&#8203; 30 Min</button>
      <button class="period-item" data-tf="1h" data-label="1H" data-name="1 Hour">&#8203; 1 Hour</button>
      <button class="period-item" data-tf="2h" data-label="2H" data-name="2 Hour">&#8203; 2 Hour</button>
      <button class="period-item" data-tf="4h" data-label="4H" data-name="4 Hour">&#8203; 4 Hour</button>
      <button class="period-item" data-tf="1d" data-label="1D" data-name="1 Day">&#8203; 1 Day</button>
      <button class="period-item" data-tf="1w" data-label="1W" data-name="1 Week">&#8203; 1 Week</button>
      <button class="period-item" data-tf="1mo" data-label="1M" data-name="1 Month">&#8203; 1 Month</button>
    </div>
  </div>
  <div class="separator"></div>
  <div class="indicators-dropdown-wrapper">
    <button class="ind-btn" id="btnIndicators"><span class="dot" style="background:#2962ff"></span>Indicators &#9662;</button>
    <div class="indicators-dropdown" id="indicatorsDropdown">
      <label class="ind-item" data-ind="ST"><span class="dot" style="background:#ff9800"></span><span>SuperTrend</span><input type="checkbox"></label>
      <label class="ind-item" data-ind="SAR"><span class="dot" style="background:#e040fb"></span><span>PSAR</span><input type="checkbox"></label>
      <label class="ind-item" data-ind="SR"><span class="dot" style="background:#42a5f5"></span><span>S/R Levels</span><input type="checkbox"></label>
      <label class="ind-item" data-ind="EMA"><span class="dot" style="background:#ffeb3b"></span><span>EMA 9/21</span><input type="checkbox"></label>
      <label class="ind-item" data-ind="VWAP"><span class="dot" style="background:#ff6d00"></span><span>VWAP</span><input type="checkbox"></label>
      <label class="ind-item" data-ind="BB"><span class="dot" style="background:#2196f3"></span><span>Bollinger Bands</span><input type="checkbox"></label>
      <label class="ind-item" data-ind="CPR"><span class="dot" style="background:#ab47bc"></span><span>CPR</span><input type="checkbox"></label>
      <label class="ind-item" data-ind="LP"><span class="dot" style="background:#ffd600"></span><span>Liquidity Pools</span><input type="checkbox"></label>
      <label class="ind-item" data-ind="FVG"><span class="dot" style="background:#80cbc4"></span><span>Fair Value Gap</span><input type="checkbox"></label>
      <label class="ind-item" data-ind="BOS"><span class="dot" style="background:#ff7043"></span><span>Break of Structure</span><input type="checkbox"></label>
      <label class="ind-item" data-ind="CHoCH"><span class="dot" style="background:#ba68c8"></span><span>Change of Character</span><input type="checkbox"></label>
      <label class="ind-item" data-ind="CVD"><span class="dot" style="background:#29b6f6"></span><span>Cum. Volume Delta</span><input type="checkbox"></label>
      <label class="ind-item" data-ind="VP"><span class="dot" style="background:#ff8a65"></span><span>Volume Profile</span><input type="checkbox"></label>
      <label class="ind-item" data-ind="Signals"><span class="dot" style="background:#00e676"></span><span>Signals</span><input type="checkbox" checked></label>
      <div style="border-top:1px solid #2a2e39;margin:6px 0"></div>
      <button class="ind-item" id="btnIndSettings" style="cursor:pointer;border:none;background:none;color:#d1d4dc;padding:8px 12px;width:100%;text-align:left;font-size:13px">&#9881; Indicator Settings</button>
    </div>
  </div>
  <div class="separator"></div>
  <div class="algo-dropdown-wrapper">
    <button class="ind-btn" id="btnAlgo"><span class="dot" style="background:#ff9100"></span>Algo &#9662;</button>
    <div class="algo-dropdown" id="algoDropdown">
      <button class="algo-item active" data-algo="trend" data-label="Trend">&#10004; Trend</button>
      <button class="algo-item active" data-algo="mstreet" data-label="MStreet">&#10004; MStreet</button>
      <button class="algo-item" data-algo="mfactor" data-label="MFactor">&#8203; MFactor</button>
      <button class="algo-item" data-algo="sniper" data-label="Sniper">&#8203; Sniper</button>
      <button class="algo-item" data-algo="orderflow" data-label="OrderFlow">&#8203; OrderFlow</button>
      <button class="algo-item" data-algo="priceaction" data-label="PriceAction">&#8203; PriceAction</button>
      <button class="algo-item" data-algo="breakout" data-label="Breakout">&#8203; Breakout</button>
      <button class="algo-item" data-algo="momentum" data-label="Momentum">&#8203; Momentum</button>
      <button class="algo-item" data-algo="scalping" data-label="Scalping">&#8203; Scalping</button>
      <button class="algo-item" data-algo="smartmoney" data-label="SmartMoney">&#8203; SmartMoney</button>
      <button class="algo-item" data-algo="quant" data-label="Quant">&#8203; Quant</button>
      <button class="algo-item" data-algo="hybrid" data-label="Hybrid">&#8203; Hybrid</button>
      <button class="algo-item" data-algo="statarb" data-label="StatArb">&#8203; StatArb</button>
      <button class="algo-item" data-algo="institution" data-label="Institution">&#8203; Institution</button>
      <button class="algo-item" data-algo="mpredict" data-label="MPredict">&#8203; MPredict</button>
      <div style="border-top:1px solid #2a2e39;margin:6px 0"></div>
      <button class="algo-item" id="btnAlgoAnalysis" style="color:#ffd600">&#9889; Signal Analysis</button>
    </div>
  </div>
  <div class="separator"></div>
  <button class="gear-btn" id="btnSettingsPanel" title="Settings">&#9881;</button>

  <!-- Settings Panel (Backtest, Data Source, Trade, Real Trade) -->
  <div class="cfg-panel" id="cfgPanel">
    <div class="cfg-header"><h3>&#9881; Settings</h3><button class="cfg-close" id="cfgClose">&times;</button></div>

    <!-- Backtest Section -->
    <div class="cfg-section">
      <div class="cfg-section-header">
        <span><span class="dot" style="background:#ff6d00"></span> Backtest</span>
        <label class="cfg-toggle"><input type="checkbox" id="cfgBacktestToggle"><span class="cfg-slider"></span></label>
      </div>
      <div class="cfg-section-body" id="cfgBacktestBody">
        <button class="cfg-item bt-algo-item" data-bt-algo="trend">&#128202; Trend</button>
        <button class="cfg-item bt-algo-item" data-bt-algo="mstreet">&#128202; MStreet</button>
        <button class="cfg-item bt-algo-item" data-bt-algo="mfactor">&#128202; MFactor</button>
        <button class="cfg-item bt-algo-item" data-bt-algo="sniper">&#128202; Sniper</button>
        <button class="cfg-item bt-algo-item" data-bt-algo="orderflow">&#128202; OrderFlow</button>
        <button class="cfg-item bt-algo-item" data-bt-algo="priceaction">&#128202; PriceAction</button>
        <button class="cfg-item bt-algo-item" data-bt-algo="breakout">&#128202; Breakout</button>
        <button class="cfg-item bt-algo-item" data-bt-algo="momentum">&#128202; Momentum</button>
        <button class="cfg-item bt-algo-item" data-bt-algo="scalping">&#128202; Scalping</button>
        <button class="cfg-item bt-algo-item" data-bt-algo="smartmoney">&#128202; SmartMoney</button>
        <button class="cfg-item bt-algo-item" data-bt-algo="quant">&#128202; Quant</button>
        <button class="cfg-item bt-algo-item" data-bt-algo="hybrid">&#128202; Hybrid</button>
        <button class="cfg-item bt-algo-item" data-bt-algo="mpredict">&#128202; MPredict</button>
      </div>
    </div>

    <!-- Data Source Section -->
    <div class="cfg-section">
      <div class="cfg-section-header">
        <span><span class="dot" style="background:#2196f3"></span> Data Source</span>
        <label class="cfg-toggle"><input type="checkbox" id="cfgDataSourceToggle" checked><span class="cfg-slider"></span></label>
      </div>
      <div class="cfg-section-body open" id="cfgDataSourceBody">
        <button class="cfg-item ds-cfg-item" data-source="yahoo" data-label="Yahoo Finance">&#8203; Yahoo Finance</button>
        <button class="cfg-item ds-cfg-item active" data-source="tradingview" data-label="TradingView">&#10004; TradingView</button>
        <button class="cfg-item ds-cfg-item" data-source="nse" data-label="NSE India">&#8203; NSE India</button>
      </div>
    </div>

    <!-- Trade Section -->
    <div class="cfg-section">
      <div class="cfg-section-header">
        <span><span class="dot" style="background:#FF5722"></span> Trade</span>
        <label class="cfg-toggle"><input type="checkbox" id="cfgTradeToggle"><span class="cfg-slider"></span></label>
      </div>
      <div class="cfg-section-body" id="cfgTradeBody">
        <button class="cfg-item disabled">&#128200; Stocks</button>
        <button class="cfg-item has-sub" id="cfgTradeFutures">&#128202; Futures</button>
        <div class="cfg-sub" id="cfgFuturesSub">
          <button class="cfg-sub-item" id="cfgTradePositions">&#128203; Positions</button>
          <button class="cfg-sub-item" id="cfgTradeLog">&#128196; Log</button>
        </div>
        <button class="cfg-item disabled">&#128176; Options</button>
      </div>
    </div>

    <!-- Real Trade Section -->
    <div class="cfg-section">
      <div class="cfg-section-header">
        <span><span class="dot" style="background:#43a047"></span> Real Trade</span>
        <label class="cfg-toggle"><input type="checkbox" id="cfgRealTradeToggle"><span class="cfg-slider"></span></label>
      </div>
      <div class="cfg-section-body" id="cfgRealTradeBody">
        <button class="cfg-item" id="cfgRealDelta">Delta</button>
        <button class="cfg-item disabled">Zerodha</button>
        <button class="cfg-item disabled">Mt5</button>
      </div>
    </div>
  </div>
  <!-- Delta Real Trade Panel Modal -->
  <div class="realtrade-panel" id="realTradePanel" style="display:none">
    <div class="rt-header">
      <h3>&#128179; Delta Real Trading</h3>
      <button class="rt-close" id="rtClose">&times;</button>
    </div>
    <div class="rt-body">
      <div class="rt-row">
        <label>Username</label>
        <input type="text" id="rtUsername" autocomplete="username">
      </div>
      <div class="rt-row">
        <label>Password</label>
        <input type="password" id="rtPassword" autocomplete="current-password">
      </div>
      <div class="rt-row">
        <label>Capital</label>
        <input type="number" id="rtCapital" value="100000" min="1000" step="1000">
      </div>
      <div class="rt-row">
        <label>Quantity</label>
        <input type="number" id="rtQty" value="" min="1" step="1" placeholder="Auto from capital">
      </div>
      <div class="rt-row">
        <label>Symbol</label>
        <input type="text" id="rtSymbol" placeholder="e.g. NIFTY50">
      </div>
      <div class="rt-row">
        <label>SL %</label>
        <input type="number" id="rtSL" value="1.0" min="0.1" step="0.1">
      </div>
      <div class="rt-row">
        <label>Target %</label>
        <input type="number" id="rtTarget" value="2.0" min="0.1" step="0.1">
      </div>
      <div class="rt-row" id="rtModeRow" style="display:flex">
        <label>Mode</label>
        <select id="rtMode">
          <option value="signals">Signals</option>
          <option value="manual">Manual</option>
        </select>
      </div>
      <div class="rt-row" id="rtManualBtns" style="display:none;gap:10px">
        <button class="rt-buy-btn" id="rtBuyBtn">Buy</button>
        <button class="rt-sell-btn" id="rtSellBtn">Sell</button>
      </div>
      <button class="rt-start-btn start" id="rtStartBtn">Start Trading</button>
      <div class="rt-status" id="rtStatus" style="display:none"></div>
      <div class="tp-status" id="rtPosStatusBox" style="display:none;margin-top:16px">
        <div class="tp-status-row"><span>Status</span><span class="val" id="rtPosStatus">Flat</span></div>
        <div class="tp-status-row"><span>Entry Price</span><span class="val" id="rtEntryPrice">-</span></div>
        <div class="tp-status-row"><span>Qty</span><span class="val" id="rtQtyVal">-</span></div>
        <div class="tp-status-row"><span>Unrealized P/L</span><span class="val" id="rtUnrealPnl">-</span></div>
        <div class="tp-status-row"><span>Capital</span><span class="val" id="rtCurCapital">-</span></div>
        <div class="tp-status-row"><span>Total Trades</span><span class="val" id="rtTotalTrades">0</span></div>
        <div class="tp-status-row"><span>Net P/L</span><span class="val" id="rtNetPnl">-</span></div>
        <div class="tp-status-row"><span>Win Rate</span><span class="val" id="rtWinRate">-</span></div>
        <div class="tp-status-row"><span>Max Drawdown</span><span class="val" id="rtMaxDD">-</span></div>
      </div>
      <div class="rt-log-panel" id="rtLogPanel" style="margin-top:18px;display:none">
        <h4 style="color:#fff;font-size:13px;margin:0 0 8px 0">Trade Log</h4>
        <div id="rtLogBody" style="max-height:120px;overflow-y:auto;background:#181c27;border-radius:6px;padding:8px 6px;font-size:12px;color:#d1d4dc"></div>
      </div>
    </div>
  </div>
  <div class="separator"></div>
  <button class="live-btn" id="btnLive" title="Toggle live continuous data feed"><span class="live-dot"></span>LIVE</button>
  <div class="separator"></div>
  <div class="zoom-dropdown-wrapper">
    <button class="ind-btn" id="btnZoom"><span class="dot" style="background:#78909c"></span>Zoom &#9662;</button>
    <div class="zoom-dropdown" id="zoomDropdown">
      <button class="zm-item" id="zoomHIn">H + &nbsp; Zoom In (Time)</button>
      <button class="zm-item" id="zoomHOut">H &minus; &nbsp; Zoom Out (Time)</button>
      <button class="zm-item" id="zoomVIn">V + &nbsp; Zoom In (Price)</button>
      <button class="zm-item" id="zoomVOut">V &minus; &nbsp; Zoom Out (Price)</button>
      <button class="zm-item" id="zoomReset">&#8634; &nbsp; Reset / Fit All</button>
    </div>
  </div>
  <div class="separator"></div>
  <div class="help-dropdown-wrapper">
    <button class="ind-btn" id="btnHelp"><span class="dot" style="background:#66bb6a"></span>Help &#9662;</button>
    <div class="help-dropdown" id="helpDropdown">
      <a class="help-item" href="/help/algos" target="_blank">&#128202; Algos</a>
      <a class="help-item" href="/help/indicators" target="_blank">&#128200; Indicators</a>
      <a class="help-item" href="/help/manual" target="_blank">&#128214; User Manual</a>
    </div>
  </div>
  <div class="separator"></div>
  <button class="ind-btn" id="btnTheme" title="Toggle Light/Dark Theme">&#127763; Theme</button>
  <div class="separator"></div>

</div>
<div id="chart-container">
  <div class="watermark" id="watermark">NIFTY 50</div>
  <div class="ohlc-legend" id="ohlcLegend">
    <span>O <span class="ohlc-val" id="legO">-</span></span>
    <span>H <span class="ohlc-val" id="legH">-</span></span>
    <span>L <span class="ohlc-val" id="legL">-</span></span>
    <span>C <span class="ohlc-val" id="legC">-</span></span>
    <span>Vol <span class="ohlc-val" id="legV">-</span></span>
  </div>
  <div class="indicator-legend" id="indLegend">
    <span class="il-st" id="legST"></span>
    <span class="il-sar" id="legSAR"></span>
  </div>
  <div class="loading-overlay" id="loader"><div class="loading-brand">Mangal View</div><div class="spinner"></div><div class="loading-text">Loading chart data...</div></div>
  <div class="signal-tooltip" id="signalTooltip"></div>

  <!-- Signal Analysis Panel -->
  <div class="signal-panel" id="signalPanel">
    <div style="display:flex;justify-content:space-between;align-items:center"><h3 style="margin:0">&#9889; Signal Analysis</h3><button id="signalPanelClose" style="background:none;border:none;color:#787b86;font-size:18px;cursor:pointer;padding:0 4px;line-height:1" title="Close">&times;</button></div>
    <div class="verdict-box neutral" id="verdictBox">LOADING...<div class="verdict-score" id="verdictScore"></div></div>
    <div id="indicatorRows"></div>
    <div class="signal-count" id="signalCount"></div>
    <div class="disclaimer">For informational purposes only. Not financial advice. Past signals do not guarantee future results.</div>
  </div>

  <!-- Settings Panel -->
  <div class="settings-panel" id="settingsPanel">
    <div style="display:flex;justify-content:space-between;align-items:center"><h3 style="margin:0">Indicator Settings</h3><button id="settingsPanelClose" style="background:none;border:none;color:#787b86;font-size:18px;cursor:pointer;padding:0 4px;line-height:1" title="Close">&times;</button></div>
    <div class="section-title" style="color:#ff9800">&#9650; SuperTrend</div>
    <label>Period <input type="number" id="stPeriod" value="10" min="1" max="50" step="1"></label>
    <label>Multiplier <input type="number" id="stMultiplier" value="3" min="0.1" max="10" step="0.1"></label>
    <div class="section-title" style="color:#e040fb">&#9679; Parabolic SAR</div>
    <label>AF Start <input type="number" id="sarStart" value="0.02" min="0.001" max="0.1" step="0.001"></label>
    <label>AF Increment <input type="number" id="sarInc" value="0.02" min="0.001" max="0.1" step="0.001"></label>
    <label>AF Max <input type="number" id="sarMax" value="0.2" min="0.01" max="0.5" step="0.01"></label>
    <div class="section-title" style="color:#2196f3">&#9679; Bollinger Bands</div>
    <label>Period <input type="number" id="bbPeriod" value="20" min="5" max="100" step="1"></label>
    <label>Std Dev <input type="number" id="bbStdDev" value="2.0" min="0.5" max="5" step="0.1"></label>
    <button class="apply-btn" id="applySettings">Apply</button>
    <button class="apply-btn" id="restoreDefaults" style="background:#2a2e39;color:#d1d4dc;margin-top:4px">Restore Defaults</button>
  </div>

  <!-- Backtest Strategy Panel -->
  <div class="backtest-panel" id="backtestPanel">
    <div class="bt-header">
      <h3>&#128200; Strategy Tester</h3>
      <div style="display:flex;align-items:center;gap:8px">
        <label style="font-size:11px;color:#787b86;display:flex;align-items:center;gap:4px">Qty <input type="number" id="btQtyInput" value="0" min="0" max="99999" step="1" style="width:60px;padding:3px 6px;background:#131722;border:1px solid #2a2e39;border-radius:3px;color:#d1d4dc;font-size:11px;text-align:right" title="Trade quantity per signal (0 = auto-size from capital)"></label>
        <button class="bt-close" id="btClose">&times;</button>
      </div>
    </div>
    <div class="bt-tabs">
      <button class="bt-tab active" data-tab="overview">Overview</button>
      <button class="bt-tab" data-tab="performance">Performance</button>
      <button class="bt-tab" data-tab="trades">Trade List</button>
    </div>
    <div class="bt-content" id="btOverview"></div>
    <div class="bt-content hidden" id="btPerformance"></div>
    <div class="bt-content hidden" id="btTrades"></div>
  </div>

  <!-- Futures Positions Panel -->
  <div class="trade-panel" id="tradePanel">
    <div class="tp-header">
      <h3>&#128202; Futures Trading</h3>
      <button class="tp-close" id="tpClose">&times;</button>
    </div>
    <div class="tp-body">
      <div class="tp-row">
        <label>Symbol</label>
        <select id="tpSymbol"></select>
      </div>
      <div class="tp-row">
        <label>Capital</label>
        <input type="number" id="tpCapital" value="100000" min="1000" step="1000">
      </div>
      <div class="tp-row">
        <label>Algorithm</label>
        <select id="tpAlgo">
          <option value="trend">Trend Strategy</option>
          <option value="mstreet" selected>MStreet Strategy</option>
          <option value="mfactor">MFactor Strategy</option>
          <option value="sniper">Sniper Entry Strategy</option>
          <option value="orderflow">OrderFlow Strategy</option>
          <option value="priceaction">Price Action Strategy</option>
          <option value="breakout">Breakout Strategy</option>
          <option value="momentum">Momentum Strategy</option>
          <option value="scalping">Scalping Strategy</option>
          <option value="smartmoney">Smart Money Strategy</option>
          <option value="quant">Quant Strategy</option>
          <option value="hybrid">Hybrid Strategy</option>
        </select>
      </div>
      <button class="tp-start-btn start" id="tpStartBtn">Start Trading</button>
      <div class="tp-status" id="tpStatus">
        <div class="tp-status-row"><span>Status</span><span class="val" id="tpPosStatus">Flat</span></div>
        <div class="tp-status-row"><span>Entry Price</span><span class="val" id="tpEntryPrice">-</span></div>
        <div class="tp-status-row"><span>Qty</span><span class="val" id="tpQty">-</span></div>
        <div class="tp-status-row"><span>Unrealized P/L</span><span class="val" id="tpUnrealPnl">-</span></div>
        <div class="tp-status-row"><span>Capital</span><span class="val" id="tpCurCapital">-</span></div>
        <div class="tp-status-row"><span>Total Trades</span><span class="val" id="tpTotalTrades">0</span></div>
        <div class="tp-status-row"><span>Net P/L</span><span class="val" id="tpNetPnl">-</span></div>
        <div class="tp-status-row"><span>Win Rate</span><span class="val" id="tpWinRate">-</span></div>
        <div class="tp-status-row"><span>Max Drawdown</span><span class="val" id="tpMaxDD">-</span></div>
      </div>
    </div>
  </div>

  <!-- Trade Log Panel -->
  <div class="trade-log-panel" id="tradeLogPanel">
    <div class="tp-header">
      <h3>&#128196; Trade Log</h3>
      <button class="tp-close" id="tlClose">&times;</button>
    </div>
    <div class="tp-body" id="tradeLogBody">
      <div style="text-align:center;padding:30px;color:#787b86">No trades yet. Start a Futures position first.</div>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<script>
(function() {
  const container = document.getElementById('chart-container');
  const loader = document.getElementById('loader');
  let currentTF = '5m';
  let currentSymbol = 'NIFTY50';
  let candleData = [];
  let liveMode = false;
  let liveInterval = null;
  let isBackgroundUpdate = false;
  let lastBacktest = {};
  let currentSource = 'tradingview';
  let signalMap = {};  // time -> signal data for tooltip
  let currentAlgo = new Set(['trend', 'mstreet']);

  // Indicator visibility
  let showST = false, showSAR = false, showSR = false, showEMA = false, showVWAP = false, showSignals = true;
  let showBB = false, showCPR = false;
  let showLP = false, showFVG = false, showBOS = false, showCHoCH = false, showCVD = false, showVP = false;

  // Create chart
  const chart = LightweightCharts.createChart(container, {
    layout: {
      background: { type: 'solid', color: '#131722' },
      textColor: '#787b86',
      fontSize: 12,
    },
    grid: {
      vertLines: { color: '#1e222d' },
      horzLines: { color: '#1e222d' },
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: { color: '#758696', width: 1, style: 3, labelBackgroundColor: '#2962ff' },
      horzLine: { color: '#758696', width: 1, style: 3, labelBackgroundColor: '#2962ff' },
    },
    rightPriceScale: {
      borderColor: '#2a2e39',
      scaleMargins: { top: 0.1, bottom: 0.2 },
    },
    timeScale: {
      borderColor: '#2a2e39',
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 5,
      barSpacing: 8,
    },
    handleScroll: { vertTouchDrag: false },
  });

  // ---- Series ----
  const candleSeries = chart.addCandlestickSeries({
    upColor: '#26a69a', downColor: '#ef5350',
    borderDownColor: '#ef5350', borderUpColor: '#26a69a',
    wickDownColor: '#ef5350', wickUpColor: '#26a69a',
  });

  const volumeSeries = chart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: '' });
  volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

  // CVD histogram (separate price scale at bottom)
  const cvdSeries = chart.addHistogramSeries({
    priceFormat: { type: 'volume' }, priceScaleId: 'cvd',
    priceLineVisible: false, lastValueVisible: false,
  });
  cvdSeries.priceScale().applyOptions({ scaleMargins: { top: 0.7, bottom: 0.02 }, visible: false });
  cvdSeries.applyOptions({ visible: false });

  // Prediction candle series (semi-transparent blue/orange)
  const predSeries = chart.addCandlestickSeries({
    upColor: 'rgba(33,150,243,0.5)', downColor: 'rgba(255,152,0,0.5)',
    borderDownColor: 'rgba(255,152,0,0.8)', borderUpColor: 'rgba(33,150,243,0.8)',
    wickDownColor: 'rgba(255,152,0,0.6)', wickUpColor: 'rgba(33,150,243,0.6)',
    priceLineVisible: false, lastValueVisible: false,
  });
  let showPredictions = true;  // controlled by mpredict algo toggle


    // ---- Delta Real Trading Logic ----
    let deltaSessionId = null;
    let deltaTrading = false;
    let deltaStatusInterval = null;
    const rtStartBtn = document.getElementById('rtStartBtn');
    const rtStatus = document.getElementById('rtStatus');
    function setDeltaPanelEnabled(enabled) {
      document.getElementById('rtUsername').disabled = !enabled;
      document.getElementById('rtPassword').disabled = !enabled;
      document.getElementById('rtCapital').disabled = !enabled;
      document.getElementById('rtQty').disabled = !enabled;
      document.getElementById('rtSymbol').disabled = !enabled;
      document.getElementById('rtSL').disabled = !enabled;
      document.getElementById('rtTarget').disabled = !enabled;
    }
    rtStartBtn.addEventListener('click', async function() {
      if (!deltaTrading) {
        // Login and start trading
        const username = document.getElementById('rtUsername').value.trim();
        const password = document.getElementById('rtPassword').value.trim();
        const capital = parseFloat(document.getElementById('rtCapital').value) || 100000;
        const qtyInput = parseInt(document.getElementById('rtQty').value) || 0;
        const symbol = document.getElementById('rtSymbol').value.trim();
        const sl_pct = parseFloat(document.getElementById('rtSL').value) || 1.0;
        const tgt_pct = parseFloat(document.getElementById('rtTarget').value) || 2.0;
        if (!username || !password || !symbol) {
          rtStatus.style.display = 'block';
          rtStatus.textContent = 'Please enter all required fields.';
          return;
        }
        rtStatus.style.display = 'block';
        rtStatus.textContent = 'Logging in...';
          document.getElementById('rtPosStatusBox').style.display = 'none';
        try {
          const resp = await fetch('/api/realtrade/delta/login', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({username, password})
          });
          const data = await resp.json();
          if (!data.success) {
            rtStatus.textContent = 'Login failed: ' + (data.error || 'Unknown error');
            return;
          }
          deltaSessionId = data.sessionId;
          setDeltaPanelEnabled(false);
          rtStartBtn.textContent = 'Stop Trading';
          rtStartBtn.classList.remove('start');
          rtStartBtn.classList.add('stop');
          deltaTrading = true;
          rtStatus.textContent = 'Trading started. Waiting for signals...';
          // Start polling status
          deltaStatusInterval = setInterval(async function() {
            if (!deltaSessionId) return;
            const resp = await fetch('/api/realtrade/delta/status?sessionId=' + deltaSessionId);
            const data = await resp.json();
            if (data.success) {
              // Update status box
              document.getElementById('rtPosStatusBox').style.display = 'block';
              document.getElementById('rtPosStatus').textContent = data.position || '-';
              document.getElementById('rtEntryPrice').textContent = data.entryPrice || '-';
              document.getElementById('rtQtyVal').textContent = data.qty || '-';
              document.getElementById('rtUnrealPnl').textContent = data.unrealPnl || '-';
              document.getElementById('rtCurCapital').textContent = data.capital || '-';
              document.getElementById('rtTotalTrades').textContent = data.totalTrades || '0';
              document.getElementById('rtNetPnl').textContent = data.netPnl || '-';
              document.getElementById('rtWinRate').textContent = data.winRate || '-';
              document.getElementById('rtMaxDD').textContent = data.maxDrawdown || '-';
              // Update trade log
              if (data.orders) {
                renderDeltaTradeLog(data.orders);
              }
            }
          }, 3000);

          // Show log panel
          document.getElementById('rtLogPanel').style.display = 'block';
              // Render Delta trade log
              function renderDeltaTradeLog(orders) {
                const body = document.getElementById('rtLogBody');
                if (!orders || orders.length === 0) {
                  body.innerHTML = '<div style="text-align:center;color:#787b86">No trades yet.</div>';
                  return;
                }
                let html = '<table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr style="color:#aaa"><th style="text-align:left">#</th><th>Type</th><th>Price</th><th>Qty</th><th>Time</th><th>P/L</th></tr></thead><tbody>';
                orders.slice(-20).forEach((o, i) => {
                  html += `<tr><td>${orders.length-20+i+1}</td><td>${o.side}</td><td>${o.price}</td><td>${o.qty}</td><td>${o.time||'-'}</td><td style="color:${o.pnl>0?'#26a69a':o.pnl<0?'#ef5350':'#d1d4dc'}">${o.pnl||'-'}</td></tr>`;
                });
                html += '</tbody></table>';
                body.innerHTML = html;
              }
          // Attach signal handler
          // Attach signal handler (signals mode only)
          window.deltaRealTradeSignalHandler = async function(signal, price) {
            if (!deltaTrading || !deltaSessionId) return;
            if (document.getElementById('rtMode').value !== 'signals') return;
            // Only act on BUY/SELL signals
            if (signal.type !== 'BUY' && signal.type !== 'SELL') return;
            await placeDeltaOrder(signal.type, price);
          };
        } catch(err) {
          rtStatus.textContent = 'Login error: ' + err;
        }
      } else {
        // Stop trading
        deltaTrading = false;
        deltaSessionId = null;
        setDeltaPanelEnabled(true);
        rtStartBtn.textContent = 'Start Trading';
        rtStartBtn.classList.remove('stop');
        rtStartBtn.classList.add('start');
        rtStatus.textContent = 'Stopped.';
        if (deltaStatusInterval) clearInterval(deltaStatusInterval);
        window.deltaRealTradeSignalHandler = null;
        document.getElementById('rtPosStatusBox').style.display = 'none';
      }
    });

    // Hook into signal processing
        // Manual buy/sell button logic
        document.getElementById('rtMode').addEventListener('change', function() {
          if (this.value === 'manual') {
            document.getElementById('rtManualBtns').style.display = 'flex';
          } else {
            document.getElementById('rtManualBtns').style.display = 'none';
          }
        });
        async function placeDeltaOrder(side, price) {
          const capital = parseFloat(document.getElementById('rtCapital').value) || 100000;
          const qtyInput = parseInt(document.getElementById('rtQty').value) || 0;
          const symbol = document.getElementById('rtSymbol').value.trim();
          const sl_pct = parseFloat(document.getElementById('rtSL').value) || 1.0;
          const tgt_pct = parseFloat(document.getElementById('rtTarget').value) || 2.0;
          let qty = qtyInput > 0 ? qtyInput : Math.floor(capital / price);
          try {
            const resp = await fetch('/api/realtrade/delta/order', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({
                sessionId: deltaSessionId,
                symbol,
                qty,
                side,
                sl_pct,
                tgt_pct,
                capital
              })
            });
            const data = await resp.json();
            if (data.success) {
              rtStatus.textContent = 'Order placed: ' + side + ' ' + qty + ' ' + symbol + ' @ ' + price;
            } else {
              rtStatus.textContent = 'Order error: ' + (data.error || 'Unknown error');
            }
          } catch(err) {
            rtStatus.textContent = 'Order error: ' + err;
          }
        }
        document.getElementById('rtBuyBtn').addEventListener('click', async function() {
          if (!deltaTrading || !deltaSessionId) return;
          // Use latest price from chart
          const lastBar = candleData[candleData.length-1];
          const price = lastBar ? lastBar.close : 0;
          await placeDeltaOrder('BUY', price);
        });
        document.getElementById('rtSellBtn').addEventListener('click', async function() {
          if (!deltaTrading || !deltaSessionId) return;
          const lastBar = candleData[candleData.length-1];
          const price = lastBar ? lastBar.close : 0;
          await placeDeltaOrder('SELL', price);
        });
    const origProcessTradeSignal = window.processTradeSignal;
    window.processTradeSignal = async function(signal, price) {
      if (window.deltaRealTradeSignalHandler) {
        await window.deltaRealTradeSignalHandler(signal, price);
      }
      if (origProcessTradeSignal) {
        await origProcessTradeSignal(signal, price);
      }
    };
  // SuperTrend: two line series (bullish=green, bearish=red)
  const stBullSeries = chart.addLineSeries({ color: '#26a69a', lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
  const stBearSeries = chart.addLineSeries({ color: '#ef5350', lineWidth: 2, priceLineVisible: false, lastValueVisible: false });

  // Parabolic SAR: markers on candleSeries
  // (We'll use a separate series with cross markers for SAR dots)
  const sarBullSeries = chart.addLineSeries({
    color: 'rgba(0,0,0,0)', lineWidth: 0, pointMarkersVisible: true,
    pointMarkersRadius: 2.5, priceLineVisible: false, lastValueVisible: false,
    crosshairMarkerVisible: false,
  });
  const sarBearSeries = chart.addLineSeries({
    color: 'rgba(0,0,0,0)', lineWidth: 0, pointMarkersVisible: true,
    pointMarkersRadius: 2.5, priceLineVisible: false, lastValueVisible: false,
    crosshairMarkerVisible: false,
  });

  // EMA lines
  const ema9Series = chart.addLineSeries({ color: '#ffeb3b', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, lineStyle: 0 });
  const ema21Series = chart.addLineSeries({ color: '#ff9800', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, lineStyle: 0 });

  // VWAP line
  const vwapSeries = chart.addLineSeries({ color: '#ff6d00', lineWidth: 2, priceLineVisible: false, lastValueVisible: false, lineStyle: 2 });

  // Bollinger Bands
  const bbUpperSeries = chart.addLineSeries({ color: '#2196f3', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, lineStyle: 2 });
  const bbMiddleSeries = chart.addLineSeries({ color: '#2196f3', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, lineStyle: 0 });
  const bbLowerSeries = chart.addLineSeries({ color: '#2196f3', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, lineStyle: 2 });
  bbUpperSeries.applyOptions({ visible: false });
  bbMiddleSeries.applyOptions({ visible: false });
  bbLowerSeries.applyOptions({ visible: false });

  // S/R: horizontal price lines on the candleSeries
  let srLines = [];
  // CPR: horizontal price lines
  let cprLines = [];
  // Liquidity Pool price lines
  let lpLines = [];
  // FVG box markers (drawn as horizontal band lines)
  let fvgLines = [];
  // BOS/CHoCH markers
  let bosMarkersSeries = chart.addLineSeries({
    color: 'rgba(0,0,0,0)', lineWidth: 0, pointMarkersVisible: true,
    pointMarkersRadius: 0, priceLineVisible: false, lastValueVisible: false,
    crosshairMarkerVisible: false,
  });
  let chochMarkersSeries = chart.addLineSeries({
    color: 'rgba(0,0,0,0)', lineWidth: 0, pointMarkersVisible: true,
    pointMarkersRadius: 0, priceLineVisible: false, lastValueVisible: false,
    crosshairMarkerVisible: false,
  });
  bosMarkersSeries.applyOptions({ visible: false });
  chochMarkersSeries.applyOptions({ visible: false });

  // Volume Profile price lines
  let vpLines = [];
  let lastVP = null;
  function drawVP(vpData) {
    vpLines.forEach(l => { try { candleSeries.removePriceLine(l); } catch(e){} });
    vpLines = [];
    if (!vpData || vpData.length === 0) return;
    vpData.forEach(vp => {
      if (vp.volume <= 0) return;
      const color = vp.isPOC ? 'rgba(255,138,101,0.9)' :
                    vp.isVAH ? 'rgba(38,166,154,0.85)' :
                    vp.isVAL ? 'rgba(239,83,80,0.85)' :
                    vp.isVA  ? 'rgba(255,138,101,0.45)' : 'rgba(255,138,101,0.2)';
      const title = vp.isPOC ? 'POC' : vp.isVAH ? 'VAH' : vp.isVAL ? 'VAL' : '';
      const lw = vp.isPOC ? 2 : (vp.isVAH || vp.isVAL) ? 2 : 1;
      const ls = vp.isPOC ? 0 : (vp.isVAH || vp.isVAL) ? 1 : 2;
      const line = candleSeries.createPriceLine({
        price: vp.price,
        color: color,
        lineWidth: lw,
        lineStyle: ls,
        axisLabelVisible: vp.isPOC || vp.isVAH || vp.isVAL,
        title: title,
      });
      vpLines.push(line);
    });
  }

  // ---- Settings Panel (opened from Indicators dropdown) ----
  const settingsPanel = document.getElementById('settingsPanel');
  document.getElementById('btnIndSettings').addEventListener('click', function(e) {
    e.stopPropagation();
    indDropdown.classList.remove('open');
    settingsPanel.classList.toggle('open');
  });
  document.getElementById('applySettings').addEventListener('click', () => {
    settingsPanel.classList.remove('open');
    loadData(currentTF);
  });
  document.getElementById('restoreDefaults').addEventListener('click', () => {
    document.getElementById('stPeriod').value = '10';
    document.getElementById('stMultiplier').value = '3';
    document.getElementById('sarStart').value = '0.02';
    document.getElementById('sarInc').value = '0.02';
    document.getElementById('sarMax').value = '0.2';
    document.getElementById('bbPeriod').value = '20';
    document.getElementById('bbStdDev').value = '2.0';
    settingsPanel.classList.remove('open');
    loadData(currentTF);
  });
  document.getElementById('settingsPanelClose').addEventListener('click', () => {
    settingsPanel.classList.remove('open');
  });

  // ---- Signal Panel (opened from Algo dropdown) ----
  const signalPanel = document.getElementById('signalPanel');
  document.getElementById('btnAlgoAnalysis').addEventListener('click', function(e) {
    e.stopPropagation();
    algoDropdown.classList.remove('open');
    signalPanel.classList.toggle('open');
    settingsPanel.classList.remove('open');
  });
  document.getElementById('signalPanelClose').addEventListener('click', () => {
    signalPanel.classList.remove('open');
  });

  // ---- Indicators Dropdown ----
  const indDropdown = document.getElementById('indicatorsDropdown');
  document.getElementById('btnIndicators').addEventListener('click', function(e) {
    e.stopPropagation();
    indDropdown.classList.toggle('open');
    settingsPanel.classList.remove('open');
    signalPanel.classList.remove('open');
    cfgPanel.classList.remove('open');
  });
  // Close dropdown on outside click
  document.addEventListener('click', function(e) {
    if (!e.target.closest('.indicators-dropdown-wrapper')) indDropdown.classList.remove('open');
  });

  // ---- Indicator Toggle via Dropdown Checkboxes ----
  document.querySelectorAll('.ind-item input[type="checkbox"]').forEach(cb => {
    cb.addEventListener('change', function() {
      const ind = this.closest('.ind-item').dataset.ind;
      const on = this.checked;
      switch(ind) {
        case 'ST':
          showST = on;
          stBullSeries.applyOptions({ visible: on }); stBearSeries.applyOptions({ visible: on });
          break;
        case 'SAR':
          showSAR = on;
          sarBullSeries.applyOptions({ visible: on }); sarBearSeries.applyOptions({ visible: on });
          break;
        case 'SR':
          showSR = on;
          srLines.forEach(l => { try { candleSeries.removePriceLine(l); } catch(e){} });
          if (on && lastSR) drawSR(lastSR);
          break;
        case 'EMA':
          showEMA = on;
          ema9Series.applyOptions({ visible: on }); ema21Series.applyOptions({ visible: on });
          break;
        case 'VWAP':
          showVWAP = on;
          vwapSeries.applyOptions({ visible: on });
          break;
        case 'BB':
          showBB = on;
          bbUpperSeries.applyOptions({ visible: on }); bbMiddleSeries.applyOptions({ visible: on }); bbLowerSeries.applyOptions({ visible: on });
          break;
        case 'CPR':
          showCPR = on;
          cprLines.forEach(l => { try { candleSeries.removePriceLine(l); } catch(e){} });
          if (on && lastCPR) drawCPR(lastCPR);
          break;
        case 'LP':
          showLP = on;
          lpLines.forEach(l => { try { candleSeries.removePriceLine(l); } catch(e){} });
          lpLines = [];
          if (on && lastLP) drawLP(lastLP);
          break;
        case 'FVG':
          showFVG = on;
          fvgLines.forEach(l => { try { candleSeries.removePriceLine(l); } catch(e){} });
          fvgLines = [];
          if (on && lastFVG) drawFVG(lastFVG);
          break;
        case 'BOS':
          showBOS = on;
          bosMarkersSeries.applyOptions({ visible: on });
          break;
        case 'CHoCH':
          showCHoCH = on;
          chochMarkersSeries.applyOptions({ visible: on });
          break;
        case 'CVD':
          showCVD = on;
          cvdSeries.applyOptions({ visible: on });
          cvdSeries.priceScale().applyOptions({ visible: on });
          break;
        case 'VP':
          showVP = on;
          vpLines.forEach(l => { try { candleSeries.removePriceLine(l); } catch(e){} });
          vpLines = [];
          if (on && lastVP) drawVP(lastVP);
          break;
        case 'Signals':
          showSignals = on;
          loadData(currentTF);
          break;
      }
    });
  });

  // ---- OHLC Legend ----
  chart.subscribeCrosshairMove(function(param) {
    const tooltip = document.getElementById('signalTooltip');
    if (!param || !param.time) { updateLegendFromLast(); tooltip.style.display = 'none'; return; }
    const data = param.seriesData.get(candleSeries);
    if (data) {
      updateLegend(data.open, data.high, data.low, data.close);
      const vData = param.seriesData.get(volumeSeries);
      document.getElementById('legV').textContent = vData ? formatVolume(vData.value) : '-';
    }
    // Signal tooltip
    let rawTime = param.time;
    if (typeof rawTime === 'object') {
      rawTime = Math.floor(new Date(rawTime.year, rawTime.month - 1, rawTime.day).getTime() / 1000);
    }
    const sig = signalMap[rawTime];
    if (sig && showSignals) {
      const isBuy = sig.type.includes('BUY');
      const reasons = sig.reasons || [];
      const algoName = sig.algo ? sig.algo.charAt(0).toUpperCase() + sig.algo.slice(1) : '';
      let html = '<div class="st-header ' + (isBuy ? 'buy' : 'sell') + '">' +
        sig.type.replace('_', ' ') + ' <span class="st-score">Score: ' + sig.score.toFixed(1) + '</span></div>';
      if (algoName) html += '<div class="st-row" style="color:#ffd600;font-size:11px;margin-bottom:4px"><span>Algo: ' + algoName + '</span></div>';
      reasons.forEach(r => {
        html += '<div class="st-row"><span class="st-reason">\u2022 ' + r + '</span></div>';
      });
      tooltip.innerHTML = html;
      tooltip.style.display = 'block';
      // Position near crosshair
      const x = param.point ? param.point.x : 0;
      const y = param.point ? param.point.y : 0;
      const cRect = container.getBoundingClientRect();
      let tx = x + 16;
      let ty = y + 16;
      if (tx + 300 > cRect.width) tx = x - 220;
      if (ty + 200 > cRect.height) ty = y - 200;
      if (ty < 0) ty = 10;
      tooltip.style.left = tx + 'px';
      tooltip.style.top = ty + 'px';
    } else {
      tooltip.style.display = 'none';
    }
  });

  function updateLegend(o, h, l, c) {
    const color = c >= o ? '#26a69a' : '#ef5350';
    document.getElementById('legO').textContent = o.toFixed(2);
    document.getElementById('legH').textContent = h.toFixed(2);
    document.getElementById('legL').textContent = l.toFixed(2);
    document.getElementById('legC').textContent = c.toFixed(2);
    ['legO','legH','legL','legC'].forEach(id => document.getElementById(id).style.color = color);
  }
  function updateLegendFromLast() {
    if (candleData.length === 0) return;
    const last = candleData[candleData.length - 1];
    updateLegend(last.open, last.high, last.low, last.close);
    document.getElementById('legV').textContent = formatVolume(last.volume);
  }
  function formatVolume(v) {
    if (v >= 1e7) return (v / 1e7).toFixed(2) + ' Cr';
    if (v >= 1e5) return (v / 1e5).toFixed(2) + ' L';
    if (v >= 1e3) return (v / 1e3).toFixed(1) + ' K';
    return v.toString();
  }
  function updatePriceHeader() {
    if (candleData.length < 2) return;
    const last = candleData[candleData.length - 1];
    const prev = candleData[candleData.length - 2];
    const change = last.close - prev.close;
    const pct = ((change / prev.close) * 100).toFixed(2);
    const el = document.getElementById('currentPrice');
    el.textContent = last.close.toFixed(2);
    el.className = 'current-price ' + (change >= 0 ? 'positive' : 'negative');
    const chEl = document.getElementById('priceChange');
    chEl.textContent = (change >= 0 ? '+' : '') + change.toFixed(2) + ' (' + pct + '%)';
    chEl.className = 'price-change ' + (change >= 0 ? 'positive' : 'negative');
  }

  function formatTime(t, isDaily) {
    if (isDaily) {
      const d = new Date(t * 1000);
      return { year: d.getFullYear(), month: d.getMonth() + 1, day: d.getDate() };
    }
    return t;
  }

  // ---- Draw Support/Resistance ----
  let lastSR = null;
  function drawSR(sr) {
    srLines.forEach(l => { try { candleSeries.removePriceLine(l); } catch(e){} });
    srLines = [];
    if (!sr) return;
    (sr.support || []).forEach((s, i) => {
      const line = candleSeries.createPriceLine({
        price: s.price, color: '#26a69a', lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true, title: 'S' + (i+1) + (s.strength > 1 ? ' (' + s.strength + ')' : ''),
      });
      srLines.push(line);
    });
    (sr.resistance || []).forEach((r, i) => {
      const line = candleSeries.createPriceLine({
        price: r.price, color: '#ef5350', lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true, title: 'R' + (i+1) + (r.strength > 1 ? ' (' + r.strength + ')' : ''),
      });
      srLines.push(line);
    });
  }

  // ---- Draw CPR ----
  let lastCPR = null;
  function drawCPR(cpr) {
    cprLines.forEach(l => { try { candleSeries.removePriceLine(l); } catch(e){} });
    cprLines = [];
    if (!cpr || !cpr.pivot) return;
    const levels = [
      { price: cpr.tc, color: '#ab47bc', title: 'TC' },
      { price: cpr.pivot, color: '#ce93d8', title: 'Pivot' },
      { price: cpr.bc, color: '#ab47bc', title: 'BC' },
    ];
    levels.forEach(lv => {
      const line = candleSeries.createPriceLine({
        price: lv.price, color: lv.color, lineWidth: 1, lineStyle: 1,
        axisLabelVisible: true, title: lv.title,
      });
      cprLines.push(line);
    });
  }

  // ---- Draw Liquidity Pools ----
  let lastLP = null;
  function drawLP(pools) {
    lpLines.forEach(l => { try { candleSeries.removePriceLine(l); } catch(e){} });
    lpLines = [];
    if (!pools || pools.length === 0) return;
    // Deduplicate: keep unique price levels, pick strongest
    const seen = {};
    pools.forEach(p => {
      const key = p.price + '_' + p.type;
      if (!seen[key] || p.strength > seen[key].strength) seen[key] = p;
    });
    Object.values(seen).forEach(p => {
      const isBuy = p.type === 'buyside';
      const line = candleSeries.createPriceLine({
        price: p.price,
        color: isBuy ? '#ffd600' : '#ffd600',
        lineWidth: 1, lineStyle: 3,
        axisLabelVisible: true,
        title: (isBuy ? 'BSL' : 'SSL') + (p.strength > 2 ? ' (' + p.strength + ')' : ''),
      });
      lpLines.push(line);
    });
  }

  // ---- Draw Fair Value Gaps ----
  let lastFVG = null;
  function drawFVG(fvgs) {
    fvgLines.forEach(l => { try { candleSeries.removePriceLine(l); } catch(e){} });
    fvgLines = [];
    if (!fvgs || fvgs.length === 0) return;
    // Show recent FVGs (last 10)
    const recent = fvgs.slice(-10);
    recent.forEach(f => {
      const isBull = f.type === 'bullish';
      const lineHi = candleSeries.createPriceLine({
        price: f.high,
        color: isBull ? 'rgba(128,203,196,0.5)' : 'rgba(239,154,154,0.5)',
        lineWidth: 1, lineStyle: 3,
        axisLabelVisible: false,
        title: isBull ? 'FVG↑' : 'FVG↓',
      });
      const lineLo = candleSeries.createPriceLine({
        price: f.low,
        color: isBull ? 'rgba(128,203,196,0.5)' : 'rgba(239,154,154,0.5)',
        lineWidth: 1, lineStyle: 3,
        axisLabelVisible: false,
        title: '',
      });
      fvgLines.push(lineHi, lineLo);
    });
  }

  // ---- Load Data ----
  async function loadData(tf, background) {
    if (!background) loader.classList.remove('hidden');
    // Save current visible range before update
    const savedLogicalRange = chart.timeScale().getVisibleLogicalRange();
    const savedBarSpacing = chart.timeScale().options().barSpacing || 8;
    try {
      const stP = document.getElementById('stPeriod').value;
      const stM = document.getElementById('stMultiplier').value;
      const sarS = document.getElementById('sarStart').value;
      const sarI = document.getElementById('sarInc').value;
      const sarMx = document.getElementById('sarMax').value;

      const bbP = document.getElementById('bbPeriod').value;
      const bbSD = document.getElementById('bbStdDev').value;

      const btQty = document.getElementById('btQtyInput').value || '0';
      const url = '/api/candles?interval=' + tf + '&symbol=' + currentSymbol
        + '&st_period=' + stP + '&st_multiplier=' + stM
        + '&sar_start=' + sarS + '&sar_inc=' + sarI + '&sar_max=' + sarMx
        + '&bb_period=' + bbP + '&bb_stddev=' + bbSD
        + '&bt_qty=' + btQty
        + '&source=' + currentSource
        + '&algo=' + Array.from(currentAlgo).join(',');

      const resp = await fetch(url);
      const json = await resp.json();
      candleData = json.candles || [];
      const supertrend = json.supertrend || [];
      const psar = json.parabolicSAR || [];
      const sr = json.supportResistance || {};
      lastSR = sr;

      const isDaily = ['1d','1w','1mo'].includes(tf);

      // --- Candles ---
      const formatted = candleData.map(c => ({
        time: formatTime(c.time, isDaily),
        open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume,
      }));
      candleSeries.setData(formatted.map(({ volume, ...rest }) => rest));
      volumeSeries.setData(formatted.map(c => ({
        time: c.time, value: c.volume || 0,
        color: c.close >= c.open ? 'rgba(38,166,154,0.3)' : 'rgba(239,83,80,0.3)',
      })));

      // --- ML Predicted Candles ---
      const preds = json.predictions || [];
      if (currentAlgo.has('mpredict') && preds.length > 0) {
        // Include the last real candle as bridge + predicted candles
        const lastReal = formatted[formatted.length - 1];
        const predFormatted = preds.map(p => ({
          time: formatTime(p.time, isDaily),
          open: p.open, high: p.high, low: p.low, close: p.close,
        }));
        predSeries.setData([{time: lastReal.time, open: lastReal.close, high: lastReal.close, low: lastReal.close, close: lastReal.close}, ...predFormatted]);
        predSeries.applyOptions({ visible: true });
      } else {
        predSeries.setData([]);
        predSeries.applyOptions({ visible: false });
      }

      // --- SuperTrend ---
      const stBull = [], stBear = [];
      for (let i = 0; i < supertrend.length; i++) {
        const s = supertrend[i];
        const t = formatTime(s.time, isDaily);
        if (s.direction === 1) {
          stBull.push({ time: t, value: s.value });
          // bridge: connect to bear with a point
          if (stBear.length > 0) stBear.push({ time: t, value: s.value });
        } else {
          stBear.push({ time: t, value: s.value });
          if (stBull.length > 0) stBull.push({ time: t, value: s.value });
        }
      }
      stBullSeries.setData(stBull);
      stBearSeries.setData(stBear);
      stBullSeries.applyOptions({ visible: showST });
      stBearSeries.applyOptions({ visible: showST });

      // Update SuperTrend legend
      if (supertrend.length > 0) {
        const last = supertrend[supertrend.length - 1];
        const stColor = last.direction === 1 ? '#26a69a' : '#ef5350';
        document.getElementById('legST').innerHTML =
          '<span style="color:' + stColor + '">ST(' + stP + ',' + stM + ') ' + last.value.toFixed(2) + '</span>';
      }

      // --- Parabolic SAR ---
      const sarBullData = [], sarBearData = [];
      for (const p of psar) {
        const t = formatTime(p.time, isDaily);
        if (p.bullish) {
          sarBullData.push({ time: t, value: p.value });
        } else {
          sarBearData.push({ time: t, value: p.value });
        }
      }
      sarBullSeries.setData(sarBullData);
      sarBearSeries.setData(sarBearData);
      sarBullSeries.applyOptions({ visible: showSAR, color: 'rgba(0,0,0,0)', pointMarkersVisible: true });
      sarBearSeries.applyOptions({ visible: showSAR, color: 'rgba(0,0,0,0)', pointMarkersVisible: true });
      // Color the SAR dots
      sarBullSeries.applyOptions({ color: '#26a69a66', lineWidth: 0, pointMarkersRadius: 2.5 });
      sarBearSeries.applyOptions({ color: '#ef535066', lineWidth: 0, pointMarkersRadius: 2.5 });

      if (psar.length > 0) {
        const lastP = psar[psar.length - 1];
        const pColor = lastP.bullish ? '#26a69a' : '#ef5350';
        document.getElementById('legSAR').innerHTML =
          '<span style="color:' + pColor + '">PSAR ' + lastP.value.toFixed(2) + '</span>';
      }

      // --- EMA 9 / 21 ---
      const ema9Data = (json.ema9 || []).map(e => ({ time: formatTime(e.time, isDaily), value: e.value }));
      const ema21Data = (json.ema21 || []).map(e => ({ time: formatTime(e.time, isDaily), value: e.value }));
      ema9Series.setData(ema9Data);
      ema21Series.setData(ema21Data);
      ema9Series.applyOptions({ visible: showEMA });
      ema21Series.applyOptions({ visible: showEMA });

      // --- VWAP ---
      const vwapArr = (json.vwap || []).map(v => ({ time: formatTime(v.time, isDaily), value: v.value }));
      vwapSeries.setData(vwapArr);
      vwapSeries.applyOptions({ visible: showVWAP });

      // --- Bollinger Bands ---
      const bbData = json.bollingerBands || [];
      const bbUpper = bbData.map(b => ({ time: formatTime(b.time, isDaily), value: b.upper }));
      const bbMiddle = bbData.map(b => ({ time: formatTime(b.time, isDaily), value: b.middle }));
      const bbLower = bbData.map(b => ({ time: formatTime(b.time, isDaily), value: b.lower }));
      bbUpperSeries.setData(bbUpper);
      bbMiddleSeries.setData(bbMiddle);
      bbLowerSeries.setData(bbLower);
      bbUpperSeries.applyOptions({ visible: showBB });
      bbMiddleSeries.applyOptions({ visible: showBB });
      bbLowerSeries.applyOptions({ visible: showBB });

      // --- CPR ---
      const cpr = json.cpr || {};
      lastCPR = cpr;
      cprLines.forEach(l => { try { candleSeries.removePriceLine(l); } catch(e){} });
      cprLines = [];
      if (showCPR) drawCPR(cpr);

      // --- Liquidity Pools ---
      const lpData = json.liquidityPools || [];
      lastLP = lpData;
      lpLines.forEach(l => { try { candleSeries.removePriceLine(l); } catch(e){} });
      lpLines = [];
      if (showLP) drawLP(lpData);

      // --- Fair Value Gaps ---
      const fvgData = json.fairValueGaps || [];
      lastFVG = fvgData;
      fvgLines.forEach(l => { try { candleSeries.removePriceLine(l); } catch(e){} });
      fvgLines = [];
      if (showFVG) drawFVG(fvgData);

      // --- Volume Profile ---
      const vpData = json.volumeProfile || [];
      lastVP = vpData;
      vpLines.forEach(l => { try { candleSeries.removePriceLine(l); } catch(e){} });
      vpLines = [];
      if (showVP) drawVP(vpData);

      // --- Break of Structure / Change of Character ---
      const bosChoch = json.bosChoch || {};
      const bosList = bosChoch.bos || [];
      const chochList = bosChoch.choch || [];

      // BOS markers on a hidden series
      const bosData = bosList.map(b => ({ time: formatTime(b.time, isDaily), value: b.price }));
      if (bosData.length > 0) {
        bosMarkersSeries.setData(bosData);
        const bosM = bosList.map(b => {
          const isBull = b.type === 'bullish';
          return {
            time: formatTime(b.time, isDaily),
            position: isBull ? 'belowBar' : 'aboveBar',
            color: isBull ? '#66bb6a' : '#ff7043',
            shape: isBull ? 'arrowUp' : 'arrowDown',
            text: 'BOS ' + b.broken.toFixed(0),
          };
        });
        bosMarkersSeries.setMarkers(bosM);
      } else {
        bosMarkersSeries.setData([]);
        bosMarkersSeries.setMarkers([]);
      }
      bosMarkersSeries.applyOptions({ visible: showBOS });

      // CHoCH markers on a hidden series
      const chochData = chochList.map(c => ({ time: formatTime(c.time, isDaily), value: c.price }));
      if (chochData.length > 0) {
        chochMarkersSeries.setData(chochData);
        const chochM = chochList.map(c => {
          const isBull = c.type === 'bullish';
          return {
            time: formatTime(c.time, isDaily),
            position: isBull ? 'belowBar' : 'aboveBar',
            color: isBull ? '#81c784' : '#ba68c8',
            shape: 'circle',
            text: 'CHoCH ' + c.broken.toFixed(0),
          };
        });
        chochMarkersSeries.setMarkers(chochM);
      } else {
        chochMarkersSeries.setData([]);
        chochMarkersSeries.setMarkers([]);
      }
      chochMarkersSeries.applyOptions({ visible: showCHoCH });

      // --- Cumulative Volume Delta ---
      const cvdData = json.cvd || [];
      const cvdFormatted = cvdData.map(c => ({
        time: formatTime(c.time, isDaily),
        value: c.cumDelta,
        color: c.delta >= 0 ? 'rgba(38,166,154,0.6)' : 'rgba(239,83,80,0.6)',
      }));
      cvdSeries.setData(cvdFormatted);
      cvdSeries.applyOptions({ visible: showCVD });
      cvdSeries.priceScale().applyOptions({ visible: showCVD });

      // --- Support / Resistance ---
      if (showSR) drawSR(sr);

      // --- Buy / Sell Signal Markers ---
      const sigs = json.signals || [];
      signalMap = {};
      sigs.forEach(s => { signalMap[s.time] = s; });
      if (showSignals && sigs.length > 0) {
        const markers = sigs.map(s => {
          const isBuy = s.type.includes('BUY');
          const isStrong = s.type.includes('STRONG');
          return {
            time: formatTime(s.time, isDaily),
            position: isBuy ? 'belowBar' : 'aboveBar',
            color: isBuy ? '#26a69a' : '#ef5350',
            shape: isBuy ? 'arrowUp' : 'arrowDown',
            text: (isStrong ? '★ ' : '') + s.type.replace('_', ' ') + ' (' + s.score.toFixed(1) + ')',
          };
        });
        // Deduplicate: max 1 signal per 5 bars to avoid clutter (skip for MFactor algo)
        const filtered = [];
        let lastSigIdx = -10;
        const hasMfactor = currentAlgo.has('mfactor');
        for (let m = 0; m < markers.length; m++) {
          if (hasMfactor) {
            filtered.push(markers[m]);
          } else {
            // Find candle index for this marker time
            const mTime = typeof markers[m].time === 'object'
              ? new Date(markers[m].time.year, markers[m].time.month-1, markers[m].time.day).getTime()/1000
              : markers[m].time;
            const cIdx = candleData.findIndex(c => c.time === sigs[m].time);
            if (cIdx - lastSigIdx >= 3) {
              filtered.push(markers[m]);
              lastSigIdx = cIdx;
            }
          }
        }
        candleSeries.setMarkers(filtered);
      } else {
        candleSeries.setMarkers([]);
      }

      // --- Update Signal Panel ---
      const summ = json.signalSummary || {};
      updateSignalPanel(summ, sigs);

      // --- Update Backtest Panel ---
      lastBacktest = json.backtest || {};
      if (document.getElementById('backtestPanel').classList.contains('open')) {
        renderBacktest(lastBacktest);
      }

      // --- Paper Trading: process new signals ---
      if (paperTrading && tradeSessionId && sigs.length > 0) {
        const latestSig = sigs[sigs.length - 1];
        if (latestSig.time > lastProcessedSigTime) {
          const sigCandle = json.candles.find(c => c.time === latestSig.time);
          const sigPrice = sigCandle ? sigCandle.close : (candleData.length > 0 ? candleData[candleData.length - 1].close : 0);
          processTradeSignal(latestSig, sigPrice);
        }
        // Update unrealized P/L
        if (candleData.length > 0) {
          updateUnrealizedPnl(candleData[candleData.length - 1].close);
        }
        // Auto-refresh log panel if open
        if (tradeLogPanel.classList.contains('open')) renderTradeLog();
      }

      // Restore zoom position - preserve view to prevent flickering
      if (savedLogicalRange) {
        chart.timeScale().applyOptions({ barSpacing: savedBarSpacing });
        chart.timeScale().setVisibleLogicalRange(savedLogicalRange);
      } else {
        chart.timeScale().fitContent();
      }
      updatePriceHeader();
      updateLegendFromLast();
    } catch (err) {
      console.error('Failed to load data:', err);
    } finally {
      if (!background) loader.classList.add('hidden');
    }
  }

  // ---- Signal Panel Renderer ----
  const algoLabels = { trend: 'Trend', mstreet: 'MStreet', mfactor: 'MFactor', sniper: 'Sniper', orderflow: 'OrderFlow', priceaction: 'PriceAction', breakout: 'Breakout', momentum: 'Momentum', scalping: 'Scalping', smartmoney: 'SmartMoney', quant: 'Quant', hybrid: 'Hybrid', mpredict: 'MPredict' };
  function updateSignalPanel(summaries, sigs) {
    const box = document.getElementById('verdictBox');
    const rowsEl = document.getElementById('indicatorRows');
    const countEl = document.getElementById('signalCount');

    const keys = Object.keys(summaries || {});
    if (!keys.length) {
      box.className = 'verdict-box neutral'; box.innerHTML = 'NO DATA';
      rowsEl.innerHTML = ''; countEl.innerHTML = '';
      return;
    }

    // Composite: average scores across algos
    let totalScore = 0; let cnt = 0;
    keys.forEach(k => { if (summaries[k] && summaries[k].score != null) { totalScore += summaries[k].score; cnt++; } });
    const avgScore = cnt ? totalScore / cnt : 0;
    const overallVerdict = avgScore >= 5 ? 'STRONG BUY' : avgScore >= 3.5 ? 'BUY' : avgScore >= -3.5 ? 'NEUTRAL' : avgScore >= -5 ? 'SELL' : 'STRONG SELL';
    const cls = overallVerdict.includes('BUY') ? 'buy' : (overallVerdict.includes('SELL') ? 'sell' : 'neutral');
    box.className = 'verdict-box ' + cls;
    box.innerHTML = overallVerdict + '<div class="verdict-score">Composite: ' + avgScore.toFixed(2) + ' / 10</div>';

    // Per-algo sections
    let html = '';
    keys.forEach(k => {
      const summ = summaries[k];
      if (!summ || !summ.verdict) return;
      const label = algoLabels[k] || k;
      const vCls = summ.verdict.includes('BUY') ? 'buy' : (summ.verdict.includes('SELL') ? 'sell' : 'neutral');
      html += '<div style="margin-top:8px;padding:6px 8px;background:#181c27;border-radius:6px">';
      html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">';
      html += '<span style="font-weight:700;color:#ffd600;font-size:12px">' + label + '</span>';
      html += '<span class="verdict-box ' + vCls + '" style="font-size:11px;padding:2px 8px;border-radius:4px">' + summ.verdict + ' (' + summ.score.toFixed(1) + ')</span>';
      html += '</div>';
      (summ.indicators || []).forEach(ind => {
        const iCls = ind.weight > 0 ? 'bull' : (ind.weight < 0 ? 'bear' : 'neut');
        html += '<div class="ind-row">' +
          '<span class="ind-name">' + ind.name + '</span>' +
          '<span class="ind-status ' + iCls + '">' + ind.status + '</span>' +
          '<span class="ind-weight">' + (ind.weight > 0 ? '+' : '') + ind.weight.toFixed(1) + '</span>' +
          '</div>';
      });
      html += '</div>';
    });
    rowsEl.innerHTML = html;

    // Signal counts
    const buys = sigs.filter(s => s.type.includes('BUY')).length;
    const sells = sigs.filter(s => s.type.includes('SELL')).length;
    countEl.innerHTML = 'Signals in period: <span style="color:#26a69a">' + buys + ' Buy</span> &middot; <span style="color:#ef5350">' + sells + ' Sell</span>';
  }

  // Timeframe dropdown
  const periodDropdown = document.getElementById('periodDropdown');
  const btnPeriod = document.getElementById('btnPeriod');
  btnPeriod.addEventListener('click', function(e) {
    e.stopPropagation();
    periodDropdown.classList.toggle('open');
    indDropdown.classList.remove('open');
    cfgPanel.classList.remove('open');
    algoDropdown.classList.remove('open');
  });
  document.addEventListener('click', function(e) {
    if (!e.target.closest('.period-dropdown-wrapper')) periodDropdown.classList.remove('open');
  });
  document.querySelectorAll('.period-item').forEach(function(item) {
    item.addEventListener('click', function() {
      const tf = this.dataset.tf;
      const label = this.dataset.label;
      currentTF = tf;
      document.querySelectorAll('.period-item').forEach(function(el) {
        el.classList.remove('active');
        el.textContent = '\u200B ' + el.dataset.name;
      });
      this.classList.add('active');
      this.textContent = '\u2714 ' + this.dataset.name;
      btnPeriod.innerHTML = '<span class="dot" style="background:#4caf50"></span>' + label + ' \u25BE';
      periodDropdown.classList.remove('open');
      loadData(currentTF);
    });
  });

  // ---- Symbol Selector ----
  const symbolNames = {
    NIFTY50: { name: 'NIFTY 50', exchange: 'NSE' },
    BANKNIFTY: { name: 'BANK NIFTY', exchange: 'NSE' },
    SENSEX: { name: 'SENSEX', exchange: 'BSE' },
    GOLD: { name: 'Gold Futures', exchange: 'COMEX' },
    SILVER: { name: 'Silver Futures', exchange: 'COMEX' },
    XAUUSD: { name: 'XAU/USD', exchange: 'COMEX' },
    XAGUSD: { name: 'XAG/USD', exchange: 'COMEX' },
    GOLDTEN: { name: 'Gold ETF (10g)', exchange: 'NSE' },
    SILVERBEES: { name: 'Silver ETF', exchange: 'NSE' },
    BTC: { name: 'Bitcoin', exchange: 'CRYPTO' },
    ETH: { name: 'Ethereum', exchange: 'CRYPTO' },
    DJI: { name: 'Dow Jones', exchange: 'NYSE' },
    NASDAQ: { name: 'NASDAQ', exchange: 'NASDAQ' },
    SP500: { name: 'S&P 500', exchange: 'NYSE' },
    CRUDEOIL: { name: 'Crude Oil', exchange: 'NYMEX' },
    NATURALGAS: { name: 'Natural Gas', exchange: 'NYMEX' },
  };
  document.getElementById('symbolSelect').addEventListener('change', function() {
    currentSymbol = this.value;
    const info = symbolNames[currentSymbol] || symbolNames.NIFTY50;
    document.getElementById('tickerExchange').textContent = ' \u00b7 ' + info.exchange;
    document.getElementById('watermark').textContent = info.name;
    document.title = info.name + ' - Live Chart';
    document.getElementById('searchInput').value = '';
    loadData(currentTF);
  });

  // ---- Search Box ----
  const searchInput = document.getElementById('searchInput');
  const searchResult = document.getElementById('searchResult');
  let searchTimeout = null;

  searchInput.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      const q = this.value.trim();
      if (!q) return;
      searchResult.style.display = 'none';
      searchAndLoad(q);
    }
  });

  searchInput.addEventListener('input', function() {
    const q = this.value.trim();
    if (searchTimeout) clearTimeout(searchTimeout);
    if (q.length < 2) { searchResult.style.display = 'none'; return; }
    searchTimeout = setTimeout(() => {
      fetch('/api/search?q=' + encodeURIComponent(q))
        .then(r => r.json())
        .then(results => {
          if (!results.length) { searchResult.style.display = 'none'; return; }
          searchResult.innerHTML = results.map(r =>
            '<div class="search-result-item" data-ticker="' + r.ticker + '" data-name="' + r.name.replace(/"/g, '&quot;') + '" data-exchange="' + (r.exchange || '') + '">' +
            '<span class="sr-ticker">' + r.ticker + '</span>' +
            '<span class="sr-name">' + r.name + '</span>' +
            '<span class="sr-exch">' + (r.exchange || '') + '</span></div>'
          ).join('');
          searchResult.style.display = 'block';
        });
    }, 400);
  });

  searchResult.addEventListener('click', function(e) {
    const item = e.target.closest('.search-result-item');
    if (!item) return;
    const ticker = item.dataset.ticker;
    const name = item.dataset.name;
    const exchange = item.dataset.exchange || '';
    searchResult.style.display = 'none';
    searchInput.value = ticker;
    loadSearchedSymbol(ticker, name, exchange);
  });

  document.addEventListener('click', function(e) {
    if (!searchInput.contains(e.target) && !searchResult.contains(e.target)) {
      searchResult.style.display = 'none';
    }
  });

  function searchAndLoad(q) {
    fetch('/api/search?q=' + encodeURIComponent(q))
      .then(r => r.json())
      .then(results => {
        if (results.length) {
          loadSearchedSymbol(results[0].ticker, results[0].name, results[0].exchange || '');
        } else {
          // Try with .NS suffix for Indian stocks as last resort
          const tryTicker = q.includes('.') ? q.toUpperCase() : q.toUpperCase() + '.NS';
          loadSearchedSymbol(tryTicker, q.toUpperCase(), '');
        }
      });
  }

  function loadSearchedSymbol(ticker, name, exchange) {
    currentSymbol = ticker;
    document.getElementById('symbolSelect').value = '';
    document.getElementById('tickerExchange').textContent = exchange ? ' \u00b7 ' + exchange : '';
    document.getElementById('watermark').textContent = name;
    document.title = name + ' - Live Chart';
    loadData(currentTF);
  }

  // ---- Settings Config Panel ----
  const cfgPanel = document.getElementById('cfgPanel');
  document.getElementById('btnSettingsPanel').addEventListener('click', function(e) {
    e.stopPropagation();
    cfgPanel.classList.toggle('open');
    indDropdown.classList.remove('open');
    algoDropdown.classList.remove('open');
    if (typeof periodDropdown !== 'undefined') periodDropdown.classList.remove('open');
  });
  document.getElementById('cfgClose').addEventListener('click', function() {
    cfgPanel.classList.remove('open');
  });
  document.addEventListener('click', function(e) {
    if (!e.target.closest('.cfg-panel') && !e.target.closest('#btnSettingsPanel')) cfgPanel.classList.remove('open');
  });

  // Toggle sections open/close
  document.querySelectorAll('.cfg-toggle input').forEach(function(toggle) {
    toggle.addEventListener('change', function() {
      const body = this.closest('.cfg-section').querySelector('.cfg-section-body');
      if (this.checked) {
        body.classList.add('open');
      } else {
        body.classList.remove('open');
      }
    });
  });

  // Backtest items (algo-named)
  document.querySelectorAll('.bt-algo-item').forEach(function(item) {
    item.addEventListener('click', function() {
      cfgPanel.classList.remove('open');
      const algo = this.dataset.btAlgo;
      // Ensure the algo is selected
      if (!currentAlgo.has(algo)) {
        currentAlgo.add(algo);
        document.querySelectorAll('.algo-item').forEach(function(el) {
          if (el.dataset.algo === algo) {
            el.classList.add('active');
            el.textContent = '\u2714 ' + el.dataset.label;
          }
        });
        // Sync mpredict
        showPredictions = currentAlgo.has('mpredict');
      }
      loadData(currentTF, true).then(function() {
        const panel = document.getElementById('backtestPanel');
        panel.classList.add('open');
        renderBacktest(lastBacktest);
      });
    });
  });
  document.getElementById('btClose').addEventListener('click', function() {
    document.getElementById('backtestPanel').classList.remove('open');
  });
  document.getElementById('btQtyInput').addEventListener('change', function() {
    loadData(currentTF);
  });

  // ---- Data Source (in Settings Panel) ----
  document.querySelectorAll('.ds-cfg-item').forEach(function(item) {
    item.addEventListener('click', function() {
      const src = this.dataset.source;
      currentSource = src;
      document.querySelectorAll('.ds-cfg-item').forEach(function(el) {
        el.classList.remove('active');
        el.textContent = '\u200B ' + el.dataset.label;
      });
      this.classList.add('active');
      this.textContent = '\u2714 ' + this.dataset.label;
      loadData(currentTF, true);
    });
  });

  // ---- Algo Dropdown (multi-select) ----
  const algoDropdown = document.getElementById('algoDropdown');
  document.getElementById('btnAlgo').addEventListener('click', function(e) {
    e.stopPropagation();
    algoDropdown.classList.toggle('open');
    indDropdown.classList.remove('open');
    cfgPanel.classList.remove('open');
  });
  document.addEventListener('click', function(e) {
    if (!e.target.closest('.algo-dropdown-wrapper')) algoDropdown.classList.remove('open');
  });
  document.querySelectorAll('.algo-item').forEach(function(item) {
    item.addEventListener('click', function(e) {
      e.stopPropagation();
      const algo = this.dataset.algo;
      if (!algo) return; // skip non-algo items (e.g. Signal Analysis)
      if (currentAlgo.has(algo)) {
        currentAlgo.delete(algo);
        this.classList.remove('active');
        this.textContent = '\u200B ' + this.dataset.label;
      } else {
        currentAlgo.add(algo);
        this.classList.add('active');
        this.textContent = '\u2714 ' + this.dataset.label;
      }
      // Sync mpredict with showPredictions
      showPredictions = currentAlgo.has('mpredict');
      if (!showPredictions) {
        predSeries.setData([]);
        predSeries.applyOptions({ visible: false });
      }
      // Debounced background reload to prevent flickering
      clearTimeout(window._algoDebounce);
      window._algoDebounce = setTimeout(() => loadData(currentTF, true), 300);
    });
  });

  // Backtest panel tabs
  document.querySelectorAll('.bt-tab').forEach(tab => {
    tab.addEventListener('click', function() {
      document.querySelectorAll('.bt-tab').forEach(t => t.classList.remove('active'));
      this.classList.add('active');
      const target = this.dataset.tab;
      document.getElementById('btOverview').classList.toggle('hidden', target !== 'overview');
      document.getElementById('btPerformance').classList.toggle('hidden', target !== 'performance');
      document.getElementById('btTrades').classList.toggle('hidden', target !== 'trades');
    });
  });

  // ---- Trade Dropdown ----
    // ---- Real Trade (in Settings Panel) ----
    const realTradePanel = document.getElementById('realTradePanel');
    document.getElementById('cfgRealDelta').addEventListener('click', function(e) {
      e.stopPropagation();
      cfgPanel.classList.remove('open');
      realTradePanel.style.display = 'block';
      setTimeout(function() { realTradePanel.classList.add('open'); }, 10);
    });
    document.getElementById('rtClose').addEventListener('click', function() {
      realTradePanel.classList.remove('open');
      setTimeout(function() { realTradePanel.style.display = 'none'; }, 200);
    });
    // Dismiss modal on chart click
    container.addEventListener('click', function(e) {
      if (!e.target.closest('.realtrade-panel') && !e.target.closest('.cfg-panel')) {
        realTradePanel.classList.remove('open');
        setTimeout(function() { realTradePanel.style.display = 'none'; }, 200);
      }
    });
    // Make Delta panel draggable
    (function() {
      const panel = realTradePanel;
      const header = panel.querySelector('.rt-header');
      let isDragging = false, startX, startY, origLeft, origTop;
      header.addEventListener('mousedown', function(e) {
        if (e.target.closest('.rt-close')) return;
        isDragging = true;
        const rect = panel.getBoundingClientRect();
        const parentRect = panel.offsetParent.getBoundingClientRect();
        origLeft = rect.left - parentRect.left;
        origTop = rect.top - parentRect.top;
        startX = e.clientX;
        startY = e.clientY;
        panel.style.right = 'auto';
        panel.style.left = origLeft + 'px';
        panel.style.top = origTop + 'px';
        e.preventDefault();
      });
      document.addEventListener('mousemove', function(e) {
        if (!isDragging) return;
        panel.style.left = (origLeft + e.clientX - startX) + 'px';
        panel.style.top = (origTop + e.clientY - startY) + 'px';
      });
      document.addEventListener('mouseup', function() { isDragging = false; });
    })();
  let paperTrading = false;
  let tradeSessionId = null;
  let lastProcessedSigTime = 0;
  const tradePanel = document.getElementById('tradePanel');
  const tradeLogPanel = document.getElementById('tradeLogPanel');

  // Populate symbol dropdown
  const tpSymbol = document.getElementById('tpSymbol');
  const symbolKeys = ['NIFTY50','BANKNIFTY','SENSEX','GOLD','SILVER','XAUUSD','XAGUSD','GOLDTEN','SILVERBEES','BTC','ETH','DJI','NASDAQ','SP500','CRUDEOIL','NATURALGAS'];
  symbolKeys.forEach(function(k) {
    const opt = document.createElement('option');
    opt.value = k;
    opt.textContent = k;
    if (k === currentSymbol) opt.selected = true;
    tpSymbol.appendChild(opt);
  });

  // ---- Trade (in Settings Panel) ----
  document.getElementById('cfgTradeFutures').addEventListener('click', function(e) {
    e.stopPropagation();
    this.classList.toggle('expanded');
    document.getElementById('cfgFuturesSub').classList.toggle('open');
  });
  document.getElementById('cfgTradePositions').addEventListener('click', function(e) {
    e.stopPropagation();
    cfgPanel.classList.remove('open');
    tradeLogPanel.classList.remove('open');
    tradePanel.classList.toggle('open');
    document.getElementById('tpAlgo').value = Array.from(currentAlgo).join(',');
  });
  document.getElementById('cfgTradeLog').addEventListener('click', function(e) {
    e.stopPropagation();
    cfgPanel.classList.remove('open');
    tradePanel.classList.remove('open');
    tradeLogPanel.classList.toggle('open');
    if (tradeLogPanel.classList.contains('open')) renderTradeLog();
  });
  document.getElementById('tpClose').addEventListener('click', function() {
    tradePanel.classList.remove('open');
  });
  document.getElementById('tlClose').addEventListener('click', function() {
    tradeLogPanel.classList.remove('open');
  });

  // Click on chart dismisses trade panels
  container.addEventListener('click', function(e) {
    if (!e.target.closest('.trade-panel') && !e.target.closest('.trade-log-panel') && !e.target.closest('.cfg-panel')) {
      tradePanel.classList.remove('open');
      tradeLogPanel.classList.remove('open');
    }
  });

  // Draggable trade panels
  function makeDraggable(panel) {
    const header = panel.querySelector('.tp-header');
    let isDragging = false, startX, startY, origLeft, origTop;
    header.addEventListener('mousedown', function(e) {
      if (e.target.closest('.tp-close')) return;
      isDragging = true;
      const rect = panel.getBoundingClientRect();
      const parentRect = panel.offsetParent.getBoundingClientRect();
      origLeft = rect.left - parentRect.left;
      origTop = rect.top - parentRect.top;
      startX = e.clientX;
      startY = e.clientY;
      panel.style.right = 'auto';
      panel.style.left = origLeft + 'px';
      panel.style.top = origTop + 'px';
      e.preventDefault();
    });
    document.addEventListener('mousemove', function(e) {
      if (!isDragging) return;
      panel.style.left = (origLeft + e.clientX - startX) + 'px';
      panel.style.top = (origTop + e.clientY - startY) + 'px';
    });
    document.addEventListener('mouseup', function() {
      isDragging = false;
    });
  }
  makeDraggable(tradePanel);
  makeDraggable(tradeLogPanel);

  // Start / Stop trading
  const tpStartBtn = document.getElementById('tpStartBtn');
  tpStartBtn.addEventListener('click', async function() {
    if (!paperTrading) {
      // START
      const symbol = tpSymbol.value;
      const capital = parseFloat(document.getElementById('tpCapital').value) || 100000;
      const tradeAlgo = document.getElementById('tpAlgo').value;
      try {
        const resp = await fetch('/api/trade/start', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({symbol: symbol, capital: capital, algo: tradeAlgo})
        });
        const data = await resp.json();
        tradeSessionId = data.sessionId;
        paperTrading = true;
        lastProcessedSigTime = 0;
        tpStartBtn.textContent = 'Stop Trading';
        tpStartBtn.classList.remove('start');
        tpStartBtn.classList.add('stop');
        tpSymbol.disabled = true;
        document.getElementById('tpCapital').disabled = true;
        document.getElementById('tpAlgo').disabled = true;
        document.getElementById('tpStatus').classList.add('visible');
        updateTradeStatus({
          totalTrades: 0, netProfit: 0, winRate: 0, maxDrawdown: 0,
          initialCapital: capital, finalCapital: capital
        }, 0, 0, 0, capital);
        // Enable live mode if not already
        if (!liveMode) {
          document.getElementById('btnLive').click();
        }
      } catch(err) {
        console.error('Trade start error:', err);
      }
    } else {
      // STOP
      const lastPrice = candleData.length > 0 ? candleData[candleData.length - 1].close : 0;
      try {
        const resp = await fetch('/api/trade/stop', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({sessionId: tradeSessionId, price: lastPrice})
        });
        const data = await resp.json();
        paperTrading = false;
        tpStartBtn.textContent = 'Start Trading';
        tpStartBtn.classList.remove('stop');
        tpStartBtn.classList.add('start');
        tpSymbol.disabled = false;
        document.getElementById('tpCapital').disabled = false;
        document.getElementById('tpAlgo').disabled = false;
        if (data.summary) {
          updateTradeStatus(data.summary, 0, 0, 0, data.summary.finalCapital);
        }
      } catch(err) {
        console.error('Trade stop error:', err);
      }
    }
  });

  function updateTradeStatus(summary, position, entryPrice, qty, capital) {
    document.getElementById('tpPosStatus').textContent = position === 1 ? 'LONG' : 'Flat';
    document.getElementById('tpPosStatus').className = 'val' + (position === 1 ? ' positive' : '');
    document.getElementById('tpEntryPrice').textContent = entryPrice > 0 ? fmtNum(entryPrice) : '-';
    document.getElementById('tpQty').textContent = qty > 0 ? qty : '-';
    document.getElementById('tpCurCapital').textContent = '\u20B9' + fmtNum(capital);
    document.getElementById('tpTotalTrades').textContent = summary.totalTrades || 0;
    const netPnl = summary.netProfit || 0;
    const netEl = document.getElementById('tpNetPnl');
    netEl.textContent = (netPnl >= 0 ? '+' : '') + '\u20B9' + fmtNum(netPnl);
    netEl.className = 'val ' + (netPnl >= 0 ? 'positive' : 'negative');
    const wr = document.getElementById('tpWinRate');
    wr.textContent = summary.winRate !== undefined ? fmtNum(summary.winRate) + '%' : '-';
    wr.className = 'val ' + (summary.winRate >= 50 ? 'positive' : 'negative');
    const dd = document.getElementById('tpMaxDD');
    dd.textContent = '\u20B9' + fmtNum(summary.maxDrawdown || 0) + ' (' + fmtNum(summary.maxDrawdownPct || 0) + '%)';
    dd.className = 'val negative';
  }

  function updateUnrealizedPnl(currentPrice) {
    if (!paperTrading || !tradeSessionId) return;
    const entryP = parseFloat(document.getElementById('tpEntryPrice').textContent.replace(/,/g, ''));
    const qtyText = document.getElementById('tpQty').textContent;
    if (isNaN(entryP) || qtyText === '-') {
      document.getElementById('tpUnrealPnl').textContent = '-';
      document.getElementById('tpUnrealPnl').className = 'val';
      return;
    }
    const qty = parseInt(qtyText);
    const unrealPnl = (currentPrice - entryP) * qty;
    const el = document.getElementById('tpUnrealPnl');
    el.textContent = (unrealPnl >= 0 ? '+' : '') + '\u20B9' + fmtNum(unrealPnl);
    el.className = 'val ' + (unrealPnl >= 0 ? 'positive' : 'negative');
  }

  async function processTradeSignal(signal, price) {
    if (!paperTrading || !tradeSessionId) return;
    if (signal.time <= lastProcessedSigTime) return;
    try {
      const resp = await fetch('/api/trade/execute', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          sessionId: tradeSessionId,
          signalType: signal.type,
          price: price,
          time: signal.time
        })
      });
      const data = await resp.json();
      if (data.status === 'ok') {
        lastProcessedSigTime = signal.time;
        // Refresh status from server
        const statusResp = await fetch('/api/trade/status?session_id=' + tradeSessionId);
        const statusData = await statusResp.json();
        updateTradeStatus(
          statusData.summary,
          statusData.position,
          statusData.entryPrice,
          statusData.qty,
          statusData.capital
        );
      }
    } catch(err) {
      console.error('Trade execute error:', err);
    }
  }

  function renderTradeLog() {
    const body = document.getElementById('tradeLogBody');
    if (!tradeSessionId) {
      body.innerHTML = '<div style="text-align:center;padding:30px;color:#787b86">No trades yet. Start a Futures position first.</div>';
      return;
    }
    fetch('/api/trade/status?session_id=' + tradeSessionId)
      .then(r => r.json())
      .then(data => {
        const trades = data.trades || [];
        const summary = data.summary || {};
        if (trades.length === 0) {
          body.innerHTML = '<div style="text-align:center;padding:30px;color:#787b86">No trades executed yet. Waiting for signals...</div>';
          return;
        }
        let html = '<table class="bt-trade-table"><thead><tr>' +
          '<th>#</th><th>Entry</th><th>Exit</th><th>Qty</th><th>Entry &#8377;</th><th>Exit &#8377;</th><th>P&L</th><th>%</th>' +
          '</tr></thead><tbody>';
        trades.forEach(function(tr, i) {
          const cls = tr.pnl >= 0 ? 'positive' : 'negative';
          const barW = Math.min(Math.abs(tr.pnlPct) * 5, 60);
          const barColor = tr.pnl >= 0 ? '#26a69a' : '#ef5350';
          html += '<tr>' +
            '<td>' + (i + 1) + '</td>' +
            '<td>' + fmtTime(tr.entryTime) + '</td>' +
            '<td>' + fmtTime(tr.exitTime) + (tr.forced ? ' &#9888;' : '') + '</td>' +
            '<td>' + tr.qty + '</td>' +
            '<td>' + fmtNum(tr.entryPrice) + '</td>' +
            '<td>' + fmtNum(tr.exitPrice) + '</td>' +
            '<td class="' + cls + '">' + (tr.pnl >= 0 ? '+' : '') + fmtNum(tr.pnl) +
              ' <span class="bt-pnl-bar" style="background:' + barColor + ';width:' + barW + 'px"></span></td>' +
            '<td class="' + cls + '">' + (tr.pnlPct >= 0 ? '+' : '') + fmtNum(tr.pnlPct) + '%</td>' +
            '</tr>';
        });
        html += '</tbody></table>';
        // Summary footer
        const npClass = (summary.netProfit || 0) >= 0 ? 'positive' : 'negative';
        html += '<div class="bt-equity-box" style="margin-top:12px">' +
          '<div class="bt-equity-row"><span class="label">Initial Capital</span><span class="val">&#8377;' + fmtNum(summary.initialCapital) + '</span></div>' +
          '<div class="bt-equity-row"><span class="label">Final Capital</span><span class="val ' + npClass + '">&#8377;' + fmtNum(summary.finalCapital) + '</span></div>' +
          '<div class="bt-equity-row"><span class="label">Net P/L</span><span class="val ' + npClass + '">&#8377;' + fmtNum(summary.netProfit) + ' (' + fmtNum(summary.netProfitPct) + '%)</span></div>' +
          '</div>' +
          '<div class="bt-stat-grid">' +
            statCell('Total Trades', summary.totalTrades, '') +
            statCell('Win Rate', fmtNum(summary.winRate) + '%', (summary.winRate || 0) >= 50 ? 'positive' : 'negative') +
            statCell('Profit Factor', summary.profitFactor, '') +
            statCell('Avg Trade', '&#8377;' + fmtNum(summary.avgTrade), (summary.avgTrade || 0) >= 0 ? 'positive' : 'negative') +
            statCell('Avg Win', '&#8377;' + fmtNum(summary.avgWin), 'positive') +
            statCell('Avg Loss', '&#8377;' + fmtNum(summary.avgLoss), 'negative') +
            statCell('Largest Win', '&#8377;' + fmtNum(summary.largestWin), 'positive') +
            statCell('Largest Loss', '&#8377;' + fmtNum(summary.largestLoss), 'negative') +
            statCell('Max Drawdown', '&#8377;' + fmtNum(summary.maxDrawdown) + ' (' + fmtNum(summary.maxDrawdownPct) + '%)', 'negative') +
          '</div>';
        body.innerHTML = html;
      })
      .catch(function(err) {
        body.innerHTML = '<div style="text-align:center;padding:30px;color:#ef5350">Error loading trade log.</div>';
      });
  }

  function fmtNum(n, decimal) {
    if (n === undefined || n === null) return '-';
    if (typeof n === 'string') return n;
    const d = decimal !== undefined ? decimal : 2;
    return n.toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d });
  }

  function fmtTime(ts) {
    // Timestamps already have IST offset baked in, use UTC methods
    const d = new Date(ts * 1000);
    const dd = String(d.getUTCDate()).padStart(2, '0');
    const mm = String(d.getUTCMonth() + 1).padStart(2, '0');
    const yy = String(d.getUTCFullYear()).slice(2);
    const hh = String(d.getUTCHours()).padStart(2, '0');
    const mi = String(d.getUTCMinutes()).padStart(2, '0');
    return dd + '/' + mm + '/' + yy + ' ' + hh + ':' + mi + ' IST';
  }

  function renderBacktest(bt) {
    const s = bt.summary || {};
    const trades = bt.trades || [];
    const overviewEl = document.getElementById('btOverview');
    const perfEl = document.getElementById('btPerformance');
    const tradesEl = document.getElementById('btTrades');

    if (!s.totalTrades) {
      overviewEl.innerHTML = '<div style="text-align:center;padding:40px;color:#787b86">No trades generated.<br>Signals need both BUY and SELL to create trades.</div>';
      perfEl.innerHTML = '';
      tradesEl.innerHTML = '';
      return;
    }

    const npClass = s.netProfit >= 0 ? 'positive' : 'negative';
    const bhClass = s.buyHoldPnl >= 0 ? 'positive' : 'negative';

    // Overview tab
    overviewEl.innerHTML =
      '<div class="bt-equity-box">' +
        '<div class="bt-equity-row"><span class="label">Initial Capital</span><span class="val">&#8377;' + fmtNum(s.initialCapital) + '</span></div>' +
        '<div class="bt-equity-row"><span class="label">Final Capital</span><span class="val ' + npClass + '">&#8377;' + fmtNum(s.finalCapital) + '</span></div>' +
        '<div class="bt-equity-row"><span class="label">Net Profit</span><span class="val ' + npClass + '">&#8377;' + fmtNum(s.netProfit) + ' (' + fmtNum(s.netProfitPct) + '%)</span></div>' +
        '<div class="bt-equity-row"><span class="label">Buy &amp; Hold</span><span class="val ' + bhClass + '">&#8377;' + fmtNum(s.buyHoldPnl) + ' (' + fmtNum(s.buyHoldPct) + '%)</span></div>' +
      '</div>' +
      '<div class="bt-stat-grid">' +
        statCell('Total Trades', s.totalTrades, '') +
        statCell('Profit Factor', s.profitFactor, s.profitFactor !== '∞' && s.profitFactor >= 1 ? 'positive' : (s.profitFactor !== '∞' ? 'negative' : '')) +
        statCell('Win Rate', fmtNum(s.winRate) + '%', s.winRate >= 50 ? 'positive' : 'negative') +
        statCell('Sharpe Ratio', fmtNum(s.sharpeRatio), s.sharpeRatio >= 0 ? 'positive' : 'negative') +
        statCell('Max Drawdown', '&#8377;' + fmtNum(s.maxDrawdown) + ' (' + fmtNum(s.maxDrawdownPct) + '%)', 'negative') +
        statCell('Expectancy', '&#8377;' + fmtNum(s.expectancy), s.expectancy >= 0 ? 'positive' : 'negative') +
      '</div>' +
      '<div class="disclaimer" style="margin-top:12px">Backtest based on composite signal engine. Past performance does not guarantee future results. Slippage and commissions not included.</div>';

    // Performance tab
    perfEl.innerHTML =
      '<div class="bt-section-title">Profit Analysis</div>' +
      '<div class="bt-stat-grid">' +
        statCell('Gross Profit', '&#8377;' + fmtNum(s.grossProfit), 'positive') +
        statCell('Gross Loss', '&#8377;' + fmtNum(s.grossLoss), 'negative') +
        statCell('Net Profit', '&#8377;' + fmtNum(s.netProfit), npClass) +
        statCell('Profit Factor', s.profitFactor, '') +
      '</div>' +
      '<div class="bt-section-title">Trade Analysis</div>' +
      '<div class="bt-stat-grid">' +
        statCell('Total Trades', s.totalTrades, '') +
        statCell('Winning', s.winningTrades, 'positive') +
        statCell('Losing', s.losingTrades, 'negative') +
        statCell('Breakeven', s.breakevenTrades, '') +
        statCell('Win Rate', fmtNum(s.winRate) + '%', s.winRate >= 50 ? 'positive' : 'negative') +
        statCell('Loss Rate', fmtNum(s.lossRate) + '%', '') +
      '</div>' +
      '<div class="bt-section-title">Average Trade</div>' +
      '<div class="bt-stat-grid">' +
        statCell('Avg Trade P&L', '&#8377;' + fmtNum(s.avgTrade), s.avgTrade >= 0 ? 'positive' : 'negative') +
        statCell('Avg Win', '&#8377;' + fmtNum(s.avgWin), 'positive') +
        statCell('Avg Loss', '&#8377;' + fmtNum(s.avgLoss), 'negative') +
        statCell('Payoff Ratio', s.payoffRatio, '') +
      '</div>' +
      '<div class="bt-section-title">Extremes</div>' +
      '<div class="bt-stat-grid">' +
        statCell('Largest Win', '&#8377;' + fmtNum(s.largestWin), 'positive') +
        statCell('Largest Loss', '&#8377;' + fmtNum(s.largestLoss), 'negative') +
        statCell('Max Consec. Wins', s.maxConsecWins, 'positive') +
        statCell('Max Consec. Losses', s.maxConsecLosses, 'negative') +
      '</div>' +
      '<div class="bt-section-title">Risk</div>' +
      '<div class="bt-stat-grid">' +
        statCell('Max Drawdown', '&#8377;' + fmtNum(s.maxDrawdown) + ' (' + fmtNum(s.maxDrawdownPct) + '%)', 'negative') +
        statCell('Sharpe Ratio', fmtNum(s.sharpeRatio), s.sharpeRatio >= 0 ? 'positive' : 'negative') +
        statCell('Expectancy', '&#8377;' + fmtNum(s.expectancy), s.expectancy >= 0 ? 'positive' : 'negative') +
        statCell('Buy & Hold Return', fmtNum(s.buyHoldPct) + '%', bhClass) +
      '</div>';

    // Trade List tab
    let thtml = '<table class="bt-trade-table"><thead><tr>' +
      '<th>#</th><th>Entry</th><th>Exit</th><th>Qty</th><th>Entry &#8377;</th><th>Exit &#8377;</th><th>P&L</th><th>%</th>' +
      '</tr></thead><tbody>';
    trades.forEach((tr, i) => {
      const cls = tr.pnl >= 0 ? 'positive' : 'negative';
      const barW = Math.min(Math.abs(tr.pnlPct) * 5, 60);
      const barColor = tr.pnl >= 0 ? '#26a69a' : '#ef5350';
      thtml += '<tr>' +
        '<td>' + (i + 1) + '</td>' +
        '<td>' + fmtTime(tr.entryTime) + '</td>' +
        '<td>' + fmtTime(tr.exitTime) + (tr.open ? ' &#128994;' : '') + '</td>' +
        '<td>' + tr.qty + '</td>' +
        '<td>' + fmtNum(tr.entryPrice) + '</td>' +
        '<td>' + fmtNum(tr.exitPrice) + '</td>' +
        '<td class="' + cls + '">' + (tr.pnl >= 0 ? '+' : '') + fmtNum(tr.pnl) +
          ' <span class="bt-pnl-bar" style="background:' + barColor + ';width:' + barW + 'px"></span></td>' +
        '<td class="' + cls + '">' + (tr.pnlPct >= 0 ? '+' : '') + fmtNum(tr.pnlPct) + '%</td>' +
        '</tr>';
    });
    thtml += '</tbody></table>';
    tradesEl.innerHTML = thtml;
  }

  function statCell(label, value, cls) {
    return '<div class="bt-stat"><span class="bt-stat-label">' + label +
      '</span><span class="bt-stat-value ' + (cls || '') + '">' + value + '</span></div>';
  }

  // Resize handler
  const ro = new ResizeObserver(() => {
    chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
  });
  ro.observe(container);

  // ---- Live Data Button ----
  const btnLive = document.getElementById('btnLive');
  btnLive.addEventListener('click', function() {
    liveMode = !liveMode;
    this.classList.toggle('active');
    if (liveMode) {
      // Start live feed: fetch every 10 seconds in background
      loadData(currentTF, true);
      liveInterval = setInterval(() => loadData(currentTF, true), 5000);
    } else {
      // Stop live feed
      if (liveInterval) { clearInterval(liveInterval); liveInterval = null; }
    }
  });

  // ---- Zoom Dropdown ----
  const zoomDropdown = document.getElementById('zoomDropdown');
  document.getElementById('btnZoom').addEventListener('click', function(e) {
    e.stopPropagation();
    zoomDropdown.classList.toggle('open');
    if (typeof periodDropdown !== 'undefined') periodDropdown.classList.remove('open');
    indDropdown.classList.remove('open');
    cfgPanel.classList.remove('open');
    algoDropdown.classList.remove('open');
  });
  document.addEventListener('click', function(e) {
    if (!e.target.closest('.zoom-dropdown-wrapper')) zoomDropdown.classList.remove('open');
  });

  // ---- Help Dropdown ----
  const helpDropdown = document.getElementById('helpDropdown');
  document.getElementById('btnHelp').addEventListener('click', function(e) {
    e.stopPropagation();
    helpDropdown.classList.toggle('open');
    zoomDropdown.classList.remove('open');
    indDropdown.classList.remove('open');
    cfgPanel.classList.remove('open');
    algoDropdown.classList.remove('open');
  });
  document.addEventListener('click', function(e) {
    if (!e.target.closest('.help-dropdown-wrapper')) helpDropdown.classList.remove('open');
  });

  // ---- Zoom Controls ----
  document.getElementById('zoomHIn').addEventListener('click', () => {
    const ts = chart.timeScale();
    const range = ts.getVisibleLogicalRange();
    if (range) {
      const center = (range.from + range.to) / 2;
      const half = (range.to - range.from) / 2 * 0.7;
      ts.setVisibleLogicalRange({ from: center - half, to: center + half });
    }
  });
  document.getElementById('zoomHOut').addEventListener('click', () => {
    const ts = chart.timeScale();
    const range = ts.getVisibleLogicalRange();
    if (range) {
      const center = (range.from + range.to) / 2;
      const half = (range.to - range.from) / 2 * 1.4;
      ts.setVisibleLogicalRange({ from: center - half, to: center + half });
    }
  });
  document.getElementById('zoomVIn').addEventListener('click', () => {
    const ps = candleSeries.priceScale();
    const opts = chart.priceScale('right').options();
    const curTop = opts.scaleMargins ? opts.scaleMargins.top : 0.1;
    const curBot = opts.scaleMargins ? opts.scaleMargins.bottom : 0.2;
    const newTop = Math.min(curTop + 0.05, 0.45);
    const newBot = Math.min(curBot + 0.05, 0.45);
    chart.priceScale('right').applyOptions({ scaleMargins: { top: newTop, bottom: newBot } });
  });
  document.getElementById('zoomVOut').addEventListener('click', () => {
    const opts = chart.priceScale('right').options();
    const curTop = opts.scaleMargins ? opts.scaleMargins.top : 0.1;
    const curBot = opts.scaleMargins ? opts.scaleMargins.bottom : 0.2;
    const newTop = Math.max(curTop - 0.05, 0.02);
    const newBot = Math.max(curBot - 0.05, 0.02);
    chart.priceScale('right').applyOptions({ scaleMargins: { top: newTop, bottom: newBot } });
  });
  document.getElementById('zoomReset').addEventListener('click', () => {
    chart.priceScale('right').applyOptions({ scaleMargins: { top: 0.1, bottom: 0.2 } });
    chart.timeScale().fitContent();
  });

  // Initial load
  loadData(currentTF);

  // Auto-refresh every 60 seconds (only when not in live mode, background)
  setInterval(() => { if (!liveMode) loadData(currentTF, true); }, 60000);

  // ---- Theme Toggle ----
  function applyTheme(theme) {
    const isLight = theme === 'light';
    document.documentElement.classList.toggle('light-theme', isLight);
    chart.applyOptions({
      layout: {
        background: { type: 'solid', color: isLight ? '#ffffff' : '#131722' },
        textColor: isLight ? '#787b86' : '#787b86',
      },
      grid: {
        vertLines: { color: isLight ? '#e0e3eb' : '#1e222d' },
        horzLines: { color: isLight ? '#e0e3eb' : '#1e222d' },
      },
      rightPriceScale: { borderColor: isLight ? '#e0e3eb' : '#2a2e39' },
      timeScale: { borderColor: isLight ? '#e0e3eb' : '#2a2e39' },
    });
    const btnTheme = document.getElementById('btnTheme');
    btnTheme.innerHTML = isLight ? '&#9728; Theme' : '&#127763; Theme';
    localStorage.setItem('mangal_theme', theme);
  }
  // Restore saved theme
  const savedTheme = localStorage.getItem('mangal_theme') || 'dark';
  if (savedTheme === 'light') applyTheme('light');
  document.getElementById('btnTheme').addEventListener('click', function() {
    const current = document.documentElement.classList.contains('light-theme') ? 'light' : 'dark';
    applyTheme(current === 'light' ? 'dark' : 'light');
  });

  // ---- Site Settings (admin-controlled panel visibility) ----
  fetch('/api/site-settings')
    .then(r => r.json())
    .then(settings => {
      // Settings panel sections
      const sectionMap = {
        'settings_backtest': 'cfgBacktestToggle',
        'settings_datasource': 'cfgDataSourceToggle',
        'settings_trade': 'cfgTradeToggle',
        'settings_realtrade': 'cfgRealTradeToggle',
      };
      for (const [key, toggleId] of Object.entries(sectionMap)) {
        if (settings[key] === 'off') {
          const toggle = document.getElementById(toggleId);
          if (toggle) {
            const section = toggle.closest('.cfg-section');
            if (section) section.style.display = 'none';
          }
        }
      }
      // Menu visibility: Symbols
      if (settings.menu_symbols) {
        try {
          const enabled = JSON.parse(settings.menu_symbols);
          document.querySelectorAll('#symbolSelect option').forEach(opt => {
            if (enabled.indexOf(opt.value) < 0) opt.style.display = 'none';
          });
        } catch(e) {}
      }
      // Menu visibility: Timeframes
      if (settings.menu_timeframes) {
        try {
          const enabled = JSON.parse(settings.menu_timeframes);
          document.querySelectorAll('.period-item[data-tf]').forEach(btn => {
            if (enabled.indexOf(btn.dataset.tf) < 0) btn.style.display = 'none';
          });
        } catch(e) {}
      }
      // Menu visibility: Indicators
      if (settings.menu_indicators) {
        try {
          const enabled = JSON.parse(settings.menu_indicators);
          document.querySelectorAll('.ind-item[data-ind]').forEach(el => {
            if (enabled.indexOf(el.dataset.ind) < 0) el.style.display = 'none';
          });
        } catch(e) {}
      }
      // Menu visibility: Algos
      if (settings.menu_algos) {
        try {
          const enabled = JSON.parse(settings.menu_algos);
          document.querySelectorAll('.algo-item[data-algo]').forEach(btn => {
            if (enabled.indexOf(btn.dataset.algo) < 0) btn.style.display = 'none';
          });
        } catch(e) {}
      }
    })
    .catch(() => {});
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print("Starting Mangal View Server...")
    print(f"Open http://localhost:{port} in your browser")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
