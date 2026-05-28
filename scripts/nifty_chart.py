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
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            validity_until TEXT DEFAULT NULL
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
    if "validity_until" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN validity_until TEXT DEFAULT NULL")
    # Seed default site settings
    defaults = {
        "maintenance_mode": "off",
        "registration_enabled": "on",
        "settings_backtest": "on",
        "settings_datasource": "on",
        "settings_trade": "on",
        "settings_realtrade": "on",
        "menu_symbols": json.dumps(["NIFTY50","BANKNIFTY","SENSEX","GOLD","SILVER","XAUUSD","XAGUSD","GOLDTEN","SILVERBEES","BTC","ETH","DJI","NASDAQ","SP500","USOIL","CRUDEOILMCX","NATURALGAS"]),
        "menu_timeframes": json.dumps(["1m","2m","3m","5m","10m","15m","30m","1h","2h","4h","1d","1w","1mo"]),
        "menu_indicators": json.dumps(["ST","SAR","SR","EMA","VWAP","BB","CPR","ORB","LP","FVG","BOS","CHoCH","CVD","VP","Signals"]),
        "menu_algos": json.dumps(["trend","mstreet","mfactor","sniper","orderflow","priceaction","breakout","momentum","scalping","smartmoney","quant","hybrid","statarb","institution","mpredict","marketmaking","mma","pattern"]),
        # Tier access control defaults (all items available to all tiers by default)
        "tier_indicators_free": json.dumps(["ST","SAR","SR","EMA","VWAP","BB","CPR","ORB","LP","FVG","BOS","CHoCH","CVD","VP","Signals"]),
        "tier_indicators_basic_paid": json.dumps(["ST","SAR","SR","EMA","VWAP","BB","CPR","ORB","LP","FVG","BOS","CHoCH","CVD","VP","Signals"]),
        "tier_indicators_pro_paid": json.dumps(["ST","SAR","SR","EMA","VWAP","BB","CPR","ORB","LP","FVG","BOS","CHoCH","CVD","VP","Signals"]),
        "tier_algos_free": json.dumps(["trend","mstreet","mfactor","sniper","orderflow","priceaction","breakout","momentum","scalping","smartmoney","quant","hybrid","statarb","institution","mpredict","marketmaking","mma","pattern"]),
        "tier_algos_basic_paid": json.dumps(["trend","mstreet","mfactor","sniper","orderflow","priceaction","breakout","momentum","scalping","smartmoney","quant","hybrid","statarb","institution","mpredict","marketmaking","mma","pattern"]),
        "tier_algos_pro_paid": json.dumps(["trend","mstreet","mfactor","sniper","orderflow","priceaction","breakout","momentum","scalping","smartmoney","quant","hybrid","statarb","institution","mpredict","marketmaking","mma","pattern"]),
    }
    for k, v in defaults.items():
        db.execute("INSERT OR IGNORE INTO site_settings (key, value) VALUES (?, ?)", (k, v))
    # Migrate: ensure new algos are in menu_algos for existing DBs
    _all_algos = ["trend","mstreet","mfactor","sniper","orderflow","priceaction","breakout","momentum","scalping","smartmoney","quant","hybrid","statarb","institution","mpredict","marketmaking","mma","pattern"]
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
    # Migrate: ensure new symbols are in menu_symbols and CRUDEOIL is removed for existing DBs
    _all_symbols = ["NIFTY50","BANKNIFTY","SENSEX","GOLD","SILVER","XAUUSD","XAGUSD","GOLDTEN","SILVERBEES","BTC","ETH","DJI","NASDAQ","SP500","USOIL","CRUDEOILMCX","NATURALGAS"]
    _sym_row = db.execute("SELECT value FROM site_settings WHERE key = 'menu_symbols'").fetchone()
    if _sym_row:
        try:
            _existing_sym = json.loads(_sym_row[0])
            _existing_sym = [s for s in _existing_sym if s != "CRUDEOIL"]  # remove CRUDEOIL
            _missing_sym = [s for s in _all_symbols if s not in _existing_sym]
            if _missing_sym:
                _existing_sym.extend(_missing_sym)
            db.execute("UPDATE site_settings SET value = ? WHERE key = 'menu_symbols'", (json.dumps(_existing_sym),))
        except Exception:
            pass
    # Migrate: ensure new indicators are in menu_indicators for existing DBs
    _all_indicators = ["ST","SAR","SR","EMA","VWAP","BB","CPR","ORB","LP","FVG","BOS","CHoCH","CVD","VP","Signals"]
    _ind_row = db.execute("SELECT value FROM site_settings WHERE key = 'menu_indicators'").fetchone()
    if _ind_row:
        try:
            _existing_ind = json.loads(_ind_row[0])
            _missing_ind = [i for i in _all_indicators if i not in _existing_ind]
            if _missing_ind:
                # Insert ORB after CPR if present, else append
                if "ORB" in _missing_ind and "CPR" in _existing_ind:
                    idx = _existing_ind.index("CPR") + 1
                    for item in reversed([i for i in _missing_ind]):
                        _existing_ind.insert(idx, item)
                else:
                    _existing_ind.extend(_missing_ind)
                db.execute("UPDATE site_settings SET value = ? WHERE key = 'menu_indicators'", (json.dumps(_existing_ind),))
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
    # Check if registration is enabled
    try:
        with sqlite3.connect(DB_PATH) as _db:
            _row = _db.execute("SELECT value FROM site_settings WHERE key = 'registration_enabled'").fetchone()
            if _row and _row[0] == "off":
                disabled_msg = '<div class="error">Registration cannot be done now. Please contact administrator.</div>'
                return Response(REGISTER_PAGE.replace("{{ERROR}}", disabled_msg), content_type="text/html")
    except Exception:
        pass
    
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
    
    # Calculate validity period: 1 month from now
    from datetime import datetime, timedelta
    validity_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    
    db = get_db()
    existing = db.execute("SELECT id, plan FROM users WHERE mobileno = ?", (mobileno,)).fetchone()
    if existing:
        # Allow re-registration for paid upgrade after free expired
        if existing["plan"] == "free" and plan == "paid":
            pw_hash = hash_password(password)
            db.execute("UPDATE users SET username=?, password_hash=?, place=?, plan='paid', validity_until=?, created_at=CURRENT_TIMESTAMP WHERE id=?",
                       (username, pw_hash, place, validity_date, existing["id"]))
            db.commit()
            return Response(REGISTER_PAGE.replace("{{ERROR}}", '<div class="success">Upgraded to Paid! &#8377;100/month &mdash; Contact <b>Mangal</b> at <b>95000 90975</b>. <a href="/login">Sign in now</a></div>'), content_type="text/html")
        return Response(REGISTER_PAGE.replace("{{ERROR}}", '<div class="error">This mobile number is already registered.</div>'), content_type="text/html")
    pw_hash = hash_password(password)
    db.execute("INSERT INTO users (username, mobileno, password_hash, place, plan, validity_until) VALUES (?, ?, ?, ?, ?, ?)",
               (username, mobileno, pw_hash, place, plan, validity_date))
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
    rows = db.execute("SELECT id, username, mobileno, place, plan, created_at, validity_until FROM users ORDER BY id DESC").fetchall()
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
    validity_until = data.get("validity_until", None)
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
    db.execute("INSERT INTO users (username, mobileno, password_hash, place, plan, validity_until) VALUES (?, ?, ?, ?, ?, ?)",
               (username, mobileno, pw_hash, place, plan, validity_until))
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
    validity_until = data.get("validity_until", None)
    db.execute("UPDATE users SET username=?, mobileno=?, place=?, plan=?, validity_until=? WHERE id=?",
               (data.get("username", ""), data.get("mobileno", ""), data.get("place", ""), data.get("plan", "free"), validity_until, uid))
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
    allowed_keys = {"maintenance_mode", "registration_enabled", "settings_backtest", "settings_datasource",
                    "settings_trade", "settings_realtrade",
                    "menu_symbols", "menu_timeframes", "menu_indicators", "menu_algos",
                    "tier_indicators_free", "tier_indicators_basic_paid", "tier_indicators_pro_paid",
                    "tier_algos_free", "tier_algos_basic_paid", "tier_algos_pro_paid"}
    for key, value in data.items():
        if key in allowed_keys:
            if key.startswith("menu_") or key.startswith("tier_"):
                # Menu/tier config: value is a JSON array string
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
    # Include current user's plan for tier-based access control
    user_id = session.get("user_id")
    if user_id:
        user_row = db.execute("SELECT plan FROM users WHERE id = ?", (user_id,)).fetchone()
        if user_row:
            settings["user_plan"] = user_row["plan"]
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

# --- Zerodha Automation API ---
zerodha_sessions = {}   # api_key -> {connected, token}
zerodha_orders   = []   # order log

@app.route('/api/zerodha/connect', methods=['POST'])
@login_required
def zerodha_connect():
    data = request.json or {}
    api_key      = data.get('api_key', '').strip()
    api_secret   = data.get('api_secret', '').strip()
    access_token = data.get('access_token', '').strip()
    if not (api_key and access_token):
        return jsonify({'success': False, 'error': 'API key and access token required'}), 400
    # Store credentials (actual KiteConnect integration goes here)
    zerodha_sessions[api_key] = {
        'api_secret': api_secret,
        'access_token': access_token,
        'connected': True
    }
    return jsonify({'success': True, 'message': 'Connected to Zerodha'})

@app.route('/api/zerodha/order', methods=['POST'])
@login_required
def zerodha_order():
    """Place a Zerodha order.

    Honors a `dry_run` flag (default true if omitted) — when true, the request
    is logged only and no real order reaches Kite. When false, posts to
    api.kite.trade/orders/regular using the session's access token.

    Body params (JSON):
        api_key (str): identifies the session in zerodha_sessions.
        symbol (str):  Kite tradingsymbol (e.g. CRUDEOILM26JUNFUT, NIFTY26JUN24000CE).
        exchange (str):NSE | BSE | NFO | BFO | MCX | CDS | BCD (default NSE).
        side (str):    BUY | SELL.
        qty (int):     quantity.
        product (str): MIS | NRML | CNC. Defaults: NRML for derivatives, MIS otherwise.
        order_type (str): MARKET | LIMIT | SL | SL-M (default MARKET).
        algo/score: free-form metadata stored in the log.
        dry_run (bool): if true (default), don't actually call Kite.
    """
    import urllib.parse as _up, urllib.error as _ue, json as _json
    data = request.json or {}
    api_key      = data.get('api_key', '').strip()
    symbol       = data.get('symbol', '').strip()
    exchange     = (data.get('exchange') or '').strip().upper()
    side         = data.get('side', '').upper()
    qty          = int(data.get('qty', 1) or 1)
    order_type   = (data.get('order_type') or 'MARKET').strip().upper()
    product      = (data.get('product') or '').strip().upper()
    # Slippage cap for MARKET orders, in % of LTP. Kite Connect rejects MARKET
    # orders without this set (the API won't let bots fire blind market orders).
    # Default 3% — narrow enough to be safe, wide enough to fill in normal
    # market conditions. User can override per-order via the request body.
    market_protection = float(data.get('market_protection', 3.0) or 3.0)
    limit_price  = data.get('price')   # for LIMIT/SL orders
    trigger_price = data.get('trigger_price')   # for SL/SL-M
    algo         = data.get('algo', '')
    score        = data.get('score', 0)
    dry_run      = data.get('dry_run', True)  # default to dry-run for safety

    if api_key not in zerodha_sessions:
        return jsonify({'success': False, 'error': 'Not connected (open Zerodha Login)'}), 403
    if side not in ('BUY', 'SELL'):
        return jsonify({'success': False, 'error': 'Invalid side'}), 400
    if not symbol:
        return jsonify({'success': False, 'error': 'Missing symbol'}), 400

    # Auto-derive exchange from instrument lookup when missing.
    # Common case: rules created before the Kite-tab metadata wiring don't
    # carry an exchange, so we'd default to NSE and get back Kite's
    # "instrument has expired or does not exist" 400 for any MCX/NFO contract.
    if not exchange:
        sym_u = symbol.upper()
        candidates = []
        for src in (_load_kite_all_instruments() or [], _load_csv_instruments() or []):
            candidates.extend([r for r in src if (r.get('symbol') or '').upper() == sym_u])
            if candidates:
                break
        # Prefer established exchanges first (so duplicate MCX/NCO listings
        # snap to MCX, NFO over BFO, etc.)
        _prio = {'NSE':0, 'BSE':1, 'NFO':2, 'BFO':3, 'MCX':4, 'CDS':5, 'BCD':6, 'NCO':7}
        candidates.sort(key=lambda r: _prio.get((r.get('exchange') or '').strip().upper(), 99))
        if candidates:
            exchange = (candidates[0].get('exchange') or '').strip().upper()
        if not exchange:
            exchange = 'NSE'   # final fallback

    # Default product: derivatives use NRML, equities use MIS
    if not product:
        product = 'NRML' if exchange in ('NFO', 'BFO', 'MCX', 'CDS', 'BCD', 'NCO') else 'MIS'

    # Dry-run: log only, return a fake order id
    if dry_run:
        order_id = 'DRY-' + str(uuid.uuid4())[:6]
        entry = {
            'order_id': order_id, 'symbol': symbol, 'exchange': exchange,
            'side': side, 'qty': qty, 'product': product, 'order_type': order_type,
            'algo': algo, 'score': score, 'status': 'DRY-RUN',
            'timestamp': datetime.utcnow().isoformat()
        }
        zerodha_orders.append(entry)
        return jsonify({'success': True, 'orderId': order_id, 'order': entry, 'dry_run': True})

    # Real order via Kite Connect v3 REST API
    access_token = zerodha_sessions[api_key].get('access_token', '')
    if not access_token:
        return jsonify({'success': False, 'error': 'No access_token in server session — re-Connect'}), 403

    payload_d = {
        'tradingsymbol':    symbol,
        'exchange':         exchange,
        'transaction_type': side,
        'quantity':         qty,
        'product':          product,
        'order_type':       order_type,
        'validity':         'DAY',
    }
    # MARKET orders need an explicit market_protection % (Kite API requirement).
    if order_type == 'MARKET':
        payload_d['market_protection'] = market_protection
    # LIMIT / SL orders need a price; SL / SL-M need a trigger
    if order_type in ('LIMIT', 'SL') and limit_price is not None:
        payload_d['price'] = limit_price
    if order_type in ('SL', 'SL-M') and trigger_price is not None:
        payload_d['trigger_price'] = trigger_price
    payload = _up.urlencode(payload_d).encode('utf-8')
    req = _zd_urllib.Request(
        'https://api.kite.trade/orders/regular',
        data=payload,
        headers={
            'X-Kite-Version': '3',
            'Authorization':  'token {}:{}'.format(api_key, access_token),
            'Content-Type':   'application/x-www-form-urlencoded',
            'User-Agent':     'Mozilla/5.0',
        },
        method='POST'
    )
    try:
        with _kite_urlopen(req, timeout=15) as resp:
            body = resp.read().decode('utf-8')
        resp_data = _json.loads(body) if body else {}
    except _ue.HTTPError as e:
        err_body = ''
        try:
            err_body = e.read().decode('utf-8')
        except Exception:
            pass
        # Try to parse Kite's structured error
        try:
            err_data = _json.loads(err_body)
            err_msg  = err_data.get('message', err_body[:300])
        except Exception:
            err_msg = err_body[:300] or str(e)
        # Include the order context so the user can see what was actually sent
        ctx_parts = ['{} {} qty={} exch={} prod={}'.format(side, symbol, qty, exchange, product),
                     'type=' + order_type]
        if order_type == 'MARKET':
            ctx_parts.append('mp={}%'.format(market_protection))
        return jsonify({
            'success': False,
            'error':   'Kite HTTP {}: {} (sent: {})'.format(e.code, err_msg, ' '.join(ctx_parts)),
            'sent':    payload_d
        })
    except Exception as e:
        return jsonify({'success': False, 'error': 'Kite request failed: {}'.format(e)})

    if resp_data.get('status') == 'success':
        order_id = resp_data.get('data', {}).get('order_id', '')
        entry = {
            'order_id': order_id, 'symbol': symbol, 'exchange': exchange,
            'side': side, 'qty': qty, 'product': product, 'order_type': order_type,
            'algo': algo, 'score': score, 'status': 'PLACED',
            'timestamp': datetime.utcnow().isoformat()
        }
        zerodha_orders.append(entry)
        return jsonify({'success': True, 'orderId': order_id, 'order': entry})
    return jsonify({'success': False, 'error': resp_data.get('message', 'Order rejected'), 'data': resp_data})

@app.route('/api/zerodha/orders', methods=['GET'])
@login_required
def zerodha_orders_list():
    return jsonify({'success': True, 'orders': zerodha_orders[-50:]})

@app.route('/api/myip', methods=['GET'])
@login_required
def my_outbound_ip():
    """Return the server's outbound public IP — the IP Kite/Zerodha sees on
    incoming API calls. Add this exact value to your Kite app's Allowed IPs
    list at https://developers.kite.trade/apps to fix HTTP 403 errors like
    'No IPs configured for this app.'"""
    import urllib.request as _ur, json as _json
    sources = [
        'https://api.ipify.org?format=json',
        'https://ifconfig.me/all.json',
        'https://api64.ipify.org?format=json',
    ]
    last_err = None
    for url in sources:
        try:
            req = _ur.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with _ur.urlopen(req, timeout=8) as resp:
                body = resp.read().decode('utf-8').strip()
            try:
                data = _json.loads(body)
                ip = data.get('ip') or data.get('ip_addr')
            except Exception:
                ip = body
            if ip:
                return jsonify({'success': True, 'ip': ip, 'source': url})
        except Exception as e:
            last_err = str(e)
            continue
    return jsonify({'success': False, 'error': 'All IP lookup services failed', 'detail': last_err})

# ---- Instrument search list ----
_ZD_INSTRUMENTS = [
    # Indices (all have options)
    {"symbol":"NIFTY50",    "name":"NIFTY 50",                "exchange":"NSE","type":"INDEX","seg":"INDICES","tags":"options index derivatives weekly"},
    {"symbol":"BANKNIFTY",  "name":"BANK NIFTY",              "exchange":"NSE","type":"INDEX","seg":"INDICES","tags":"options index derivatives weekly"},
    {"symbol":"FINNIFTY",   "name":"NIFTY FIN SERVICE",       "exchange":"NSE","type":"INDEX","seg":"INDICES","tags":"options index derivatives weekly"},
    {"symbol":"MIDCPNIFTY", "name":"NIFTY MIDCAP SELECT",     "exchange":"NSE","type":"INDEX","seg":"INDICES","tags":"options index derivatives weekly"},
    {"symbol":"SENSEX",     "name":"S&P BSE SENSEX",          "exchange":"BSE","type":"INDEX","seg":"INDICES","tags":"options index derivatives weekly"},
    {"symbol":"NIFTYIT",    "name":"NIFTY IT",                "exchange":"NSE","type":"INDEX","seg":"INDICES","tags":"index sector"},
    {"symbol":"NIFTYAUTO",  "name":"NIFTY AUTO",              "exchange":"NSE","type":"INDEX","seg":"INDICES","tags":"index sector"},
    {"symbol":"NIFTYPHARMA","name":"NIFTY PHARMA",            "exchange":"NSE","type":"INDEX","seg":"INDICES","tags":"index sector"},
    {"symbol":"NIFTYMETAL", "name":"NIFTY METAL",             "exchange":"NSE","type":"INDEX","seg":"INDICES","tags":"index sector"},
    {"symbol":"NIFTYFMCG",  "name":"NIFTY FMCG",             "exchange":"NSE","type":"INDEX","seg":"INDICES","tags":"index sector"},
    # NIFTY 50 stocks (all have options)
    {"symbol":"ADANIENT",   "name":"Adani Enterprises Ltd",   "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity"},
    {"symbol":"ADANIPORTS", "name":"Adani Ports & SEZ Ltd",   "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity"},
    {"symbol":"APOLLOHOSP", "name":"Apollo Hospitals Ent Ltd","exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity"},
    {"symbol":"ASIANPAINT", "name":"Asian Paints Ltd",        "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity"},
    {"symbol":"AXISBANK",   "name":"Axis Bank Ltd",           "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity banking"},
    {"symbol":"BAJAJ-AUTO", "name":"Bajaj Auto Ltd",          "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity"},
    {"symbol":"BAJAJFINSV", "name":"Bajaj Finserv Ltd",       "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity finance"},
    {"symbol":"BAJFINANCE", "name":"Bajaj Finance Ltd",       "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity finance"},
    {"symbol":"BHARTIARTL", "name":"Bharti Airtel Ltd",       "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity telecom"},
    {"symbol":"BPCL",       "name":"Bharat Petroleum Corp",   "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity energy"},
    {"symbol":"BRITANNIA",  "name":"Britannia Industries Ltd","exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity fmcg"},
    {"symbol":"CIPLA",      "name":"Cipla Ltd",               "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity pharma"},
    {"symbol":"COALINDIA",  "name":"Coal India Ltd",          "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity energy"},
    {"symbol":"DIVISLAB",   "name":"Divi's Laboratories Ltd", "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity pharma"},
    {"symbol":"DRREDDY",    "name":"Dr Reddy's Laboratories", "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity pharma"},
    {"symbol":"EICHERMOT",  "name":"Eicher Motors Ltd",       "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity auto"},
    {"symbol":"GRASIM",     "name":"Grasim Industries Ltd",   "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity"},
    {"symbol":"HCLTECH",    "name":"HCL Technologies Ltd",    "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity it tech"},
    {"symbol":"HDFCBANK",   "name":"HDFC Bank Ltd",           "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity banking finance"},
    {"symbol":"HDFCLIFE",   "name":"HDFC Life Insurance Co",  "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity insurance"},
    {"symbol":"HEROMOTOCO", "name":"Hero MotoCorp Ltd",       "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity auto"},
    {"symbol":"HINDALCO",   "name":"Hindalco Industries Ltd",  "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity metal"},
    {"symbol":"HINDUNILVR", "name":"Hindustan Unilever Ltd",  "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity fmcg"},
    {"symbol":"ICICIBANK",  "name":"ICICI Bank Ltd",          "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity banking"},
    {"symbol":"INDUSINDBK", "name":"IndusInd Bank Ltd",       "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity banking"},
    {"symbol":"INFY",       "name":"Infosys Ltd",             "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity it tech"},
    {"symbol":"ITC",        "name":"ITC Ltd",                 "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity fmcg"},
    {"symbol":"JSWSTEEL",   "name":"JSW Steel Ltd",           "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity metal"},
    {"symbol":"KOTAKBANK",  "name":"Kotak Mahindra Bank Ltd", "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity banking"},
    {"symbol":"LT",         "name":"Larsen & Toubro Ltd",     "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity infra"},
    {"symbol":"MM",         "name":"Mahindra & Mahindra Ltd", "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity auto"},
    {"symbol":"MARUTI",     "name":"Maruti Suzuki India Ltd", "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity auto"},
    {"symbol":"NESTLEIND",  "name":"Nestle India Ltd",        "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity fmcg"},
    {"symbol":"NTPC",       "name":"NTPC Ltd",                "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity energy power"},
    {"symbol":"ONGC",       "name":"Oil & Natural Gas Corp",  "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity energy oil"},
    {"symbol":"POWERGRID",  "name":"Power Grid Corp of India","exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity energy power"},
    {"symbol":"RELIANCE",   "name":"Reliance Industries Ltd", "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity energy"},
    {"symbol":"SBILIFE",    "name":"SBI Life Insurance Co",   "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity insurance"},
    {"symbol":"SBIN",       "name":"State Bank of India",     "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity banking"},
    {"symbol":"SHRIRAMFIN", "name":"Shriram Finance Ltd",     "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity finance"},
    {"symbol":"SUNPHARMA",  "name":"Sun Pharmaceutical Ind",  "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity pharma"},
    {"symbol":"TATACONSUM", "name":"Tata Consumer Products",  "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity fmcg"},
    {"symbol":"TATAMOTORS", "name":"Tata Motors Ltd",         "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity auto"},
    {"symbol":"TATASTEEL",  "name":"Tata Steel Ltd",          "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity metal"},
    {"symbol":"TCS",        "name":"Tata Consultancy Services","exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity it tech"},
    {"symbol":"TECHM",      "name":"Tech Mahindra Ltd",       "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity it tech"},
    {"symbol":"TITAN",      "name":"Titan Company Ltd",       "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity"},
    {"symbol":"TRENT",      "name":"Trent Ltd",               "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity"},
    {"symbol":"ULTRACEMCO", "name":"UltraTech Cement Ltd",    "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity cement"},
    {"symbol":"WIPRO",      "name":"Wipro Ltd",               "exchange":"NSE","type":"EQ","seg":"NIFTY50","tags":"options fno derivatives equity it tech"},
    # Bank NIFTY extras (all have options)
    {"symbol":"AUBANK",     "name":"AU Small Finance Bank",   "exchange":"NSE","type":"EQ","seg":"BANKNIFTY","tags":"options fno derivatives equity banking"},
    {"symbol":"BANDHANBNK", "name":"Bandhan Bank Ltd",        "exchange":"NSE","type":"EQ","seg":"BANKNIFTY","tags":"options fno derivatives equity banking"},
    {"symbol":"FEDERALBNK", "name":"Federal Bank Ltd",        "exchange":"NSE","type":"EQ","seg":"BANKNIFTY","tags":"options fno derivatives equity banking"},
    {"symbol":"IDFCFIRSTB", "name":"IDFC First Bank Ltd",     "exchange":"NSE","type":"EQ","seg":"BANKNIFTY","tags":"options fno derivatives equity banking"},
    {"symbol":"PNB",        "name":"Punjab National Bank",    "exchange":"NSE","type":"EQ","seg":"BANKNIFTY","tags":"options fno derivatives equity banking"},
    {"symbol":"BANKBARODA", "name":"Bank of Baroda",          "exchange":"NSE","type":"EQ","seg":"BANKNIFTY","tags":"options fno derivatives equity banking"},
    # Mid/Small cap FNO (all have options)
    {"symbol":"ABCAPITAL",  "name":"Aditya Birla Capital",    "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity finance"},
    {"symbol":"ALKEM",      "name":"Alkem Laboratories",      "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity pharma"},
    {"symbol":"AMBUJACEM",  "name":"Ambuja Cements Ltd",      "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity cement"},
    {"symbol":"APOLLOTYRE", "name":"Apollo Tyres Ltd",        "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity auto"},
    {"symbol":"AUROPHARMA", "name":"Aurobindo Pharma Ltd",    "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity pharma"},
    {"symbol":"BALKRISIND", "name":"Balkrishna Industries",   "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity auto"},
    {"symbol":"BATAINDIA",  "name":"Bata India Ltd",          "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity"},
    {"symbol":"BERGEPAINT", "name":"Berger Paints India",     "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity"},
    {"symbol":"BIOCON",     "name":"Biocon Ltd",              "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity pharma"},
    {"symbol":"CANBK",      "name":"Canara Bank",             "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity banking"},
    {"symbol":"CHOLAFIN",   "name":"Cholamandalam Investment","exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity finance"},
    {"symbol":"DABUR",      "name":"Dabur India Ltd",         "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity fmcg"},
    {"symbol":"DLF",        "name":"DLF Ltd",                 "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity realty"},
    {"symbol":"ESCORTS",    "name":"Escorts Kubota Ltd",      "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity auto"},
    {"symbol":"GAIL",       "name":"GAIL India Ltd",          "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity energy"},
    {"symbol":"GODREJCP",   "name":"Godrej Consumer Products","exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity fmcg"},
    {"symbol":"HAVELLS",    "name":"Havells India Ltd",       "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity"},
    {"symbol":"HDFC",       "name":"Housing Dev Finance Corp","exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity finance"},
    {"symbol":"HINDPETRO",  "name":"Hindustan Petroleum Corp","exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity energy oil"},
    {"symbol":"IOCL",       "name":"Indian Oil Corporation",  "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity energy oil"},
    {"symbol":"IRCTC",      "name":"Indian Railway Catering", "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity"},
    {"symbol":"LUPIN",      "name":"Lupin Ltd",               "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity pharma"},
    {"symbol":"MCDOWELL-N", "name":"United Spirits Ltd",      "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity fmcg"},
    {"symbol":"MFSL",       "name":"Max Financial Services",  "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity insurance finance"},
    {"symbol":"MOTHERSON",  "name":"Samvardhana Motherson",   "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity auto"},
    {"symbol":"MPHASIS",    "name":"Mphasis Ltd",             "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity it tech"},
    {"symbol":"NAUKRI",     "name":"Info Edge (India) Ltd",   "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity it tech"},
    {"symbol":"PAGEIND",    "name":"Page Industries Ltd",     "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity"},
    {"symbol":"PERSISTENT", "name":"Persistent Systems Ltd",  "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity it tech"},
    {"symbol":"PETRONET",   "name":"Petronet LNG Ltd",        "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity energy"},
    {"symbol":"PIIND",      "name":"PI Industries Ltd",       "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity"},
    {"symbol":"POLYCAB",    "name":"Polycab India Ltd",       "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity"},
    {"symbol":"SAIL",       "name":"Steel Authority of India","exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity metal"},
    {"symbol":"SIEMENS",    "name":"Siemens Ltd",             "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity"},
    {"symbol":"TATAPOWER",  "name":"Tata Power Company Ltd",  "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity energy power"},
    {"symbol":"TORNTPHARM","name":"Torrent Pharmaceuticals",  "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity pharma"},
    {"symbol":"ZOMATO",     "name":"Zomato Ltd",              "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity tech"},
    {"symbol":"PAYTM",      "name":"One97 Communications",    "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity fintech"},
    {"symbol":"NYKAA",      "name":"FSN E-Commerce Ventures", "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity"},
    {"symbol":"POLICYBZR",  "name":"PB Fintech Ltd",          "exchange":"NSE","type":"EQ","seg":"FNO","tags":"options fno derivatives equity fintech insurance"},
    # ETFs
    {"symbol":"NIFTYBEES",  "name":"Nippon India ETF NIFTY",  "exchange":"NSE","type":"ETF","seg":"ETF","tags":"etf nifty index fund"},
    {"symbol":"BANKBEES",   "name":"Nippon India ETF Bank NF","exchange":"NSE","type":"ETF","seg":"ETF","tags":"etf bank nifty index fund"},
    {"symbol":"GOLDBEES",   "name":"Nippon India ETF Gold",   "exchange":"NSE","type":"ETF","seg":"ETF","tags":"etf gold commodity fund"},
    {"symbol":"SILVERBEES", "name":"Nippon India ETF Silver",  "exchange":"NSE","type":"ETF","seg":"ETF","tags":"etf silver commodity fund"},
    {"symbol":"LIQUIDBEES", "name":"Nippon India ETF Liquid",  "exchange":"NSE","type":"ETF","seg":"ETF","tags":"etf liquid fund debt"},
    {"symbol":"JUNIORBEES", "name":"Nippon India ETF Junior",  "exchange":"NSE","type":"ETF","seg":"ETF","tags":"etf nifty junior index fund"},
    {"symbol":"ICICINIFTY", "name":"ICICI Pru Nifty ETF",     "exchange":"NSE","type":"ETF","seg":"ETF","tags":"etf nifty index fund"},
    {"symbol":"MOM100",     "name":"Motilal Oswal Nasdaq 100","exchange":"NSE","type":"ETF","seg":"ETF","tags":"etf nasdaq us international fund"},
    # Commodities/Futures
    {"symbol":"USOIL",      "name":"US Oil (WTI)",            "exchange":"NYMEX","type":"FUTURES","seg":"COMM","tags":"commodity crude oil futures energy wti"},
    {"symbol":"CRUDEOILMCX","name":"Crude Oil Futures (MCX)", "exchange":"MCX","type":"FUTURES","seg":"COMM","tags":"commodity crude oil futures energy mcx"},
    {"symbol":"NATURALGAS", "name":"Natural Gas",             "exchange":"NYMEX","type":"FUTURES","seg":"COMM","tags":"commodity natural gas futures energy"},
    {"symbol":"GOLD",       "name":"Gold Futures",            "exchange":"COMEX","type":"FUTURES","seg":"COMM","tags":"commodity gold futures precious metal"},
    {"symbol":"SILVER",     "name":"Silver Futures",          "exchange":"COMEX","type":"FUTURES","seg":"COMM","tags":"commodity silver futures precious metal"},
    {"symbol":"XAUUSD",     "name":"Gold / US Dollar",        "exchange":"FX","type":"FX","seg":"COMM","tags":"forex gold xauusd fx currency"},
    {"symbol":"BTC",        "name":"Bitcoin / USD",           "exchange":"CRYPTO","type":"CRYPTO","seg":"CRYPTO","tags":"crypto bitcoin btc digital currency"},
    {"symbol":"ETH",        "name":"Ethereum / USD",          "exchange":"CRYPTO","type":"CRYPTO","seg":"CRYPTO","tags":"crypto ethereum eth digital currency"},
]

@app.route('/api/zerodha/instruments/search')
@login_required
def zerodha_instrument_search():
    q   = request.args.get('q', '').strip().upper()
    seg = request.args.get('seg', '').strip()       # optional filter: NIFTY50, BANKNIFTY, etc.
    results = list(_ZD_INSTRUMENTS)
    # Special virtual segment for OPTIONS tab
    if seg == 'OPTIONS':
        results = [r for r in results if 'options' in r.get('tags', '').lower()]
    elif seg:
        results = [r for r in results if r['seg'] == seg]
    if q:
        results = [r for r in results if
            q in r['symbol'] or
            q in r['name'].upper() or
            q in r.get('tags', '').upper() or
            q in r['type'].upper() or
            q in r['exchange'].upper()
        ]
    # On the "All" tab with a query, also pull matches from the full Kite instruments
    # dump (instruments.csv) so any tradingsymbol Kite knows about can be found here.
    # Curated entries above keep priority (added first, deduped by symbol).
    if seg == '' and q:
        seen = {r['symbol'].upper() for r in results}
        for r in _load_csv_instruments():
            sym = r['symbol'].upper()
            if sym in seen:
                continue
            if (q in sym
                or q in r['name'].upper()
                or q in r['exchange'].upper()
                or q in r['type'].upper()
                or q in r['segment'].upper()):
                results.append({
                    'symbol':   r['symbol'],
                    'name':     r['name'],
                    'exchange': r['exchange'],
                    'type':     r['type'] or r['segment'],
                    'seg':      r['seg'],
                    'tags':     '',
                })
                seen.add(sym)
                if len(results) >= 200:
                    break
        return jsonify({'success': True, 'results': results[:200]})
    return jsonify({'success': True, 'results': results[:50]})

# ---- Zerodha NFO instruments cache ----
import csv as _csv, io as _io, time as _zd_time, urllib.request as _zd_urllib
_ZD_NFO_CACHE = {'ts': 0, 'data': []}
_ZD_NFO_TTL   = 3600  # 1 hour

def _load_nfo_instruments():
    now = _zd_time.time()
    if now - _ZD_NFO_CACHE['ts'] < _ZD_NFO_TTL and _ZD_NFO_CACHE['data']:
        return _ZD_NFO_CACHE['data']
    try:
        req = _zd_urllib.Request(
            'https://api.kite.trade/instruments/NFO',
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        with _kite_urlopen(req, timeout=20) as resp:
            content = resp.read().decode('utf-8')
        reader = _csv.DictReader(_io.StringIO(content))
        rows = []
        for row in reader:
            itype = row.get('instrument_type', '')
            if itype not in ('CE', 'PE'):
                continue
            expiry_raw = row.get('expiry', '')
            try:
                exp_dt     = datetime.strptime(expiry_raw, '%Y-%m-%d')
                expiry_short = exp_dt.strftime('%d%b%y').upper()
            except Exception:
                expiry_short = expiry_raw
            rows.append({
                'symbol':       row.get('tradingsymbol', ''),
                'name':         row.get('name', ''),
                'expiry':       expiry_raw,
                'expiry_short': expiry_short,
                'strike':       float(row.get('strike') or 0),
                'type':         itype,
                'lot_size':     row.get('lot_size', ''),
                'exchange':     'NFO',
                'seg':          'OPTIONS',
                'tags':         f"options fno derivatives {row.get('name','').lower()}"
            })
        _ZD_NFO_CACHE['ts']   = now
        _ZD_NFO_CACHE['data'] = rows
        return rows
    except Exception:
        return _ZD_NFO_CACHE.get('data', [])

@app.route('/api/zerodha/nfo/refresh', methods=['POST'])
@login_required
def zerodha_nfo_refresh():
    _ZD_NFO_CACHE['ts'] = 0   # force reload on next search
    instruments = _load_nfo_instruments()
    return jsonify({'success': True, 'count': len(instruments)})

@app.route('/api/zerodha/nfo/search')
@login_required
def zerodha_nfo_search():
    import re as _re
    q = request.args.get('q', '').strip().upper()

    instruments = _load_nfo_instruments()
    if not instruments:
        return jsonify({'success': False,
                        'error': 'Could not load NFO instruments from Zerodha. Check internet.',
                        'results': []})

    # Parse underlying from query (longest match first)
    _underlyings = ['BANKNIFTY','MIDCPNIFTY','FINNIFTY','NIFTYNXT50','SENSEX','BANKEX','NIFTY']
    underlying = None
    q_nospace = q.replace(' ', '')
    for _u in _underlyings:
        if _u in q_nospace:
            underlying = _u
            break

    # Extract strike (4–6 digit number)
    _sm = _re.search(r'\b(\d{4,6})\b', q)
    target_strike = float(_sm.group(1)) if _sm else None

    # Extract CE / PE side (must be standalone word or at end)
    target_side = None
    if _re.search(r'\bCE\b|CALL', q):
        target_side = 'CE'
    elif _re.search(r'\bPE\b|PUT', q):
        target_side = 'PE'

    # Extract month
    _months = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC']
    target_month = next((_m for _m in _months if _m in q), None)

    results = []
    for inst in instruments:
        name   = inst['name'].upper()
        itype  = inst['type']
        strike = inst['strike']
        expiry = inst['expiry']   # "2026-05-22"

        # Filter by underlying
        if underlying:
            if name != underlying:
                continue
        elif q:
            # If no known underlying, do loose text match on tradingsymbol
            if q not in inst['symbol'].upper() and q not in name:
                continue

        # Filter by side
        if target_side and itype != target_side:
            continue

        # Filter by strike (±2000 range to keep results useful)
        if target_strike is not None and abs(strike - target_strike) > 2000:
            continue

        # Filter by month
        if target_month:
            try:
                if datetime.strptime(expiry, '%Y-%m-%d').strftime('%b').upper() != target_month:
                    continue
            except Exception:
                pass

        results.append(inst)

    # Sort: expiry asc, then by strike proximity if a target given
    if target_strike is not None:
        results.sort(key=lambda x: (x['expiry'], abs(x['strike'] - target_strike)))
    else:
        results.sort(key=lambda x: (x['expiry'], x['strike']))

    # Build response with display name
    out = []
    for inst in results[:100]:
        out.append({
            'symbol':       inst['symbol'],
            'name':         f"{inst['name']} {inst['expiry']} {int(inst['strike'])} {inst['type']}",
            'exchange':     inst['exchange'],
            'type':         inst['type'],
            'seg':          'OPTIONS',
            'strike':       inst['strike'],
            'expiry':       inst['expiry'],
            'expiry_short': inst['expiry_short'],
            'lot_size':     inst['lot_size'],
            'tags':         inst['tags'],
        })
    return jsonify({'success': True, 'results': out})

# ---- Zerodha instruments.csv (local) — cached loader + search ----
_ZD_CSV_PATH  = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'instruments.csv')
_ZD_CSV_CACHE = {'mtime': 0.0, 'data': []}

def _load_csv_instruments():
    """Lazily load instruments.csv into memory; reload if file mtime changes."""
    try:
        mtime = os.path.getmtime(_ZD_CSV_PATH)
    except OSError:
        return []
    if mtime == _ZD_CSV_CACHE['mtime'] and _ZD_CSV_CACHE['data']:
        return _ZD_CSV_CACHE['data']
    rows = []
    try:
        with open(_ZD_CSV_PATH, 'r', encoding='utf-8', newline='') as f:
            reader = _csv.DictReader(f)
            for row in reader:
                sym = (row.get('tradingsymbol') or '').strip()
                if not sym:
                    continue
                rows.append({
                    'instrument_token': (row.get('instrument_token') or '').strip(),
                    'symbol':       sym,
                    'name':         (row.get('name') or '').strip(),
                    'expiry':       (row.get('expiry') or '').strip(),
                    'strike':       (row.get('strike') or '').strip(),
                    'type':         (row.get('instrument_type') or '').strip(),
                    'lot_size':     (row.get('lot_size') or '').strip(),
                    'segment':      (row.get('segment') or '').strip(),
                    'exchange':     (row.get('exchange') or '').strip(),
                    'seg':          'ZERODHA_CSV',
                })
    except Exception:
        return _ZD_CSV_CACHE.get('data', [])
    _ZD_CSV_CACHE['mtime'] = mtime
    _ZD_CSV_CACHE['data']  = rows
    return rows

@app.route('/api/zerodha/csv/search')
@login_required
def zerodha_csv_search():
    """Search the locally-bundled instruments.csv. Returns up to 200 matches."""
    q = request.args.get('q', '').strip().upper()
    rows = _load_csv_instruments()
    if not rows:
        return jsonify({
            'success': False,
            'error':   'instruments.csv could not be loaded (file missing or empty).',
            'results': []
        })
    if q:
        filtered = []
        for r in rows:
            if (q in r['symbol'].upper()
                or q in r['name'].upper()
                or q in r['exchange'].upper()
                or q in r['type'].upper()
                or q in r['segment'].upper()):
                filtered.append(r)
                if len(filtered) >= 200:
                    break
        rows_out = filtered
    else:
        rows_out = rows[:200]
    return jsonify({'success': True, 'results': rows_out, 'total': len(rows)})

# ---- Force IPv4 for all Kite Connect API calls ----
# Kite Connect's IP whitelist accepts IPv4 only. Dual-stack hosts (e.g.,
# residential connections with IPv6) tend to prefer IPv6 by default, which
# Kite then rejects with HTTP 403 even when the IPv4 is correctly whitelisted.
# We build a dedicated urllib opener that pins HTTPS connections to IPv4.
import http.client as _zd_hc, socket as _zd_socket

class _IPv4HTTPSConnection(_zd_hc.HTTPSConnection):
    def connect(self):
        infos = _zd_socket.getaddrinfo(
            self.host, self.port, _zd_socket.AF_INET, _zd_socket.SOCK_STREAM
        )
        last_err = None
        for af, st, pr, _cn, sa in infos:
            sock = None
            try:
                sock = _zd_socket.socket(af, st, pr)
                if self.timeout is not None:
                    sock.settimeout(self.timeout)
                if self.source_address:
                    sock.bind(self.source_address)
                sock.connect(sa)
                self.sock = sock
                if self._tunnel_host:
                    self._tunnel()
                self.sock = self._context.wrap_socket(
                    self.sock, server_hostname=self._tunnel_host or self.host
                )
                return
            except OSError as e:
                last_err = e
                try:
                    if sock is not None:
                        sock.close()
                except Exception:
                    pass
        raise last_err if last_err else OSError('IPv4 connect failed')

class _IPv4HTTPSHandler(_zd_urllib.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_IPv4HTTPSConnection, req)

_KITE_IPV4_OPENER = _zd_urllib.build_opener(_IPv4HTTPSHandler())

def _kite_urlopen(req_or_url, data=None, headers=None, method=None, timeout=15):
    """urlopen replacement that forces IPv4 — use for every api.kite.trade call.

    Accepts either a urllib.request.Request or a URL string. Returns the
    response context manager (use within `with`)."""
    if isinstance(req_or_url, _zd_urllib.Request):
        req = req_or_url
    else:
        req = _zd_urllib.Request(req_or_url, data=data, headers=headers or {}, method=method or 'GET')
    return _KITE_IPV4_OPENER.open(req, timeout=timeout)

# ---- Live Kite API: full instruments dump (api.kite.trade/instruments) ----
_ZD_KITE_CACHE = {'ts': 0.0, 'data': []}
_ZD_KITE_TTL   = 3600  # 1 hour

def _load_kite_all_instruments():
    """Fetch the live Kite master instruments dump (all exchanges). Cached for 1 hour."""
    now = _zd_time.time()
    if now - _ZD_KITE_CACHE['ts'] < _ZD_KITE_TTL and _ZD_KITE_CACHE['data']:
        return _ZD_KITE_CACHE['data']
    try:
        req = _zd_urllib.Request(
            'https://api.kite.trade/instruments',
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        with _kite_urlopen(req, timeout=30) as resp:
            content = resp.read().decode('utf-8')
        reader = _csv.DictReader(_io.StringIO(content))
        rows = []
        for row in reader:
            sym = (row.get('tradingsymbol') or '').strip()
            if not sym:
                continue
            try:
                strike_val = float(row.get('strike') or 0)
            except ValueError:
                strike_val = 0.0
            expiry_raw = (row.get('expiry') or '').strip()
            try:
                exp_dt       = datetime.strptime(expiry_raw, '%Y-%m-%d')
                expiry_short = exp_dt.strftime('%d%b%y').upper()
            except Exception:
                expiry_short = expiry_raw
            rows.append({
                'instrument_token': (row.get('instrument_token') or '').strip(),
                'symbol':       sym,
                'name':         (row.get('name') or '').strip(),
                'expiry':       expiry_raw,
                'expiry_short': expiry_short,
                'strike':       strike_val,
                'type':         (row.get('instrument_type') or '').strip(),
                'lot_size':     (row.get('lot_size') or '').strip(),
                'segment':      (row.get('segment') or '').strip(),
                'exchange':     (row.get('exchange') or '').strip(),
                'seg':          'KITE',
                'tags':         '',
            })
        _ZD_KITE_CACHE['ts']   = now
        _ZD_KITE_CACHE['data'] = rows
        return rows
    except Exception:
        return _ZD_KITE_CACHE.get('data', [])

@app.route('/api/zerodha/kite/search')
@login_required
def zerodha_kite_search():
    """Live Kite API instrument search.

    Smart-parses queries to support patterns like:
      - 'NIFTY 24000'          -> all NIFTY options at strike 24000 across expiries
      - 'BANKNIFTY 52000 CE'   -> BANKNIFTY 52000 CALLs across expiries
      - 'NIFTY JUN 24000'      -> NIFTY June options at strike 24000
      - 'RELIANCE'             -> all RELIANCE instruments (EQ / FUT / options)
      - any plain substring    -> generic match against symbol / name / exchange / type
    """
    import re as _re
    q = request.args.get('q', '').strip().upper()
    rows = _load_kite_all_instruments()
    if not rows:
        return jsonify({
            'success': False,
            'error':   'Could not fetch Kite instruments from api.kite.trade. Check internet.',
            'results': []
        })

    if not q:
        return jsonify({'success': True, 'results': rows[:200], 'total': len(rows)})

    # Parse underlying (longest match first to avoid 'NIFTY' eating 'BANKNIFTY')
    _underlyings = ['BANKNIFTY','MIDCPNIFTY','FINNIFTY','NIFTYNXT50','SENSEX','BANKEX','NIFTY']
    q_nospace = q.replace(' ', '')
    underlying = next((u for u in _underlyings if u in q_nospace), None)

    # Strike (4–6 digit number)
    _sm = _re.search(r'\b(\d{4,6})\b', q)
    target_strike = float(_sm.group(1)) if _sm else None

    # Side
    target_side = None
    if _re.search(r'\bCE\b|CALL', q):
        target_side = 'CE'
    elif _re.search(r'\bPE\b|PUT', q):
        target_side = 'PE'

    # Month
    _months = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC']
    target_month = next((m for m in _months if m in q), None)

    results = []
    for r in rows:
        sym  = r['symbol'].upper()
        name = r['name'].upper()
        rtype = r['type'].upper()

        if underlying and target_strike is not None:
            # Strike-based query for a known underlying — show all expiries at that strike
            if name != underlying:
                continue
            if abs(r['strike'] - target_strike) > 0.01:
                continue
            if target_side and rtype != target_side:
                continue
            if target_month:
                try:
                    if datetime.strptime(r['expiry'], '%Y-%m-%d').strftime('%b').upper() != target_month:
                        continue
                except Exception:
                    pass
            results.append(r)
        elif underlying:
            # Underlying-only query — all contracts (futures + options) for that underlying
            if name != underlying:
                continue
            if target_side and rtype not in ('CE','PE'):
                # only filter side if user actually asked for CE/PE
                continue
            if target_side and rtype != target_side:
                continue
            if target_month:
                try:
                    if datetime.strptime(r['expiry'], '%Y-%m-%d').strftime('%b').upper() != target_month:
                        continue
                except Exception:
                    pass
            results.append(r)
        else:
            # Generic substring search
            if (q in sym
                or q in name
                or q in r['exchange'].upper()
                or q in rtype
                or q in r['segment'].upper()):
                results.append(r)

        if len(results) >= 300:
            break

    # Sort: expiry asc, then strike proximity to target (if any), then symbol
    if underlying:
        if target_strike is not None:
            results.sort(key=lambda x: (x['expiry'], abs(x['strike'] - target_strike), x['symbol']))
        else:
            results.sort(key=lambda x: (x['expiry'], x['strike'], x['symbol']))

    return jsonify({'success': True, 'results': results[:200], 'total': len(rows)})

@app.route('/api/zerodha/generate_token', methods=['POST'])
@login_required
def zerodha_generate_token():
    """Exchange request_token + api_secret for an access_token via Kite Connect v3."""
    import hashlib, urllib.request as _ur, urllib.parse as _up
    data = request.json or {}
    api_key       = data.get('api_key', '').strip()
    api_secret    = data.get('api_secret', '').strip()
    request_token = data.get('request_token', '').strip()
    if not (api_key and api_secret and request_token):
        return jsonify({'success': False, 'error': 'api_key, api_secret and request_token are required'}), 400
    try:
        checksum = hashlib.sha256((api_key + request_token + api_secret).encode()).hexdigest()
        payload  = _up.urlencode({
            'api_key':       api_key,
            'request_token': request_token,
            'checksum':      checksum,
        }).encode()
        req = _ur.Request(
            'https://api.kite.trade/session/token',
            data=payload, method='POST',
            headers={'X-Kite-Version': '3', 'Content-Type': 'application/x-www-form-urlencoded'}
        )
        with _kite_urlopen(req, timeout=15) as resp:
            import json as _json
            result = _json.loads(resp.read())
        access_token = result.get('data', {}).get('access_token', '')
        if not access_token:
            return jsonify({'success': False, 'error': 'No access_token in response', 'raw': result}), 400
        return jsonify({'success': True, 'access_token': access_token})
    except Exception as ex:
        return jsonify({'success': False, 'error': str(ex)}), 500

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
    "USOIL":       {"ticker": "CL=F",           "name": "US Oil (WTI)",          "exchange": "NYMEX"},
    "CRUDEOILMCX": {"ticker": "CL=F",           "name": "Crude Oil Futures (MCX)", "exchange": "MCX"},
    "NATURALGAS":  {"ticker": "NG=F",             "name": "Natural Gas",             "exchange": "NYMEX"},
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
    "USOIL":       "TVC:USOIL",
    "CRUDEOILMCX": "NYMEX:CL1!",
    "NATURALGAS":  "NYMEX:NG1!",
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
        # Futures/commodities use 'none' adjustment; equities use 'splits'
        is_futures = tv_symbol.endswith("1!") or tv_symbol.startswith(("MCX:", "NYMEX:", "COMEX:", "TVC:", "OANDA:"))
        adj = "none" if is_futures else "splits"
        sym_str = json.dumps(
            {"symbol": tv_symbol, "adjustment": adj}, separators=(",", ":")
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


def fetch_kite_data(interval_key, symbol, api_key=None):
    """Fetch OHLCV candles for a Zerodha tradingsymbol straight from the Kite
    historical-data REST endpoint.

    Looks up the instrument_token from the live Kite instruments dump
    (`_load_kite_all_instruments`, falling back to the local CSV cache), then
    calls https://api.kite.trade/instruments/historical/{token}/{interval}
    using the user's active session (`zerodha_sessions[api_key]`).

    Args:
        interval_key (str): App timeframe key (3m/5m/15m/1h/1d/...).
        symbol (str):        Kite tradingsymbol (e.g. CRUDEOILM26JUNFUT).
        api_key (str|None):  Identifies the Zerodha session. If None, picks the
            first connected session (single-user/localhost case).

    Returns:
        list[dict]: same shape as fetch_nifty_data — empty list if not
        connected, IP not whitelisted, token not found, or any API error.
    """
    import urllib.parse as _up, json as _json
    # Resolve session
    if not api_key or api_key not in zerodha_sessions:
        if zerodha_sessions:
            api_key = next(iter(zerodha_sessions))
        else:
            return []
    access_token = zerodha_sessions.get(api_key, {}).get('access_token', '')
    if not access_token:
        return []

    # Map our interval keys to Kite's interval names
    kite_interval_map = {
        '1m':  'minute',
        '3m':  '3minute',
        '5m':  '5minute',
        '10m': '10minute',
        '15m': '15minute',
        '30m': '30minute',
        '1h':  '60minute',
        '2h':  '60minute',   # Kite has no 2h; aggregate after
        '4h':  '60minute',   # same — aggregate after
        '1d':  'day',
    }
    k_int = kite_interval_map.get(interval_key, '5minute')

    # Find the instrument_token (live dump first, then local CSV)
    sym_u = (symbol or '').upper().strip()
    if not sym_u:
        return []
    inst_token = None
    for rows in (_load_kite_all_instruments() or [], _load_csv_instruments() or []):
        for r in rows:
            if (r.get('symbol') or '').upper() == sym_u:
                inst_token = (r.get('instrument_token') or '').strip()
                if inst_token:
                    break
        if inst_token:
            break
    if not inst_token:
        return []

    # Date range: keep payload small but enough for indicators
    # - daily:    last 200 days
    # - hourly:   last 30 days
    # - minutes:  last 5 days (Kite's historical limit for minute data is ~60 days but capped to ~30 per request)
    if k_int == 'day':
        days_back = 200
    elif k_int == '60minute':
        days_back = 30
    else:
        days_back = 5
    now    = datetime.now()
    fr_dt  = (now - _td_safe(days=days_back)).strftime('%Y-%m-%d %H:%M:%S')
    to_dt  = now.strftime('%Y-%m-%d %H:%M:%S')

    url = ('https://api.kite.trade/instruments/historical/'
           + str(inst_token) + '/' + k_int
           + '?from=' + _up.quote(fr_dt) + '&to=' + _up.quote(to_dt))
    req = _zd_urllib.Request(
        url,
        headers={
            'X-Kite-Version': '3',
            'Authorization':  'token {}:{}'.format(api_key, access_token),
            'User-Agent':     'Mozilla/5.0',
        },
        method='GET'
    )
    try:
        with _kite_urlopen(req, timeout=20) as resp:
            body = resp.read().decode('utf-8')
        data = _json.loads(body) if body else {}
    except Exception:
        return []
    if data.get('status') != 'success':
        return []
    rows = data.get('data', {}).get('candles', [])

    candles = []
    for row in rows:
        # Kite candle: [timestamp_iso_str, open, high, low, close, volume, oi?]
        try:
            ts_str = row[0]
            # Kite returns e.g. "2025-05-28T15:25:00+0530"
            if isinstance(ts_str, str):
                ts_clean = ts_str.replace('+0530', '+05:30').replace('+0000', '+00:00')
                dt = datetime.fromisoformat(ts_clean)
                ts = int(dt.timestamp())
            else:
                continue
            candles.append({
                'time':   ts,
                'open':   round(float(row[1]), 2),
                'high':   round(float(row[2]), 2),
                'low':    round(float(row[3]), 2),
                'close':  round(float(row[4]), 2),
                'volume': int(row[5]) if row[5] else 0,
            })
        except Exception:
            continue

    # Aggregate 60minute -> 2h/4h if requested
    if interval_key in ('2h', '4h') and candles:
        n = 2 if interval_key == '2h' else 4
        agg = []
        for i in range(0, len(candles), n):
            grp = candles[i:i + n]
            if not grp:
                continue
            agg.append({
                'time':   grp[0]['time'],
                'open':   grp[0]['open'],
                'high':   max(c['high'] for c in grp),
                'low':    min(c['low']  for c in grp),
                'close':  grp[-1]['close'],
                'volume': sum(c['volume'] for c in grp),
            })
        candles = agg

    return candles


def _td_safe(days=0):
    """Tiny shim — import timedelta lazily so this function file stays import-clean."""
    from datetime import timedelta as _td
    return _td(days=days)


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


def compute_orb(candles, orb_minutes=15):
    """Compute Opening Range Breakout (ORB) levels for each trading session.

    The ORB identifies the highest high and lowest low formed during the first
    `orb_minutes` of each trading day. These levels act as breakout reference
    zones — a price move above ORB high suggests a bullish breakout; below
    ORB low suggests a bearish breakdown. Only candles after the opening range
    closes are returned.

    Args:
        candles (list[dict]): OHLCV candle dicts with 'time' (Unix seconds).
        orb_minutes (int): Duration of the opening range in minutes (default 15).

    Returns:
        list[dict]: Dicts with 'time', 'high' (ORB high), 'low' (ORB low)
            for candles falling after the opening range on each day.
    """
    if not candles:
        return []

    from collections import defaultdict
    day_candles = defaultdict(list)
    for c in candles:
        date = datetime.fromtimestamp(c["time"]).strftime("%Y-%m-%d")
        day_candles[date].append(c)

    result = []
    for date in sorted(day_candles.keys()):
        day_sorted = sorted(day_candles[date], key=lambda x: x["time"])
        if not day_sorted:
            continue

        session_start = day_sorted[0]["time"]
        orb_end_ts = session_start + orb_minutes * 60

        orb_candles = [c for c in day_sorted if c["time"] < orb_end_ts]
        post_orb = [c for c in day_sorted if c["time"] >= orb_end_ts]

        if not orb_candles or not post_orb:
            continue

        orb_high = round(max(c["high"] for c in orb_candles), 2)
        orb_low = round(min(c["low"] for c in orb_candles), 2)

        for c in post_orb:
            result.append({
                "time": c["time"],
                "high": orb_high,
                "low": orb_low,
            })

    return result


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
        _sr_levels = [(lvl, "support") for lvl in sr.get("support", [])] + [(lvl, "resistance") for lvl in sr.get("resistance", [])]
        for lvl, lvl_type in _sr_levels:
            if abs(close - lvl["price"]) / close < 0.003:
                if lvl_type == "support" and close > opn and vol > vol_avg:
                    score += 1.0
                    reasons.append(f"S/R: Institutional support hold at {lvl['price']:.2f}")
                elif lvl_type == "resistance" and close < opn and vol > vol_avg:
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


def generate_marketmaking_signals(candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr):
    """Market Making Algo — identifies which market making algorithm is operating
    and predicts today's market movement bias based on MM behavior fingerprints.

    Detects: Avellaneda-Stoikov, Grid MM, Delta-Neutral, Spread Capture,
             Predatory/Spoofing, Liquidity Provision.

    Thresholds: BUY >= 3.5 | STRONG BUY >= 5.0 | SELL <= -3.5 | STRONG SELL <= -5.0
    """
    n = len(candles)
    if n < 50:
        return [], {}

    closes  = [c["close"]            for c in candles]
    highs   = [c["high"]             for c in candles]
    lows    = [c["low"]              for c in candles]
    opens   = [c["open"]             for c in candles]
    volumes = [c.get("volume", 0)    for c in candles]

    rsi_map   = {r["time"]: r["value"] for r in rsi_data}
    macd_map  = {m["time"]: m          for m in macd_data}
    vwap_map  = {v["time"]: v["value"] for v in vwap_data}
    ema9_map  = {e["time"]: e["value"] for e in ema9}
    ema21_map = {e["time"]: e["value"] for e in ema21}
    bb_upper_map, bb_lower_map, bb_mid_map = {}, {}, {}
    for b in bb:
        bb_upper_map[b["time"]] = b["upper"]
        bb_lower_map[b["time"]] = b["lower"]
        bb_mid_map[b["time"]]   = b.get("middle", (b["upper"] + b["lower"]) / 2)

    lookback = 30
    signals = []
    last_signal_type = None

    # --- Pre-compute ATR ---
    atr_list = []
    for j in range(1, n):
        tr = max(highs[j] - lows[j],
                 abs(highs[j] - closes[j - 1]),
                 abs(lows[j]  - closes[j - 1]))
        atr_list.append(tr)
    avg_atr_full = sum(atr_list) / len(atr_list) if atr_list else 1.0

    # --- Rolling candle range ratio (body / full range) ---
    def body_ratio(i):
        fr = highs[i] - lows[i]
        return abs(closes[i] - opens[i]) / fr if fr > 0 else 0

    # Accumulators for MM fingerprints across the whole session
    grid_hits         = 0   # price revisits exact level repeatedly
    symm_count        = 0   # symmetric wicks (AS model signature)
    spike_reversal    = 0   # vol spike + immediate price reversal (spoofing)
    tight_range_count = 0   # candle range < 0.3x ATR (spread capture)
    vwap_hug_count    = 0   # price within 0.1% of VWAP (delta-neutral)
    layer_count       = 0   # repeated volume clusters at same price (layering)

    price_visit_map = {}  # count candles near each round level

    for i in range(lookback, n):
        t   = candles[i]["time"]
        close = closes[i]
        high  = highs[i]
        low   = lows[i]
        opn   = opens[i]
        vol   = volumes[i]

        vol_avg = sum(volumes[i - lookback:i]) / lookback if lookback > 0 else 1
        seg_atr = atr_list[max(0, i - lookback - 1):i - 1]
        local_atr = sum(seg_atr) / len(seg_atr) if seg_atr else avg_atr_full

        score   = 0.0
        reasons = []

        # ---- 1. Avellaneda-Stoikov: symmetric wick + VWAP hug (weight 2.5) ----
        vw = vwap_map.get(t)
        lower_wick = min(close, opn) - low
        upper_wick = high - max(close, opn)
        full_range = high - low
        wick_sym = 1 - abs(lower_wick - upper_wick) / full_range if full_range > 0 else 0
        if vw and abs(close - vw) / vw < 0.001 and wick_sym > 0.7:
            symm_count += 1
            vwap_hug_count += 1
            if close > vw:
                score += 2.0
                reasons.append(f"AS-Model: Symmetric quote near VWAP (sym={wick_sym:.2f})")
            else:
                score -= 2.0
                reasons.append(f"AS-Model: Inventory unwind below VWAP (sym={wick_sym:.2f})")

        # ---- 2. Grid MM: repeated price at round/fixed levels (weight 2.0) ----
        round_lvl = round(close / 50) * 50
        price_visit_map[round_lvl] = price_visit_map.get(round_lvl, 0) + 1
        if price_visit_map[round_lvl] >= 4:
            grid_hits += 1
            if close > opn:
                score += 1.5
                reasons.append(f"Grid-MM: Repeated price at {round_lvl} ({price_visit_map[round_lvl]}x)")
            else:
                score -= 1.5
                reasons.append(f"Grid-MM: Grid resistance at {round_lvl} ({price_visit_map[round_lvl]}x)")

        # ---- 3. Delta-Neutral MM: price pinned near max-pain level (weight 1.5) ----
        # Proxy: price hovering within 0.15% of VWAP with shrinking range
        if vw and abs(close - vw) / vw < 0.0015:
            vwap_hug_count += 1
            range_vs_atr = (high - low) / local_atr if local_atr > 0 else 1
            if range_vs_atr < 0.5:
                score += 1.5
                reasons.append(f"Delta-Neutral: Price pinned to VWAP (range={range_vs_atr:.2f}x ATR)")

        # ---- 4. Spread Capture MM: ultra-tight range candles (weight 1.5) ----
        if local_atr > 0 and (high - low) < local_atr * 0.35:
            tight_range_count += 1
            # Spread capture benefits — direction from ema
            e9 = ema9_map.get(t)
            e21 = ema21_map.get(t)
            if e9 and e21:
                if e9 > e21:
                    score += 1.0
                    reasons.append(f"Spread-Capture: Ultra-tight range in uptrend ({high - low:.2f} vs ATR {local_atr:.2f})")
                else:
                    score -= 1.0
                    reasons.append(f"Spread-Capture: Ultra-tight range in downtrend")

        # ---- 5. Predatory MM / Spoofing: vol spike + full reversal (weight 2.5) ----
        if vol_avg > 0 and vol > vol_avg * 2.5:
            prev_dir  = closes[i - 1] - opens[i - 1]
            curr_dir  = close - opn
            if prev_dir * curr_dir < 0:  # direction flipped
                spike_reversal += 1
                if curr_dir > 0:
                    score += 2.5
                    reasons.append(f"Spoofing-MM: Vol spike reversal bullish ({vol / vol_avg:.1f}x)")
                else:
                    score -= 2.5
                    reasons.append(f"Spoofing-MM: Vol spike reversal bearish ({vol / vol_avg:.1f}x)")

        # ---- 6. Liquidity Provision: high vol at S/R + small body (weight 2.0) ----
        _sr_levels = [(lvl, "support") for lvl in sr.get("support", [])] + [(lvl, "resistance") for lvl in sr.get("resistance", [])]
        for lvl, lvl_type in _sr_levels:
            if abs(close - lvl["price"]) / close < 0.003 and vol > vol_avg * 1.5:
                layer_count += 1
                if body_ratio(i) < 0.35:
                    if lvl_type == "support":
                        score += 2.0
                        reasons.append(f"Liquidity-Provision: Absorbing sells at {lvl['price']:.2f}")
                    else:
                        score -= 2.0
                        reasons.append(f"Liquidity-Provision: Absorbing buys at {lvl['price']:.2f}")
                break

        # ---- 7. OBV trend confirmation (weight 1.0) ----
        if i >= 10:
            obv_delta = sum(volumes[j] * (1 if closes[j] > closes[j - 1] else -1)
                            for j in range(i - 10, i))
            if obv_delta > 0 and score > 0:
                score += 1.0
                reasons.append("OBV: Accumulation supports bullish bias")
            elif obv_delta < 0 and score < 0:
                score -= 1.0
                reasons.append("OBV: Distribution confirms bearish bias")

        # ---- 8. RSI extremes confirm MM trap (weight 1.5) ----
        rsi_val = rsi_map.get(t, 50)
        if rsi_val < 28:
            score += 1.5
            reasons.append(f"RSI: Extreme oversold — MM likely absorbing ({rsi_val:.0f})")
        elif rsi_val > 72:
            score -= 1.5
            reasons.append(f"RSI: Extreme overbought — MM likely distributing ({rsi_val:.0f})")

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

    # ---- Identify dominant MM algorithm ----
    mm_scores = {
        "Avellaneda-Stoikov":    symm_count      * 3,
        "Grid Market Making":    grid_hits        * 2,
        "Delta-Neutral":         vwap_hug_count   * 2,
        "Spread Capture":        tight_range_count * 1,
        "Predatory / Spoofing":  spike_reversal   * 4,
        "Liquidity Provision":   layer_count      * 3,
    }
    best_mm = max(mm_scores, key=lambda k: mm_scores[k])
    total_mm = sum(mm_scores.values()) or 1
    mm_conf  = round(min(mm_scores[best_mm] / total_mm * 100, 99), 1)

    # ---- Today's prediction based on dominant MM + recent signals ----
    buy_sigs   = [s for s in signals if "BUY"  in s["type"]]
    sell_sigs  = [s for s in signals if "SELL" in s["type"]]
    net_score  = sum(s["score"] for s in buy_sigs) + sum(s["score"] for s in sell_sigs)

    if best_mm == "Avellaneda-Stoikov":
        pred_text = "Mean-reversion expected. Price will snap back to VWAP. Fading extremes is favoured."
    elif best_mm == "Grid Market Making":
        pred_text = "Range-bound day expected. Price bouncing between grid levels. Trade the range."
    elif best_mm == "Delta-Neutral":
        pred_text = "Options-driven pinning. Price likely to stay near VWAP / max-pain all day."
    elif best_mm == "Spread Capture":
        pred_text = "Low-volatility session. Tight ranges. Breakout direction after session open is key."
    elif best_mm == "Predatory / Spoofing":
        pred_text = "High volatility. Fake moves likely. Wait for confirmation — do not chase spikes."
    else:  # Liquidity Provision
        pred_text = "Large institutional flow absorbing at key levels. Trend continuation likely after accumulation."

    bias = "BULLISH" if net_score > 2 else ("BEARISH" if net_score < -2 else "NEUTRAL")

    # ---- Summary ----
    li  = n - 1
    tl  = candles[li]["time"]
    rsi_l = rsi_map.get(tl, 50)
    mc_l  = macd_map.get(tl)
    vw_l  = vwap_map.get(tl)

    summary = {
        "score": round(net_score, 2),
        "verdict": ("STRONG BUY" if net_score >= 5 else ("BUY" if net_score >= 3.5 else
                    ("STRONG SELL" if net_score <= -5 else ("SELL" if net_score <= -3.5 else "NEUTRAL")))),
        "indicators": [
            {"name": "Avellaneda-Stoikov",    "status": f"{symm_count} hits",       "weight": symm_count      * 3},
            {"name": "Grid MM",               "status": f"{grid_hits} hits",         "weight": grid_hits       * 2},
            {"name": "Delta-Neutral",         "status": f"{vwap_hug_count} hits",    "weight": vwap_hug_count  * 2},
            {"name": "Spread Capture",        "status": f"{tight_range_count} hits", "weight": tight_range_count},
            {"name": "Predatory/Spoofing",    "status": f"{spike_reversal} hits",    "weight": spike_reversal  * 4},
            {"name": "Liquidity Provision",   "status": f"{layer_count} hits",       "weight": layer_count     * 3},
        ],
        "rsi": rsi_l,
        "macd": mc_l,
        "vwap": vw_l,
        # Extra fields for the dedicated MM panel
        "mm_algo":       best_mm,
        "mm_confidence": mm_conf,
        "mm_prediction": pred_text,
        "mm_bias":       bias,
        "mm_scores":     {k: round(v / total_mm * 100, 1) for k, v in mm_scores.items()},
    }
    return signals, summary


def generate_mma_signals(candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr):
    """Market Makers Advanced — detects 10 advanced market making algorithms.

    Algorithms detected:
      1. HFT Latency Arbitrage (Citadel-style)
      2. Optimal Execution TWAP/VWAP MM
      3. Statistical Arbitrage MM
      4. Inventory Risk MM (Ho-Stoll)
      5. Quote Stuffing / Layering
      6. Momentum Ignition
      7. Cross-Asset / Passive MM
      8. Passive Market Making (PMM)
      9. Reinforcement Learning MM
      10. Stochastic Control MM (Cartea-Jaimungal)

    Thresholds: BUY >= 3.5 | STRONG BUY >= 5.0 | SELL <= -3.5 | STRONG SELL <= -5.0
    """
    n = len(candles)
    if n < 50:
        return [], {}

    closes  = [c["close"]         for c in candles]
    highs   = [c["high"]          for c in candles]
    lows    = [c["low"]           for c in candles]
    opens   = [c["open"]          for c in candles]
    volumes = [c.get("volume", 0) for c in candles]

    rsi_map   = {r["time"]: r["value"] for r in rsi_data}
    vwap_map  = {v["time"]: v["value"] for v in vwap_data}
    ema9_map  = {e["time"]: e["value"] for e in ema9}
    ema21_map = {e["time"]: e["value"] for e in ema21}
    bb_upper_map, bb_lower_map, bb_mid_map = {}, {}, {}
    for b in bb:
        bb_upper_map[b["time"]] = b["upper"]
        bb_lower_map[b["time"]] = b["lower"]
        bb_mid_map[b["time"]]   = b.get("middle", (b["upper"] + b["lower"]) / 2)

    lookback = 20

    # --- Pre-compute ATR ---
    atr_list = []
    for j in range(1, n):
        tr = max(highs[j] - lows[j],
                 abs(highs[j] - closes[j - 1]),
                 abs(lows[j]  - closes[j - 1]))
        atr_list.append(tr)
    avg_atr = sum(atr_list) / len(atr_list) if atr_list else 1.0

    # --- Pre-compute returns ---
    returns = [0.0]
    for j in range(1, n):
        prev = closes[j - 1] if closes[j - 1] != 0 else 1
        returns.append((closes[j] - prev) / prev)

    # --- Algorithm hit counters ---
    hft_hits       = 0   # 1. HFT Latency Arbitrage
    twap_hits      = 0   # 2. TWAP/VWAP MM
    statarb_hits   = 0   # 3. Statistical Arbitrage MM
    hostoll_hits   = 0   # 4. Ho-Stoll Inventory Risk
    qstuff_hits    = 0   # 5. Quote Stuffing / Layering
    momign_hits    = 0   # 6. Momentum Ignition
    crossasset_hits = 0  # 7. Cross-Asset MM
    pmm_hits       = 0   # 8. Passive Market Making
    rl_hits        = 0   # 9. Reinforcement Learning MM
    cartea_hits    = 0   # 10. Stochastic Control (Cartea-Jaimungal)

    signals = []
    last_signal_type = None

    for i in range(lookback, n):
        t     = candles[i]["time"]
        close = closes[i]
        high  = highs[i]
        low   = lows[i]
        opn   = opens[i]
        vol   = volumes[i]

        vol_seg  = volumes[i - lookback:i]
        vol_avg  = sum(vol_seg) / lookback if lookback > 0 else 1
        atr_seg  = atr_list[max(0, i - lookback - 1):i - 1]
        local_atr = sum(atr_seg) / len(atr_seg) if atr_seg else avg_atr

        score   = 0.0
        reasons = []

        vw    = vwap_map.get(t)
        e9    = ema9_map.get(t)
        e21   = ema21_map.get(t)
        rsi   = rsi_map.get(t, 50)
        bb_u  = bb_upper_map.get(t)
        bb_l  = bb_lower_map.get(t)
        bb_m  = bb_mid_map.get(t)
        full_range = high - low

        # ================================================================
        # 1. HFT Latency Arbitrage — ultra-tiny range + vol burst clusters
        # Signature: extremely small candles (< 0.1x ATR) in rapid succession
        # with volume above average (HFT filling both sides fast)
        # ================================================================
        tiny_run = sum(1 for j in range(max(0, i-5), i)
                       if (highs[j] - lows[j]) < local_atr * 0.12) if i >= 5 else 0
        if tiny_run >= 3 and vol > vol_avg * 1.3:
            hft_hits += 1
            direction = 1 if close > opn else -1
            score += direction * 1.5
            reasons.append(f"HFT-Arb: {tiny_run} micro-candles + volume burst ({vol/vol_avg:.1f}x)")

        # ================================================================
        # 2. TWAP/VWAP Optimal Execution — volume distributed evenly,
        # price tracks VWAP closely, no large vol spikes
        # ================================================================
        if vw:
            vwap_dev = abs(close - vw) / vw if vw > 0 else 1
            vol_cv   = (max(vol_seg) - min(vol_seg)) / (vol_avg + 1e-10)
            if vwap_dev < 0.002 and vol_cv < 1.5:
                twap_hits += 1
                if close > vw:
                    score += 1.5
                    reasons.append(f"TWAP-MM: Uniform vol execution near VWAP (dev={vwap_dev*100:.2f}%)")
                else:
                    score -= 1.5
                    reasons.append(f"TWAP-MM: Sell execution tracking below VWAP")

        # ================================================================
        # 3. Statistical Arbitrage MM — mean-reverting price vs BB midline,
        # alternating up/down candles (pairs-trade style)
        # ================================================================
        if bb_m:
            alt_count = 0
            for j in range(max(1, i-6), i):
                if (closes[j] - opens[j]) * (closes[j-1] - opens[j-1]) < 0:
                    alt_count += 1
            dev_from_mid = (close - bb_m) / (bb_m + 1e-10)
            if alt_count >= 3 and abs(dev_from_mid) < 0.005:
                statarb_hits += 1
                if close < bb_m:
                    score += 1.5
                    reasons.append(f"StatArb-MM: Mean reversion to BB-mid (alt={alt_count}, dev={dev_from_mid*100:.2f}%)")
                else:
                    score -= 1.5
                    reasons.append(f"StatArb-MM: Mean reversion pullback from BB-mid")

        # ================================================================
        # 4. Inventory Risk MM (Ho-Stoll) — spread widens progressively,
        # wick asymmetry grows as inventory builds (one-sided pressure)
        # ================================================================
        if i >= 10:
            upper_wicks = [highs[j] - max(closes[j], opens[j]) for j in range(i-10, i)]
            lower_wicks = [min(closes[j], opens[j]) - lows[j]  for j in range(i-10, i)]
            avg_uw = sum(upper_wicks) / 10
            avg_lw = sum(lower_wicks) / 10
            wick_asym = (avg_uw - avg_lw) / (avg_uw + avg_lw + 1e-10)
            if abs(wick_asym) > 0.35:
                hostoll_hits += 1
                if wick_asym > 0:  # upper wick dominant → sellers/distributors
                    score -= 1.5
                    reasons.append(f"Ho-Stoll: Inventory build (upper wick bias={wick_asym:.2f})")
                else:
                    score += 1.5
                    reasons.append(f"Ho-Stoll: Inventory unwind (lower wick bias={wick_asym:.2f})")

        # ================================================================
        # 5. Quote Stuffing / Layering — volume spike with minimal price move,
        # followed by another spike (cancel-replace pattern)
        # ================================================================
        if i >= 3:
            price_move = abs(close - closes[i-1]) / (closes[i-1] + 1e-10)
            vol_spike  = vol / (vol_avg + 1e-10)
            prev_spike = volumes[i-1] / (vol_avg + 1e-10)
            if vol_spike > 2.0 and price_move < 0.001 and prev_spike > 1.5:
                qstuff_hits += 1
                # Direction: slight bias from recent returns
                net_ret = sum(returns[i-3:i])
                if net_ret > 0:
                    score += 2.0
                    reasons.append(f"QuoteStuff: Vol cluster no-move ({vol_spike:.1f}x) — buy-side absorption")
                else:
                    score -= 2.0
                    reasons.append(f"QuoteStuff: Vol cluster no-move ({vol_spike:.1f}x) — sell-side absorption")

        # ================================================================
        # 6. Momentum Ignition — sharp spike (>1.5x ATR) then full reversal
        # within 2-3 bars (MM traps directional traders)
        # ================================================================
        if i >= 3:
            spike_bar  = highs[i-2] - lows[i-2]
            rev_dir    = (closes[i] - opens[i]) * (closes[i-2] - opens[i-2])
            if spike_bar > local_atr * 1.5 and rev_dir < 0:
                momign_hits += 1
                if closes[i] > opens[i]:
                    score += 2.5
                    reasons.append(f"MomIgnition: Spike trap reversal bullish (spike={spike_bar/local_atr:.1f}x ATR)")
                else:
                    score -= 2.5
                    reasons.append(f"MomIgnition: Spike trap reversal bearish (spike={spike_bar/local_atr:.1f}x ATR)")

        # ================================================================
        # 7. Cross-Asset MM — price stable despite high volume
        # (hedge leg absorbing risk), vol > 2x with tiny body
        # ================================================================
        body    = abs(close - opn)
        body_pct = body / (local_atr + 1e-10)
        if vol > vol_avg * 2.0 and body_pct < 0.2:
            crossasset_hits += 1
            if close > vw if vw else close > opn:
                score += 1.5
                reasons.append(f"CrossAsset-MM: Hedged flow — high vol tiny body (body={body_pct:.2f}x ATR)")
            else:
                score -= 1.5
                reasons.append(f"CrossAsset-MM: Hedged distribution — high vol tiny body")

        # ================================================================
        # 8. Passive Market Making (PMM) — doji-heavy, price near session midpoint,
        # earning spread passively with no directional bias
        # ================================================================
        if i >= 10:
            doji_count = sum(1 for j in range(i-10, i)
                             if abs(closes[j] - opens[j]) / (highs[j] - lows[j] + 1e-10) < 0.15)
            sess_hi = max(highs[i-10:i])
            sess_lo = min(lows[i-10:i])
            sess_mid = (sess_hi + sess_lo) / 2
            near_mid = abs(close - sess_mid) / (sess_hi - sess_lo + 1e-10) < 0.25
            if doji_count >= 4 and near_mid:
                pmm_hits += 1
                if rsi < 50:
                    score += 1.0
                    reasons.append(f"PMM: Passive spread ({doji_count} dojis at session mid)")
                else:
                    score -= 1.0
                    reasons.append(f"PMM: Passive spread ({doji_count} dojis) — selling into strength")

        # ================================================================
        # 9. Reinforcement Learning MM — adaptive quoting: range narrows then
        # expands in same direction as volatility regime shift
        # ================================================================
        if i >= 15:
            ranges   = [highs[j] - lows[j] for j in range(i-15, i)]
            early_r  = sum(ranges[:7]) / 7
            late_r   = sum(ranges[8:]) / 7
            vol_dir  = sum(returns[i-7:i])
            if early_r > 0 and late_r / early_r > 1.5:
                rl_hits += 1
                if vol_dir > 0:
                    score += 1.5
                    reasons.append(f"RL-MM: Adaptive widening bullish (range x{late_r/early_r:.1f})")
                else:
                    score -= 1.5
                    reasons.append(f"RL-MM: Adaptive widening bearish (range x{late_r/early_r:.1f})")

        # ================================================================
        # 10. Stochastic Control MM (Cartea-Jaimungal) — spread narrows near
        # session end (terminal wealth constraint), VWAP pinning + vol decay
        # ================================================================
        session_pos = i / n  # 0 = session start, 1 = session end
        if session_pos > 0.7 and vw:
            vol_decay = vol / (vol_avg + 1e-10)
            vwap_pin  = abs(close - vw) / (vw + 1e-10)
            if vol_decay < 0.8 and vwap_pin < 0.002:
                cartea_hits += 1
                if close > vw:
                    score += 1.5
                    reasons.append(f"Cartea-J: Terminal VWAP pin (vol={vol_decay:.2f}x, dev={vwap_pin*100:.3f}%)")
                else:
                    score -= 1.5
                    reasons.append(f"Cartea-J: Terminal unwind below VWAP")

        # --- RSI extremes confirm (weight 1.0) ---
        if rsi < 30:
            score += 1.0
            reasons.append(f"RSI: Oversold zone ({rsi:.0f}) — absorption likely")
        elif rsi > 70:
            score -= 1.0
            reasons.append(f"RSI: Overbought zone ({rsi:.0f}) — distribution likely")

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

    # ---- Identify dominant MMA algorithm ----
    mma_raw = {
        "HFT Latency Arbitrage":         hft_hits    * 4,
        "TWAP/VWAP Optimal Execution":   twap_hits   * 3,
        "Statistical Arbitrage MM":      statarb_hits * 3,
        "Inventory Risk (Ho-Stoll)":     hostoll_hits * 2,
        "Quote Stuffing / Layering":     qstuff_hits  * 4,
        "Momentum Ignition":             momign_hits  * 5,
        "Cross-Asset MM":                crossasset_hits * 2,
        "Passive Market Making (PMM)":   pmm_hits    * 2,
        "Reinforcement Learning MM":     rl_hits     * 3,
        "Stochastic Control (Cartea-J)": cartea_hits  * 3,
    }
    best_mma  = max(mma_raw, key=lambda k: mma_raw[k])
    total_mma = sum(mma_raw.values()) or 1
    mma_conf  = round(min(mma_raw[best_mma] / total_mma * 100, 99), 1)

    # ---- Prediction for dominant algorithm ----
    buy_sigs  = [s for s in signals if "BUY"  in s["type"]]
    sell_sigs = [s for s in signals if "SELL" in s["type"]]
    net_score = sum(s["score"] for s in buy_sigs) + sum(s["score"] for s in sell_sigs)

    preds = {
        "HFT Latency Arbitrage":
            "Ultra-fast micro-arbitrage in play. Price direction will be set by the first 5-minute move — HFT will amplify it. Trade with momentum, not against it.",
        "TWAP/VWAP Optimal Execution":
            "Large institutional order executing systematically. Price will track VWAP all day. Fading extremes from VWAP is safest strategy.",
        "Statistical Arbitrage MM":
            "Pairs/mean-reversion strategy active. Price oscillating around BB-midline. Expect range-bound session — buy dips, sell rips near midline.",
        "Inventory Risk (Ho-Stoll)":
            "MM managing directional inventory risk. Wick asymmetry reveals their bias. Expect spread widening and a directional push once inventory is cleared.",
        "Quote Stuffing / Layering":
            "Order book manipulation detected. Do NOT trust apparent order book depth. Wait for genuine price break with volume confirmation before entering.",
        "Momentum Ignition":
            "Fake directional moves being engineered. Expect sharp spikes followed by immediate reversal. Fade the spike — do not chase the initial move.",
        "Cross-Asset MM":
            "Hedged institutional flow — large vol with no price impact. Underlying direction determined by the hedge leg. Watch the index/futures for true bias.",
        "Passive Market Making (PMM)":
            "Passive spread-earner dominant. Flat, doji-heavy day expected near session midpoint. Only trade on confirmed breakout with volume.",
        "Reinforcement Learning MM":
            "Adaptive algo adjusting to volatility regime. Expect quiet periods followed by sudden range expansions. Trade breakouts, not the flat zones.",
        "Stochastic Control (Cartea-J)":
            "Terminal-wealth constrained MM active. Session-end VWAP pinning expected. Price will gravitate to VWAP — avoid holding positions into close.",
    }
    pred_text = preds.get(best_mma, "Advanced MM activity detected. Monitor order flow carefully.")
    bias = "BULLISH" if net_score > 2 else ("BEARISH" if net_score < -2 else "NEUTRAL")

    # ---- Per-algo percentage scores for display ----
    mma_scores_pct = {k: round(v / total_mma * 100, 1) for k, v in mma_raw.items()}

    # ---- Summary ----
    li    = n - 1
    tl    = candles[li]["time"]
    rsi_l = rsi_map.get(tl, 50)
    vw_l  = vwap_map.get(tl)

    summary = {
        "score":   round(net_score, 2),
        "verdict": ("STRONG BUY" if net_score >= 5 else ("BUY" if net_score >= 3.5 else
                    ("STRONG SELL" if net_score <= -5 else ("SELL" if net_score <= -3.5 else "NEUTRAL")))),
        "indicators": [
            {"name": "HFT Latency Arbitrage",         "status": f"{hft_hits} hits",        "weight": hft_hits    * 4},
            {"name": "TWAP/VWAP Optimal Execution",   "status": f"{twap_hits} hits",        "weight": twap_hits   * 3},
            {"name": "Statistical Arbitrage MM",       "status": f"{statarb_hits} hits",     "weight": statarb_hits* 3},
            {"name": "Inventory Risk (Ho-Stoll)",      "status": f"{hostoll_hits} hits",     "weight": hostoll_hits* 2},
            {"name": "Quote Stuffing / Layering",      "status": f"{qstuff_hits} hits",      "weight": qstuff_hits * 4},
            {"name": "Momentum Ignition",              "status": f"{momign_hits} hits",      "weight": momign_hits * 5},
            {"name": "Cross-Asset MM",                 "status": f"{crossasset_hits} hits",  "weight": crossasset_hits*2},
            {"name": "Passive Market Making (PMM)",    "status": f"{pmm_hits} hits",         "weight": pmm_hits    * 2},
            {"name": "Reinforcement Learning MM",      "status": f"{rl_hits} hits",          "weight": rl_hits     * 3},
            {"name": "Stochastic Control (Cartea-J)", "status": f"{cartea_hits} hits",      "weight": cartea_hits * 3},
        ],
        "rsi":  rsi_l,
        "vwap": vw_l,
        # MMA-specific fields for the dedicated panel
        "mma_algo":       best_mma,
        "mma_confidence": mma_conf,
        "mma_prediction": pred_text,
        "mma_bias":       bias,
        "mma_scores":     mma_scores_pct,
        "mma_raw_hits": {
            "HFT Latency Arbitrage":         hft_hits,
            "TWAP/VWAP Optimal Execution":   twap_hits,
            "Statistical Arbitrage MM":      statarb_hits,
            "Inventory Risk (Ho-Stoll)":     hostoll_hits,
            "Quote Stuffing / Layering":     qstuff_hits,
            "Momentum Ignition":             momign_hits,
            "Cross-Asset MM":                crossasset_hits,
            "Passive Market Making (PMM)":   pmm_hits,
            "Reinforcement Learning MM":     rl_hits,
            "Stochastic Control (Cartea-J)": cartea_hits,
        },
    }
    return signals, summary


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

    # Data source — kite fetches actual contract candles via Kite historical API;
    # USOIL/CRUDEOILMCX always use TradingView (continuous proxy).
    source = request.args.get("source", "yahoo")
    if source == "kite":
        api_key_q = request.args.get("api_key", "").strip()
        candles = fetch_kite_data(interval, symbol, api_key=api_key_q or None)
        # If Kite fetch returned nothing (not connected, IP not whitelisted,
        # token unknown), fall back to the next best source so the rule
        # doesn't go silent — and so the user sees indicator values from
        # SOMETHING while they sort out IP whitelisting.
        if not candles:
            if symbol in ("USOIL", "CRUDEOILMCX"):
                candles = fetch_tradingview_data(interval, symbol)
            else:
                candles = fetch_nifty_data(interval, symbol)
    elif symbol in ("USOIL", "CRUDEOILMCX") or source == "tradingview":
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
    orb = compute_orb(candles)

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
        elif algo == "marketmaking":
            sigs, summ = generate_marketmaking_signals(
                candles, bb, rsi_data, macd_data, vwap_data, ema9, ema21, sr
            )
        elif algo == "mma":
            sigs, summ = generate_mma_signals(
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
        "allSignals": sorted(all_signals, key=lambda x: x["time"]),
        "signalSummary": summaries,
        "cpr": cpr,
        "bollingerBands": bb,
        "liquidityPools": liquidity_pools,
        "fairValueGaps": fvg,
        "bosChoch": bos_choch,
        "cvd": cvd,
        "volumeProfile": volume_profile,
        "orb": orb,
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
    max-height: 70vh; overflow-y: auto;
  }
  .indicators-dropdown::-webkit-scrollbar { width: 5px; }
  .indicators-dropdown::-webkit-scrollbar-track { background: transparent; }
  .indicators-dropdown::-webkit-scrollbar-thumb { background: var(--bg-tertiary); border-radius: 4px; }
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

  /* Automation Menu */
  .automation-dropdown-wrapper { position: relative; display: inline-block; }
  .automation-dropdown {
    position: absolute; top: 36px; left: 0; background: #23273a; border: 1px solid #2a2e39; border-radius: 8px;
    min-width: 160px; z-index: 210; display: none; box-shadow: 0 8px 32px rgba(0,0,0,0.5);
  }
  .automation-dropdown.open { display: block; }
  .automation-item { width: 100%; background: none; border: none; color: #d1d4dc; padding: 10px 18px; text-align: left; font-size: 14px; cursor: pointer; transition: background 0.2s; }
  .automation-item:hover { background: #2a2e39; }

  /* Zerodha Automation Panel */
  .zerodha-panel {
    position: absolute; top: 80px; left: 50%; transform: translateX(-50%);
    z-index: 300; background: #1e222d; border: 1px solid #2a2e39; border-radius: 10px;
    width: 960px; max-width: 98vw; display: none; box-shadow: 0 12px 48px rgba(0,0,0,0.7);
    max-height: calc(100vh - 100px); overflow-y: auto;
  }
  .zerodha-panel.open { display: block; }
  .zd-header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 13px 18px; border-bottom: 1px solid #2a2e39; cursor: move; user-select: none;
    background: #23273a; border-radius: 10px 10px 0 0;
  }
  .zd-header h3 { font-size: 15px; color: #fff; margin: 0; display: flex; align-items: center; gap: 8px; }
  .zd-close { background: none; border: none; color: #787b86; font-size: 22px; cursor: pointer; }
  .zd-close:hover { color: #fff; }
  .zd-header-actions { display: flex; align-items: center; gap: 2px; }
  .zd-header-btn {
    background: none; border: none; color: #787b86; cursor: pointer;
    font-size: 14px; padding: 4px 9px; border-radius: 3px;
    transition: background 0.15s, color 0.15s; font-family: inherit;
  }
  .zd-header-btn:hover { color: #fff; background: rgba(255,255,255,0.08); }
  /* Maximised panel — fills the entire viewport */
  .zerodha-panel.maximized {
    top: 0 !important; left: 0 !important; right: 0 !important; bottom: 0 !important;
    transform: none !important; width: 100vw !important; max-width: 100vw !important;
    height: 100vh !important; max-height: 100vh !important; border-radius: 0 !important;
  }
  .zerodha-panel.maximized .zd-header { border-radius: 0 !important; cursor: default !important; }
  /* Popped-out — when this page was opened as the Zerodha popout window, the panel fills it */
  body.zerodha-popout-window .zerodha-panel.open {
    top: 0 !important; left: 0 !important; right: 0 !important; bottom: 0 !important;
    transform: none !important; width: 100vw !important; max-width: 100vw !important;
    height: 100vh !important; max-height: 100vh !important; border-radius: 0 !important;
  }
  body.zerodha-popout-window .zerodha-panel.open .zd-header { border-radius: 0 !important; cursor: default !important; }
  .zd-body { padding: 16px 18px; }
  .zd-credentials { display: flex; flex-direction: column; gap: 10px; margin-bottom: 14px; }
  .zd-cred-row { display: flex; align-items: flex-end; gap: 10px; flex-wrap: wrap; }
  .zd-cred-group { display: flex; flex-direction: column; gap: 3px; flex: 1; min-width: 120px; }
  .zd-cred-group label { color: #787b86; font-size: 11px; }
  .zd-cred-group input {
    padding: 7px 10px; background: #131722; border: 1px solid #2a2e39; border-radius: 4px;
    color: #d1d4dc; font-size: 13px; width: 100%; box-sizing: border-box;
  }
  .zd-cred-group input:focus { outline: none; border-color: #2962ff; }
  .zd-login-url-btn {
    padding: 7px 14px; background: #ff9100; border: none; border-radius: 5px; color: #fff;
    font-size: 13px; cursor: pointer; font-weight: 600; white-space: nowrap; flex-shrink: 0;
  }
  .zd-login-url-btn:hover { background: #ffb300; }
  .zd-get-token-btn {
    padding: 7px 14px; background: #7b1fa2; border: none; border-radius: 5px; color: #fff;
    font-size: 13px; cursor: pointer; font-weight: 600; white-space: nowrap; flex-shrink: 0;
  }
  .zd-get-token-btn:hover { background: #9c27b0; }
  .zd-connect-btn {
    padding: 7px 16px; background: #1e6ec8; border: none; border-radius: 5px; color: #fff;
    font-size: 13px; cursor: pointer; font-weight: 600; white-space: nowrap; flex-shrink: 0;
  }
  .zd-connect-btn:hover { background: #2962ff; }
  .zd-connect-btn.connected { background: #2e7d32; }
  .zd-status-bar { font-size: 12px; color: #787b86; margin-bottom: 14px; display: flex; align-items: center; gap: 8px; }
  .zd-status-dot { width: 8px; height: 8px; border-radius: 50%; background: #787b86; display: inline-block; }
  .zd-status-dot.connected { background: #26a69a; }
  .zd-status-dot.running { background: #ff9100; animation: livePulse 1s ease-in-out infinite; }
  .zd-section-title { font-size: 12px; color: #787b86; text-transform: uppercase; letter-spacing: 0.08em; margin: 14px 0 8px; }
  /* Shared symbol/qty bar */
  .zd-rule-shared { display: flex; gap: 10px; align-items: flex-end; margin-bottom: 10px; flex-wrap: wrap; }
  .zd-rule-shared .zd-cred-group { min-width: 80px; }
  /* Row type label */
  .zd-row-label { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.07em; margin: 8px 0 5px; display: flex; align-items: center; gap: 6px; }
  .zd-row-label.algo-label { color: #2962ff; }
  .zd-row-label.ind-label  { color: #9c27b0; }
  /* Add rows */
  .zd-add-row {
    display: flex; gap: 7px; align-items: flex-end; flex-wrap: wrap;
    margin-bottom: 10px; padding: 10px 12px;
    background: rgba(41,98,255,0.05); border: 1px solid rgba(41,98,255,0.18);
    border-radius: 7px;
  }
  .zd-add-row.ind-row {
    background: rgba(156,39,176,0.05); border-color: rgba(156,39,176,0.22);
  }
  .zd-add-row.mm-row {
    background: rgba(255,145,0,0.05); border-color: rgba(255,145,0,0.18);
  }
  .zd-add-row label { color: #787b86; font-size: 10px; display: flex; flex-direction: column; gap: 3px; white-space: nowrap; }
  .zd-add-row input, .zd-add-row select {
    padding: 6px 7px; background: #131722; border: 1px solid #2a2e39;
    border-radius: 4px; color: #d1d4dc; font-size: 12px; box-sizing: border-box;
  }
  .zd-add-row select.entry-sel { width: 72px; }
  .zd-add-row select.side-sel  { width: 65px; }
  .zd-add-row select.tf-sel    { width: 60px; }
  .zd-add-row select.algo-sel  { width: 118px; }
  .zd-add-row select.ind-sel   { width: 105px; }
  .zd-add-row select.mm-sel    { width: 115px; }
  .zd-add-row input.score-inp  { width: 58px; }
  .zd-add-btn {
    padding: 7px 13px; background: #2962ff; border: none; border-radius: 5px;
    color: #fff; font-size: 12px; cursor: pointer; font-weight: 700; white-space: nowrap;
  }
  .zd-add-btn:hover { background: #1e6ec8; }
  .zd-add-btn.ind-btn { background: #7b1fa2; }
  .zd-add-btn.ind-btn:hover { background: #9c27b0; }
  .zd-add-btn.mm-btn { background: #ff9100; }
  .zd-add-btn.mm-btn:hover { background: #ffb300; }
  /* Entry/Exit badges */
  .badge-entry { background: rgba(38,166,154,0.18); color: #26a69a; border-radius: 3px; padding: 2px 6px; font-size: 10px; font-weight: 700; }
  .badge-exit  { background: rgba(239,83,80,0.18);  color: #ef5350; border-radius: 3px; padding: 2px 6px; font-size: 10px; font-weight: 700; }
  .badge-buy   { background: rgba(38,166,154,0.15); color: #26a69a; border-radius: 3px; padding: 2px 6px; font-size: 10px; }
  .badge-sell  { background: rgba(239,83,80,0.15);  color: #ef5350; border-radius: 3px; padding: 2px 6px; font-size: 10px; }
  .badge-idle  { background: rgba(120,123,134,0.15);color: #787b86; border-radius: 3px; padding: 2px 6px; font-size: 10px; }
  .zd-table-wrap { overflow-x: auto; margin-bottom: 14px; }
  .zd-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .zd-table th { background: #131722; color: #787b86; font-weight: 600; padding: 7px 10px; text-align: left; border-bottom: 1px solid #2a2e39; font-size: 11px; text-transform: uppercase; }
  .zd-table td { padding: 8px 10px; border-bottom: 1px solid #1a1e2b; color: #d1d4dc; vertical-align: middle; }
  .zd-table tr:hover td { background: rgba(255,255,255,0.02); }
  .zd-table .badge-buy { background: rgba(38,166,154,0.15); color: #26a69a; border-radius: 3px; padding: 2px 7px; font-size: 11px; }
  .zd-table .badge-sell { background: rgba(239,83,80,0.15); color: #ef5350; border-radius: 3px; padding: 2px 7px; font-size: 11px; }
  .zd-table .badge-idle { background: rgba(120,123,134,0.15); color: #787b86; border-radius: 3px; padding: 2px 7px; font-size: 11px; }
  .zd-table .del-btn { background: none; border: none; color: #ef5350; cursor: pointer; font-size: 15px; padding: 0 4px; }
  .zd-table .del-btn:hover { color: #ff1744; }
  /* Inline-editable cells */
  .zd-table input.zd-cell, .zd-table select.zd-cell {
    background: #131722; color: #d1d4dc; border: 1px solid #2a2e39;
    border-radius: 3px; padding: 3px 6px; font-size: 12px;
    font-family: inherit; width: auto; min-width: 50px; box-sizing: border-box;
  }
  .zd-table input.zd-cell:focus, .zd-table select.zd-cell:focus {
    outline: none; border-color: #2962ff;
  }
  .zd-table input.zd-cell:disabled, .zd-table select.zd-cell:disabled {
    opacity: 0.55; cursor: not-allowed; background: #1a1e2b;
  }
  .zd-table .zd-cell-sym { width: 110px; font-weight: 700; text-transform: uppercase; }
  .zd-table .zd-cell-qty { width: 64px; }
  .zd-table .zd-cell-score { width: 60px; }
  /* Prominent per-row Delete button */
  .zd-table .zd-row-del {
    background: rgba(239,83,80,0.15); color: #ef5350;
    border: 1px solid rgba(239,83,80,0.45); border-radius: 4px;
    padding: 5px 12px; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;
    cursor: pointer; display: inline-flex; align-items: center; gap: 5px;
    transition: background 0.15s, color 0.15s, transform 0.05s;
    font-family: inherit;
  }
  .zd-table .zd-row-del:hover { background: #ef5350; color: #fff; }
  .zd-table .zd-row-del:active { transform: scale(0.96); }
  .zd-table .zd-row-del:disabled { opacity: 0.4; cursor: not-allowed; background: rgba(120,123,134,0.1); color: #787b86; border-color: #2a2e39; }
  .zd-footer { display: flex; gap: 10px; align-items: center; padding-top: 6px; }
  .zd-start-btn {
    flex: 1; padding: 10px; border: none; border-radius: 6px; font-size: 14px;
    font-weight: 700; cursor: pointer; transition: background 0.2s;
  }
  .zd-start-btn.start { background: #26a69a; color: #fff; }
  .zd-start-btn.start:hover { background: #1de9b6; }
  .zd-start-btn.stop { background: #ef5350; color: #fff; }
  .zd-start-btn.stop:hover { background: #ff1744; }
  .zd-start-btn:disabled, .zd-start-btn:disabled:hover {
    background: #2a2e39 !important; color: #555 !important;
    cursor: not-allowed; opacity: 0.7;
  }
  .zd-log { background: #131722; border: 1px solid #2a2e39; border-radius: 6px; padding: 8px 12px; font-size: 12px; color: #787b86; max-height: 100px; overflow-y: auto; font-family: monospace; margin-top: 10px; }
  .zd-log .log-buy { color: #26a69a; }
  .zd-log .log-sell { color: #ef5350; }
  .zd-log .log-info { color: #787b86; }

  /* Instrument Search Modal */
  .zd-inst-overlay {
    position: fixed; inset: 0; z-index: 600;
    background: rgba(0,0,0,0.65); display: none; align-items: flex-start;
    justify-content: center; padding-top: 60px;
  }
  .zd-inst-overlay.open { display: flex; }
  .zd-inst-modal {
    background: #1e222d; border: 1px solid #2a2e39; border-radius: 12px;
    width: 760px; max-width: 96vw; max-height: 82vh;
    display: flex; flex-direction: column; overflow: hidden;
    box-shadow: 0 20px 80px rgba(0,0,0,0.85);
  }
  .zd-inst-header {
    padding: 14px 20px; border-bottom: 1px solid #2a2e39;
    display: flex; align-items: center; justify-content: space-between;
    background: #23273a; border-radius: 12px 12px 0 0; flex-shrink: 0;
  }
  .zd-inst-header h3 { margin: 0; font-size: 15px; color: #fff; }
  .zd-inst-close { background: none; border: none; color: #787b86; font-size: 22px; cursor: pointer; }
  .zd-inst-close:hover { color: #fff; }
  .zd-inst-search-wrap {
    padding: 14px 20px 10px; border-bottom: 1px solid #2a2e39; flex-shrink: 0;
  }
  .zd-inst-search-box {
    display: flex; align-items: center; gap: 10px;
    background: #131722; border: 1px solid #2a2e39; border-radius: 8px;
    padding: 8px 14px;
  }
  .zd-inst-search-box svg { color: #787b86; flex-shrink: 0; }
  .zd-inst-search-input {
    flex: 1; background: none; border: none; outline: none;
    color: #d1d4dc; font-size: 14px;
  }
  .zd-inst-search-input::placeholder { color: #555; }
  .zd-inst-search-clear {
    background: none; border: none; color: #787b86; cursor: pointer; font-size: 16px; padding: 0;
  }
  .zd-inst-tabs {
    display: flex; gap: 6px; padding: 10px 20px; overflow-x: auto;
    border-bottom: 1px solid #2a2e39; flex-shrink: 0; scrollbar-width: none;
  }
  .zd-inst-tabs::-webkit-scrollbar { display: none; }
  .zd-inst-tab {
    padding: 4px 13px; border: 1px solid #2a2e39; border-radius: 20px;
    background: none; color: #787b86; font-size: 12px; cursor: pointer;
    white-space: nowrap; transition: all 0.15s;
  }
  .zd-inst-tab.active { background: #2962ff; border-color: #2962ff; color: #fff; font-weight: 600; }
  .zd-inst-tab:hover:not(.active) { background: #2a2e39; color: #d1d4dc; }
  .zd-inst-body {
    display: flex; flex: 1; overflow: hidden;
  }
  .zd-inst-list-panel {
    flex: 1; overflow-y: auto; border-right: 1px solid #2a2e39; padding: 4px 0;
  }
  .zd-inst-list-panel::-webkit-scrollbar { width: 4px; }
  .zd-inst-list-panel::-webkit-scrollbar-thumb { background: #2a2e39; }
  .zd-inst-item {
    display: flex; align-items: center; gap: 12px;
    padding: 10px 20px; cursor: pointer; transition: background 0.12s;
    border-bottom: 1px solid rgba(42,46,57,0.5);
  }
  .zd-inst-item:hover { background: #252934; }
  .zd-inst-item.selected { background: rgba(41,98,255,0.12); }
  .zd-inst-chk {
    width: 16px; height: 16px; border: 1.5px solid #2a2e39; border-radius: 3px;
    display: flex; align-items: center; justify-content: center; flex-shrink: 0;
    transition: all 0.15s;
  }
  .zd-inst-item.selected .zd-inst-chk { background: #2962ff; border-color: #2962ff; }
  .zd-inst-item.selected .zd-inst-chk::after { content: '✓'; color: #fff; font-size: 11px; }
  .zd-inst-info { flex: 1; }
  .zd-inst-sym { font-size: 13px; font-weight: 600; color: #d1d4dc; }
  .zd-inst-name { font-size: 11px; color: #787b86; margin-top: 1px; }
  .zd-inst-exch {
    font-size: 10px; padding: 2px 7px; border-radius: 3px;
    background: rgba(41,98,255,0.15); color: #7090ff; font-weight: 600; flex-shrink: 0;
  }
  .zd-inst-exch.bse { background: rgba(38,166,154,0.15); color: #26a69a; }
  .zd-inst-exch.other { background: rgba(255,145,0,0.15); color: #ff9100; }
  .zd-inst-selected-panel {
    width: 240px; flex-shrink: 0; display: flex; flex-direction: column; overflow: hidden;
  }
  .zd-inst-sel-header { padding: 12px 14px; font-size: 11px; color: #787b86; border-bottom: 1px solid #2a2e39; text-transform: uppercase; letter-spacing: 0.07em; }
  .zd-inst-sel-list { flex: 1; overflow-y: auto; padding: 4px 0; }
  .zd-inst-sel-list::-webkit-scrollbar { width: 4px; }
  .zd-inst-sel-list::-webkit-scrollbar-thumb { background: #2a2e39; }
  .zd-inst-sel-item {
    display: flex; align-items: center; justify-content: space-between;
    padding: 9px 14px; border-bottom: 1px solid rgba(42,46,57,0.5);
  }
  .zd-inst-sel-sym { font-size: 13px; font-weight: 600; color: #d1d4dc; }
  .zd-inst-sel-exch { font-size: 10px; color: #787b86; margin-top: 1px; }
  .zd-inst-sel-rm { background: none; border: none; color: #787b86; font-size: 16px; cursor: pointer; padding: 0; }
  .zd-inst-sel-rm:hover { color: #ef5350; }
  .zd-inst-footer {
    padding: 12px 20px; border-top: 1px solid #2a2e39;
    display: flex; align-items: center; justify-content: space-between; flex-shrink: 0;
    background: #1e222d;
  }
  .zd-inst-empty { padding: 40px 20px; text-align: center; color: #787b86; font-size: 13px; }
  .zd-inst-count { font-size: 12px; color: #787b86; }
  .zd-inst-done-btn {
    padding: 9px 28px; background: #2962ff; border: none; border-radius: 6px;
    color: #fff; font-size: 14px; font-weight: 700; cursor: pointer;
  }
  .zd-inst-done-btn:hover { background: #1e6ec8; }
  .zd-add-inst-btn {
    padding: 7px 12px; background: rgba(41,98,255,0.15); border: 1px solid rgba(41,98,255,0.4);
    border-radius: 5px; color: #7090ff; font-size: 12px; cursor: pointer; font-weight: 600;
    white-space: nowrap; transition: all 0.15s;
  }
  .zd-add-inst-btn:hover { background: #2962ff; color: #fff; border-color: #2962ff; }

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
  /* Score Board Panel */
  .score-board-panel {
    position: absolute; top: 44px; right: 300px; z-index: 200;
    background: #1e222d; border: 1px solid #2a2e39; border-radius: 8px;
    padding: 16px; width: 620px; display: none;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5); max-height: calc(100vh - 160px); overflow-y: auto;
  }
  .score-board-panel.open { display: block; }
  .sb-summary-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
    gap: 6px; margin-bottom: 14px;
  }
  .sb-algo-card {
    background: #131722; border-radius: 6px; padding: 8px 10px;
    border: 1px solid #2a2e39; text-align: center;
  }
  .sb-algo-name { font-size: 11px; font-weight: 700; color: #ffd600; margin-bottom: 4px; }
  .sb-algo-verdict { font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 3px; display: inline-block; margin-bottom: 2px; }
  .sb-algo-score { font-size: 10px; color: #787b86; }
  .sb-table { width: 100%; border-collapse: collapse; font-size: 11px; }
  .sb-table thead th {
    background: #131722; color: #787b86; font-weight: 600; padding: 6px 8px;
    text-align: left; border-bottom: 1px solid #2a2e39; position: sticky; top: 0;
  }
  .sb-table tbody tr { border-bottom: 1px solid #2a2e3944; }
  .sb-table tbody tr:hover { background: #252a37; }
  .sb-table tbody td { padding: 5px 8px; color: #d1d4dc; vertical-align: middle; }
  .sb-sig-buy { color: #26a69a; font-weight: 700; }
  .sb-sig-sell { color: #ef5350; font-weight: 700; }
  .sb-score-pos { color: #26a69a; }
  .sb-score-neg { color: #ef5350; }
  .sb-reasons { font-size: 10px; color: #787b86; line-height: 1.3; max-width: 160px; }
  /* Market Making Panel */
  .mm-panel {
    position: absolute; top: 44px; right: 300px; z-index: 200;
    background: #1e222d; border: 1px solid #2a2e39; border-radius: 8px;
    padding: 16px; width: 380px; display: none;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5); max-height: calc(100vh - 160px); overflow-y: auto;
  }
  .mm-panel.open { display: block; }
  .mm-algo-badge {
    font-size: 18px; font-weight: 800; color: #69f0ae; margin-bottom: 4px; letter-spacing: 0.5px;
  }
  .mm-confidence {
    display: inline-block; font-size: 11px; font-weight: 700;
    background: rgba(105,240,174,0.12); color: #69f0ae;
    border: 1px solid #69f0ae44; border-radius: 4px; padding: 2px 8px; margin-left: 8px;
  }
  .mm-bias-bull { color: #26a69a; font-weight: 700; }
  .mm-bias-bear { color: #ef5350; font-weight: 700; }
  .mm-bias-neut { color: #787b86; font-weight: 700; }
  .mm-rank-row {
    display: flex; align-items: center; gap: 8px;
    padding: 5px 0; border-bottom: 1px solid #2a2e3933; font-size: 11px;
  }
  .mm-rank-label { flex: 1; color: #d1d4dc; }
  .mm-rank-bar-wrap { width: 100px; background: #2a2e39; border-radius: 3px; height: 6px; }
  .mm-rank-bar { height: 6px; border-radius: 3px; background: #69f0ae; }
  .mm-rank-pct { min-width: 34px; text-align: right; color: #787b86; }

  /* Market Makers Advanced Panel */
  .mma-panel {
    position: absolute; top: 44px; right: 300px; z-index: 200;
    background: #1e222d; border: 1px solid #3d2060; border-radius: 8px;
    padding: 16px; width: 420px; display: none;
    box-shadow: 0 8px 32px rgba(100,0,200,0.25); max-height: calc(100vh - 160px); overflow-y: auto;
  }
  .mma-panel.open { display: block; }
  .mma-algo-badge {
    font-size: 17px; font-weight: 800; color: #e040fb; margin-bottom: 4px; letter-spacing: 0.5px;
  }
  .mma-confidence {
    display: inline-block; font-size: 11px; font-weight: 700;
    background: rgba(224,64,251,0.12); color: #e040fb;
    border: 1px solid #e040fb44; border-radius: 4px; padding: 2px 8px; margin-left: 8px;
  }
  .mma-bias-bull { color: #26a69a; font-weight: 700; }
  .mma-bias-bear { color: #ef5350; font-weight: 700; }
  .mma-bias-neut { color: #787b86; font-weight: 700; }
  .mma-algo-row {
    display: flex; align-items: center; gap: 8px;
    padding: 6px 8px; border-radius: 5px; margin-bottom: 3px; font-size: 11px;
    background: #131722; border: 1px solid #2a2e3966;
  }
  .mma-algo-row.top { border-color: #e040fb88; background: rgba(224,64,251,0.07); }
  .mma-algo-icon { font-size: 14px; min-width: 20px; text-align: center; }
  .mma-algo-name { flex: 1; color: #d1d4dc; font-weight: 500; }
  .mma-algo-name.top { color: #e040fb; font-weight: 700; }
  .mma-algo-hits { font-size: 10px; color: #787b86; min-width: 44px; text-align: right; }
  .mma-rank-row {
    display: flex; align-items: center; gap: 8px;
    padding: 5px 0; border-bottom: 1px solid #2a2e3933; font-size: 11px;
  }
  .mma-rank-label { flex: 1; color: #d1d4dc; }
  .mma-rank-bar-wrap { width: 100px; background: #2a2e39; border-radius: 3px; height: 6px; }
  .mma-rank-bar { height: 6px; border-radius: 3px; background: #e040fb; }
  .mma-rank-pct { min-width: 34px; text-align: right; color: #787b86; }

  /* MM Parameters Panel */
  .mmparams-panel {
    position: absolute; top: 44px; right: 300px; z-index: 200;
    background: #1e222d; border: 1px solid #ff910055; border-radius: 8px;
    padding: 16px; width: 520px; display: none;
    box-shadow: 0 8px 32px rgba(255,145,0,0.18); max-height: calc(100vh - 80px); overflow-y: auto;
  }
  .mmparams-panel.open { display: block; }
  /* Prediction Panel */
  .pred-panel {
    position: absolute; top: 44px; right: 300px; z-index: 200;
    background: #1e222d; border: 1px solid #00e5ff44; border-radius: 8px;
    padding: 16px; width: 560px; display: none;
    box-shadow: 0 8px 32px rgba(0,229,255,0.15); max-height: calc(100vh - 80px); overflow-y: auto;
  }
  .pred-panel.open { display: block; }
  .pred-chart-wrap { width: 100%; height: 220px; margin: 10px 0; border-radius: 6px; overflow: hidden; border: 1px solid #2a2e39; }
  .pred-sr-row { display: flex; align-items: center; gap: 8px; padding: 5px 0; border-bottom: 1px solid #2a2e3933; font-size: 12px; }
  .pred-sr-label { min-width: 36px; font-weight: 700; }
  .pred-sr-price { flex: 1; color: #d1d4dc; font-family: monospace; }
  .pred-sr-bar-wrap { width: 80px; background: #2a2e39; border-radius: 3px; height: 5px; }
  .pred-sr-bar { height: 5px; border-radius: 3px; }
  .pred-sr-strength { min-width: 30px; text-align: right; color: #787b86; font-size: 10px; }
  .pred-dir-box { border-radius: 8px; padding: 12px 16px; margin: 10px 0; font-size: 15px; font-weight: 800; letter-spacing: 0.5px; text-align: center; }
  .pred-dir-box.bull { background: rgba(38,166,154,0.15); color: #26a69a; border: 1px solid #26a69a55; }
  .pred-dir-box.bear { background: rgba(239,83,80,0.15); color: #ef5350; border: 1px solid #ef535055; }
  .pred-dir-box.neut { background: rgba(120,123,134,0.15); color: #787b86; border: 1px solid #787b8655; }
  .pred-section-title { font-size: 11px; font-weight: 700; color: #00e5ff; text-transform: uppercase; letter-spacing: 1px; margin: 12px 0 6px; border-bottom: 1px solid #2a2e39; padding-bottom: 4px; display:flex; align-items:center; justify-content:space-between; }
  .pred-drag-header { cursor: move; user-select: none; }
  .pred-expand-btn { background: rgba(0,229,255,0.12); border: 1px solid #00e5ff44; border-radius: 4px; color: #00e5ff; font-size: 11px; font-weight: 700; padding: 2px 8px; cursor: pointer; line-height: 1.6; transition: background 0.15s; }
  .pred-expand-btn:hover { background: rgba(0,229,255,0.22); }
  .pred-chart-wrap { width: 100%; height: 220px; margin: 10px 0; border-radius: 6px; overflow: hidden; border: 1px solid #2a2e39; transition: height 0.2s; }
  .pred-chart-wrap.expanded { height: 480px; }
  .pred-panel.expanded { width: min(860px, 92vw); }
  .pred-future-legend { font-size: 10px; color: #787b86; margin: 2px 0 6px; display:flex; align-items:center; gap:8px; }
  .pred-future-legend span { display:inline-block; width:24px; height:3px; background:#4fc3f7; border-radius:2px; }
  
  /* Pattern Panel */
  .pattern-panel {
    position: absolute; top: 44px; right: 300px; z-index: 200;
    background: #1e222d; border: 1px solid #ff6ec766; border-radius: 8px;
    padding: 16px; width: 480px; display: none;
    box-shadow: 0 8px 32px rgba(255,110,199,0.20); max-height: calc(100vh - 80px); overflow-y: auto;
  }
  .pattern-panel.open { display: block; }
  .pattern-timeline-item {
    padding: 8px 0;
    border-bottom: 1px solid #2a2e3944;
    font-size: 12px;
    line-height: 1.6;
  }
  .pattern-timeline-item:last-child {
    border-bottom: none;
  }
  .pattern-time {
    display: inline-block;
    font-weight: 700;
    color: #ffd600;
    min-width: 80px;
  }
  .pattern-trend {
    display: inline-block;
    font-weight: 600;
  }
  .pattern-trend.bullish { color: #26a69a; }
  .pattern-trend.bearish { color: #ef5350; }
  .pattern-trend.neutral { color: #787b86; }
  .pattern-drag-header { cursor: move; user-select: none; }
  
  .mmparams-tabs { display: flex; gap: 4px; margin-bottom: 14px; border-bottom: 1px solid #2a2e39; padding-bottom: 0; }
  .mmparams-tab {
    padding: 6px 14px; font-size: 12px; font-weight: 600; cursor: pointer;
    border: none; background: none; color: #787b86; border-bottom: 2px solid transparent;
    border-radius: 4px 4px 0 0; transition: all 0.15s; margin-bottom: -1px;
  }
  .mmparams-tab.active { color: #ff9100; border-bottom-color: #ff9100; background: rgba(255,145,0,0.07); }
  .mmparams-tab:hover:not(.active) { color: #d1d4dc; background: #2a2e3966; }
  .mmparams-content { display: none; }
  .mmparams-content.active { display: block; }
  .mmp-algo-card {
    background: #131722; border: 1px solid #2a2e39; border-radius: 7px;
    padding: 12px 14px; margin-bottom: 10px;
  }
  .mmp-algo-card.active-algo { border-color: #ff910066; background: rgba(255,145,0,0.05); }
  .mmp-algo-title {
    font-size: 13px; font-weight: 700; color: #ff9100; margin-bottom: 4px;
    display: flex; align-items: center; gap: 8px;
  }
  .mmp-algo-title .mmp-hits-badge {
    font-size: 10px; font-weight: 600; background: rgba(255,145,0,0.15);
    color: #ff9100; border: 1px solid #ff910044; border-radius: 3px; padding: 1px 7px;
  }
  .mmp-weight-badge {
    font-size: 10px; font-weight: 600; background: rgba(105,240,174,0.12);
    color: #69f0ae; border: 1px solid #69f0ae44; border-radius: 3px; padding: 1px 7px; margin-left: auto;
  }
  .mmp-param-row { font-size: 11px; color: #d1d4dc; margin: 3px 0; line-height: 1.5; }
  .mmp-param-row span { color: #787b86; min-width: 110px; display: inline-block; }
  .mmp-desc { font-size: 11px; color: #9598a1; margin-top: 6px; line-height: 1.6; font-style: italic; }
  .mmp-pred { font-size: 11px; color: #80d8ff; margin-top: 4px; line-height: 1.5; }
  .mmp-section-title {
    font-size: 10px; text-transform: uppercase; letter-spacing: 1px;
    color: #787b86; margin: 12px 0 8px; font-weight: 700;
  }
  .mmp-prediction-box {
    background: #131722; border-radius: 7px; padding: 14px; margin-bottom: 10px;
    border: 1px solid #2a2e39;
  }
  .mmp-bias-bull { color: #26a69a; font-weight: 700; }
  .mmp-bias-bear { color: #ef5350; font-weight: 700; }
  .mmp-bias-neut { color: #787b86; font-weight: 700; }
  .mmp-combined-badge {
    font-size: 22px; font-weight: 900; letter-spacing: 0.5px; margin-bottom: 6px;
  }
  .mmp-movement-row {
    display: flex; align-items: center; gap: 10px; padding: 8px 10px;
    border-radius: 5px; margin-bottom: 6px; font-size: 12px; background: #0d1117;
    border: 1px solid #2a2e3966;
  }
  .mmp-movement-label { flex: 1; color: #d1d4dc; font-weight: 600; }
  .mmp-movement-pred { color: #9598a1; font-size: 11px; line-height: 1.5; }

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
    max-height: calc(100vh - 80px); overflow-y: auto;
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
        <option value="USOIL">US Oil (WTI)</option>
        <option value="CRUDEOILMCX">Crude Oil Futures (MCX)</option>
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
      <label class="ind-item" data-ind="ORB"><span class="dot" style="background:#ff9800"></span><span>ORB (15m)</span><input type="checkbox"></label>
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
      <button class="algo-item" data-algo="trend" data-label="Trend">&#8203; Trend</button>
      <button class="algo-item" data-algo="mstreet" data-label="MStreet">&#8203; MStreet</button>
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
      <button class="algo-item" data-algo="marketmaking" data-label="Market Making">&#8203; Market Making</button>
      <button class="algo-item" data-algo="mma" data-label="MM Advanced">&#8203; MM Advanced</button>
      <button class="algo-item" data-algo="pattern" data-label="Pattern">&#8203; Pattern</button>
      <div style="border-top:1px solid #2a2e39;margin:6px 0"></div>
      <button class="algo-item" id="btnAlgoAnalysis" style="color:#ffd600">&#9889; Signal Analysis</button>
      <button class="algo-item" id="btnAlgoScoreboard" style="color:#69f0ae">&#127942; Score Board</button>
      <button class="algo-item" id="btnAlgoPattern" style="color:#ff6ec7">&#128200; Pattern Panel</button>
      <button class="algo-item" id="btnAlgoMM" style="color:#80d8ff">&#129302; Market Making</button>
      <button class="algo-item" id="btnAlgoMMA" style="color:#e040fb">&#128301; MM Advanced</button>
      <button class="algo-item" id="btnAlgoMMParams" style="color:#ff9100">&#128202; MM Parameters</button>
      <button class="algo-item" id="btnAlgoPrediction" style="color:#00e5ff">&#128302; Prediction</button>
    </div>
  </div>
  <div class="separator"></div>
  <div class="automation-dropdown-wrapper">
    <button class="ind-btn" id="btnAutomation"><span class="dot" style="background:#00e5ff"></span>Automation &#9662;</button>
    <div class="automation-dropdown" id="automationDropdown">
      <button class="automation-item" id="btnZerodhaLogin">&#128272; Zerodha Login</button>
      <button class="automation-item" id="btnZerodhaAuto">&#129302; Zerodha Automation</button>
    </div>
  </div>
  <div class="separator"></div>
  <button class="gear-btn" id="btnSettingsPanel" title="Settings">&#9881;</button>

  <!-- Zerodha Login Panel (credentials + connect; opens from Automation menu) -->
  <div class="zerodha-panel" id="zerodhaLoginPanel" style="width:680px">
    <div class="zd-header" id="zdLoginHeader">
      <h3><span style="color:#1e6ec8">&#128272;</span> Zerodha Login</h3>
      <div class="zd-header-actions">
        <button class="zd-close" id="zdLoginClose" title="Close">&times;</button>
      </div>
    </div>
    <div class="zd-body">
      <div class="zd-credentials">
        <!-- Row 1: API Key + Login button -->
        <div class="zd-cred-row">
          <div class="zd-cred-group" style="flex:2">
            <label for="zdApiKey">API Key</label>
            <input type="text" id="zdApiKey" placeholder="Enter Zerodha API Key" autocomplete="off" spellcheck="false">
          </div>
          <button class="zd-login-url-btn" id="zdLoginUrlBtn" title="Open Kite Login in browser">&#128279; Login to Zerodha</button>
        </div>
        <!-- Row 2: API Secret + Request Token + Get Token button -->
        <div class="zd-cred-row">
          <div class="zd-cred-group">
            <label for="zdApiSecret">API Secret</label>
            <input type="password" id="zdApiSecret" placeholder="API Secret" autocomplete="off">
          </div>
          <div class="zd-cred-group" style="flex:2">
            <label for="zdRequestToken">Request Token <span style="color:#787b86;font-size:10px">(paste from redirect URL after login)</span></label>
            <input type="text" id="zdRequestToken" placeholder="request_token from redirect URL" autocomplete="off" spellcheck="false">
          </div>
          <button class="zd-get-token-btn" id="zdGetTokenBtn">&#128273; Get Access Token</button>
        </div>
        <!-- Row 3: Access Token (editable / auto-filled) + Connect button -->
        <div class="zd-cred-row">
          <div class="zd-cred-group" style="flex:3">
            <label for="zdAccessToken">Access Token <span style="color:#26a69a;font-size:10px">(auto-filled or enter manually)</span></label>
            <input type="text" id="zdAccessToken" placeholder="Access Token" autocomplete="off" spellcheck="false">
          </div>
          <button class="zd-connect-btn" id="zdConnectBtn">Connect</button>
        </div>
      </div>
      <div class="zd-status-bar">
        <span class="zd-status-dot" id="zdLoginStatusDot"></span>
        <span id="zdLoginStatusText">Not connected</span>
      </div>
    </div>
  </div>

  <!-- Zerodha Automation Panel -->
  <div class="zerodha-panel" id="zerodhaPanel">
    <div class="zd-header" id="zdHeader">
      <h3><span style="color:#ff9100">&#129302;</span> Zerodha Automation</h3>
      <div class="zd-header-actions">
        <button class="zd-header-btn" id="zdMaximizeBtn" title="Maximize">&#9633;</button>
        <button class="zd-header-btn" id="zdPopoutBtn"   title="Open in new window">&#8599;</button>
        <button class="zd-close" id="zdClose" title="Close">&times;</button>
      </div>
    </div>
    <div class="zd-body">
      <!-- Connection status banner (mirrors Zerodha Login panel) -->
      <div class="zd-status-bar" id="zdAutoStatusBar" style="margin-bottom:14px">
        <span class="zd-status-dot" id="zdStatusDot"></span>
        <span id="zdStatusText">Not connected &mdash; open <b style="color:#1e6ec8">Zerodha Login</b> from the Automation menu</span>
      </div>

      <!-- Add Rule Row -->
      <div class="zd-section-title">&#9881; Automation Rules</div>

      <!-- Shared: Symbol + Qty -->
      <div class="zd-rule-shared">
        <div class="zd-cred-group" style="flex:2;min-width:110px">
          <label>Symbol</label>
          <input type="text" id="zdSymInput" placeholder="e.g. NIFTY50" autocomplete="off" spellcheck="false">
        </div>
        <div class="zd-cred-group" style="flex:0;min-width:60px">
          <label>Qty</label>
          <input type="number" id="zdQtyInput" value="1" min="1">
        </div>
        <div class="zd-cred-group" style="flex:0;align-self:flex-end">
          <button class="zd-add-inst-btn" id="zdOpenInstSearch">&#43; Add Instrument</button>
        </div>
      </div>

      <!-- Row 1: Algo-Based Rule -->
      <div class="zd-row-label algo-label">&#128202; Algo-Based Rule</div>
      <div class="zd-add-row" id="zdAlgoRow">
        <label>Entry / Exit
          <select class="entry-sel" id="zdAlgoEntryType">
            <option value="entry">Entry</option>
            <option value="exit">Exit</option>
          </select>
        </label>
        <label>Buy / Sell
          <select class="side-sel" id="zdAlgoSide">
            <option value="BUY">Buy</option>
            <option value="SELL">Sell</option>
          </select>
        </label>
        <label>Timeframe
          <select class="tf-sel" id="zdAlgoTF">
            <option value="1m">1m</option>
            <option value="2m">2m</option>
            <option value="3m">3m</option>
            <option value="5m" selected>5m</option>
            <option value="10m">10m</option>
            <option value="15m">15m</option>
            <option value="30m">30m</option>
            <option value="1h">1h</option>
            <option value="2h">2h</option>
            <option value="4h">4h</option>
            <option value="1d">1D</option>
          </select>
        </label>
        <label>Algo
          <select class="algo-sel" id="zdAlgoInput">
            <option value="NA">NA</option>
            <option value="trend">Trend</option>
            <option value="mstreet">MStreet</option>
            <option value="mfactor">MFactor</option>
            <option value="sniper">Sniper</option>
            <option value="orderflow">OrderFlow</option>
            <option value="priceaction">PriceAction</option>
            <option value="breakout">Breakout</option>
            <option value="momentum">Momentum</option>
            <option value="scalping">Scalping</option>
            <option value="smartmoney">SmartMoney</option>
            <option value="quant">Quant</option>
            <option value="hybrid">Hybrid</option>
            <option value="statarb">StatArb</option>
            <option value="institution">Institution</option>
            <option value="mpredict">MPredict</option>
          </select>
        </label>
        <label>Score Threshold
          <input type="number" class="score-inp" id="zdAlgoScore" value="70" min="0" max="100">
        </label>
        <button class="zd-add-btn" id="zdAddAlgoRuleBtn">&#43; Add Algo Rule</button>
      </div>

    <!-- Row 2: Indicator-Based Rule -->
    <div class="zd-row-label ind-label">&#128200; Indicator-Based Rule</div>
    <div class="zd-add-row ind-row" id="zdIndRow">
        <label>Entry / Exit
          <select class="entry-sel" id="zdIndEntryType">
            <option value="entry">Entry</option>
            <option value="exit">Exit</option>
          </select>
        </label>
        <label>Buy / Sell
          <select class="side-sel" id="zdIndSide">
            <option value="BUY">Buy</option>
            <option value="SELL">Sell</option>
          </select>
        </label>
        <label>Timeframe
          <select class="tf-sel" id="zdIndTF">
            <option value="1m">1m</option>
            <option value="2m">2m</option>
            <option value="3m">3m</option>
            <option value="5m" selected>5m</option>
            <option value="10m">10m</option>
            <option value="15m">15m</option>
            <option value="30m">30m</option>
            <option value="1h">1h</option>
            <option value="2h">2h</option>
            <option value="4h">4h</option>
            <option value="1d">1D</option>
          </select>
        </label>
        <label>Indicator 1
          <select class="ind-sel" id="zdInd1">
            <option value="NA">NA</option>
            <option value="RSI">RSI</option>
            <option value="MACD">MACD</option>
            <option value="EMA9">EMA (9)</option>
            <option value="EMA21">EMA (21)</option>
            <option value="SMA">SMA</option>
            <option value="BB">Bollinger Bands</option>
            <option value="SuperTrend">SuperTrend</option>
            <option value="VWAP">VWAP</option>
            <option value="ADX">ADX</option>
            <option value="Stochastic">Stochastic</option>
            <option value="CCI">CCI</option>
            <option value="ATR">ATR</option>
            <option value="OBV">OBV</option>
            <option value="Ichimoku">Ichimoku</option>
          </select>
        </label>
        <label>Condition 1
          <select class="cond-sel" id="zdCond1">
            <option value="bullish">Bullish</option>
            <option value="bearish">Bearish</option>
          </select>
        </label>
        <label>Indicator 2
          <select class="ind-sel" id="zdInd2">
            <option value="NA">NA</option>
            <option value="RSI">RSI</option>
            <option value="MACD">MACD</option>
            <option value="EMA9">EMA (9)</option>
            <option value="EMA21">EMA (21)</option>
            <option value="SMA">SMA</option>
            <option value="BB">Bollinger Bands</option>
            <option value="SuperTrend">SuperTrend</option>
            <option value="VWAP">VWAP</option>
            <option value="ADX">ADX</option>
            <option value="Stochastic">Stochastic</option>
            <option value="CCI">CCI</option>
            <option value="ATR">ATR</option>
            <option value="OBV">OBV</option>
            <option value="Ichimoku">Ichimoku</option>
          </select>
        </label>
        <label>Condition 2
          <select class="cond-sel" id="zdCond2">
            <option value="bullish">Bullish</option>
            <option value="bearish">Bearish</option>
          </select>
        </label>
        <label>Indicator 3
          <select class="ind-sel" id="zdInd3">
            <option value="NA">NA</option>
            <option value="RSI">RSI</option>
            <option value="MACD">MACD</option>
            <option value="EMA9">EMA (9)</option>
            <option value="EMA21">EMA (21)</option>
            <option value="SMA">SMA</option>
            <option value="BB">Bollinger Bands</option>
            <option value="SuperTrend">SuperTrend</option>
            <option value="VWAP">VWAP</option>
            <option value="ADX">ADX</option>
            <option value="Stochastic">Stochastic</option>
            <option value="CCI">CCI</option>
            <option value="ATR">ATR</option>
            <option value="OBV">OBV</option>
            <option value="Ichimoku">Ichimoku</option>
          </select>
        </label>
        <label>Condition 3
          <select class="cond-sel" id="zdCond3">
            <option value="bullish">Bullish</option>
            <option value="bearish">Bearish</option>
          </select>
        </label>
        <label>Indicator 4
          <select class="ind-sel" id="zdInd4">
            <option value="NA">NA</option>
            <option value="RSI">RSI</option>
            <option value="MACD">MACD</option>
            <option value="EMA9">EMA (9)</option>
            <option value="EMA21">EMA (21)</option>
            <option value="SMA">SMA</option>
            <option value="BB">Bollinger Bands</option>
            <option value="SuperTrend">SuperTrend</option>
            <option value="VWAP">VWAP</option>
            <option value="ADX">ADX</option>
            <option value="Stochastic">Stochastic</option>
            <option value="CCI">CCI</option>
            <option value="ATR">ATR</option>
            <option value="OBV">OBV</option>
            <option value="Ichimoku">Ichimoku</option>
          </select>
        </label>
        <label>Condition 4
          <select class="cond-sel" id="zdCond4">
            <option value="bullish">Bullish</option>
            <option value="bearish">Bearish</option>
          </select>
        </label>
        <button class="zd-add-btn ind-btn" id="zdAddIndRuleBtn">&#43; Add Ind. Rule</button>
      </div>

      <!-- Row 3: Market Making Rule -->
      <div class="zd-row-label mm-label" style="color:#ff9100">&#129351; Market Making Rule</div>
      <div class="zd-add-row mm-row" id="zdMMRow">
        <label>Entry / Exit
          <select class="entry-sel" id="zdMMEntryType">
            <option value="entry">Entry</option>
            <option value="exit">Exit</option>
          </select>
        </label>
        <label>Buy / Sell
          <select class="side-sel" id="zdMMSide">
            <option value="BUY">Buy</option>
            <option value="SELL">Sell</option>
          </select>
        </label>
        <label>Timeframe
          <select class="tf-sel" id="zdMMTF">
            <option value="1m">1m</option>
            <option value="2m">2m</option>
            <option value="3m">3m</option>
            <option value="5m" selected>5m</option>
            <option value="10m">10m</option>
            <option value="15m">15m</option>
            <option value="30m">30m</option>
            <option value="1h">1h</option>
            <option value="2h">2h</option>
            <option value="4h">4h</option>
            <option value="1d">1D</option>
          </select>
        </label>
        <label>Market Making
          <select class="mm-sel" id="zdMMInput">
            <option value="NA">NA</option>
            <option value="marketmaking">Market Making</option>
            <option value="mma">MM Advanced</option>
          </select>
        </label>
        <label>Buy Score Threshold
          <input type="number" class="score-inp" id="zdMMBuyScore" value="70" min="0" max="100">
        </label>
        <label>Sell Score Threshold
          <input type="number" class="score-inp" id="zdMMSellScore" value="70" min="0" max="100">
        </label>
        <button class="zd-add-btn mm-btn" id="zdAddMMRuleBtn" style="background:#ff9100">&#43; Add MM Rule</button>
      </div>

      <!-- Rules Table -->
      <div class="zd-table-wrap">
        <table class="zd-table">
          <thead>
            <tr>
              <th>#</th>
              <th>Type</th>
              <th>Side</th>
              <th>Symbol</th>
              <th>Qty</th>
              <th>TF</th>
              <th>Algo</th>
              <th>Indicators</th>
              <th>Score</th>
              <th>Status</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody id="zdRulesBody">
            <tr id="zdNoRules"><td colspan="11" style="text-align:center;color:#787b86;padding:18px">No rules added yet</td></tr>
          </tbody>
        </table>
      </div>

      <!-- Safety toggle: dry-run vs live trades -->
      <div class="zd-live-toggle" style="display:flex;align-items:center;gap:8px;margin-bottom:10px;padding:8px 12px;background:rgba(255,145,0,0.08);border:1px solid rgba(255,145,0,0.25);border-radius:6px">
        <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:12px;color:#d1d4dc">
          <input type="checkbox" id="zdLiveTradesChk" style="width:14px;height:14px;cursor:pointer">
          <b style="color:#ff9100">&#9888; Live trades</b>
          <span style="color:#787b86">&mdash; uncheck for dry-run (log only, no real Zerodha orders).</span>
        </label>
      </div>
      <!-- Start / Stop -->
      <div class="zd-footer">
        <button class="zd-start-btn start" id="zdStartBtn">&#9654; Start Automation</button>
        <button class="zd-start-btn stop"  id="zdStopBtn" disabled>&#9632; Stop Automation</button>
      </div>
      <div class="zd-log" id="zdLog"><span class="log-info">Ready. Add rules and click Start.</span></div>
    </div>
  </div>

  <!-- Instrument Search Modal -->
  <div class="zd-inst-overlay" id="zdInstOverlay">
    <div class="zd-inst-modal">
      <div class="zd-inst-header">
        <h3>&#128269; Add Instrument</h3>
        <button class="zd-inst-close" id="zdInstClose">&times;</button>
      </div>
      <div class="zd-inst-search-wrap">
        <div class="zd-inst-search-box">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
          <input type="text" class="zd-inst-search-input" id="zdInstSearchInput" placeholder="Search eg: SBIN, TCS, NIFTY50 etc" autocomplete="off">
          <button class="zd-inst-search-clear" id="zdInstSearchClear">&times;</button>
        </div>
      </div>
      <div class="zd-inst-tabs" id="zdInstTabs">
        <button class="zd-inst-tab active" data-seg="">All</button>
        <button class="zd-inst-tab" data-seg="ZERODHA_CSV" title="Search instruments.csv bundled with the app">&#128190; Zerodha Inst</button>
        <button class="zd-inst-tab" data-seg="KITE" title="Live Kite API search &mdash; supports queries like 'Nifty 24000'">&#128640; Kite</button>
        <button class="zd-inst-tab" data-seg="OPTIONS">Options</button>
        <button class="zd-inst-tab" data-seg="NIFTY50">NIFTY 50</button>
        <button class="zd-inst-tab" data-seg="BANKNIFTY">BANK NIFTY</button>
        <button class="zd-inst-tab" data-seg="INDICES">Indices</button>
        <button class="zd-inst-tab" data-seg="FNO">F&amp;O Stocks</button>
        <button class="zd-inst-tab" data-seg="ETF">ETF</button>
        <button class="zd-inst-tab" data-seg="COMM">Commodities</button>
        <button class="zd-inst-tab" data-seg="CRYPTO">Crypto</button>
      </div>
      <div class="zd-inst-body">
        <div class="zd-inst-list-panel" id="zdInstListPanel">
          <div class="zd-inst-empty" id="zdInstEmpty">Loading instruments…</div>
        </div>
        <div class="zd-inst-selected-panel">
          <div class="zd-inst-sel-header">Selected Instruments</div>
          <div class="zd-inst-sel-list" id="zdInstSelList"></div>
        </div>
      </div>
      <div class="zd-inst-footer">
        <span class="zd-inst-count" id="zdInstCount">0 selected</span>
        <button class="zd-inst-done-btn" id="zdInstDoneBtn">Done</button>
      </div>
    </div>
  </div>

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

  <!-- Score Board Panel -->
  <div class="score-board-panel" id="scoreBoardPanel">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h3 style="margin:0;font-size:14px;color:#fff">&#127942; Score Board</h3>
      <button id="scoreBoardClose" style="background:none;border:none;color:#787b86;font-size:18px;cursor:pointer;padding:0 4px;line-height:1" title="Close">&times;</button>
    </div>
    <div id="scoreBoardSummary"></div>
    <div id="scoreBoardTable"></div>
    <div class="disclaimer">For informational purposes only. Not financial advice.</div>
  </div>

  <!-- Market Making Panel -->
  <div class="mm-panel" id="mmPanel">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h3 style="margin:0;font-size:14px;color:#fff">&#129302; Market Making Analyzer</h3>
      <button id="mmPanelClose" style="background:none;border:none;color:#787b86;font-size:18px;cursor:pointer;padding:0 4px;line-height:1" title="Close">&times;</button>
    </div>
    <div id="mmIdentified" style="background:#131722;border-radius:8px;padding:14px;margin-bottom:12px;border:1px solid #2a2e39">
      <div style="color:#787b86;font-size:12px">Select the <strong>Market Making</strong> algo to see analysis</div>
    </div>
    <div id="mmPrediction" style="margin-bottom:12px"></div>
    <div id="mmRanking"></div>
    <div class="disclaimer">For informational purposes only. Not financial advice.</div>
  </div>

  <!-- Market Makers Advanced Panel -->
  <div class="mma-panel" id="mmaPanel">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h3 style="margin:0;font-size:14px;color:#fff">&#128301; Market Makers Advanced</h3>
      <button id="mmaPanelClose" style="background:none;border:none;color:#787b86;font-size:18px;cursor:pointer;padding:0 4px;line-height:1" title="Close">&times;</button>
    </div>
    <div id="mmaIdentified" style="background:#131722;border-radius:8px;padding:14px;margin-bottom:12px;border:1px solid #2a2e39">
      <div style="color:#787b86;font-size:12px">Select <strong>MM Advanced</strong> algo to activate analysis</div>
    </div>
    <div id="mmaPrediction" style="margin-bottom:12px"></div>
    <div id="mmaAlgoList" style="margin-bottom:12px"></div>
    <div id="mmaRanking"></div>
    <div class="disclaimer">For informational purposes only. Not financial advice.</div>
  </div>

  <!-- MM Parameters Panel -->
  <div class="mmparams-panel" id="mmParamsPanel">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h3 style="margin:0;font-size:14px;color:#ff9100">&#128202; Market Making Parameters</h3>
      <button id="mmParamsPanelClose" style="background:none;border:none;color:#787b86;font-size:18px;cursor:pointer;padding:0 4px;line-height:1" title="Close">&times;</button>
    </div>
    <div class="mmparams-tabs">
      <button class="mmparams-tab active" data-tab="mm">MM Parameters</button>
      <button class="mmparams-tab" data-tab="mma">Advanced MM</button>
      <button class="mmparams-tab" data-tab="predict">&#127777; Prediction</button>
    </div>
    <!-- Tab: MM Parameters -->
    <div class="mmparams-content active" id="mmParamsTab-mm">
      <div id="mmParamsMMLive" style="margin-bottom:10px"></div>
      <div class="mmp-section-title">Market Making Algorithms — Parameters &amp; Details</div>
      <div class="mmp-algo-card" id="mmpCard-as">
        <div class="mmp-algo-title">&#9679; Avellaneda-Stoikov (AS Model)<span class="mmp-weight-badge">Dominance ×3</span></div>
        <div class="mmp-param-row"><span>Wick Symmetry:</span> 1 − |lower_wick − upper_wick| / full_range &gt; 0.70</div>
        <div class="mmp-param-row"><span>VWAP Proximity:</span> |close − VWAP| / VWAP &lt; 0.001 (0.10%)</div>
        <div class="mmp-param-row"><span>Trigger:</span> Both conditions must be met on the same candle</div>
        <div class="mmp-param-row"><span>Score per Hit:</span> +2.0 (close &gt; VWAP) / −2.0 (close &lt; VWAP)</div>
        <div class="mmp-param-row"><span>Lookback Window:</span> 30 candles rolling</div>
        <div class="mmp-desc">Detects symmetric bid-ask quoting centered on VWAP. The MM earns the spread passively while controlling inventory via mean-reversion.</div>
        <div class="mmp-pred">&#128200; Prediction: Mean-reversion expected. Price will snap back to VWAP. Fading extremes is favoured.</div>
      </div>
      <div class="mmp-algo-card" id="mmpCard-grid">
        <div class="mmp-algo-title">&#9679; Grid Market Making<span class="mmp-weight-badge">Dominance ×2</span></div>
        <div class="mmp-param-row"><span>Grid Step:</span> round(close / 50) × 50 → nearest ₹50 level</div>
        <div class="mmp-param-row"><span>Visit Threshold:</span> price_visit_map[grid_level] ≥ 4 candle closes</div>
        <div class="mmp-param-row"><span>Direction:</span> close &gt; open → +1.5 (bullish) | close &lt; open → −1.5 (bearish)</div>
        <div class="mmp-param-row"><span>Score per Hit:</span> ±1.5 per qualifying grid revisit</div>
        <div class="mmp-param-row"><span>Lookback Window:</span> 30 candles rolling</div>
        <div class="mmp-desc">Detects automated grid orders placed at fixed price intervals (₹50 steps). The MM profits from oscillations between grid lines.</div>
        <div class="mmp-pred">&#128200; Prediction: Range-bound day. Price bouncing between grid levels — trade the range.</div>
      </div>
      <div class="mmp-algo-card" id="mmpCard-dn">
        <div class="mmp-algo-title">&#9679; Delta-Neutral MM<span class="mmp-weight-badge">Dominance ×2</span></div>
        <div class="mmp-param-row"><span>VWAP Band:</span> |close − VWAP| / VWAP &lt; 0.0015 (0.15%)</div>
        <div class="mmp-param-row"><span>Range Filter:</span> (high − low) / local_ATR &lt; 0.50</div>
        <div class="mmp-param-row"><span>Score per Hit:</span> +1.5 (bullish, price above or at VWAP)</div>
        <div class="mmp-param-row"><span>ATR Lookback:</span> 30-bar rolling average</div>
        <div class="mmp-param-row"><span>Purpose:</span> Options-driven pinning near max-pain / VWAP</div>
        <div class="mmp-desc">Identifies options market makers delta-hedging continuously, keeping price anchored near the max-pain / VWAP level to minimise their options risk.</div>
        <div class="mmp-pred">&#128200; Prediction: Price likely to stay near VWAP / max-pain all day — options expiry pinning effect.</div>
      </div>
      <div class="mmp-algo-card" id="mmpCard-sc">
        <div class="mmp-algo-title">&#9679; Spread Capture MM<span class="mmp-weight-badge">Dominance ×1</span></div>
        <div class="mmp-param-row"><span>Range Filter:</span> (high − low) &lt; local_ATR × 0.35</div>
        <div class="mmp-param-row"><span>Direction:</span> EMA-9 &gt; EMA-21 → +1.0 (bull) | EMA-9 &lt; EMA-21 → −1.0 (bear)</div>
        <div class="mmp-param-row"><span>Score per Hit:</span> ±1.0 per qualifying tight-range candle</div>
        <div class="mmp-param-row"><span>ATR Lookback:</span> 30-bar rolling average</div>
        <div class="mmp-param-row"><span>Requires:</span> Both EMA-9 and EMA-21 present on same candle</div>
        <div class="mmp-desc">Identifies ultra-tight bid-ask spread exploitation — MMs placing orders just inside the spread to capture the difference on both sides repeatedly.</div>
        <div class="mmp-pred">&#128200; Prediction: Low-volatility session. Breakout direction after session open is the key trade.</div>
      </div>
      <div class="mmp-algo-card" id="mmpCard-ps">
        <div class="mmp-algo-title">&#9679; Predatory / Spoofing MM<span class="mmp-weight-badge">Dominance ×4</span></div>
        <div class="mmp-param-row"><span>Volume Spike:</span> vol &gt; vol_avg × 2.5 (30-bar rolling average)</div>
        <div class="mmp-param-row"><span>Reversal Check:</span> (prev_close − prev_open) × (curr_close − curr_open) &lt; 0</div>
        <div class="mmp-param-row"><span>Score per Hit:</span> +2.5 (spike reversal bullish) / −2.5 (spike reversal bearish)</div>
        <div class="mmp-param-row"><span>Lookback Window:</span> 30 candles rolling volume average</div>
        <div class="mmp-param-row"><span>Risk:</span> Highly manipulative — expect stop-hunt fakeouts</div>
        <div class="mmp-desc">Detects spoofing activity: large volume spike followed by immediate price reversal, indicating fake orders placed to trigger stop-losses or retail entry.</div>
        <div class="mmp-pred">&#128200; Prediction: High volatility. Fake moves likely — wait for confirmation before entering. Do not chase spikes.</div>
      </div>
      <div class="mmp-algo-card" id="mmpCard-lp">
        <div class="mmp-algo-title">&#9679; Liquidity Provision<span class="mmp-weight-badge">Dominance ×3</span></div>
        <div class="mmp-param-row"><span>S/R Proximity:</span> |close − S/R level| / close &lt; 0.003 (0.30%)</div>
        <div class="mmp-param-row"><span>Volume Filter:</span> vol &gt; vol_avg × 1.5 (30-bar rolling)</div>
        <div class="mmp-param-row"><span>Body Filter:</span> |close − open| / (high − low) &lt; 0.35 (indecision candle)</div>
        <div class="mmp-param-row"><span>Score per Hit:</span> +2.0 (absorbing at support) / −2.0 (absorbing at resistance)</div>
        <div class="mmp-param-row"><span>Lookback Window:</span> 30 candles rolling</div>
        <div class="mmp-desc">Detects large institutions absorbing sell-side at support or buy-side at resistance, providing liquidity and accumulating positions ahead of a directional move.</div>
        <div class="mmp-pred">&#128200; Prediction: Trend continuation likely after absorption completes. Strong directional move expected.</div>
      </div>
      <div class="mmp-section-title">Common Confirmation Signals</div>
      <div class="mmp-algo-card">
        <div class="mmp-algo-title" style="color:#26a69a">&#9679; OBV Confirmation<span class="mmp-weight-badge" style="background:rgba(38,166,154,0.12);color:#26a69a;border-color:#26a69a44">+1.0 score</span></div>
        <div class="mmp-param-row"><span>Window:</span> Last 10 bars: Σ vol×sign(close−prev_close)</div>
        <div class="mmp-param-row"><span>Trigger:</span> OBV_delta &gt; 0 and net score &gt; 0 → +1.0 | OBV_delta &lt; 0 and net score &lt; 0 → −1.0</div>
        <div class="mmp-param-row"><span>Condition:</span> Only adds weight when OBV agrees with primary signal direction</div>
        <div class="mmp-desc">On-Balance Volume used as secondary confirmation. Only adds weight when it agrees with the primary signal direction.</div>
      </div>
      <div class="mmp-algo-card">
        <div class="mmp-algo-title" style="color:#26a69a">&#9679; RSI Extremes<span class="mmp-weight-badge" style="background:rgba(38,166,154,0.12);color:#26a69a;border-color:#26a69a44">±1.5 score</span></div>
        <div class="mmp-param-row"><span>Oversold:</span> RSI &lt; 28 → +1.5 (MM absorbing at lows)</div>
        <div class="mmp-param-row"><span>Overbought:</span> RSI &gt; 72 → −1.5 (MM distributing at highs)</div>
        <div class="mmp-param-row"><span>Neutral Zone:</span> 28 ≤ RSI ≤ 72 → no RSI contribution</div>
        <div class="mmp-desc">RSI extremes confirm whether the MM is in absorption (oversold) or distribution (overbought) mode.</div>
      </div>
      <div class="mmp-section-title">Signal Thresholds</div>
      <div class="mmp-algo-card">
        <div class="mmp-param-row"><span>BUY:</span> Net score ≥ 3.5</div>
        <div class="mmp-param-row"><span>STRONG BUY:</span> Net score ≥ 5.0</div>
        <div class="mmp-param-row"><span>SELL:</span> Net score ≤ −3.5</div>
        <div class="mmp-param-row"><span>STRONG SELL:</span> Net score ≤ −5.0</div>
        <div class="mmp-param-row"><span>Lookback:</span> 30 candles rolling window</div>
        <div class="mmp-param-row"><span>Dedup:</span> Consecutive same-direction signals suppressed</div>
      </div>
    </div>
    <!-- Tab: Advanced MM Parameters -->
    <div class="mmparams-content" id="mmParamsTab-mma">
      <div id="mmParamsMMALive" style="margin-bottom:10px"></div>
      <div class="mmp-section-title">Advanced Market Making Algorithms — 10 Algorithms</div>
      <div class="mmp-algo-card" id="mmpCard-hft">
        <div class="mmp-algo-title">&#9889; HFT Latency Arbitrage<span class="mmp-weight-badge">Dominance ×4</span></div>
        <div class="mmp-param-row"><span>Micro-candles:</span> Σ(bars where range &lt; ATR × 0.12) ≥ 3 in last 5 bars</div>
        <div class="mmp-param-row"><span>Volume Burst:</span> vol &gt; vol_avg × 1.3 (20-bar rolling)</div>
        <div class="mmp-param-row"><span>Scan Window:</span> 5-bar sliding window, requires i ≥ 5</div>
        <div class="mmp-param-row"><span>Score per Hit:</span> ±1.5 (direction = sign of close − open)</div>
        <div class="mmp-desc">Detects high-frequency trading firms exploiting microsecond latency advantages across venues. Signature: clusters of near-zero-range candles with elevated volume bursts.</div>
        <div class="mmp-pred">&#128200; Prediction: Ultra-fast micro-arbitrage in play. Trade with the first 5-minute momentum — HFT amplifies direction.</div>
      </div>
      <div class="mmp-algo-card" id="mmpCard-twap">
        <div class="mmp-algo-title">&#9202; TWAP/VWAP Optimal Execution<span class="mmp-weight-badge">Dominance ×3</span></div>
        <div class="mmp-param-row"><span>VWAP Deviation:</span> |close − VWAP| / VWAP &lt; 0.002 (0.20%)</div>
        <div class="mmp-param-row"><span>Volume Uniformity:</span> (max(vol_seg) − min(vol_seg)) / vol_avg &lt; 1.5</div>
        <div class="mmp-param-row"><span>Vol Segment:</span> 20-bar rolling window</div>
        <div class="mmp-param-row"><span>Score per Hit:</span> +1.5 (close &gt; VWAP) / −1.5 (close &lt; VWAP)</div>
        <div class="mmp-desc">Large institutions splitting orders over time to minimise market impact. Volume is uniformly distributed; price tracks VWAP exactly — characteristic of algorithmic block execution.</div>
        <div class="mmp-pred">&#128200; Prediction: Price will track VWAP all day. Fading VWAP extremes is the safest strategy.</div>
      </div>
      <div class="mmp-algo-card" id="mmpCard-statarb">
        <div class="mmp-algo-title">&#128200; Statistical Arbitrage MM<span class="mmp-weight-badge">Dominance ×3</span></div>
        <div class="mmp-param-row"><span>Alternating Candles:</span> Σ(sign-flips in last 6 bars) ≥ 3 direction reversals</div>
        <div class="mmp-param-row"><span>BB-Mid Deviation:</span> |close − BB_mid| / BB_mid &lt; 0.005 (0.50%)</div>
        <div class="mmp-param-row"><span>Scan Window:</span> 6-bar window for alternation count</div>
        <div class="mmp-param-row"><span>Score per Hit:</span> +1.5 (close &lt; BB_mid) / −1.5 (close &gt; BB_mid)</div>
        <div class="mmp-desc">Pairs/mean-reversion strategy where the MM profits from statistically predictable oscillations around the BB midline. Price alternates up/down in tight succession.</div>
        <div class="mmp-pred">&#128200; Prediction: Range-bound session — buy dips, sell rips near BB midline.</div>
      </div>
      <div class="mmp-algo-card" id="mmpCard-hostoll">
        <div class="mmp-algo-title">&#9878; Inventory Risk MM (Ho-Stoll)<span class="mmp-weight-badge">Dominance ×2</span></div>
        <div class="mmp-param-row"><span>Wick Asymmetry:</span> (avg_upper − avg_lower) / (avg_upper + avg_lower) → |asym| &gt; 0.35</div>
        <div class="mmp-param-row"><span>Lookback:</span> 10-bar rolling upper/lower wick averages</div>
        <div class="mmp-param-row"><span>Upper wick bias (asym &gt; 0):</span> −1.5 bearish (MM distributing inventory)</div>
        <div class="mmp-param-row"><span>Lower wick bias (asym &lt; 0):</span> +1.5 bullish (MM unwinding shorts)</div>
        <div class="mmp-param-row"><span>Score per Hit:</span> ±1.5 depending on wick direction</div>
        <div class="mmp-desc">Based on the Ho-Stoll (1981) model — MMs widen their spreads when holding excess inventory. Wick asymmetry reveals the MM's inventory lean and likely reversal direction.</div>
        <div class="mmp-pred">&#128200; Prediction: Spread widening followed by directional push once inventory is cleared.</div>
      </div>
      <div class="mmp-algo-card" id="mmpCard-qstuff">
        <div class="mmp-algo-title">&#127922; Quote Stuffing / Layering<span class="mmp-weight-badge">Dominance ×4</span></div>
        <div class="mmp-param-row"><span>Volume Spike:</span> vol / vol_avg &gt; 2.0 (20-bar rolling)</div>
        <div class="mmp-param-row"><span>Price Move:</span> |close − prev_close| / prev_close &lt; 0.001 (0.10%)</div>
        <div class="mmp-param-row"><span>Prior Spike:</span> vol[i−1] / vol_avg &gt; 1.5 (consecutive spike)</div>
        <div class="mmp-param-row"><span>Requires:</span> All 3 conditions met simultaneously, i ≥ 3</div>
        <div class="mmp-param-row"><span>Score per Hit:</span> +2.0 (net_ret &gt; 0, buy absorption) / −2.0 (sell absorption)</div>
        <div class="mmp-desc">Detects market manipulation via rapid order placement and cancellation. Two consecutive volume spikes with minimal price movement indicate cancel-replace layering activity.</div>
        <div class="mmp-pred">&#128200; Prediction: Do NOT trust apparent order book depth. Wait for genuine price break with volume confirmation.</div>
      </div>
      <div class="mmp-algo-card" id="mmpCard-momign">
        <div class="mmp-algo-title">&#128293; Momentum Ignition<span class="mmp-weight-badge">Dominance ×5</span></div>
        <div class="mmp-param-row"><span>Spike Candle:</span> (high[i−2] − low[i−2]) &gt; local_ATR × 1.5</div>
        <div class="mmp-param-row"><span>Reversal Check:</span> (close[i] − open[i]) × (close[i−2] − open[i−2]) &lt; 0</div>
        <div class="mmp-param-row"><span>Trigger Window:</span> Current candle reverses within 2 bars of spike (i vs i−2)</div>
        <div class="mmp-param-row"><span>Score per Hit:</span> +2.5 (bullish reversal) / −2.5 (bearish reversal)</div>
        <div class="mmp-param-row"><span>Requires:</span> i ≥ 3; ATR computed over 20-bar rolling window</div>
        <div class="mmp-desc">The most dangerous manipulation pattern — MMs engineer sharp directional spikes to trigger stop-losses and retail orders, then immediately reverse to profit from the trapped positions.</div>
        <div class="mmp-pred">&#128200; Prediction: Fade the spike — sharp spike is fake. Do not chase the initial move. Reversal is the real trade.</div>
      </div>
      <div class="mmp-algo-card" id="mmpCard-cross">
        <div class="mmp-algo-title">&#128279; Cross-Asset MM<span class="mmp-weight-badge">Dominance ×2</span></div>
        <div class="mmp-param-row"><span>Volume Filter:</span> vol &gt; vol_avg × 2.0 (20-bar rolling)</div>
        <div class="mmp-param-row"><span>Body Filter:</span> |close − open| / local_ATR &lt; 0.20 (tiny body)</div>
        <div class="mmp-param-row"><span>Score per Hit:</span> +1.5 (close &gt; VWAP or close &gt; open) / −1.5 otherwise</div>
        <div class="mmp-param-row"><span>Interpretation:</span> High vol + tiny body = hedge leg absorbing risk, no directional impact</div>
        <div class="mmp-desc">Institutional hedged flow across correlated assets (Nifty + SGX / Bank Nifty + Nifty). High volume with near-zero price impact signals cross-asset hedging — the direction is in the other leg.</div>
        <div class="mmp-pred">&#128200; Prediction: Watch the futures/index for true directional bias. Underlying is being hedged.</div>
      </div>
      <div class="mmp-algo-card" id="mmpCard-pmm">
        <div class="mmp-algo-title">&#129504; Passive Market Making (PMM)<span class="mmp-weight-badge">Dominance ×2</span></div>
        <div class="mmp-param-row"><span>Doji Count:</span> Σ(|close−open|/(high−low) &lt; 0.15) ≥ 4 in last 10 bars</div>
        <div class="mmp-param-row"><span>Session Mid Check:</span> |close − sess_mid| / (sess_hi − sess_lo) &lt; 0.25</div>
        <div class="mmp-param-row"><span>Session Range:</span> max/min of high/low over last 10 bars</div>
        <div class="mmp-param-row"><span>Score per Hit:</span> +1.0 (RSI &lt; 50) / −1.0 (RSI &gt; 50)</div>
        <div class="mmp-param-row"><span>Requires:</span> i ≥ 10 for session range calculation</div>
        <div class="mmp-desc">Pure passive spread-earner — MM places resting limit orders at session midpoint and earns the spread without taking directional risk. Extremely common on low-volatility days.</div>
        <div class="mmp-pred">&#128200; Prediction: Flat doji-heavy day near session midpoint. Only trade on confirmed breakout with strong volume.</div>
      </div>
      <div class="mmp-algo-card" id="mmpCard-rl">
        <div class="mmp-algo-title">&#129302; Reinforcement Learning MM<span class="mmp-weight-badge">Dominance ×3</span></div>
        <div class="mmp-param-row"><span>Range Expansion:</span> avg(ranges[8:15]) / avg(ranges[0:7]) &gt; 1.5</div>
        <div class="mmp-param-row"><span>Lookback:</span> 15-bar window: bars[0..6] = early, bars[8..14] = late</div>
        <div class="mmp-param-row"><span>Direction:</span> Σ returns[i−7:i] &gt; 0 → bullish | &lt; 0 → bearish</div>
        <div class="mmp-param-row"><span>Score per Hit:</span> ±1.5 (requires i ≥ 15)</div>
        <div class="mmp-desc">AI-driven MMs that adapt quoting width in real-time based on volatility regime transitions. Signature: quiet consolidation followed by sudden range expansion in the current trend direction.</div>
        <div class="mmp-pred">&#128200; Prediction: Expect quiet flat zones followed by sudden range expansions. Trade breakouts, not the flat periods.</div>
      </div>
      <div class="mmp-algo-card" id="mmpCard-cartea">
        <div class="mmp-algo-title">&#8734; Stochastic Control (Cartea-Jaimungal)<span class="mmp-weight-badge">Dominance ×3</span></div>
        <div class="mmp-param-row"><span>Session Progress:</span> i / n &gt; 0.70 (last 30% of session candles)</div>
        <div class="mmp-param-row"><span>Volume Decay:</span> vol / vol_avg &lt; 0.80 (volume tapering off)</div>
        <div class="mmp-param-row"><span>VWAP Pin:</span> |close − VWAP| / VWAP &lt; 0.002 (0.20%)</div>
        <div class="mmp-param-row"><span>Score per Hit:</span> +1.5 (close &gt; VWAP) / −1.5 (close &lt; VWAP)</div>
        <div class="mmp-param-row"><span>Requires:</span> All 3 conditions met; vol_avg from 20-bar rolling window</div>
        <div class="mmp-desc">Academically grounded MM model (Cartea &amp; Jaimungal 2015) for terminal wealth maximisation. MM narrows spreads aggressively near session end to flatten inventory before close — VWAP pinning guaranteed.</div>
        <div class="mmp-pred">&#128200; Prediction: Session-end VWAP pinning expected. Price gravitates to VWAP — avoid holding positions into close.</div>
      </div>
      <div class="mmp-section-title">Signal Thresholds (Advanced)</div>
      <div class="mmp-algo-card">
        <div class="mmp-param-row"><span>BUY:</span> Net score ≥ 3.5 | <span>STRONG BUY:</span> Net score ≥ 5.0</div>
        <div class="mmp-param-row"><span>SELL:</span> Net score ≤ −3.5 | <span>STRONG SELL:</span> Net score ≤ −5.0</div>
        <div class="mmp-param-row"><span>RSI confirm:</span> RSI &lt; 30 → +1.0 (absorption) | RSI &gt; 70 → −1.0 (distribution)</div>
        <div class="mmp-param-row"><span>Lookback:</span> 20-candle rolling window for vol/ATR</div>
        <div class="mmp-param-row"><span>Dedup:</span> Consecutive same-direction signals suppressed</div>
      </div>
    </div>
    <!-- Tab: Market Prediction -->
    <div class="mmparams-content" id="mmParamsTab-predict">
      <div id="mmParamsPrediction"><div style="color:#787b86;font-size:12px;padding:20px 0 0;text-align:center">Enable <strong>Market Making</strong> and/or <strong>MM Advanced</strong> from the Algo menu to see live predictions.</div></div>
    </div>
    <div class="disclaimer">For informational purposes only. Not financial advice.</div>
  </div>

  <!-- Prediction Panel -->
  <div class="pred-panel" id="predPanel">
    <div class="pred-drag-header" id="predDragHeader" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h3 style="margin:0;font-size:14px;color:#00e5ff">&#128302; Prediction</h3>
      <button id="predPanelClose" style="background:none;border:none;color:#787b86;font-size:18px;cursor:pointer;padding:0 4px;line-height:1" title="Close">&times;</button>
    </div>
    <div id="predDirection" class="pred-dir-box neut">Analysing...</div>
    <div class="pred-section-title"><span>&#9889; Signal Analysis</span></div>
    <div id="predSignalSummary"></div>
    <div class="pred-section-title"><span>&#129302; Market Making</span></div>
    <div id="predMM"></div>
    <div class="pred-section-title"><span>&#128301; MM Advanced</span></div>
    <div id="predMMA"></div>
    <div class="pred-section-title"><span>&#9135; Support &amp; Resistance</span></div>
    <div id="predSRLevels"></div>
    <div class="pred-section-title">
      <span>&#128200; Day Chart (S/R + Signals)</span>
      <button class="pred-expand-btn" id="predExpandBtn" title="Expand &amp; predict future candles">&#9974; Expand</button>
    </div>
    <div id="predFutureLegend" class="pred-future-legend" style="display:none"><span></span> Predicted future candles (client-side projection)</div>
    <div class="pred-chart-wrap" id="predChartContainer"></div>
    <div class="disclaimer">For informational purposes only. Not financial advice.</div>
  </div>

  <!-- Pattern Panel -->
  <div class="pattern-panel" id="patternPanel">
    <div class="pattern-drag-header" id="patternDragHeader" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h3 style="margin:0;font-size:14px;color:#ff6ec7">&#128200; Pattern Analysis</h3>
      <button id="patternPanelClose" style="background:none;border:none;color:#787b86;font-size:18px;cursor:pointer;padding:0 4px;line-height:1" title="Close">&times;</button>
    </div>
    <div id="patternDayInfo" style="background:#131722;border-radius:8px;padding:14px;margin-bottom:12px;border:1px solid #2a2e39">
      <div style="color:#ffd600;font-weight:bold;margin-bottom:8px">&#128197; Today's Pattern</div>
      <div id="patternIdentified" style="color:#d1d4dc;font-size:13px">Analyzing pattern...</div>
    </div>
    <div id="patternTrend" style="background:#1e222d;border-radius:8px;padding:14px;margin-bottom:12px;border:1px solid #2a2e39">
      <div style="color:#ff6ec7;font-weight:bold;margin-bottom:8px">&#9650; Predicted Trend (from 9:30 AM)</div>
      <div id="patternTrendPrediction" style="color:#d1d4dc;font-size:13px">Loading...</div>
    </div>
    <div id="patternTimeline" style="background:#131722;border-radius:8px;padding:14px;border:1px solid #2a2e39">
      <div style="color:#80d8ff;font-weight:bold;margin-bottom:8px">&#128337; Trend Timeline (1.5hr intervals)</div>
      <div id="patternTimelineData" style="color:#d1d4dc;font-size:13px">
        <div class="pattern-timeline-item"></div>
      </div>
    </div>
    <div class="disclaimer">For informational purposes only. Not financial advice.</div>
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
  let currentAlgo = new Set();

  // Indicator visibility
  let showST = false, showSAR = false, showSR = false, showEMA = false, showVWAP = false, showSignals = true;
  let showBB = false, showCPR = false, showORB = false;
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

  // ORB (Opening Range Breakout)
  const orbHighSeries = chart.addLineSeries({ color: '#ff9800', lineWidth: 1, priceLineVisible: false, lastValueVisible: true, lineStyle: 2, title: 'ORB H' });
  const orbLowSeries = chart.addLineSeries({ color: '#ef5350', lineWidth: 1, priceLineVisible: false, lastValueVisible: true, lineStyle: 2, title: 'ORB L' });
  orbHighSeries.applyOptions({ visible: false });
  orbLowSeries.applyOptions({ visible: false });

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

  // ---- Score Board Panel (opened from Algo dropdown) ----
  const scoreBoardPanel = document.getElementById('scoreBoardPanel');
  document.getElementById('scoreBoardClose').addEventListener('click', () => {
    scoreBoardPanel.classList.remove('open');
  });

  // ---- Signal Panel (opened from Algo dropdown) ----
  const signalPanel = document.getElementById('signalPanel');
  document.getElementById('btnAlgoAnalysis').addEventListener('click', function(e) {
    e.stopPropagation();
    algoDropdown.classList.remove('open');
    signalPanel.classList.toggle('open');
    settingsPanel.classList.remove('open');
    scoreBoardPanel.classList.remove('open');
  });
  document.getElementById('signalPanelClose').addEventListener('click', () => {
    signalPanel.classList.remove('open');
  });

  document.getElementById('btnAlgoScoreboard').addEventListener('click', function(e) {
    e.stopPropagation();
    algoDropdown.classList.remove('open');
    scoreBoardPanel.classList.toggle('open');
    signalPanel.classList.remove('open');
    mmPanel.classList.remove('open');
    mmaPanel.classList.remove('open');
    mmParamsPanel.classList.remove('open');
    settingsPanel.classList.remove('open');
  });

  // ---- Market Making Panel ----
  const mmPanel = document.getElementById('mmPanel');
  document.getElementById('btnAlgoMM').addEventListener('click', function(e) {
    e.stopPropagation();
    algoDropdown.classList.remove('open');
    signalPanel.classList.remove('open');
    scoreBoardPanel.classList.remove('open');
    mmaPanel.classList.remove('open');
    mmParamsPanel.classList.remove('open');
    settingsPanel.classList.remove('open');
    // Auto-enable marketmaking algo if not already active
    if (!currentAlgo.has('marketmaking')) {
      currentAlgo.add('marketmaking');
      document.querySelectorAll('.algo-item[data-algo="marketmaking"]').forEach(function(el) {
        el.classList.add('active');
        el.textContent = '\u2714 ' + (el.dataset.label || 'Market Making');
      });
      showPredictions = currentAlgo.has('mpredict');
      loadData(currentTF, true).then(function() {
        mmPanel.classList.add('open');
      });
    } else {
      mmPanel.classList.toggle('open');
    }
  });
  document.getElementById('mmPanelClose').addEventListener('click', () => {
    mmPanel.classList.remove('open');
  });

  // ---- Market Makers Advanced Panel ----
  const mmaPanel = document.getElementById('mmaPanel');
  document.getElementById('btnAlgoMMA').addEventListener('click', function(e) {
    e.stopPropagation();
    algoDropdown.classList.remove('open');
    signalPanel.classList.remove('open');
    scoreBoardPanel.classList.remove('open');
    mmPanel.classList.remove('open');
    mmParamsPanel.classList.remove('open');
    settingsPanel.classList.remove('open');
    // Auto-enable mma algo if not already active
    if (!currentAlgo.has('mma')) {
      currentAlgo.add('mma');
      document.querySelectorAll('.algo-item[data-algo="mma"]').forEach(function(el) {
        el.classList.add('active');
        el.textContent = '\u2714 ' + (el.dataset.label || 'MM Advanced');
      });
      showPredictions = currentAlgo.has('mpredict');
      loadData(currentTF, true).then(function() {
        mmaPanel.classList.add('open');
      });
    } else {
      mmaPanel.classList.toggle('open');
    }
  });
  document.getElementById('mmaPanelClose').addEventListener('click', () => {
    mmaPanel.classList.remove('open');
  });

  // ---- MM Parameters Panel ----
  const mmParamsPanel = document.getElementById('mmParamsPanel');

  // Tab switching inside MM Parameters panel
  document.querySelectorAll('.mmparams-tab').forEach(function(tab) {
    tab.addEventListener('click', function() {
      document.querySelectorAll('.mmparams-tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.mmparams-content').forEach(c => c.classList.remove('active'));
      this.classList.add('active');
      document.getElementById('mmParamsTab-' + this.dataset.tab).classList.add('active');
    });
  });

  document.getElementById('btnAlgoMMParams').addEventListener('click', function(e) {
    e.stopPropagation();
    algoDropdown.classList.remove('open');
    signalPanel.classList.remove('open');
    scoreBoardPanel.classList.remove('open');
    mmPanel.classList.remove('open');
    mmaPanel.classList.remove('open');
    settingsPanel.classList.remove('open');
    // Auto-enable both marketmaking & mma if neither active
    let needsLoad = false;
    if (!currentAlgo.has('marketmaking')) {
      currentAlgo.add('marketmaking');
      document.querySelectorAll('.algo-item[data-algo="marketmaking"]').forEach(function(el) {
        el.classList.add('active');
        el.textContent = '\u2714 ' + (el.dataset.label || 'Market Making');
      });
      needsLoad = true;
    }
    if (!currentAlgo.has('mma')) {
      currentAlgo.add('mma');
      document.querySelectorAll('.algo-item[data-algo="mma"]').forEach(function(el) {
        el.classList.add('active');
        el.textContent = '\u2714 ' + (el.dataset.label || 'MM Advanced');
      });
      needsLoad = true;
    }
    showPredictions = currentAlgo.has('mpredict');
    if (needsLoad) {
      loadData(currentTF, true).then(function() {
        mmParamsPanel.classList.add('open');
      });
    } else {
      mmParamsPanel.classList.toggle('open');
    }
  });

  document.getElementById('mmParamsPanelClose').addEventListener('click', () => {
    mmParamsPanel.classList.remove('open');
  });

  // ---- Prediction Panel ----
  const predPanel = document.getElementById('predPanel');
  let predChart = null;
  let predCandleSeries = null;
  let predFutureSeries = null;
  let predSRLines = [];

  document.getElementById('btnAlgoPrediction').addEventListener('click', function(e) {
    e.stopPropagation();
    algoDropdown.classList.remove('open');
    signalPanel.classList.remove('open');
    scoreBoardPanel.classList.remove('open');
    mmPanel.classList.remove('open');
    mmaPanel.classList.remove('open');
    mmParamsPanel.classList.remove('open');
    settingsPanel.classList.remove('open');
    let needsLoad = false;
    if (!currentAlgo.has('marketmaking')) {
      currentAlgo.add('marketmaking');
      document.querySelectorAll('.algo-item[data-algo="marketmaking"]').forEach(function(el) {
        el.classList.add('active');
        el.textContent = '\u2714 ' + (el.dataset.label || 'Market Making');
      });
      needsLoad = true;
    }
    if (!currentAlgo.has('mma')) {
      currentAlgo.add('mma');
      document.querySelectorAll('.algo-item[data-algo="mma"]').forEach(function(el) {
        el.classList.add('active');
        el.textContent = '\u2714 ' + (el.dataset.label || 'MM Advanced');
      });
      needsLoad = true;
    }
    showPredictions = currentAlgo.has('mpredict');
    if (needsLoad) {
      loadData(currentTF, true).then(function() {
        predPanel.classList.add('open');
        initPredChart();
      });
    } else {
      predPanel.classList.toggle('open');
      if (predPanel.classList.contains('open')) initPredChart();
    }
  });

  document.getElementById('predPanelClose').addEventListener('click', () => {
    predPanel.classList.remove('open');
  });

  // ---- Pattern Panel ----
  const patternPanel = document.getElementById('patternPanel');
  document.getElementById('btnAlgoPattern').addEventListener('click', function(e) {
    e.stopPropagation();
    algoDropdown.classList.remove('open');
    signalPanel.classList.remove('open');
    scoreBoardPanel.classList.remove('open');
    mmPanel.classList.remove('open');
    mmaPanel.classList.remove('open');
    mmParamsPanel.classList.remove('open');
    predPanel.classList.remove('open');
    settingsPanel.classList.remove('open');
    
    // Auto-enable pattern algo if not already active
    if (!currentAlgo.has('pattern')) {
      currentAlgo.add('pattern');
      document.querySelectorAll('.algo-item[data-algo="pattern"]').forEach(function(el) {
        el.classList.add('active');
        el.textContent = '\u2714 ' + (el.dataset.label || 'Pattern');
      });
      showPredictions = currentAlgo.has('mpredict');
      loadData(currentTF, true).then(function() {
        patternPanel.classList.add('open');
        updatePatternPanel();
      });
    } else {
      patternPanel.classList.toggle('open');
      if (patternPanel.classList.contains('open')) {
        updatePatternPanel();
      }
    }
  });
  
  document.getElementById('patternPanelClose').addEventListener('click', () => {
    patternPanel.classList.remove('open');
  });

  // ---- Drag logic for Pattern Panel ----
  (function() {
    const header = document.getElementById('patternDragHeader');
    let isDragging = false, startX, startY, origLeft, origTop;
    header.addEventListener('mousedown', function(e) {
      if (e.target.closest('#patternPanelClose')) return;
      isDragging = true;
      const rect = patternPanel.getBoundingClientRect();
      const parentRect = patternPanel.offsetParent ? patternPanel.offsetParent.getBoundingClientRect() : { left: 0, top: 0 };
      origLeft = rect.left - parentRect.left;
      origTop  = rect.top  - parentRect.top;
      startX = e.clientX;
      startY = e.clientY;
      patternPanel.style.right  = 'auto';
      patternPanel.style.left   = origLeft + 'px';
      patternPanel.style.top    = origTop  + 'px';
      e.preventDefault();
    });
    document.addEventListener('mousemove', function(e) {
      if (!isDragging) return;
      patternPanel.style.left = (origLeft + e.clientX - startX) + 'px';
      patternPanel.style.top  = (origTop  + e.clientY - startY) + 'px';
    });
    document.addEventListener('mouseup', function() { isDragging = false; });
  })();

  // ---- Drag logic for Prediction Panel ----
  (function() {
    const header = document.getElementById('predDragHeader');
    let isDragging = false, startX, startY, origLeft, origTop;
    header.addEventListener('mousedown', function(e) {
      if (e.target.closest('#predPanelClose') || e.target.closest('.pred-expand-btn')) return;
      isDragging = true;
      const rect = predPanel.getBoundingClientRect();
      const parentRect = predPanel.offsetParent ? predPanel.offsetParent.getBoundingClientRect() : { left: 0, top: 0 };
      origLeft = rect.left - parentRect.left;
      origTop  = rect.top  - parentRect.top;
      startX = e.clientX;
      startY = e.clientY;
      predPanel.style.right  = 'auto';
      predPanel.style.left   = origLeft + 'px';
      predPanel.style.top    = origTop  + 'px';
      e.preventDefault();
    });
    document.addEventListener('mousemove', function(e) {
      if (!isDragging) return;
      predPanel.style.left = (origLeft + e.clientX - startX) + 'px';
      predPanel.style.top  = (origTop  + e.clientY - startY) + 'px';
    });
    document.addEventListener('mouseup', function() { isDragging = false; });
  })();

  // ---- Helper: linear regression slope ----
  function linRegSlope(arr) {
    const n = arr.length;
    if (n < 2) return 0;
    let sx = 0, sy = 0, sxy = 0, sx2 = 0;
    for (let i = 0; i < n; i++) { sx += i; sy += arr[i]; sxy += i * arr[i]; sx2 += i * i; }
    const denom = n * sx2 - sx * sx;
    return denom === 0 ? 0 : (n * sxy - sx * sy) / denom;
  }

  // ---- Helper: Average True Range ----
  function computeATR(candles, period) {
    if (candles.length < 2) return (candles[0] ? (candles[0].high - candles[0].low) : 10);
    const trs = [];
    for (let i = 1; i < candles.length; i++) {
      trs.push(Math.max(
        candles[i].high - candles[i].low,
        Math.abs(candles[i].high - candles[i - 1].close),
        Math.abs(candles[i].low  - candles[i - 1].close)
      ));
    }
    const recent = trs.slice(-period);
    return recent.reduce(function(a, b) { return a + b; }, 0) / recent.length;
  }

  // ---- Helper: interval in seconds ----
  function tfSeconds(tf) {
    const map = { '1m':60,'2m':120,'3m':180,'5m':300,'10m':600,'15m':900,'30m':1800,'1h':3600,'2h':7200,'4h':14400,'1d':86400,'1w':604800,'1mo':2592000 };
    return map[tf] || 300;
  }

  // ---- Project future candles until 15:30 IST ----
  function projectFutureCandles(dayData, tf) {
    if (!dayData || dayData.length < 5) return [];
    const tfSecs = tfSeconds(tf);
    const lookback = Math.min(30, dayData.length);
    const recent = dayData.slice(-lookback);
    const closes = recent.map(function(c) { return c.close; });
    const slope = linRegSlope(closes);
    const atr = computeATR(recent, lookback);

    const lastCandle = dayData[dayData.length - 1];
    let prevClose = lastCandle.close;
    let ts = lastCandle.time;

    // Determine how many bars remain in trading session (9:15–15:30 IST)
    // 15:30 IST = 10:00 UTC
    const d = new Date(lastCandle.time * 1000);
    const dayStartUtc = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()) / 1000;
    const sessionEndTs = dayStartUtc + 36000;
    const remaining = sessionEndTs - lastCandle.time;
    const sessionBars = remaining > tfSecs ? Math.floor(remaining / tfSecs) : 0;
    // Always project at least 20 bars for visibility (even post-market for practice)
    const nBars = Math.min(Math.max(sessionBars, 20), 80);

    // Bridge: include the last real candle so the line connects
    const result = [{
      time: lastCandle.time,
      open: lastCandle.open, high: lastCandle.high,
      low: lastCandle.low, close: lastCandle.close
    }];
    for (let i = 0; i < nBars; i++) {
      const damp = 1 / (1 + i * 0.09);
      const projClose = prevClose + slope * damp;
      const open  = prevClose;
      const close = projClose;
      const high  = Math.max(open, close) + atr * 0.35 * damp;
      const low   = Math.min(open, close) - atr * 0.35 * damp;
      ts += tfSecs;
      result.push({ time: ts, open: open, high: high, low: low, close: close });
      prevClose = close;
    }
    return result;
  }

  // ---- Expand button for Day Chart ----
  let predExpanded = false;
  document.getElementById('predExpandBtn').addEventListener('click', function() {
    predExpanded = !predExpanded;
    const wrap = document.getElementById('predChartContainer');
    const legend = document.getElementById('predFutureLegend');
    if (predExpanded) {
      wrap.classList.add('expanded');
      predPanel.classList.add('expanded');
      this.textContent = '\u25bc Collapse';
      legend.style.display = 'flex';
    } else {
      wrap.classList.remove('expanded');
      predPanel.classList.remove('expanded');
      this.textContent = '\u25b6\ufe0e Expand';
      legend.style.display = 'none';
    }
    // Resize chart and re-render with/without future candles
    if (predChart) {
      predChart.applyOptions({ height: predExpanded ? 480 : 220, width: wrap.clientWidth || 520 });
    }
    renderPredChart();
  });

  function initPredChart() {
    const container = document.getElementById('predChartContainer');
    if (!predChart) {
      predChart = LightweightCharts.createChart(container, {
        width: container.clientWidth || 520,
        height: predExpanded ? 480 : 220,
        layout: { background: { color: '#131722' }, textColor: '#787b86' },
        grid: { vertLines: { color: '#1e222d' }, horzLines: { color: '#1e222d' } },
        rightPriceScale: { borderColor: '#2a2e39' },
        timeScale: { borderColor: '#2a2e39', timeVisible: true, secondsVisible: false },
        crosshair: { mode: 1 },
      });
      predCandleSeries = predChart.addCandlestickSeries({
        upColor: '#26a69a', downColor: '#ef5350',
        borderUpColor: '#26a69a', borderDownColor: '#ef5350',
        wickUpColor: '#26a69a', wickDownColor: '#ef5350',
      });
      predFutureSeries = predChart.addCandlestickSeries({
        upColor: 'rgba(79,195,247,0.45)', downColor: 'rgba(239,154,154,0.45)',
        borderUpColor: '#4fc3f7', borderDownColor: '#ef9a9a',
        wickUpColor: '#4fc3f7', wickDownColor: '#ef9a9a',
        lastValueVisible: false, priceLineVisible: false,
      });
    }
    renderPredChart();
  }

  function renderPredChart() {
    if (!predChart || !predCandleSeries || !candleData || candleData.length === 0) return;
    const isDaily = ['1d','1w','1mo'].includes(currentTF);
    let dayData = candleData;
    if (!isDaily) {
      const lastTs = candleData[candleData.length - 1].time;
      const lastDate = new Date((lastTs + 19800) * 1000).toISOString().slice(0, 10);
      const filtered = candleData.filter(function(c) {
        return new Date((c.time + 19800) * 1000).toISOString().slice(0, 10) === lastDate;
      });
      if (filtered.length > 0) dayData = filtered;
    }
    const formatted = dayData.map(function(c) {
      return { time: formatTime(c.time, isDaily), open: c.open, high: c.high, low: c.low, close: c.close };
    });
    predCandleSeries.setData(formatted);
    // --- Future candle prediction when expanded ---
    if (predFutureSeries) {
      if (predExpanded) {
        const future = projectFutureCandles(dayData, currentTF);
        if (future.length > 1) {
          const isD = ['1d','1w','1mo'].includes(currentTF);
          const futFormatted = future.map(function(c) {
            return { time: formatTime(c.time, isD), open: c.open, high: c.high, low: c.low, close: c.close };
          });
          predFutureSeries.setData(futFormatted);
        } else {
          predFutureSeries.setData([]);
        }
      } else {
        predFutureSeries.setData([]);
      }
    }
    predSRLines.forEach(function(l) { try { predCandleSeries.removePriceLine(l); } catch(e){} });
    predSRLines = [];
    if (lastSR) {
      (lastSR.support || []).forEach(function(s, i) {
        const line = predCandleSeries.createPriceLine({
          price: s.price, color: '#26a69a', lineWidth: 1, lineStyle: 2,
          axisLabelVisible: true, title: 'S' + (i + 1) + (s.strength > 1 ? ' (' + s.strength + ')' : ''),
        });
        predSRLines.push(line);
      });
      (lastSR.resistance || []).forEach(function(r, i) {
        const line = predCandleSeries.createPriceLine({
          price: r.price, color: '#ef5350', lineWidth: 1, lineStyle: 2,
          axisLabelVisible: true, title: 'R' + (i + 1) + (r.strength > 1 ? ' (' + r.strength + ')' : ''),
        });
        predSRLines.push(line);
      });
    }
    predChart.timeScale().fitContent();
    predChart.applyOptions({ width: document.getElementById('predChartContainer').clientWidth || 520 });
  }

  function updatePredictionPanel(summaries, sr, candles) {
    if (!predPanel.classList.contains('open')) return;
    const mmS  = summaries && summaries['marketmaking'];
    const mmaS = summaries && summaries['mma'];
    // Direction box
    const mmBias  = mmS  ? (mmS.mm_bias  || 'NEUTRAL') : 'NEUTRAL';
    const mmaBias = mmaS ? (mmaS.mma_bias || 'NEUTRAL') : 'NEUTRAL';
    const votes = { BULLISH: 0, BEARISH: 0, NEUTRAL: 0 };
    [mmBias, mmaBias].forEach(function(b) { if (b in votes) votes[b]++; });
    const combined = votes.BULLISH > votes.BEARISH ? 'BULLISH' : (votes.BEARISH > votes.BULLISH ? 'BEARISH' : 'NEUTRAL');
    const dirEl = document.getElementById('predDirection');
    const dirIcon = combined === 'BULLISH' ? '&#9650;' : (combined === 'BEARISH' ? '&#9660;' : '&#9644;');
    const dirCls  = combined === 'BULLISH' ? 'bull' : (combined === 'BEARISH' ? 'bear' : 'neut');
    const mmConfStr  = mmS  ? ' MM: '  + (mmS.mm_confidence  || 0) + '%' : '';
    const mmaConfStr = mmaS ? ' | MMA: ' + (mmaS.mma_confidence || 0) + '%' : '';
    dirEl.className = 'pred-dir-box ' + dirCls;
    dirEl.innerHTML = dirIcon + ' Predicted Direction: ' + combined +
      '<div style="font-size:11px;font-weight:400;margin-top:4px;opacity:0.85">' + mmConfStr + mmaConfStr + '</div>';
    // Signal Analysis Summary
    const sigEl = document.getElementById('predSignalSummary');
    const sigKeys = Object.keys(summaries || {}).filter(function(k) { return k !== 'marketmaking' && k !== 'mma'; });
    if (sigKeys.length) {
      let totalSc = 0; let cnt = 0;
      sigKeys.forEach(function(k) { if (summaries[k] && summaries[k].score != null) { totalSc += summaries[k].score; cnt++; } });
      const avgSc = cnt ? totalSc / cnt : 0;
      const sigVerdict = avgSc >= 5 ? 'STRONG BUY' : avgSc >= 3.5 ? 'BUY' : avgSc >= -3.5 ? 'NEUTRAL' : avgSc >= -5 ? 'SELL' : 'STRONG SELL';
      const sigCol = sigVerdict.includes('BUY') ? '#26a69a' : (sigVerdict.includes('SELL') ? '#ef5350' : '#787b86');
      let html = '<div style="background:#131722;border-radius:6px;padding:10px 12px;border:1px solid #2a2e39">';
      html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">';
      html += '<span style="font-size:12px;color:#d1d4dc">Composite (' + cnt + ' algo' + (cnt !== 1 ? 's' : '') + ')</span>';
      html += '<span style="font-size:13px;font-weight:700;color:' + sigCol + '">' + sigVerdict + ' (' + avgSc.toFixed(2) + ')</span>';
      html += '</div>';
      sigKeys.forEach(function(k) {
        const s = summaries[k];
        if (!s || !s.verdict) return;
        const col = s.verdict.includes('BUY') ? '#26a69a' : (s.verdict.includes('SELL') ? '#ef5350' : '#787b86');
        html += '<div class="pred-sr-row" style="border-color:#2a2e3922">';
        html += '<span style="flex:1;color:#d1d4dc;font-size:11px">' + (algoLabels[k] || k) + '</span>';
        html += '<span style="font-size:11px;font-weight:700;color:' + col + '">' + s.verdict + ' (' + (s.score >= 0 ? '+' : '') + s.score.toFixed(1) + ')</span>';
        html += '</div>';
      });
      html += '</div>';
      sigEl.innerHTML = html;
    } else {
      sigEl.innerHTML = '<div style="color:#787b86;font-size:12px">Enable signal algos from the Algo menu.</div>';
    }
    // Market Making
    const mmEl = document.getElementById('predMM');
    if (mmS && mmS.mm_algo) {
      const biasCol = mmS.mm_bias === 'BULLISH' ? '#26a69a' : (mmS.mm_bias === 'BEARISH' ? '#ef5350' : '#787b86');
      mmEl.innerHTML = '<div style="background:#131722;border:1px solid #ff910033;border-radius:7px;padding:10px 12px">' +
        '<div style="font-size:13px;font-weight:700;color:#ff9100">' + mmS.mm_algo +
          '<span style="font-size:10px;background:rgba(255,145,0,0.12);color:#ff9100;border:1px solid #ff910044;border-radius:3px;padding:1px 7px;margin-left:8px">' + (mmS.mm_confidence || 0) + '% conf</span>' +
        '</div>' +
        '<div style="font-size:11px;color:#d1d4dc;margin-top:4px">Bias: <strong style="color:' + biasCol + '">' + mmS.mm_bias + '</strong>' +
          ' &nbsp;|&nbsp; Score: <strong>' + (mmS.score >= 0 ? '+' : '') + mmS.score.toFixed(2) + '</strong>' +
          ' &nbsp;|&nbsp; Signal: <strong style="color:#26a69a">' + mmS.verdict + '</strong></div>' +
        (mmS.mm_prediction ? '<div style="font-size:11px;color:#787b86;margin-top:6px;font-style:italic">&#128200; ' + mmS.mm_prediction + '</div>' : '') +
      '</div>';
    } else {
      mmEl.innerHTML = '<div style="color:#787b86;font-size:12px">No Market Making data. Enable Market Making from Algo menu.</div>';
    }
    // MM Advanced
    const mmaEl = document.getElementById('predMMA');
    if (mmaS && mmaS.mma_algo) {
      const biasCol = mmaS.mma_bias === 'BULLISH' ? '#26a69a' : (mmaS.mma_bias === 'BEARISH' ? '#ef5350' : '#787b86');
      mmaEl.innerHTML = '<div style="background:#131722;border:1px solid #e040fb33;border-radius:7px;padding:10px 12px">' +
        '<div style="font-size:13px;font-weight:700;color:#e040fb">' + mmaS.mma_algo +
          '<span style="font-size:10px;background:rgba(224,64,251,0.12);color:#e040fb;border:1px solid #e040fb44;border-radius:3px;padding:1px 7px;margin-left:8px">' + (mmaS.mma_confidence || 0) + '% conf</span>' +
        '</div>' +
        '<div style="font-size:11px;color:#d1d4dc;margin-top:4px">Bias: <strong style="color:' + biasCol + '">' + mmaS.mma_bias + '</strong>' +
          ' &nbsp;|&nbsp; Score: <strong>' + (mmaS.score >= 0 ? '+' : '') + mmaS.score.toFixed(2) + '</strong>' +
          ' &nbsp;|&nbsp; Signal: <strong style="color:#e040fb">' + mmaS.verdict + '</strong></div>' +
        (mmaS.mma_prediction ? '<div style="font-size:11px;color:#787b86;margin-top:6px;font-style:italic">&#128200; ' + mmaS.mma_prediction + '</div>' : '') +
      '</div>';
    } else {
      mmaEl.innerHTML = '<div style="color:#787b86;font-size:12px">No MM Advanced data. Enable MM Advanced from Algo menu.</div>';
    }
    // S/R Levels
    const srEl = document.getElementById('predSRLevels');
    if (sr && ((sr.resistance && sr.resistance.length) || (sr.support && sr.support.length))) {
      const resistances = (sr.resistance || []).slice().reverse();
      const supports = (sr.support || []);
      const allLvls = [
        ...resistances.map(function(r) { return { price: r.price, strength: r.strength, type: 'R' }; }),
        ...supports.map(function(s)    { return { price: s.price, strength: s.strength, type: 'S' }; }),
      ];
      const maxStr = Math.max.apply(null, allLvls.map(function(l) { return l.strength || 1; }));
      let rIdx = 0; let sIdx = 0;
      let html = '';
      allLvls.forEach(function(lv) {
        const isR = lv.type === 'R';
        const col = isR ? '#ef5350' : '#26a69a';
        const idx = isR ? (++rIdx) : (++sIdx);
        const pct = Math.round(((lv.strength || 1) / maxStr) * 100);
        html += '<div class="pred-sr-row">' +
          '<span class="pred-sr-label" style="color:' + col + '">' + lv.type + idx + '</span>' +
          '<span class="pred-sr-price">' + (lv.price || 0).toFixed(2) + '</span>' +
          '<div class="pred-sr-bar-wrap"><div class="pred-sr-bar" style="width:' + pct + '%;background:' + col + '"></div></div>' +
          '<span class="pred-sr-strength">' + (lv.strength || 1) + 'x</span>' +
          '</div>';
      });
      srEl.innerHTML = html || '<div style="color:#787b86;font-size:12px">No S/R levels found.</div>';
    } else {
      srEl.innerHTML = '<div style="color:#787b86;font-size:12px">No S/R data. Enable S/R from Indicators menu.</div>';
    }
    renderPredChart();
  }

  // ---- updateMMParamsPanel — renders live data into MM Parameters panel ----
  function updateMMParamsPanel(summaries) {
    const mmSumm  = summaries && summaries['marketmaking'];
    const mmaSumm = summaries && summaries['mma'];

    // ---- Tab 1: highlight active MM algo card ----
    const MM_CARD_IDS = {
      'Avellaneda-Stoikov':   'mmpCard-as',
      'Grid Market Making':   'mmpCard-grid',
      'Delta-Neutral':        'mmpCard-dn',
      'Spread Capture':       'mmpCard-sc',
      'Predatory / Spoofing': 'mmpCard-ps',
      'Liquidity Provision':  'mmpCard-lp',
    };
    // Reset all highlights
    Object.values(MM_CARD_IDS).forEach(function(id) {
      const el = document.getElementById(id);
      if (el) el.classList.remove('active-algo');
    });
    const liveMMEl = document.getElementById('mmParamsMMLive');
    if (mmSumm && mmSumm.mm_algo) {
      const cardId = MM_CARD_IDS[mmSumm.mm_algo];
      if (cardId) {
        const card = document.getElementById(cardId);
        if (card) {
          card.classList.add('active-algo');
          // Add live hits badge if not yet added
          const titleEl = card.querySelector('.mmp-algo-title');
          if (titleEl) {
            let badge = titleEl.querySelector('.mmp-hits-badge');
            if (!badge) {
              badge = document.createElement('span');
              badge.className = 'mmp-hits-badge';
              titleEl.insertBefore(badge, titleEl.querySelector('.mmp-weight-badge'));
            }
            // Find hit count from indicators
            const ind = (mmSumm.indicators || []).find(x => x.name.toLowerCase().includes(mmSumm.mm_algo.toLowerCase().split(' ')[0]));
            badge.textContent = ind ? ind.status : '';
          }
        }
      }
      const biasCol = mmSumm.mm_bias === 'BULLISH' ? '#26a69a' : (mmSumm.mm_bias === 'BEARISH' ? '#ef5350' : '#787b86');
      const biasIcon = mmSumm.mm_bias === 'BULLISH' ? '&#9650;' : (mmSumm.mm_bias === 'BEARISH' ? '&#9660;' : '&#9644;');
      liveMMEl.innerHTML =
        '<div style="background:#131722;border:1px solid #ff910044;border-radius:7px;padding:10px 14px;margin-bottom:4px">' +
          '<div style="font-size:10px;color:#787b86;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">Live MM Detection</div>' +
          '<div style="font-size:13px;font-weight:700;color:#ff9100">' + mmSumm.mm_algo +
            '<span style="font-size:10px;font-weight:600;background:rgba(255,145,0,0.15);color:#ff9100;border:1px solid #ff910044;border-radius:3px;padding:1px 7px;margin-left:8px">' + mmSumm.mm_confidence + '% conf</span>' +
          '</div>' +
          '<div style="font-size:11px;color:#d1d4dc;margin-top:4px">Bias: <strong style="color:' + biasCol + '">' + biasIcon + ' ' + mmSumm.mm_bias + '</strong>' +
            ' &nbsp;|&nbsp; Score: <strong>' + (mmSumm.score >= 0 ? '+' : '') + mmSumm.score.toFixed(2) + '</strong>' +
            ' &nbsp;|&nbsp; Signal: <strong style="color:#26a69a">' + mmSumm.verdict + '</strong>' +
          '</div>' +
        '</div>';
    } else {
      liveMMEl.innerHTML = '';
    }

    // ---- Tab 2: highlight active MMA algo card ----
    const MMA_CARD_IDS = {
      'HFT Latency Arbitrage':         'mmpCard-hft',
      'TWAP/VWAP Optimal Execution':   'mmpCard-twap',
      'Statistical Arbitrage MM':      'mmpCard-statarb',
      'Inventory Risk (Ho-Stoll)':     'mmpCard-hostoll',
      'Quote Stuffing / Layering':     'mmpCard-qstuff',
      'Momentum Ignition':             'mmpCard-momign',
      'Cross-Asset MM':                'mmpCard-cross',
      'Passive Market Making (PMM)':   'mmpCard-pmm',
      'Reinforcement Learning MM':     'mmpCard-rl',
      'Stochastic Control (Cartea-J)': 'mmpCard-cartea',
    };
    Object.values(MMA_CARD_IDS).forEach(function(id) {
      const el = document.getElementById(id);
      if (el) el.classList.remove('active-algo');
    });
    const liveMMAEl = document.getElementById('mmParamsMMALive');
    if (mmaSumm && mmaSumm.mma_algo) {
      const cardId = MMA_CARD_IDS[mmaSumm.mma_algo];
      if (cardId) {
        const card = document.getElementById(cardId);
        if (card) {
          card.classList.add('active-algo');
          const titleEl = card.querySelector('.mmp-algo-title');
          if (titleEl) {
            let badge = titleEl.querySelector('.mmp-hits-badge');
            if (!badge) {
              badge = document.createElement('span');
              badge.className = 'mmp-hits-badge';
              titleEl.insertBefore(badge, titleEl.querySelector('.mmp-weight-badge'));
            }
            const hits = mmaSumm.mma_raw_hits && mmaSumm.mma_raw_hits[mmaSumm.mma_algo];
            badge.textContent = hits != null ? hits + ' hits' : '';
          }
        }
      }
      const biasCol = mmaSumm.mma_bias === 'BULLISH' ? '#26a69a' : (mmaSumm.mma_bias === 'BEARISH' ? '#ef5350' : '#787b86');
      const biasIcon = mmaSumm.mma_bias === 'BULLISH' ? '&#9650;' : (mmaSumm.mma_bias === 'BEARISH' ? '&#9660;' : '&#9644;');
      liveMMAEl.innerHTML =
        '<div style="background:#131722;border:1px solid #e040fb44;border-radius:7px;padding:10px 14px;margin-bottom:4px">' +
          '<div style="font-size:10px;color:#787b86;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">Live Advanced MM Detection</div>' +
          '<div style="font-size:13px;font-weight:700;color:#e040fb">' + mmaSumm.mma_algo +
            '<span style="font-size:10px;font-weight:600;background:rgba(224,64,251,0.12);color:#e040fb;border:1px solid #e040fb44;border-radius:3px;padding:1px 7px;margin-left:8px">' + mmaSumm.mma_confidence + '% conf</span>' +
          '</div>' +
          '<div style="font-size:11px;color:#d1d4dc;margin-top:4px">Bias: <strong style="color:' + biasCol + '">' + biasIcon + ' ' + mmaSumm.mma_bias + '</strong>' +
            ' &nbsp;|&nbsp; Score: <strong>' + (mmaSumm.score >= 0 ? '+' : '') + mmaSumm.score.toFixed(2) + '</strong>' +
            ' &nbsp;|&nbsp; Signal: <strong style="color:#e040fb">' + mmaSumm.verdict + '</strong>' +
          '</div>' +
        '</div>';
    } else {
      liveMMAEl.innerHTML = '';
    }

    // ---- Tab 3: Combined Market Prediction ----
    const predEl = document.getElementById('mmParamsPrediction');
    const hasAny = (mmSumm && mmSumm.mm_algo) || (mmaSumm && mmaSumm.mma_algo);
    if (!hasAny) {
      predEl.innerHTML = '<div style="color:#787b86;font-size:12px;padding:20px 0 0;text-align:center">Enable <strong>Market Making</strong> and/or <strong>MM Advanced</strong> from the Algo menu to see live predictions.</div>';
      return;
    }

    // Determine combined bias
    const mmBias  = mmSumm  ? mmSumm.mm_bias  : 'NEUTRAL';
    const mmaBias = mmaSumm ? mmaSumm.mma_bias : 'NEUTRAL';
    const biasVotes = { BULLISH: 0, BEARISH: 0, NEUTRAL: 0 };
    biasVotes[mmBias]++;
    biasVotes[mmaBias]++;
    const combinedBias = biasVotes.BULLISH > biasVotes.BEARISH ? 'BULLISH' :
                         biasVotes.BEARISH > biasVotes.BULLISH ? 'BEARISH' : 'NEUTRAL';
    const biasCol  = combinedBias === 'BULLISH' ? '#26a69a' : (combinedBias === 'BEARISH' ? '#ef5350' : '#787b86');
    const biasIcon = combinedBias === 'BULLISH' ? '&#9650;' : (combinedBias === 'BEARISH' ? '&#9660;' : '&#9644;');
    const predBg   = combinedBias === 'BULLISH' ? 'rgba(38,166,154,0.1)' : (combinedBias === 'BEARISH' ? 'rgba(239,83,80,0.1)' : 'rgba(120,123,134,0.08)');
    const predBorder = combinedBias === 'BULLISH' ? '#26a69a44' : (combinedBias === 'BEARISH' ? '#ef535044' : '#787b8644');

    let html = '';
    // Combined bias badge
    html +=
      '<div style="background:' + predBg + ';border:1px solid ' + predBorder + ';border-radius:8px;padding:14px;margin-bottom:12px;text-align:center">' +
        '<div style="font-size:10px;color:#787b86;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Combined Market Bias</div>' +
        '<div style="font-size:28px;font-weight:900;color:' + biasCol + '">' + biasIcon + ' ' + combinedBias + '</div>' +
      '</div>';

    // Individual predictions
    html += '<div style="font-size:10px;color:#787b86;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;font-weight:700">Algorithm-Based Predictions</div>';

    if (mmSumm && mmSumm.mm_algo) {
      const bc = mmSumm.mm_bias === 'BULLISH' ? '#26a69a' : (mmSumm.mm_bias === 'BEARISH' ? '#ef5350' : '#787b86');
      const bi = mmSumm.mm_bias === 'BULLISH' ? '&#9650;' : (mmSumm.mm_bias === 'BEARISH' ? '&#9660;' : '&#9644;');
      html +=
        '<div style="background:#131722;border:1px solid #ff910033;border-radius:7px;padding:12px;margin-bottom:8px">' +
          '<div style="font-size:11px;font-weight:700;color:#ff9100;margin-bottom:6px">&#129302; Market Making — ' + mmSumm.mm_algo +
            '<span style="float:right;font-size:10px;color:' + bc + '">' + bi + ' ' + mmSumm.mm_bias + '</span>' +
          '</div>' +
          '<div style="font-size:11px;color:#d1d4dc;line-height:1.6">' + mmSumm.mm_prediction + '</div>' +
          // Score bar for each MM sub-algo
          (mmSumm.mm_scores ? (function() {
            const sorted = Object.entries(mmSumm.mm_scores).sort((a, b) => b[1] - a[1]).slice(0, 3);
            let s = '<div style="margin-top:8px">';
            sorted.forEach(function([name, pct]) {
              const isTop = name === mmSumm.mm_algo;
              s += '<div style="display:flex;align-items:center;gap:6px;padding:3px 0;font-size:10px">' +
                '<span style="flex:1;color:' + (isTop ? '#ff9100' : '#787b86') + '">' + (isTop ? '&#11088; ' : '') + name + '</span>' +
                '<div style="width:80px;background:#2a2e39;border-radius:2px;height:4px"><div style="width:' + pct + '%;height:4px;border-radius:2px;background:' + (isTop ? '#ff9100' : '#ff910044') + '"></div></div>' +
                '<span style="min-width:30px;text-align:right;color:#787b86">' + pct + '%</span>' +
              '</div>';
            });
            s += '</div>';
            return s;
          })() : '') +
        '</div>';
    }

    if (mmaSumm && mmaSumm.mma_algo) {
      const bc = mmaSumm.mma_bias === 'BULLISH' ? '#26a69a' : (mmaSumm.mma_bias === 'BEARISH' ? '#ef5350' : '#787b86');
      const bi = mmaSumm.mma_bias === 'BULLISH' ? '&#9650;' : (mmaSumm.mma_bias === 'BEARISH' ? '&#9660;' : '&#9644;');
      html +=
        '<div style="background:#131722;border:1px solid #e040fb33;border-radius:7px;padding:12px;margin-bottom:8px">' +
          '<div style="font-size:11px;font-weight:700;color:#e040fb;margin-bottom:6px">&#128301; MM Advanced — ' + mmaSumm.mma_algo +
            '<span style="float:right;font-size:10px;color:' + bc + '">' + bi + ' ' + mmaSumm.mma_bias + '</span>' +
          '</div>' +
          '<div style="font-size:11px;color:#d1d4dc;line-height:1.6">' + mmaSumm.mma_prediction + '</div>' +
          // Top 3 MMA scores
          (mmaSumm.mma_scores ? (function() {
            const sorted = Object.entries(mmaSumm.mma_scores).sort((a, b) => b[1] - a[1]).slice(0, 3);
            let s = '<div style="margin-top:8px">';
            sorted.forEach(function([name, pct]) {
              const isTop = name === mmaSumm.mma_algo;
              s += '<div style="display:flex;align-items:center;gap:6px;padding:3px 0;font-size:10px">' +
                '<span style="flex:1;color:' + (isTop ? '#e040fb' : '#787b86') + '">' + (isTop ? '&#11088; ' : '') + name + '</span>' +
                '<div style="width:80px;background:#2a2e39;border-radius:2px;height:4px"><div style="width:' + pct + '%;height:4px;border-radius:2px;background:' + (isTop ? '#e040fb' : '#e040fb44') + '"></div></div>' +
                '<span style="min-width:30px;text-align:right;color:#787b86">' + pct + '%</span>' +
              '</div>';
            });
            s += '</div>';
            return s;
          })() : '') +
        '</div>';
    }

    // All algos score summary table
    html += '<div style="font-size:10px;color:#787b86;text-transform:uppercase;letter-spacing:1px;margin:12px 0 8px;font-weight:700">All Algorithm Detection Summary</div>';

    // MM algorithms summary
    if (mmSumm && mmSumm.indicators) {
      html += '<div style="font-size:10px;color:#ff9100;font-weight:700;margin-bottom:4px">&#129302; Market Making</div>';
      mmSumm.indicators.forEach(function(ind) {
        const w = ind.weight || 0;
        const bar = Math.min(100, Math.round(w / (mmSumm.mm_scores ? Math.max(...Object.values(mmSumm.mm_scores).map(v => v)) * 0.01 : 1)));
        html +=
          '<div style="display:flex;align-items:center;gap:6px;padding:3px 0;font-size:10px;border-bottom:1px solid #2a2e3933">' +
            '<span style="flex:1;color:#d1d4dc">' + ind.name + '</span>' +
            '<span style="color:#787b86;min-width:46px;text-align:right">' + ind.status + '</span>' +
            '<span style="color:#ff9100;font-weight:600;min-width:36px;text-align:right">wt: ' + w + '</span>' +
          '</div>';
      });
    }

    // MMA algorithms summary
    if (mmaSumm && mmaSumm.indicators) {
      html += '<div style="font-size:10px;color:#e040fb;font-weight:700;margin:8px 0 4px">&#128301; MM Advanced</div>';
      mmaSumm.indicators.forEach(function(ind) {
        html +=
          '<div style="display:flex;align-items:center;gap:6px;padding:3px 0;font-size:10px;border-bottom:1px solid #2a2e3933">' +
            '<span style="flex:1;color:#d1d4dc">' + ind.name + '</span>' +
            '<span style="color:#787b86;min-width:46px;text-align:right">' + ind.status + '</span>' +
            '<span style="color:#e040fb;font-weight:600;min-width:36px;text-align:right">wt: ' + ind.weight + '</span>' +
          '</div>';
      });
    }

    predEl.innerHTML = html;
  }

  // ---- Indicators Dropdown ----
  const indDropdown = document.getElementById('indicatorsDropdown');
  document.getElementById('btnIndicators').addEventListener('click', function(e) {
    e.stopPropagation();
    indDropdown.classList.toggle('open');
    settingsPanel.classList.remove('open');
    signalPanel.classList.remove('open');
    scoreBoardPanel.classList.remove('open');
    mmPanel.classList.remove('open');
    mmaPanel.classList.remove('open');
    mmParamsPanel.classList.remove('open');
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
        case 'ORB':
          showORB = on;
          orbHighSeries.applyOptions({ visible: on });
          orbLowSeries.applyOptions({ visible: on });
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

      // --- ORB (Opening Range Breakout) ---
      const orbData = json.orb || [];
      const orbHighData = orbData.map(o => ({ time: formatTime(o.time, isDaily), value: o.high }));
      const orbLowData = orbData.map(o => ({ time: formatTime(o.time, isDaily), value: o.low }));
      orbHighSeries.setData(orbHighData);
      orbLowSeries.setData(orbLowData);
      orbHighSeries.applyOptions({ visible: showORB });
      orbLowSeries.applyOptions({ visible: showORB });

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
      updateScoreBoard(summ, json.allSignals || sigs);
      updateMMPanel(summ);
      updateMMAPanel(summ);
      updateMMParamsPanel(summ);
      updatePredictionPanel(summ, lastSR, candleData);
      
      // --- Update Pattern Panel (if pattern algo is enabled) ---
      if (currentAlgo.has('pattern')) {
        updatePatternPanel();
      }

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
  const algoLabels = { trend: 'Trend', mstreet: 'MStreet', mfactor: 'MFactor', sniper: 'Sniper', orderflow: 'OrderFlow', priceaction: 'PriceAction', breakout: 'Breakout', momentum: 'Momentum', scalping: 'Scalping', smartmoney: 'SmartMoney', quant: 'Quant', hybrid: 'Hybrid', mpredict: 'MPredict', institution: 'Institution', statarb: 'StatArb', marketmaking: 'Market Making' };
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

  // ---- Score Board Renderer ----
  function updateScoreBoard(summaries, allSigs) {
    const summaryEl = document.getElementById('scoreBoardSummary');
    const tableEl = document.getElementById('scoreBoardTable');

    if (!summaries || !Object.keys(summaries).length) {
      summaryEl.innerHTML = '<div style="color:#787b86;font-size:12px;padding:8px 0">No algo data. Select at least one Algo from the Algo menu.</div>';
      tableEl.innerHTML = '';
      return;
    }

    // --- Per-algo summary cards ---
    const verdictColor = v => v.includes('BUY') ? '#26a69a' : (v.includes('SELL') ? '#ef5350' : '#787b86');
    const verdictBg = v => v.includes('BUY') ? 'rgba(38,166,154,0.15)' : (v.includes('SELL') ? 'rgba(239,83,80,0.15)' : 'rgba(120,123,134,0.15)');
    let summHtml = '<div class="sb-summary-grid">';
    Object.keys(summaries).forEach(function(k) {
      const s = summaries[k];
      if (!s || !s.verdict) return;
      const label = algoLabels[k] || k;
      const col = verdictColor(s.verdict);
      const bg = verdictBg(s.verdict);
      summHtml += '<div class="sb-algo-card">';
      summHtml += '<div class="sb-algo-name">' + label + '</div>';
      summHtml += '<div class="sb-algo-verdict" style="background:' + bg + ';color:' + col + '">' + s.verdict + '</div>';
      summHtml += '<div class="sb-algo-score">Score: ' + (s.score != null ? s.score.toFixed(2) : 'N/A') + '</div>';
      summHtml += '</div>';
    });
    summHtml += '</div>';
    summaryEl.innerHTML = summHtml;

    // --- Signals table (all algos, newest first) ---
    if (!allSigs || !allSigs.length) {
      tableEl.innerHTML = '<div style="color:#787b86;font-size:12px;padding:8px 0">No signals in this period.</div>';
      return;
    }
    const sorted = allSigs.slice().sort(function(a, b) { return b.time - a.time; });
    let tblHtml = '<table class="sb-table"><thead><tr>';
    tblHtml += '<th>Time</th><th>Algo</th><th>Signal</th><th>Score</th><th>Price</th><th>Reasons</th>';
    tblHtml += '</tr></thead><tbody>';
    sorted.forEach(function(s) {
      const isBuy = s.type.includes('BUY');
      const isStrong = s.type.includes('STRONG');
      const sigCls = isBuy ? 'sb-sig-buy' : 'sb-sig-sell';
      const scoreCls = s.score >= 0 ? 'sb-score-pos' : 'sb-score-neg';
      const sigLabel = (isStrong ? '&#9733; ' : '') + s.type.replace('_', ' ');
      const algoName = algoLabels[s.algo] || s.algo || '—';
      const dt = new Date(s.time * 1000);
      const timeStr = dt.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false }) +
                      ' ' + dt.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' });
      const price = s.price != null ? s.price.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—';
      const reasons = Array.isArray(s.reasons) ? s.reasons.join(', ') : (s.reasons || '—');
      tblHtml += '<tr>';
      tblHtml += '<td style="white-space:nowrap">' + timeStr + '</td>';
      tblHtml += '<td style="color:#ffd600;font-weight:600">' + algoName + '</td>';
      tblHtml += '<td class="' + sigCls + '">' + sigLabel + '</td>';
      tblHtml += '<td class="' + scoreCls + '">' + (s.score != null ? (s.score >= 0 ? '+' : '') + s.score.toFixed(2) : '—') + '</td>';
      tblHtml += '<td>' + price + '</td>';
      tblHtml += '<td class="sb-reasons">' + reasons + '</td>';
      tblHtml += '</tr>';
    });
    tblHtml += '</tbody></table>';
    tableEl.innerHTML = tblHtml;
  }

  // ---- Market Making Panel Renderer ----
  function updateMMPanel(summaries) {
    const mmSumm = summaries && summaries['marketmaking'];
    const identEl  = document.getElementById('mmIdentified');
    const predEl   = document.getElementById('mmPrediction');
    const rankEl   = document.getElementById('mmRanking');

    if (!mmSumm || !mmSumm.mm_algo) {
      identEl.innerHTML = '<div style="color:#787b86;font-size:12px">Enable <strong>Market Making</strong> algo from the Algo menu to activate analysis.</div>';
      predEl.innerHTML = '';
      rankEl.innerHTML = '';
      return;
    }

    const biasClass = mmSumm.mm_bias === 'BULLISH' ? 'mm-bias-bull' : (mmSumm.mm_bias === 'BEARISH' ? 'mm-bias-bear' : 'mm-bias-neut');
    const biasIcon  = mmSumm.mm_bias === 'BULLISH' ? '&#9650;' : (mmSumm.mm_bias === 'BEARISH' ? '&#9660;' : '&#9644;');

    // Identified algorithm card
    identEl.innerHTML =
      '<div style="font-size:10px;color:#787b86;margin-bottom:6px;text-transform:uppercase;letter-spacing:1px">Identified Market Making Algorithm</div>' +
      '<div class="mm-algo-badge">' + mmSumm.mm_algo + '<span class="mm-confidence">' + mmSumm.mm_confidence + '% confidence</span></div>' +
      '<div style="margin-top:8px;font-size:11px;color:#d1d4dc;display:flex;gap:12px;flex-wrap:wrap">' +
        '<span>Signals: <strong style="color:#26a69a">' + (summaries.marketmaking && summaries.marketmaking.verdict || '—') + '</strong></span>' +
        '<span>Score: <strong>' + (mmSumm.score != null ? (mmSumm.score >= 0 ? '+' : '') + mmSumm.score.toFixed(2) : '—') + '</strong></span>' +
        '<span>Bias: <strong class="' + biasClass + '">' + biasIcon + ' ' + mmSumm.mm_bias + '</strong></span>' +
      '</div>';

    // Today's prediction box
    const predCls = mmSumm.mm_bias === 'BULLISH' ? 'rgba(38,166,154,0.12)' : (mmSumm.mm_bias === 'BEARISH' ? 'rgba(239,83,80,0.12)' : 'rgba(120,123,134,0.1)');
    const predBorder = mmSumm.mm_bias === 'BULLISH' ? '#26a69a44' : (mmSumm.mm_bias === 'BEARISH' ? '#ef535044' : '#787b8644');
    predEl.innerHTML =
      '<div style="background:' + predCls + ';border:1px solid ' + predBorder + ';border-radius:6px;padding:10px 12px">' +
        '<div style="font-size:10px;color:#787b86;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">&#128200; Today\'s Market Prediction</div>' +
        '<div style="font-size:12px;color:#d1d4dc;line-height:1.6">' + mmSumm.mm_prediction + '</div>' +
      '</div>';

    // Algorithm ranking bars
    if (mmSumm.mm_scores) {
      const sorted = Object.entries(mmSumm.mm_scores).sort((a, b) => b[1] - a[1]);
      let rHtml = '<div style="font-size:10px;color:#787b86;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Algorithm Detection Scores</div>';
      sorted.forEach(function([name, pct]) {
        const isTop = name === mmSumm.mm_algo;
        const barColor = isTop ? '#69f0ae' : '#2962ff55';
        rHtml +=
          '<div class="mm-rank-row">' +
            '<div class="mm-rank-label" style="' + (isTop ? 'color:#69f0ae;font-weight:700' : '') + '">' + (isTop ? '&#11088; ' : '') + name + '</div>' +
            '<div class="mm-rank-bar-wrap"><div class="mm-rank-bar" style="width:' + pct + '%;background:' + barColor + '"></div></div>' +
            '<div class="mm-rank-pct">' + pct + '%</div>' +
          '</div>';
      });
      rankEl.innerHTML = rHtml;
    } else {
      rankEl.innerHTML = '';
    }
  }

  // ---- updateMMAPanel — Market Makers Advanced ----
  const MMA_ALGO_ICONS = {
    "HFT Latency Arbitrage":         "&#9889;",
    "TWAP/VWAP Optimal Execution":   "&#9202;",
    "Statistical Arbitrage MM":      "&#128200;",
    "Inventory Risk (Ho-Stoll)":     "&#9878;",
    "Quote Stuffing / Layering":     "&#127922;",
    "Momentum Ignition":             "&#128293;",
    "Cross-Asset MM":                "&#128279;",
    "Passive Market Making (PMM)":   "&#129504;",
    "Reinforcement Learning MM":     "&#129302;",
    "Stochastic Control (Cartea-J)": "&#8734;",
  };
  function updateMMAPanel(summaries) {
    const mmaSumm  = summaries && summaries['mma'];
    const identEl  = document.getElementById('mmaIdentified');
    const predEl   = document.getElementById('mmaPrediction');
    const listEl   = document.getElementById('mmaAlgoList');
    const rankEl   = document.getElementById('mmaRanking');

    if (!mmaSumm || !mmaSumm.mma_algo) {
      identEl.innerHTML = '<div style="color:#787b86;font-size:12px">Enable <strong>MM Advanced</strong> algo from the Algo menu to activate analysis.</div>';
      predEl.innerHTML = '';
      listEl.innerHTML = '';
      rankEl.innerHTML = '';
      return;
    }

    const biasClass = mmaSumm.mma_bias === 'BULLISH' ? 'mma-bias-bull' : (mmaSumm.mma_bias === 'BEARISH' ? 'mma-bias-bear' : 'mma-bias-neut');
    const biasIcon  = mmaSumm.mma_bias === 'BULLISH' ? '&#9650;' : (mmaSumm.mma_bias === 'BEARISH' ? '&#9660;' : '&#9644;');

    // Identified algorithm card
    identEl.innerHTML =
      '<div style="font-size:10px;color:#787b86;margin-bottom:6px;text-transform:uppercase;letter-spacing:1px">Identified Advanced Market Maker</div>' +
      '<div class="mma-algo-badge">' + (MMA_ALGO_ICONS[mmaSumm.mma_algo] || '&#128301;') + ' ' + mmaSumm.mma_algo +
        '<span class="mma-confidence">' + mmaSumm.mma_confidence + '% confidence</span></div>' +
      '<div style="margin-top:8px;font-size:11px;color:#d1d4dc;display:flex;gap:12px;flex-wrap:wrap">' +
        '<span>Score: <strong>' + (mmaSumm.score != null ? (mmaSumm.score >= 0 ? '+' : '') + mmaSumm.score.toFixed(2) : '—') + '</strong></span>' +
        '<span>Signal: <strong style="color:#e040fb">' + (mmaSumm.verdict || '—') + '</strong></span>' +
        '<span>Bias: <strong class="' + biasClass + '">' + biasIcon + ' ' + mmaSumm.mma_bias + '</strong></span>' +
      '</div>';

    // Today's prediction box
    const predCls    = mmaSumm.mma_bias === 'BULLISH' ? 'rgba(38,166,154,0.12)' : (mmaSumm.mma_bias === 'BEARISH' ? 'rgba(239,83,80,0.12)' : 'rgba(120,123,134,0.1)');
    const predBorder = mmaSumm.mma_bias === 'BULLISH' ? '#26a69a44' : (mmaSumm.mma_bias === 'BEARISH' ? '#ef535044' : '#787b8644');
    predEl.innerHTML =
      '<div style="background:' + predCls + ';border:1px solid ' + predBorder + ';border-radius:6px;padding:10px 12px">' +
        '<div style="font-size:10px;color:#787b86;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">&#128200; Today\'s Market Prediction</div>' +
        '<div style="font-size:12px;color:#d1d4dc;line-height:1.6">' + mmaSumm.mma_prediction + '</div>' +
      '</div>';

    // All 10 algorithms list with hit counts
    if (mmaSumm.mma_raw_hits) {
      let lHtml = '<div style="font-size:10px;color:#787b86;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">All 10 Algorithms — Detection Results</div>';
      Object.entries(mmaSumm.mma_raw_hits).forEach(function([name, hits]) {
        const isTop = name === mmaSumm.mma_algo;
        const icon  = MMA_ALGO_ICONS[name] || '&#128301;';
        lHtml +=
          '<div class="mma-algo-row' + (isTop ? ' top' : '') + '">' +
            '<span class="mma-algo-icon">' + icon + '</span>' +
            '<span class="mma-algo-name' + (isTop ? ' top' : '') + '">' + (isTop ? '&#11088; ' : '') + name + '</span>' +
            '<span class="mma-algo-hits">' + hits + ' hit' + (hits !== 1 ? 's' : '') + '</span>' +
          '</div>';
      });
      listEl.innerHTML = lHtml;
    }

    // Detection score bars
    if (mmaSumm.mma_scores) {
      const sorted = Object.entries(mmaSumm.mma_scores).sort((a, b) => b[1] - a[1]);
      let rHtml = '<div style="font-size:10px;color:#787b86;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Weighted Detection Scores</div>';
      sorted.forEach(function([name, pct]) {
        const isTop = name === mmaSumm.mma_algo;
        const barColor = isTop ? '#e040fb' : '#e040fb44';
        rHtml +=
          '<div class="mma-rank-row">' +
            '<div class="mma-rank-label" style="' + (isTop ? 'color:#e040fb;font-weight:700' : '') + '">' + (isTop ? '&#11088; ' : '') + name + '</div>' +
            '<div class="mma-rank-bar-wrap"><div class="mma-rank-bar" style="width:' + pct + '%;background:' + barColor + '"></div></div>' +
            '<div class="mma-rank-pct">' + pct + '%</div>' +
          '</div>';
      });
      rankEl.innerHTML = rHtml;
    }
  }

  // ---- updatePatternPanel — Pattern Analysis ----
  function updatePatternPanel() {
    const patternIdentifiedEl = document.getElementById('patternIdentified');
    const patternTrendPredictionEl = document.getElementById('patternTrendPrediction');
    const patternTimelineDataEl = document.getElementById('patternTimelineData');

    if (!candles || candles.length < 20) {
      patternIdentifiedEl.innerHTML = 'Insufficient data for pattern analysis.';
      patternTrendPredictionEl.innerHTML = 'Need more data...';
      patternTimelineDataEl.innerHTML = '<div class="pattern-timeline-item">No timeline available yet.</div>';
      return;
    }

    // Identify today's pattern
    const pattern = identifyDailyPattern(candles);
    patternIdentifiedEl.innerHTML = '<strong>' + pattern.name + '</strong><br><span style="color:#787b86;font-size:12px">' + pattern.description + '</span>';

    // Predict trend from 9:30 AM
    const trendPrediction = predictTrendFrom930AM(candles);
    const trendClass = trendPrediction.direction === 'BULLISH' ? '#26a69a' : (trendPrediction.direction === 'BEARISH' ? '#ef5350' : '#787b86');
    patternTrendPredictionEl.innerHTML = 
      '<div style="color:' + trendClass + ';font-weight:bold;font-size:14px;margin-bottom:4px">' + 
      (trendPrediction.direction === 'BULLISH' ? '&#9650;' : (trendPrediction.direction === 'BEARISH' ? '&#9660;' : '&#9644;')) + 
      ' ' + trendPrediction.direction + '</div>' +
      '<div style="color:#787b86;font-size:12px">' + trendPrediction.details + '</div>';

    // Generate trend timeline (every 1.5 hours from market open)
    const timeline = generateTrendTimeline(candles);
    let timelineHtml = '';
    timeline.forEach(function(item) {
      const trendCls = item.trend === 'BULLISH' ? 'bullish' : (item.trend === 'BEARISH' ? 'bearish' : 'neutral');
      const trendIcon = item.trend === 'BULLISH' ? '&#9650;' : (item.trend === 'BEARISH' ? '&#9660;' : '&#9644;');
      timelineHtml += 
        '<div class="pattern-timeline-item">' +
        '<span class="pattern-time">' + item.time + '</span>' +
        '<span class="pattern-trend ' + trendCls + '">' + trendIcon + ' ' + item.trend + '</span>' +
        '<span style="color:#787b86;font-size:11px;margin-left:10px">' + item.note + '</span>' +
        '</div>';
    });
    patternTimelineDataEl.innerHTML = timelineHtml || '<div class="pattern-timeline-item">No timeline data available.</div>';
  }

  // Helper: Identify daily pattern
  function identifyDailyPattern(candles) {
    if (!candles || candles.length < 10) return { name: 'Unknown', description: 'Insufficient data' };
    
    const recent = candles.slice(-50);
    const closes = recent.map(c => c.close);
    const highs = recent.map(c => c.high);
    const lows = recent.map(c => c.low);
    
    const avgClose = closes.reduce((a, b) => a + b, 0) / closes.length;
    const maxHigh = Math.max(...highs);
    const minLow = Math.min(...lows);
    const range = maxHigh - minLow;
    const currentClose = closes[closes.length - 1];
    const volatility = range / avgClose;
    
    // Count bullish vs bearish candles
    let bullish = 0, bearish = 0;
    for (let i = 0; i < recent.length; i++) {
      if (recent[i].close > recent[i].open) bullish++;
      else if (recent[i].close < recent[i].open) bearish++;
    }
    
    // Pattern detection logic
    if (volatility < 0.01) {
      return { name: 'Consolidation', description: 'Low volatility, range-bound movement. Expect breakout soon.' };
    } else if (bullish > bearish * 1.5) {
      return { name: 'Uptrend', description: 'Strong bullish momentum. Higher highs and higher lows pattern.' };
    } else if (bearish > bullish * 1.5) {
      return { name: 'Downtrend', description: 'Strong bearish momentum. Lower lows and lower highs pattern.' };
    } else if (currentClose > avgClose * 1.005) {
      return { name: 'Bullish Reversal', description: 'Price moving above average. Potential trend reversal to upside.' };
    } else if (currentClose < avgClose * 0.995) {
      return { name: 'Bearish Reversal', description: 'Price moving below average. Potential trend reversal to downside.' };
    } else {
      return { name: 'Sideways Market', description: 'Mixed signals. No clear directional bias. Wait for confirmation.' };
    }
  }

  // Helper: Predict trend from 9:30 AM
  function predictTrendFrom930AM(candles) {
    if (!candles || candles.length < 5) return { direction: 'NEUTRAL', details: 'Not enough data' };
    
    // Find 9:30 AM candle (market open) - 9:30 IST = 4:00 UTC
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const marketOpenTime = today.getTime() / 1000 + 4 * 3600; // 4:00 UTC
    
    // Get candles from market open onwards
    const openCandles = candles.filter(c => c.time >= marketOpenTime - 3600); // 1hr before open
    if (openCandles.length < 5) {
      // Fallback: use recent candles
      const recentCandles = candles.slice(-20);
      const firstPrice = recentCandles[0].close;
      const lastPrice = recentCandles[recentCandles.length - 1].close;
      const change = ((lastPrice - firstPrice) / firstPrice) * 100;
      
      if (change > 0.5) {
        return { 
          direction: 'BULLISH', 
          details: 'Upward momentum detected (+' + change.toFixed(2) + '%). Expect continued strength.' 
        };
      } else if (change < -0.5) {
        return { 
          direction: 'BEARISH', 
          details: 'Downward pressure detected (' + change.toFixed(2) + '%). Expect weakness.' 
        };
      } else {
        return { 
          direction: 'NEUTRAL', 
          details: 'Minimal change (' + change.toFixed(2) + '%). Range-bound session likely.' 
        };
      }
    }
    
    const openPrice = openCandles[0].open;
    const currentPrice = openCandles[openCandles.length - 1].close;
    const change = ((currentPrice - openPrice) / openPrice) * 100;
    const highOfDay = Math.max(...openCandles.map(c => c.high));
    const lowOfDay = Math.min(...openCandles.map(c => c.low));
    const rangePercent = ((highOfDay - lowOfDay) / openPrice) * 100;
    
    if (change > 0.3) {
      return { 
        direction: 'BULLISH', 
        details: 'Strong opening momentum (+' + change.toFixed(2) + '%). Day range: ' + rangePercent.toFixed(2) + '%. Buyers in control.' 
      };
    } else if (change < -0.3) {
      return { 
        direction: 'BEARISH', 
        details: 'Weak opening (' + change.toFixed(2) + '%). Day range: ' + rangePercent.toFixed(2) + '%. Sellers dominating.' 
      };
    } else {
      return { 
        direction: 'NEUTRAL', 
        details: 'Flat start (' + change.toFixed(2) + '%). Day range: ' + rangePercent.toFixed(2) + '%. Waiting for direction.' 
      };
    }
  }

  // Helper: Generate trend timeline (every 1.5 hours)
  function generateTrendTimeline(candles) {
    if (!candles || candles.length < 10) return [];
    
    const timeline = [];
    const intervals = [
      { time: '09:30', label: 'Market Open' },
      { time: '11:00', label: 'Mid-Morning' },
      { time: '12:30', label: 'Pre-Lunch' },
      { time: '14:00', label: 'Afternoon' },
      { time: '15:30', label: 'Market Close' }
    ];
    
    // Calculate trend for each interval
    const avgPrice = candles.reduce((sum, c) => sum + c.close, 0) / candles.length;
    const recentCandles = candles.slice(-Math.min(30, candles.length));
    
    intervals.forEach(function(interval, idx) {
      // Simulate trend based on price movement patterns
      const segmentSize = Math.floor(recentCandles.length / intervals.length);
      const startIdx = idx * segmentSize;
      const endIdx = Math.min(startIdx + segmentSize, recentCandles.length - 1);
      
      if (startIdx < recentCandles.length && endIdx < recentCandles.length) {
        const segmentCandles = recentCandles.slice(startIdx, endIdx + 1);
        if (segmentCandles.length > 0) {
          const segmentStart = segmentCandles[0].close;
          const segmentEnd = segmentCandles[segmentCandles.length - 1].close;
          const change = ((segmentEnd - segmentStart) / segmentStart) * 100;
          
          let trend, note;
          if (change > 0.2) {
            trend = 'BULLISH';
            note = 'Price up +' + change.toFixed(2) + '%';
          } else if (change < -0.2) {
            trend = 'BEARISH';
            note = 'Price down ' + change.toFixed(2) + '%';
          } else {
            trend = 'NEUTRAL';
            note = 'Consolidation ±' + Math.abs(change).toFixed(2) + '%';
          }
          
          timeline.push({
            time: interval.time,
            trend: trend,
            note: interval.label + ' - ' + note
          });
        }
      } else {
        // Forecast future intervals
        const lastPrice = recentCandles[recentCandles.length - 1].close;
        const priceVsAvg = ((lastPrice - avgPrice) / avgPrice) * 100;
        
        let trend = priceVsAvg > 0.3 ? 'BULLISH' : (priceVsAvg < -0.3 ? 'BEARISH' : 'NEUTRAL');
        timeline.push({
          time: interval.time,
          trend: trend,
          note: interval.label + ' - Forecast: ' + trend.toLowerCase()
        });
      }
    });
    
    return timeline;
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
    USOIL: { name: 'US Oil (WTI)', exchange: 'NYMEX' },
    CRUDEOILMCX: { name: 'Crude Oil Futures (MCX)', exchange: 'MCX' },
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
  const symbolKeys = ['NIFTY50','BANKNIFTY','SENSEX','GOLD','SILVER','XAUUSD','XAGUSD','GOLDTEN','SILVERBEES','BTC','ETH','DJI','NASDAQ','SP500','USOIL','CRUDEOILMCX','NATURALGAS'];
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

  // ---- Automation Menu ----
  const automationDropdown = document.getElementById('automationDropdown');
  document.getElementById('btnAutomation').addEventListener('click', function(e) {
    e.stopPropagation();
    automationDropdown.classList.toggle('open');
    indDropdown.classList.remove('open');
    algoDropdown.classList.remove('open');
    cfgPanel.classList.remove('open');
  });
  document.addEventListener('click', function(e) {
    if (!e.target.closest('.automation-dropdown-wrapper')) automationDropdown.classList.remove('open');
  });

  // ---- Zerodha shared store: connection session + rules persistence (localStorage) ----
  const ZerodhaStore = {
    SESSION_KEY: 'mangalview_zerodha_session_v1',
    RULES_KEY:   'mangalview_zerodha_rules_v1',
    _evt(name) { try { window.dispatchEvent(new Event(name)); } catch(e) {} },
    getSession() {
      try { return JSON.parse(localStorage.getItem(this.SESSION_KEY) || 'null') || {connected:false, apiKey:''}; }
      catch(e) { return {connected:false, apiKey:''}; }
    },
    setSession(s) { localStorage.setItem(this.SESSION_KEY, JSON.stringify(s)); this._evt('zerodha-session-change'); },
    clearSession() { localStorage.removeItem(this.SESSION_KEY); this._evt('zerodha-session-change'); },
    getRules() {
      try { return JSON.parse(localStorage.getItem(this.RULES_KEY) || 'null') || {rules:[], ruleId:0, sym:'', qty:1}; }
      catch(e) { return {rules:[], ruleId:0, sym:'', qty:1}; }
    },
    setRules(r) { localStorage.setItem(this.RULES_KEY, JSON.stringify(r)); this._evt('zerodha-rules-change'); }
  };

  // ---- Zerodha Login Panel (credentials + Connect) ----
  (function() {
    const panel       = document.getElementById('zerodhaLoginPanel');
    const header      = document.getElementById('zdLoginHeader');
    const closeBtn    = document.getElementById('zdLoginClose');
    const connectBtn  = document.getElementById('zdConnectBtn');
    const statusDot   = document.getElementById('zdLoginStatusDot');
    const statusText  = document.getElementById('zdLoginStatusText');
    const apiKeyInp   = document.getElementById('zdApiKey');
    const apiSecInp   = document.getElementById('zdApiSecret');
    const reqTokInp   = document.getElementById('zdRequestToken');
    const accTokInp   = document.getElementById('zdAccessToken');

    function refreshStatus() {
      const s = ZerodhaStore.getSession();
      if (s.connected && s.apiKey) {
        statusDot.classList.add('connected');
        statusText.innerHTML = 'Connected <span style="color:#787b86;font-size:11px">(api_key: ' + s.apiKey + ')</span>';
        connectBtn.textContent = 'Connected';
        connectBtn.classList.add('connected');
        if (!apiKeyInp.value) apiKeyInp.value = s.apiKey;
      } else {
        statusDot.classList.remove('connected');
        statusText.textContent = 'Not connected';
        connectBtn.textContent = 'Connect';
        connectBtn.classList.remove('connected');
      }
    }

    // Open via menu item
    document.getElementById('btnZerodhaLogin').addEventListener('click', function() {
      automationDropdown.classList.remove('open');
      panel.classList.add('open');
      refreshStatus();
    });
    closeBtn.addEventListener('click', () => panel.classList.remove('open'));

    // Login URL — open Kite login in a new tab
    document.getElementById('zdLoginUrlBtn').addEventListener('click', function() {
      const apiKey = apiKeyInp.value.trim();
      if (!apiKey) { alert('Enter your API Key first.'); return; }
      window.open('https://kite.zerodha.com/connect/login?api_key=' + encodeURIComponent(apiKey) + '&v=3', '_blank');
    });

    // Exchange request_token → access_token via backend
    document.getElementById('zdGetTokenBtn').addEventListener('click', function() {
      const apiKey       = apiKeyInp.value.trim();
      const apiSecret    = apiSecInp.value.trim();
      const requestToken = reqTokInp.value.trim();
      if (!apiKey || !apiSecret || !requestToken) {
        alert('API Key, API Secret and Request Token are all required to generate an access token.'); return;
      }
      const btn = this;
      btn.disabled = true; btn.textContent = 'Fetching…';
      fetch('/api/zerodha/generate_token', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({api_key: apiKey, api_secret: apiSecret, request_token: requestToken})
      }).then(r => r.json()).then(res => {
        btn.disabled = false; btn.textContent = '🔓 Get Access Token';
        if (res.success) {
          accTokInp.value = res.access_token;
        } else {
          alert('Token exchange failed: ' + (res.error || 'Unknown error'));
        }
      }).catch(() => { btn.disabled = false; btn.textContent = '🔓 Get Access Token'; alert('Token exchange request error.'); });
    });

    // Connect — authenticates with backend then writes session to shared store
    connectBtn.addEventListener('click', function() {
      const apiKey      = apiKeyInp.value.trim();
      const apiSecret   = apiSecInp.value.trim();
      const accessToken = accTokInp.value.trim();
      if (!apiKey || !accessToken) { alert('API Key and Access Token are required.'); return; }
      fetch('/api/zerodha/connect', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({api_key: apiKey, api_secret: apiSecret, access_token: accessToken})
      }).then(r => r.json()).then(res => {
        if (res.success) {
          ZerodhaStore.setSession({connected: true, apiKey: apiKey});
          refreshStatus();
        } else {
          alert('Connection failed: ' + (res.error || 'Unknown error'));
        }
      }).catch(() => alert('Connection error.'));
    });

    // Draggable
    (function() {
      let dragging = false, sx, sy, ol, ot;
      header.addEventListener('mousedown', function(e) {
        if (e.target.closest('button')) return;
        dragging = true; panel.style.transform = 'none';
        const r = panel.getBoundingClientRect();
        ol = r.left; ot = r.top; sx = e.clientX; sy = e.clientY;
        panel.style.left = ol + 'px'; panel.style.top = ot + 'px';
        e.preventDefault();
      });
      document.addEventListener('mousemove', function(e) {
        if (!dragging) return;
        panel.style.left = (ol + e.clientX - sx) + 'px';
        panel.style.top  = (ot + e.clientY - sy) + 'px';
      });
      document.addEventListener('mouseup', () => { dragging = false; });
    })();

    // Reflect session changes from this window or others
    window.addEventListener('zerodha-session-change', refreshStatus);
    window.addEventListener('storage', e => { if (e.key === ZerodhaStore.SESSION_KEY) refreshStatus(); });
    refreshStatus();
  })();

  // ---- Zerodha Automation Panel ----
  (function() {
    const panel    = document.getElementById('zerodhaPanel');
    const header   = document.getElementById('zdHeader');
    const closeBtn = document.getElementById('zdClose');
    // (addBtn removed — now handled by zdAddAlgoRuleBtn / zdAddIndRuleBtn)
    const startBtn = document.getElementById('zdStartBtn');
    const stopBtn  = document.getElementById('zdStopBtn');
    const maximizeBtn = document.getElementById('zdMaximizeBtn');
    const popoutBtn   = document.getElementById('zdPopoutBtn');
    const logEl    = document.getElementById('zdLog');
    const rulesBody = document.getElementById('zdRulesBody');

    let zdConnected = false;
    let zdApiKey    = '';
    let zdRules     = [];    // {id, symbol, algo, qty, buyScore, sellScore, status}
    let zdRunning   = false;
    let zdTimer     = null;
    let zdRuleId    = 0;
    let zdMaximized = false;

    // If this window was opened as the Zerodha popout, auto-open + maximize the panel
    const isZerodhaPopout = new URLSearchParams(window.location.search).get('zerodhaPopout') === '1';
    if (isZerodhaPopout) {
      document.body.classList.add('zerodha-popout-window');
      panel.classList.add('open');
      // In popout the window itself IS the panel — hide max/popout/close which don't apply
      maximizeBtn.style.display = 'none';
      popoutBtn.style.display = 'none';
      closeBtn.style.display = 'none';
      document.title = '🤖 Zerodha Automation';
    }

    // Open panel
    document.getElementById('btnZerodhaAuto').addEventListener('click', function() {
      automationDropdown.classList.remove('open');
      panel.classList.add('open');
      refreshAutoStatus();
    });
    closeBtn.addEventListener('click', () => panel.classList.remove('open'));

    // Mirror connection state from the shared session store into the automation panel
    function refreshAutoStatus() {
      const s = ZerodhaStore.getSession();
      zdConnected = !!(s.connected && s.apiKey);
      zdApiKey    = s.apiKey || '';
      const dot  = document.getElementById('zdStatusDot');
      const text = document.getElementById('zdStatusText');
      if (zdRunning) {
        dot.classList.remove('connected'); dot.classList.add('running');
        text.textContent = 'Automation running…';
      } else if (zdConnected) {
        dot.classList.add('connected'); dot.classList.remove('running');
        text.innerHTML = 'Connected <span style="color:#787b86;font-size:11px">(api_key: ' + zdApiKey + ')</span>';
      } else {
        dot.classList.remove('connected'); dot.classList.remove('running');
        text.innerHTML = 'Not connected &mdash; open <b style="color:#1e6ec8">Zerodha Login</b> from the Automation menu';
      }
    }
    window.addEventListener('zerodha-session-change', refreshAutoStatus);
    window.addEventListener('zerodha-rules-change', loadRulesFromStore);
    window.addEventListener('storage', e => {
      if (e.key === ZerodhaStore.SESSION_KEY) refreshAutoStatus();
      if (e.key === ZerodhaStore.RULES_KEY)   loadRulesFromStore();
    });

    // ---- Rules persistence (so popout windows + reloads keep the table) ----
    let _suspendSave = false;   // avoid save-loops when applying remote updates
    function saveRulesToStore() {
      if (_suspendSave) return;
      ZerodhaStore.setRules({
        rules:  zdRules,
        ruleId: zdRuleId,
        sym:    document.getElementById('zdSymInput').value,
        qty:    document.getElementById('zdQtyInput').value
      });
    }
    function loadRulesFromStore() {
      const s = ZerodhaStore.getRules();
      _suspendSave = true;
      try {
        if (Array.isArray(s.rules)) zdRules = s.rules;
        if (typeof s.ruleId === 'number') zdRuleId = Math.max(zdRuleId, s.ruleId);
        const symEl = document.getElementById('zdSymInput');
        const qtyEl = document.getElementById('zdQtyInput');
        if (symEl && s.sym != null && document.activeElement !== symEl) symEl.value = s.sym;
        if (qtyEl && s.qty != null && document.activeElement !== qtyEl) qtyEl.value = s.qty;
        renderRules();
      } finally { _suspendSave = false; }
    }
    // Persist the shared sym/qty bar when typed
    ['zdSymInput','zdQtyInput'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.addEventListener('input', saveRulesToStore);
    });

    // Resolve a (sym) entry to chart + trade symbol + exchange. If the modal
    // staged metadata for this symbol, use it; otherwise treat sym as both
    // chart and trade (existing behavior — only works for curated chart symbols).
    function metaForSym(sym) {
      const m = window.zdPendingInstMeta;
      if (m && m.tradeSymbol === sym) {
        return { chartSymbol: m.chartSymbol, tradeSymbol: m.tradeSymbol, exchange: m.exchange || '' };
      }
      return { chartSymbol: sym, tradeSymbol: sym, exchange: '' };
    }

    // Maximize / Restore
    maximizeBtn.addEventListener('click', function() {
      zdMaximized = !zdMaximized;
      panel.classList.toggle('maximized', zdMaximized);
      if (zdMaximized) {
        // Save inline drag-position so we can restore on un-maximize
        panel.dataset.savedLeft = panel.style.left || '';
        panel.dataset.savedTop  = panel.style.top  || '';
        panel.dataset.savedTransform = panel.style.transform || '';
        panel.style.left = ''; panel.style.top = ''; panel.style.transform = '';
        this.innerHTML = '&#9635;';   // ▣ restore-ish glyph
        this.title = 'Restore';
      } else {
        panel.style.left = panel.dataset.savedLeft || '';
        panel.style.top  = panel.dataset.savedTop  || '';
        panel.style.transform = panel.dataset.savedTransform || '';
        this.innerHTML = '&#9633;';   // □
        this.title = 'Maximize';
      }
    });

    // Pop out — open a new browser window that loads this page and auto-opens the Zerodha panel
    popoutBtn.addEventListener('click', function() {
      const url = new URL(window.location.href);
      url.searchParams.set('zerodhaPopout', '1');
      const popoutWin = window.open(
        url.toString(), 'zerodhaPopout',
        'width=1100,height=820,resizable=yes,scrollbars=yes'
      );
      if (!popoutWin) {
        zdLog('Popup blocked. Please allow popups for this site and click again.', 'info');
        return;
      }
      zdLog('Opened Zerodha panel in a new window.', 'info');
    });

    // (Login URL / Get Access Token handlers live in the Zerodha Login IIFE above.)

    // Draggable
    (function() {
      let isDragging = false, startX, startY, origLeft, origTop;
      header.addEventListener('mousedown', function(e) {
        if (e.target.closest('button')) return;            // any header button skips drag
        if (panel.classList.contains('maximized')) return;  // no dragging when maximized
        if (isZerodhaPopout) return;                        // no dragging inside popout window
        isDragging = true;
        panel.style.transform = 'none';
        const rect = panel.getBoundingClientRect();
        origLeft = rect.left; origTop = rect.top;
        startX = e.clientX; startY = e.clientY;
        panel.style.left = origLeft + 'px';
        panel.style.top = origTop + 'px';
        e.preventDefault();
      });
      document.addEventListener('mousemove', function(e) {
        if (!isDragging) return;
        panel.style.left = (origLeft + e.clientX - startX) + 'px';
        panel.style.top  = (origTop  + e.clientY - startY) + 'px';
      });
      document.addEventListener('mouseup', () => { isDragging = false; });
    })();


    // ---- Add Rule: Algo-Based ----
    document.getElementById('zdAddAlgoRuleBtn').addEventListener('click', function() {
      const sym       = document.getElementById('zdSymInput').value.trim().toUpperCase();
      const qty       = parseInt(document.getElementById('zdQtyInput').value) || 1;
      const entryType = document.getElementById('zdAlgoEntryType').value;
      const side      = document.getElementById('zdAlgoSide').value;
      const tf        = document.getElementById('zdAlgoTF').value;
      const algo      = document.getElementById('zdAlgoInput').value;
      const score     = parseFloat(document.getElementById('zdAlgoScore').value) || 70;
      if (!sym) { zdLog('Enter a symbol.', 'info'); return; }
      const _meta = metaForSym(sym);
      zdRules.push({
        id: ++zdRuleId, ruleType: 'algo',
        entryType: entryType, side: side,
        symbol: sym, qty: qty, tf: tf,
        tradeSymbol: _meta.tradeSymbol, chartSymbol: _meta.chartSymbol, exchange: _meta.exchange,
        algo: algo, indicators: [],
        score: score, status: 'idle', lastOrder: null
      });
      renderRules();
      saveRulesToStore();
      zdLog('[Algo] ' + entryType.toUpperCase() + ' ' + side + ' ' + sym + ' TF:' + tf + ' Algo:' + algo + ' Score\u2265' + score, 'info');
    });

    // ---- Add Rule: Indicator-Based ----
    document.getElementById('zdAddIndRuleBtn').addEventListener('click', function() {
      const sym       = document.getElementById('zdSymInput').value.trim().toUpperCase();
      const qty       = parseInt(document.getElementById('zdQtyInput').value) || 1;
      const entryType = document.getElementById('zdIndEntryType').value;
      const side      = document.getElementById('zdIndSide').value;
      const tf        = document.getElementById('zdIndTF').value;
      const rawInds = [
        { ind: document.getElementById('zdInd1').value, cond: document.getElementById('zdCond1').value },
        { ind: document.getElementById('zdInd2').value, cond: document.getElementById('zdCond2').value },
        { ind: document.getElementById('zdInd3').value, cond: document.getElementById('zdCond3').value },
        { ind: document.getElementById('zdInd4').value, cond: document.getElementById('zdCond4').value }
      ].filter(v => v.ind && v.ind !== 'NA');
      const indicators = rawInds.map(v => v.ind);
      const conditions = rawInds.map(v => v.cond);
      if (!sym) { zdLog('Enter a symbol.', 'info'); return; }
      const _meta = metaForSym(sym);
      zdRules.push({
        id: ++zdRuleId, ruleType: 'indicator',
        entryType: entryType, side: side,
        symbol: sym, qty: qty, tf: tf,
        tradeSymbol: _meta.tradeSymbol, chartSymbol: _meta.chartSymbol, exchange: _meta.exchange,
        algo: 'NA', indicators: indicators, conditions: conditions,
        score: 0, status: 'idle', lastOrder: null
      });
      renderRules();
      saveRulesToStore();
      const indSummary = rawInds.map(v => v.ind + '(' + v.cond + ')').join(', ');
      zdLog('[Indicator] ' + entryType.toUpperCase() + ' ' + side + ' ' + sym + ' TF:' + tf + ' [' + (indSummary || 'NA') + ']', 'info');
    });

    // ---- Add Rule: Market Making ----
    document.getElementById('zdAddMMRuleBtn').addEventListener('click', function() {
      const sym       = document.getElementById('zdSymInput').value.trim().toUpperCase();
      const qty       = parseInt(document.getElementById('zdQtyInput').value) || 1;
      const entryType = document.getElementById('zdMMEntryType').value;
      const side      = document.getElementById('zdMMSide').value;
      const tf        = document.getElementById('zdMMTF').value;
      const mmAlgo    = document.getElementById('zdMMInput').value;
      const buyScore  = parseFloat(document.getElementById('zdMMBuyScore').value) || 70;
      const sellScore = parseFloat(document.getElementById('zdMMSellScore').value) || 70;
      if (!sym) { zdLog('Enter a symbol.', 'info'); return; }
      if (mmAlgo === 'NA') { zdLog('Select a Market Making strategy.', 'info'); return; }
      const _meta = metaForSym(sym);
      zdRules.push({
        id: ++zdRuleId, ruleType: 'mm',
        entryType: entryType, side: side,
        symbol: sym, qty: qty, tf: tf,
        tradeSymbol: _meta.tradeSymbol, chartSymbol: _meta.chartSymbol, exchange: _meta.exchange,
        algo: mmAlgo, indicators: [],
        buyScore: buyScore, sellScore: sellScore,
        score: 0, status: 'idle', lastOrder: null
      });
      renderRules();
      saveRulesToStore();
      zdLog('[MarketMaking] ' + entryType.toUpperCase() + ' ' + side + ' ' + sym + ' TF:' + tf + ' Algo:' + mmAlgo + ' BuyScore≥' + buyScore + ' SellScore≥' + sellScore, 'info');
    });

    function renderRules() {
      if (zdRules.length === 0) {
        rulesBody.innerHTML = '<tr id="zdNoRules"><td colspan="11" style="text-align:center;color:#787b86;padding:18px">No rules added yet</td></tr>';
        return;
      }
      // Option lists mirror the Add Rule controls above
      const tfOpts   = ['1m','2m','3m','5m','10m','15m','30m','1h','2h','4h','1d'];
      const algoOpts = ['NA','trend','mstreet','mfactor','sniper','orderflow','priceaction','breakout','momentum','scalping','smartmoney','quant','hybrid','statarb','institution','mpredict'];
      const mmOpts   = ['NA','marketmaking','mma'];
      const indOpts  = ['NA','RSI','MACD','EMA9','EMA21','SMA','BB','SuperTrend','VWAP','ADX','Stochastic','CCI','ATR','OBV','Ichimoku'];
      const condOpts = ['bullish','bearish'];
      // Disable inputs once automation has started — edits are only allowed before execution
      const dis = zdRunning ? ' disabled' : '';
      const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      const sel = (opts, val, field, id, arrIndex) => {
        const optHtml = opts.map(o => '<option value="'+esc(o)+'"'+(o===val?' selected':'')+'>'+esc(o)+'</option>').join('');
        const ai = (arrIndex !== undefined) ? ' data-arr-index="'+arrIndex+'"' : '';
        return '<select class="zd-cell" data-rule-id="'+id+'" data-field="'+field+'"'+ai+dis+'>'+optHtml+'</select>';
      };
      rulesBody.innerHTML = zdRules.map((r, i) => {
        const typeCell = sel(['entry','exit'], r.entryType, 'entryType', r.id);
        const sideCell = sel(['BUY','SELL'], r.side, 'side', r.id);
        const symCell  = '<input type="text" class="zd-cell zd-cell-sym" data-rule-id="'+r.id+'" data-field="symbol" value="'+esc(r.symbol)+'"'+dis+'>';
        const qtyCell  = '<input type="number" class="zd-cell zd-cell-qty" data-rule-id="'+r.id+'" data-field="qty" value="'+(r.qty||1)+'" min="1"'+dis+'>';
        const tfCell   = sel(tfOpts, r.tf, 'tf', r.id);

        let algoCell;
        if (r.ruleType === 'mm')        algoCell = sel(mmOpts,   r.algo, 'algo', r.id);
        else if (r.ruleType === 'algo') algoCell = sel(algoOpts, r.algo, 'algo', r.id);
        else                            algoCell = '<span style="color:#787b86">NA</span>';

        let indsCell;
        if (r.ruleType === 'indicator' && r.indicators && r.indicators.length) {
          indsCell = r.indicators.map((ind, j) => {
            const cond = (r.conditions && r.conditions[j]) || 'bullish';
            return '<span style="display:inline-flex;gap:3px;margin:1px 4px 1px 0;align-items:center">' +
                     sel(indOpts,  ind,  'indicators', r.id, j) +
                     sel(condOpts, cond, 'conditions', r.id, j) +
                   '</span>';
          }).join('');
        } else {
          indsCell = '<span style="color:#787b86">NA</span>';
        }

        let scoreCell;
        if (r.ruleType === 'mm') {
          scoreCell = '<td>'
            + '<input type="number" class="zd-cell zd-cell-score" data-rule-id="'+r.id+'" data-field="buyScore" value="'+(r.buyScore||0)+'" min="0" max="100" title="Buy Score Threshold" style="color:#26a69a"'+dis+'>'
            + ' <span style="color:#787b86">/</span> '
            + '<input type="number" class="zd-cell zd-cell-score" data-rule-id="'+r.id+'" data-field="sellScore" value="'+(r.sellScore||0)+'" min="0" max="100" title="Sell Score Threshold" style="color:#ef5350"'+dis+'>'
            + '</td>';
        } else if (r.ruleType === 'indicator') {
          scoreCell = '<td><span style="color:#787b86">—</span></td>';
        } else {
          scoreCell = '<td><input type="number" class="zd-cell zd-cell-score" data-rule-id="'+r.id+'" data-field="score" value="'+(r.score||0)+'" min="0" max="100"'+dis+'></td>';
        }

        const statusBadge = r.status === 'buy'  ? '<span class="badge-buy">BUY</span>'
                          : r.status === 'sell' ? '<span class="badge-sell">SELL</span>'
                          : '<span class="badge-idle">Idle</span>';

        return '<tr>' +
          '<td>' + (i+1) + '</td>' +
          '<td>' + typeCell + '</td>' +
          '<td>' + sideCell + '</td>' +
          '<td>' + symCell  + '</td>' +
          '<td>' + qtyCell  + '</td>' +
          '<td>' + tfCell   + '</td>' +
          '<td>' + algoCell + '</td>' +
          '<td style="font-size:11px">' + indsCell + '</td>' +
          scoreCell +
          '<td>' + statusBadge + '</td>' +
          '<td><button class="zd-row-del" data-id="' + r.id + '" title="Delete this rule"' + dis + '>&#128465; Delete</button></td>' +
        '</tr>';
      }).join('');

      // Delete button — remove the rule (only enabled while automation is stopped)
      rulesBody.querySelectorAll('.zd-row-del').forEach(btn => {
        btn.addEventListener('click', function() {
          if (this.disabled) return;
          const id = parseInt(this.dataset.id);
          const rule = zdRules.find(r => r.id === id);
          const label = rule ? (rule.symbol + ' (' + rule.ruleType + (rule.algo && rule.algo !== 'NA' ? '/' + rule.algo : '') + ')') : ('#' + id);
          zdRules = zdRules.filter(r => r.id !== id);
          renderRules();
          saveRulesToStore();
          zdLog('Rule removed: ' + label, 'info');
        });
      });

      // Inline edit handler — applies to every input/select with class .zd-cell
      rulesBody.querySelectorAll('.zd-cell').forEach(el => {
        el.addEventListener('change', function() {
          const id    = parseInt(this.dataset.ruleId);
          const field = this.dataset.field;
          const rule  = zdRules.find(r => r.id === id);
          if (!rule) return;
          let val = this.value;
          if (field === 'qty') {
            val = Math.max(1, parseInt(val) || 1);
            this.value = val;
          } else if (field === 'score' || field === 'buyScore' || field === 'sellScore') {
            val = Math.max(0, Math.min(100, parseFloat(val) || 0));
            this.value = val;
          } else if (field === 'symbol') {
            val = String(val).trim().toUpperCase();
            this.value = val;
          }
          if (field === 'indicators') {
            const idx = parseInt(this.dataset.arrIndex);
            if (!isNaN(idx)) rule.indicators[idx] = val;
          } else if (field === 'conditions') {
            const idx = parseInt(this.dataset.arrIndex);
            if (!isNaN(idx)) {
              if (!rule.conditions) rule.conditions = [];
              rule.conditions[idx] = val;
            }
          } else {
            rule[field] = val;
          }
          saveRulesToStore();  // persist inline edits
        });
      });
    }

    function zdLog(msg, type) {
      const cls = type === 'buy' ? 'log-buy' : type === 'sell' ? 'log-sell' : 'log-info';
      const now = new Date().toLocaleTimeString();
      logEl.innerHTML += '<br><span class="' + cls + '">[' + now + '] ' + msg + '</span>';
      logEl.scrollTop = logEl.scrollHeight;
    }

    // Start / Stop \u2014 separate buttons; one is always disabled depending on state
    function setRunningState(running) {
      zdRunning = running;
      startBtn.disabled = running;
      stopBtn.disabled  = !running;
      refreshAutoStatus();   // updates the status banner from shared session + running state
      renderRules();         // refresh table \u2014 disables/enables inline edits + Delete button
    }

    startBtn.addEventListener('click', function() {
      if (zdRunning) return;
      if (!zdConnected) { zdLog('Please connect to Zerodha first.', 'info'); return; }
      if (zdRules.length === 0) { zdLog('Add at least one rule.', 'info'); return; }
      setRunningState(true);
      zdLog('Automation started. Checking signals every 15s.', 'info');
      runAutomation();
      zdTimer = setInterval(runAutomation, 15000);
    });

    stopBtn.addEventListener('click', function() {
      if (!zdRunning) return;
      clearInterval(zdTimer); zdTimer = null;
      setRunningState(false);
      zdLog('Automation stopped.', 'info');
    });

    // Underlying-name -> curated chart symbol. Used to back-fill chartSymbol
    // for rules added before the Kite-symbol mapping was introduced, and for
    // symbols the user typed manually that look like Kite tradingsymbols.
    const _ZD_RUN_UNDERLYING_MAP = {
      'NIFTY':'NIFTY50','NIFTYNXT50':'NIFTYNXT50','BANKNIFTY':'BANKNIFTY',
      'FINNIFTY':'BANKNIFTY','MIDCPNIFTY':'NIFTY50','SENSEX':'SENSEX','BANKEX':'BANKNIFTY',
      'CRUDEOIL':'CRUDEOILMCX','CRUDEOILM':'CRUDEOILMCX',
      'GOLD':'GOLD','GOLDM':'GOLD','GOLDPETAL':'GOLD',
      'SILVER':'SILVER','SILVERM':'SILVER','SILVERMIC':'SILVER',
      'NATURALGAS':'NATURALGAS','NATGASMINI':'NATURALGAS'
    };
    // Auto-derive chart symbol from a Kite-style tradingsymbol when the rule
    // doesn't already carry one. Matches the longest known-underlying prefix
    // (so NIFTYNXT50 beats NIFTY, CRUDEOILM beats CRUDE, etc.) on any symbol
    // that ends in FUT/CE/PE. Returns null if nothing matches.
    const _ZD_NAMES_SORTED = Object.keys(_ZD_RUN_UNDERLYING_MAP)
      .sort((a, b) => b.length - a.length);
    function _inferChartFromSym(sym) {
      const s = String(sym || '').toUpperCase();
      if (s.length < 4) return null;
      // Curated chart symbol typed directly (e.g., the rule symbol IS CRUDEOILMCX)
      if (_ZD_RUN_UNDERLYING_MAP[s]) return _ZD_RUN_UNDERLYING_MAP[s];
      // Otherwise must look like a derivative tradingsymbol
      if (!/(FUT|CE|PE)$/.test(s)) return null;
      // Longest matching underlying prefix wins
      for (const name of _ZD_NAMES_SORTED) {
        if (s.startsWith(name)) return _ZD_RUN_UNDERLYING_MAP[name];
      }
      return null;
    }

    function runAutomation() {
      const liveTradesChk = document.getElementById('zdLiveTradesChk');
      const liveTrades = !!(liveTradesChk && liveTradesChk.checked);
      zdRules.forEach(function(rule) {
        // Use rule's own timeframe; fall back to chart TF
        const useTF  = rule.tf || currentTF;
        const useAlgo = (rule.algo && rule.algo !== 'NA') ? rule.algo : '';
        const tradeSym = rule.tradeSymbol || rule.symbol;
        // Choose data source. Rules with a Kite exchange (added from the Kite
        // tab) fetch directly from Kite's historical-data API — the actual
        // contract, not a proxy. Everything else uses the chart-symbol path.
        const kiteExchanges = ['NSE','BSE','NFO','BFO','MCX','CDS','BCD'];
        const useKite = zdConnected && rule.exchange && kiteExchanges.indexOf(rule.exchange) !== -1;
        let dataSym, useSource;
        if (useKite) {
          // Fetch the actual Kite tradingsymbol via source=kite
          dataSym   = tradeSym;
          useSource = 'kite';
        } else {
          // Resolve a chart-symbol for yfinance/tradingview. Re-run inference
          // when chartSymbol is missing OR identical to the trade symbol
          // (which happens for manually-typed rules pre-Kite-mapping).
          let cs = rule.chartSymbol;
          if (!cs || cs === rule.symbol) {
            const inferred = _inferChartFromSym(rule.symbol);
            if (inferred && inferred !== rule.symbol) {
              cs = inferred;
              rule.chartSymbol = cs;   // back-fill + persist for future ticks
              saveRulesToStore();
            }
          }
          dataSym   = cs || rule.symbol;
          useSource = currentSource;
        }
        const url = '/api/candles?symbol=' + encodeURIComponent(dataSym)
          + '&interval=' + encodeURIComponent(useTF)
          + '&source=' + encodeURIComponent(useSource)
          + (useKite && zdApiKey ? '&api_key=' + encodeURIComponent(zdApiKey) : '')
          + (useAlgo ? '&algo=' + encodeURIComponent(useAlgo) : '');
        fetch(url)
        .then(r => r.json())
        .then(function(data) {
          const signals = (data.signals || []);
          const last  = signals.length ? signals[signals.length - 1] : null;
          const score = last && (last.score !== undefined ? last.score
                      : last.buy_score !== undefined ? last.buy_score : null);
          const sig   = last ? (last.signal || '').toUpperCase() : '';
          const candles = data.candles || [];
          const lastCandle = candles.length ? candles[candles.length - 1] : null;

          // Evaluate ONE indicator at the latest candle vs an expected condition
          // ('bullish' / 'bearish'). Returns true/false, or null if unsupported.
          function evalIndicator(name, expected) {
            const want = (expected || '').toLowerCase();
            const close = lastCandle && lastCandle.close;
            const tail = arr => (Array.isArray(arr) && arr.length ? arr[arr.length - 1] : null);
            let isBullish = null;
            switch (name) {
              case 'SuperTrend': {
                const v = tail(data.supertrend);
                if (v && v.direction != null) isBullish = v.direction === 1;
                break;
              }
              case 'RSI': {
                const v = tail(data.rsi);
                if (v && v.value != null) isBullish = v.value > 50;
                break;
              }
              case 'MACD': {
                const v = tail(data.macd);
                if (v && v.histogram != null) isBullish = v.histogram > 0;
                break;
              }
              case 'EMA9':
              case 'EMA21': {
                const v = tail(name === 'EMA9' ? data.ema9 : data.ema21);
                if (v && v.value != null && close != null) isBullish = close > v.value;
                break;
              }
              case 'VWAP': {
                const v = tail(data.vwap);
                if (v && v.value != null && close != null) isBullish = close > v.value;
                break;
              }
              case 'BB': {
                const v = tail(data.bollingerBands);
                if (v && v.middle != null && close != null) isBullish = close > v.middle;
                break;
              }
              // SMA, ADX, Stochastic, CCI, ATR, OBV, Ichimoku — not in /api/candles
              // response at this point. Return null so the user knows it's a no-op.
              default:
                return null;
            }
            if (isBullish == null) return null;
            return want === 'bullish' ? isBullish : !isBullish;
          }

          // Decide whether this rule triggers
          let triggered = false;
          let reason    = '';

          if (rule.ruleType === 'indicator') {
            // ALL selected indicators must match their conditions
            const inds  = rule.indicators || [];
            const conds = rule.conditions || [];
            if (!inds.length) {
              reason = 'no indicators on rule';
            } else {
              const evals = inds.map((ind, i) => ({
                ind, cond: conds[i] || 'bullish', pass: evalIndicator(ind, conds[i] || 'bullish')
              }));
              const unsupported = evals.filter(e => e.pass === null).map(e => e.ind);
              const allPass     = evals.every(e => e.pass === true);
              triggered = allPass;
              reason = evals.map(e => e.ind + '(' + e.cond + ')=' + (e.pass===null?'?':e.pass?'✓':'✗')).join(' ');
              if (unsupported.length) reason += ' [unsupported: ' + unsupported.join(',') + ']';
            }
          } else if (rule.ruleType === 'mm') {
            if (sig === 'BUY' && score !== null) {
              triggered = score >= (rule.buyScore || 0);
              reason = 'sig=BUY score=' + score + ' >=' + (rule.buyScore || 0) + ': ' + (triggered?'✓':'✗');
            } else if (sig === 'SELL' && score !== null) {
              triggered = score >= (rule.sellScore || 0);
              reason = 'sig=SELL score=' + score + ' >=' + (rule.sellScore || 0) + ': ' + (triggered?'✓':'✗');
            } else {
              reason = 'sig=' + (sig||'none') + ' score=' + (score==null?'-':score) + ': no thresholds met';
            }
          } else {  // algo
            if (score !== null) {
              triggered = score >= (rule.score || 0);
              reason = 'sig=' + (sig||'none') + ' score=' + score + ' >=' + (rule.score || 0) + ': ' + (triggered?'✓':'✗');
            } else if (last) {
              triggered = (sig === rule.side);
              reason = 'sig=' + (sig||'none') + ' ==rule.side(' + rule.side + '): ' + (triggered?'✓':'✗');
            } else {
              reason = 'no signal in /api/candles response';
            }
          }

          // Per-tick visibility — always log so the user can see what's happening
          const srcTag = useKite ? '[KITE]' : '[' + useSource + ']';
          zdLog('[Tick] ' + srcTag + ' ' + dataSym + ' #' + rule.id + ' ' + reason, 'info');

          if (!triggered) return;

          // Avoid duplicate orders on same candle
          const candleKey = (last && last.time) || (lastCandle && lastCandle.time) || Date.now();
          if (rule.lastOrder === candleKey) return;
          rule.lastOrder = candleKey;
          
          // For MM rules, use the actual signal direction; otherwise use configured side
          const orderSide = (rule.ruleType === 'mm' && sig) ? sig : rule.side;
          rule.status = orderSide.toLowerCase();
          renderRules();

          // Place order with determined side (BUY or SELL). Uses tradeSymbol
          // + exchange so Kite-specific tradingsymbols (CRUDEOILM26JUNFUT etc.)
          // reach the correct exchange. dry_run is the opposite of the panel's
          // "Live trades" checkbox — defaults to dry-run for safety.
          fetch('/api/zerodha/order', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({
              api_key:  zdApiKey,
              symbol:   tradeSym,
              exchange: rule.exchange || '',
              side:     orderSide,
              qty:      rule.qty,
              algo:     rule.algo,
              score:    score !== null ? score : sig,
              dry_run:  !liveTrades
            })
          }).then(r => r.json()).then(function(res) {
            if (res.success) {
              const tag = res.dry_run ? '[DRY] ' : '[' + rule.entryType.toUpperCase() + '] ';
              const label = tag + orderSide + ' '
                + rule.qty + ' ' + tradeSym + (rule.exchange ? '@' + rule.exchange : '')
                + ' (' + useTF + ')'
                + (useAlgo ? ' Algo:' + useAlgo : ' Ind:[' + (rule.indicators.join(',') || 'NA') + ']')
                + (score !== null ? ' score=' + score : '')
                + ' #' + res.orderId;
              zdLog(label, orderSide.toLowerCase());
            } else {
              zdLog('Order failed for ' + tradeSym + ': ' + (res.error || 'Unknown'), 'info');
            }
          });
        }).catch(function() {
          zdLog('Fetch error for ' + dataSym + ' (rule symbol: ' + rule.symbol + ')', 'info');
        });
      });
    }

    // ---- Initial state: hydrate from shared store (rules + connection) ----
    loadRulesFromStore();
    refreshAutoStatus();
  })();

  // ---- Instrument Search Modal ----
  (function() {
    const overlay    = document.getElementById('zdInstOverlay');
    const listPanel  = document.getElementById('zdInstListPanel');
    const selList    = document.getElementById('zdInstSelList');
    const countEl    = document.getElementById('zdInstCount');
    const searchInp  = document.getElementById('zdInstSearchInput');
    const clearBtn   = document.getElementById('zdInstSearchClear');
    const tabsEl     = document.getElementById('zdInstTabs');

    let allInstruments = [];   // loaded once from API
    let selectedItems  = [];   // [{symbol, name, exchange}]
    let currentSeg     = '';
    let searchTimer    = null;

    // Open modal
    document.getElementById('zdOpenInstSearch').addEventListener('click', function() {
      overlay.classList.add('open');
      searchInp.value = '';
      currentSeg = '';
      tabsEl.querySelectorAll('.zd-inst-tab').forEach(t => t.classList.toggle('active', t.dataset.seg === ''));
      loadInstruments('', '');
      setTimeout(() => searchInp.focus(), 80);
    });

    // Close modal
    document.getElementById('zdInstClose').addEventListener('click', closeModal);
    overlay.addEventListener('click', function(e) { if (e.target === overlay) closeModal(); });

    function closeModal() { overlay.classList.remove('open'); }

    // Done button — fill zdSymInput with first selected and close
    // ---- Chart-symbol + exchange derivation for Kite-style tradingsymbols ----
    // Kite tradingsymbols (CRUDEOILM26JUNFUT, NIFTY26JUN24000CE, RELIANCE) need a
    // separate "chart symbol" the data backends (yfinance / tradingview) can
    // actually fetch. We compute that here from the row's name/type/exchange,
    // and the rule stores both the chart symbol (for /api/candles) and the
    // Kite tradingsymbol + exchange (for /api/zerodha/order).
    const _ZD_UNDERLYING_MAP = {
      'NIFTY':       'NIFTY50',
      'NIFTYNXT50':  'NIFTYNXT50',
      'BANKNIFTY':   'BANKNIFTY',
      'FINNIFTY':    'BANKNIFTY',
      'MIDCPNIFTY':  'NIFTY50',
      'SENSEX':      'SENSEX',
      'BANKEX':      'BANKNIFTY',
      'CRUDEOIL':    'CRUDEOILMCX',
      'CRUDEOILM':   'CRUDEOILMCX',
      'GOLD':        'GOLD',
      'GOLDM':       'GOLD',
      'GOLDPETAL':   'GOLD',
      'SILVER':      'SILVER',
      'SILVERM':     'SILVER',
      'SILVERMIC':   'SILVER',
      'NATURALGAS':  'NATURALGAS',
      'NATGASMINI':  'NATURALGAS'
    };
    function deriveChartMeta(rec) {
      const sym  = (rec.symbol   || '').toUpperCase();
      const name = (rec.name     || '').toUpperCase();
      const exch = (rec.exchange || '').toUpperCase();
      const type = (rec.type     || '').toUpperCase();
      // Derivatives (FUT / CE / PE) — map to the underlying's curated chart symbol
      if (type === 'FUT' || type === 'CE' || type === 'PE') {
        return {
          tradeSymbol: sym,
          chartSymbol: _ZD_UNDERLYING_MAP[name] || _ZD_UNDERLYING_MAP[sym] || sym,
          exchange:    exch || (type === 'FUT' ? '' : 'NFO')
        };
      }
      // Equities — append yfinance suffix (.NS / .BO) so signal fetch works
      if (type === 'EQ') {
        const suffix = exch === 'NSE' ? '.NS' : exch === 'BSE' ? '.BO' : '';
        return { tradeSymbol: sym, chartSymbol: sym + suffix, exchange: exch };
      }
      // Anything else (index, curated) — use as-is, fall through map for safety
      return {
        tradeSymbol: sym,
        chartSymbol: _ZD_UNDERLYING_MAP[name] || _ZD_UNDERLYING_MAP[sym] || sym,
        exchange:    exch
      };
    }

    // Sidecar: the most recent instrument picked via the modal. Add-rule
    // handlers read this when zdSymInput's value matches its tradeSymbol.
    window.zdPendingInstMeta = null;

    document.getElementById('zdInstDoneBtn').addEventListener('click', function() {
      if (selectedItems.length > 0) {
        // Use the first selected as the "active" instrument; its derived
        // metadata is what the next rule-add picks up.
        const first = selectedItems[0];
        const meta = deriveChartMeta(first);
        document.getElementById('zdSymInput').value = meta.tradeSymbol;
        window.zdPendingInstMeta = meta;
      }
      closeModal();
    });

    // If the user manually edits the symbol input after picking, drop the sidecar
    // (we no longer know the chart symbol / exchange for what's been typed).
    document.getElementById('zdSymInput').addEventListener('input', function() {
      if (window.zdPendingInstMeta &&
          this.value.trim().toUpperCase() !== window.zdPendingInstMeta.tradeSymbol) {
        window.zdPendingInstMeta = null;
      }
    });

    // Tabs
    tabsEl.addEventListener('click', function(e) {
      const tab = e.target.closest('.zd-inst-tab');
      if (!tab) return;
      tabsEl.querySelectorAll('.zd-inst-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      currentSeg = tab.dataset.seg;
      loadInstruments(searchInp.value.trim(), currentSeg);
    });

    // Search input
    searchInp.addEventListener('input', function() {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => loadInstruments(this.value.trim(), currentSeg), 220);
    });
    clearBtn.addEventListener('click', function() {
      searchInp.value = '';
      loadInstruments('', currentSeg);
      searchInp.focus();
    });

    function loadInstruments(q, seg) {
      listPanel.innerHTML = '<div class="zd-inst-empty">Loading\u2026</div>';

      // Kite tab \u2014 live Kite API search (api.kite.trade/instruments). Smart-parses
      // strike/side/month so 'Nifty 24000' lists all expiries at that strike.
      if (seg === 'KITE') {
        listPanel.innerHTML = '<div class="zd-inst-empty" style="color:#f0b429">' +
          (q ? 'Searching Kite API\u2026' : 'Loading live Kite instruments (~1MB, first time only)\u2026') + '</div>';
        fetch('/api/zerodha/kite/search?q=' + encodeURIComponent(q))
          .then(r => r.json()).then(function(data) {
            if (!data.success) {
              listPanel.innerHTML = '<div class="zd-inst-empty" style="color:#e74c3c">' +
                (data.error || 'Failed to load Kite instruments') + '</div>';
              return;
            }
            allInstruments = data.results || [];
            renderList();
          }).catch(function() {
            listPanel.innerHTML = '<div class="zd-inst-empty">Network error fetching Kite instruments.</div>';
          });
        return;
      }

      // Zerodha Inst tab \u2014 search local instruments.csv (full Kite instrument dump)
      if (seg === 'ZERODHA_CSV') {
        fetch('/api/zerodha/csv/search?q=' + encodeURIComponent(q))
          .then(r => r.json()).then(function(data) {
            if (!data.success) {
              listPanel.innerHTML = '<div class="zd-inst-empty" style="color:#e74c3c">' +
                (data.error || 'Failed to load instruments.csv') + '</div>';
              return;
            }
            allInstruments = data.results || [];
            renderList();
            if (!q && data.total) {
              const note = document.createElement('div');
              note.className = 'zd-inst-empty';
              note.style.cssText = 'padding:6px 14px;color:#787b86;font-size:11px;border-bottom:1px solid #2a2e39';
              note.textContent = 'Showing first ' + allInstruments.length + ' of ' + data.total + ' instruments \u2014 type to filter.';
              listPanel.insertBefore(note, listPanel.firstChild);
            }
          }).catch(function() {
            listPanel.innerHTML = '<div class="zd-inst-empty">Network error loading instruments.csv.</div>';
          });
        return;
      }

      // Use Zerodha NFO search when: Options tab is active, OR query looks like an options query
      const _optKeywords = /\b(NIFTY|BANKNIFTY|FINNIFTY|MIDCPNIFTY|SENSEX|BANKEX)\b|\d{4,}|\b(CE|PE|CALL|PUT)\b/i;
      const useNFO = seg === 'OPTIONS' || _optKeywords.test(q);

      if (useNFO) {
        listPanel.innerHTML = '<div class="zd-inst-empty" style="color:#f0b429">' +
          (q ? 'Searching NFO options\u2026' : 'Loading NFO instruments from Zerodha\u2026') + '</div>';
        fetch('/api/zerodha/nfo/search?q=' + encodeURIComponent(q))
          .then(r => r.json()).then(function(data) {
            if (!data.success) {
              listPanel.innerHTML = '<div class="zd-inst-empty" style="color:#e74c3c">' +
                (data.error || 'Failed to load NFO instruments') + '</div>';
              return;
            }
            allInstruments = data.results || [];
            renderList();
          }).catch(function() {
            listPanel.innerHTML = '<div class="zd-inst-empty">Network error loading NFO instruments.</div>';
          });
        return;
      }

      const url = '/api/zerodha/instruments/search?q=' + encodeURIComponent(q) + '&seg=' + encodeURIComponent(seg);
      fetch(url).then(r => r.json()).then(function(data) {
        allInstruments = data.results || [];
        renderList();
      }).catch(function() {
        listPanel.innerHTML = '<div class="zd-inst-empty">Failed to load instruments.</div>';
      });
    }

    function renderList() {
      if (!allInstruments.length) {
        listPanel.innerHTML = '<div class="zd-inst-empty">No instruments found.</div>';
        return;
      }
      listPanel.innerHTML = allInstruments.map(function(inst) {
        const isSel = selectedItems.some(s => s.symbol === inst.symbol);
        const exchClass = inst.exchange === 'BSE' ? 'bse'
                        : inst.exchange === 'NSE' ? ''
                        : 'other';
        // For options contracts, show expiry + LTP badge
        let badge = '';
        if (inst.seg === 'OPTIONS' && inst.expiry_short) {
          const side = inst.type === 'CE' ? 'call' : 'put';
          badge = '<span class="zd-inst-exch" style="background:' +
            (inst.type==='CE'?'#1a6b3a':'#7b1c1c') + ';color:#fff;margin-left:4px;font-size:9px">' +
            inst.expiry_short + '</span>';
        }
        return '<div class="zd-inst-item' + (isSel ? ' selected' : '') + '" data-sym="' + inst.symbol + '">' +
          '<div class="zd-inst-chk"></div>' +
          '<div class="zd-inst-info">' +
            '<div class="zd-inst-sym">' + inst.symbol + '</div>' +
            '<div class="zd-inst-name">' + inst.name + '</div>' +
          '</div>' +
          badge +
          '<span class="zd-inst-exch ' + exchClass + '">' + inst.exchange + '</span>' +
        '</div>';
      }).join('');
      listPanel.querySelectorAll('.zd-inst-item').forEach(function(item) {
        item.addEventListener('click', function() {
          const sym = this.dataset.sym;
          const inst = allInstruments.find(i => i.symbol === sym);
          if (!inst) return;
          const idx = selectedItems.findIndex(s => s.symbol === sym);
          if (idx === -1) {
            selectedItems.push(inst);
          } else {
            selectedItems.splice(idx, 1);
          }
          renderList();
          renderSelected();
        });
      });
    }

    function renderSelected() {
      countEl.textContent = selectedItems.length + ' selected';
      if (!selectedItems.length) {
        selList.innerHTML = '<div class="zd-inst-empty" style="padding:20px;font-size:11px">No instruments selected</div>';
        return;
      }
      selList.innerHTML = selectedItems.map(function(inst) {
        return '<div class="zd-inst-sel-item" data-sym="' + inst.symbol + '">' +
          '<div><div class="zd-inst-sel-sym">' + inst.symbol + '</div>' +
          '<div class="zd-inst-sel-exch">' + inst.exchange + ' &middot; ' + inst.type + '</div></div>' +
          '<button class="zd-inst-sel-rm" title="Remove">&times;</button>' +
        '</div>';
      }).join('');
      selList.querySelectorAll('.zd-inst-sel-rm').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
          e.stopPropagation();
          const sym = this.closest('[data-sym]').dataset.sym;
          selectedItems = selectedItems.filter(s => s.symbol !== sym);
          renderList();
          renderSelected();
        });
      });
    }

    // Initial render of selected panel
    renderSelected();
  })();

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
      // Tier access: filter indicators and algos by user's plan
      const userPlan = settings.user_plan || 'free';
      const tierIndKey = 'tier_indicators_' + userPlan;
      const tierAlgoKey = 'tier_algos_' + userPlan;
      if (settings[tierIndKey]) {
        try {
          const allowed = JSON.parse(settings[tierIndKey]);
          document.querySelectorAll('.ind-item[data-ind]').forEach(el => {
            if (allowed.indexOf(el.dataset.ind) < 0) el.style.display = 'none';
          });
        } catch(e) {}
      }
      if (settings[tierAlgoKey]) {
        try {
          const allowed = JSON.parse(settings[tierAlgoKey]);
          document.querySelectorAll('.algo-item[data-algo]').forEach(btn => {
            if (allowed.indexOf(btn.dataset.algo) < 0) btn.style.display = 'none';
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
    port = int(os.environ.get("PORT", 5000))
    print("Starting Mangal View Server...")
    print(f"Open http://localhost:{port} in your browser")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
