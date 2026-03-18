"""
SaaS Trading Bot Platform — SolTrader
Run: python app.py
Then open: http://localhost:5000

Required .env variables:
  SECRET_KEY, FERNET_KEY, HELIUS_RPC,
  STRIPE_SECRET_KEY, STRIPE_PRICE_BASIC, STRIPE_PRICE_PRO, ADMIN_EMAIL
"""

import atexit
import os, threading, time, base64, json, requests, base58, secrets, hashlib, random
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
import signal
import psycopg2
import psycopg2.extras
import psycopg2.pool
import string
from collections import deque
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo
from flask import Flask, request, redirect, url_for, session, jsonify, Response
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from cryptography.fernet import Fernet
import bcrypt
import stripe

from dotenv import load_dotenv

# ── Enhanced Trading Systems (Research Paper Implementation) ──────────────────
# These modules implement whale detection, risk management, MEV protection,
# and observability as recommended by the research paper
try:
    from whale_detection import get_whale_detection_system, WhaleDetectionSystem
    from risk_engine import get_risk_engine, RiskEngine, RiskLevel
    from mev_protection import get_mev_protection, MEVProtectionSystem, SubmissionStrategy
    from observability import get_observability, ObservabilitySystem, AlertSeverity
    from enhanced_trading import (
        EnhancedSignalEvaluator,
        EnhancedExecutionHandler,
        EnhancedPositionMonitor,
        EnhancedBotMixin,
        create_enhanced_systems,
    )
    ENHANCED_SYSTEMS_AVAILABLE = True
except ImportError as _import_err:
    print(f"[WARN] Enhanced trading systems not available: {_import_err}")
    ENHANCED_SYSTEMS_AVAILABLE = False


def load_environment():
    # Prefer the project-local .env and keep the legacy Desktop path as fallback.
    env_paths = [
        os.path.join(os.path.dirname(__file__), ".env"),
        os.path.join(os.path.expanduser("~"), "Desktop", ".env"),
    ]
    for env_path in env_paths:
        if os.path.exists(env_path):
            load_dotenv(dotenv_path=env_path, override=False)


load_environment()


def require_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} environment variable is required.")
    return value

# ── Config ─────────────────────────────────────────────────────────────────────
SECRET_KEY         = require_env("SECRET_KEY")
FERNET_KEY         = require_env("FERNET_KEY").encode()
HELIUS_RPC         = require_env("HELIUS_RPC")
STRIPE_SECRET       = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_PRICE_BASIC  = os.getenv("STRIPE_PRICE_BASIC", "")
STRIPE_PRICE_PRO    = os.getenv("STRIPE_PRICE_PRO", "")
STRIPE_PRICE_ELITE  = os.getenv("STRIPE_PRICE_ELITE", "")
ADMIN_EMAIL        = os.getenv("ADMIN_EMAIL", "admin@admin.com")
ADMIN_EMAILS       = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", ADMIN_EMAIL).split(",") if e.strip()}
PERF_FEE_FREE      = 0.25   # 25% of profits (profit-only plan, no subscription)
PERF_FEE_BASIC     = 0.15   # 15% of profits
PERF_FEE_PRO       = 0.10   # 10% of profits
PERF_FEE_ELITE     = 0.08   # 8% of profits
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
SENDGRID_API_KEY   = os.getenv("SENDGRID_API_KEY", "")
SMTP_FROM          = os.getenv("SMTP_FROM", "noreply@soltrader.app")
REFERRAL_COMMISSION = 0.10  # 10% of referred user's first month
FEE_WALLET          = os.getenv("FEE_WALLET", "")  # your SOL wallet to receive perf fees

fernet        = Fernet(FERNET_KEY)
stripe.api_key = STRIPE_SECRET
SOL_MINT      = "So11111111111111111111111111111111111111112"
PUMP_PROGRAM  = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
HEADERS       = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
SESSION_COOKIE_SECURE = os.getenv(
    "SESSION_COOKIE_SECURE",
    "1" if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("FLASK_ENV") == "production" else "0",
) == "1"
CENTRAL_TZ = ZoneInfo("America/Chicago")

RAYDIUM_AMM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
PUMP_FUN    = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# ── DexScreener rate limiter (token bucket) ───────────────────────────────────
class _RateLimiter:
    """Simple thread-safe token bucket rate limiter."""
    def __init__(self, rate_per_sec=15):
        self._rate = rate_per_sec
        self._tokens = rate_per_sec
        self._last = time.time()
        self._lock = threading.Lock()

    def acquire(self, timeout=5):
        deadline = time.time() + timeout
        while True:
            with self._lock:
                now = time.time()
                self._tokens = min(self._rate, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return True
            if time.time() >= deadline:
                return False
            time.sleep(0.05)

_dex_limiter = _RateLimiter(rate_per_sec=4)
_dex_backoff_until = 0  # epoch timestamp — skip requests until this time
_rpc_health = deque(maxlen=600)


def record_rpc_health(source, ok, latency_ms, method=""):
    _rpc_health.appendleft({
        "source": source,
        "ok": bool(ok),
        "latency_ms": int(latency_ms or 0),
        "method": method or "",
        "ts": time.time(),
    })


def rpc_health_snapshot(window_sec=900):
    now = time.time()
    rows = [row for row in list(_rpc_health) if now - float(row.get("ts") or 0) <= window_sec]
    if not rows:
        return {
            "total": 0,
            "ok": 0,
            "fail_rate": 0.0,
            "avg_ms": 0.0,
            "p95_ms": 0.0,
            "by_source": [],
        }
    latencies = sorted(int(row.get("latency_ms") or 0) for row in rows)
    idx = min(len(latencies) - 1, max(0, math.ceil(len(latencies) * 0.95) - 1))
    ok_count = sum(1 for row in rows if row.get("ok"))
    by_source = {}
    for row in rows:
        src = row.get("source") or "unknown"
        bucket = by_source.setdefault(src, {"source": src, "total": 0, "ok": 0, "latencies": []})
        bucket["total"] += 1
        if row.get("ok"):
            bucket["ok"] += 1
        bucket["latencies"].append(int(row.get("latency_ms") or 0))
    source_rows = []
    for src, bucket in by_source.items():
        lats = bucket.pop("latencies")
        bucket["avg_ms"] = round(sum(lats) / len(lats), 1) if lats else 0.0
        bucket["fail_rate"] = round((1 - (bucket["ok"] / bucket["total"])) * 100, 1) if bucket["total"] else 0.0
        source_rows.append(bucket)
    source_rows.sort(key=lambda row: (-row["fail_rate"], row["source"]))
    return {
        "total": len(rows),
        "ok": ok_count,
        "fail_rate": round((1 - (ok_count / len(rows))) * 100, 1) if rows else 0.0,
        "avg_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
        "p95_ms": float(latencies[idx]) if latencies else 0.0,
        "by_source": source_rows,
    }

def dex_get(url, **kwargs):
    """Rate-limited GET to DexScreener API with 429 backoff."""
    global _dex_backoff_until
    now = time.time()
    if now < _dex_backoff_until:
        # Still in backoff — return a fake 429 without hitting the API
        class _Fake429:
            status_code = 429
            text = ""
            def json(self): return {}
        record_rpc_health("dexscreener", False, 0, "backoff")
        return _Fake429()
    _dex_limiter.acquire()
    kwargs.setdefault("headers", HEADERS)
    kwargs.setdefault("timeout", 8)
    started = time.perf_counter()
    resp = requests.get(url, **kwargs)
    record_rpc_health("dexscreener", resp.ok, round((time.perf_counter() - started) * 1000), "GET")
    if resp.status_code == 429:
        _dex_backoff_until = time.time() + 30  # back off 30s on any 429
    return resp


def safe_json_response(resp, default=None):
    """Parse JSON safely from HTTP responses that may return empty/non-JSON bodies."""
    if default is None:
        default = {}
    if resp is None:
        return default
    try:
        if not (resp.text or "").strip():
            return default
    except Exception:
        return default
    try:
        data = resp.json()
        return default if data is None else data
    except Exception:
        return default

PLAN_LIMITS = {
    "free":  {"max_buy_sol": 0.05, "label": "Profit Only — 25% fee", "perf_fee": PERF_FEE_FREE},
    "basic": {"max_buy_sol": 0.1,  "label": "Basic — $49/mo",        "perf_fee": PERF_FEE_BASIC},
    "pro":   {"max_buy_sol": 1.0,  "label": "Pro — $99/mo",          "perf_fee": PERF_FEE_PRO},
    "elite": {"max_buy_sol": 5.0,  "label": "Elite — $199/mo",       "perf_fee": PERF_FEE_ELITE},
    "trial": {"max_buy_sol": 0.05, "label": "Free Trial (7 days)",   "perf_fee": PERF_FEE_BASIC},
}
PRICE_TO_PLAN = {}
for _plan_name, _price_id in (("basic", STRIPE_PRICE_BASIC), ("pro", STRIPE_PRICE_PRO), ("elite", STRIPE_PRICE_ELITE)):
    if _price_id:
        PRICE_TO_PLAN[_price_id] = _plan_name

PRESETS = {
    "safe": {
        "label":"Safe — Low Risk / Consistent",
        "description":"Small positions, tight stops. Capital preservation first.",
        "max_buy_sol":0.02,"tp1_mult":2.0,"tp2_mult":3.0,
        "trail_pct":0.15,"stop_loss":0.70,"max_age_min":360,"time_stop_min":20,
        "min_liq":10000,"min_mc":10000,"max_mc":150000,"priority_fee":10000,
        "min_vol":5000,"min_score":40,"cooldown_min":15,
        "risk_per_trade_pct":2.0,"min_holder_growth_pct":40,"min_narrative_score":18,
        "min_green_lights":1,"min_volume_spike_mult":8,"late_entry_mult":5.0,
        "offpeak_min_change":20,
        "max_hot_change":400.0,
        "nuclear_narrative_score":42,
        "anti_rug":True,"check_holders":True,"max_correlated":2,"drawdown_limit_sol":0.3,
        "listing_sniper":True,
    },
    "balanced": {
        "label":"Balanced — Medium Risk / Steady Profit",
        "description":"Moderate positions, balanced take-profits. Best for most markets.",
        "max_buy_sol":0.04,"tp1_mult":2.0,"tp2_mult":4.0,
        "trail_pct":0.20,"stop_loss":0.70,"max_age_min":240,"time_stop_min":30,
        "min_liq":5000,"min_mc":5000,"max_mc":250000,"priority_fee":30000,
        "min_vol":3000,"min_score":30,"cooldown_min":10,
        "risk_per_trade_pct":2.0,"min_holder_growth_pct":30,"min_narrative_score":16,
        "min_green_lights":1,"min_volume_spike_mult":6,"late_entry_mult":5.0,
        "offpeak_min_change":18,
        "max_hot_change":400.0,
        "nuclear_narrative_score":40,
        "anti_rug":True,"check_holders":True,"max_correlated":3,"drawdown_limit_sol":0.5,
        "listing_sniper":True,
    },
    "aggressive": {
        "label":"Aggressive — Higher Risk / Bigger Swings",
        "description":"Larger positions, wider stops. More exposure for trending markets.",
        "max_buy_sol":0.07,"tp1_mult":2.0,"tp2_mult":6.0,
        "trail_pct":0.25,"stop_loss":0.70,"max_age_min":180,"time_stop_min":45,
        "min_liq":5000,"min_mc":3000,"max_mc":400000,"priority_fee":60000,
        "min_vol":1000,"min_score":20,"cooldown_min":7,
        "risk_per_trade_pct":2.0,"min_holder_growth_pct":25,"min_narrative_score":14,
        "min_green_lights":1,"min_volume_spike_mult":5,"late_entry_mult":5.0,
        "offpeak_min_change":15,
        "max_hot_change":400.0,
        "nuclear_narrative_score":38,
        "anti_rug":True,"check_holders":True,"max_correlated":5,"drawdown_limit_sol":0.8,
        "listing_sniper":True,
    },
    "degen": {
        "label":"Degen — High Risk / Max Profit",
        "description":"Larger positions, wide stops. For hot markets only.",
        "max_buy_sol":0.10,"tp1_mult":2.0,"tp2_mult":10.0,
        "trail_pct":0.30,"stop_loss":0.70,"max_age_min":120,"time_stop_min":60,
        "min_liq":3000,"min_mc":2000,"max_mc":500000,"priority_fee":100000,
        "min_vol":500,"min_score":15,"cooldown_min":5,
        "risk_per_trade_pct":2.0,"min_holder_growth_pct":20,"min_narrative_score":12,
        "min_green_lights":1,"min_volume_spike_mult":4,"late_entry_mult":5.0,
        "offpeak_min_change":12,
        "max_hot_change":400.0,
        "nuclear_narrative_score":35,
        "anti_rug":True,"check_holders":False,"max_correlated":5,"drawdown_limit_sol":1.0,
        "listing_sniper":True,
    },
    "custom": {
        "label":"Custom — Manual Exit Tuning",
        "description":"Balanced entry filters with your own take-profit and stop rules.",
        "max_buy_sol":0.04,"tp1_mult":2.0,"tp2_mult":4.0,
        "trail_pct":0.20,"stop_loss":0.70,"max_age_min":240,"time_stop_min":30,
        "min_liq":5000,"min_mc":5000,"max_mc":250000,"priority_fee":30000,
        "min_vol":3000,"min_score":30,"cooldown_min":10,
        "risk_per_trade_pct":2.0,"min_holder_growth_pct":30,"min_narrative_score":16,
        "min_green_lights":1,"min_volume_spike_mult":6,"late_entry_mult":5.0,
        "offpeak_min_change":18,
        "max_hot_change":400.0,
        "nuclear_narrative_score":40,
        "anti_rug":True,"check_holders":True,"max_correlated":3,"drawdown_limit_sol":0.5,
        "listing_sniper":True,
    },
}
# keep backward compat
PRESETS["steady"] = PRESETS["balanced"]
PRESETS["max"]    = PRESETS["degen"]

BOT_OVERRIDE_FIELDS = [
    ("max_correlated", int), ("tp1_mult", float), ("tp2_mult", float),
    ("stop_loss", float), ("trail_pct", float), ("max_buy_sol", float),
    ("drawdown_limit_sol", float), ("cooldown_min", int),
    ("max_age_min", int), ("time_stop_min", int), ("min_liq", float),
    ("min_mc", float), ("max_mc", float), ("priority_fee", int),
    ("min_vol", float), ("min_score", int), ("risk_per_trade_pct", float),
    ("min_holder_growth_pct", float), ("min_narrative_score", int),
    ("min_green_lights", int), ("min_volume_spike_mult", float),
    ("late_entry_mult", float), ("nuclear_narrative_score", int),
    ("offpeak_min_change", float), ("max_hot_change", float),
    ("anti_rug", lambda v: bool(v) if isinstance(v, bool) else str(v or "").strip().lower() in {"1", "true", "yes", "on"}),
    ("check_holders", lambda v: bool(v) if isinstance(v, bool) else str(v or "").strip().lower() in {"1", "true", "yes", "on"}),
]

AUTO_RELAX_STATE_FIELDS = (
    "adaptive_relax_level",
    "adaptive_zero_buy_hours",
    "adaptive_last_relax_at",
)

def normalize_preset_name(preset):
    preset = str(preset or "balanced").strip().lower()
    aliases = {
        "steady": "balanced",
        "max": "degen",
    }
    preset = aliases.get(preset, preset)
    return preset if preset in PRESETS else "balanced"


def strip_auto_relax_state(settings):
    if not isinstance(settings, dict):
        return {}
    cleaned = dict(settings)
    for key in AUTO_RELAX_STATE_FIELDS:
        cleaned.pop(key, None)
    return cleaned


def central_trading_window():
    now = datetime.now(CENTRAL_TZ)
    return now, True


ADMIN_PRESET_FIELDS = {
    "max_buy_sol": "buy",
    "tp1_mult": "tp1",
    "tp2_mult": "tp2",
    "stop_loss": "sl",
    "trail_pct": "trail",
    "max_age_min": "age",
    "time_stop_min": "tstop",
    "min_liq": "liq",
    "min_mc": "minmc",
    "max_mc": "maxmc",
    "priority_fee": "prio",
    "drawdown_limit_sol": "dd",
    "max_correlated": "maxpos",
    "min_vol": "minvol",
    "min_score": "minscore",
    "risk_per_trade_pct": "risk",
    "min_holder_growth_pct": "holders",
    "min_narrative_score": "narr",
    "min_green_lights": "lights",
    "min_volume_spike_mult": "volspike",
    "late_entry_mult": "latemult",
    "nuclear_narrative_score": "nuclear",
}


def admin_preset_defaults():
    defaults = {}
    for preset_name, preset in PRESETS.items():
        if preset_name in {"steady", "max"}:
            continue
        defaults[preset_name] = {
            admin_key: preset.get(source_key)
            for source_key, admin_key in ADMIN_PRESET_FIELDS.items()
        }
    return defaults


def dashboard_preset_settings():
    return {
        preset_name: dict(PRESETS[preset_name])
        for preset_name in ("safe", "balanced", "aggressive", "degen", "custom")
    }


def build_bot_overrides(source):
    source = source or {}
    overrides = {}
    for key, cast in BOT_OVERRIDE_FIELDS:
        if key not in source or source.get(key) is None:
            continue
        try:
            value = source.get(key)
            overrides[key] = str(value) if cast is str else cast(value)
        except Exception:
            pass
    return overrides


def persist_bot_settings(user_id, preset, run_mode, duration, profit, settings):
    preset = normalize_preset_name(preset)
    settings = strip_auto_relax_state(settings)
    overrides = build_bot_overrides(settings)
    print(f"[SETTINGS] Saving for user {user_id}: preset={preset}, overrides={overrides}", flush=True)
    conn = db()
    try:
        cur = conn.cursor()
        # Use INSERT ... ON CONFLICT to ensure row exists
        cur.execute("""
            INSERT INTO bot_settings (user_id, preset, run_mode, run_duration_min, profit_target_sol,
                max_correlated, drawdown_limit_sol, custom_settings)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                preset = EXCLUDED.preset,
                run_mode = EXCLUDED.run_mode,
                run_duration_min = EXCLUDED.run_duration_min,
                profit_target_sol = EXCLUDED.profit_target_sol,
                max_correlated = EXCLUDED.max_correlated,
                drawdown_limit_sol = EXCLUDED.drawdown_limit_sol,
                custom_settings = EXCLUDED.custom_settings
        """, (
            user_id,
            preset,
            run_mode,
            int(duration or 0),
            float(profit or 0),
            int(overrides.get("max_correlated", settings.get("max_correlated", PRESETS[preset]["max_correlated"]))),
            float(overrides.get("drawdown_limit_sol", settings.get("drawdown_limit_sol", PRESETS[preset]["drawdown_limit_sol"]))),
            json.dumps(overrides) if overrides else None,
        ))
        conn.commit()
        print(f"[SETTINGS] Saved successfully", flush=True)
    except Exception as e:
        print(f"[SETTINGS] Save error: {e}", flush=True)
    finally:
        db_return(conn)
    return overrides


def load_user_effective_settings(user_id):
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT preset, custom_settings, max_correlated, drawdown_limit_sol FROM bot_settings WHERE user_id=%s",
            (user_id,),
        )
        row = cur.fetchone()
    finally:
        db_return(conn)
    preset_name = normalize_preset_name((row or {}).get("preset", "balanced"))
    settings = dict(PRESETS.get(preset_name, PRESETS["balanced"]))
    if row:
        if row.get("max_correlated") is not None:
            settings["max_correlated"] = row["max_correlated"]
        if row.get("drawdown_limit_sol") is not None:
            settings["drawdown_limit_sol"] = row["drawdown_limit_sol"]
        if row.get("custom_settings"):
            try:
                custom = json.loads(row["custom_settings"])
                if isinstance(custom, dict):
                    settings.update(custom)
            except Exception:
                pass
    return preset_name, strip_auto_relax_state(settings)


def replay_recent_market_feed(bot, limit=40):
    """Re-evaluate the latest unique scanner candidates with current bot settings."""
    if not bot:
        return 0
    seen = set()
    replayed = 0
    for item in list(market_feed):
        mint = item.get("mint")
        if not mint or mint in seen:
            continue
        seen.add(mint)
        try:
            bot.evaluate_signal(
                mint,
                item.get("name", "Unknown"),
                float(item.get("price") or 0),
                float(item.get("mc") or 0),
                float(item.get("vol") or 0),
                float(item.get("liq") or 0),
                float(item.get("age_min") or 0),
                float(item.get("change") or 0),
            )
            replayed += 1
        except Exception as _e:
            print(f"[ERROR] {_e}", flush=True)
        if replayed >= limit:
            break
    return replayed

# ── Database ───────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set. Add a PostgreSQL database to your Railway project.")

_db_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=2, maxconn=20,
    dsn=DATABASE_URL,
    cursor_factory=psycopg2.extras.RealDictCursor,
)

def db():
    conn = _db_pool.getconn()
    conn.autocommit = False
    return conn

def db_return(conn):
    """Return a connection to the pool. Call this instead of conn.close()."""
    try:
        conn.rollback()  # reset any uncommitted state
    except Exception:
        pass
    _db_pool.putconn(conn)

def init_db():
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            plan TEXT DEFAULT 'trial',
            trial_ends TEXT,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            referral_code TEXT UNIQUE,
            referred_by INTEGER,
            referral_earnings_sol REAL DEFAULT 0,
            telegram_chat_id TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id SERIAL PRIMARY KEY,
            referrer_id INTEGER,
            referred_id INTEGER,
            commission_sol REAL DEFAULT 0,
            paid INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS wallets (
            user_id INTEGER PRIMARY KEY,
            encrypted_key TEXT NOT NULL,
            public_key TEXT NOT NULL
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            user_id INTEGER PRIMARY KEY,
            preset TEXT DEFAULT 'steady',
            custom_settings TEXT,
            run_mode TEXT DEFAULT 'indefinite',
            run_duration_min INTEGER DEFAULT 0,
            profit_target_sol REAL DEFAULT 0,
            is_running INTEGER DEFAULT 0,
            drawdown_limit_sol REAL DEFAULT 0.5,
            max_correlated INTEGER DEFAULT 3
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS open_positions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            mint TEXT NOT NULL,
            name TEXT,
            entry_price REAL,
            peak_price REAL,
            entry_sol REAL,
            tp1_hit INTEGER DEFAULT 0,
            dev_wallet TEXT,
            opened_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(user_id, mint)
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            mint TEXT, name TEXT, action TEXT,
            price REAL, pnl_sol REAL,
            timestamp TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS perf_fees (
            id SERIAL PRIMARY KEY,
            session_id TEXT UNIQUE,
            user_id INTEGER,
            pnl_sol REAL, fee_sol REAL, fee_usd REAL,
            charged INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS dev_blacklist (
            dev_wallet TEXT PRIMARY KEY,
            reason TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS filter_log (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            mint TEXT, name TEXT,
            passed INTEGER DEFAULT 0,
            reason TEXT,
            score INTEGER DEFAULT 0,
            ts TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS signal_explorer_log (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            mint TEXT,
            name TEXT,
            passed INTEGER DEFAULT 0,
            reason TEXT,
            payload_json TEXT,
            ts TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS execution_events (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            mint TEXT,
            name TEXT,
            side TEXT,
            phase TEXT,
            ok INTEGER DEFAULT 0,
            latency_ms INTEGER DEFAULT 0,
            slippage_bps INTEGER,
            expected_out REAL,
            actual_out REAL,
            route_source TEXT,
            failure_reason TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS preset_overrides (
            preset TEXT PRIMARY KEY,
            overrides_json TEXT,
            updated_at TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS deployer_stats (
            deployer_wallet TEXT PRIMARY KEY,
            launches_total INTEGER DEFAULT 0,
            wins_2x INTEGER DEFAULT 0,
            wins_5x INTEGER DEFAULT 0,
            wins_10x INTEGER DEFAULT 0,
            best_multiple REAL DEFAULT 1,
            last_token_mint TEXT,
            reputation_score INTEGER DEFAULT 0,
            last_dormant_days REAL DEFAULT 0,
            first_seen_at TIMESTAMP DEFAULT NOW(),
            last_seen_at TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS token_intel (
            mint TEXT PRIMARY KEY,
            name TEXT,
            symbol TEXT,
            deployer_wallet TEXT,
            first_seen_at TIMESTAMP DEFAULT NOW(),
            last_seen_at TIMESTAMP DEFAULT NOW(),
            first_price REAL,
            max_price REAL,
            first_mc REAL,
            max_mc REAL,
            first_vol REAL,
            latest_vol REAL,
            first_liq REAL,
            latest_liq REAL,
            holder_count INTEGER DEFAULT 0,
            holder_growth_1h REAL DEFAULT 0,
            volume_spike_ratio REAL DEFAULT 0,
            first_buyer_count INTEGER DEFAULT 0,
            smart_wallet_buys INTEGER DEFAULT 0,
            smart_wallet_first10 INTEGER DEFAULT 0,
            narrative_tags TEXT,
            social_links TEXT,
            social_keys TEXT,
            narrative_score INTEGER DEFAULT 0,
            deployer_score INTEGER DEFAULT 0,
            whale_score INTEGER DEFAULT 0,
            whale_action_score INTEGER DEFAULT 0,
            cluster_confidence INTEGER DEFAULT 0,
            infra_penalty INTEGER DEFAULT 0,
            token_program TEXT,
            transfer_hook_enabled INTEGER DEFAULT 0,
            can_exit INTEGER,
            threat_risk_score INTEGER DEFAULT 0,
            threat_flags TEXT,
            infra_labels TEXT,
            liquidity_drop_pct REAL DEFAULT 0,
            max_multiple REAL DEFAULT 1,
            green_lights INTEGER DEFAULT 0,
            checklist_json TEXT,
            milestones_json TEXT,
            last_updated TIMESTAMP DEFAULT NOW()
        )""")
        conn.commit()
        # indexes on columns added by migrate_db — safe to skip if column not yet present
        for _idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_filter_log_user_id_ts ON filter_log (user_id, ts DESC)",
            "CREATE INDEX IF NOT EXISTS idx_signal_explorer_user_id_ts ON signal_explorer_log (user_id, ts DESC)",
            "CREATE INDEX IF NOT EXISTS idx_perf_fees_user_id_created_at ON perf_fees (user_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_execution_events_user_id_created_at ON execution_events (user_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_users_email ON users (email)",
            "CREATE INDEX IF NOT EXISTS idx_trades_user_id_ts ON trades (user_id, timestamp DESC)",
            "CREATE INDEX IF NOT EXISTS idx_bot_settings_user_id ON bot_settings (user_id)",
            "CREATE INDEX IF NOT EXISTS idx_open_positions_user_id ON open_positions (user_id)",
            "CREATE INDEX IF NOT EXISTS idx_token_intel_last_updated ON token_intel (last_updated DESC)",
            "CREATE INDEX IF NOT EXISTS idx_deployer_stats_last_seen ON deployer_stats (last_seen_at DESC)",
        ]:
            try:
                cur.execute(_idx_sql)
                conn.commit()
            except Exception:
                conn.rollback()
    finally:
        db_return(conn)

init_db()

# migrate existing DB — add new columns if missing
def migrate_db():
    conn = db()
    try:
        cur = conn.cursor()
        migrations = [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT 'trial'",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_ends TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by INTEGER",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_earnings_sol REAL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_chat_id TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS preset TEXT DEFAULT 'steady'",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS custom_settings TEXT",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS run_mode TEXT DEFAULT 'indefinite'",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS run_duration_min INTEGER DEFAULT 0",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS profit_target_sol REAL DEFAULT 0",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS is_running INTEGER DEFAULT 0",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS drawdown_limit_sol REAL DEFAULT 0.5",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS max_correlated INTEGER DEFAULT 3",
            "ALTER TABLE perf_fees ADD COLUMN IF NOT EXISTS session_id TEXT",
            "ALTER TABLE filter_log ADD COLUMN IF NOT EXISTS user_id INTEGER",
            "ALTER TABLE signal_explorer_log ADD COLUMN IF NOT EXISTS user_id INTEGER",
            "ALTER TABLE signal_explorer_log ADD COLUMN IF NOT EXISTS mint TEXT",
            "ALTER TABLE signal_explorer_log ADD COLUMN IF NOT EXISTS name TEXT",
            "ALTER TABLE signal_explorer_log ADD COLUMN IF NOT EXISTS passed INTEGER DEFAULT 0",
            "ALTER TABLE signal_explorer_log ADD COLUMN IF NOT EXISTS reason TEXT",
            "ALTER TABLE signal_explorer_log ADD COLUMN IF NOT EXISTS payload_json TEXT",
            "ALTER TABLE signal_explorer_log ADD COLUMN IF NOT EXISTS ts TIMESTAMP DEFAULT NOW()",
            "ALTER TABLE execution_events ADD COLUMN IF NOT EXISTS route_source TEXT",
            "ALTER TABLE token_intel ADD COLUMN IF NOT EXISTS token_program TEXT",
            "ALTER TABLE token_intel ADD COLUMN IF NOT EXISTS transfer_hook_enabled INTEGER DEFAULT 0",
            "ALTER TABLE token_intel ADD COLUMN IF NOT EXISTS can_exit INTEGER",
            "ALTER TABLE token_intel ADD COLUMN IF NOT EXISTS threat_risk_score INTEGER DEFAULT 0",
            "ALTER TABLE token_intel ADD COLUMN IF NOT EXISTS threat_flags TEXT",
            "ALTER TABLE token_intel ADD COLUMN IF NOT EXISTS infra_labels TEXT",
            "ALTER TABLE token_intel ADD COLUMN IF NOT EXISTS whale_score INTEGER DEFAULT 0",
            "ALTER TABLE token_intel ADD COLUMN IF NOT EXISTS whale_action_score INTEGER DEFAULT 0",
            "ALTER TABLE token_intel ADD COLUMN IF NOT EXISTS cluster_confidence INTEGER DEFAULT 0",
            "ALTER TABLE token_intel ADD COLUMN IF NOT EXISTS infra_penalty INTEGER DEFAULT 0",
            "ALTER TABLE token_intel ADD COLUMN IF NOT EXISTS liquidity_drop_pct REAL DEFAULT 0",
        ]
        for m in migrations:
            try:
                cur.execute(m)
            except Exception:
                conn.rollback()
        try:
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_perf_fees_session_id ON perf_fees (session_id) WHERE session_id IS NOT NULL")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_filter_log_user_id_ts ON filter_log (user_id, ts DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_signal_explorer_user_id_ts ON signal_explorer_log (user_id, ts DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_perf_fees_user_id_created_at ON perf_fees (user_id, created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_execution_events_user_id_created_at ON execution_events (user_id, created_at DESC)")
        except Exception:
            conn.rollback()
        try:
            cur.execute("""CREATE TABLE IF NOT EXISTS referrals (
                id SERIAL PRIMARY KEY,
                referrer_id INTEGER, referred_id INTEGER,
                commission_sol REAL DEFAULT 0, paid INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW())""")
        except Exception:
            conn.rollback()
        try:
            cur.execute("""CREATE TABLE IF NOT EXISTS deployer_stats (
                deployer_wallet TEXT PRIMARY KEY,
                launches_total INTEGER DEFAULT 0,
                wins_2x INTEGER DEFAULT 0,
                wins_5x INTEGER DEFAULT 0,
                wins_10x INTEGER DEFAULT 0,
                best_multiple REAL DEFAULT 1,
                last_token_mint TEXT,
                reputation_score INTEGER DEFAULT 0,
                last_dormant_days REAL DEFAULT 0,
                first_seen_at TIMESTAMP DEFAULT NOW(),
                last_seen_at TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS token_intel (
                mint TEXT PRIMARY KEY,
                name TEXT,
                symbol TEXT,
                deployer_wallet TEXT,
                first_seen_at TIMESTAMP DEFAULT NOW(),
                last_seen_at TIMESTAMP DEFAULT NOW(),
                first_price REAL,
                max_price REAL,
                first_mc REAL,
                max_mc REAL,
                first_vol REAL,
                latest_vol REAL,
                first_liq REAL,
                latest_liq REAL,
                holder_count INTEGER DEFAULT 0,
                holder_growth_1h REAL DEFAULT 0,
                volume_spike_ratio REAL DEFAULT 0,
                first_buyer_count INTEGER DEFAULT 0,
                smart_wallet_buys INTEGER DEFAULT 0,
                smart_wallet_first10 INTEGER DEFAULT 0,
                narrative_tags TEXT,
                social_links TEXT,
                social_keys TEXT,
                narrative_score INTEGER DEFAULT 0,
                deployer_score INTEGER DEFAULT 0,
                whale_score INTEGER DEFAULT 0,
                whale_action_score INTEGER DEFAULT 0,
                cluster_confidence INTEGER DEFAULT 0,
                infra_penalty INTEGER DEFAULT 0,
                token_program TEXT,
                transfer_hook_enabled INTEGER DEFAULT 0,
                can_exit INTEGER,
                threat_risk_score INTEGER DEFAULT 0,
                threat_flags TEXT,
                infra_labels TEXT,
                liquidity_drop_pct REAL DEFAULT 0,
                max_multiple REAL DEFAULT 1,
                green_lights INTEGER DEFAULT 0,
                checklist_json TEXT,
                milestones_json TEXT,
                last_updated TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS signal_explorer_log (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                mint TEXT,
                name TEXT,
                passed INTEGER DEFAULT 0,
                reason TEXT,
                payload_json TEXT,
                ts TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS execution_events (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                mint TEXT,
                name TEXT,
                side TEXT,
                phase TEXT,
                ok INTEGER DEFAULT 0,
                latency_ms INTEGER DEFAULT 0,
                slippage_bps INTEGER,
                expected_out REAL,
                actual_out REAL,
                route_source TEXT,
                failure_reason TEXT,
                created_at TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS preset_overrides (
                preset TEXT PRIMARY KEY,
                overrides_json TEXT,
                updated_at TIMESTAMP DEFAULT NOW())""")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_token_intel_last_updated ON token_intel (last_updated DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_deployer_stats_last_seen ON deployer_stats (last_seen_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_execution_events_user_id_created_at ON execution_events (user_id, created_at DESC)")
        except Exception:
            conn.rollback()
        conn.commit()
    finally:
        db_return(conn)
migrate_db()


def load_preset_overrides():
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT preset, overrides_json FROM preset_overrides")
        rows = cur.fetchall()
    except Exception:
        return
    finally:
        db_return(conn)
    for row in rows or []:
        preset = normalize_preset_name(row.get("preset"))
        if preset not in PRESETS:
            continue
        try:
            overrides = json.loads(row.get("overrides_json") or "{}")
        except Exception:
            overrides = {}
        if isinstance(overrides, dict):
            PRESETS[preset].update(overrides)


load_preset_overrides()

def make_referral_code():
    return ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))

# ── Telegram alerts ────────────────────────────────────────────────────────────
def send_telegram(chat_id, msg):
    if not TELEGRAM_TOKEN or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception as _e:
        print(f"[ERROR] Telegram send failed: {_e}", flush=True)

# ── Email notifications (SendGrid) ────────────────────────────────────────────
def send_email(to_email, subject, body_html):
    if not SENDGRID_API_KEY:
        return
    try:
        requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json={
                "personalizations": [{"to": [{"email": to_email}]}],
                "from": {"email": SMTP_FROM, "name": "SolTrader"},
                "subject": subject,
                "content": [{"type": "text/html", "value": body_html}]
            },
            timeout=10
        )
    except Exception as e:
        print(f"Email error: {e}")

def send_daily_summaries():
    """Send daily profit/loss summary emails to all users at midnight UTC."""
    while True:
        now = datetime.utcnow()
        # sleep until next midnight
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        time.sleep((next_midnight - now).total_seconds())
        try:
            conn = db()
            try:
                cur = conn.cursor()
                cur.execute("SELECT id, email FROM users")
                users = cur.fetchall()
            finally:
                db_return(conn)
            for u in users:
                uid = u["id"]
                conn = db()
                try:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT action, pnl_sol, name, timestamp FROM trades
                        WHERE user_id=%s AND timestamp >= NOW() - INTERVAL '1 day'
                        ORDER BY timestamp DESC
                    """, (uid,))
                    trades = cur.fetchall()
                finally:
                    db_return(conn)
                if not trades:
                    continue
                total_pnl = sum(t["pnl_sol"] or 0 for t in trades)
                wins   = sum(1 for t in trades if (t["pnl_sol"] or 0) > 0)
                losses = sum(1 for t in trades if (t["pnl_sol"] or 0) < 0)
                rows = "".join(
                    f"<tr><td>{t['name']}</td><td>{t['action']}</td>"
                    f"<td style='color:{'#14c784' if (t['pnl_sol'] or 0)>=0 else '#f23645'}'>"
                    f"{(t['pnl_sol'] or 0):+.4f} SOL</td></tr>"
                    for t in trades
                )
                body = f"""
                <div style='font-family:sans-serif;background:#0a0a1a;color:#fff;padding:24px;border-radius:12px;max-width:600px'>
                <h2 style='color:#14c784'>SolTrader Daily Summary</h2>
                <p>{now.strftime('%B %d, %Y')}</p>
                <div style='background:#14141e;border-radius:8px;padding:16px;margin:16px 0'>
                  <h3>Total P&L: <span style='color:{"#14c784" if total_pnl>=0 else "#f23645"}'>{total_pnl:+.4f} SOL</span></h3>
                  <p>✅ Wins: {wins} &nbsp;&nbsp; ❌ Losses: {losses}</p>
                </div>
                <table width='100%' style='border-collapse:collapse'>
                <tr style='color:#888'><th align='left'>Token</th><th align='left'>Action</th><th align='left'>P&L</th></tr>
                {rows}
                </table>
                <p style='color:#888;margin-top:24px'>Keep trading — <a href='https://soltrader-production.up.railway.app/dashboard' style='color:#14c784'>View Dashboard</a></p>
                </div>"""
                send_email(u["email"], f"SolTrader Daily Report — {total_pnl:+.4f} SOL", body)
        except Exception as e:
            print(f"Daily summary error: {e}")

# ── Encryption ─────────────────────────────────────────────────────────────────
def encrypt_key(private_key_b58: str) -> str:
    return fernet.encrypt(private_key_b58.encode()).decode()

def decrypt_key(encrypted: str) -> str:
    return fernet.decrypt(encrypted.encode()).decode()

# ── Per-user bot instances ─────────────────────────────────────────────────────
user_bots    = {}
user_bots_lock = threading.Lock()
seen_tokens  = set()
seen_tokens_lock = threading.Lock()
market_feed  = deque(maxlen=100)   # live token stream for market board
BACKGROUND_WORKER_LOCK_ID = 48270431
_background_workers_started = False
_background_workers_lock = threading.Lock()
_background_lock_conn = None
_UNSET = object()

def ai_score_detailed(info):
    """Score a token 0-100 with component breakdown."""
    vol = info.get("vol", 0)
    liq = info.get("liq", 0)
    age = info.get("age_min", 9999)
    chg = info.get("change", 0)
    mom = info.get("momentum", 0)
    vol_s = 25 if vol > 100000 else 18 if vol > 50000 else 10 if vol > 10000 else 5 if vol > 1000 else 0
    liq_s = 20 if liq > 50000 else 14 if liq > 20000 else 8 if liq > 5000 else 3 if liq > 1000 else 0
    age_s = 20 if age < 5 else 14 if age < 15 else 8 if age < 30 else 3 if age < 60 else 0
    chg_s = 20 if chg > 100 else 15 if chg > 50 else 10 if chg > 20 else 5 if chg > 5 else -10 if chg < -20 else 0
    mom_s = min(15, int(mom * 0.15))
    total = max(0, min(100, vol_s + liq_s + age_s + chg_s + mom_s))
    return {"total": total, "volume": vol_s, "liquidity": liq_s, "age": age_s, "price_change": chg_s, "momentum": mom_s}

def ai_score(info):
    """Score a token 0-100 based on multiple signals."""
    return ai_score_detailed(info)["total"]

def check_holder_concentration(mint):
    """Returns True if top holders don't own >50% of supply."""
    try:
        r = requests.post(HELIUS_RPC, json={
            "jsonrpc":"2.0","id":1,
            "method":"getTokenLargestAccounts",
            "params":[mint]
        }, timeout=6).json()
        accounts = r.get("result",{}).get("value",[])
        if not accounts: return True
        total = sum(float(a.get("uiAmount") or 0) for a in accounts)
        top5  = sum(float(a.get("uiAmount") or 0) for a in accounts[:5])
        if total <= 0: return True
        return (top5 / total) < 0.50
    except Exception as _e:
        print(f"[ERROR] Holder check failed: {_e}", flush=True)
        return True

def check_social_signals(info):
    """Return bonus score if token has social links."""
    bonus = 0
    urls = str(info).lower()
    if "t.me" in urls or "telegram" in urls: bonus += 5
    if "twitter" in urls or "x.com" in urls: bonus += 5
    return bonus

def dynamic_slippage_bps(liq_usd):
    """Returns slippage in bps based on pool liquidity."""
    if liq_usd > 100000: return 300
    if liq_usd > 50000:  return 800
    if liq_usd > 10000:  return 1500
    return 2500

def check_dev_blacklist(dev_wallet):
    if not dev_wallet: return True
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM dev_blacklist WHERE dev_wallet=%s", (dev_wallet,))
        row = cur.fetchone()
    finally:
        db_return(conn)
    return row is None

def blacklist_dev(dev_wallet, reason="rugged"):
    try:
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("INSERT INTO dev_blacklist (dev_wallet,reason) VALUES (%s,%s) ON CONFLICT DO NOTHING", (dev_wallet, reason))
            conn.commit()
        finally:
            db_return(conn)
    except Exception as _e:
        print(f"[ERROR] {_e}", flush=True)

def get_market_stats():
    """Aggregate recent trade stats for AI settings suggestion."""
    try:
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT pnl_sol, action, timestamp FROM trades
                WHERE timestamp >= NOW() - INTERVAL '24 hours'
            """)
            trades = cur.fetchall()
        finally:
            db_return(conn)
        if not trades: return {}
        wins   = [t for t in trades if (t["pnl_sol"] or 0) > 0]
        losses = [t for t in trades if (t["pnl_sol"] or 0) < 0]
        win_rate = len(wins) / len(trades) if trades else 0
        avg_win  = sum(t["pnl_sol"] for t in wins)   / max(len(wins),1)
        avg_loss = sum(t["pnl_sol"] for t in losses) / max(len(losses),1)
        return {
            "total_trades": len(trades),
            "win_rate": round(win_rate * 100, 1),
            "avg_win_sol": round(avg_win, 4),
            "avg_loss_sol": round(avg_loss, 4),
        }
    except: return {}

def ai_suggest_settings(stats):
    """Given market stats, return suggested preset name + rationale."""
    if not stats:
        return {"preset":"balanced","reason":"Not enough data yet — using balanced defaults."}
    trades = int(stats.get("total_trades", 0) or 0)
    wr = float(stats.get("win_rate", 50) or 50)
    avg_win = float(stats.get("avg_win_sol", 0) or 0)
    avg_loss = abs(float(stats.get("avg_loss_sol", 0) or 0))
    expectancy = (wr / 100.0) * avg_win - ((100.0 - wr) / 100.0) * avg_loss
    if trades < 8:
        return {"preset":"balanced","reason":"Not enough 24h trade data yet — balanced stays the safest default."}
    if wr < 40 or expectancy <= 0:
        return {"preset":"safe","reason":f"Win rate {wr:.1f}% and expectancy {expectancy:+.4f} SOL suggest defensive settings."}
    if wr >= 62 and expectancy >= 0.03 and avg_win > max(avg_loss, 0.0001) * 1.8:
        return {"preset":"degen","reason":f"Win rate {wr:.1f}% with strong expectancy {expectancy:+.4f} SOL points to a very hot tape."}
    if wr >= 52 and expectancy >= 0.015:
        return {"preset":"aggressive","reason":f"Win rate {wr:.1f}% and positive expectancy {expectancy:+.4f} SOL support a more aggressive preset."}
    return {"preset":"balanced","reason":f"Win rate {wr:.1f}% is workable but not strong enough for max aggression."}

# ── SOL transfer (for fee collection) ─────────────────────────────────────────
def send_sol(keypair, to_address, amount_sol):
    """Transfer SOL from keypair to to_address. Returns tx signature or None."""
    if not to_address or amount_sol < 0.0001:
        return None
    try:
        from solders.system_program import transfer, TransferParams
        from solders.pubkey import Pubkey
        from solders.message import MessageV0
        from solders.hash import Hash as SolHash
        lamports = int(amount_sol * 1e9)
        # get recent blockhash
        bh_r = requests.post(HELIUS_RPC, json={
            "jsonrpc":"2.0","id":1,"method":"getLatestBlockhash",
            "params":[{"commitment":"confirmed"}]
        }, timeout=8).json()
        blockhash = SolHash.from_string(bh_r["result"]["value"]["blockhash"])
        to_pk = Pubkey.from_string(to_address)
        ix    = transfer(TransferParams(from_pubkey=keypair.pubkey(), to_pubkey=to_pk, lamports=lamports))
        msg   = MessageV0.try_compile(keypair.pubkey(), [ix], [], blockhash)
        tx    = VersionedTransaction(msg, [keypair])
        enc   = base64.b64encode(bytes(tx)).decode()
        res   = requests.post(HELIUS_RPC, json={
            "jsonrpc":"2.0","id":1,"method":"sendTransaction",
            "params":[enc,{"encoding":"base64","skipPreflight":False,"preflightCommitment":"confirmed"}]
        }, timeout=15).json()
        return res.get("result")
    except Exception as e:
        print(f"send_sol error: {e}")
        return None

class BotInstance:
    def __init__(self, user_id, keypair, settings, run_mode, run_duration_min, profit_target_sol, preset_name="balanced"):
        self.user_id          = user_id
        self.keypair          = keypair
        self.wallet           = str(keypair.pubkey())
        self.settings         = strip_auto_relax_state(dict(settings))
        self.preset_name      = normalize_preset_name(preset_name)
        self.positions        = {}
        self.log              = []
        self.running          = True
        self.thread           = None
        self.sol_balance      = 0.0
        self.start_balance    = 0.0
        self.stats            = {"wins": 0, "losses": 0, "total_pnl_sol": 0.0}
        self.run_mode         = run_mode
        self.run_duration_min = run_duration_min
        self.profit_target    = profit_target_sol
        self.started_at       = time.time()
        self.session_id       = secrets.token_hex(16)
        self.session_drawdown  = 0.0
        self.perf_fee_recorded = False
        self.filter_log        = deque(maxlen=50)
        self.signal_explorer_log = deque(maxlen=200)
        self.execution_events  = deque(maxlen=200)
        # ── Risk controls ───────────────────────────────────────────────────
        self.peak_balance       = 0.0   # for max-drawdown circuit breaker
        self.buys_this_hour     = 0
        self.evals_this_hour    = 0
        self.hour_start         = time.time()
        self.consecutive_losses = 0
        self.cooldown_until     = 0.0   # retained for API compatibility; cooldown is disabled
        self.loss_mints         = {}    # mint -> epoch when the bot last realized a loss on that mint
        self.auto_relax_level   = 0
        
        # ── Enhanced Trading Systems (Research Paper Implementation) ────────
        self.enhanced_enabled = ENHANCED_SYSTEMS_AVAILABLE
        if self.enhanced_enabled:
            try:
                self.whale_system = get_whale_detection_system()
                self.risk_engine = get_risk_engine(HELIUS_RPC)
                self.mev_system = get_mev_protection(HELIUS_RPC)
                self.observability = get_observability()
                self.signal_evaluator = EnhancedSignalEvaluator(
                    self.whale_system, self.risk_engine, self.observability
                )
                self.execution_handler = EnhancedExecutionHandler(
                    self.mev_system, self.risk_engine, self.observability
                )
                self.position_monitor = EnhancedPositionMonitor(
                    self.whale_system, self.risk_engine, self.observability
                )
                self.log_msg("🚀 Enhanced trading systems initialized (whale detection, risk engine, MEV protection)")
            except Exception as _enh_err:
                self.enhanced_enabled = False
                print(f"[WARN] Enhanced systems init failed: {_enh_err}")

    def relax_entry_guards(self):
        return None

    def persist_runtime_settings(self):
        persist_bot_settings(
            self.user_id,
            self.preset_name,
            self.run_mode,
            self.run_duration_min,
            self.profit_target,
            self.settings,
        )

    def maybe_relax_guards(self):
        now = time.time()
        if now - self.hour_start < 3600:
            return
        self.hour_start = now
        self.buys_this_hour = 0
        self.evals_this_hour = 0
        return

    def execution_health_alert(self):
        now = time.time()
        recent_exec = [
            row for row in list(self.execution_events)
            if now - float(row.get("timestamp") or 0) <= 1200 and row.get("phase") in ("quote", "send", "exit-check")
        ]
        if len(recent_exec) >= 8:
            fail_rate = round(sum(1 for row in recent_exec if not row.get("ok")) / len(recent_exec) * 100, 1)
            exit_checks = [row for row in recent_exec if row.get("phase") == "exit-check"]
            if fail_rate >= 75:
                return f"Execution failure rate {fail_rate:.0f}% over last {len(recent_exec)} attempts"
            if len(exit_checks) >= 5 and all(not row.get("ok") for row in exit_checks):
                return "Recent exit simulations all failed"
        rpc_stats = rpc_health_snapshot(window_sec=900)
        if rpc_stats["total"] >= 12 and (rpc_stats["fail_rate"] >= 55 or rpc_stats["p95_ms"] >= 4500):
            return f"RPC degraded — fail {rpc_stats['fail_rate']:.0f}% / p95 {rpc_stats['p95_ms']:.0f}ms"
        return None

    def log_msg(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.log.insert(0, f"[{ts}] {msg}")
        if len(self.log) > 200:
            self.log.pop()
        print(f"[U{self.user_id}] {msg}")

    def log_filter(self, name, mint, passed, reason, score=0):
        entry = {
            "name": name, "mint": mint, "passed": passed,
            "reason": reason, "score": score,
            "ts": time.strftime("%H:%M:%S")
        }
        self.filter_log.appendleft(entry)
        # also save to DB
        try:
            conn = db()
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO filter_log (user_id,mint,name,passed,reason,score) VALUES (%s,%s,%s,%s,%s,%s)",
                    (self.user_id, mint, name, int(passed), reason, score)
                )
                conn.commit()
            finally:
                db_return(conn)
        except Exception as _e:
            print(f"[ERROR] {_e}", flush=True)

    def log_signal_entry(self, entry):
        payload = dict(entry or {})
        self.signal_explorer_log.appendleft(payload)
        try:
            conn = db()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO signal_explorer_log (user_id,mint,name,passed,reason,payload_json)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        self.user_id,
                        payload.get("mint"),
                        payload.get("name"),
                        int(bool(payload.get("passed"))),
                        payload.get("reason", ""),
                        json.dumps(payload),
                    )
                )
                conn.commit()
            finally:
                db_return(conn)
        except Exception as _e:
            print(f"[ERROR] {_e}", flush=True)

    def log_execution_event(self, mint, name, side, phase, ok, latency_ms=0, slippage_bps=None,
                            expected_out=None, actual_out=None, route_source=None, failure_reason=None):
        payload = {
            "mint": mint,
            "name": name,
            "side": side,
            "phase": phase,
            "ok": bool(ok),
            "latency_ms": int(latency_ms or 0),
            "slippage_bps": None if slippage_bps is None else int(slippage_bps),
            "expected_out": None if expected_out is None else float(expected_out),
            "actual_out": None if actual_out is None else float(actual_out),
            "route_source": route_source,
            "failure_reason": failure_reason,
            "ts": time.strftime("%H:%M:%S"),
            "timestamp": time.time(),
        }
        self.execution_events.appendleft(payload)
        try:
            conn = db()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO execution_events (
                        user_id,mint,name,side,phase,ok,latency_ms,slippage_bps,
                        expected_out,actual_out,route_source,failure_reason
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        self.user_id, mint, name, side, phase, int(bool(ok)), int(latency_ms or 0),
                        None if slippage_bps is None else int(slippage_bps),
                        None if expected_out is None else float(expected_out),
                        None if actual_out is None else float(actual_out),
                        route_source, failure_reason,
                    )
                )
                conn.commit()
            finally:
                db_return(conn)
        except Exception as _e:
            print(f"[ERROR] {_e}", flush=True)

    def should_stop(self):
        if self.run_mode == "duration" and self.run_duration_min > 0:
            if (time.time() - self.started_at) / 60 >= self.run_duration_min:
                self.log_msg(f"Run duration reached ({self.run_duration_min} min) — stopping")
                return True
        if self.run_mode == "profit" and self.profit_target > 0:
            if self.stats["total_pnl_sol"] >= self.profit_target:
                self.log_msg(f"Profit target reached ({self.profit_target:.4f} SOL) — stopping")
                return True
        return False

    def refresh_balance(self):
        payload = {"jsonrpc":"2.0","id":1,"method":"getBalance","params":[self.wallet]}
        for rpc_url in [HELIUS_RPC, "https://rpc.ankr.com/solana", "https://solana-rpc.publicnode.com", "https://api.mainnet-beta.solana.com"]:
            try:
                r = requests.post(rpc_url, json=payload, timeout=5)
                data = r.json()
                if "error" in data:
                    print(f"[WARN] Balance RPC error from {rpc_url[:40]}: {data['error']}", flush=True)
                    continue
                val = data.get("result",{}).get("value",0)
                self.sol_balance = val / 1e9
                if self.sol_balance > self.peak_balance:
                    self.peak_balance = self.sol_balance
                return
            except Exception as _e:
                print(f"[ERROR] Balance fetch failed ({rpc_url[:40]}): {_e}", flush=True)
        print(f"[ERROR] All RPCs failed for balance check, wallet={self.wallet}", flush=True)

    # ── Jito tip accounts (mainnet) ──────────────────────────────────────────
    JITO_TIPS = [
        "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE",
        "D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ",
        "9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta",
        "5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn",
        "2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD",
        "2q5pghRs6arqVjRvT5gfgWfWcHWmw1ZuCzphgd5KfWGJ",
        "wyvPkWjVZz1M8fHQnMMCDTQDbkManefNNhweYk5WkcF",
        "3KCKozbAaF75qEU33jtzozcJ29yJuaLJTy2jFdzUY8bT",
        "4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey",
        "4TQLFNWK8AovT1gFvda5jfw2oJeRMKEmw7aH6MGBJ3or",
    ]
    TIP_LAMPORTS = 200_000  # 0.0002 SOL — minimum for Jito routing

    def _get_dynamic_tip(self):
        """Fetch 75th-percentile landed tip from Jito API; fallback to 0.0002 SOL."""
        try:
            r = requests.get("https://bundles.jito.wtf/api/v1/bundles/tip_floor", timeout=4).json()
            tip_sol = r[0].get("landed_tips_75th_percentile", 0.0002)
            return max(int(tip_sol * 1e9), self.TIP_LAMPORTS)
        except Exception as _e:
            print(f"[ERROR] {_e}", flush=True)
            return self.TIP_LAMPORTS

    def _inject_tip(self, tx):
        """
        Inject a Jito tip SOL-transfer instruction into an existing Jupiter
        VersionedTransaction (V0).  Returns a NEW signed VersionedTransaction
        with the tip appended, or None if injection fails.

        Strategy (mirrors the TypeScript createSenderTransactionFromSwapResponse):
          1. Read static account_keys and header from the compiled V0 message.
          2. Find (or add) the System Program in the static keys.
          3. Insert the tip Pubkey at the start of the readonly-unsigned section
             so it is classified as a writable non-signer by the runtime.
          4. Shift every account index in every existing instruction by +1 for
             each account inserted before it.
          5. Append a CompiledInstruction for System::Transfer(tip_lamports).
          6. Rebuild MessageV0 + sign with our keypair.
        """
        import struct
        from solders.pubkey import Pubkey
        from solders.message import MessageV0, MessageHeader
        from solders.instruction import CompiledInstruction as CI

        SYSTEM_PROG = Pubkey.from_string("11111111111111111111111111111111")
        tip_pk      = Pubkey.from_string(random.choice(self.JITO_TIPS))
        tip_lamps   = self._get_dynamic_tip()

        msg       = tx.message
        old_keys  = list(msg.account_keys)
        h         = msg.header
        ns        = h.num_required_signatures
        nrs       = h.num_readonly_signed_accounts
        nru       = h.num_readonly_unsigned_accounts

        # ── locate accounts ────────────────────────────────────────────────
        sys_idx = next((i for i, k in enumerate(old_keys) if k == SYSTEM_PROG), None)
        tip_idx = next((i for i, k in enumerate(old_keys) if k == tip_pk),      None)

        new_keys = list(old_keys)
        new_nru  = nru
        # insertion point = first readonly-unsigned slot
        insert_at = len(new_keys) - nru

        # ── insert tip account as writable non-signer ──────────────────────
        if tip_idx is None:
            new_keys.insert(insert_at, tip_pk)
            tip_idx   = insert_at
            tip_shift = insert_at          # indices >= this shift +1
        else:
            tip_shift = len(new_keys)      # no shift

        # adjust sys_idx after potential shift
        if sys_idx is not None and sys_idx >= tip_shift:
            sys_idx += 1

        # ── add System Program as readonly non-signer if missing ───────────
        sys_shift = len(new_keys)         # default: no shift
        if sys_idx is None:
            new_keys.append(SYSTEM_PROG)
            sys_idx  = len(new_keys) - 1
            new_nru += 1                  # one more readonly-unsigned

        # ── remap existing instruction account indices ──────────────────────
        def remap(i):
            if i >= sys_shift:  return i + (1 if sys_idx == len(new_keys) - 1 else 0)
            if i >= tip_shift:  return i + 1
            return i

        new_ixs = []
        for ix in msg.instructions:
            new_ixs.append(CI(
                program_id_index = remap(ix.program_id_index),
                accounts         = bytes(remap(a) for a in bytes(ix.accounts)),
                data             = bytes(ix.data),
            ))

        # ── append tip instruction ─────────────────────────────────────────
        tip_data = struct.pack('<IQ', 2, tip_lamps)   # System::Transfer
        new_ixs.append(CI(
            program_id_index = sys_idx,
            accounts         = bytes([0, tip_idx]),    # payer=0, tip account
            data             = tip_data,
        ))

        # ── rebuild message & sign ─────────────────────────────────────────
        new_msg = MessageV0(
            header              = MessageHeader(ns, nrs, new_nru),
            account_keys        = new_keys,
            recent_blockhash    = msg.recent_blockhash,
            instructions        = new_ixs,
            address_table_lookups = list(msg.address_table_lookups),
        )
        return VersionedTransaction(new_msg, [self.keypair])

    def _sender_endpoints(self):
        from urllib.parse import urlparse, parse_qs
        parsed  = urlparse(HELIUS_RPC)
        api_key = parse_qs(parsed.query).get("api-key", [""])[0]
        qs = f"?api-key={api_key}" if api_key else ""
        return [
            f"http://slc-sender.helius-rpc.com/fast{qs}",  # Salt Lake City
            f"http://ewr-sender.helius-rpc.com/fast{qs}",  # Newark
            f"http://lon-sender.helius-rpc.com/fast{qs}",  # London
            f"http://fra-sender.helius-rpc.com/fast{qs}",  # Frankfurt
            f"http://ams-sender.helius-rpc.com/fast{qs}",  # Amsterdam
            f"http://sg-sender.helius-rpc.com/fast{qs}",   # Singapore
            f"http://tyo-sender.helius-rpc.com/fast{qs}",  # Tokyo
        ]

    def _encode_signed_transaction(self, signed_tx):
        return base64.b64encode(bytes(signed_tx)).decode()

    def _latest_blockhash(self):
        result = rpc_call("getLatestBlockhash", [{"commitment": "processed"}], timeout=6) or {}
        value = result.get("value") if isinstance(result, dict) else {}
        return (value or {}).get("blockhash")

    def _resign_with_blockhash(self, tx, recent_blockhash):
        from solders.hash import Hash as SolHash
        from solders.message import MessageV0

        if not recent_blockhash:
            return tx
        msg = tx.message
        fresh_hash = recent_blockhash if not isinstance(recent_blockhash, str) else SolHash.from_string(recent_blockhash)
        new_msg = MessageV0(
            header=msg.header,
            account_keys=list(msg.account_keys),
            recent_blockhash=fresh_hash,
            instructions=list(msg.instructions),
            address_table_lookups=list(msg.address_table_lookups),
        )
        return VersionedTransaction(new_msg, [self.keypair])

    def _simulate_signed_transaction(self, signed_tx):
        started = time.perf_counter()
        result = rpc_call("simulateTransaction", [
            self._encode_signed_transaction(signed_tx),
            {
                "encoding": "base64",
                "replaceRecentBlockhash": True,
                "sigVerify": False,
                "commitment": "processed",
            },
        ], timeout=10) or {}
        value = result.get("value") if isinstance(result, dict) else {}
        replacement = None
        replacement_blockhash = (value or {}).get("replacementBlockhash")
        if isinstance(replacement_blockhash, dict):
            replacement = replacement_blockhash.get("blockhash")
        return {
            "ok": bool(value) and not value.get("err"),
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "units_consumed": value.get("unitsConsumed"),
            "err": value.get("err"),
            "logs": value.get("logs") or [],
            "replacement_blockhash": replacement,
        }

    def _is_blockhash_error(self, message):
        text = str(message or "").lower()
        return any(token in text for token in (
            "blockhash not found",
            "blockhash expired",
            "transaction expired",
            "signature has expired",
        ))

    def _send_encoded_transaction(self, enc):
        send_params = {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}
        req_id = str(int(time.time()*1000))
        region_errors = []

        def _send_region(url):
            r = requests.post(url, json={
                "jsonrpc":"2.0","id":req_id,
                "method":"sendTransaction","params":[enc, send_params]
            }, timeout=5)
            res = r.json()
            if res.get("result"):
                return url, res["result"]
            if res.get("error"):
                raise ValueError(res["error"].get("message","?"))
            return None

        try:
            with ThreadPoolExecutor(max_workers=7) as pool:
                futures = {pool.submit(_send_region, u): u for u in self._sender_endpoints()}
                for fut in as_completed(futures, timeout=6):
                    try:
                        result = fut.result()
                        if result:
                            url, sig = result
                            region = url.split("//")[1].split("-sender")[0]
                            return {
                                "sig": sig,
                                "source": f"helius-sender:{region}",
                                "error": None,
                            }
                    except Exception as e:
                        region_errors.append(str(e))
        except Exception as e:
            region_errors.append(str(e))

        # ── fallback: regular Helius RPC ────────────────────────────────────
        try:
            r   = requests.post(HELIUS_RPC, json={
                "jsonrpc":"2.0","id":1,"method":"sendTransaction",
                "params":[enc,{"encoding":"base64","skipPreflight":True,"maxRetries":3}]
            }, timeout=15)
            res = r.json()
            if res.get("result"):
                return {
                    "sig": res["result"],
                    "source": "helius-rpc-fallback",
                    "error": None,
                }
            error_text = ""
            if res.get("error"):
                error_text = res["error"].get("message", "")
            return {
                "sig": None,
                "source": "helius-rpc-fallback",
                "error": error_text or "; ".join(region_errors[:3]) or "transaction-send-failed",
            }
        except Exception as e:
            return {
                "sig": None,
                "source": "helius-rpc-fallback",
                "error": str(e) or "; ".join(region_errors[:3]) or "transaction-send-failed",
            }

    def sign_and_send(self, swap_tx_b64):
        raw = base64.b64decode(swap_tx_b64)
        tx  = VersionedTransaction.from_bytes(raw)
        meta = {
            "sig": None,
            "source": "",
            "tip_injected": False,
            "simulation_ok": None,
            "simulation_units": None,
            "simulation_error": "",
            "simulation_latency_ms": 0,
            "send_latency_ms": 0,
            "resend_count": 0,
            "failure_reason": "",
        }

        try:
            signed_tx = self._inject_tip(tx)
            meta["tip_injected"] = True
            self.log_msg(f"💰 Jito tip injected ({self.TIP_LAMPORTS/1e9:.4f} SOL)")
        except Exception as e:
            self.log_msg(f"Tip injection failed ({e}), sending without tip")
            signed_tx = VersionedTransaction(tx.message, [self.keypair])
            meta["failure_reason"] = f"tip-injection:{e}"

        fresh_blockhash = self._latest_blockhash()
        if fresh_blockhash:
            try:
                signed_tx = self._resign_with_blockhash(signed_tx, fresh_blockhash)
            except Exception as e:
                meta["failure_reason"] = f"blockhash-refresh:{e}"

        simulation = self._simulate_signed_transaction(signed_tx)
        meta["simulation_ok"] = bool(simulation.get("ok"))
        meta["simulation_units"] = simulation.get("units_consumed")
        meta["simulation_latency_ms"] = int(simulation.get("latency_ms") or 0)
        meta["simulation_error"] = _format_rpc_error(simulation.get("err"))
        if meta["simulation_units"]:
            self.log_msg(f"📋 Simulated swap | {int(meta['simulation_units'])} CU")
        if meta["simulation_error"]:
            self.log_msg(f"📋 Simulation warning: {meta['simulation_error']}")
        if simulation.get("replacement_blockhash"):
            try:
                signed_tx = self._resign_with_blockhash(signed_tx, simulation["replacement_blockhash"])
            except Exception:
                pass

        send_started = time.perf_counter()
        last_error = meta["simulation_error"] or meta["failure_reason"]
        for attempt in range(2):
            if attempt:
                meta["resend_count"] = attempt
                refreshed = self._latest_blockhash()
                if refreshed:
                    try:
                        signed_tx = self._resign_with_blockhash(signed_tx, refreshed)
                        self.log_msg("🔁 Refreshed blockhash and retried send")
                    except Exception as e:
                        last_error = f"blockhash-resign-failed:{e}"
                        break
            send_result = self._send_encoded_transaction(self._encode_signed_transaction(signed_tx))
            if send_result.get("sig"):
                meta["sig"] = send_result["sig"]
                meta["source"] = send_result.get("source") or ""
                if meta["source"].startswith("helius-sender:"):
                    self.log_msg(f"⚡ Sent via Helius Sender ({meta['source'].split(':', 1)[1]})")
                else:
                    self.log_msg("📡 Sent via Helius RPC (fallback)")
                break
            last_error = send_result.get("error") or last_error or "transaction-send-failed"
            if not self._is_blockhash_error(last_error):
                break
        meta["send_latency_ms"] = round((time.perf_counter() - send_started) * 1000)
        meta["failure_reason"] = "" if meta["sig"] else (last_error or "transaction-send-failed")
        return meta

    def _jupiter_quote_single(self, url):
        """Try a single Jupiter quote URL with retries. Returns quote dict or None."""
        for attempt in range(2):
            try:
                r = requests.get(url, timeout=8).json()
                if "error" not in r:
                    return r
            except Exception:
                if attempt < 1:
                    time.sleep(0.5)
        return None

    def jupiter_quote(self, input_mint, output_mint, amount, slippage_bps=1500):
        urls = [
            f"https://lite-api.jup.ag/swap/v1/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount}&slippageBps={slippage_bps}",
            f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount}&slippageBps={slippage_bps}",
        ]
        # Hit both endpoints in parallel, take first success
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(self._jupiter_quote_single, u): u for u in urls}
            for fut in as_completed(futures, timeout=12):
                try:
                    result = fut.result()
                    if result:
                        return result
                except Exception as e:
                    self.log_msg(f"Jupiter quote failed ({futures[fut].split('/')[2]}): {e}")
        return None

    def jupiter_swap(self, quote):
        endpoints = ["https://lite-api.jup.ag/swap/v1/swap", "https://quote-api.jup.ag/v6/swap"]
        for url in endpoints:
            try:
                r = requests.post(url, json={
                    "quoteResponse":quote,"userPublicKey":self.wallet,"wrapAndUnwrapSol":True,
                    "dynamicComputeUnitLimit": True,
                    "prioritizationFeeLamports": {
                        "priorityLevelWithMaxLamports": {
                            "maxLamports": int(self.settings.get("priority_fee", 1_000_000)),
                            "priorityLevel": "veryHigh"
                        }
                    },
                }, timeout=12).json()
                if r.get("swapTransaction"):
                    return r["swapTransaction"]
            except Exception as e:
                self.log_msg(f"Jupiter swap failed ({url.split('/')[2]}): {e}")
        return None

    def get_token_balance(self, mint):
        r = requests.post(HELIUS_RPC, json={
            "jsonrpc":"2.0","id":1,"method":"getTokenAccountsByOwner",
            "params":[self.wallet,{"mint":mint},{"encoding":"jsonParsed"}]
        }, timeout=5)
        accounts = r.json().get("result",{}).get("value",[])
        if not accounts:
            return 0
        return int(accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])

    def get_token_price(self, mint):
        try:
            resp = dex_get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                timeout=5
            )
            if resp.status_code != 200:
                return None
            pairs = resp.json().get("pairs")
            if pairs:
                return float(pairs[0].get("priceUsd") or 0)
        except Exception:
            pass
        return None

    def is_safe_token(self, mint):
        try:
            r = requests.post(HELIUS_RPC, json={
                "jsonrpc":"2.0","id":1,"method":"getAccountInfo",
                "params":[mint,{"encoding":"jsonParsed"}]
            }, timeout=5).json()
            info = r.get("result",{}).get("value",{})
            if not info:
                return True
            parsed      = info.get("data",{}).get("parsed",{}).get("info",{})
            mint_auth   = parsed.get("mintAuthority")
            freeze_auth = parsed.get("freezeAuthority")
            if mint_auth is not None and mint_auth != PUMP_PROGRAM:
                return False
            if freeze_auth is not None:
                return False
            return True
        except Exception as _e:
            print(f"[ERROR] {_e}", flush=True)
            return True

    def check_circuit_breakers(self):
        """Returns a reason string if trading should halt, else None."""
        if self.start_balance > 0:
            daily_loss_pct = (self.start_balance - self.sol_balance) / self.start_balance
            if daily_loss_pct >= 0.05:
                return f"Daily loss limit −5% hit ({daily_loss_pct*100:.1f}%)"
        if self.peak_balance > 0:
            drawdown_pct = (self.peak_balance - self.sol_balance) / self.peak_balance
            if drawdown_pct >= 0.12:
                return f"Max drawdown −12% hit ({drawdown_pct*100:.1f}%)"
        health_alert = self.execution_health_alert()
        if health_alert:
            return health_alert
        return None

    def check_rate_limit(self, name, mint):
        """Returns a block reason string if rate-limited, else None."""
        last_loss = self.loss_mints.get(mint)
        if last_loss is not None:
            if time.time() - last_loss < 30 * 60:
                return "this mint lost within the past 30 minutes"
            self.loss_mints.pop(mint, None)
        return None

    def check_honeypot(self, mint, age_min=0):
        """Simulate a sell via Jupiter. Skip check for very new tokens (< 20m) — no route yet."""
        if age_min < 20:
            return True  # too new for Jupiter to index; rely on authority checks instead
        try:
            started = time.perf_counter()
            test_quote = self.jupiter_quote(mint, SOL_MINT, 10_000_000, 5000)
            self.log_execution_event(
                mint, None, "risk", "exit-check", bool(test_quote),
                latency_ms=round((time.perf_counter() - started) * 1000),
                slippage_bps=5000,
                expected_out=(test_quote or {}).get("outAmount"),
                route_source=extract_route_label(test_quote),
                failure_reason=None if test_quote else "no-sell-route",
            )
            return test_quote is not None
        except Exception as _e:
            self.log_execution_event(mint, None, "risk", "exit-check", False, failure_reason=str(_e)[:180])
            print(f"[ERROR] {_e}", flush=True)
            return True

    def buy(self, mint, name, price, liq=0, dev_wallet=None, age_min=0):
        s = self.settings
        trade_sol = round(float(s["max_buy_sol"]), 4)
        print(f"[BUY U{self.user_id}] Attempting {name} | bal={self.sol_balance:.4f} need={trade_sol+0.01:.4f}", flush=True)

        # ── Circuit breakers ─────────────────────────────────────────────────
        cb = self.check_circuit_breakers()
        if cb:
            self.log_msg(f"⛔ CIRCUIT BREAKER — {cb} — halting bot")
            self.running = False
            return
        # ── Rate limiting ────────────────────────────────────────────────────
        rl = self.check_rate_limit(name, mint)
        if rl:
            self.log_filter(name, mint, False, rl)
            self.log_msg(f"SKIP {name} — {rl}")
            return
        # ── Honeypot simulation (skip for tokens < 20m — Jupiter not indexed yet) ─
        if not self.check_honeypot(mint, age_min=age_min):
            self.log_filter(name, mint, False, "HONEYPOT — no sell route found via Jupiter")
            self.log_msg(f"SKIP {name} — HONEYPOT detected")
            return
        # Drawdown limit
        if s.get("drawdown_limit_sol",0) > 0 and self.session_drawdown >= s["drawdown_limit_sol"]:
            reason = f"Drawdown limit reached ({self.session_drawdown:.3f} SOL)"
            self.log_filter(name, mint, False, reason)
            self.log_msg(f"SKIP {name} — {reason}")
            return
        # Correlated position limit
        if len(self.positions) >= s.get("max_correlated", 5):
            reason = f"Max correlated positions ({len(self.positions)})"
            self.log_filter(name, mint, False, reason)
            self.log_msg(f"SKIP {name} — {reason}")
            return
        if self.sol_balance < trade_sol + 0.01:
            reason = f"Low balance ({self.sol_balance:.4f} SOL, need {trade_sol+0.01:.4f})"
            self.log_filter(name, mint, False, reason)
            self.log_msg(f"SKIP {name} — {reason}")
            return
        if mint in self.positions:
            self.log_msg(f"SKIP {name} — already in position")
            return
        # Anti-rug / mint auth check (skip for brand-new tokens — bonding curve has non-null mint auth by design)
        if s.get("anti_rug") and age_min >= 30 and not self.is_safe_token(mint):
            self.log_filter(name, mint, False, "RUG RISK — mint/freeze auth active")
            self.log_msg(f"SKIP {name} — RUG RISK (mint/freeze auth)")
            return
        # Dev blacklist
        if dev_wallet and not check_dev_blacklist(dev_wallet):
            reason = f"Dev blacklisted ({dev_wallet[:8]}...)"
            self.log_filter(name, mint, False, reason)
            self.log_msg(f"SKIP {name} — {reason}")
            return
        # Holder concentration (skip for very new tokens — pump.fun always starts concentrated)
        if s.get("check_holders") and age_min >= 30 and not check_holder_concentration(mint):
            self.log_filter(name, mint, False, "Top 5 holders own >50% of supply")
            self.log_msg(f"SKIP {name} — holder concentration too high")
            return
        # Dynamic slippage
        slippage = dynamic_slippage_bps(liq)
        self.log_msg(f"Quoting {name} | size={trade_sol:.4f} SOL | slippage={slippage}bps ...")
        quote_started = time.perf_counter()
        quote = self.jupiter_quote(SOL_MINT, mint, int(trade_sol*1e9), slippage)
        route_source = extract_route_label(quote)
        self.log_execution_event(
            mint, name, "buy", "quote", bool(quote),
            latency_ms=round((time.perf_counter() - quote_started) * 1000),
            slippage_bps=slippage,
            expected_out=(quote or {}).get("outAmount"),
            route_source=route_source,
            failure_reason=None if quote else "no-quote",
        )
        if not quote:
            self.log_filter(name, mint, False, "No Jupiter quote available")
            self.log_msg(f"SKIP {name} — no Jupiter quote (token may not be tradeable yet)")
            return
        swap_started = time.perf_counter()
        swap_tx = self.jupiter_swap(quote)
        self.log_execution_event(
            mint, name, "buy", "build", bool(swap_tx),
            latency_ms=round((time.perf_counter() - swap_started) * 1000),
            slippage_bps=slippage,
            expected_out=quote.get("outAmount"),
            route_source=route_source,
            failure_reason=None if swap_tx else "swap-build-failed",
        )
        if not swap_tx:
            self.log_filter(name, mint, False, "Jupiter swap build failed")
            self.log_msg(f"SKIP {name} — Jupiter swap build failed")
            return
        send_result = self.sign_and_send(swap_tx)
        simulate_note = None
        if send_result.get("simulation_units") is not None:
            simulate_note = f"units={int(send_result['simulation_units'])}"
        if send_result.get("simulation_error"):
            simulate_note = send_result["simulation_error"]
        self.log_execution_event(
            mint, name, "buy", "simulate", bool(send_result.get("simulation_ok")),
            latency_ms=send_result.get("simulation_latency_ms"),
            slippage_bps=slippage,
            expected_out=quote.get("outAmount"),
            route_source=route_source,
            failure_reason=simulate_note,
        )
        if send_result.get("resend_count"):
            self.log_execution_event(
                mint, name, "buy", "resend", bool(send_result.get("sig")),
                latency_ms=0,
                slippage_bps=slippage,
                expected_out=quote.get("outAmount"),
                route_source=route_source,
                failure_reason=f"attempts={int(send_result['resend_count'])}",
            )
        sig = send_result.get("sig")
        self.log_execution_event(
            mint, name, "buy", "send", bool(sig),
            latency_ms=send_result.get("send_latency_ms"),
            slippage_bps=slippage,
            expected_out=quote.get("outAmount"),
            route_source=route_source,
            failure_reason=None if sig else (send_result.get("failure_reason") or "transaction-send-failed"),
        )
        if sig:
            # Compute real entry price from execution instead of DexScreener quote
            real_price = price  # fallback to scanner price
            fill_started = time.perf_counter()
            tokens_received = 0
            expected_out = int(quote.get("outAmount", 0) or 0)
            realized_slip_bps = None
            try:
                time.sleep(1.5)  # brief wait for tx to land
                tokens_received = self.get_token_balance(mint)
                if tokens_received > 0:
                    # real_price = SOL spent per token (in USD-equivalent via price)
                    # Use ratio: we paid max_buy_sol SOL for tokens_received tokens
                    # So entry_price = dexscreener_price * (expected_tokens / actual_tokens)
                    # Expected tokens at quote price = (max_buy_sol * SOL_price) / token_price
                    # Simpler: just derive from the Jupiter quote output vs actual
                    if expected_out > 0:
                        # Adjust entry price by slippage ratio (actual vs quoted)
                        slip_ratio = expected_out / tokens_received if tokens_received > 0 else 1
                        real_price = price * slip_ratio
                        realized_slip_bps = int(round((1 - (tokens_received / max(expected_out, 1))) * 10000))
                        if abs(slip_ratio - 1) > 0.01:
                            self.log_msg(f"📊 Entry adjusted: ${price:.8f} → ${real_price:.8f} (slip {(slip_ratio-1)*100:+.1f}%)")
            except Exception as _ep:
                self.log_msg(f"[WARN] Could not compute real entry: {_ep}")
            self.log_execution_event(
                mint, name, "buy", "fill", tokens_received > 0,
                latency_ms=round((time.perf_counter() - fill_started) * 1000),
                slippage_bps=realized_slip_bps,
                expected_out=expected_out,
                actual_out=tokens_received or None,
                route_source=route_source,
                failure_reason=None if tokens_received > 0 else "balance-check-unavailable",
            )

            self.positions[mint] = {
                "name":name,"entry_price":real_price,"peak_price":real_price,
                "timestamp":time.time(),"tp1_hit":False,"entry_sol":trade_sol,
                "dev_wallet": dev_wallet,
                "surge_hold_active": False,
                "surge_peak_price": real_price,
            }
            try:
                _conn = db()
                try:
                    _c = _conn.cursor()
                    _c.execute("""INSERT INTO open_positions
                                  (user_id,mint,name,entry_price,peak_price,entry_sol,tp1_hit,dev_wallet)
                                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                                  ON CONFLICT (user_id,mint) DO UPDATE
                                  SET name=EXCLUDED.name,entry_price=EXCLUDED.entry_price,
                                      peak_price=EXCLUDED.peak_price,entry_sol=EXCLUDED.entry_sol,
                                      tp1_hit=EXCLUDED.tp1_hit,dev_wallet=EXCLUDED.dev_wallet""",
                               (self.user_id, mint, name, real_price, real_price, trade_sol, 0, dev_wallet))
                    _conn.commit()
                finally:
                    db_return(_conn)
            except Exception as _e:
                self.log_msg(f"[WARN] Could not persist position to DB: {_e}")
            self.buys_this_hour     += 1
            self.consecutive_losses  = 0   # reset on successful buy
            self.log_filter(name, mint, True, f"BUY @ ${real_price:.8f} | slip={slippage}bps", score=0)
            self.log_msg(f"BUY {name} @ ${real_price:.8f} | size={trade_sol:.4f} SOL | slip={slippage}bps | solscan.io/tx/{sig}")
            self.refresh_balance()
            try:
                conn = db()
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT telegram_chat_id FROM users WHERE id=%s", (self.user_id,))
                    u = cur.fetchone()
                finally:
                    db_return(conn)
                if u and u["telegram_chat_id"]:
                    send_telegram(u["telegram_chat_id"],
                        f"🟢 <b>BUY</b> {name}\n💰 ${price:.8f}\n📊 {trade_sol} SOL\n🔗 solscan.io/tx/{sig}")
            except Exception as _e:
                print(f"[ERROR] {_e}", flush=True)
        else:
            self.log_filter(name, mint, False, "Transaction failed to send")

    def sell(self, mint, pct, reason):
        pos = self.positions.get(mint)
        if not pos:
            return
        sell_start_time = time.perf_counter()
        try:
            total = self.get_token_balance(mint)
            if total == 0:
                del self.positions[mint]
                try:
                    _conn = db()
                    try:
                        _conn.cursor().execute("DELETE FROM open_positions WHERE user_id=%s AND mint=%s", (self.user_id, mint))
                        _conn.commit()
                    finally:
                        db_return(_conn)
                except Exception as _e:
                    print(f"[ERROR] {_e}", flush=True)
                return
            amount = int(total * pct)
            if not amount:
                return
            quote_started = time.perf_counter()
            quote = self.jupiter_quote(mint, SOL_MINT, amount)
            route_source = extract_route_label(quote)
            self.log_execution_event(
                mint, pos["name"], "sell", "quote", bool(quote),
                latency_ms=round((time.perf_counter() - quote_started) * 1000),
                expected_out=(quote or {}).get("outAmount"),
                route_source=route_source,
                failure_reason=None if quote else "no-quote",
            )
            if not quote:
                # Record failed exit in enhanced risk engine
                if self.enhanced_enabled:
                    try:
                        self.risk_engine.post_trade_update(
                            user_id=self.user_id, mint=mint,
                            sol_amount=pos.get("entry_sol", 0),
                            success=False, is_exit=True, exit_failed=True,
                        )
                    except Exception:
                        pass
                return
            swap_started = time.perf_counter()
            swap_tx = self.jupiter_swap(quote)
            self.log_execution_event(
                mint, pos["name"], "sell", "build", bool(swap_tx),
                latency_ms=round((time.perf_counter() - swap_started) * 1000),
                expected_out=quote.get("outAmount"),
                route_source=route_source,
                failure_reason=None if swap_tx else "swap-build-failed",
            )
            if not swap_tx:
                if self.enhanced_enabled:
                    try:
                        self.risk_engine.post_trade_update(
                            user_id=self.user_id, mint=mint,
                            sol_amount=pos.get("entry_sol", 0),
                            success=False, is_exit=True, exit_failed=True,
                        )
                    except Exception:
                        pass
                return
            send_result = self.sign_and_send(swap_tx)
            simulate_note = None
            if send_result.get("simulation_units") is not None:
                simulate_note = f"units={int(send_result['simulation_units'])}"
            if send_result.get("simulation_error"):
                simulate_note = send_result["simulation_error"]
            self.log_execution_event(
                mint, pos["name"], "sell", "simulate", bool(send_result.get("simulation_ok")),
                latency_ms=send_result.get("simulation_latency_ms"),
                expected_out=quote.get("outAmount"),
                route_source=route_source,
                failure_reason=simulate_note,
            )
            if send_result.get("resend_count"):
                self.log_execution_event(
                    mint, pos["name"], "sell", "resend", bool(send_result.get("sig")),
                    latency_ms=0,
                    expected_out=quote.get("outAmount"),
                    route_source=route_source,
                    failure_reason=f"attempts={int(send_result['resend_count'])}",
                )
            sig = send_result.get("sig")
            self.log_execution_event(
                mint, pos["name"], "sell", "send", bool(sig),
                latency_ms=send_result.get("send_latency_ms"),
                expected_out=quote.get("outAmount"),
                route_source=route_source,
                failure_reason=None if sig else (send_result.get("failure_reason") or "transaction-send-failed"),
            )
            if sig:
                cur     = self.get_token_price(mint) or pos["entry_price"]
                pnl_pct = (cur / pos["entry_price"] - 1) * 100 if pos["entry_price"] else 0
                pnl_sol = pos["entry_sol"] * pct * (pnl_pct / 100)
                self.log_msg(f"SELL {pos['name']} {int(pct*100)}% — {reason} | {pnl_pct:+.1f}% ({pnl_sol:+.4f} SOL)")
                self.stats["total_pnl_sol"] += pnl_sol
                if pnl_sol < 0:
                    self.session_drawdown += abs(pnl_sol)
                
                # ── Enhanced observability tracking ─────────────────────────────
                if self.enhanced_enabled:
                    try:
                        total_latency = time.perf_counter() - sell_start_time
                        self.observability.record_trade(
                            user_id=self.user_id,
                            mint=mint,
                            side="sell",
                            sol_amount=pos.get("entry_sol", 0) * pct,
                            price=cur,
                            pnl_sol=pnl_sol,
                            pnl_pct=pnl_pct,
                            latency_sec=total_latency,
                            success=True,
                        )
                        self.risk_engine.post_trade_update(
                            user_id=self.user_id, mint=mint,
                            sol_amount=pos.get("entry_sol", 0),
                            success=True, pnl_pct=pnl_pct, is_exit=True,
                        )
                    except Exception as _obs_err:
                        print(f"[WARN] Observability record error: {_obs_err}")
                
                # blacklist dev if rugged (big loss, fast)
                pos_age = (time.time() - pos.get("timestamp", time.time())) / 60
                if pnl_sol < -0.05 and pos_age < 5 and pos.get("dev_wallet"):
                    blacklist_dev(pos["dev_wallet"], "auto-rug-detected")
                    self.log_msg(f"⛔ Dev blacklisted: {pos['dev_wallet'][:8]}...")
                    # Also record in enhanced rug detector
                    if self.enhanced_enabled:
                        try:
                            self.risk_engine.rug_detector.mark_rugged(mint, "fast-loss-detected")
                        except Exception:
                            pass
                if pnl_pct >= 0:
                    self.stats["wins"] += 1
                    self.consecutive_losses = 0
                else:
                    self.stats["losses"] += 1
                    self.consecutive_losses += 1
                    self.loss_mints[mint] = time.time()
                # Telegram alert
                try:
                    conn = db()
                    try:
                        _cur = conn.cursor()
                        _cur.execute("SELECT telegram_chat_id FROM users WHERE id=%s", (self.user_id,))
                        u = _cur.fetchone()
                    finally:
                        db_return(conn)
                    if u and u["telegram_chat_id"]:
                        emoji = "🟢" if pnl_pct >= 0 else "🔴"
                        send_telegram(u["telegram_chat_id"],
                            f"{emoji} <b>SELL</b> {pos['name']} {int(pct*100)}%\n"
                            f"📈 {pnl_pct:+.1f}% | {pnl_sol:+.4f} SOL\n"
                            f"📝 {reason}")
                except Exception as _e:
                    print(f"[ERROR] {_e}", flush=True)
                conn = db()
                try:
                    _cur = conn.cursor()
                    _cur.execute(
                        "INSERT INTO trades (user_id,mint,name,action,price,pnl_sol) VALUES (%s,%s,%s,%s,%s,%s)",
                        (self.user_id, mint, pos["name"], f"SELL-{reason}", cur, pnl_sol)
                    )
                    conn.commit()
                finally:
                    db_return(conn)
                if pct >= 1.0:
                    del self.positions[mint]
                    try:
                        _conn = db()
                        try:
                            _conn.cursor().execute("DELETE FROM open_positions WHERE user_id=%s AND mint=%s", (self.user_id, mint))
                            _conn.commit()
                        finally:
                            db_return(_conn)
                    except Exception as _e:
                        print(f"[ERROR] {_e}", flush=True)
                else:
                    self.positions[mint]["tp1_hit"] = True
                    try:
                        _conn = db()
                        try:
                            _conn.cursor().execute("UPDATE open_positions SET tp1_hit=1 WHERE user_id=%s AND mint=%s", (self.user_id, mint))
                            _conn.commit()
                        finally:
                            db_return(_conn)
                    except Exception as _e:
                        print(f"[ERROR] {_e}", flush=True)
                self.refresh_balance()
            else:
                # Transaction send failed
                if self.enhanced_enabled:
                    try:
                        self.risk_engine.post_trade_update(
                            user_id=self.user_id, mint=mint,
                            sol_amount=pos.get("entry_sol", 0),
                            success=False, is_exit=True, exit_failed=True,
                        )
                    except Exception:
                        pass
        except Exception as e:
            self.log_msg(f"Sell error {pos['name']}: {e}")

    def _batch_token_prices(self, mints):
        """Fetch prices for multiple mints in parallel. Returns {mint: price}."""
        prices = {}
        if not mints:
            return prices
        def _fetch(m):
            return m, self.get_token_price(m)
        with ThreadPoolExecutor(max_workers=min(len(mints), 5)) as pool:
            for m, p in pool.map(lambda m: _fetch(m), mints):
                if p:
                    prices[m] = p
        return prices

    def check_positions(self):
        s = self.settings
        mints = list(self.positions.keys())
        prices = self._batch_token_prices(mints)
        for mint in mints:
            pos = self.positions.get(mint)
            if not pos:
                continue
            cur = prices.get(mint)
            if not cur or not pos["entry_price"]:
                continue
            if cur > pos["peak_price"]:
                self.positions[mint]["peak_price"] = cur
                try:
                    _conn = db()
                    try:
                        _conn.cursor().execute("UPDATE open_positions SET peak_price=%s WHERE user_id=%s AND mint=%s", (cur, self.user_id, mint))
                        _conn.commit()
                    finally:
                        db_return(_conn)
                except Exception as _e:
                    print(f"[ERROR] {_e}", flush=True)
            ratio      = cur / pos["entry_price"]
            peak_ratio = pos["peak_price"] / pos["entry_price"]
            age_sec    = time.time() - pos["timestamp"]
            age_min    = (time.time() - pos["timestamp"]) / 60
            trail_line = pos["peak_price"] * (1 - s["trail_pct"])

            # ── Enhanced position monitoring (whale + rug detection) ─────────
            if self.enhanced_enabled:
                try:
                    should_exit, exit_reason, warnings = self.position_monitor.check_position(
                        mint=mint,
                        entry_price=pos["entry_price"],
                        current_price=cur,
                        entry_sol=pos.get("entry_sol", 0),
                        deployer_wallet=pos.get("dev_wallet"),
                    )
                    if warnings:
                        for warn in warnings[:2]:
                            self.log_msg(f"⚠️ {pos['name']}: {warn}")
                    if should_exit:
                        self.log_msg(f"🚨 ENHANCED EXIT SIGNAL: {exit_reason}")
                        self.sell(mint, 1.0, f"ENHANCED_{exit_reason}")
                        continue
                except Exception as _enh_err:
                    print(f"[WARN] Enhanced monitor error: {_enh_err}")

            # ── Listing sniper exit logic ──────────────────────────────────
            if pos.get("listing"):
                listing_tp = pos.get("listing_tp", pos["entry_price"] * 1.40)
                if cur >= listing_tp:
                    self.sell(mint, 1.0, f"LISTING TP +40%")
                    continue
                # Hard stop: 15% loss (listing can gap down if token already on Solana)
                elif ratio <= 0.85:
                    self.sell(mint, 1.0, f"LISTING SL {ratio:.2f}x")
                    continue
                # 4h time stop: if still up, take profit at market
                elif age_min >= 240 and ratio >= 1.0:
                    self.sell(mint, 1.0, f"LISTING TIME 4h {ratio:.2f}x")
                    continue
                elif age_min >= 240:
                    self.sell(mint, 1.0, f"LISTING TIME 4h (exit at loss)")
                    continue
                continue   # skip regular TP/SL logic for listing positions

            if age_sec <= 10 and ratio >= 2.0 and not pos.get("surge_hold_active"):
                pos["surge_hold_active"] = True
                pos["surge_peak_price"] = pos["peak_price"]
                self.log_msg(f"🚀 {pos['name']} entered surge hold — doubled in {age_sec:.1f}s")
            if pos.get("surge_hold_active"):
                pos["surge_peak_price"] = max(float(pos.get("surge_peak_price") or 0), pos["peak_price"])
                surge_trail_line = float(pos.get("surge_peak_price") or pos["peak_price"]) * 0.86
                if cur <= surge_trail_line:
                    self.sell(mint, 1.0, f"SURGE TRAIL {ratio:.2f}x")
                continue

            if ratio <= s["stop_loss"]:
                self.sell(mint, 1.0, f"SL {ratio:.2f}x")
            elif age_min >= s["time_stop_min"] and ratio < 1.10:
                self.sell(mint, 1.0, f"TIME {age_min:.0f}m")
            elif not pos["tp1_hit"] and ratio >= s["tp1_mult"]:
                self.sell(mint, 0.5, f"TP1 {ratio:.2f}x")
            elif pos["tp1_hit"] and ratio >= s["tp2_mult"]:
                self.sell(mint, 1.0, f"TP2 {ratio:.2f}x")
            elif (pos["tp1_hit"] or peak_ratio >= 1.3) and cur < trail_line:
                self.sell(mint, 1.0, f"TRAIL {ratio:.2f}x")

    def evaluate_signal(self, mint, name, price, mc, vol, liq, age_min, change):
        if mint in self.positions:
            return
        s = self.settings
        min_liq = float(s.get("min_liq", 0) or 0)
        liq_filter_on = min_liq > 0
        min_mc  = s.get("min_mc", 0)
        max_mc  = s.get("max_mc", 999999)
        max_age = s.get("max_age_min", 999)
        min_vol = s.get("min_vol", 0)
        min_score = s.get("min_score", 0)
        min_green_lights = max(1, int(s.get("min_green_lights", 1)))
        min_holder_growth_pct = float(s.get("min_holder_growth_pct", 30))
        min_narrative_score = int(s.get("min_narrative_score", 16))
        min_volume_spike_mult = float(s.get("min_volume_spike_mult", 6))
        late_entry_mult = float(s.get("late_entry_mult", 5.0))
        nuclear_narrative_score = int(s.get("nuclear_narrative_score", 40))
        max_hot_change = float(s.get("max_hot_change", 400.0))
        # Build signal explorer entry with detailed AI score
        _sinfo = {"vol": vol, "liq": liq, "mc": mc, "age_min": age_min, "change": change, "momentum": volume_velocity(mint, vol)}
        _sd = ai_score_detailed(_sinfo)
        score_total = _sd["total"]
        self.evals_this_hour += 1
        sig_entry = {
            "mint": mint, "name": name, "price": price,
            "mc": mc, "vol": vol, "liq": liq, "age_min": age_min, "change": change,
            "score": _sd, "passed": False, "reason": "",
            "filters": [
                {"name": "Market Cap", "passed": min_mc <= mc <= max_mc, "value": f"${mc:,.0f}", "threshold": f"${min_mc:,}\u2013${max_mc:,}"},
                {"name": "Liquidity", "passed": (not liq_filter_on) or liq >= min_liq, "value": f"${liq:,.0f}", "threshold": "off" if not liq_filter_on else f"\u2265 ${min_liq:,.0f}"},
                {"name": "Token Age", "passed": age_min <= max_age, "value": f"{age_min:.0f}m", "threshold": f"\u2264 {max_age}m"},
                {"name": "Price Change", "passed": 0 < change <= max_hot_change, "value": f"{change:+.0f}%", "threshold": f"> 0% and \u2264 {max_hot_change:.0f}%"},
                {"name": "Volume", "passed": vol >= min_vol, "value": f"${vol:,.0f}", "threshold": f"\u2265 ${min_vol:,}"},
                {"name": "AI Score", "passed": score_total >= min_score, "value": f"{score_total}/100", "threshold": f"\u2265 {min_score}"},
            ],
            "ts": time.strftime("%H:%M:%S"), "timestamp": time.time(),
        }
        liq_text = f"min {min_liq:,.0f}" if liq_filter_on else "filter off"
        print(f"[EVAL U{self.user_id}] {name} MC=${mc:,.0f}({min_mc:,}-{max_mc:,}) Liq=${liq:,.0f}({liq_text}) Age={age_min:.0f}m(max {max_age}) Chg={change:+.0f}% Vol=${vol:,.0f}(min {min_vol:,}) Score={score_total}(min {min_score})", flush=True)
        if not (min_mc <= mc <= max_mc):
            sig_entry["reason"] = f"MC ${mc:,.0f} outside [{min_mc:,}\u2013{max_mc:,}]"
            self.log_signal_entry(sig_entry)
            self.log_filter(name, mint, False, sig_entry["reason"])
            return
        if liq_filter_on and liq < min_liq:
            sig_entry["reason"] = f"Liquidity ${liq:,.0f} < min ${min_liq:,.0f}"
            self.log_signal_entry(sig_entry)
            self.log_filter(name, mint, False, sig_entry["reason"])
            return
        if age_min > max_age:
            sig_entry["reason"] = f"Age {age_min:.0f}m > max {max_age}m"
            self.log_signal_entry(sig_entry)
            self.log_filter(name, mint, False, sig_entry["reason"])
            return
        if change <= 0:
            sig_entry["reason"] = f"1h change {change:.0f}% not positive enough"
            self.log_signal_entry(sig_entry)
            self.log_filter(name, mint, False, sig_entry["reason"])
            return
        if change > max_hot_change:
            sig_entry["reason"] = f"1h change {change:.0f}% too extended"
            self.log_signal_entry(sig_entry)
            self.log_filter(name, mint, False, sig_entry["reason"])
            return
        if vol < min_vol:
            sig_entry["reason"] = f"Volume ${vol:,.0f} < min ${min_vol:,}"
            self.log_signal_entry(sig_entry)
            self.log_filter(name, mint, False, sig_entry["reason"])
            return
        if score_total < min_score:
            sig_entry["reason"] = f"AI Score {score_total} < min {min_score}"
            self.log_signal_entry(sig_entry)
            self.log_filter(name, mint, False, sig_entry["reason"])
            return
        central_now, can_trade = central_trading_window()
        if not can_trade:
            local_clock = central_now.strftime("%I:%M %p").lstrip("0")
            sig_entry["reason"] = f"Trading window closed ({local_clock} CT) — resumes at 6:00 AM CT"
            self.log_signal_entry(sig_entry)
            self.log_filter(name, mint, False, sig_entry["reason"])
            return
        intel = ensure_token_intel(mint, base_info={
            "mint": mint,
            "name": name,
            "price": price,
            "mc": mc,
            "vol": vol,
            "liq": liq,
            "age_min": age_min,
            "change": change,
            "score": score_total,
        })
        threat_score = int(intel.get("threat_risk_score") or 0)
        threat_flags = intel.get("threat_flags") or []
        can_exit = intel.get("can_exit")
        transfer_hook_enabled = bool(intel.get("transfer_hook_enabled"))
        whale_score = int(intel.get("whale_score") or 0)
        whale_action_score = int(intel.get("whale_action_score") or 0)
        checklist = build_three_signal_checklist(_sinfo, intel, settings=s)
        intel["checklist"] = checklist["items"]
        intel["green_lights"] = checklist["green_lights"]
        adaptive_green_override = (
            checklist["green_lights"] + 1 >= min_green_lights and
            whale_score >= 55 and
            whale_action_score >= 40 and
            threat_score < 25 and
            score_total >= (min_score + 5)
        )
        sig_entry["intel"] = intel
        sig_entry["filters"].extend([
            {
                "name": "Tracked Wallet Edge",
                "passed": checklist["items"][0]["passed"],
                "value": checklist["items"][0]["value"],
                "threshold": checklist["items"][0]["threshold"],
            },
            {
                "name": "Volume / Holder Acceleration",
                "passed": checklist["items"][1]["passed"],
                "value": f"{float(intel.get('volume_spike_ratio') or 0):.1f}x | {float(intel.get('holder_growth_1h') or 0):+.0f}%",
                "threshold": f">= {min_volume_spike_mult:.0f}x or >= {min_holder_growth_pct:.0f}%",
            },
            {
                "name": "Narrative Timing",
                "passed": checklist["items"][2]["passed"],
                "value": f"{intel.get('narrative_score', 0)}",
                "threshold": f">= {min_narrative_score}",
            },
            {
                "name": "Whale / Entity Score",
                "passed": whale_score >= 28,
                "value": f"w {intel.get('whale_score', 0)} · a {intel.get('whale_action_score', 0)} · cc {intel.get('cluster_confidence', 0)}",
                "threshold": "whale >= 28",
            },
            {
                "name": "Green Lights",
                "passed": checklist["green_lights"] >= min_green_lights or adaptive_green_override,
                "value": f"{checklist['green_lights']}/3" + (" + whale override" if adaptive_green_override else ""),
                "threshold": f">= {min_green_lights} or strong whale + low threat",
            },
            {
                "name": "Late Entry Guard",
                "passed": float(intel.get("max_multiple") or 1) <= late_entry_mult or int(intel.get("narrative_score") or 0) >= nuclear_narrative_score,
                "value": f"{float(intel.get('max_multiple') or 1):.2f}x seen",
                "threshold": f"<= {late_entry_mult:.1f}x unless narrative >= {nuclear_narrative_score}",
            },
            {
                "name": "Threat / Exit Risk",
                "passed": threat_score < 60 and can_exit is not False and not transfer_hook_enabled,
                "value": f"{threat_score}/100 {' · '.join(threat_flags[:2]) if threat_flags else 'clean'}",
                "threshold": "score < 60, exit route available, no transfer hook",
            },
        ])
        if transfer_hook_enabled:
            sig_entry["reason"] = "Transfer hook / Token-2022 exit risk"
            self.log_signal_entry(sig_entry)
            self.log_filter(name, mint, False, sig_entry["reason"])
            return
        if can_exit is False:
            sig_entry["reason"] = "No exit route available"
            self.log_signal_entry(sig_entry)
            self.log_filter(name, mint, False, sig_entry["reason"])
            return
        if threat_score >= 60:
            sig_entry["reason"] = f"Threat risk {threat_score}/100 ({', '.join(threat_flags[:3]) or 'risk'})"
            self.log_signal_entry(sig_entry)
            self.log_filter(name, mint, False, sig_entry["reason"])
            return
        if checklist["green_lights"] < min_green_lights and not adaptive_green_override:
            sig_entry["reason"] = f"Only {checklist['green_lights']}/3 green lights"
            self.log_signal_entry(sig_entry)
            self.log_filter(name, mint, False, sig_entry["reason"])
            return
        if float(intel.get("max_multiple") or 1) > late_entry_mult and int(intel.get("narrative_score") or 0) < nuclear_narrative_score:
            sig_entry["reason"] = f"Late entry ({float(intel.get('max_multiple') or 1):.2f}x) without nuclear narrative"
            self.log_signal_entry(sig_entry)
            self.log_filter(name, mint, False, sig_entry["reason"])
            return
        sig_entry["passed"] = True
        sig_entry["reason"] = "Passed all filters"
        self.log_signal_entry(sig_entry)
        self.log_msg(f"SIGNAL {name} | MC:${mc:,.0f} Liq:${liq:,.0f} Age:{age_min:.0f}m Chg:{change:+.0f}% Score:{score_total}")
        self.log_filter(name, mint, True, "Signal passed all filters")
        self.buy(mint, name, price, liq=liq, dev_wallet=intel.get("deployer_wallet"), age_min=age_min)

    def cashout_all(self):
        self.log_msg("CASHOUT ALL — selling all positions")
        for mint in list(self.positions.keys()):
            self.sell(mint, 1.0, "CASHOUT")

    def run(self):
        # Retry balance fetch on startup — RPC may be degraded
        for attempt in range(5):
            self.refresh_balance()
            if self.sol_balance > 0:
                break
            self.log_msg(f"[WARN] Balance read 0.0 SOL, retrying ({attempt+1}/5)...")
            time.sleep(2)
        self.start_balance = self.sol_balance
        self.peak_balance  = self.sol_balance
        self.log_msg(f"Bot started | Wallet: {self.wallet} | Balance: {self.sol_balance:.4f} SOL")
        while self.running:
            try:
                self.maybe_relax_guards()
                if self.should_stop():
                    self.cashout_all()
                    self.running = False
                    self.record_perf_fee()
                    break
                if self.positions:
                    self.check_positions()
                time.sleep(3)
            except Exception as e:
                self.log_msg(f"Loop error: {e}")
                time.sleep(3)

    def record_perf_fee(self):
        if self.perf_fee_recorded:
            return
        pnl = self.stats["total_pnl_sol"]
        if pnl <= 0:
            self.perf_fee_recorded = True
            return
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT plan, email FROM users WHERE id=%s", (self.user_id,))
            row = cur.fetchone()
            plan = row["plan"] if row else "basic"
            email = row["email"] if row else ""
        finally:
            db_return(conn)
        if is_admin(email):
            self.perf_fee_recorded = True
            return  # no performance fee on admin accounts
        pct = PLAN_LIMITS.get(effective_plan(plan, email), PLAN_LIMITS["basic"])["perf_fee"]
        fee_sol = pnl * pct
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO perf_fees (session_id,user_id,pnl_sol,fee_sol)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (session_id) DO NOTHING
                """,
                (self.session_id, self.user_id, pnl, fee_sol)
            )
            conn.commit()
        finally:
            db_return(conn)
        self.perf_fee_recorded = True
        self.log_msg(f"Performance fee: {fee_sol:.4f} SOL ({int(pct*100)}% of {pnl:.4f} SOL profit) — collecting…")
        if FEE_WALLET:
            sig = send_sol(self.keypair, FEE_WALLET, fee_sol)
            if sig:
                self.log_msg(f"✅ Fee collected: {fee_sol:.4f} SOL → solscan.io/tx/{sig}")
                conn = db()
                try:
                    cur = conn.cursor()
                    cur.execute("UPDATE perf_fees SET charged=1 WHERE session_id=%s", (self.session_id,))
                    conn.commit()
                finally:
                    db_return(conn)
            else:
                self.log_msg(f"⚠️ Fee collection failed — {fee_sol:.4f} SOL logged for manual collection")

# ── Shared DexScreener scanner ─────────────────────────────────────────────────
# ── Momentum tracking ──────────────────────────────────────────────────────────
# Stores recent volume snapshots per token to detect momentum spikes
_volume_history = {}   # mint -> [(timestamp, vol), ...]

def volume_velocity(mint, current_vol):
    """Returns volume acceleration score 0-100. Measures rate of volume growth."""
    now = time.time()
    hist = _volume_history.get(mint, [])
    hist = [(t, v) for t, v in hist if now - t < 300]
    hist.append((now, current_vol))
    _volume_history[mint] = hist
    if len(hist) < 3: return 0
    # compare last third vs first third
    third = max(1, len(hist)//3)
    early_avg = sum(v for _,v in hist[:third]) / third
    late_avg  = sum(v for _,v in hist[-third:]) / third
    if early_avg <= 0: return 0
    accel = (late_avg - early_avg) / early_avg * 100
    return min(int(accel), 100)

# backward compat alias
def get_momentum_score(mint, current_vol):
    return volume_velocity(mint, current_vol)

# ── Known profitable whale wallets to track ────────────────────────────────────
WHALE_WALLETS = [
    "GSTnwUkbsGqXbBHzA8sHsm7FijhKb4CDGS1zAxZAVwsV",  # pump.fun top sniper
    "HVWBLYoq5Nvh8vqKnFDqbGAXkLCKiMoRcVGR4JEGMEX",  # meme coin sniper
    "AKnL4NNf3DGWZJS6cPknBuEGnVsV4A4m33NDcj8ppump",  # active degen wallet
    "5tzFkiKscXHK5ZXCGbGuykB2NZuNQn8aVJHsZcFKpNGe",  # dex whale
]
WHALE_LABELS = {
    "GSTnwUkbsGqXbBHzA8sHsm7FijhKb4CDGS1zAxZAVwsV": "Pump Sniper",
    "HVWBLYoq5Nvh8vqKnFDqbGAXkLCKiMoRcVGR4JEGMEX": "Meme Sniper",
    "AKnL4NNf3DGWZJS6cPknBuEGnVsV4A4m33NDcj8ppump": "Degen Alpha",
    "5tzFkiKscXHK5ZXCGbGuykB2NZuNQn8aVJHsZcFKpNGe": "DEX Whale",
}
_whale_seen  = set()
_whale_mints = {}   # mint -> last_seen timestamp (dedup per token)
_whale_buys  = deque(maxlen=200)  # recent whale buys for dashboard

SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
JUPITER_ROUTER_PROGRAM = "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB"
WORMHOLE_CORE_BRIDGE = "worm2ZoG2kUd4vFXhvjh93UUH596ayRfgQ2MgjNMTth"
KNOWN_INFRA_LABELS = {
    SPL_TOKEN_PROGRAM: "spl-token",
    TOKEN_2022_PROGRAM: "token-2022",
    PUMP_PROGRAM: "pump-fun",
    RAYDIUM_AMM: "raydium-amm",
    JUPITER_ROUTER_PROGRAM: "jupiter-router",
    WORMHOLE_CORE_BRIDGE: "wormhole-bridge",
}
INTEL_TRACK_WINDOW_SEC = 3600
INTEL_REFRESH_SEC = 75
INTEL_MAX_TRACKED = 250
_token_intel_cache = {}
_deployer_stats_cache = {}
_token_intel_lock = threading.Lock()
_holder_history = {}
NARRATIVE_KEYWORDS = {
    "dog": ["dog", "inu", "shib", "woof", "bonk"],
    "cat": ["cat", "kitty", "meow"],
    "frog": ["pepe", "frog"],
    "politics": ["trump", "maga", "biden", "election", "sec", "fed", "tariff"],
    "freedom": ["free", "freedom", "unban", "justice", "banned", "lawsuit"],
    "sports": ["nba", "nfl", "soccer", "ufc", "march", "madness", "cup"],
    "holiday": ["xmas", "christmas", "santa", "easter", "halloween", "turkey"],
    "ai": ["ai", "gpt", "agent", "bot", "terminal"],
    "solana": ["sol", "solana", "jup", "jupiter", "raydium", "pump"],
    "celebrity": ["elon", "drake", "ye", "kanye", "mrbeast"],
}


def _json_load(value, default):
    if value in (None, "", b""):
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        loaded = json.loads(value)
        return loaded if loaded is not None else default
    except Exception:
        return default


def _dedupe_keep_order(items):
    seen = set()
    out = []
    for item in items or []:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def infer_infrastructure_labels(wallets):
    labels = []
    for wallet in wallets or []:
        if not wallet:
            continue
        label = KNOWN_INFRA_LABELS.get(wallet)
        if label:
            labels.append(label)
        if wallet in WHALE_WALLETS:
            labels.append("tracked-whale")
    return _dedupe_keep_order(labels)


def jupiter_quote_direct(input_mint, output_mint, amount, slippage_bps=5000):
    urls = [
        f"https://lite-api.jup.ag/swap/v1/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount}&slippageBps={slippage_bps}",
        f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount}&slippageBps={slippage_bps}",
    ]
    for url in urls:
        try:
            data = requests.get(url, timeout=6).json()
            if "error" not in data:
                return data
        except Exception:
            pass
    return None


def extract_route_label(quote):
    try:
        route = (quote or {}).get("routePlan") or []
        if route:
            swap_info = (route[0] or {}).get("swapInfo") or {}
            label = swap_info.get("label") or swap_info.get("ammKey")
            if label:
                return str(label)
    except Exception:
        pass
    return "jupiter"


def _nested_lookup(value, keys):
    wanted = {str(k).lower() for k in (keys or [])}
    if isinstance(value, dict):
        for raw_key, raw_val in value.items():
            if str(raw_key).lower() in wanted and raw_val not in (None, ""):
                return raw_val
            found = _nested_lookup(raw_val, wanted)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for item in value:
            found = _nested_lookup(item, wanted)
            if found not in (None, ""):
                return found
    return None


def _coerce_int(value, default=0):
    try:
        if value in (None, ""):
            return int(default)
        if isinstance(value, bool):
            return int(value)
        return int(float(value))
    except Exception:
        return int(default)


def _coerce_bool(value):
    if isinstance(value, bool):
        return value
    if value in (None, "", 0, "0", "false", "False", "FALSE", "none", "None"):
        return False
    return True


def _format_rpc_error(value):
    if value in (None, "", {}):
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, separators=(",", ":"), default=str)
    except Exception:
        return str(value)


def _extract_token2022_risk(info):
    extensions = info.get("extensions") or []
    ext_blob = json.dumps(extensions, separators=(",", ":"), default=str).lower() if extensions else ""
    transfer_hook_enabled = ("transferhook" in ext_blob) or ("transfer_hook" in ext_blob)
    permanent_delegate_enabled = ("permanentdelegate" in ext_blob) or ("permanent_delegate" in ext_blob)
    mint_close_authority_enabled = ("mintcloseauthority" in ext_blob) or ("mint_close_authority" in ext_blob)
    transfer_fee_bps = 0
    raw_fee = _nested_lookup(
        extensions,
        {
            "transferFeeBasisPoints",
            "transfer_fee_basis_points",
            "basisPoints",
            "basis_points",
            "feeBasisPoints",
            "fee_basis_points",
        },
    )
    if raw_fee not in (None, "") and "transferfee" in ext_blob:
        transfer_fee_bps = max(0, _coerce_int(raw_fee, 0))
    return {
        "transfer_hook_enabled": bool(transfer_hook_enabled),
        "permanent_delegate_enabled": bool(permanent_delegate_enabled),
        "mint_close_authority_enabled": bool(mint_close_authority_enabled),
        "transfer_fee_bps": transfer_fee_bps,
    }


def _now_utc():
    return datetime.utcnow()


def _to_iso(dt):
    if not dt:
        return ""
    if isinstance(dt, str):
        return dt
    return dt.isoformat()


def _from_iso(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def get_helius_api_key():
    try:
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(HELIUS_RPC)
        api_key = parse_qs(parsed.query).get("api-key", [""])[0]
        if api_key:
            return api_key
    except Exception:
        pass
    tail = HELIUS_RPC.split("/")[-1]
    return "" if "?" in tail else tail


def rpc_call(method, params=None, timeout=10):
    started = time.perf_counter()
    try:
        resp = requests.post(HELIUS_RPC, json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or [],
        }, headers=HEADERS, timeout=timeout)
        if resp.ok:
            body = resp.json()
            record_rpc_health("helius", True, round((time.perf_counter() - started) * 1000), method)
            return body.get("result")
        record_rpc_health("helius", False, round((time.perf_counter() - started) * 1000), method)
    except Exception as e:
        record_rpc_health("helius", False, round((time.perf_counter() - started) * 1000), method)
        print(f"[RPC] {method} failed: {e}", flush=True)
    return None


def helius_address_transactions(address, limit=50, tx_type=None, timeout=10):
    api_key = get_helius_api_key()
    if not api_key:
        return []
    params = {"api-key": api_key, "limit": int(limit)}
    if tx_type:
        params["type"] = tx_type
    try:
        resp = requests.get(
            f"https://api.helius.xyz/v0/addresses/{address}/transactions",
            params=params,
            headers=HEADERS,
            timeout=timeout,
        )
        data = resp.json() if resp.ok else []
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[HELIUS] {address} tx fetch failed: {e}", flush=True)
        return []


def extract_social_links(pair):
    info = (pair or {}).get("info") or {}
    links = []
    for website in info.get("websites") or []:
        url = (website or {}).get("url")
        if url:
            links.append(url.strip())
    for social in info.get("socials") or []:
        url = (social or {}).get("url") or (social or {}).get("handle")
        if url:
            links.append(str(url).strip())
    return _dedupe_keep_order(links)


def social_keys(links):
    keys = []
    for raw in links or []:
        item = str(raw or "").strip().lower()
        if not item:
            continue
        for prefix in ("https://", "http://"):
            if item.startswith(prefix):
                item = item[len(prefix):]
        if item.startswith("www."):
            item = item[4:]
        item = item.split("?", 1)[0].rstrip("/")
        for marker in ("x.com/", "twitter.com/", "t.me/", "telegram.me/", "discord.gg/"):
            if marker in item:
                item = item.split(marker, 1)[1]
        keys.append(item)
    return _dedupe_keep_order(keys)


def market_mood_snapshot():
    now = _now_utc()
    tags = []
    if now.month in (11, 12):
        tags.extend(["holiday", "christmas"])
    if now.month in (3, 4):
        tags.extend(["tax", "sports"])
    if now.month == 10:
        tags.extend(["holiday", "halloween"])
    if now.weekday() >= 5:
        tags.append("weekend")
    return tags


def infer_narrative_tags(name, symbol, socials=None):
    haystack = " ".join([str(name or ""), str(symbol or "")] + list(socials or [])).lower()
    tags = []
    for tag, keywords in NARRATIVE_KEYWORDS.items():
        if any(word in haystack for word in keywords):
            tags.append(tag)
    return _dedupe_keep_order(tags)


def extract_wallet_flow_from_swaps(txns, mint):
    buyers = []
    sellers = []
    unique_buyers = []
    unique_sellers = []
    ordered = sorted((tx for tx in txns if isinstance(tx, dict)), key=lambda tx: tx.get("timestamp") or 0)
    for tx in ordered:
        fee_payer = tx.get("feePayer", "")
        swap = (tx.get("events") or {}).get("swap") or {}
        if not fee_payer or not swap:
            continue
        token_outputs = swap.get("tokenOutputs") or []
        token_inputs = swap.get("tokenInputs") or []
        native_input = swap.get("nativeInput") or {}
        native_output = swap.get("nativeOutput") or {}
        if any(t.get("mint") == mint for t in token_outputs):
            buyers.append({
                "wallet": fee_payer,
                "timestamp": tx.get("timestamp") or 0,
                "sol": round(float(native_input.get("amount") or 0) / 1e9, 4),
            })
            if fee_payer not in unique_buyers:
                unique_buyers.append(fee_payer)
        elif any(t.get("mint") == mint for t in token_inputs):
            sellers.append({
                "wallet": fee_payer,
                "timestamp": tx.get("timestamp") or 0,
                "sol": round(float(native_output.get("amount") or 0) / 1e9, 4),
            })
            if fee_payer not in unique_sellers:
                unique_sellers.append(fee_payer)
    first_buyers = unique_buyers[:10]
    smart_wallet_first10 = sum(1 for wallet in first_buyers if wallet in WHALE_WALLETS)
    smart_wallet_buys = sum(1 for wallet in unique_buyers if wallet in WHALE_WALLETS)
    deployer_candidate = None
    for tx in ordered:
        fee_payer = tx.get("feePayer", "")
        if fee_payer and fee_payer not in (mint, SOL_MINT, SPL_TOKEN_PROGRAM, PUMP_PROGRAM) and fee_payer not in KNOWN_INFRA_LABELS:
            deployer_candidate = fee_payer
            break
    return {
        "buyers": buyers,
        "sellers": sellers,
        "first_buyers": first_buyers,
        "smart_wallet_buys": smart_wallet_buys,
        "smart_wallet_first10": smart_wallet_first10,
        "deployer_candidate": deployer_candidate,
    }


def resolve_deployer_wallet(mint, txns=None):
    txns = txns if txns is not None else helius_address_transactions(mint, limit=40, timeout=8)
    flow = extract_wallet_flow_from_swaps(txns, mint)
    if flow.get("deployer_candidate"):
        return flow["deployer_candidate"]
    for tx in sorted((tx for tx in txns if isinstance(tx, dict)), key=lambda tx: tx.get("timestamp") or 0):
        fee_payer = tx.get("feePayer", "")
        if fee_payer and fee_payer not in (mint, SOL_MINT, SPL_TOKEN_PROGRAM, PUMP_PROGRAM) and fee_payer not in KNOWN_INFRA_LABELS:
            return fee_payer
    return None


def get_token_holder_count(mint, txns=None):
    count = 0
    try:
        largest = rpc_call("getTokenLargestAccounts", [mint], timeout=8) or {}
        rows = largest.get("value", []) if isinstance(largest, dict) else []
        count = len([row for row in rows if float(row.get("uiAmount") or 0) > 0])
    except Exception:
        pass
    txns = txns or []
    wallets = set()
    for tx in txns:
        fee_payer = (tx or {}).get("feePayer")
        if fee_payer:
            wallets.add(fee_payer)
    return max(count, len(wallets))


def volume_spike_ratio(mint, current_vol):
    now = time.time()
    hist = _volume_history.get(mint, [])
    hist = [(t, v) for t, v in hist if now - t < 300]
    if current_vol:
        hist.append((now, current_vol))
    _volume_history[mint] = hist
    previous = [v for _, v in hist[:-1] if v > 0]
    if not previous:
        return 1.0 if current_vol else 0.0
    baseline = max(min(previous), 1)
    return round(float(current_vol or 0) / baseline, 2)


def social_reuse_score(keys):
    keys = set(keys or [])
    if not keys:
        return 0
    hits = 0
    for cached in list(_token_intel_cache.values()):
        if float(cached.get("max_multiple") or 1) < 2:
            continue
        if keys.intersection(set(cached.get("social_keys") or [])):
            hits += 1
    if hits < 2:
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT social_keys, max_multiple
                FROM token_intel
                WHERE social_keys IS NOT NULL AND social_keys <> ''
                ORDER BY last_updated DESC
                LIMIT 200
            """)
            for row in cur.fetchall():
                if float(row.get("max_multiple") or 1) < 2:
                    continue
                if keys.intersection(set(_json_load(row.get("social_keys"), []))):
                    hits += 1
        finally:
            db_return(conn)
    return min(20, hits * 5)


def compute_whale_scores(intel):
    intel = intel or {}
    deployer_score = int(intel.get("deployer_score") or 0)
    smart_first10 = int(intel.get("smart_wallet_first10") or 0)
    first_buyer_count = int(intel.get("first_buyer_count") or 0)
    holder_growth = max(0.0, float(intel.get("holder_growth_1h") or 0))
    volume_spike = max(0.0, float(intel.get("volume_spike_ratio") or 0))
    narrative_score = int(intel.get("narrative_score") or 0)
    infra_labels = [label for label in (intel.get("infra_labels") or []) if label and label != "tracked-whale"]
    infra_penalty = min(35, len(infra_labels) * 8)
    cluster_confidence = min(
        100,
        (20 if intel.get("deployer_wallet") else 0) +
        min(35, deployer_score // 2) +
        min(25, smart_first10 * 12) +
        min(20, first_buyer_count * 2),
    )
    whale_score = min(
        100,
        max(
            0,
            round(
                deployer_score * 0.28 +
                min(20, volume_spike * 2.2) +
                min(16, holder_growth / 8.0) +
                min(18, smart_first10 * 7) +
                min(10, first_buyer_count) +
                min(10, narrative_score * 0.18) -
                infra_penalty
            )
        )
    )
    whale_action_score = min(
        100,
        max(
            0,
            round(
                min(28, volume_spike * 2.8) +
                min(22, holder_growth / 6.0) +
                min(20, smart_first10 * 8) +
                min(12, first_buyer_count * 1.2) +
                min(10, deployer_score * 0.12) -
                infra_penalty
            )
        )
    )
    return {
        "whale_score": whale_score,
        "whale_action_score": whale_action_score,
        "cluster_confidence": cluster_confidence,
        "infra_penalty": infra_penalty,
    }


def compute_deployer_reputation(stats):
    if not stats or not stats.get("deployer_wallet"):
        return 0
    score = 0
    score += int(stats.get("wins_2x") or 0) * 8
    score += int(stats.get("wins_5x") or 0) * 10
    score += int(stats.get("wins_10x") or 0) * 14
    score += min(20, int(float(stats.get("best_multiple") or 1) * 2))
    score += 8 if float(stats.get("last_dormant_days") or 0) >= 180 else 0
    if int(stats.get("launches_total") or 0) > 5 and int(stats.get("wins_2x") or 0) == 0:
        score = max(0, score - 8)
    return min(100, score)


def get_deployer_stats(deployer_wallet):
    if not deployer_wallet:
        return {}
    cached = _deployer_stats_cache.get(deployer_wallet)
    if cached:
        return dict(cached)
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM deployer_stats WHERE deployer_wallet=%s", (deployer_wallet,))
        row = cur.fetchone()
    finally:
        db_return(conn)
    stats = dict(row) if row else {
        "deployer_wallet": deployer_wallet,
        "launches_total": 0,
        "wins_2x": 0,
        "wins_5x": 0,
        "wins_10x": 0,
        "best_multiple": 1,
        "last_token_mint": None,
        "reputation_score": 0,
        "last_dormant_days": 0,
        "first_seen_at": _now_utc(),
        "last_seen_at": _now_utc(),
    }
    stats["reputation_score"] = compute_deployer_reputation(stats)
    _deployer_stats_cache[deployer_wallet] = dict(stats)
    return stats


def persist_deployer_stats(stats):
    if not stats or not stats.get("deployer_wallet"):
        return
    stats = dict(stats)
    stats["reputation_score"] = compute_deployer_reputation(stats)
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO deployer_stats (
                deployer_wallet, launches_total, wins_2x, wins_5x, wins_10x,
                best_multiple, last_token_mint, reputation_score, last_dormant_days,
                first_seen_at, last_seen_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (deployer_wallet) DO UPDATE SET
                launches_total=EXCLUDED.launches_total,
                wins_2x=EXCLUDED.wins_2x,
                wins_5x=EXCLUDED.wins_5x,
                wins_10x=EXCLUDED.wins_10x,
                best_multiple=EXCLUDED.best_multiple,
                last_token_mint=EXCLUDED.last_token_mint,
                reputation_score=EXCLUDED.reputation_score,
                last_dormant_days=EXCLUDED.last_dormant_days,
                first_seen_at=LEAST(deployer_stats.first_seen_at, EXCLUDED.first_seen_at),
                last_seen_at=GREATEST(deployer_stats.last_seen_at, EXCLUDED.last_seen_at)
        """, (
            stats["deployer_wallet"],
            int(stats.get("launches_total") or 0),
            int(stats.get("wins_2x") or 0),
            int(stats.get("wins_5x") or 0),
            int(stats.get("wins_10x") or 0),
            float(stats.get("best_multiple") or 1),
            stats.get("last_token_mint"),
            int(stats.get("reputation_score") or 0),
            float(stats.get("last_dormant_days") or 0),
            _from_iso(stats.get("first_seen_at")) or _now_utc(),
            _from_iso(stats.get("last_seen_at")) or _now_utc(),
        ))
        conn.commit()
    finally:
        db_return(conn)
    _deployer_stats_cache[stats["deployer_wallet"]] = dict(stats)


def register_deployer_launch(deployer_wallet, mint):
    if not deployer_wallet:
        return {}
    stats = get_deployer_stats(deployer_wallet)
    now = _now_utc()
    last_seen = _from_iso(stats.get("last_seen_at"))
    if stats.get("last_token_mint") != mint:
        stats["launches_total"] = int(stats.get("launches_total") or 0) + 1
    if last_seen:
        stats["last_dormant_days"] = round(max(0.0, (now - last_seen).total_seconds() / 86400), 1)
    stats["deployer_wallet"] = deployer_wallet
    stats["last_token_mint"] = mint
    stats["last_seen_at"] = now
    stats["first_seen_at"] = _from_iso(stats.get("first_seen_at")) or now
    persist_deployer_stats(stats)
    return stats


def update_holder_history(mint, holder_count):
    now = time.time()
    hist = _holder_history.get(mint, [])
    hist = [(t, v) for t, v in hist if now - t < INTEL_TRACK_WINDOW_SEC]
    hist.append((now, int(holder_count or 0)))
    _holder_history[mint] = hist
    baseline = next((v for _, v in hist if v > 0), 0)
    if not baseline:
        return 0.0
    return round(((holder_count - baseline) / baseline) * 100, 1)


def build_narrative_profile(name, symbol, pair=None, deployer_stats=None, social_keys_list=None):
    socials = social_keys_list or social_keys(extract_social_links(pair))
    tags = infer_narrative_tags(name, symbol, socials)
    mood = set(market_mood_snapshot())
    aligned = [tag for tag in tags if tag in mood or (tag == "holiday" and {"christmas", "halloween"} & mood)]
    score = len(tags) * 6 + len(aligned) * 8 + min(12, len(socials) * 4)
    if float((deployer_stats or {}).get("last_dormant_days") or 0) >= 180:
        score += 8
    score += social_reuse_score(socials)
    return {
        "tags": _dedupe_keep_order(tags),
        "aligned_tags": _dedupe_keep_order(aligned),
        "score": min(100, score),
        "social_keys": socials,
    }


def build_three_signal_checklist(info, intel, settings=None):
    settings = settings or PRESETS["balanced"]
    deployer_threshold = max(10, int(settings.get("min_narrative_score", 20)) - 4)
    whale_override = int(intel.get("whale_score") or 0) >= 45 or int(intel.get("whale_action_score") or 0) >= 35
    price_momentum_override = (
        10 <= float(info.get("change") or 0) <= 25 and
        float(info.get("momentum") or 0) >= 12 and
        (
            whale_override or
            int(intel.get("narrative_score") or 0) >= int(settings.get("nuclear_narrative_score", 40))
        )
    )
    deployer_pass = (
        (intel.get("deployer_wallet") in WHALE_WALLETS) or
        int(intel.get("deployer_score") or 0) >= deployer_threshold or
        int(intel.get("smart_wallet_first10") or 0) > 0 or
        whale_override
    )
    volume_pass = (
        float(intel.get("volume_spike_ratio") or 0) >= float(settings.get("min_volume_spike_mult", 10)) or
        float(intel.get("holder_growth_1h") or 0) >= float(settings.get("min_holder_growth_pct", 50)) or
        price_momentum_override
    )
    narrative_floor = max(8, int(settings.get("min_narrative_score", 20)) - 3)
    aligned_tags = [tag for tag in (intel.get("narrative_tags") or []) if tag in set(market_mood_snapshot())]
    narrative_pass = (
        int(intel.get("narrative_score") or 0) >= narrative_floor or
        bool(aligned_tags)
    )
    items = [
        {
            "name": "Tracked deployer / first-buyer quality",
            "passed": deployer_pass,
            "value": f"{intel.get('deployer_score', 0)} rep | {intel.get('smart_wallet_first10', 0)} smart in first10",
            "threshold": f"tracked wallet, rep >= {deployer_threshold}, strong whale score, or smart first10",
        },
        {
            "name": "Volume / holder acceleration",
            "passed": volume_pass,
            "value": f"{float(intel.get('volume_spike_ratio') or 0):.1f}x vol | {float(intel.get('holder_growth_1h') or 0):+.0f}% holders",
            "threshold": (
                f">= {settings.get('min_volume_spike_mult', 10)}x, "
                f">= {settings.get('min_holder_growth_pct', 50)}%, "
                "or 10-25% price change + strong momentum + whale/narrative confirmation"
            ),
        },
        {
            "name": "Narrative timing fit",
            "passed": narrative_pass,
            "value": f"{intel.get('narrative_score', 0)} score",
            "threshold": f">= {narrative_floor} or aligned with market mood",
        },
    ]
    return {"items": items, "green_lights": sum(1 for item in items if item["passed"])}


def inspect_token_risk(mint, age_min=0, first_liq=0, latest_liq=0):
    owner = ""
    info = {}
    transfer_hook_enabled = False
    permanent_delegate_enabled = False
    mint_close_authority_enabled = False
    transfer_fee_bps = 0
    freeze_authority = None
    mint_authority = None
    can_exit = None
    try:
        result = rpc_call("getAccountInfo", [mint, {"encoding": "jsonParsed"}], timeout=8) or {}
        value = result.get("value") if isinstance(result, dict) else {}
        owner = (value or {}).get("owner") or ""
        info = (((value or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        token_2022_risk = _extract_token2022_risk(info)
        transfer_hook_enabled = bool(token_2022_risk.get("transfer_hook_enabled"))
        permanent_delegate_enabled = bool(token_2022_risk.get("permanent_delegate_enabled"))
        mint_close_authority_enabled = bool(token_2022_risk.get("mint_close_authority_enabled"))
        transfer_fee_bps = max(0, _coerce_int(token_2022_risk.get("transfer_fee_bps"), 0))
        freeze_authority = info.get("freezeAuthority")
        mint_authority = info.get("mintAuthority")
    except Exception as e:
        print(f"[RISK] token account inspect failed for {mint}: {e}", flush=True)
    if age_min >= 20:
        can_exit = bool(jupiter_quote_direct(mint, SOL_MINT, 10_000_000, 5000))
    liq_drop_pct = 0.0
    if float(first_liq or 0) > 0 and float(latest_liq or 0) >= 0:
        latest_ratio = float(latest_liq or 0) / max(float(first_liq or 0), 1.0)
        liq_drop_pct = round(max(0.0, (1.0 - latest_ratio) * 100.0), 1)
    flags = []
    score = 0
    if owner == TOKEN_2022_PROGRAM:
        flags.append("token-2022")
        score += 10
    if transfer_hook_enabled:
        flags.append("transfer-hook")
        score += 35
    if permanent_delegate_enabled:
        flags.append("permanent-delegate")
        score += 30
    if mint_close_authority_enabled:
        flags.append("mint-close-authority")
        score += 20
    if transfer_fee_bps > 0:
        flags.append(f"transfer-fee-{transfer_fee_bps}bps")
        score += min(20, max(8, math.ceil(transfer_fee_bps / 20)))
    if freeze_authority not in (None, ""):
        flags.append("freeze-authority")
        score += 30
    if mint_authority not in (None, "", PUMP_PROGRAM):
        flags.append("mint-authority")
        score += 20
    if can_exit is False:
        flags.append("no-exit-route")
        score += 40
    if liq_drop_pct >= 55:
        flags.append("liquidity-drain")
        score += 25
    return {
        "token_program": owner or SPL_TOKEN_PROGRAM,
        "transfer_hook_enabled": bool(transfer_hook_enabled),
        "permanent_delegate_enabled": bool(permanent_delegate_enabled),
        "mint_close_authority_enabled": bool(mint_close_authority_enabled),
        "transfer_fee_bps": transfer_fee_bps,
        "can_exit": can_exit,
        "threat_risk_score": min(100, score),
        "threat_flags": _dedupe_keep_order(flags),
        "liquidity_drop_pct": liq_drop_pct,
    }


def token_intel_payload(row):
    if not row:
        return {}
    payload = dict(row)
    for key, default in [
        ("narrative_tags", []),
        ("social_links", []),
        ("social_keys", []),
        ("threat_flags", []),
        ("infra_labels", []),
        ("checklist_json", []),
        ("milestones_json", {}),
    ]:
        payload[key] = _json_load(payload.get(key), default)
    payload["checklist"] = payload.pop("checklist_json", [])
    payload["milestones"] = payload.pop("milestones_json", {})
    payload["transfer_hook_enabled"] = bool(payload.get("transfer_hook_enabled"))
    if "permanent_delegate_enabled" in payload:
        payload["permanent_delegate_enabled"] = _coerce_bool(payload.get("permanent_delegate_enabled"))
    if "mint_close_authority_enabled" in payload:
        payload["mint_close_authority_enabled"] = _coerce_bool(payload.get("mint_close_authority_enabled"))
    if "transfer_fee_bps" in payload:
        payload["transfer_fee_bps"] = _coerce_int(payload.get("transfer_fee_bps"), 0)
    if payload.get("can_exit") is not None:
        payload["can_exit"] = bool(payload.get("can_exit"))
    for dt_key in ("first_seen_at", "last_seen_at", "last_updated"):
        payload[dt_key] = _to_iso(payload.get(dt_key))
    return payload


def persist_token_intel(intel):
    if not intel or not intel.get("mint"):
        return
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO token_intel (
                mint, name, symbol, deployer_wallet, first_seen_at, last_seen_at,
                first_price, max_price, first_mc, max_mc, first_vol, latest_vol,
                first_liq, latest_liq, holder_count, holder_growth_1h, volume_spike_ratio,
                first_buyer_count, smart_wallet_buys, smart_wallet_first10, narrative_tags,
                social_links, social_keys, narrative_score, deployer_score, whale_score,
                whale_action_score, cluster_confidence, infra_penalty, token_program,
                transfer_hook_enabled, can_exit, threat_risk_score, threat_flags, infra_labels, liquidity_drop_pct,
                max_multiple, green_lights, checklist_json, milestones_json, last_updated
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (mint) DO UPDATE SET
                name=EXCLUDED.name,
                symbol=EXCLUDED.symbol,
                deployer_wallet=COALESCE(token_intel.deployer_wallet, EXCLUDED.deployer_wallet),
                last_seen_at=GREATEST(token_intel.last_seen_at, EXCLUDED.last_seen_at),
                max_price=GREATEST(COALESCE(token_intel.max_price, 0), COALESCE(EXCLUDED.max_price, 0)),
                max_mc=GREATEST(COALESCE(token_intel.max_mc, 0), COALESCE(EXCLUDED.max_mc, 0)),
                latest_vol=EXCLUDED.latest_vol,
                latest_liq=EXCLUDED.latest_liq,
                holder_count=EXCLUDED.holder_count,
                holder_growth_1h=EXCLUDED.holder_growth_1h,
                volume_spike_ratio=EXCLUDED.volume_spike_ratio,
                first_buyer_count=EXCLUDED.first_buyer_count,
                smart_wallet_buys=EXCLUDED.smart_wallet_buys,
                smart_wallet_first10=EXCLUDED.smart_wallet_first10,
                narrative_tags=EXCLUDED.narrative_tags,
                social_links=EXCLUDED.social_links,
                social_keys=EXCLUDED.social_keys,
                narrative_score=EXCLUDED.narrative_score,
                deployer_score=EXCLUDED.deployer_score,
                whale_score=EXCLUDED.whale_score,
                whale_action_score=EXCLUDED.whale_action_score,
                cluster_confidence=EXCLUDED.cluster_confidence,
                infra_penalty=EXCLUDED.infra_penalty,
                token_program=EXCLUDED.token_program,
                transfer_hook_enabled=EXCLUDED.transfer_hook_enabled,
                can_exit=EXCLUDED.can_exit,
                threat_risk_score=EXCLUDED.threat_risk_score,
                threat_flags=EXCLUDED.threat_flags,
                infra_labels=EXCLUDED.infra_labels,
                liquidity_drop_pct=EXCLUDED.liquidity_drop_pct,
                max_multiple=GREATEST(COALESCE(token_intel.max_multiple, 1), COALESCE(EXCLUDED.max_multiple, 1)),
                green_lights=EXCLUDED.green_lights,
                checklist_json=EXCLUDED.checklist_json,
                milestones_json=EXCLUDED.milestones_json,
                last_updated=EXCLUDED.last_updated
        """, (
            intel["mint"],
            intel.get("name"),
            intel.get("symbol"),
            intel.get("deployer_wallet"),
            _from_iso(intel.get("first_seen_at")) or _now_utc(),
            _from_iso(intel.get("last_seen_at")) or _now_utc(),
            float(intel.get("first_price") or 0),
            float(intel.get("max_price") or intel.get("first_price") or 0),
            float(intel.get("first_mc") or 0),
            float(intel.get("max_mc") or intel.get("first_mc") or 0),
            float(intel.get("first_vol") or 0),
            float(intel.get("latest_vol") or 0),
            float(intel.get("first_liq") or 0),
            float(intel.get("latest_liq") or 0),
            int(intel.get("holder_count") or 0),
            float(intel.get("holder_growth_1h") or 0),
            float(intel.get("volume_spike_ratio") or 0),
            int(intel.get("first_buyer_count") or 0),
            int(intel.get("smart_wallet_buys") or 0),
            int(intel.get("smart_wallet_first10") or 0),
            json.dumps(_dedupe_keep_order(intel.get("narrative_tags") or [])),
            json.dumps(_dedupe_keep_order(intel.get("social_links") or [])),
            json.dumps(_dedupe_keep_order(intel.get("social_keys") or [])),
            int(intel.get("narrative_score") or 0),
            int(intel.get("deployer_score") or 0),
            int(intel.get("whale_score") or 0),
            int(intel.get("whale_action_score") or 0),
            int(intel.get("cluster_confidence") or 0),
            int(intel.get("infra_penalty") or 0),
            intel.get("token_program"),
            int(bool(intel.get("transfer_hook_enabled"))),
            None if intel.get("can_exit") is None else int(bool(intel.get("can_exit"))),
            int(intel.get("threat_risk_score") or 0),
            json.dumps(_dedupe_keep_order(intel.get("threat_flags") or [])),
            json.dumps(_dedupe_keep_order(intel.get("infra_labels") or [])),
            float(intel.get("liquidity_drop_pct") or 0),
            float(intel.get("max_multiple") or 1),
            int(intel.get("green_lights") or 0),
            json.dumps(intel.get("checklist") or []),
            json.dumps(intel.get("milestones") or {}),
            _from_iso(intel.get("last_updated")) or _now_utc(),
        ))
        conn.commit()
    finally:
        db_return(conn)


def update_deployer_outcomes(mint, multiple, deployer_wallet=None):
    if not mint or float(multiple or 0) <= 1:
        return
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT deployer_wallet, milestones_json FROM token_intel WHERE mint=%s", (mint,))
        row = cur.fetchone()
    finally:
        db_return(conn)
    if not row:
        return
    deployer_wallet = deployer_wallet or row.get("deployer_wallet")
    if not deployer_wallet:
        return
    milestones = _json_load(row.get("milestones_json"), {})
    changed = False
    stats = get_deployer_stats(deployer_wallet)
    for level in (2, 5, 10):
        key = f"{level}x"
        if float(multiple) >= level and not milestones.get(key):
            milestones[key] = True
            stats[f"wins_{level}x"] = int(stats.get(f"wins_{level}x") or 0) + 1
            changed = True
    if float(multiple) > float(stats.get("best_multiple") or 1):
        stats["best_multiple"] = float(multiple)
        changed = True
    if not changed:
        return
    persist_deployer_stats(stats)
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE token_intel
            SET milestones_json=%s, max_multiple=GREATEST(COALESCE(max_multiple, 1), %s), last_updated=NOW()
            WHERE mint=%s
        """, (json.dumps(milestones), float(multiple), mint))
        conn.commit()
    finally:
        db_return(conn)


def prime_token_intel(info, pair=None, source="scanner"):
    mint = (info or {}).get("mint")
    if not mint:
        return {}
    now = _now_utc()
    links = extract_social_links(pair)
    keys = social_keys(links)
    with _token_intel_lock:
        cached = token_intel_payload(_token_intel_cache.get(mint) or {})
        first_price = float(cached.get("first_price") or info.get("price") or 0)
        first_mc = float(cached.get("first_mc") or info.get("mc") or 0)
        intel = {
            "mint": mint,
            "name": info.get("name") or cached.get("name"),
            "symbol": info.get("symbol") or cached.get("symbol"),
            "deployer_wallet": cached.get("deployer_wallet"),
            "first_seen_at": cached.get("first_seen_at") or now,
            "last_seen_at": now,
            "first_price": first_price,
            "max_price": max(float(cached.get("max_price") or 0), float(info.get("price") or 0), first_price),
            "first_mc": first_mc,
            "max_mc": max(float(cached.get("max_mc") or 0), float(info.get("mc") or 0), first_mc),
            "first_vol": float(cached.get("first_vol") or info.get("vol") or 0),
            "latest_vol": float(info.get("vol") or cached.get("latest_vol") or 0),
            "first_liq": float(cached.get("first_liq") or info.get("liq") or 0),
            "latest_liq": float(info.get("liq") or cached.get("latest_liq") or 0),
            "holder_count": int(cached.get("holder_count") or 0),
            "holder_growth_1h": float(cached.get("holder_growth_1h") or 0),
            "volume_spike_ratio": float(volume_spike_ratio(mint, float(info.get("vol") or 0)) or 0),
            "first_buyer_count": int(cached.get("first_buyer_count") or 0),
            "smart_wallet_buys": int(cached.get("smart_wallet_buys") or 0),
            "smart_wallet_first10": int(cached.get("smart_wallet_first10") or 0),
            "narrative_tags": cached.get("narrative_tags") or [],
            "social_links": _dedupe_keep_order((cached.get("social_links") or []) + links),
            "social_keys": _dedupe_keep_order((cached.get("social_keys") or []) + keys),
            "narrative_score": int(cached.get("narrative_score") or 0),
            "deployer_score": int(cached.get("deployer_score") or 0),
            "whale_score": int(cached.get("whale_score") or 0),
            "whale_action_score": int(cached.get("whale_action_score") or 0),
            "cluster_confidence": int(cached.get("cluster_confidence") or 0),
            "infra_penalty": int(cached.get("infra_penalty") or 0),
            "token_program": cached.get("token_program"),
            "transfer_hook_enabled": bool(cached.get("transfer_hook_enabled")),
            "can_exit": cached.get("can_exit"),
            "threat_risk_score": int(cached.get("threat_risk_score") or 0),
            "threat_flags": cached.get("threat_flags") or [],
            "infra_labels": cached.get("infra_labels") or [],
            "liquidity_drop_pct": float(cached.get("liquidity_drop_pct") or 0),
            "max_multiple": float(cached.get("max_multiple") or 1),
            "green_lights": int(cached.get("green_lights") or 0),
            "checklist": cached.get("checklist") or [],
            "milestones": cached.get("milestones") or {},
            "last_updated": cached.get("last_updated"),
            "source": source,
        }
        if first_price and float(info.get("price") or 0):
            intel["max_multiple"] = max(intel["max_multiple"], round(float(info.get("price")) / first_price, 2))
        if first_mc and float(info.get("mc") or 0):
            intel["max_multiple"] = max(intel["max_multiple"], round(float(info.get("mc")) / first_mc, 2))
        if len(_token_intel_cache) >= INTEL_MAX_TRACKED and mint not in _token_intel_cache:
            stale_key = min(_token_intel_cache, key=lambda k: _to_iso(_token_intel_cache[k].get("last_seen_at")) or "")
            _token_intel_cache.pop(stale_key, None)
        _token_intel_cache[mint] = dict(intel)
    persist_token_intel(intel)
    if intel.get("deployer_wallet"):
        update_deployer_outcomes(mint, intel.get("max_multiple") or 1, intel.get("deployer_wallet"))
    return token_intel_payload(intel)


def refresh_token_intel(mint, base_info=None, pair=None, force=False):
    if not mint:
        return {}
    cached = token_intel_payload(_token_intel_cache.get(mint) or {})
    if cached and not force:
        last_updated = _from_iso(cached.get("last_updated"))
        if last_updated and (_now_utc() - last_updated).total_seconds() < INTEL_REFRESH_SEC:
            return cached
    base_info = dict(base_info or {})
    base_info.setdefault("mint", mint)
    if not base_info.get("name") or not base_info.get("price"):
        try:
            resp = dex_get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=6)
            if resp.status_code == 200:
                pairs = resp.json().get("pairs") or []
                if pairs:
                    pair = pair or pairs[0]
                    derived = _process_dex_pair(pair)
                    if derived:
                        base_info.update(derived)
        except Exception as e:
            print(f"[INTEL] Dex refresh failed for {mint}: {e}", flush=True)
    seeded = prime_token_intel(base_info, pair=pair, source="refresh")
    txns = helius_address_transactions(mint, limit=60, timeout=8)
    flow = extract_wallet_flow_from_swaps(txns, mint)
    deployer_wallet = seeded.get("deployer_wallet") or resolve_deployer_wallet(mint, txns)
    if deployer_wallet and deployer_wallet != seeded.get("deployer_wallet"):
        deployer_stats = register_deployer_launch(deployer_wallet, mint)
    else:
        deployer_stats = get_deployer_stats(deployer_wallet)
    holder_count = get_token_holder_count(mint, txns)
    holder_growth = update_holder_history(mint, holder_count)
    narrative = build_narrative_profile(
        base_info.get("name") or seeded.get("name"),
        base_info.get("symbol") or seeded.get("symbol"),
        pair=pair,
        deployer_stats=deployer_stats,
        social_keys_list=seeded.get("social_keys") or [],
    )
    infra_labels = infer_infrastructure_labels(
        [deployer_wallet] + list(flow.get("first_buyers") or [])
    )
    risk = inspect_token_risk(
        mint,
        age_min=float(base_info.get("age_min") or seeded.get("age_min") or 0),
        first_liq=float(seeded.get("first_liq") or base_info.get("liq") or 0),
        latest_liq=float(base_info.get("liq") or seeded.get("latest_liq") or 0),
    )
    intel = dict(seeded)
    intel.update({
        "deployer_wallet": deployer_wallet,
        "holder_count": holder_count,
        "holder_growth_1h": holder_growth,
        "first_buyer_count": len(flow.get("first_buyers") or []),
        "smart_wallet_buys": int(flow.get("smart_wallet_buys") or 0),
        "smart_wallet_first10": int(flow.get("smart_wallet_first10") or 0),
        "narrative_tags": narrative["tags"],
        "social_keys": _dedupe_keep_order((seeded.get("social_keys") or []) + narrative["social_keys"]),
        "narrative_score": narrative["score"],
        "deployer_score": compute_deployer_reputation(deployer_stats),
        "token_program": risk.get("token_program"),
        "transfer_hook_enabled": risk.get("transfer_hook_enabled"),
        "permanent_delegate_enabled": risk.get("permanent_delegate_enabled"),
        "mint_close_authority_enabled": risk.get("mint_close_authority_enabled"),
        "transfer_fee_bps": risk.get("transfer_fee_bps"),
        "can_exit": risk.get("can_exit"),
        "threat_risk_score": risk.get("threat_risk_score"),
        "threat_flags": risk.get("threat_flags") or [],
        "infra_labels": infra_labels,
        "liquidity_drop_pct": float(risk.get("liquidity_drop_pct") or 0),
        "last_seen_at": _now_utc(),
        "last_updated": _now_utc(),
    })
    intel.update(compute_whale_scores(intel))
    checklist = build_three_signal_checklist(base_info, intel)
    intel["checklist"] = checklist["items"]
    intel["green_lights"] = checklist["green_lights"]
    first_price = float(intel.get("first_price") or 0)
    first_mc = float(intel.get("first_mc") or 0)
    if first_price and float(base_info.get("price") or 0):
        intel["max_multiple"] = max(float(intel.get("max_multiple") or 1), round(float(base_info["price"]) / first_price, 2))
    if first_mc and float(base_info.get("mc") or 0):
        intel["max_multiple"] = max(float(intel.get("max_multiple") or 1), round(float(base_info["mc"]) / first_mc, 2))
    persist_token_intel(intel)
    with _token_intel_lock:
        _token_intel_cache[mint] = dict(intel)
    if deployer_wallet:
        update_deployer_outcomes(mint, intel.get("max_multiple") or 1, deployer_wallet)
    return token_intel_payload(intel)


def ensure_token_intel(mint, base_info=None, pair=None, force=False):
    if not mint:
        return {}
    cached = token_intel_payload(_token_intel_cache.get(mint) or {})
    if cached and not force:
        last_updated = _from_iso(cached.get("last_updated"))
        if last_updated and (_now_utc() - last_updated).total_seconds() < INTEL_REFRESH_SEC:
            return cached
    try:
        return refresh_token_intel(mint, base_info=base_info, pair=pair, force=force)
    except Exception as e:
        print(f"[INTEL] refresh failed for {mint}: {e}", flush=True)
        return cached or token_intel_payload(prime_token_intel(base_info or {"mint": mint}, pair=pair))


def token_pattern_monitor():
    time.sleep(20)
    while True:
        try:
            feed = list(market_feed)[:60]
            ranked = sorted(
                [item for item in feed if item.get("mint")],
                key=lambda item: ((item.get("score") or 0), -(item.get("age_min") or 9999)),
                reverse=True,
            )[:8]
            for item in ranked:
                intel = ensure_token_intel(item["mint"], base_info=item, force=False)
                if intel:
                    item["intel"] = intel
                    item["green_lights"] = intel.get("green_lights", 0)
                    item["deployer_score"] = intel.get("deployer_score", 0)
                    item["narrative_score"] = intel.get("narrative_score", 0)
            with _token_intel_lock:
                if len(_token_intel_cache) > INTEL_MAX_TRACKED:
                    by_age = sorted(_token_intel_cache.items(), key=lambda kv: str(kv[1].get("last_seen_at") or ""))
                    for stale_key, _ in by_age[:max(0, len(_token_intel_cache) - INTEL_MAX_TRACKED)]:
                        _token_intel_cache.pop(stale_key, None)
        except Exception as e:
            print(f"[INTEL] monitor error: {e}", flush=True)
        time.sleep(45)

def check_whale_wallets():
    """Poll recent transactions of whale wallets and copy their buys."""
    time.sleep(15)  # let app fully start first
    while True:
        try:
            with user_bots_lock:
                active_bots = [b for b in user_bots.values() if b.running]
            if not active_bots:
                time.sleep(30)
                continue
            for wallet in WHALE_WALLETS:
                try:
                    r = requests.post(HELIUS_RPC, json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getSignaturesForAddress",
                        "params": [wallet, {"limit": 5}]
                    }, timeout=8)
                    sigs = r.json().get("result", [])
                    for sig_info in sigs:
                        sig = sig_info.get("signature")
                        if not sig or sig in _whale_seen:
                            continue
                        _whale_seen.add(sig)
                        # get transaction details
                        tx_r = requests.post(HELIUS_RPC, json={
                            "jsonrpc": "2.0", "id": 1,
                            "method": "getTransaction",
                            "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
                        }, timeout=8)
                        tx = tx_r.json().get("result")
                        if not tx:
                            continue
                        # look for token transfers (buys) in post token balances
                        pre  = {a["accountIndex"]: a for a in (tx.get("meta") or {}).get("preTokenBalances",  [])}
                        post = {a["accountIndex"]: a for a in (tx.get("meta") or {}).get("postTokenBalances", [])}
                        for idx, pb in post.items():
                            mint = pb.get("mint")
                            if not mint or mint == SOL_MINT:
                                continue
                            pre_amt  = float((pre.get(idx)  or {}).get("uiTokenAmount", {}).get("uiAmount") or 0)
                            post_amt = float(pb.get("uiTokenAmount", {}).get("uiAmount") or 0)
                            if post_amt > pre_amt * 1.5 and post_amt > 0:
                                # whale bought this token — get price info and signal bots
                                try:
                                    _wr = dex_get(
                                        f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                                        timeout=5
                                    )
                                    if _wr.status_code != 200:
                                        continue
                                    pairs = _wr.json().get("pairs")
                                    if not pairs:
                                        continue
                                    p = pairs[0]
                                    price  = float(p.get("priceUsd") or 0)
                                    mc     = p.get("marketCap", 0) or 0
                                    vol    = (p.get("volume") or {}).get("h24", 0) or 0
                                    liq    = (p.get("liquidity") or {}).get("usd", 0) or 0
                                    name   = p.get("baseToken", {}).get("name", "Unknown")
                                    created_at = p.get("pairCreatedAt")
                                    age_min = (time.time()*1000 - created_at)/60000 if created_at else 9999
                                    if not price:
                                        continue
                                    # pre-filter: skip old/huge/stable tokens
                                    if age_min > 60:
                                        continue
                                    if mc > 500_000:
                                        continue
                                    if liq < 3000:
                                        continue
                                    # dedup: skip if we signalled this mint in last 5 min
                                    now = time.time()
                                    if now - _whale_mints.get(mint, 0) < 300:
                                        continue
                                    _whale_mints[mint] = now
                                    _whale_buys.appendleft({
                                        "wallet": wallet, "label": WHALE_LABELS.get(wallet, wallet[:8]+"..."),
                                        "mint": mint, "name": name, "price": price,
                                        "mc": mc, "vol": vol, "liq": liq,
                                        "timestamp": now, "ts": time.strftime("%H:%M:%S"),
                                    })
                                    for bot in list(user_bots.values()):
                                        if bot.running and mint not in bot.positions:
                                            bot.log_msg(f"🐋 WHALE COPY: {name} ({wallet[:8]}...)")
                                            try:
                                                change = (p.get("priceChange") or {}).get("h1", 0) or 0
                                                bot.evaluate_signal(mint, name, price, mc, vol, liq, age_min, change)
                                            except Exception as _e:
                                                print(f"[ERROR] {_e}", flush=True)
                                except Exception as _e:
                                    print(f"[ERROR] {_e}", flush=True)
                except Exception as _e:
                    print(f"[ERROR] {_e}", flush=True)
            time.sleep(20)
        except Exception as e:
            print(f"Whale tracker error: {e}")
            time.sleep(30)

def _process_dex_pair(p):
    """Extract token info dict from a DexScreener pair object. Returns None if unusable."""
    try:
        mint = (p.get("baseToken") or {}).get("address")
        if not mint or mint == SOL_MINT:
            return None
        price = float(p.get("priceUsd") or 0)
        if not price:
            return None
        created_at = p.get("pairCreatedAt")
        age_min    = (time.time()*1000 - created_at) / 60000 if created_at else 9999
        vol        = (p.get("volume") or {}).get("h24", 0) or 0
        change     = (p.get("priceChange") or {}).get("h1", 0) or 0
        momentum   = volume_velocity(mint, vol)
        info = {
            "mint":     mint,
            "name":     p.get("baseToken",{}).get("name","Unknown"),
            "symbol":   p.get("baseToken",{}).get("symbol","?"),
            "price":    price,
            "mc":       p.get("marketCap", 0) or 0,
            "vol":      vol,
            "liq":      (p.get("liquidity") or {}).get("usd", 0) or 0,
            "age_min":  age_min,
            "change":   change,
            "momentum": momentum,
            "ts":       int(time.time()),
        }
        info["score"] = min(100, ai_score(info) + check_social_signals(str(p)))
        intel = prime_token_intel(info, pair=p, source="scanner")
        if intel:
            info["intel"] = intel
            info["green_lights"] = intel.get("green_lights", 0)
            info["deployer_score"] = intel.get("deployer_score", 0)
            info["narrative_score"] = intel.get("narrative_score", 0)
        return info
    except Exception as _e:
        print(f"[ERROR] {_e}", flush=True)
        return None

def _broadcast_signal(info):
    """Push info to market_feed and evaluate against all running bots."""
    mint = info["mint"]
    intel = ensure_token_intel(mint, base_info=info, pair=None, force=False)
    if intel:
        info["intel"] = intel
        info["green_lights"] = intel.get("green_lights", 0)
        info["deployer_score"] = intel.get("deployer_score", 0)
        info["narrative_score"] = intel.get("narrative_score", 0)
    market_feed.appendleft(info)
    record_price(mint, info["price"])
    with user_bots_lock:
        running_bots = [b for b in user_bots.values() if b.running]
    print(f"[SCAN] {info['name']} | MC:${info['mc']:,.0f} Liq:${info['liq']:,.0f} Age:{info['age_min']:.0f}m Chg:{info['change']:+.0f}% | bots={len(running_bots)}", flush=True)
    for bot in running_bots:
        try:
            effective_change = info["change"]
            if info["momentum"] >= 60:
                bot.log_msg(f"⚡ MOMENTUM: {info['name']} vel={info['momentum']}")
                effective_change = max(effective_change, 25)
            bot.evaluate_signal(
                mint, info["name"], info["price"],
                info["mc"], info["vol"], info["liq"],
                info["age_min"], effective_change
            )
        except Exception as _be:
            print(f"[SCAN] evaluate_signal error: {_be}", flush=True)

def global_scanner():
    """Primary scanner: DexScreener latest token profiles (established flow)."""
    time.sleep(10)
    while True:
        try:
            r = dex_get(
                "https://api.dexscreener.com/token-profiles/latest/v1",
                timeout=10
            )
            if r.status_code != 200:
                print(f"[SCANNER] token-profiles HTTP {r.status_code}", flush=True)
                time.sleep(30 if r.status_code == 429 else 15)
                continue
            try:
                data = r.json()
                tokens = data if isinstance(data, list) else []
            except Exception:
                tokens = []
            sol_tokens = [t for t in tokens if t.get("chainId") == "solana"]
            with seen_tokens_lock:
                new_tokens  = [t for t in sol_tokens if t.get("tokenAddress") and t["tokenAddress"] not in seen_tokens]
                for t in new_tokens:
                    seen_tokens.add(t["tokenAddress"])
            if new_tokens:
                print(f"[SCANNER] token-profiles: {len(tokens)} total, {len(sol_tokens)} solana, {len(new_tokens)} new", flush=True)
            for t in new_tokens:
                mint = t["tokenAddress"]
                try:
                    resp = dex_get(
                        f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                        timeout=5
                    )
                    if resp.status_code != 200:
                        continue
                    pairs = resp.json().get("pairs") or []
                    if not pairs:
                        continue
                    info = _process_dex_pair(pairs[0])
                    if info:
                        _broadcast_signal(info)
                    time.sleep(0.3)  # small delay between per-token lookups
                except Exception:
                    pass
            time.sleep(20)
        except Exception as e:
            print(f"[SCANNER] error: {e}", flush=True)
            time.sleep(20)

def new_pairs_scanner():
    """Secondary scanner: polls DexScreener boosted + latest Solana pairs directly."""
    time.sleep(15)
    SCAN_URLS = [
        "https://api.dexscreener.com/token-boosts/latest/v1",
        "https://api.dexscreener.com/token-boosts/top/v1",
    ]
    while True:
        try:
            got_429 = False
            for url in SCAN_URLS:
                label = url.split("/")[-2]
                try:
                    resp = dex_get(url, timeout=8)
                    if resp.status_code != 200:
                        if resp.status_code == 429:
                            got_429 = True
                        else:
                            print(f"[SCANNER2] {label} HTTP {resp.status_code}", flush=True)
                        continue
                    try:
                        data = resp.json()
                        items = data if isinstance(data, list) else []
                    except Exception:
                        items = []
                    sol = [i for i in items if i.get("chainId") == "solana"]
                    with seen_tokens_lock:
                        new = [i for i in sol if i.get("tokenAddress") and i["tokenAddress"] not in seen_tokens]
                        for item in new:
                            seen_tokens.add(item["tokenAddress"])
                    if new:
                        print(f"[SCANNER2] {label}: {len(items)} total, {len(sol)} solana, {len(new)} new", flush=True)
                    for item in new:
                        mint = item["tokenAddress"]
                        try:
                            r2 = dex_get(
                                f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                                timeout=5
                            )
                            if r2.status_code != 200:
                                continue
                            pairs = r2.json().get("pairs") or []
                            if pairs:
                                info = _process_dex_pair(pairs[0])
                                if info:
                                    _broadcast_signal(info)
                            time.sleep(0.3)
                        except Exception:
                            pass
                    time.sleep(1)  # pause between the two URLs
                except Exception as e2:
                    print(f"[SCANNER2] {label} error: {e2}", flush=True)
            time.sleep(30 if got_429 else 15)
        except Exception as e:
            print(f"[SCANNER2] outer error: {e}", flush=True)
            time.sleep(20)

# ── Helius websocket pool sniping ──────────────────────────────────────────────
def _sniper_emit_token(mint, source_label="helius-ws"):
    if not mint or mint == SOL_MINT:
        return False
    with seen_tokens_lock:
        if mint in seen_tokens:
            return False
    try:
        resp = dex_get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=5)
        if resp.status_code != 200:
            return False
        pairs = safe_json_response(resp, {}).get("pairs") or []
        if not pairs:
            return False
        info = _process_dex_pair(pairs[0])
        if not info:
            return False
        with seen_tokens_lock:
            if mint in seen_tokens:
                return False
            seen_tokens.add(mint)
        info["sniped"] = True
        info["source"] = source_label
        print(f"[Sniper] {source_label}: {info['name']} | MC:${info['mc']:,.0f} Age:{info['age_min']:.0f}m", flush=True)
        _broadcast_signal(info)
        return True
    except Exception as e:
        print(f"[Sniper] emit error for {mint}: {e}", flush=True)
        return False


def helius_pool_sniper():
    """Listen for fresh Pump.fun and Raydium transactions and feed them into the main signal path."""
    import json as _json
    ws_url = HELIUS_RPC.replace("https://", "wss://").replace("http://", "ws://")
    api_key = ""
    if "api-key=" in HELIUS_RPC:
        api_key = HELIUS_RPC.split("api-key=")[-1]
    elif "?" in HELIUS_RPC:
        api_key = HELIUS_RPC.split("?")[-1].replace("api-key=", "")
    tracked_programs = [
        (PUMP_FUN, "pumpfun"),
        (RAYDIUM_AMM, "raydium"),
    ]

    while True:
        try:
            import websocket as ws_lib
            wss = f"wss://atlas-mainnet.helius-rpc.com/?api-key={api_key}" if api_key else ws_url
            seen_signatures = set()

            def on_open(ws):
                for idx, (program_id, label) in enumerate(tracked_programs, start=1):
                    ws.send(_json.dumps({
                        "jsonrpc": "2.0",
                        "id": idx,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [program_id]},
                            {"commitment": "confirmed"},
                        ],
                    }))
                    print(f"[Sniper] Subscribed to {label} logs", flush=True)

            def on_message(ws, message):
                try:
                    data = _json.loads(message)
                    value = (((data or {}).get("params") or {}).get("result") or {}).get("value") or {}
                    logs = value.get("logs") or []
                    sig = value.get("signature") or ""
                    if not sig or sig in seen_signatures:
                        return
                    if not any(token in (line or "").lower() for line in logs for token in ("initialize", "create", "buy", "swap")):
                        return
                    seen_signatures.add(sig)
                    tx_result = rpc_call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}], timeout=8) or {}
                    meta = (tx_result.get("meta") if isinstance(tx_result, dict) else {}) or {}
                    post_bals = meta.get("postTokenBalances") or []
                    source_label = "helius-ws"
                    joined_logs = " ".join(str(line or "").lower() for line in logs)
                    if PUMP_FUN.lower() in joined_logs:
                        source_label = "helius-ws:pumpfun"
                    elif RAYDIUM_AMM.lower() in joined_logs:
                        source_label = "helius-ws:raydium"
                    for pb in post_bals:
                        mint = pb.get("mint") or ""
                        _sniper_emit_token(mint, source_label=source_label)
                except Exception as e:
                    print(f"[Sniper] message error: {e}", flush=True)

            def on_error(ws, err):
                print(f"[Sniper] WS error: {err}", flush=True)

            def on_close(ws, *a):
                print("[Sniper] WS closed — reconnecting...", flush=True)

            ws_app = ws_lib.WebSocketApp(
                wss,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws_app.run_forever(ping_interval=30, ping_timeout=10)
        except ImportError:
            print("[Sniper] websocket-client not installed — skipping WS sniper", flush=True)
            break
        except Exception as e:
            print(f"[Sniper] reconnecting in 10s: {e}", flush=True)
            time.sleep(10)

def auto_restart_bots():
    """Restart bots that were running before a server restart."""
    time.sleep(5)  # wait for app to fully initialize
    try:
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT bs.user_id, bs.preset, bs.run_mode, bs.run_duration_min, bs.profit_target_sol,
                       bs.max_correlated, bs.drawdown_limit_sol, bs.custom_settings,
                       w.encrypted_key, u.plan, u.email
                FROM bot_settings bs
                JOIN wallets w ON w.user_id = bs.user_id
                JOIN users u ON u.id = bs.user_id
                WHERE bs.is_running = 1
            """)
            rows = cur.fetchall()
        finally:
            db_return(conn)
        for row in rows:
            uid = row["user_id"]
            try:
                kp = Keypair.from_bytes(base58.b58decode(decrypt_key(row["encrypted_key"])))
                preset_name = normalize_preset_name(row["preset"])
                settings = dict(PRESETS.get(preset_name, PRESETS["balanced"]))
                if row.get("max_correlated") is not None:
                    settings["max_correlated"] = row["max_correlated"]
                if row.get("drawdown_limit_sol") is not None:
                    settings["drawdown_limit_sol"] = row["drawdown_limit_sol"]
                if row.get("custom_settings"):
                    try:
                        import json as _json
                        custom = _json.loads(row["custom_settings"])
                        if isinstance(custom, dict):
                            settings.update(custom)
                    except Exception:
                        pass
                max_sol = PLAN_LIMITS.get(effective_plan(row["plan"], row.get("email","")), PLAN_LIMITS["basic"])["max_buy_sol"]
                settings["max_buy_sol"] = min(settings["max_buy_sol"], max_sol)
                bot = BotInstance(uid, kp, settings,
                    run_mode=row["run_mode"],
                    run_duration_min=row["run_duration_min"],
                    profit_target_sol=row["profit_target_sol"],
                    preset_name=preset_name)
                with user_bots_lock:
                    user_bots[uid] = bot
                # Reload open positions from DB so they survive Railway restarts
                try:
                    _conn = db()
                    try:
                        _c = _conn.cursor()
                        _c.execute("SELECT * FROM open_positions WHERE user_id=%s", (uid,))
                        saved_pos = _c.fetchall()
                    finally:
                        db_return(_conn)
                    for p in saved_pos:
                        opened_ts = p["opened_at"].timestamp() if p.get("opened_at") else time.time()
                        bot.positions[p["mint"]] = {
                            "name":       p["name"],
                            "entry_price": p["entry_price"],
                            "peak_price":  p["peak_price"],
                            "timestamp":   opened_ts,
                            "tp1_hit":     bool(p["tp1_hit"]),
                            "entry_sol":   p["entry_sol"],
                            "dev_wallet":  p["dev_wallet"],
                            "surge_hold_active": False,
                            "surge_peak_price": p["peak_price"] or p["entry_price"],
                        }
                    if saved_pos:
                        print(f"[U{uid}] Restored {len(saved_pos)} open position(s) from DB")
                except Exception as _e:
                    print(f"[WARN] Could not reload positions for user {uid}: {_e}")
                t = threading.Thread(target=bot.run, daemon=True)
                t.start()
                bot.thread = t
            except Exception as e:
                conn = db()
                try:
                    cur = conn.cursor()
                    cur.execute("UPDATE bot_settings SET is_running=0 WHERE user_id=%s", (uid,))
                    conn.commit()
                finally:
                    db_return(conn)
    except Exception:
        pass

def warm_sender_connections():
    """Ping Helius Sender regional nodes every 30s to reduce cold-start latency."""
    from urllib.parse import urlparse, parse_qs
    parsed  = urlparse(HELIUS_RPC)
    api_key = parse_qs(parsed.query).get("api-key", [""])[0]
    qs = f"?api-key={api_key}" if api_key else ""
    endpoints = [
        f"http://slc-sender.helius-rpc.com/fast{qs}",
        f"http://ewr-sender.helius-rpc.com/fast{qs}",
    ]
    while True:
        for url in endpoints:
            try:
                requests.get(url.replace("/fast", "/ping"), timeout=4)
            except Exception as _e:
                print(f"[ERROR] {_e}", flush=True)
        time.sleep(30)

# ── CEX Listing Sniper ─────────────────────────────────────────────────────────
# Monitors 7 exchange public APIs (free, no auth) every 5s.
# When a new token pair appears → finds it on Solana via DexScreener → buys.
# Listing pumps average +50–200%. Execution via Helius Sender for max speed.

import queue as _queue
listing_alert_queue = _queue.Queue()   # (symbol, mint, name, price, liq, exchange)
_listing_known = {}   # exchange → set of pair symbols we've already seen

LISTING_EXCHANGES = {
    "binance":  ("https://api.binance.com/api/v3/exchangeInfo",          "symbols",  "symbol",     None),
    "coinbase": ("https://api.exchange.coinbase.com/products",           None,       "id",         None),
    "okx":      ("https://www.okx.com/api/v5/public/instruments?instType=SPOT", "data", "instId", None),
    "kraken":   ("https://api.kraken.com/0/public/AssetPairs",           "result",   None,         None),
    "bybit":    ("https://api.bybit.com/v5/market/instruments-info?category=spot", "result.list", "symbol", None),
    "kucoin":   ("https://api.kucoin.com/api/v1/symbols",                "data",     "symbol",     None),
    "gate":     ("https://api.gateio.ws/api/v4/spot/currency_pairs",     None,       "id",         None),
}

def _extract_symbols(exchange, data):
    """Parse the raw API response for each exchange into a set of uppercase base symbols."""
    syms = set()
    try:
        data_dict = data if isinstance(data, dict) else {}
        data_list = data if isinstance(data, list) else []
        if exchange == "binance":
            for s in data_dict.get("symbols", []):
                if s.get("status") == "TRADING" and s.get("quoteAsset") in ("USDT","BUSD","FDUSD"):
                    syms.add(s["baseAsset"].upper())
        elif exchange == "coinbase":
            for p in data_list:
                if p.get("status") == "online" and p.get("quote_currency") in ("USD","USDT"):
                    syms.add(p["base_currency"].upper())
        elif exchange == "okx":
            for p in (data_dict.get("data") or []):
                if p.get("state") == "live" and p.get("instId","").endswith("-USDT"):
                    syms.add(p["instId"].split("-")[0].upper())
        elif exchange == "kraken":
            for k, v in (data_dict.get("result") or {}).items():
                if isinstance(v, dict) and v.get("status") == "online":
                    base = v.get("base","")
                    if base.startswith("X"): base = base[1:]
                    if base.startswith("Z"): base = base[1:]
                    syms.add(base.upper())
        elif exchange == "bybit":
            lst = (data_dict.get("result") or {}).get("list") or []
            for p in lst:
                if p.get("status") == "Trading" and p.get("symbol","").endswith("USDT"):
                    syms.add(p["symbol"][:-4].upper())
        elif exchange == "kucoin":
            for p in (data_dict.get("data") or []):
                if p.get("enableTrading") and p.get("quoteCurrency") in ("USDT","USDC"):
                    syms.add(p["baseCurrency"].upper())
        elif exchange == "gate":
            for p in data_list:
                if p.get("trade_status") == "tradable" and p.get("id","").endswith("_USDT"):
                    syms.add(p["id"].split("_")[0].upper())
    except Exception as _e:
        print(f"[ERROR] {_e}", flush=True)
    return syms

def _find_solana_token(symbol):
    """Search DexScreener for a Solana token by symbol. Returns (mint, name, price, liq) or None."""
    try:
        _sr = dex_get(
            f"https://api.dexscreener.com/latest/dex/search?q={symbol}",
            timeout=8
        )
        if _sr.status_code != 200:
            return None
        r = safe_json_response(_sr, {"pairs": []})
        pairs = r.get("pairs") or []
        sol_pairs = [p for p in pairs
                     if p.get("chainId") == "solana"
                     and p.get("baseToken",{}).get("symbol","").upper() == symbol
                     and (p.get("liquidity") or {}).get("usd", 0) < 50_000_000]
        if not sol_pairs:
            return None
        # prefer highest liquidity pair
        sol_pairs.sort(key=lambda p: (p.get("liquidity") or {}).get("usd", 0), reverse=True)
        p    = sol_pairs[0]
        mint = p["baseToken"]["address"]
        name = p["baseToken"].get("name", symbol)
        price = float(p.get("priceUsd") or 0)
        liq   = float((p.get("liquidity") or {}).get("usd", 0))
        if not price:
            return None
        return mint, name, price, liq
    except Exception as _e:
        print(f"[ERROR] {_e}", flush=True)
        return None

def listing_scanner():
    """Poll exchange APIs for new token listings every 5s. Zero cost — all public endpoints."""
    time.sleep(15)  # let app boot
    print("[Listing] Scanner started — monitoring 7 exchanges")

    # Seed known pairs so we don't fire on startup
    for exchange, (url, _, _, _) in LISTING_EXCHANGES.items():
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            data = safe_json_response(resp, [])
            _listing_known[exchange] = _extract_symbols(exchange, data)
        except Exception as _e:
            print(f"[ERROR] {_e}", flush=True)
            _listing_known[exchange] = set()
        time.sleep(1)

    while True:
        try:
            for exchange, (url, _, _, _) in LISTING_EXCHANGES.items():
                try:
                    resp = requests.get(url, headers=HEADERS, timeout=10)
                    data = safe_json_response(resp, [])
                    current = _extract_symbols(exchange, data)
                    prev    = _listing_known.get(exchange, set())
                    new_syms = current - prev
                    if new_syms:
                        _listing_known[exchange] = current
                        for symbol in new_syms:
                            print(f"[Listing] 🔔 NEW on {exchange}: {symbol}")
                            token = _find_solana_token(symbol)
                            if token:
                                mint, name, price, liq = token
                                listing_alert_queue.put({
                                    "exchange": exchange,
                                    "symbol":   symbol,
                                    "mint":     mint,
                                    "name":     name,
                                    "price":    price,
                                    "liq":      liq,
                                    "ts":       time.time(),
                                })
                                print(f"[Listing] ✅ Found on Solana: {name} ({mint[:8]}...) — alerting bots")
                except Exception:
                    pass  # exchange API timeouts are normal
                time.sleep(0.8)   # stagger requests across exchanges
            time.sleep(5)
        except Exception as e:
            print(f"[Listing] Scanner error: {e}")
            time.sleep(10)

def process_listing_alerts():
    """
    Consume listing_alert_queue and execute buys on all running bots.
    Listing buys use a higher TP (40%) and fixed 10-min stop-loss window.
    """
    while True:
        try:
            alert = listing_alert_queue.get(timeout=2)
        except _queue.Empty:
            continue
        try:
            exchange = alert["exchange"].upper()
            mint     = alert["mint"]
            name     = alert["name"]
            price    = alert["price"]
            liq      = alert["liq"]
            for bot in list(user_bots.values()):
                if not bot.running:
                    continue
                if mint in bot.positions:
                    continue
                # Skip if listing sniper is disabled for this bot
                if not bot.settings.get("listing_sniper", True):
                    continue
                bot.log_msg(f"🔔 CEX LISTING: {name} on {exchange} — buying now")
                try:
                    bot.buy(mint, f"[LISTING:{exchange}] {name}", price, liq=liq)
                    # Override the position's TP to 40% (listing pump target)
                    if mint in bot.positions:
                        pos = bot.positions[mint]
                        pos["listing"] = True
                        pos["listing_tp"] = price * 1.40
                except Exception as e:
                    bot.log_msg(f"Listing buy error: {e}")
        except Exception as e:
            print(f"[Listing] Alert processing error: {e}")

def _release_background_lock():
    global _background_lock_conn
    if _background_lock_conn is not None:
        try:
            _background_lock_conn.close()
        except Exception:
            pass
        _background_lock_conn = None


atexit.register(_release_background_lock)

SEEN_TOKENS_MAX = 50_000

def _prune_seen_tokens():
    """Periodically clear seen_tokens to prevent unbounded memory growth."""
    while True:
        time.sleep(3600)  # every hour
        with seen_tokens_lock:
            size = len(seen_tokens)
            if size > SEEN_TOKENS_MAX:
                seen_tokens.clear()
                print(f"[PRUNE] Cleared seen_tokens ({size} entries)", flush=True)


def _acquire_background_worker_lock():
    global _background_lock_conn
    if _background_lock_conn is not None:
        return True
    try:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (BACKGROUND_WORKER_LOCK_ID,))
            locked = cur.fetchone()[0]
        if not locked:
            conn.close()
            return False
        _background_lock_conn = conn
        return True
    except Exception as e:
        print(f"[WARN] Background worker lock failed: {e}")
        return False


def ensure_background_workers_started():
    global _background_workers_started
    if _background_workers_started:
        return
    with _background_workers_lock:
        if _background_workers_started:
            return
        if not _acquire_background_worker_lock():
            return
        worker_targets = [
            send_daily_summaries,
            check_whale_wallets,
            global_scanner,
            new_pairs_scanner,
            helius_pool_sniper,
            token_pattern_monitor,
            auto_restart_bots,
            warm_sender_connections,
            listing_scanner,
            process_listing_alerts,
            _prune_seen_tokens,
        ]
        for target in worker_targets:
            threading.Thread(target=target, daemon=True).start()
        _background_workers_started = True


# ── Flask app ──────────────────────────────────────────────────────────────────
from werkzeug.middleware.proxy_fix import ProxyFix
app = Flask(__name__, static_folder="static", static_url_path="/static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)
try:
    ensure_background_workers_started()
except Exception as _e:
    print(f"[WARN] Background workers not started at module load: {_e}")

@app.errorhandler(500)
def handle_500(e):
    import traceback
    traceback.print_exc()
    print(f"[ERROR-500] {e}", flush=True)
    return f"<h1>500</h1><pre>{e}</pre>", 500


def _graceful_shutdown(signum, frame):
    """Stop all running bots and persist positions on SIGTERM/SIGINT."""
    print(f"[SHUTDOWN] Signal {signum} received — stopping all bots...", flush=True)
    with user_bots_lock:
        bots = list(user_bots.values())
    for bot in bots:
        try:
            bot.running = False
        except Exception:
            pass
    # Give bots a moment to finish current sell cycles
    time.sleep(2)
    print("[SHUTDOWN] All bots stopped. Exiting.", flush=True)
    _release_background_lock()
    raise SystemExit(0)

signal.signal(signal.SIGTERM, _graceful_shutdown)
signal.signal(signal.SIGINT, _graceful_shutdown)


@app.before_request
def _before_request_security():
    ensure_background_workers_started()


@app.after_request
def _apply_security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.is_secure:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp

@app.route("/health")
def health_check():
    """Health check endpoint for Railway / load balancers."""
    try:
        conn = db()
        try:
            conn.cursor().execute("SELECT 1")
        finally:
            db_return(conn)
        return jsonify({"status": "ok", "db": "ok"}), 200
    except Exception as e:
        return jsonify({"status": "error", "db": str(e)}), 503

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def is_admin(email=None):
    e = (email or session.get("email", "")).lower()
    return e in ADMIN_EMAILS

def effective_plan(plan, email=None):
    """Admins always get Elite limits regardless of their DB plan."""
    if is_admin(email):
        return "elite"
    return plan

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_admin():
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated


def _plan_from_stripe_subscription(subscription):
    items = (((subscription or {}).get("items") or {}).get("data") or [])
    for item in items:
        price_id = ((item.get("price") or {}).get("id") or "").strip()
        if price_id in PRICE_TO_PLAN:
            return PRICE_TO_PLAN[price_id]
    return None


def _update_user_subscription(user_id, plan=_UNSET, customer_id=_UNSET, subscription_id=_UNSET):
    if not user_id:
        return
    updates = []
    values = []
    if plan is not _UNSET:
        updates.append("plan=%s")
        values.append(plan)
    if customer_id is not _UNSET:
        updates.append("stripe_customer_id=%s")
        values.append(customer_id)
    if subscription_id is not _UNSET:
        updates.append("stripe_subscription_id=%s")
        values.append(subscription_id)
    if not updates:
        return
    values.append(user_id)
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute(f"UPDATE users SET {', '.join(updates)} WHERE id=%s", tuple(values))
        conn.commit()
    finally:
        db_return(conn)

# ── PWA routes ─────────────────────────────────────────────────────────────────
@app.route("/manifest.json")
def manifest():
    return Response(json.dumps({
        "name": "SolTrader",
        "short_name": "SolTrader",
        "description": "Automated Solana trading bot with anti-rug protection",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#07101E",
        "theme_color": "#07101E",
        "orientation": "portrait",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ]
    }), mimetype="application/json")

@app.route("/.well-known/assetlinks.json")
def assetlinks():
    # Replace sha256_cert_fingerprints with your actual keystore fingerprint after signing
    return Response(json.dumps([{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": "com.soltrader.app",
            "sha256_cert_fingerprints": ["B3:4E:0E:5F:A7:5D:DD:C8:A9:4F:75:68:54:73:EA:C2:80:CB:61:26:6F:05:0A:68:7B:DC:F2:A7:B8:16:DA:61"]
        }
    }]), mimetype="application/json")

@app.route("/sw.js")
def service_worker():
    sw = """
const CACHE = 'soltrader-v1';
const SHELL = ['/', '/login', '/signup', '/dashboard'];
self.addEventListener('install', e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL))));
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
"""
    return Response(sw, mimetype="application/javascript")

@app.route("/privacy")
def privacy():
    return """<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Privacy Policy – SolTrader</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:800px;margin:40px auto;padding:0 24px;color:#1a1a2e;line-height:1.7}
h1{color:#0a0a1a;border-bottom:2px solid #14c784;padding-bottom:12px}
h2{color:#0a0a1a;margin-top:32px}
a{color:#14c784}
.updated{color:#888;font-size:14px}
</style></head><body>
<h1>Privacy Policy</h1>
<p class="updated">Last updated: March 12, 2026</p>

<h2>1. Introduction</h2>
<p>SolTrader ("we", "us", or "our") operates the SolTrader platform, available at soltrader-production.up.railway.app and through our Android application. This Privacy Policy explains how we collect, use, and protect your information.</p>

<h2>2. Information We Collect</h2>
<p><strong>Account Information:</strong> Email address and hashed password when you register.</p>
<p><strong>Wallet Information:</strong> Your Solana private key, which is encrypted with AES-256 encryption before storage. We never store your key in plain text.</p>
<p><strong>Trading Data:</strong> Trade history, profit/loss records, and bot activity logs associated with your account.</p>
<p><strong>Usage Data:</strong> Standard server logs including IP address, browser type, and pages visited.</p>

<h2>3. How We Use Your Information</h2>
<ul>
<li>To operate and maintain your trading bot account</li>
<li>To execute trades on your behalf on the Solana blockchain</li>
<li>To process subscription payments via Stripe</li>
<li>To calculate and collect performance fees</li>
<li>To send account-related notifications</li>
</ul>

<h2>4. Non-Custodial Wallet</h2>
<p>SolTrader is a non-custodial platform. Your private key is encrypted and stored solely to execute trades you authorize. You retain full ownership of your wallet and funds at all times. You may delete your private key from our system at any time by contacting us.</p>

<h2>5. Data Sharing</h2>
<p>We do not sell your personal data. We share data only with:</p>
<ul>
<li><strong>Stripe</strong> – for payment processing (subject to Stripe's Privacy Policy)</li>
<li><strong>Helius</strong> – Solana RPC provider for blockchain interactions</li>
<li>Law enforcement when required by law</li>
</ul>

<h2>6. Data Security</h2>
<p>Private keys are encrypted using Fernet (AES-128-CBC). Passwords are hashed using bcrypt. We use HTTPS for all data transmission.</p>

<h2>7. Data Retention</h2>
<p>We retain your data for as long as your account is active. You may request deletion of your account and all associated data by emailing us.</p>

<h2>8. Your Rights</h2>
<p>You have the right to access, correct, or delete your personal data. Contact us at <a href="mailto:founder@drinkwhy.com">founder@drinkwhy.com</a> for any requests.</p>

<h2>9. Children's Privacy</h2>
<p>SolTrader is not intended for users under 18 years of age. We do not knowingly collect data from minors.</p>

<h2>10. Changes to This Policy</h2>
<p>We may update this policy periodically. Continued use of the platform after changes constitutes acceptance.</p>

<h2>11. Contact</h2>
<p>For privacy-related questions: <a href="mailto:founder@drinkwhy.com">founder@drinkwhy.com</a></p>

<p><a href="/">&larr; Back to SolTrader</a></p>
</body></html>"""

# ── Auth routes ────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return Response(LANDING_HTML, mimetype="text/html")

@app.route("/signup", methods=["GET","POST"])
def signup():
    error = ""
    if request.method == "POST":
        email    = request.form.get("email","").strip().lower()
        password = request.form.get("password","")
        if not email or not password:
            error = "Email and password required"
        elif len(password) < 8:
            error = "Password must be at least 8 characters"
        else:
            hashed       = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            signup_plan  = request.args.get("plan", "trial")
            if signup_plan not in ("trial", "free"):
                signup_plan = "trial"
            trial_ends = (datetime.utcnow() + timedelta(days=7)).isoformat() if signup_plan == "trial" else None
            ref_code   = make_referral_code()
            ref_by     = None
            # check if came via referral link
            ref_param = request.args.get("ref") or request.form.get("ref", "")
            if ref_param:
                conn = db()
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT id FROM users WHERE referral_code=%s", (ref_param,))
                    referrer = cur.fetchone()
                    if referrer:
                        ref_by = referrer["id"]
                finally:
                    db_return(conn)
            try:
                conn = db()
                try:
                    cur = conn.cursor()
                    cur.execute(
                        "INSERT INTO users (email, password_hash, plan, trial_ends, referral_code, referred_by) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                        (email, hashed, signup_plan, trial_ends, ref_code, ref_by)
                    )
                    user_id = cur.fetchone()["id"]
                    cur.execute("INSERT INTO bot_settings (user_id) VALUES (%s)", (user_id,))
                    if ref_by:
                        cur.execute("INSERT INTO referrals (referrer_id,referred_id) VALUES (%s,%s)",
                                     (ref_by, user_id))
                    conn.commit()
                finally:
                    db_return(conn)
                session.permanent = True
                session["user_id"] = user_id
                session["email"]   = email
                return redirect(url_for("setup"))
            except Exception as e:
                if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                    error = "Email already registered"
                else:
                    error = f"Signup error: {e}"
    return Response(auth_page("Create Account", "signup", error), mimetype="text/html")

@app.route("/login", methods=["GET","POST"])
def login():
    error = ""
    if request.method == "POST":
        email    = request.form.get("email","").strip().lower()
        password = request.form.get("password","")
        try:
            conn = db()
            try:
                cur = conn.cursor()
                cur.execute("SELECT * FROM users WHERE email=%s", (email,))
                user = cur.fetchone()
            finally:
                db_return(conn)
            if user and bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
                session.permanent = True
                session["user_id"] = user["id"]
                session["email"]   = email
                return redirect(url_for("dashboard"))
            error = "Invalid email or password"
        except Exception as e:
            error = f"Login error: {e}"
    return Response(auth_page("Sign In", "login", error), mimetype="text/html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

# ── Setup ──────────────────────────────────────────────────────────────────────
@app.route("/setup", methods=["GET","POST"])
@login_required
def setup():
    uid = session["user_id"]
    error = ""
    if request.method == "POST":
        private_key = request.form.get("private_key","").strip()
        preset      = normalize_preset_name(request.form.get("preset","steady"))
        run_mode    = request.form.get("run_mode","indefinite")
        duration    = int(request.form.get("run_duration_min",0) or 0)
        profit      = float(request.form.get("profit_target_sol",0) or 0)
        try:
            kp  = Keypair.from_bytes(base58.b58decode(private_key))
            pub = str(kp.pubkey())
            enc = encrypt_key(private_key)
            conn = db()
            try:
                cur = conn.cursor()
                cur.execute("""INSERT INTO wallets (user_id, encrypted_key, public_key) VALUES (%s,%s,%s)
                    ON CONFLICT (user_id) DO UPDATE SET encrypted_key=EXCLUDED.encrypted_key, public_key=EXCLUDED.public_key""",
                    (uid, enc, pub))
                cur.execute("""INSERT INTO bot_settings (user_id, preset, run_mode, run_duration_min, profit_target_sol, is_running)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (user_id) DO UPDATE SET preset=EXCLUDED.preset, run_mode=EXCLUDED.run_mode,
                    run_duration_min=EXCLUDED.run_duration_min, profit_target_sol=EXCLUDED.profit_target_sol, is_running=EXCLUDED.is_running""",
                    (uid, preset, run_mode, duration, profit, 0))
                conn.commit()
            finally:
                db_return(conn)
            return redirect(url_for("dashboard"))
        except Exception as e:
            error = f"Invalid private key: {e}"
    return Response(SETUP_HTML.replace("{{ERROR}}", error), mimetype="text/html")

# ── Dashboard ──────────────────────────────────────────────────────────────────
@app.route("/dashboard")
@login_required
def dashboard():
    try:
        uid = session["user_id"]
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
            user = cur.fetchone()
            cur.execute("SELECT * FROM wallets WHERE user_id=%s", (uid,))
            wallet = cur.fetchone()
            cur.execute("SELECT * FROM bot_settings WHERE user_id=%s", (uid,))
            bsettings = cur.fetchone()
        finally:
            db_return(conn)
        if not user:
            session.clear()
            return redirect(url_for("login"))
        if not wallet:
            return redirect(url_for("setup"))
        plan      = effective_plan(user["plan"], user["email"])
        plan_info = PLAN_LIMITS.get(plan, PLAN_LIMITS["basic"])
        if plan == "elite":
            upgrade_btn = ""
        elif plan == "pro":
            upgrade_btn = '<a href="/subscribe/elite" class="nbtn" style="background:linear-gradient(135deg,#a855f7,#7c3aed)">Go Elite</a>'
        elif plan == "basic":
            upgrade_btn = '<a href="/subscribe/pro" class="nbtn" style="background:linear-gradient(135deg,#14c784,#0fa86a)">Upgrade to Pro</a>'
        elif plan == "free":
            upgrade_btn = '<a href="/subscribe/basic" class="nbtn">Subscribe</a>'
        else:
            upgrade_btn = '<a href="/subscribe/free" class="nbtn" style="background:var(--bg3);border:1px solid var(--grn);color:var(--grn)">Profit Only</a>'
        preset_settings_json = json.dumps(dashboard_preset_settings())
        return Response(DASHBOARD_HTML
            .replace("{{EMAIL}}", user.get("email", ""))
            .replace("{{PLAN_LABEL}}", plan_info.get("label", ""))
            .replace("{{UPGRADE_BTN}}", upgrade_btn)
            .replace("{{PLAN}}", plan_info.get("label", ""))
            .replace("{{WALLET}}", wallet.get("public_key", ""))
            .replace("{{PRESET}}", normalize_preset_name((bsettings or {}).get("preset", "balanced")))
            .replace("{{PRESET_SETTINGS}}", preset_settings_json),
            mimetype="text/html"
        )
    except Exception as e:
        print(f"[ERROR] Dashboard route: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return Response(f"<h1>Dashboard Error</h1><pre>{e}</pre>", status=500, mimetype="text/html")

# ── API ────────────────────────────────────────────────────────────────────────
@app.route("/api/state")
@login_required
def api_state():
    uid = session["user_id"]
    bot = user_bots.get(uid)
    pos_list = []
    preset_name = normalize_preset_name(bot.preset_name if bot else "balanced")
    if bot:
        pos_snapshot = dict(bot.positions)  # thread-safe snapshot
        for mint, p in pos_snapshot.items():
            try:
                cur   = bot.get_token_price(mint)
                ratio = (cur / p["entry_price"]) if cur and p["entry_price"] else None
                pos_list.append({
                    "address":       mint,
                    "name":          p["name"],
                    "entry_price":   p["entry_price"],
                    "current_price": cur,
                    "ratio":         ratio,
                    "pnl":           f"{(ratio-1)*100:+.1f}%" if ratio else "?",
                    "age_min":       round((time.time()-p["timestamp"])/60,1),
                    "tp1_hit":       p["tp1_hit"],
                })
            except Exception:
                pass  # position was sold mid-iteration
    stats = bot.stats if bot else {"wins":0,"losses":0,"total_pnl_sol":0}
    total_trades = stats["wins"] + stats["losses"]
    stats["win_rate"] = round(stats["wins"] / total_trades * 100, 1) if total_trades else 0
    stats["streak"] = -(bot.consecutive_losses) if bot and bot.consecutive_losses > 0 else stats["wins"]
    
    # Load settings from database, not bot memory
    db_settings = {}
    try:
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT preset, custom_settings, max_correlated, drawdown_limit_sol FROM bot_settings WHERE user_id=%s", (uid,))
            row = cur.fetchone()
            if row:
                preset_name = normalize_preset_name(row.get("preset", "balanced"))
                db_settings = dict(PRESETS.get(preset_name, PRESETS["balanced"]))
                if row.get("max_correlated") is not None:
                    db_settings["max_correlated"] = row["max_correlated"]
                if row.get("drawdown_limit_sol") is not None:
                    db_settings["drawdown_limit_sol"] = row["drawdown_limit_sol"]
                if row.get("custom_settings"):
                    try:
                        custom = json.loads(row["custom_settings"])
                        if isinstance(custom, dict):
                            db_settings.update(custom)
                    except Exception:
                        pass
                db_settings = strip_auto_relax_state(db_settings)
        finally:
            db_return(conn)
    except Exception:
        db_settings = strip_auto_relax_state(bot.settings) if bot else {}
    
    return jsonify({
        "running":    bot.running if bot else False,
        "preset":     preset_name,
        "balance":    round(bot.sol_balance, 4) if bot else 0,
        "positions":  pos_list,
        "log":        bot.log[:120] if bot else [],
        "stats":      stats,
        "filter_log": list(bot.filter_log)[:10] if bot else [],
        "settings":   db_settings,
        "adaptive": {
            "relax_level": 0,
            "zero_buy_hours": 0,
            "offpeak_min_change": float(db_settings.get("offpeak_min_change") or 0),
        },
    })

@app.route("/api/start", methods=["POST"])
@login_required
def api_start():
    uid = session["user_id"]
    with user_bots_lock:
        if uid in user_bots and user_bots[uid].running:
            return jsonify({"ok":False,"msg":"Already running"})
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM wallets WHERE user_id=%s", (uid,))
        w = cur.fetchone()
        cur.execute("SELECT * FROM bot_settings WHERE user_id=%s", (uid,))
        bs = cur.fetchone()
        cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
        u = cur.fetchone()
    finally:
        db_return(conn)
    if not w:
        return jsonify({"ok":False,"msg":"Wallet not configured — go to /setup"})
    plan = effective_plan(u["plan"], u["email"])
    if plan == "trial":
        trial_ends = datetime.fromisoformat(u["trial_ends"]) if u["trial_ends"] else datetime.utcnow()
        if datetime.utcnow() > trial_ends:
            return jsonify({"ok":False,"msg":"Trial expired — please subscribe at /subscribe/basic"})
    try:
        kp = Keypair.from_bytes(base58.b58decode(decrypt_key(w["encrypted_key"])))
    except Exception:
        return jsonify({"ok":False,"msg":"Wallet key could not be decrypted — please re-enter your private key at /setup"})
    preset   = normalize_preset_name(bs["preset"] if bs else "balanced")
    settings = dict(PRESETS.get(preset, PRESETS["balanced"]))
    # Apply user-specific overrides saved in DB on top of preset defaults
    if bs:
        if bs.get("max_correlated") is not None:
            settings["max_correlated"] = bs["max_correlated"]
        if bs.get("drawdown_limit_sol") is not None:
            settings["drawdown_limit_sol"] = bs["drawdown_limit_sol"]
        # Apply custom_settings JSON overrides (tp1_mult, stop_loss, etc.)
        if bs.get("custom_settings"):
            try:
                import json as _json
                custom = _json.loads(bs["custom_settings"])
                if isinstance(custom, dict):
                    settings.update(custom)
            except Exception:
                pass
    max_sol  = PLAN_LIMITS.get(plan, PLAN_LIMITS["basic"])["max_buy_sol"]
    settings["max_buy_sol"] = min(settings["max_buy_sol"], max_sol)
    bot = BotInstance(
        uid, kp, settings,
        run_mode          = bs["run_mode"] if bs else "indefinite",
        run_duration_min  = bs["run_duration_min"] if bs else 0,
        profit_target_sol = bs["profit_target_sol"] if bs else 0,
        preset_name       = preset,
    )
    with user_bots_lock:
        user_bots[uid] = bot
    t = threading.Thread(target=bot.run, daemon=True)
    t.start()
    bot.thread = t
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE bot_settings SET is_running=1 WHERE user_id=%s", (uid,))
        conn.commit()
    finally:
        db_return(conn)
    return jsonify({"ok": True})

@app.route("/api/stop", methods=["POST"])
@login_required
def api_stop():
    uid = session["user_id"]
    bot = user_bots.get(uid)
    if bot:
        bot.running = False
        bot.record_perf_fee()
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE bot_settings SET is_running=0 WHERE user_id=%s", (uid,))
            conn.commit()
        finally:
            db_return(conn)
    return jsonify({"ok":True})

@app.route("/api/cashout", methods=["POST"])
@login_required
def api_cashout():
    uid = session["user_id"]
    bot = user_bots.get(uid)
    if bot:
        bot.cashout_all()
    return jsonify({"ok":True})

@app.route("/api/manual-sell", methods=["POST"])
@login_required
def api_manual_sell():
    uid = session["user_id"]
    bot = user_bots.get(uid)
    if not bot:
        return jsonify({"ok": False, "msg": "Bot not running"})
    data = request.get_json(force=True) or {}
    mint = data.get("mint", "").strip()
    pct = float(data.get("pct", 1.0))
    if not mint or mint not in bot.positions:
        return jsonify({"ok": False, "msg": "Position not found"})
    pct = max(0.01, min(1.0, pct))
    try:
        bot.sell(mint, pct, f"Manual sell {int(pct*100)}%")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/settings", methods=["POST"])
@login_required
def api_settings():
    uid  = session["user_id"]
    data = request.json or {}
    preset   = normalize_preset_name(data.get("preset","balanced"))
    run_mode = data.get("run_mode","indefinite")
    duration = int(data.get("run_duration_min",0) or 0)
    profit   = float(data.get("profit_target_sol",0) or 0)
    overrides = build_bot_overrides(data)
    if preset == "custom":
        _, base_settings = load_user_effective_settings(uid)
        if not base_settings:
            base_settings = dict(PRESETS["custom"])
    else:
        base_settings = dict(PRESETS.get(preset, PRESETS["balanced"]))
    base_settings.update(overrides)
    persist_bot_settings(uid, preset, run_mode, duration, profit, base_settings)
    bot = user_bots.get(uid)
    replayed = 0
    if bot:
        bot.settings.update(base_settings)
        bot.preset_name       = preset
        bot.run_mode         = run_mode
        bot.run_duration_min = duration
        bot.profit_target    = profit
        replayed = replay_recent_market_feed(bot)
        bot.log_msg(f"Settings updated — mode {preset}. Re-evaluated {replayed} recent scanner candidates.")
    return jsonify({
        "ok": True,
        "preset": preset,
        "overrides_count": len(overrides),
        "saved_fields": sorted(overrides.keys()),
        "replayed_candidates": replayed,
        "settings": strip_auto_relax_state(base_settings),
        "run_mode": run_mode,
        "run_duration_min": duration,
        "profit_target_sol": profit,
    })

@app.route("/api/chart/<mint>")
@login_required
def api_chart(mint):
    tf_min    = max(1, int(request.args.get("tf", 1)))
    samples   = _price_history.get(mint, [])
    if not samples:
        return jsonify([])
    bucket_sec = tf_min * 60
    candles    = {}
    for ts, price in samples:
        b = int(ts // bucket_sec) * bucket_sec
        if b not in candles:
            candles[b] = {"t": b, "o": price, "h": price, "l": price, "c": price}
        else:
            candles[b]["h"] = max(candles[b]["h"], price)
            candles[b]["l"] = min(candles[b]["l"], price)
            candles[b]["c"] = price
    return jsonify(sorted(candles.values(), key=lambda x: x["t"]))

@app.route("/api/wallet-tree/<mint>")
@login_required
def api_wallet_tree(mint):
    """Fetch recent swap txns for a mint from Helius and return wallet tree data."""
    try:
        from urllib.parse import urlparse, parse_qs
        parsed  = urlparse(HELIUS_RPC)
        api_key = parse_qs(parsed.query).get("api-key", [""])[0] or HELIUS_RPC.split("/")[-1]
        url  = f"https://api.helius.xyz/v0/addresses/{mint}/transactions"
        resp = requests.get(url, params={"api-key": api_key, "limit": 50, "type": "SWAP"}, timeout=10)
        txns = resp.json() if resp.ok else []
        if not isinstance(txns, list):
            txns = []
        buy_wallets  = {}
        sell_wallets = {}
        for tx in txns:
            if not isinstance(tx, dict):
                continue
            fee_payer = tx.get("feePayer", "")
            if not fee_payer:
                continue
            swap = (tx.get("events") or {}).get("swap") or {}
            if not swap:
                continue
            token_outputs = swap.get("tokenOutputs") or []
            token_inputs  = swap.get("tokenInputs")  or []
            native_input  = swap.get("nativeInput")  or {}
            native_output = swap.get("nativeOutput") or {}
            is_buy  = any(t.get("mint") == mint for t in token_outputs)
            is_sell = any(t.get("mint") == mint for t in token_inputs)
            if is_buy:
                amt = native_input.get("amount", 0) or 0
                buy_wallets[fee_payer]  = buy_wallets.get(fee_payer,  0) + amt
            elif is_sell:
                amt = native_output.get("amount", 0) or 0
                sell_wallets[fee_payer] = sell_wallets.get(fee_payer, 0) + amt
        nodes = [{"id": "root", "label": "TOKEN", "type": "token"}]
        edges = []
        if buy_wallets:
            nodes.append({"id": "buys", "label": f"BUYS ({len(buy_wallets)})", "type": "group_buy"})
            edges.append({"from": "root", "to": "buys"})
            for w, amt in sorted(buy_wallets.items(), key=lambda x: -x[1])[:12]:
                nid = "b_" + w
                nodes.append({"id": nid, "label": w[:4]+"…"+w[-4:], "type": "buy",
                               "sol": round(amt / 1e9, 4), "wallet": w})
                edges.append({"from": "buys", "to": nid})
        if sell_wallets:
            nodes.append({"id": "sells", "label": f"SELLS ({len(sell_wallets)})", "type": "group_sell"})
            edges.append({"from": "root", "to": "sells"})
            for w, amt in sorted(sell_wallets.items(), key=lambda x: -x[1])[:12]:
                nid = "s_" + w
                nodes.append({"id": nid, "label": w[:4]+"…"+w[-4:], "type": "sell",
                               "sol": round(amt / 1e9, 4), "wallet": w})
                edges.append({"from": "sells", "to": nid})
        return jsonify({"nodes": nodes, "edges": edges,
                        "total_buys": len(buy_wallets), "total_sells": len(sell_wallets)})
    except Exception as e:
        return jsonify({"nodes": [], "edges": [], "error": str(e)})

@app.route("/api/market-feed")
@login_required
def api_market_feed():
    """Polling endpoint — returns latest market tokens as JSON."""
    since = int(request.args.get("since", 0))
    latest = {}
    for item in list(market_feed):
        mint = item.get("mint")
        ts = int(item.get("ts", 0) or 0)
        if not mint or ts <= since:
            continue
        prev = latest.get(mint)
        if not prev or ts > int(prev.get("ts", 0) or 0):
            merged = dict(item)
            intel = merged.get("intel") or token_intel_payload(_token_intel_cache.get(mint) or {})
            if intel:
                merged["intel"] = intel
                merged["green_lights"] = intel.get("green_lights", 0)
                merged["deployer_score"] = intel.get("deployer_score", 0)
                merged["narrative_score"] = intel.get("narrative_score", 0)
            latest[mint] = merged
    tokens = sorted(latest.values(), key=lambda item: int(item.get("ts", 0) or 0), reverse=True)
    return jsonify(tokens[:40])

@app.route("/api/manual-buy", methods=["POST"])
@login_required
def api_manual_buy():
    uid  = session["user_id"]
    mint = (request.json or {}).get("mint", "").strip()
    name = (request.json or {}).get("name", "Unknown").strip()[:64]
    if not mint or len(mint) < 32 or len(mint) > 64:
        return jsonify({"ok": False, "msg": "Invalid mint address"})
    if not all(c in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz" for c in mint):
        return jsonify({"ok": False, "msg": "Invalid mint address format"})
    bot = user_bots.get(uid)
    if not bot or not bot.running:
        return jsonify({"ok": False, "msg": "Bot not running — start bot first"})
    if mint in bot.positions:
        return jsonify({"ok": False, "msg": "Already in position"})
    # get current price
    try:
        pairs = dex_get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                        timeout=5).json().get("pairs")
        price = float(pairs[0].get("priceUsd") or 0) if pairs else 0
    except Exception as _e:
        print(f"[ERROR] {_e}", flush=True)
        price = 0
    if not price:
        return jsonify({"ok": False, "msg": "Could not fetch price"})
    threading.Thread(target=bot.buy, args=(mint, name, price), kwargs={"liq": 0, "dev_wallet": None}, daemon=True).start()
    return jsonify({"ok": True, "msg": f"Manual buy triggered for {name}"})

@app.route("/api/telegram", methods=["POST"])
@login_required
def api_telegram():
    uid     = session["user_id"]
    chat_id = (request.json or {}).get("chat_id", "").strip()
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET telegram_chat_id=%s WHERE id=%s", (chat_id or None, uid))
        conn.commit()
    finally:
        db_return(conn)
    if chat_id:
        send_telegram(chat_id, "✅ <b>SolTrader</b> connected! You'll receive alerts when your bot buys and sells.")
    return jsonify({"ok": True})

@app.route("/ref/<code>")
def referral_link(code):
    return redirect(url_for("signup") + f"?ref={code}")

@app.route("/api/filter-log")
@login_required
def api_filter_log():
    uid = session["user_id"]
    bot = user_bots.get(uid)
    if bot:
        return jsonify(list(bot.filter_log))
    # fallback to DB
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM filter_log WHERE user_id=%s ORDER BY id DESC LIMIT 30", (uid,))
        rows = cur.fetchall()
    finally:
        db_return(conn)
    return jsonify([dict(r) for r in rows])


# ══════════════════════════════════════════════════════════════════════════════
# ENHANCED TRADING SYSTEM API ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/enhanced/dashboard")
@login_required
def api_enhanced_dashboard():
    """Get enhanced trading dashboard data."""
    uid = session["user_id"]
    bot = user_bots.get(uid)
    
    if not bot or not getattr(bot, 'enhanced_enabled', False):
        return jsonify({"enabled": False, "msg": "Enhanced systems not available"})
    
    try:
        data = {
            "enabled": True,
            "trading_stats": bot.observability.trading_metrics.get_user_stats(uid),
            "system_health": bot.observability.system_metrics.get_health_summary(),
            "active_alerts": [a.to_dict() for a in bot.observability.alert_manager.get_active_alerts()],
            "mev_stats": bot.mev_system.get_health_report(),
            "circuit_breaker": bot.risk_engine.get_circuit_breaker_state(uid),
        }
        return jsonify(data)
    except Exception as e:
        return jsonify({"enabled": False, "error": str(e)})


@app.route("/api/enhanced/whale-activity/<mint>")
@login_required
def api_enhanced_whale_activity(mint):
    """Get whale activity for a specific token."""
    if not ENHANCED_SYSTEMS_AVAILABLE:
        return jsonify({"error": "Enhanced systems not available"})
    
    try:
        whale_system = get_whale_detection_system()
        activity = whale_system.get_token_whale_activity(mint)
        entry_signal, entry_reasons = whale_system.check_whale_entry_signal(mint)
        exit_warning, exit_reasons = whale_system.check_whale_exit_warning(mint)
        
        return jsonify({
            "mint": mint,
            "activity": activity,
            "entry_signal": {"triggered": entry_signal, "reasons": entry_reasons},
            "exit_warning": {"triggered": exit_warning, "reasons": exit_reasons},
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/enhanced/risk-check/<mint>")
@login_required
def api_risk_check(mint):
    """Get risk assessment for a token."""
    if not ENHANCED_SYSTEMS_AVAILABLE:
        return jsonify({"error": "Enhanced systems not available"})
    
    try:
        risk_engine = get_risk_engine(HELIUS_RPC)
        assessment = risk_engine.token_analyzer.analyze_token(mint)
        return jsonify(assessment.to_dict())
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/enhanced/circuit-breaker", methods=["GET", "POST"])
@login_required
def api_circuit_breaker():
    """Get or reset circuit breaker state."""
    uid = session["user_id"]
    
    if not ENHANCED_SYSTEMS_AVAILABLE:
        return jsonify({"error": "Enhanced systems not available"})
    
    try:
        risk_engine = get_risk_engine(HELIUS_RPC)
        
        if request.method == "POST":
            action = (request.json or {}).get("action", "")
            if action == "reset":
                risk_engine.reset_circuit_breaker(uid)
                return jsonify({"ok": True, "msg": "Circuit breaker reset"})
        
        state = risk_engine.get_circuit_breaker_state(uid)
        return jsonify(state)
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/enhanced/mev-stats")
@login_required
def api_mev_stats():
    """Get MEV protection statistics."""
    if not ENHANCED_SYSTEMS_AVAILABLE:
        return jsonify({"error": "Enhanced systems not available"})
    
    try:
        mev_system = get_mev_protection(HELIUS_RPC)
        return jsonify(mev_system.get_health_report())
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/enhanced/observability")
@login_required
def api_observability():
    """Get observability metrics."""
    if not ENHANCED_SYSTEMS_AVAILABLE:
        return jsonify({"error": "Enhanced systems not available"})
    
    try:
        obs = get_observability()
        return jsonify({
            "metrics": obs.get_metrics_export(),
            "alerts": obs.alert_manager.get_alert_history(3600),
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/enhanced/smart-money")
@login_required
def api_smart_money():
    """Get smart money wallet stats."""
    if not ENHANCED_SYSTEMS_AVAILABLE:
        return jsonify({"error": "Enhanced systems not available"})
    
    try:
        whale_system = get_whale_detection_system()
        wallets = whale_system.smart_money_tracker.get_smart_money_wallets()
        return jsonify({
            "smart_money_count": len(wallets),
            "wallets": [w[:8] + "..." for w in list(wallets)[:20]],
        })
    except Exception as e:
        return jsonify({"error": str(e)})




# Price history for candlestick chart — mint → [(unix_ts, price), ...]
_price_history = {}
_PRICE_HISTORY_MAX = 300

def record_price(mint, price):
    if not mint or not price or price <= 0:
        return
    h = _price_history.setdefault(mint, [])
    h.append((time.time(), price))
    if len(h) > _PRICE_HISTORY_MAX:
        del h[0]

# Recent listing alerts (for UI feed) — keep last 50 in memory
_recent_listing_alerts = []
_original_queue_put = listing_alert_queue.put
def _tracked_put(item, *a, **kw):
    _recent_listing_alerts.insert(0, item)
    if len(_recent_listing_alerts) > 50:
        _recent_listing_alerts.pop()
    return _original_queue_put(item, *a, **kw)
listing_alert_queue.put = _tracked_put

@app.route("/api/listing-alerts")
@login_required
def api_listing_alerts():
    return jsonify(_recent_listing_alerts[:20])

@app.route("/api/signal-explorer")
@login_required
def api_signal_explorer():
    uid = session["user_id"]
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT payload_json, ts
            FROM signal_explorer_log
            WHERE user_id=%s
            ORDER BY ts DESC
        """, (uid,))
        rows = cur.fetchall()
    finally:
        db_return(conn)
    recent = []
    for row in rows:
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except Exception:
            payload = {}
        if isinstance(payload, dict) and payload:
            if row.get("ts") and not payload.get("logged_at"):
                payload["logged_at"] = row["ts"].isoformat()
            recent.append(payload)
    total = len(recent)
    passed = sum(1 for s in recent if s.get("passed"))
    reasons = {}
    for s in recent:
        if not s.get("passed") and s.get("reason"):
            reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1
    top_reasons = sorted(reasons.items(), key=lambda x: -x[1])[:5]
    return jsonify({
        "recent": recent,
        "stats": {
            "total_evaluated": total,
            "pass_rate": round(passed / total * 100, 1) if total > 0 else 0,
            "top_reject_reasons": [{"reason": r, "count": c} for r, c in top_reasons],
        }
    })


@app.route("/api/pattern-lab")
@login_required
def api_pattern_lab():
    tokens = []
    with _token_intel_lock:
        tokens = [token_intel_payload(row) for row in _token_intel_cache.values()]
    if len(tokens) < 10:
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT *
                FROM token_intel
                ORDER BY max_multiple DESC, last_updated DESC
                LIMIT 40
            """)
            tokens.extend(token_intel_payload(row) for row in cur.fetchall())
        finally:
            db_return(conn)
    deduped = {}
    for token in tokens:
        mint = token.get("mint")
        if mint and mint not in deduped:
            deduped[mint] = token
    ranked = sorted(
        deduped.values(),
        key=lambda token: (
            int(token.get("whale_score") or 0),
            float(token.get("max_multiple") or 1),
            int(token.get("green_lights") or 0),
            int(token.get("narrative_score") or 0),
        ),
        reverse=True,
    )[:10]
    tag_counts = {}
    for token in ranked:
        for tag in token.get("narrative_tags") or []:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    deployers = []
    for token in ranked:
        wallet = token.get("deployer_wallet")
        if not wallet:
            continue
        stats = get_deployer_stats(wallet)
        deployers.append({
            "wallet": wallet,
            "reputation_score": int(stats.get("reputation_score") or 0),
            "launches_total": int(stats.get("launches_total") or 0),
            "wins_2x": int(stats.get("wins_2x") or 0),
            "wins_5x": int(stats.get("wins_5x") or 0),
            "best_multiple": float(stats.get("best_multiple") or 1),
        })
    deployers = sorted(deployers, key=lambda row: (row["reputation_score"], row["best_multiple"]), reverse=True)
    return jsonify({
        "tokens": [{
            "mint": token.get("mint"),
            "name": token.get("name"),
            "symbol": token.get("symbol"),
            "max_multiple": round(float(token.get("max_multiple") or 1), 2),
            "green_lights": int(token.get("green_lights") or 0),
            "deployer_wallet": token.get("deployer_wallet"),
            "deployer_score": int(token.get("deployer_score") or 0),
            "whale_score": int(token.get("whale_score") or 0),
            "whale_action_score": int(token.get("whale_action_score") or 0),
            "cluster_confidence": int(token.get("cluster_confidence") or 0),
            "infra_penalty": int(token.get("infra_penalty") or 0),
            "narrative_score": int(token.get("narrative_score") or 0),
            "narrative_tags": token.get("narrative_tags") or [],
            "holder_growth_1h": float(token.get("holder_growth_1h") or 0),
            "volume_spike_ratio": float(token.get("volume_spike_ratio") or 0),
            "threat_risk_score": int(token.get("threat_risk_score") or 0),
        } for token in ranked],
        "deployers": deployers[:6],
        "themes": [{"tag": tag, "count": count} for tag, count in sorted(tag_counts.items(), key=lambda item: -item[1])[:6]],
    })


@app.route("/api/ops-metrics")
@login_required
def api_ops_metrics():
    uid = session["user_id"]
    bot = user_bots.get(uid)
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT side, phase, ok, latency_ms, slippage_bps, route_source, failure_reason, created_at
            FROM execution_events
            WHERE user_id=%s AND created_at >= NOW() - INTERVAL '1 day'
            ORDER BY created_at DESC
            LIMIT 300
        """, (uid,))
        exec_rows = cur.fetchall()
        cur.execute("""
            SELECT mint, name, symbol, whale_score, whale_action_score, cluster_confidence, infra_penalty,
                   deployer_score, max_multiple, narrative_tags, threat_risk_score, threat_flags,
                   transfer_hook_enabled, can_exit, liquidity_drop_pct, last_updated
            FROM token_intel
            ORDER BY whale_score DESC, whale_action_score DESC, last_updated DESC
            LIMIT 30
        """)
        token_rows = [token_intel_payload(row) for row in cur.fetchall()]
    finally:
        db_return(conn)

    def _phase_rows(phase):
        return [row for row in exec_rows if row.get("phase") == phase]

    def _rate(rows):
        return round(sum(1 for row in rows if row.get("ok")) / len(rows) * 100, 1) if rows else 0.0

    def _avg_latency(rows):
        vals = [int(row.get("latency_ms") or 0) for row in rows if int(row.get("latency_ms") or 0) > 0]
        return round(sum(vals) / len(vals), 1) if vals else 0.0

    quote_rows = _phase_rows("quote")
    send_rows = _phase_rows("send")
    fill_rows = [row for row in _phase_rows("fill") if row.get("slippage_bps") is not None]
    fill_slips = [int(row.get("slippage_bps") or 0) for row in fill_rows]
    route_mix = {}
    fail_reasons = {}
    for row in exec_rows:
        route = row.get("route_source") or "unknown"
        route_mix[route] = route_mix.get(route, 0) + 1
        if not row.get("ok") and row.get("failure_reason"):
            fail_reasons[row["failure_reason"]] = fail_reasons.get(row["failure_reason"], 0) + 1

    top_whales = []
    threat_map = []
    liquidity_risks = []
    transfer_hook_count = 0
    no_exit_count = 0
    for token in token_rows:
        if bool(token.get("transfer_hook_enabled")):
            transfer_hook_count += 1
        if token.get("can_exit") is False:
            no_exit_count += 1
        top_whales.append({
            "mint": token.get("mint"),
            "name": token.get("name") or token.get("symbol") or "?",
            "whale_score": int(token.get("whale_score") or 0),
            "whale_action_score": int(token.get("whale_action_score") or 0),
            "cluster_confidence": int(token.get("cluster_confidence") or 0),
            "infra_penalty": int(token.get("infra_penalty") or 0),
            "deployer_score": int(token.get("deployer_score") or 0),
            "max_multiple": round(float(token.get("max_multiple") or 1), 2),
        })
        if int(token.get("threat_risk_score") or 0) > 0 or bool(token.get("transfer_hook_enabled")) or token.get("can_exit") is False:
            threat_map.append({
                "mint": token.get("mint"),
                "name": token.get("name") or token.get("symbol") or "?",
                "threat_risk_score": int(token.get("threat_risk_score") or 0),
                "threat_flags": (token.get("threat_flags") or [])[:3],
                "can_exit": token.get("can_exit"),
                "transfer_hook_enabled": bool(token.get("transfer_hook_enabled")),
            })
        if float(token.get("liquidity_drop_pct") or 0) > 0:
            liquidity_risks.append({
                "mint": token.get("mint"),
                "name": token.get("name") or token.get("symbol") or "?",
                "liquidity_drop_pct": round(float(token.get("liquidity_drop_pct") or 0), 1),
                "threat_risk_score": int(token.get("threat_risk_score") or 0),
            })

    top_whales = sorted(top_whales, key=lambda row: (row["whale_score"], row["whale_action_score"], row["cluster_confidence"]), reverse=True)[:6]
    threat_map = sorted(threat_map, key=lambda row: row["threat_risk_score"], reverse=True)[:6]
    liquidity_risks = sorted(liquidity_risks, key=lambda row: (row["liquidity_drop_pct"], row["threat_risk_score"]), reverse=True)[:6]

    return jsonify({
        "stats": {
            "quotes_24h": len(quote_rows),
            "quote_success_rate": _rate(quote_rows),
            "avg_quote_ms": _avg_latency(quote_rows),
            "sends_24h": len(send_rows),
            "send_success_rate": _rate(send_rows),
            "avg_send_ms": _avg_latency(send_rows),
            "avg_fill_slippage_bps": round(sum(fill_slips) / len(fill_slips), 1) if fill_slips else 0.0,
            "transfer_hook_count": transfer_hook_count,
            "no_exit_count": no_exit_count,
        },
        "rpc_health": rpc_health_snapshot(window_sec=900),
        "adaptive": {
            "relax_level": 0,
            "zero_buy_hours": 0,
            "offpeak_min_change": float((bot.settings if bot else {}).get("offpeak_min_change") or 0),
            "last_relax_at": None,
        },
        "top_whales": top_whales,
        "threat_map": threat_map,
        "liquidity_risks": liquidity_risks,
        "route_mix": [{"route": route, "count": count} for route, count in sorted(route_mix.items(), key=lambda item: -item[1])[:6]],
        "failure_reasons": [{"reason": reason, "count": count} for reason, count in sorted(fail_reasons.items(), key=lambda item: -item[1])[:6]],
    })

@app.route("/api/pnl-history")
@login_required
def api_pnl_history():
    uid = session["user_id"]
    days = request.args.get("days", "7")
    interval = "7 days" if days == "7" else "1 day" if days == "1" else "365 days"
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, mint, name, action, price, pnl_sol, timestamp
            FROM trades WHERE user_id=%s AND timestamp >= NOW() - INTERVAL '""" + interval + """'
            ORDER BY timestamp ASC
        """, (uid,))
        trades = cur.fetchall()
        cur.execute("""
            SELECT session_id, pnl_sol, fee_sol, created_at
            FROM perf_fees WHERE user_id=%s ORDER BY created_at DESC LIMIT 50
        """, (uid,))
        fees = cur.fetchall()
    finally:
        db_return(conn)
    cumulative = []
    running_pnl = 0
    peak_pnl = 0
    max_dd = 0
    wins = 0
    losses = 0
    best_t = 0
    worst_t = 0
    for t in trades:
        pnl = t["pnl_sol"] or 0
        action = str(t.get("action") or "").upper()
        if action.startswith("SELL"):
            running_pnl += pnl
            if pnl > 0: wins += 1
            else: losses += 1
            best_t = max(best_t, pnl)
            worst_t = min(worst_t, pnl)
        peak_pnl = max(peak_pnl, running_pnl)
        dd = peak_pnl - running_pnl
        max_dd = max(max_dd, dd)
        cumulative.append({
            "ts": t["timestamp"].isoformat() if hasattr(t["timestamp"], "isoformat") else str(t["timestamp"]),
            "pnl": round(running_pnl, 4),
            "drawdown": round(dd, 4),
        })
    total_t = wins + losses
    return jsonify({
        "cumulative": cumulative,
        "stats": {
            "total_pnl": round(running_pnl, 4),
            "total_trades": total_t,
            "wins": wins, "losses": losses,
            "win_rate": round(wins / total_t * 100, 1) if total_t else 0,
            "best_trade": round(best_t, 4),
            "worst_trade": round(worst_t, 4),
            "max_drawdown": round(max_dd, 4),
        },
        "fees": [{
            "session_id": f["session_id"],
            "pnl_sol": f["pnl_sol"],
            "fee_sol": f["fee_sol"],
            "created_at": f["created_at"].isoformat() if hasattr(f["created_at"], "isoformat") else str(f.get("created_at", "")),
        } for f in fees],
    })

@app.route("/api/position-analytics")
@login_required
def api_position_analytics():
    uid = session["user_id"]
    bot = user_bots.get(uid)
    if not bot:
        return jsonify({"positions": [], "risk": {}})
    positions = []
    pos_snapshot = dict(bot.positions)  # thread-safe snapshot
    for mint, p in pos_snapshot.items():
        try:
            cur_price = bot.get_token_price(mint)
            ratio = (cur_price / p["entry_price"]) if cur_price and p["entry_price"] else None
            peak_ratio = (p.get("peak_price", p["entry_price"]) / p["entry_price"]) if p["entry_price"] else None
            chart_data = [{"ts": ts, "price": pr} for ts, pr in _price_history.get(mint, [])[-50:]]
            positions.append({
                "mint": mint, "name": p["name"],
                "entry_price": p["entry_price"], "current_price": cur_price,
                "peak_price": p.get("peak_price", p["entry_price"]),
                "ratio": ratio, "peak_ratio": peak_ratio,
                "pnl_pct": round((ratio - 1) * 100, 1) if ratio else 0,
                "age_min": round((time.time() - p["timestamp"]) / 60, 1),
                "tp1_hit": p["tp1_hit"], "entry_sol": p["entry_sol"],
                "dev_wallet": p.get("dev_wallet"),
                "chart": chart_data,
                "tp1_price": p["entry_price"] * bot.settings.get("tp1_mult", 1.5),
                "tp2_price": p["entry_price"] * bot.settings.get("tp2_mult", 2.0),
                "sl_price": p["entry_price"] * bot.settings.get("stop_loss", 0.75),
            })
        except Exception:
            pass  # position was sold mid-iteration
    risk = {
        "session_drawdown": round(bot.session_drawdown, 4),
        "drawdown_limit": bot.settings.get("drawdown_limit_sol", 0.5),
        "consecutive_losses": bot.consecutive_losses,
        "cooldown_remaining": 0,
        "peak_balance": round(bot.peak_balance, 4),
        "current_balance": round(bot.sol_balance, 4),
        "positions_count": len(bot.positions),
        "max_positions": bot.settings.get("max_correlated", 5),
    }
    return jsonify({"positions": positions, "risk": risk})

@app.route("/api/whale-activity")
@login_required
def api_whale_activity():
    recent = list(_whale_buys)[:50]
    for entry in recent:
        try:
            hist = _price_history.get(entry["mint"], [])
            cur_price = hist[-1][1] if hist else None
            entry["current_price"] = cur_price
            entry["pnl_pct"] = round((cur_price / entry["price"] - 1) * 100, 1) if cur_price and entry.get("price") else None
        except Exception:
            entry["current_price"] = None
            entry["pnl_pct"] = None
    whale_stats = []
    for w in WHALE_WALLETS:
        buys = [b for b in recent if b["wallet"] == w]
        whale_stats.append({
            "label": WHALE_LABELS.get(w, w[:8]+"..."), "address": w,
            "buys_24h": len(buys),
            "avg_pnl": round(sum(b.get("pnl_pct", 0) or 0 for b in buys) / len(buys), 1) if buys else 0,
        })
    return jsonify({"recent_buys": recent, "whale_stats": whale_stats})

@app.route("/api/ai-recommendation")
@login_required
def api_ai_recommendation():
    stats = get_market_stats()
    suggestion = ai_suggest_settings(stats)
    return jsonify({
        "stats": stats,
        "suggestion": suggestion,
        "logic": "Uses last 24h realized trade outcomes: trade count, win rate, average win, average loss, and expectancy."
    })


@app.route("/api/admin/suggest-settings")
@admin_required
def api_suggest_settings():
    return api_ai_recommendation()

@app.route("/api/admin/blacklist-dev", methods=["POST"])
@admin_required
def api_blacklist_dev():
    dw = request.json.get("dev_wallet","").strip()
    if dw:
        blacklist_dev(dw, "manual-admin")
        return jsonify({"ok": True})
    return jsonify({"ok": False})

@app.route("/api/admin/dev-blacklist")
@admin_required
def api_dev_blacklist():
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM dev_blacklist ORDER BY created_at DESC LIMIT 50")
        rows = cur.fetchall()
    finally:
        db_return(conn)
    return jsonify([dict(r) for r in rows])

@app.route("/api/admin/override-preset", methods=["POST"])
@admin_required
def api_override_preset():
    data   = request.json or {}
    preset = normalize_preset_name(data.get("preset", "balanced"))
    if preset in PRESETS:
        overrides = {}
        for k, v in data.items():
            if k == "preset" or k not in PRESETS[preset]:
                continue
            PRESETS[preset][k] = v
            overrides[k] = v
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO preset_overrides (preset, overrides_json, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (preset)
                DO UPDATE SET overrides_json=EXCLUDED.overrides_json, updated_at=NOW()
            """, (preset, json.dumps(overrides)))
            cur.execute("SELECT user_id FROM bot_settings WHERE preset=%s AND is_running=1", (preset,))
            rows = cur.fetchall()
            conn.commit()
        finally:
            db_return(conn)
        for row in rows:
            bot = user_bots.get(row["user_id"])
            if bot:
                bot.settings.update(PRESETS[preset])
        return jsonify({"ok": True})
    return jsonify({"ok": False})

@app.route("/api/referral")
@login_required
def api_referral():
    uid = session["user_id"]
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT referral_code, referral_earnings_sol FROM users WHERE id=%s", (uid,))
        u = cur.fetchone()
        cur.execute("SELECT COUNT(*) as c FROM referrals WHERE referrer_id=%s", (uid,))
        count = cur.fetchone()["c"]
    finally:
        db_return(conn)
    code = u["referral_code"] or ""
    link = f"https://soltrader-production.up.railway.app/ref/{code}"
    return jsonify({"ok": True, "code": code, "link": link,
                    "referrals": count, "earnings_sol": u["referral_earnings_sol"] or 0})

# ── Stripe ─────────────────────────────────────────────────────────────────────
@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET:
        return Response("Webhook not configured", status=503)
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        return Response("Invalid payload", status=400)
    except stripe.error.SignatureVerificationError:
        return Response("Invalid signature", status=400)

    event_type = event.get("type", "")
    data = (event.get("data") or {}).get("object") or {}

    if event_type == "checkout.session.completed":
        metadata = data.get("metadata") or {}
        user_id = int(metadata.get("user_id") or data.get("client_reference_id") or 0)
        plan = metadata.get("plan") or ""
        _update_user_subscription(
            user_id,
            plan=plan if plan else _UNSET,
            customer_id=data.get("customer"),
            subscription_id=data.get("subscription"),
        )
    elif event_type in {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}:
        customer_id = data.get("customer")
        subscription_id = data.get("id")
        status = (data.get("status") or "").lower()
        plan = _plan_from_stripe_subscription(data)
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id FROM users
                WHERE stripe_subscription_id=%s OR stripe_customer_id=%s
                LIMIT 1
                """,
                (subscription_id, customer_id),
            )
            row = cur.fetchone()
        finally:
            db_return(conn)
        if row:
            resolved_plan = plan if status in {"trialing", "active"} else "free"
            _update_user_subscription(
                row["id"],
                plan=resolved_plan,
                customer_id=customer_id,
                subscription_id=subscription_id if status in {"trialing", "active"} else None,
            )
    return jsonify({"ok": True})


@app.route("/subscribe/<plan>")
@login_required
def subscribe(plan):
    uid   = session["user_id"]
    email = session["email"]
    # Profit-only plan has no subscription — just activate it directly
    if plan == "free":
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT stripe_subscription_id FROM users WHERE id=%s", (uid,))
            user = cur.fetchone()
            if user and user.get("stripe_subscription_id"):
                return "Cancel the active Stripe subscription before switching to the free plan.", 400
            cur.execute("UPDATE users SET plan='free' WHERE id=%s", (uid,))
            conn.commit()
        finally:
            db_return(conn)
        return redirect(url_for("dashboard"))
    price_map = {"basic": STRIPE_PRICE_BASIC, "pro": STRIPE_PRICE_PRO, "elite": STRIPE_PRICE_ELITE}
    price_id  = price_map.get(plan, "")
    if plan not in price_map:
        return "Unknown plan.", 400
    if not price_id or not STRIPE_SECRET:
        return "Stripe not configured.", 500
    try:
        checkout = stripe.checkout.Session.create(
            client_reference_id=str(uid),
            customer_email=email,
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            metadata={"user_id": str(uid), "plan": plan},
            mode="subscription",
            success_url=request.host_url.rstrip("/") + url_for("subscribe_success") + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.host_url + "dashboard",
        )
        return redirect(checkout.url)
    except Exception as e:
        return f"Stripe error: {e}", 500

@app.route("/subscribe/success")
@login_required
def subscribe_success():
    uid = session["user_id"]
    session_id = request.args.get("session_id", "").strip()
    if not session_id:
        return "Missing checkout session.", 400
    try:
        checkout = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        return f"Stripe verification error: {e}", 400

    metadata = checkout.get("metadata") or {}
    linked_user_id = str(metadata.get("user_id") or checkout.get("client_reference_id") or "")
    if linked_user_id != str(uid):
        return "Checkout session does not match the current account.", 403
    if checkout.get("mode") != "subscription" or checkout.get("status") != "complete":
        return "Checkout session is not complete.", 400
    if checkout.get("payment_status") not in {"paid", "no_payment_required"}:
        return "Payment has not been finalized.", 400

    plan = metadata.get("plan") or ""
    if not plan and checkout.get("subscription"):
        try:
            subscription = stripe.Subscription.retrieve(checkout["subscription"])
            plan = _plan_from_stripe_subscription(subscription) or ""
        except Exception:
            plan = ""
    if plan not in {"basic", "pro", "elite"}:
        return "Could not determine subscribed plan.", 400

    _update_user_subscription(
        uid,
        plan=plan,
        customer_id=checkout.get("customer"),
        subscription_id=checkout.get("subscription"),
    )
    return redirect(url_for("dashboard"))

# ── Admin ──────────────────────────────────────────────────────────────────────
@app.route("/admin")
@login_required
def admin():
    return redirect(url_for("dashboard"))

# ── HTML Templates ─────────────────────────────────────────────────────────────
_CSS = """<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#07101E">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="SolTrader">
<link rel="manifest" href="/manifest.json">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script>if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');</script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#07101E;--surf:#0C1829;--card:#101F32;--b1:#1A2E45;--b2:#1E3450;
  --bg2:#0C1829;--bg3:#101F32;--bdr:#1A2E45;
  --t1:#E2E8F0;--t2:#94A3B8;--t3:#475569;
  --blue:#2563EB;--blue2:#3B82F6;--blue3:#1D4ED8;
  --grn:#14c784;--grn2:#10B981;--red:#DC2626;--red2:#EF4444;--gold:#D97706;--gold2:#F59E0B;
}
html,body{min-height:100vh}
body{background:var(--bg);color:var(--t1);font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}
a{color:var(--blue2);text-decoration:none}
.nav{background:rgba(7,16,30,.97);border-bottom:1px solid var(--b1);padding:0 28px;height:58px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;backdrop-filter:blur(14px)}
.logo{font-size:15px;font-weight:700;color:var(--t1);display:flex;align-items:center;gap:9px;letter-spacing:-.2px;text-decoration:none}
.logo-mark{width:28px;height:28px;background:linear-gradient(135deg,#2563EB,#60A5FA);border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:800;color:#fff;flex-shrink:0}
.nav-r{display:flex;align-items:center;gap:20px}
.nav-r a{color:var(--t2);font-size:13px;font-weight:500;transition:.15s;text-decoration:none}
.nav-r a:hover{color:var(--t1)}
.nbtn{background:var(--blue) !important;color:#fff !important;padding:7px 16px;border-radius:6px;font-weight:600 !important}
.nbtn:hover{background:var(--blue3) !important;color:#fff !important}
.center-page{display:flex;align-items:center;justify-content:center;min-height:calc(100vh - 58px);padding:32px 16px}
.wrap{max-width:960px;margin:0 auto;padding:32px 24px}
.card{background:var(--card);border:1px solid var(--b1);border-radius:12px;padding:28px}
.panel{background:var(--card);border:1px solid var(--b1);border-radius:10px;padding:18px;margin-bottom:14px}
.page-title{font-size:21px;font-weight:700;letter-spacing:-.3px;margin-bottom:4px}
.sec-label{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:1.2px;color:var(--t3);margin-bottom:12px}
.fgroup{margin-bottom:14px}
.flabel{display:block;font-size:12px;font-weight:500;color:var(--t2);margin-bottom:5px}
.finput{width:100%;background:var(--surf);border:1px solid var(--b2);color:var(--t1);border-radius:7px;padding:9px 13px;font-size:13.5px;font-family:inherit;transition:.2s;display:block}
.finput:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px rgba(37,99,235,.15)}
.finput::placeholder{color:var(--t3)}
select.finput{cursor:pointer}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;border:none;border-radius:7px;padding:9px 18px;font-size:13px;font-weight:600;font-family:inherit;cursor:pointer;transition:.15s;text-decoration:none;line-height:1}
.btn-full{width:100%}
.btn-primary{background:var(--blue);color:#fff}.btn-primary:hover{background:var(--blue3)}
.btn-success{background:var(--grn);color:#fff}.btn-success:hover{background:#047857}
.btn-danger{background:var(--red);color:#fff}.btn-danger:hover{background:#B91C1C}
.btn-ghost{background:transparent;border:1px solid var(--b2);color:var(--t2)}.btn-ghost:hover{color:var(--t1);border-color:var(--t3)}
.btn-outline{background:transparent;border:1px solid var(--blue);color:var(--blue2)}.btn-outline:hover{background:var(--blue);color:#fff}
.btn-gold{background:var(--gold);color:#fff}.btn-gold:hover{background:#B45309}
.alert{padding:10px 14px;border-radius:7px;font-size:13px;margin-bottom:14px}
.alert:empty{display:none}
.alert-error{background:rgba(220,38,38,.1);border:1px solid rgba(220,38,38,.25);color:#FCA5A5}
.alert-info{background:rgba(37,99,235,.08);border:1px solid rgba(37,99,235,.2);color:#93C5FD}
.alert-success{background:rgba(5,150,105,.1);border:1px solid rgba(5,150,105,.25);color:#6EE7B7}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-bottom:16px}
.stat{background:var(--card);border:1px solid var(--b1);border-radius:10px;padding:15px 16px}
.slabel{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.8px;color:var(--t3);margin-bottom:7px}
.sval{font-size:22px;font-weight:700;letter-spacing:-.5px;color:var(--t1)}
.ssub{font-size:11px;color:var(--t3);margin-top:2px}
.c-grn{color:var(--grn2)}.c-red{color:var(--red2)}.c-gold{color:var(--gold2)}.c-blue{color:var(--blue2)}.c-muted{color:var(--t3)}
.status{display:flex;align-items:center;gap:7px}
.sdot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.sdot-on{background:var(--grn2);box-shadow:0 0 8px var(--grn2);animation:blink 2s infinite}
.sdot-off{background:var(--t3)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.stxt{font-size:12px;font-weight:500;color:var(--t2)}
.tbl{width:100%;border-collapse:collapse}
.tbl th{font-size:10px;font-weight:600;color:var(--t3);text-transform:uppercase;letter-spacing:.8px;padding:7px 10px;border-bottom:1px solid var(--b1);text-align:left}
.tbl td{padding:9px 10px;border-bottom:1px solid rgba(26,46,69,.4);font-size:12.5px;color:var(--t2)}
.tbl tr:last-child td{border:none}
.tbl tbody tr:hover td{background:rgba(255,255,255,.02)}
.log{background:var(--surf);border:1px solid var(--b1);border-radius:10px;padding:14px;height:210px;overflow-y:auto}
.lline{font-size:12px;padding:4px 6px;border-bottom:1px solid rgba(255,255,255,.04);color:#94a3b8;font-family:'SF Mono','Courier New',monospace;line-height:1.65;border-radius:3px}
.lline:hover{background:rgba(255,255,255,.03)}
.lbuy{color:#4ade80!important;font-weight:600}.lsell{color:#f87171!important;font-weight:600}.lsig{color:#fbbf24!important;font-weight:600}.linfo{color:#60a5fa!important}.lscan{color:#818cf8}
.badge{display:inline-flex;padding:2px 7px;border-radius:20px;font-size:10px;font-weight:600;letter-spacing:.2px}
.bg-grn{background:rgba(5,150,105,.15);color:var(--grn2)}
.bg-red{background:rgba(220,38,38,.15);color:var(--red2)}
.bg-blue{background:rgba(37,99,235,.15);color:var(--blue2)}
.bg-gold{background:rgba(217,119,6,.15);color:var(--gold2)}
.bg-muted{background:rgba(71,85,105,.15);color:var(--t3)}
.divider{border:none;border-top:1px solid var(--b1);margin:16px 0}
.row{display:flex;gap:12px;flex-wrap:wrap}
.field-row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
.field-row .fgroup{flex:1;min-width:90px;margin-bottom:0}
.txt-sm{font-size:12px;color:var(--t3);text-align:center;margin-top:14px}
.plans{display:flex;gap:16px;flex-wrap:wrap;justify-content:center}
.plan{background:var(--card);border:1px solid var(--b1);border-radius:12px;padding:24px;flex:1;min-width:200px;max-width:280px;position:relative}
.plan.hot{border-color:var(--blue)}
.plan-tag{position:absolute;top:-11px;left:50%;transform:translateX(-50%);background:var(--blue);color:#fff;font-size:10px;font-weight:700;padding:3px 12px;border-radius:20px;text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}
.plan-name{font-size:11px;font-weight:600;color:var(--t2);text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px}
.plan-price{font-size:28px;font-weight:700;letter-spacing:-1px;color:var(--t1)}
.plan-price sub{font-size:13px;font-weight:400;color:var(--t3)}
.plan-fee{font-size:12px;color:var(--t3);margin:4px 0 16px}
.features{list-style:none;font-size:12.5px;color:var(--t2);line-height:2.2}
.features li:before{content:'✓  ';color:var(--grn2);font-weight:700}
.trust{display:flex;flex-wrap:wrap;gap:10px 20px;justify-content:center}
.titem{display:flex;align-items:center;gap:5px;font-size:11.5px;color:var(--t3)}
</style>"""

# ── Landing Page ───────────────────────────────────────────────────────────────
LANDING_HTML = _CSS + """
<style>
.lp-feat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin-bottom:60px}
.lp-feat{background:var(--bg2);border:1px solid var(--bdr);border-radius:14px;padding:22px 18px}
.lp-feat-icon{width:42px;height:42px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:20px;margin-bottom:12px}
.lp-feat h3{font-size:13.5px;font-weight:700;color:var(--t1);margin-bottom:5px}
.lp-feat p{font-size:12px;color:var(--t2);line-height:1.65}
.plans3{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px;margin-bottom:48px}
.plan3{background:var(--bg2);border:1px solid var(--bdr);border-radius:16px;padding:28px 22px;position:relative}
.plan3.hot{border-color:var(--grn);box-shadow:0 0 0 1px rgba(20,199,132,.3),0 8px 32px rgba(20,199,132,.08)}
.plan3.elite{border-color:#a855f7;box-shadow:0 0 0 1px rgba(168,85,247,.3),0 8px 32px rgba(168,85,247,.08)}
.plan3-tag{position:absolute;top:-11px;left:50%;transform:translateX(-50%);font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;padding:3px 12px;border-radius:20px;white-space:nowrap}
.plan3-tag.grn{background:var(--grn);color:#000}
.plan3-tag.purple{background:#a855f7;color:#fff}
.plan3-name{font-size:17px;font-weight:800;margin-bottom:4px}
.plan3-price{font-size:38px;font-weight:900;letter-spacing:-1.5px;line-height:1}
.plan3-price sub{font-size:15px;font-weight:500;color:var(--t2);letter-spacing:0}
.plan3-perf{font-size:11px;color:var(--t3);margin:6px 0 18px}
.plan3 ul{list-style:none;padding:0;margin:0 0 20px;display:flex;flex-direction:column;gap:8px}
.plan3 li{font-size:12.5px;color:var(--t2);display:flex;align-items:flex-start;gap:7px}
.plan3 li::before{content:"✓";color:var(--grn);font-weight:700;flex-shrink:0;margin-top:1px}
.plan3 li.purple-check::before{color:#a855f7}
.cmp-tbl{width:100%;border-collapse:collapse;font-size:12.5px}
.cmp-tbl th{padding:10px 14px;text-align:left;color:var(--t3);font-weight:600;border-bottom:1px solid var(--bdr);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
.cmp-tbl td{padding:10px 14px;border-bottom:1px solid var(--bdr);color:var(--t2)}
.cmp-tbl tr:last-child td{border-bottom:none}
.cmp-tbl .yes{color:#14c784;font-weight:700}
.cmp-tbl .no{color:var(--t3)}
.cmp-tbl .col-h{color:var(--t1);font-weight:600}
.sniper-badge{display:inline-flex;align-items:center;gap:5px;background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.3);color:#f59e0b;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600}
</style>

<nav class="nav">
  <a href="/" class="logo"><div class="logo-mark">S</div>SolTrader</a>
  <div class="nav-r">
    <a href="#pricing">Pricing</a>
    <a href="/login">Sign In</a>
    <a href="/signup" class="nbtn">Get Started Free</a>
  </div>
</nav>

<!-- Hero -->
<div style="text-align:center;padding:80px 24px 60px;max-width:680px;margin:0 auto">
  <div class="sniper-badge" style="margin-bottom:20px">NEW &nbsp;·&nbsp; Signal Explorer + Whale Copy Dashboard</div>
  <h1 style="font-size:44px;font-weight:800;line-height:1.1;letter-spacing:-1.5px;margin-bottom:18px">
    The Most Advanced<br>Solana Trading Bot
  </h1>
  <p style="font-size:15.5px;color:var(--t2);max-width:480px;margin:0 auto 38px;line-height:1.75">
    See WHY every token is accepted or rejected. Track whale wallets in real time. Visualize your P&amp;L with interactive charts. No other bot shows you this.
  </p>
  <div style="display:flex;gap:12px;justify-content:center;flex-wrap:wrap">
    <a href="/signup" class="btn btn-primary" style="padding:14px 36px;font-size:15px">Start Trading in 2 Minutes</a>
    <a href="#pricing" class="btn btn-ghost" style="padding:14px 26px;font-size:15px">View Pricing ↓</a>
  </div>
  <div class="trust" style="margin-top:30px">
    <div class="titem">🔒 AES-256 Keys</div>
    <div class="titem">⚡ Helius Sender</div>
    <div class="titem">🎯 Jito Tips</div>
    <div class="titem">🔔 CEX Sniper</div>
    <div class="titem">✅ Cancel Anytime</div>
  </div>
</div>

<!-- Unique Features -->
<div style="max-width:960px;margin:0 auto;padding:0 24px 48px">
  <div style="text-align:center;margin-bottom:32px">
    <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1.5px;color:var(--t3);margin-bottom:10px">Features No Other Bot Has</div>
    <h2 style="font-size:27px;font-weight:700;letter-spacing:-.5px">4 Tools You Won't Find Anywhere Else</h2>
  </div>
  <div class="lp-feat-grid" style="grid-template-columns:repeat(2,1fr)">
    <div class="lp-feat" style="border-color:rgba(37,99,235,.3)">
      <div class="lp-feat-icon" style="background:rgba(37,99,235,.12)">🔬</div>
      <h3>Signal Explorer</h3>
      <p>See exactly WHY every token was accepted or rejected. Radar charts show AI score breakdowns. Filter pipeline shows each check with actual values vs thresholds.</p>
    </div>
    <div class="lp-feat" style="border-color:rgba(168,85,247,.3)">
      <div class="lp-feat-icon" style="background:rgba(168,85,247,.12)">🐋</div>
      <h3>Whale Copy Dashboard</h3>
      <p>Live feed of whale wallet buys with real-time P&amp;L tracking. See which whales are profitable, what they're buying, and whether you copied their trades.</p>
    </div>
    <div class="lp-feat" style="border-color:rgba(20,199,132,.3)">
      <div class="lp-feat-icon" style="background:rgba(20,199,132,.12)">📊</div>
      <h3>Position Analytics</h3>
      <p>Mini price charts per position with TP/SL overlay lines. Risk dashboard shows drawdown progress, cooldown timers, and consecutive loss tracking in real time.</p>
    </div>
    <div class="lp-feat" style="border-color:rgba(245,158,11,.3)">
      <div class="lp-feat-icon" style="background:rgba(245,158,11,.12)">📈</div>
      <h3>P&amp;L Curves</h3>
      <p>Interactive Chart.js visualizations of your cumulative P&amp;L and drawdown over time. Stats cards show win rate, best/worst trades, profit factor, and max drawdown.</p>
    </div>
  </div>

  <!-- All Features Grid -->
  <div style="text-align:center;margin:48px 0 32px">
    <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1.5px;color:var(--t3);margin-bottom:10px">Everything Included</div>
    <h2 style="font-size:27px;font-weight:700;letter-spacing:-.5px">Built for Serious Traders</h2>
  </div>
  <div class="lp-feat-grid">
    <div class="lp-feat">
      <div class="lp-feat-icon" style="background:rgba(245,158,11,.12)">🔔</div>
      <h3>CEX Listing Sniper</h3>
      <p>Monitors Binance, Coinbase, OKX, Kraken, Bybit, KuCoin &amp; Gate.io simultaneously. Auto-buys before the public pump.</p>
    </div>
    <div class="lp-feat">
      <div class="lp-feat-icon" style="background:rgba(20,199,132,.12)">⚡</div>
      <h3>Helius Sender + Jito Tips</h3>
      <p>Transactions route through Jito validator network with priority tips. Skip the mempool, beat the bots.</p>
    </div>
    <div class="lp-feat">
      <div class="lp-feat-icon" style="background:rgba(37,99,235,.12)">🛡️</div>
      <h3>Multi-Layer Rug Detection</h3>
      <p>Checks mint authority, freeze authority, LP lock, holder concentration, and dev wallet history before every buy.</p>
    </div>
    <div class="lp-feat">
      <div class="lp-feat-icon" style="background:rgba(5,150,105,.12)">🎯</div>
      <h3>Multi-Stage Take Profits</h3>
      <p>TP1 secures initial profits, TP2 rides the moonshot, trailing stop maximizes gains with partial sells.</p>
    </div>
    <div class="lp-feat">
      <div class="lp-feat-icon" style="background:rgba(239,68,68,.12)">🔍</div>
      <h3>Bundle &amp; Snipe Detection</h3>
      <p>Identifies coordinated launch bundles. Avoids tokens where bots already dominate supply.</p>
    </div>
    <div class="lp-feat">
      <div class="lp-feat-icon" style="background:rgba(168,85,247,.12)">🤖</div>
      <h3>AI Score + Volume Velocity</h3>
      <p>AI scoring with volume acceleration detection. Catches momentum 3-5 minutes before trending lists.</p>
    </div>
  </div>

  <!-- Competitor Comparison -->
  <div style="text-align:center;margin:48px 0 24px">
    <h2 style="font-size:27px;font-weight:700;letter-spacing:-.5px;margin-bottom:8px">How We Compare</h2>
    <p style="font-size:13.5px;color:var(--t2)">Features that set SolTrader apart from every competitor</p>
  </div>
  <div class="panel" style="padding:0;overflow:hidden;margin-bottom:48px">
    <table class="cmp-tbl">
      <thead>
        <tr>
          <th style="width:30%">Feature</th>
          <th style="text-align:center"><span style="color:var(--grn);font-weight:700">SolTrader</span></th>
          <th style="text-align:center">BullX</th>
          <th style="text-align:center">Photon</th>
          <th style="text-align:center">GMGN</th>
        </tr>
      </thead>
      <tbody>
        <tr><td class="col-h">Signal Explorer (WHY rejected)</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="no">—</td></tr>
        <tr><td class="col-h">Whale Copy Dashboard</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="no">partial</td></tr>
        <tr><td class="col-h">Position Analytics + Charts</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="no">—</td></tr>
        <tr><td class="col-h">P&amp;L Curves + Drawdown</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="no">basic</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="no">basic</td></tr>
        <tr><td class="col-h">CEX Listing Sniper</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="no">—</td></tr>
        <tr><td class="col-h">AI Token Scoring</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="yes">✓</td></tr>
        <tr><td class="col-h">Jito MEV Priority</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="no">—</td></tr>
        <tr><td class="col-h">Non-Custodial</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="yes">✓</td></tr>
        <tr><td class="col-h">No credit card to start</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="yes">✓</td></tr>
      </tbody>
    </table>
  </div>

  <!-- Pricing -->
  <div id="pricing" style="text-align:center;margin-bottom:34px">
    <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1.5px;color:var(--t3);margin-bottom:10px">Pricing</div>
    <h2 style="font-size:27px;font-weight:700;letter-spacing:-.5px;margin-bottom:8px">Performance-Based Pricing</h2>
    <p style="font-size:13.5px;color:var(--t2)">We only earn when you profit. 7-day free trial on all plans — no credit card required.</p>
  </div>

  <div class="plans3" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">
    <!-- Profit Only -->
    <div class="plan3">
      <div class="plan3-name" style="color:var(--t2)">Profit Only</div>
      <div class="plan3-price" style="font-size:30px">$0<sub>/mo</sub></div>
      <div class="plan3-perf">25% performance fee on profits only</div>
      <ul>
        <li>No monthly fee</li>
        <li>Up to 0.05 SOL per trade</li>
        <li>CEX Listing Sniper</li>
        <li>Anti-rug protection</li>
        <li>Whale tracker</li>
        <li>Live dashboard &amp; P&amp;L</li>
        <li>Start immediately</li>
      </ul>
      <a href="/signup?plan=free" class="btn btn-ghost btn-full" style="padding:11px;font-size:13px">Start for Free</a>
    </div>

    <!-- Basic -->
    <div class="plan3">
      <div class="plan3-name">Basic</div>
      <div class="plan3-price">$49<sub>/mo</sub></div>
      <div class="plan3-perf">+ 15% performance fee on profits</div>
      <ul>
        <li>Up to 0.1 SOL per trade</li>
        <li>Safe &amp; Balanced presets</li>
        <li>CEX Listing Sniper</li>
        <li>Anti-rug protection</li>
        <li>Whale tracker</li>
        <li>Live dashboard &amp; P&amp;L</li>
        <li>7-day free trial</li>
      </ul>
      <a href="/signup" class="btn btn-ghost btn-full" style="padding:11px;font-size:13px">Start Free Trial</a>
    </div>

    <!-- Pro -->
    <div class="plan3 hot">
      <div class="plan3-tag grn">Most Popular</div>
      <div class="plan3-name" style="color:var(--grn)">Pro</div>
      <div class="plan3-price">$99<sub>/mo</sub></div>
      <div class="plan3-perf">+ 10% performance fee on profits</div>
      <ul>
        <li>Up to 1.0 SOL per trade</li>
        <li>All presets incl. Degen mode</li>
        <li>CEX Listing Sniper (priority)</li>
        <li>Helius Sender fast execution</li>
        <li>Jito tip priority routing</li>
        <li>Volume velocity scoring</li>
        <li>Session drawdown limits</li>
        <li>7-day free trial</li>
      </ul>
      <a href="/signup" class="btn btn-primary btn-full" style="padding:11px;font-size:13px">Start Free Trial</a>
    </div>

    <!-- Elite -->
    <div class="plan3 elite">
      <div class="plan3-tag purple">Maximum Edge</div>
      <div class="plan3-name" style="color:#a855f7">Elite</div>
      <div class="plan3-price">$199<sub>/mo</sub></div>
      <div class="plan3-perf">+ 8% performance fee on profits</div>
      <ul>
        <li class="purple-check">Up to 5.0 SOL per trade</li>
        <li class="purple-check">Full admin panel access</li>
        <li class="purple-check">AI settings auto-optimization</li>
        <li class="purple-check">Bundle &amp; snipe detection</li>
        <li class="purple-check">Dev wallet blacklist</li>
        <li class="purple-check">Correlated position limits</li>
        <li class="purple-check">Lowest performance fee (8%)</li>
        <li class="purple-check">7-day free trial</li>
      </ul>
      <a href="/signup" class="btn btn-full" style="padding:11px;font-size:13px;background:#a855f7;color:#fff;border-radius:8px;font-weight:700;text-align:center;display:block">Start Free Trial</a>
    </div>
  </div>

  <!-- Comparison table -->
  <div class="panel" style="padding:0;overflow:hidden;margin-bottom:48px">
    <table class="cmp-tbl">
      <thead>
        <tr>
          <th style="width:32%">Feature</th>
          <th style="text-align:center">Profit Only<br><span style="color:var(--t2);font-size:12px">$0/mo</span></th>
          <th style="text-align:center">Basic<br><span style="color:var(--t1);font-size:12px">$49/mo</span></th>
          <th style="text-align:center">Pro<br><span style="color:var(--grn);font-size:12px">$99/mo</span></th>
          <th style="text-align:center">Elite<br><span style="color:#a855f7;font-size:12px">$199/mo</span></th>
        </tr>
      </thead>
      <tbody>
        <tr><td class="col-h">Max SOL per trade</td><td style="text-align:center">0.05</td><td style="text-align:center">0.1</td><td style="text-align:center">1.0</td><td style="text-align:center">5.0</td></tr>
        <tr><td class="col-h">CEX Listing Sniper</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="yes">✓</td></tr>
        <tr><td class="col-h">Whale Tracker</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="yes">✓</td></tr>
        <tr><td class="col-h">Anti-Rug + LP Lock Check</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="yes">✓</td></tr>
        <tr><td class="col-h">Helius Sender (Jito routing)</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="yes">✓</td></tr>
        <tr><td class="col-h">Bundle &amp; Snipe Detection</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="yes">✓</td></tr>
        <tr><td class="col-h">Volume Velocity Scoring</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="yes">✓</td><td style="text-align:center" class="yes">✓</td></tr>
        <tr><td class="col-h">AI Settings Optimization</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="yes">✓</td></tr>
        <tr><td class="col-h">Dev Wallet Blacklist</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="no">—</td><td style="text-align:center" class="yes">✓</td></tr>
        <tr><td class="col-h">Performance fee</td><td style="text-align:center;color:#f59e0b;font-weight:600">25%</td><td style="text-align:center">15%</td><td style="text-align:center">10%</td><td style="text-align:center">8%</td></tr>
      </tbody>
    </table>
  </div>

  <!-- Trust strip -->
  <div style="text-align:center;padding:20px 0 10px">
    <div class="trust" style="justify-content:center;flex-wrap:wrap;gap:14px">
      <div class="titem">🔒 AES-256 Encrypted Private Keys</div>
      <div class="titem">🌐 Non-Custodial — Your Keys, Your Funds</div>
      <div class="titem">⚡ Jupiter V6 + Helius Sender</div>
      <div class="titem">🛡️ Runs 24/7 on Railway Cloud</div>
      <div class="titem">✅ Cancel Anytime</div>
    </div>
  </div>
</div>

<div style="border-top:1px solid var(--bdr);padding:22px 24px;text-align:center">
  <div style="font-size:11.5px;color:var(--t3)">SolTrader &nbsp;·&nbsp; Automated Solana Trading &nbsp;·&nbsp; Non-Custodial &nbsp;·&nbsp; Powered by Helius &amp; Jupiter</div>
</div>
"""

# ── Auth Pages ─────────────────────────────────────────────────────────────────
def auth_page(title, action, error=""):
    other_route = "login"  if action=="signup" else "signup"
    other_label = "Sign In" if action=="signup" else "Create Account"
    btn_label   = "Create Account" if action=="signup" else "Sign In"
    subtitle    = "Start your 7-day free trial" if action=="signup" else "Welcome back"
    err_html    = f'<div class="alert alert-error">{error}</div>' if error else ""
    trust_html  = """<div class="trust" style="margin-top:18px">
      <div class="titem">🔒 AES-256 Encrypted</div>
      <div class="titem">🛡️ Non-Custodial</div>
      <div class="titem">✅ 7-Day Free Trial</div>
    </div>""" if action=="signup" else ""
    return _CSS + f"""
<nav class="nav">
  <a href="/" class="logo"><div class="logo-mark">S</div>SolTrader</a>
  <div class="nav-r"><a href="/">Home</a></div>
</nav>
<div class="center-page">
  <div class="card" style="width:100%;max-width:400px">
    <div style="text-align:center;margin-bottom:22px">
      <div style="font-size:20px;font-weight:700;margin-bottom:4px">{title}</div>
      <div style="font-size:13px;color:var(--t2)">{subtitle}</div>
    </div>
    {err_html}
    <form method="POST">
      <div class="fgroup">
        <label class="flabel">Email Address</label>
        <input class="finput" type="email" name="email" placeholder="you@example.com" required autocomplete="email">
      </div>
      <div class="fgroup">
        <label class="flabel">Password</label>
        <input class="finput" type="password" name="password" placeholder="Minimum 8 characters" required autocomplete="{'new-password' if action=='signup' else 'current-password'}">
      </div>
      <button type="submit" class="btn btn-primary btn-full" style="padding:11px;font-size:14px">{btn_label}</button>
    </form>
    <div class="divider"></div>
    <div class="txt-sm"><a href="/{other_route}">{other_label}</a> &nbsp;·&nbsp; <a href="/">Home</a></div>
    {trust_html}
  </div>
</div>"""

# ── Setup Page ─────────────────────────────────────────────────────────────────
SETUP_HTML = _CSS + """
<nav class="nav">
  <a href="/" class="logo"><div class="logo-mark">S</div>SolTrader</a>
  <div class="nav-r"><a href="/logout">Sign Out</a></div>
</nav>
<div style="max-width:560px;margin:0 auto;padding:40px 20px">
  <div class="card">
    <div style="margin-bottom:20px">
      <div class="page-title">Configure Your Bot</div>
      <div style="font-size:13px;color:var(--t2);margin-top:5px">Your private key is encrypted with AES-256 and never transmitted or shared.</div>
    </div>
    <div class="alert alert-error">{{ERROR}}</div>
    <div class="alert alert-info">
      Non-custodial: you retain full ownership. The bot signs transactions directly from your Solana wallet.
    </div>
    <form method="POST" style="margin-top:16px">
      <div class="fgroup">
        <label class="flabel">Solana Private Key (base58)</label>
        <input class="finput" type="password" name="private_key" placeholder="Paste your base58 private key…" required autocomplete="off">
      </div>
      <div class="fgroup">
        <label class="flabel">Trading Strategy</label>
        <select class="finput" name="preset">
          <option value="steady">Steady Profit — Conservative (1.5x TP1, 3x TP2, −25% stop)</option>
          <option value="max">Max Profit — Aggressive (2x TP1, 10x TP2, trailing stop)</option>
        </select>
      </div>
      <div class="fgroup">
        <label class="flabel">Stop Condition</label>
        <select class="finput" name="run_mode" onchange="toggleStop(this.value)">
          <option value="indefinite">Run indefinitely (manual stop only)</option>
          <option value="duration">Stop after a set duration</option>
          <option value="profit">Stop at profit target</option>
        </select>
      </div>
      <div id="f-dur" style="display:none" class="fgroup">
        <label class="flabel">Duration (minutes)</label>
        <input class="finput" type="number" name="run_duration_min" placeholder="e.g. 60" min="1">
      </div>
      <div id="f-pft" style="display:none" class="fgroup">
        <label class="flabel">Profit Target (SOL)</label>
        <input class="finput" type="number" name="profit_target_sol" placeholder="e.g. 0.5" step="0.01" min="0.01">
      </div>
      <button type="submit" class="btn btn-primary btn-full" style="padding:11px;font-size:14px;margin-top:6px">Save & Open Dashboard →</button>
    </form>
  </div>
</div>
<script>
function toggleStop(v){
  document.getElementById('f-dur').style.display = v==='duration' ? 'block' : 'none';
  document.getElementById('f-pft').style.display = v==='profit'   ? 'block' : 'none';
}
</script>
"""

# ── Dashboard Page ─────────────────────────────────────────────────────────────
DASHBOARD_HTML = _CSS + """
<style>
.tab-bar{display:flex;gap:0;padding:0 24px;background:rgba(12,24,41,.95);border-bottom:1px solid var(--b1);overflow-x:auto;-webkit-overflow-scrolling:touch;backdrop-filter:blur(12px)}
.tab-btn{padding:13px 20px;font-size:12px;font-weight:600;color:var(--t3);border:none;background:none;cursor:pointer;white-space:nowrap;border-bottom:2px solid transparent;transition:.2s;letter-spacing:.3px}
.tab-btn:hover{color:var(--t2)}
.tab-btn.active{color:var(--blue2);border-bottom-color:var(--blue2)}
.tab-pane{display:none;padding:20px 24px}
.tab-pane.active{display:block}
.glass{background:rgba(16,31,50,.6);backdrop-filter:blur(12px);border:1px solid rgba(255,255,255,.06);border-radius:12px;padding:16px}
.scanner-layout{display:flex;gap:14px}
.scanner-sidebar{width:250px;flex-shrink:0;display:flex;flex-direction:column;gap:12px}
.scanner-main{flex:1;min-width:0}
@media(max-width:860px){.scanner-layout{flex-direction:column}.scanner-sidebar{width:100%}.tab-bar{gap:0;padding:0 8px}}
.dex-row{cursor:pointer;transition:.12s}.dex-row:hover td{background:rgba(255,255,255,.03)}.dex-row.selected td{background:rgba(37,99,235,.06)}
.tok-icon{width:30px;height:30px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;margin-right:8px;flex-shrink:0}
.tok-name{font-size:12px;font-weight:600;color:var(--t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:110px}
.tok-sym{font-size:10px;color:var(--t3)}
.chg-pos{color:var(--grn);font-weight:600;font-size:11px}.chg-neg{color:var(--red2);font-weight:600;font-size:11px}.chg-0{color:var(--t3);font-size:11px}
.price-val{font-family:monospace;font-size:11px;color:var(--t2)}.num-val{font-family:monospace;font-size:11px;color:var(--t3)}
.score-mini{width:44px;height:5px;background:var(--b1);border-radius:3px;overflow:hidden}.score-fill{height:100%;border-radius:3px}
.sort-pill{padding:4px 10px;font-size:10px;border-radius:20px;border:1px solid var(--b1);background:none;color:var(--t3);cursor:pointer;font-weight:600}.sort-pill.active{background:var(--blue);border-color:var(--blue);color:#fff}
.buy-btn-mini{padding:3px 10px;font-size:10px;font-weight:700;border:1px solid var(--grn);background:rgba(20,199,132,.1);color:var(--grn);border-radius:6px;cursor:pointer;white-space:nowrap}
.buy-btn-mini:hover{background:var(--grn);color:#000}
.preset-card{background:var(--surf);border:1px solid var(--b1);border-radius:10px;padding:12px 14px;cursor:pointer;transition:.15s}
.preset-card:hover{border-color:var(--blue)}.preset-card.active{border-color:var(--blue);box-shadow:0 0 0 1px var(--blue)}
.preset-name{font-size:12px;font-weight:700;margin-bottom:2px}.preset-desc{font-size:10px;color:var(--t3)}
.launch-config{display:flex;flex-direction:column;gap:12px}
.launch-select{margin-top:-2px}
.custom-panel{display:none;border-top:1px solid rgba(255,255,255,.06);padding-top:12px}
.custom-panel.show{display:block}
.launch-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.launch-grid .fgroup{margin:0}
.launch-note{font-size:10px;line-height:1.5;color:var(--t3)}
.settings-pane-grid{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(320px,.85fr);gap:16px;align-items:start}
.settings-stack{display:flex;flex-direction:column;gap:14px}
.settings-card{background:rgba(16,31,50,.6);backdrop-filter:blur(12px);border:1px solid rgba(255,255,255,.06);border-radius:12px;padding:16px}
.settings-section-title{font-size:12px;font-weight:800;color:var(--t1);letter-spacing:.06em;text-transform:uppercase;margin-bottom:12px}
.setting-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(116px,160px) 72px;gap:10px;align-items:center;padding:10px 0;border-bottom:1px solid rgba(255,255,255,.06)}
.setting-row:last-child{border-bottom:none}
.setting-label{font-size:13px;color:var(--t2);font-weight:700}
.setting-desc{font-size:10px;color:var(--t3);margin-top:2px;line-height:1.4}
.setting-input{background:var(--surf);border:1px solid var(--b1);color:var(--t1);padding:8px 10px;border-radius:8px;font-size:12px;width:100%;font-family:monospace}
.setting-unit{font-size:11px;color:var(--t3);text-align:right}
.setting-toggle-row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid rgba(255,255,255,.06);gap:14px}
.setting-toggle-row:last-child{border-bottom:none}
.toggle-wrap{display:flex;align-items:center;gap:10px}
.settings-echo{display:flex;gap:6px;flex-wrap:wrap}
.checkpoint-path{display:flex;flex-direction:column;gap:10px}
.checkpoint-card{border:1px solid rgba(59,130,246,.18);background:linear-gradient(180deg,rgba(8,16,26,.96),rgba(10,18,29,.82));border-radius:12px;padding:12px 14px}
.checkpoint-step{display:flex;align-items:flex-start;gap:10px}
.checkpoint-index{width:22px;height:22px;border-radius:999px;background:rgba(59,130,246,.14);border:1px solid rgba(59,130,246,.28);color:var(--blue2);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:800;flex-shrink:0}
.checkpoint-meta{font-size:10px;color:var(--t3);margin-top:4px;line-height:1.45}
.operator-map{display:flex;flex-direction:column;gap:14px}
.operator-lane{border:1px solid rgba(255,255,255,.06);background:linear-gradient(180deg,rgba(8,16,26,.92),rgba(11,21,34,.82));border-radius:14px;padding:14px}
.operator-lane-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:10px;flex-wrap:wrap}
.operator-lane-title{font-size:12px;font-weight:800;color:var(--t1);text-transform:uppercase;letter-spacing:.08em}
.operator-lane-note{font-size:11px;color:var(--t3);line-height:1.45;max-width:520px}
.operator-chip-row{display:flex;flex-wrap:wrap;gap:8px}
.operator-chip{padding:7px 10px;border-radius:999px;border:1px solid rgba(59,130,246,.24);background:rgba(59,130,246,.08);font-size:11px;color:var(--t2);font-weight:700}
.operator-chip.feed{border-color:rgba(20,199,132,.22);background:rgba(20,199,132,.08);color:var(--grn)}
.operator-chip.guard{border-color:rgba(245,158,11,.24);background:rgba(245,158,11,.08);color:var(--gold2)}
.operator-chip.exit{border-color:rgba(220,38,38,.24);background:rgba(220,38,38,.08);color:#ff8b8b}
.operator-stage-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.operator-stage{border:1px solid rgba(59,130,246,.12);background:rgba(14,23,36,.82);border-radius:12px;padding:12px}
.operator-stage-num{font-size:10px;font-weight:800;color:var(--blue2);letter-spacing:.08em;text-transform:uppercase;margin-bottom:6px}
.operator-stage-title{font-size:13px;font-weight:700;color:var(--t1);margin-bottom:5px}
.operator-stage-value{font-size:11px;color:var(--t2);line-height:1.5}
.operator-stage-meta{font-size:10px;color:var(--t3);line-height:1.45;margin-top:6px}
.operator-arrow-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:8px 0 10px}
.operator-arrow{font-size:12px;color:var(--blue2);font-weight:800}
.operator-rule-list{display:flex;flex-direction:column;gap:7px}
.operator-rule{display:flex;justify-content:space-between;gap:10px;padding:8px 10px;border:1px solid rgba(255,255,255,.06);border-radius:10px;background:rgba(7,13,23,.4);font-size:11px}
.operator-rule-label{font-weight:700;color:var(--t2)}
.operator-rule-value{color:var(--t3);text-align:right}
.settings-save-row{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.save-pill{padding:6px 10px;border-radius:999px;border:1px solid rgba(20,199,132,.24);background:rgba(20,199,132,.08);font-size:11px;color:var(--grn);font-weight:700}
.save-pill.pending{border-color:rgba(245,158,11,.24);background:rgba(245,158,11,.08);color:var(--gold2)}
@media(max-width:1080px){.settings-pane-grid{grid-template-columns:1fr}}
@media(max-width:860px){.launch-grid{grid-template-columns:1fr}}
.ai-panel{background:linear-gradient(135deg,rgba(20,199,132,.14),rgba(59,130,246,.08));border:1px solid rgba(20,199,132,.24);border-radius:14px;padding:18px;margin-bottom:18px}
.ai-panel h3{margin:0 0 6px;font-size:16px;color:var(--t1)}
.ai-panel p{margin:0;color:var(--t2);font-size:12px;line-height:1.5}
.ai-stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin:14px 0}
.ai-stat{background:rgba(7,13,23,.45);border:1px solid var(--b1);border-radius:10px;padding:12px}
.ai-stat-value{font-size:20px;font-weight:800;color:var(--t1)}
.ai-stat-label{font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.08em;margin-top:4px}
.sig-table-wrap{max-height:640px;overflow:auto}
.sig-table{width:100%;border-collapse:collapse;font-size:11px;min-width:1520px}
.sig-table th{position:sticky;top:0;background:#0c1522;color:var(--t3);font-size:10px;text-transform:uppercase;letter-spacing:.08em;padding:10px 8px;border-bottom:1px solid var(--b1);z-index:1}
.sig-table td{padding:10px 8px;border-bottom:1px solid rgba(26,46,69,.5);vertical-align:top}
.sig-table tbody tr{cursor:pointer;transition:.12s}
.sig-table tbody tr:hover{background:rgba(255,255,255,.025)}
.sig-table tbody tr.active{background:rgba(37,99,235,.08)}
.sig-cell-token{min-width:180px}
.sig-token-name{font-weight:700;color:var(--t1);font-size:12px}
.sig-token-meta{font-size:10px;color:var(--t3);font-family:monospace}
.sig-checks{display:flex;gap:4px;flex-wrap:wrap;min-width:180px}
.sig-check{padding:3px 6px;border-radius:999px;font-size:9px;font-weight:700;border:1px solid transparent;white-space:nowrap}
.sig-check.pass{color:var(--grn);border-color:rgba(20,199,132,.35);background:rgba(20,199,132,.08)}
.sig-check.fail{color:var(--red2);border-color:rgba(220,38,38,.35);background:rgba(220,38,38,.08)}
.sig-reason{max-width:300px;color:var(--t2);line-height:1.35}
.sig-mini-muted{font-size:10px;color:var(--t3)}
.sig-badge{padding:2px 8px;border-radius:12px;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.3px}
.sig-pass{background:rgba(20,199,132,.15);color:var(--grn)}.sig-fail{background:rgba(220,38,38,.15);color:var(--red2)}
.score-bar{height:6px;border-radius:3px;background:var(--b1);overflow:hidden;width:60px;flex-shrink:0}
.score-bar-fill{height:100%;border-radius:3px}
.filter-step{display:flex;align-items:center;gap:8px;padding:5px 0;font-size:12px;color:var(--t2)}
.filter-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.filter-dot.pass{background:var(--grn)}.filter-dot.fail{background:var(--red2)}
.pos-card{background:var(--card);border:1px solid var(--b1);border-radius:12px;padding:16px;margin-bottom:12px;transition:.2s}
.pos-card:hover{border-color:var(--blue)}
.risk-bar{height:8px;border-radius:4px;background:var(--b1);overflow:hidden;margin-top:4px}
.risk-fill{height:100%;border-radius:4px;transition:width .3s}
.whale-entry{display:flex;align-items:center;gap:12px;padding:10px 14px;border-bottom:1px solid var(--b1);animation:wslide .3s ease}
@keyframes wslide{from{opacity:0;transform:translateX(-16px)}to{opacity:1;transform:translateX(0)}}
.whale-card{background:var(--card);border:1px solid var(--b1);border-radius:12px;padding:16px;min-width:200px}
.settings-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}
.tree-wallet-row{display:flex;align-items:center;gap:8px;padding:6px 10px;border-bottom:1px solid var(--b1);font-size:11px;cursor:pointer;transition:.1s}
.tree-wallet-row:hover{background:rgba(255,255,255,.03)}
.tree-wallet-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.tree-wallet-addr{flex:1;font-family:monospace;color:var(--t2);font-size:10px;overflow:hidden;text-overflow:ellipsis}
.tree-wallet-sol{font-family:monospace;color:var(--t1);font-weight:600;font-size:10px}
.fp-item{display:flex;align-items:center;gap:6px;padding:3px 0;font-size:11px}
.fp-pass{color:var(--grn);font-weight:700;font-size:10px}.fp-fail{color:var(--red2);font-weight:700;font-size:10px}
.fp-name{color:var(--t2);font-weight:500}.fp-reason{color:var(--t3);font-size:10px;flex:1;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.confirm-modal{position:fixed;inset:0;background:rgba(2,6,23,.72);display:none;align-items:center;justify-content:center;z-index:260;padding:20px}
.confirm-modal.show{display:flex}
.confirm-card{width:min(420px,100%);background:linear-gradient(180deg,#0c1522,#08101a);border:1px solid rgba(59,130,246,.28);border-radius:16px;padding:20px;box-shadow:0 24px 80px rgba(0,0,0,.45)}
.confirm-title{font-size:18px;font-weight:800;color:var(--t1);margin-bottom:8px}
.confirm-body{font-size:13px;line-height:1.55;color:var(--t2);margin-bottom:16px}
.confirm-meta{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}
</style>

<nav class="nav">
  <a href="/" class="logo"><div class="logo-mark">S</div>SolTrader</a>
  <div class="nav-r">
    <div class="status"><div id="sdot" class="sdot sdot-off"></div><span id="stxt" class="stxt">Loading…</span></div>
    <a href="/logout">Sign Out</a>
  </div>
</nav>

<div class="ticker-strip" style="background:var(--surf);border-bottom:1px solid var(--b1);overflow:hidden;height:32px;display:flex;align-items:center">
  <div id="ticker-inner" style="display:flex;gap:28px;white-space:nowrap;animation:tickscroll 40s linear infinite;font-size:12px;color:var(--t2)">Loading market data…</div>
</div>
<style>.tick-item{display:inline-flex;gap:6px;align-items:center}.tick-name{font-weight:700;color:var(--t1);font-size:11px}@keyframes tickscroll{0%{transform:translateX(0)}100%{transform:translateX(-50%)}}</style>

<div class="wrap" style="max-width:1480px">
  <!-- Stats Row -->
  <div class="stats" style="grid-template-columns:repeat(7,1fr);margin-top:16px">
    <div class="stat"><div class="slabel">Balance</div><div class="sval"><span id="balance">—</span> <span style="font-size:12px;color:var(--t3)">SOL</span></div></div>
    <div class="stat"><div class="slabel">Positions</div><div class="sval" id="pos-count">0</div></div>
    <div class="stat"><div class="slabel">Wins</div><div class="sval c-grn" id="wins">0</div></div>
    <div class="stat"><div class="slabel">Losses</div><div class="sval c-red" id="losses">0</div></div>
    <div class="stat"><div class="slabel">Win Rate</div><div class="sval c-blue" id="win-rate">—</div></div>
    <div class="stat"><div class="slabel">Session P&L</div><div class="sval" id="pnl">—</div></div>
    <div class="stat"><div class="slabel">Streak</div><div class="sval" id="streak">—</div></div>
  </div>

  <!-- Tab Bar -->
  <div class="tab-bar">
    <button class="tab-btn active" onclick="switchTab('scanner',this)">Scanner</button>
    <button class="tab-btn" onclick="switchTab('settings',this)">Settings</button>
    <button class="tab-btn" onclick="switchTab('signals',this)">Signals</button>
    <button class="tab-btn" onclick="switchTab('whales',this)">Whales</button>
    <button class="tab-btn" onclick="switchTab('positions',this)">Positions</button>
    <button class="tab-btn" onclick="switchTab('pnl',this)">P&L</button>
  </div>

  <!-- ═══════════════════════ SCANNER TAB ═══════════════════════ -->
  <div id="tab-scanner" class="tab-pane active">
    <div class="scanner-layout">
      <div class="scanner-sidebar">
        <div class="glass">
          <div style="display:flex;gap:8px;margin-bottom:10px">
            <button id="toggle-btn" class="btn btn-success btn-full" onclick="toggleBot()">▶ Start Bot</button>
            <button class="btn btn-ghost" onclick="cashout()" style="padding:9px 12px" title="Sell all">💰</button>
          </div>
          <div class="launch-config">
            <div class="sec-label">Launch Bot</div>
            <div id="launch-summary" class="settings-echo">
              <span class="badge bg-muted">Mode loading…</span>
            </div>
            <div class="launch-note">
              Open the Settings tab to edit every checkpoint in the coin path. Saved values stay visible there even when the bot is off.
            </div>
            <button class="btn btn-ghost btn-full" type="button" onclick="openSettingsTab()">Open Settings</button>
          </div>
        </div>
        <div class="glass">
          <div class="sec-label">Open Positions</div>
          <div id="pos-tbl" style="max-height:200px;overflow-y:auto"><div style="font-size:12px;color:var(--t3)">No open positions</div></div>
        </div>
        <div class="glass">
          <div class="sec-label">Filter Pipeline</div>
          <div id="filter-pipe" style="max-height:160px;overflow-y:auto"><div style="font-size:11px;color:var(--t3)">Scanning…</div></div>
        </div>
        <div class="glass">
          <div class="sec-label" style="display:flex;justify-content:space-between;align-items:center">
            <span>CEX Sniper</span>
            <span id="listing-count-badge" style="font-size:9px;color:var(--gold2);text-transform:none;letter-spacing:0">monitoring</span>
          </div>
          <div id="listing-feed" style="max-height:120px;overflow-y:auto;font-size:11px;color:var(--t3)">Monitoring exchanges…</div>
          <div style="margin-top:6px;font-size:10px;color:var(--t3)">Catches: <span id="listing-stat" style="color:var(--gold2);font-weight:700">0</span></div>
        </div>
      </div>
      <div class="scanner-main">
        <div class="glass" style="padding:0;overflow:hidden">
          <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--b1)">
            <div style="display:flex;align-items:center;gap:10px">
              <span style="font-weight:700;font-size:13px">Live Scanner</span>
              <span id="token-count" style="font-size:10px;color:var(--t3)">0 tokens</span>
            </div>
            <div style="display:flex;gap:6px;align-items:center">
              <input id="scan-search" type="text" placeholder="Search…" oninput="renderTokenRows()" style="background:var(--surf);border:1px solid var(--b1);color:var(--t1);padding:5px 10px;border-radius:6px;font-size:11px;width:130px">
              <button class="sort-pill active" onclick="setSortCol('score',this)">Score</button>
              <button class="sort-pill" onclick="setSortCol('vol',this)">Vol</button>
              <button class="sort-pill" onclick="setSortCol('chg',this)">Chg</button>
              <button class="sort-pill" onclick="setSortCol('age',this)">Age</button>
              <button style="background:none;border:none;cursor:pointer;font-size:14px" onclick="openDexScreener()" title="Open DexScreener">↗</button>
            </div>
          </div>
          <div style="max-height:550px;overflow-y:auto">
            <table class="tbl" style="font-size:11px">
              <thead><tr><th>#</th><th>Token</th><th>Price</th><th>1h</th><th>Vol</th><th>MCap</th><th>Liq</th><th>Age</th><th>Score</th><th></th></tr></thead>
            </table>
            <div id="token-rows"></div>
          </div>
        </div>
        <!-- Wallet Tree Panel -->
        <div id="tree-panel" style="display:none;margin-top:14px" class="glass">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
            <div>
              <div id="tree-tok-name" style="font-weight:700;font-size:14px"></div>
              <div id="tree-tok-mint" style="font-size:10px;color:var(--t3);font-family:monospace"></div>
            </div>
            <div style="display:flex;gap:10px;align-items:center">
              <span id="tree-buy-count" class="badge bg-grn">0</span>
              <span id="tree-sell-count" class="badge bg-red">0</span>
              <a id="tree-dex-link" href="#" target="_blank" style="font-size:11px;color:var(--blue2)">DexScreener ↗</a>
              <button onclick="closeTree()" style="background:none;border:none;color:var(--t3);cursor:pointer;font-size:16px">&times;</button>
            </div>
          </div>
          <div style="position:relative">
            <canvas id="tree-canvas" style="width:100%;border-radius:8px;background:#070d17"></canvas>
            <div id="tree-loading" style="display:none;position:absolute;inset:0;align-items:center;justify-content:center;color:var(--t3);font-size:12px">Loading wallet tree…</div>
          </div>
          <div id="tree-list" style="max-height:180px;overflow-y:auto;margin-top:8px"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- ═══════════════════════ SETTINGS TAB ═══════════════════════ -->
  <div id="tab-settings" class="tab-pane">
    <div class="settings-pane-grid">
      <div class="settings-stack">
        <div class="settings-card">
          <div class="settings-save-row">
            <div>
              <div class="settings-section-title" style="margin-bottom:6px">Saved Settings</div>
              <div style="font-size:12px;color:var(--t2)">Edit the exact checkpoint values the bot uses, then save them before you start.</div>
            </div>
            <div id="settings-save-state" class="save-pill">Loaded from saved state</div>
          </div>
          <div style="height:12px"></div>
          <div class="setting-row">
            <div>
              <div class="setting-label">Mode</div>
              <div class="setting-desc">Choose a base profile, then fine-tune the checkpoints below.</div>
            </div>
            <select class="setting-input" id="s-preset" onchange="selectPreset(this.value)">
              <option value="safe">Safe</option>
              <option value="balanced">Balanced (Default)</option>
              <option value="aggressive">Aggressive</option>
              <option value="degen">Degen</option>
              <option value="custom">Custom</option>
            </select>
            <div class="setting-unit">preset</div>
          </div>
        </div>

        <div class="settings-card">
          <div class="settings-section-title">Trade Plan</div>
          <div class="setting-row">
            <div><div class="setting-label">Max Buy Size</div><div class="setting-desc">SOL committed when a coin clears every checkpoint.</div></div>
            <input class="setting-input" id="s-max-buy" type="number" step="0.01">
            <div class="setting-unit">SOL</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">TP1 Multiplier</div><div class="setting-desc">First partial sell trigger.</div></div>
            <input class="setting-input" id="s-tp1" type="number" step="0.01">
            <div class="setting-unit">x</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">TP2 Multiplier</div><div class="setting-desc">Second target for the remaining size.</div></div>
            <input class="setting-input" id="s-tp2" type="number" step="0.01">
            <div class="setting-unit">x</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Stop Loss</div><div class="setting-desc">Hard floor as an entry ratio. `0.70` means stop near -30%.</div></div>
            <input class="setting-input" id="s-sl" type="number" step="0.01">
            <div class="setting-unit">ratio</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Trailing Loss</div><div class="setting-desc">Peak retrace used after the trail is armed.</div></div>
            <input class="setting-input" id="s-trail" type="number" step="0.01">
            <div class="setting-unit">ratio</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Time Stop</div><div class="setting-desc">Exit if the position stalls too long.</div></div>
            <input class="setting-input" id="s-tstop" type="number" step="1">
            <div class="setting-unit">min</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Priority Fee</div><div class="setting-desc">Lamports added to compete for landing.</div></div>
            <input class="setting-input" id="s-prio" type="number" step="1000">
            <div class="setting-unit">lamports</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Drawdown Limit</div><div class="setting-desc">Session loss cap before the bot halts itself.</div></div>
            <input class="setting-input" id="s-dd" type="number" step="0.01">
            <div class="setting-unit">SOL</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Max Correlated Positions</div><div class="setting-desc">Maximum overlapping positions before new buys are skipped.</div></div>
            <input class="setting-input" id="s-maxpos" type="number" step="1">
            <div class="setting-unit">count</div>
          </div>
        </div>

        <div class="settings-card">
          <div class="settings-section-title">Filter Path</div>
          <div class="setting-row">
            <div><div class="setting-label">Min Liquidity</div><div class="setting-desc">Set to `0` to disable the liquidity checkpoint entirely.</div></div>
            <input class="setting-input" id="s-liq" type="number" step="100">
            <div class="setting-unit">USD</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Min Market Cap</div><div class="setting-desc">Lowest market cap allowed into the path.</div></div>
            <input class="setting-input" id="s-minmc" type="number" step="100">
            <div class="setting-unit">USD</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Max Market Cap</div><div class="setting-desc">Upper market cap ceiling for new entries.</div></div>
            <input class="setting-input" id="s-maxmc" type="number" step="1000">
            <div class="setting-unit">USD</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Max Token Age</div><div class="setting-desc">Older coins fail the entry path.</div></div>
            <input class="setting-input" id="s-age" type="number" step="1">
            <div class="setting-unit">min</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Min Volume</div><div class="setting-desc">24h volume floor for the volume checkpoint.</div></div>
            <input class="setting-input" id="s-minvol" type="number" step="100">
            <div class="setting-unit">USD</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Min AI Score</div><div class="setting-desc">Score floor after the AI score breakdown is computed.</div></div>
            <input class="setting-input" id="s-minscore" type="number" step="1">
            <div class="setting-unit">/100</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Hot Change Cap</div><div class="setting-desc">Maximum 1h move before a coin is treated as too extended.</div></div>
            <input class="setting-input" id="s-hotchg" type="number" step="1">
            <div class="setting-unit">%</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Green Lights Required</div><div class="setting-desc">Minimum checklist confirmations to reach `SIGNAL`.</div></div>
            <input class="setting-input" id="s-lights" type="number" step="1">
            <div class="setting-unit">count</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Holder Growth</div><div class="setting-desc">Required holder acceleration for the quality checklist.</div></div>
            <input class="setting-input" id="s-holders" type="number" step="1">
            <div class="setting-unit">%</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Narrative Score</div><div class="setting-desc">Narrative timing floor before the buy path stays alive.</div></div>
            <input class="setting-input" id="s-narr" type="number" step="1">
            <div class="setting-unit">pts</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Volume Spike Multiple</div><div class="setting-desc">Minimum expansion multiple used in the three-signal checklist.</div></div>
            <input class="setting-input" id="s-volspike" type="number" step="0.1">
            <div class="setting-unit">x</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Late Entry Limit</div><div class="setting-desc">Reject coins already too extended unless narrative strength overrides it.</div></div>
            <input class="setting-input" id="s-latemult" type="number" step="0.1">
            <div class="setting-unit">x</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Nuclear Narrative Score</div><div class="setting-desc">Narrative score needed to override late-entry rejection.</div></div>
            <input class="setting-input" id="s-nuclear" type="number" step="1">
            <div class="setting-unit">pts</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Off-Peak Change Floor</div><div class="setting-desc">Saved with the profile for off-peak logic, even if the current runtime keeps entries open all day.</div></div>
            <input class="setting-input" id="s-offpeak" type="number" step="1">
            <div class="setting-unit">%</div>
          </div>
        </div>

        <div class="settings-card">
          <div class="settings-section-title">Safety Toggles</div>
          <div class="setting-toggle-row">
            <div>
              <div class="setting-label">Authority / Rug Check</div>
              <div class="setting-desc">Blocks older tokens when mint or freeze authority still looks unsafe.</div>
            </div>
            <label class="toggle-wrap"><input id="s-antirug" type="checkbox"> <span style="color:var(--t2);font-size:12px">Enabled</span></label>
          </div>
          <div class="setting-toggle-row">
            <div>
              <div class="setting-label">Holder Concentration Check</div>
              <div class="setting-desc">Blocks concentrated ownership once the token is old enough for holder analysis.</div>
            </div>
            <label class="toggle-wrap"><input id="s-checkholders" type="checkbox"> <span style="color:var(--t2);font-size:12px">Enabled</span></label>
          </div>
          <div class="setting-toggle-row">
            <div>
              <div class="setting-label">Risk Per Trade</div>
              <div class="setting-desc">Wallet percentage cap the buy logic can use as a secondary position-size guard.</div>
            </div>
            <div style="display:flex;align-items:center;gap:10px">
              <input class="setting-input" id="s-risk" type="number" step="0.1" style="width:120px">
              <span class="setting-unit">%</span>
            </div>
          </div>
        </div>

        <div class="settings-card">
          <div class="settings-save-row">
            <button class="btn btn-primary" type="button" onclick="saveSettings()">Save Settings</button>
            <button class="btn btn-ghost" type="button" onclick="refresh()">Reload Saved Settings</button>
          </div>
        </div>
      </div>

      <div class="settings-stack">
        <div class="settings-card">
          <div class="settings-section-title">Operator Map</div>
          <div style="font-size:12px;color:var(--t2);margin-bottom:12px">One shared engine handles every coin. Whales, snipes, scanners, and listings only change how a token enters the path, not how it gets approved.</div>
          <div id="settings-operator-map" class="operator-map">
            <div style="font-size:12px;color:var(--t3)">Building operator map…</div>
          </div>
        </div>
        <div class="settings-card">
          <div class="settings-section-title">Checkpoint Map</div>
          <div style="font-size:12px;color:var(--t2);margin-bottom:12px">This is the live pass order. A coin has to survive these checkpoints before it reaches quote and buy.</div>
          <div id="settings-checkpoint-path" class="checkpoint-path"></div>
        </div>
        <div class="settings-card">
          <div class="settings-section-title">Saved Snapshot</div>
          <div id="settings-snapshot" class="settings-echo">
            <span class="badge bg-muted">Waiting for saved settings…</span>
          </div>
          <div id="settings-snapshot-note" style="font-size:11px;color:var(--t3);margin-top:10px;line-height:1.5">Save while the bot is off and the exact persisted values stay visible here.</div>
        </div>
      </div>
    </div>
  </div>

  <!-- ═══════════════════════ SIGNALS TAB ═══════════════════════ -->
  <div id="tab-signals" class="tab-pane">
    <div class="stats" style="grid-template-columns:repeat(3,1fr);margin-bottom:16px">
      <div class="stat"><div class="slabel">Evaluated</div><div class="sval" id="sig-total">0</div></div>
      <div class="stat"><div class="slabel">Pass Rate</div><div class="sval c-grn" id="sig-pass-rate">0%</div></div>
      <div class="stat"><div class="slabel">Top Rejection</div><div class="sval" id="sig-top-reject" style="font-size:13px;color:var(--t2)">—</div></div>
    </div>
    <div style="display:flex;gap:8px;margin-bottom:12px;align-items:center;flex-wrap:wrap">
      <button class="sort-pill active" onclick="sigFilter='all';renderSignals();this.classList.add('active');this.nextElementSibling.classList.remove('active');this.nextElementSibling.nextElementSibling.classList.remove('active')">All</button>
      <button class="sort-pill" onclick="sigFilter='pass';renderSignals();this.classList.add('active');this.previousElementSibling.classList.remove('active');this.nextElementSibling.classList.remove('active')">Passed</button>
      <button class="sort-pill" onclick="sigFilter='fail';renderSignals();this.classList.add('active');this.previousElementSibling.classList.remove('active');this.previousElementSibling.previousElementSibling.classList.remove('active')">Rejected</button>
      <input id="sig-search" type="text" placeholder="Search token, mint, reason, tag…" oninput="renderSignals()" style="margin-left:auto;background:var(--surf);border:1px solid var(--b1);color:var(--t1);padding:7px 12px;border-radius:8px;font-size:11px;min-width:260px">
    </div>
    <div style="display:flex;gap:16px;align-items:flex-start">
      <div style="flex:1;min-width:0">
        <div class="glass" style="padding:0;overflow:hidden">
          <div style="display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid var(--b1)">
            <div>
              <div style="font-weight:700;font-size:13px;color:var(--t1)">All Evaluated Coins</div>
              <div style="font-size:11px;color:var(--t3)">Every evaluated token is logged here with the three-signal checklist, timing, and reject reason.</div>
            </div>
            <div id="sig-visible-count" style="font-size:11px;color:var(--t3)">0 rows</div>
          </div>
          <div id="sig-list" class="sig-table-wrap"></div>
        </div>
      </div>
      <div style="width:420px;flex-shrink:0;position:sticky;top:78px">
        <div class="glass" id="sig-detail">
          <div style="text-align:center;color:var(--t3);font-size:12px;padding:40px 0">Click any evaluated coin row to inspect the full checklist and score breakdown.</div>
        </div>
        <div class="glass" id="pattern-lab" style="margin-top:14px">
          <div style="text-align:center;color:var(--t3);font-size:12px;padding:24px 0">Pattern lab loading…</div>
        </div>
        <div class="glass" id="ops-radar" style="margin-top:14px">
          <div style="text-align:center;color:var(--t3);font-size:12px;padding:24px 0">Ops radar loading…</div>
        </div>
      </div>
    </div>
  </div>

  <!-- ═══════════════════════ WHALES TAB ═══════════════════════ -->
  <div id="tab-whales" class="tab-pane">
    <div style="display:flex;gap:12px;margin-bottom:16px;overflow-x:auto;padding-bottom:4px" id="whale-cards"></div>
    <div class="glass" style="padding:0">
      <div style="padding:12px 16px;border-bottom:1px solid var(--b1);font-weight:700;font-size:13px">Live Whale Feed</div>
      <div id="whale-feed" style="max-height:500px;overflow-y:auto">
        <div style="padding:20px;text-align:center;color:var(--t3);font-size:12px">Waiting for whale activity…</div>
      </div>
    </div>
  </div>

  <!-- ═══════════════════════ POSITIONS TAB ═══════════════════════ -->
  <div id="tab-positions" class="tab-pane">
    <div style="display:flex;gap:16px">
      <div style="flex:1;min-width:0" id="pos-cards">
        <div style="text-align:center;color:var(--t3);font-size:13px;padding:40px">No open positions</div>
      </div>
      <div style="width:280px;flex-shrink:0">
        <div class="glass">
          <div class="sec-label">Risk Dashboard</div>
          <div id="risk-panel">
            <div style="margin-bottom:12px">
              <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--t2)"><span>Drawdown</span><span id="risk-dd-val">0 / 0.5 SOL</span></div>
              <div class="risk-bar"><div class="risk-fill" id="risk-dd-bar" style="width:0;background:var(--red2)"></div></div>
            </div>
            <div style="margin-bottom:12px">
              <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--t2)"><span>Positions</span><span id="risk-pos-val">0 / 5</span></div>
              <div class="risk-bar"><div class="risk-fill" id="risk-pos-bar" style="width:0;background:var(--blue2)"></div></div>
            </div>
            <div style="font-size:11px;color:var(--t2);margin-bottom:6px">Consecutive Losses: <b id="risk-losses">0</b></div>
            <div style="font-size:11px;color:var(--t2);margin-bottom:6px">Cooldown: <span id="risk-cooldown" style="color:var(--t3)">None</span></div>
            <div style="font-size:11px;color:var(--t2)">Peak: <b id="risk-peak">—</b> SOL &nbsp; Now: <b id="risk-now">—</b> SOL</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- ═══════════════════════ P&L TAB ═══════════════════════ -->
  <div id="tab-pnl" class="tab-pane">
    <div style="display:flex;gap:8px;margin-bottom:14px">
      <button class="sort-pill active" onclick="loadPnl('1');document.querySelectorAll('.pnl-range').forEach(b=>b.classList.remove('active'));this.classList.add('active')" data-range="1">24h</button>
      <button class="sort-pill pnl-range" onclick="loadPnl('7');document.querySelectorAll('.pnl-range').forEach(b=>b.classList.remove('active'));this.classList.add('active')" data-range="7">7d</button>
      <button class="sort-pill pnl-range" onclick="loadPnl('all');document.querySelectorAll('.pnl-range').forEach(b=>b.classList.remove('active'));this.classList.add('active')" data-range="all">All</button>
    </div>
    <div class="stats" style="grid-template-columns:repeat(auto-fit,minmax(130px,1fr));margin-bottom:16px">
      <div class="stat"><div class="slabel">Total P&L</div><div class="sval" id="pnl-total">—</div></div>
      <div class="stat"><div class="slabel">Win Rate</div><div class="sval" id="pnl-winrate">—</div></div>
      <div class="stat"><div class="slabel">Best Trade</div><div class="sval c-grn" id="pnl-best">—</div></div>
      <div class="stat"><div class="slabel">Worst Trade</div><div class="sval c-red" id="pnl-worst">—</div></div>
      <div class="stat"><div class="slabel">Max Drawdown</div><div class="sval c-red" id="pnl-dd">—</div></div>
      <div class="stat"><div class="slabel">Trades</div><div class="sval" id="pnl-trades">—</div></div>
    </div>
    <div class="glass" style="margin-bottom:14px">
      <div style="font-weight:600;font-size:13px;margin-bottom:8px">Cumulative P&L</div>
      <canvas id="pnl-chart" height="220"></canvas>
    </div>
    <div class="glass">
      <div style="font-weight:600;font-size:13px;margin-bottom:8px">Drawdown</div>
      <canvas id="dd-chart" height="120"></canvas>
    </div>
  </div>

</div>

<!-- Toast -->
<div id="toast" style="position:fixed;bottom:230px;left:50%;transform:translateX(-50%);background:var(--card);border:1px solid var(--grn);color:var(--grn);padding:10px 24px;border-radius:8px;font-size:13px;font-weight:600;z-index:200;opacity:0;transition:opacity .3s;pointer-events:none"></div>
<style>#toast.show{opacity:1}</style>
<div id="confirm-modal" class="confirm-modal" onclick="if(event.target===this)hideConfirmModal()">
  <div class="confirm-card">
    <div class="confirm-title" id="confirm-title">Settings Saved</div>
    <div class="confirm-body" id="confirm-body">Your settings were saved successfully.</div>
    <div class="confirm-meta" id="confirm-meta"></div>
    <button class="btn btn-primary btn-full" onclick="hideConfirmModal()">OK</button>
  </div>
</div>

<!-- Activity Bar -->
<div id="activity-bar" style="position:fixed;bottom:0;left:0;right:0;height:200px;background:rgba(7,16,30,.98);border-top:1px solid var(--b1);display:flex;flex-direction:column;z-index:90;backdrop-filter:blur(10px);transition:height .2s">
  <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 16px;border-bottom:1px solid var(--b1);flex-shrink:0">
    <div style="display:flex;align-items:center;gap:8px">
      <span style="font-size:11px;font-weight:600;color:var(--t2)">Activity Log</span>
      <span id="log-count" style="font-size:10px;color:var(--t3)"></span>
    </div>
    <div style="display:flex;align-items:center;gap:8px">
      <span style="width:6px;height:6px;border-radius:50%;background:#14c784;display:inline-block;animation:blink 2s infinite"></span>
      <button onclick="toggleLogBar()" id="log-toggle-btn" style="background:none;border:none;color:var(--t3);font-size:11px;cursor:pointer;padding:2px 6px">▼ collapse</button>
    </div>
  </div>
  <div id="log" style="flex:1;overflow-y:auto;display:flex;flex-direction:column-reverse;padding:4px 0"></div>
</div>

<style>
.lline{font-size:12px;padding:3px 16px;border-bottom:1px solid rgba(255,255,255,.03);color:#64748b;font-family:'SF Mono','Courier New',monospace;line-height:1.65}
.lline:hover{background:rgba(255,255,255,.02)}
.lbuy{color:#4ade80!important;font-weight:600}.lsell{color:#f87171!important;font-weight:600}
.lsig{color:#fbbf24!important;font-weight:600}.linfo{color:#60a5fa!important}.lscan{color:#818cf8}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
</style>

<script>
document.querySelector('.wrap').style.paddingBottom = '214px';

// ── State ─────────────────────────────────────────────────────────────────────
let running = false, allTokens = [], sortCol = 'score', feedSince = 0;
let selectedMint = null, logBarExpanded = true;
let treeCanvas = null, treeCtx = null;
let _charts = {};
let _activeTab = 'scanner';
let _sigData = [], _sigView = [], _sigSelected = null, _sigSelectedKey = null, sigFilter = 'all';
let _patternLab = { tokens: [], deployers: [], themes: [] };
let _opsMetrics = { stats: {}, top_whales: [], threat_map: [], liquidity_risks: [], route_mix: [], failure_reasons: [] };
let _tabPollers = {};
let _settingsDirty = false;
let aiSuggestion = null;

// ── Tab System ────────────────────────────────────────────────────────────────
function switchTab(tab, btn) {
  _activeTab = tab;
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  if (btn) btn.classList.add('active');
  // Start tab-specific polling
  Object.values(_tabPollers).forEach(id => clearInterval(id));
  _tabPollers = {};
  if (tab === 'signals') { pollSignals(); _tabPollers.sig = setInterval(pollSignals, 6000); }
  if (tab === 'whales') { pollWhales(); _tabPollers.whale = setInterval(pollWhales, 8000); }
  if (tab === 'positions') { pollPositions(); _tabPollers.pos = setInterval(pollPositions, 5000); }
  if (tab === 'pnl') { loadPnl('7'); }
}

// ── Activity bar ──────────────────────────────────────────────────────────────
function toggleLogBar() {
  const bar = document.getElementById('activity-bar');
  const btn = document.getElementById('log-toggle-btn');
  logBarExpanded = !logBarExpanded;
  bar.style.height = logBarExpanded ? '200px' : '34px';
  btn.textContent  = logBarExpanded ? '▼ collapse' : '▲ expand';
}

// ── Utilities ─────────────────────────────────────────────────────────────────
function fmtK(n) {
  if (!n) return '\u2014';
  if (n >= 1e9) return '$' + (n/1e9).toFixed(1) + 'B';
  if (n >= 1e6) return '$' + (n/1e6).toFixed(1) + 'M';
  if (n >= 1e3) return '$' + (n/1e3).toFixed(0) + 'K';
  return '$' + n.toFixed(0);
}
function fmtPrice(p) {
  if (!p) return '\u2014';
  if (p < 0.000001) return '$' + p.toExponential(2);
  if (p < 0.0001)   return '$' + p.toFixed(8);
  if (p < 0.01)     return '$' + p.toFixed(6);
  if (p < 1)        return '$' + p.toFixed(4);
  return '$' + p.toFixed(2);
}
function fmtAge(m) { return m < 60 ? m.toFixed(0) + 'm' : (m/60).toFixed(1) + 'h'; }
function chgClass(v) { return v > 0 ? 'chg-pos' : v < 0 ? 'chg-neg' : 'chg-0'; }
function chgStr(v) { return (v >= 0 ? '+' : '') + v.toFixed(1) + '%'; }
function scoreColor(s) { return s >= 70 ? '#14c784' : s >= 45 ? '#f5a623' : '#f23645'; }
function shortWallet(w) { return w ? (w.slice(0, 6) + '…' + w.slice(-4)) : '—'; }
function markSettingsDirty() {
  _settingsDirty = true;
  document._settingsFocused = true;
  setSettingsSaveState('Unsaved changes', true);
  renderSettingsVisuals(getSettingsFromForm());
}
function bindSettingsInputs() {
  getSettingsFocusIds().forEach(id => {
    const el = document.getElementById(id);
    if (!el || el._settingsBound) return;
    el._settingsBound = true;
    el.addEventListener('focus', () => { document._settingsFocused = true; });
    el.addEventListener('blur', () => {
      setTimeout(() => {
        const active = document.activeElement;
        const ids = getSettingsFocusIds();
        document._settingsFocused = !!(active && ids.includes(active.id));
      }, 0);
    });
    el.addEventListener('change', markSettingsDirty);
    if (el.tagName !== 'SELECT' && el.type !== 'checkbox') {
      el.addEventListener('input', markSettingsDirty);
    }
  });
}
function signalKey(s) { return `${s.mint || ''}|${s.logged_at || s.ts || ''}|${s.passed ? 1 : 0}|${s.reason || ''}`; }
function fmtDateTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? (ts || '—') : d.toLocaleString();
}
function getSignalChecklist(s) {
  const checklist = s.intel?.checklist;
  return Array.isArray(checklist) && checklist.length ? checklist : [];
}
function signalSearchText(s) {
  const tags = (s.intel?.narrative_tags || []).join(' ');
  const links = (s.intel?.social_links || []).join(' ');
  return [
    s.name, s.mint, s.reason, tags, links,
    s.intel?.deployer_wallet, s.intel?.deployer_score,
  ].join(' ').toLowerCase();
}
function renderChecklistMini(s) {
  const checklist = getSignalChecklist(s);
  if (!checklist.length) return '<span class="sig-mini-muted">Waiting for intel</span>';
  return checklist.slice(0, 3).map(c => `<span class="sig-check ${c.passed ? 'pass' : 'fail'}">${c.passed ? '✓' : '✕'} ${c.name}</span>`).join('');
}
function tokColor(name) {
  const h = [...(name||'?')].reduce((a,c) => a + c.charCodeAt(0), 0);
  return ['#14c784','#3b82f6','#f59e0b','#a855f7','#06b6d4','#f43f5e','#84cc16','#f97316'][h % 8];
}
function openDexScreener() { window.open('https://dexscreener.com/solana', '_blank'); }

// ── Sort & render token rows ──────────────────────────────────────────────────
function setSortCol(col, btn) {
  sortCol = col;
  document.querySelectorAll('.sort-pill').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderTokenRows();
}

function getSortedTokens() {
  const q = (document.getElementById('scan-search')?.value || '').toLowerCase();
  let tokens = allTokens.filter(t =>
    !q || (t.name||'').toLowerCase().includes(q) || (t.symbol||'').toLowerCase().includes(q)
  );
  return [...tokens].sort((a, b) => {
    if (sortCol === 'score') return (b.score||0) - (a.score||0);
    if (sortCol === 'vol')   return (b.vol||0)   - (a.vol||0);
    if (sortCol === 'chg')   return (b.change||0) - (a.change||0);
    if (sortCol === 'age')   return (a.age_min||0) - (b.age_min||0);
    return 0;
  }).slice(0, 80);
}

function renderTokenRows() {
  const tokens = getSortedTokens();
  document.getElementById('token-count').textContent = tokens.length + ' tokens';
  const container = document.getElementById('token-rows');
  if (!tokens.length) {
    container.innerHTML = '<div style="padding:28px;text-align:center;color:var(--t3);font-size:13px">No tokens yet \u2014 start the bot to begin scanning</div>';
    return;
  }
  container.innerHTML = tokens.map((t, i) => {
    const chg = t.change || 0;
    const sc  = t.score  || 0;
    const col = tokColor(t.name || '?');
    const sym = (t.symbol || t.name || '?').slice(0, 7);
    const isSel = t.mint === selectedMint;
    const nameSafe = (t.name||'').replace(/'/g, '');
    return `<table style="width:100%;border-collapse:collapse"><tbody>
      <tr class="dex-row${isSel?' selected':''}" onclick="openWalletTree('${t.mint}','${nameSafe}')">
        <td style="width:30px;color:var(--t3);font-size:10px;font-family:monospace">${i+1}</td>
        <td>
          <div style="display:flex;align-items:center">
            <div class="tok-icon" style="background:${col}1a;color:${col}">${sym.charAt(0)}</div>
            <div>
              <div class="tok-name">${t.name||sym}${sc>=80?' &#x1f525;':''}</div>
              <div class="tok-sym">${sym}${t.whale?' &#x1f40b;':''} · ${(t.green_lights||0)}/3 GL · N${t.narrative_score||0}</div>
            </div>
          </div>
        </td>
        <td><span class="price-val">${fmtPrice(t.price)}</span></td>
        <td><span class="${chgClass(chg)}">${chgStr(chg)}</span></td>
        <td><span class="num-val">${fmtK(t.vol)}</span></td>
        <td><span class="num-val">${fmtK(t.mc)}</span></td>
        <td><span class="num-val">${fmtK(t.liq)}</span></td>
        <td><span class="num-val">${fmtAge(t.age_min||0)}</span></td>
        <td>
          <div style="display:flex;align-items:center;gap:5px">
            <div class="score-mini"><div class="score-fill" style="width:${sc}%;background:${scoreColor(sc)}"></div></div>
            <span style="font-size:10px;color:${scoreColor(sc)};font-family:monospace;font-weight:700">${sc}</span>
          </div>
        </td>
        <td><button class="buy-btn-mini" onclick="event.stopPropagation();quickBuy('${t.mint}','${nameSafe}',this)">\u26a1 Buy</button></td>
      </tr>
    </tbody></table>`;
  }).join('');
}

// ── Quick buy ─────────────────────────────────────────────────────────────────
async function quickBuy(mint, name, btn) {
  const orig = btn.textContent;
  btn.textContent = '\u2026'; btn.disabled = true;
  const res = await fetch('/api/manual-buy', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({mint, name})
  }).then(r => r.json()).catch(() => ({ok:false, msg:'Failed'}));
  btn.textContent = res.ok ? '\u2705' : '\u274c';
  showToast(res.ok ? '\u26a1 Buy order sent!' : '\u26a0 ' + (res.msg||'Buy failed'), res.ok);
  setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 3000);
  if (res.ok) setTimeout(refresh, 2000);
}

// ── Wallet Tree ───────────────────────────────────────────────────────────────
async function openWalletTree(mint, name) {
  selectedMint = mint;
  renderTokenRows();
  const panel = document.getElementById('tree-panel');
  panel.style.display = '';
  document.getElementById('tree-tok-name').textContent = name || mint.slice(0,14)+'\u2026';
  document.getElementById('tree-tok-mint').textContent = mint;
  document.getElementById('tree-buy-count').textContent  = '\u2026';
  document.getElementById('tree-sell-count').textContent = '\u2026';
  const dexLink = document.getElementById('tree-dex-link');
  if (dexLink) dexLink.href = 'https://dexscreener.com/solana/' + mint;
  if (!treeCanvas) {
    treeCanvas = document.getElementById('tree-canvas');
    treeCtx    = treeCanvas.getContext('2d');
  }
  treeCanvas.width  = treeCanvas.parentElement.clientWidth;
  treeCanvas.height = 360;
  const loading = document.getElementById('tree-loading');
  loading.style.display = 'flex';
  drawTreeBg();
  try {
    const data = await fetch('/api/wallet-tree/' + mint).then(r => r.json());
    loading.style.display = 'none';
    const buys  = data.nodes.filter(n => n.type === 'buy').length;
    const sells = data.nodes.filter(n => n.type === 'sell').length;
    document.getElementById('tree-buy-count').textContent  = buys  + ' buys';
    document.getElementById('tree-sell-count').textContent = sells + ' sells';
    drawWalletTree(data.nodes, data.edges, name || mint.slice(0,8));
    renderTreeList(data.nodes);
  } catch(e) {
    loading.style.display = 'none';
    drawTreeMsg('Failed to load \u2014 token may be too new');
  }
  panel.scrollIntoView({behavior:'smooth', block:'nearest'});
}
function closeTree() { selectedMint = null; document.getElementById('tree-panel').style.display = 'none'; renderTokenRows(); }
function drawTreeBg() { if (!treeCanvas) return; treeCtx.fillStyle = '#070d17'; treeCtx.fillRect(0,0,treeCanvas.width,treeCanvas.height); }
function drawTreeMsg(msg) {
  const W = treeCanvas.width, H = treeCanvas.height;
  treeCtx.fillStyle = '#070d17'; treeCtx.fillRect(0,0,W,H);
  treeCtx.fillStyle = '#4b5563'; treeCtx.font = '13px monospace'; treeCtx.textAlign = 'center';
  treeCtx.fillText(msg, W/2, H/2);
}
function drawWalletTree(nodes, edges, tokenName) {
  const W = treeCanvas.width, H = treeCanvas.height, ctx = treeCtx;
  ctx.clearRect(0,0,W,H); ctx.fillStyle = '#070d17'; ctx.fillRect(0,0,W,H);
  if (!nodes || nodes.length <= 1) { drawTreeMsg('No swap transactions found yet'); return; }
  const positions = {};
  const buyGroup  = nodes.find(n => n.id === 'buys');
  const sellGroup = nodes.find(n => n.id === 'sells');
  const buyWals   = nodes.filter(n => n.type === 'buy');
  const sellWals  = nodes.filter(n => n.type === 'sell');
  positions['root'] = {x: W/2, y: 56};
  if (buyGroup && sellGroup) { positions['buys']={x:W*0.27,y:140}; positions['sells']={x:W*0.73,y:140}; }
  else if (buyGroup) positions['buys']={x:W/2,y:140};
  else if (sellGroup) positions['sells']={x:W/2,y:140};
  function spreadWallets(wals, gid, yBase) {
    const gp = positions[gid]; if (!gp || !wals.length) return;
    const cols = Math.min(wals.length, 5);
    const colW = Math.min(88, (W * 0.44) / Math.max(cols, 1));
    wals.forEach((n, i) => { positions[n.id] = {x: gp.x - (cols-1)*colW/2 + (i%cols)*colW, y: yBase + Math.floor(i/cols)*78}; });
  }
  spreadWallets(buyWals, 'buys', 230); spreadWallets(sellWals, 'sells', 230);
  ctx.strokeStyle = '#0f1a28'; ctx.lineWidth = 1;
  for (let x=0;x<W;x+=60){ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,H);ctx.stroke();}
  for (let y=0;y<H;y+=60){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(W,y);ctx.stroke();}
  edges.forEach(e => {
    const f=positions[e.from],t=positions[e.to]; if(!f||!t) return;
    const toN=nodes.find(n=>n.id===e.to); const isSell=toN&&(toN.type==='sell'||toN.id==='sells');
    const grd=ctx.createLinearGradient(f.x,f.y,t.x,t.y);
    grd.addColorStop(0,isSell?'rgba(242,54,69,.0)':'rgba(20,199,132,.0)');
    grd.addColorStop(1,isSell?'rgba(242,54,69,.4)':'rgba(20,199,132,.4)');
    ctx.strokeStyle=grd; ctx.lineWidth=1.5; ctx.beginPath(); ctx.moveTo(f.x,f.y);
    const cy=(f.y+t.y)/2; ctx.bezierCurveTo(f.x,cy,t.x,cy,t.x,t.y); ctx.stroke();
  });
  nodes.forEach(n => {
    const pos=positions[n.id]; if(!pos) return;
    const isRoot=n.id==='root',isGroup=n.type==='group_buy'||n.type==='group_sell';
    const isBuy=n.type==='buy'||n.id==='buys',isSell=n.type==='sell'||n.id==='sells';
    const r=isRoot?26:isGroup?18:15; const color=isRoot?'#f59e0b':isBuy?'#14c784':'#f23645';
    ctx.shadowBlur=isRoot?22:10; ctx.shadowColor=color;
    ctx.beginPath(); ctx.arc(pos.x,pos.y,r,0,Math.PI*2);
    ctx.fillStyle=color+'18'; ctx.fill(); ctx.strokeStyle=color; ctx.lineWidth=isRoot?2.5:1.8; ctx.stroke();
    ctx.shadowBlur=0; ctx.fillStyle=color; ctx.textAlign='center';
    ctx.font=isRoot?'bold 9px monospace':isGroup?'bold 8px monospace':'8px monospace';
    ctx.fillText((isRoot?tokenName:(n.label||'')).slice(0,9),pos.x,pos.y+3);
    if(n.sol>0){ctx.fillStyle='#94a3b8';ctx.font='7px monospace';ctx.fillText(n.sol.toFixed(3)+'\u25ce',pos.x,pos.y+r+9);}
  });
}
function renderTreeList(nodes) {
  const all = [...nodes.filter(n=>n.type==='buy').map(n=>({...n,side:'buy'})),...nodes.filter(n=>n.type==='sell').map(n=>({...n,side:'sell'}))];
  document.getElementById('tree-list').innerHTML = all.map(n => `
    <div class="tree-wallet-row is-${n.side}" onclick="window.open('https://solscan.io/account/${n.wallet||''}','_blank')" title="${n.wallet||''}">
      <div class="tree-wallet-dot" style="background:${n.side==='buy'?'#14c784':'#f23645'}"></div>
      <div class="tree-wallet-addr">${n.label||'?'}</div>
      <div class="tree-wallet-sol">${(n.sol||0).toFixed(4)}\u25ce</div>
    </div>`).join('');
}

// ── Market feed polling ───────────────────────────────────────────────────────
async function pollFeed() {
  try {
    const tokens = await fetch('/api/market-feed?since=' + feedSince).then(r => r.json());
    if (tokens && tokens.length) {
      const byMint = new Map(allTokens.map(t => [t.mint, t]));
      tokens.forEach(t => {
        const prev = byMint.get(t.mint) || {};
        byMint.set(t.mint, { ...prev, ...t, intel: { ...(prev.intel||{}), ...(t.intel||{}) } });
      });
      allTokens = [...byMint.values()].sort((a, b) => (b.ts||0) - (a.ts||0)).slice(0, 100);
      feedSince = Math.max(...tokens.map(t => t.ts||0), feedSince);
      renderTokenRows();
      updateTicker();
    }
  } catch(e) {}
}

function updateTicker() {
  const items = allTokens.slice(0, 20).map(t => {
    const chg = t.change || 0;
    const col = chg >= 0 ? '#14c784' : '#f23645';
    return `<span class="tick-item">
      <span class="tick-name">${t.symbol||t.name||'?'}</span>
      <span style="font-family:monospace;font-size:11px">${fmtPrice(t.price)}</span>
      <span style="color:${col};font-weight:700">${chgStr(chg)}</span>
    </span>`;
  });
  const html = [...items, ...items].join('');
  document.getElementById('ticker-inner').innerHTML = html || 'Loading market data\u2026';
}

// ── CEX Listing alerts ────────────────────────────────────────────────────────
const seenListings = new Set();
let listingCatchCount = 0;
async function pollListings() {
  try {
    const alerts = await fetch('/api/listing-alerts').then(r => r.json());
    if (!alerts || !alerts.length) return;
    const feed = document.getElementById('listing-feed');
    let added = false;
    alerts.forEach(a => {
      const key = a.exchange + a.symbol;
      if (seenListings.has(key)) return;
      seenListings.add(key); listingCatchCount++;
      const row = document.createElement('div');
      row.style.cssText = 'padding:7px 10px;border-bottom:1px solid rgba(245,158,11,.12);font-size:11px';
      row.innerHTML = `<div style="display:flex;justify-content:space-between">
        <span><b style="color:#f59e0b;font-size:10px">${a.exchange}</b> &nbsp;<b style="color:var(--t1)">${a.symbol}</b></span>
        <span style="color:var(--t3);font-size:10px">${new Date(a.ts*1000).toLocaleTimeString()}</span>
      </div>`;
      if (feed.children[0]?.textContent.includes('Monitoring')) feed.innerHTML = '';
      feed.insertBefore(row, feed.firstChild);
      added = true;
    });
    if (added) {
      document.getElementById('listing-stat').textContent = listingCatchCount;
      document.getElementById('listing-count-badge').textContent = listingCatchCount + ' caught';
    }
  } catch(e) {}
}

// ── Settings ──────────────────────────────────────────────────────────────────
const PRESET_SETTINGS = {{PRESET_SETTINGS}};
const SETTINGS_FIELD_IDS = [
  's-preset', 's-max-buy', 's-tp1', 's-tp2', 's-sl', 's-trail',
  's-age', 's-tstop', 's-liq', 's-minmc', 's-maxmc', 's-prio',
  's-dd', 's-maxpos', 's-minvol', 's-minscore', 's-risk', 's-holders',
  's-narr', 's-lights', 's-volspike', 's-latemult', 's-nuclear',
  's-offpeak', 's-hotchg', 's-antirug', 's-checkholders',
];
const SETTINGS_DEFAULTS = {
  max_buy_sol: 0.04,
  tp1_mult: 2.0,
  tp2_mult: 4.0,
  stop_loss: 0.70,
  trail_pct: 0.20,
  max_age_min: 240,
  time_stop_min: 30,
  min_liq: 0,
  min_mc: 5000,
  max_mc: 250000,
  priority_fee: 30000,
  drawdown_limit_sol: 0.5,
  max_correlated: 3,
  min_vol: 3000,
  min_score: 30,
  risk_per_trade_pct: 2.0,
  min_holder_growth_pct: 30,
  min_narrative_score: 16,
  min_green_lights: 1,
  min_volume_spike_mult: 6,
  late_entry_mult: 5.0,
  nuclear_narrative_score: 40,
  offpeak_min_change: 18,
  max_hot_change: 400,
  anti_rug: true,
  check_holders: true,
};
const SETTINGS_META = [
  ['Mode', s => String(s.preset || 'balanced')],
  ['TP1', s => Number(s.tp1_mult || 0).toFixed(2) + 'x'],
  ['TP2', s => Number(s.tp2_mult || 0).toFixed(2) + 'x'],
  ['SL', s => Number(s.stop_loss || 0).toFixed(2)],
  ['Trail', s => (Number(s.trail_pct || 0) * 100).toFixed(0) + '%'],
  ['MC', s => '$' + Number(s.min_mc || 0).toLocaleString() + ' - $' + Number(s.max_mc || 0).toLocaleString()],
  ['Age', s => Number(s.max_age_min || 0).toLocaleString() + 'm'],
  ['Score', s => '>=' + Number(s.min_score || 0)],
  ['Liquidity', s => Number(s.min_liq || 0) > 0 ? '>=$' + Number(s.min_liq || 0).toLocaleString() : 'off'],
  ['Green Lights', s => Number(s.min_green_lights || 0)],
  ['Holder Check', s => s.check_holders ? 'on' : 'off'],
];
function setSettingsSaveState(text, pending=false) {
  const el = document.getElementById('settings-save-state');
  if (!el) return;
  el.textContent = text;
  el.classList.toggle('pending', pending);
}
function readNum(id, fallback=0) {
  const el = document.getElementById(id);
  const v = el ? Number(el.value) : NaN;
  return Number.isFinite(v) ? v : fallback;
}
function setPresetChoice(name) {
  const presetInput = document.getElementById('s-preset');
  if (presetInput) presetInput.value = name;
}
function applySettingsToForm(settings, presetName) {
  const s = { ...SETTINGS_DEFAULTS, ...(settings || {}) };
  const setVal = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.value = value;
  };
  const setChecked = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.checked = !!value;
  };
  setPresetChoice(presetName || s.preset || 'balanced');
  setVal('s-max-buy', s.max_buy_sol ?? SETTINGS_DEFAULTS.max_buy_sol);
  setVal('s-tp1', s.tp1_mult ?? SETTINGS_DEFAULTS.tp1_mult);
  setVal('s-tp2', s.tp2_mult ?? SETTINGS_DEFAULTS.tp2_mult);
  setVal('s-sl', s.stop_loss ?? SETTINGS_DEFAULTS.stop_loss);
  setVal('s-trail', s.trail_pct ?? SETTINGS_DEFAULTS.trail_pct);
  setVal('s-age', s.max_age_min ?? SETTINGS_DEFAULTS.max_age_min);
  setVal('s-tstop', s.time_stop_min ?? SETTINGS_DEFAULTS.time_stop_min);
  setVal('s-liq', s.min_liq ?? SETTINGS_DEFAULTS.min_liq);
  setVal('s-minmc', s.min_mc ?? SETTINGS_DEFAULTS.min_mc);
  setVal('s-maxmc', s.max_mc ?? SETTINGS_DEFAULTS.max_mc);
  setVal('s-prio', s.priority_fee ?? SETTINGS_DEFAULTS.priority_fee);
  setVal('s-dd', s.drawdown_limit_sol ?? SETTINGS_DEFAULTS.drawdown_limit_sol);
  setVal('s-maxpos', s.max_correlated ?? SETTINGS_DEFAULTS.max_correlated);
  setVal('s-minvol', s.min_vol ?? SETTINGS_DEFAULTS.min_vol);
  setVal('s-minscore', s.min_score ?? SETTINGS_DEFAULTS.min_score);
  setVal('s-risk', s.risk_per_trade_pct ?? SETTINGS_DEFAULTS.risk_per_trade_pct);
  setVal('s-holders', s.min_holder_growth_pct ?? SETTINGS_DEFAULTS.min_holder_growth_pct);
  setVal('s-narr', s.min_narrative_score ?? SETTINGS_DEFAULTS.min_narrative_score);
  setVal('s-lights', s.min_green_lights ?? SETTINGS_DEFAULTS.min_green_lights);
  setVal('s-volspike', s.min_volume_spike_mult ?? SETTINGS_DEFAULTS.min_volume_spike_mult);
  setVal('s-latemult', s.late_entry_mult ?? SETTINGS_DEFAULTS.late_entry_mult);
  setVal('s-nuclear', s.nuclear_narrative_score ?? SETTINGS_DEFAULTS.nuclear_narrative_score);
  setVal('s-offpeak', s.offpeak_min_change ?? SETTINGS_DEFAULTS.offpeak_min_change);
  setVal('s-hotchg', s.max_hot_change ?? SETTINGS_DEFAULTS.max_hot_change);
  setChecked('s-antirug', s.anti_rug ?? SETTINGS_DEFAULTS.anti_rug);
  setChecked('s-checkholders', s.check_holders ?? SETTINGS_DEFAULTS.check_holders);
  renderSettingsVisuals({ ...s, preset: presetName || s.preset || 'balanced' });
}
function getSettingsFromForm() {
  return {
    preset: document.getElementById('s-preset')?.value || 'balanced',
    max_buy_sol: readNum('s-max-buy', SETTINGS_DEFAULTS.max_buy_sol),
    tp1_mult: readNum('s-tp1', SETTINGS_DEFAULTS.tp1_mult),
    tp2_mult: readNum('s-tp2', SETTINGS_DEFAULTS.tp2_mult),
    stop_loss: readNum('s-sl', SETTINGS_DEFAULTS.stop_loss),
    trail_pct: readNum('s-trail', SETTINGS_DEFAULTS.trail_pct),
    max_age_min: readNum('s-age', SETTINGS_DEFAULTS.max_age_min),
    time_stop_min: readNum('s-tstop', SETTINGS_DEFAULTS.time_stop_min),
    min_liq: readNum('s-liq', SETTINGS_DEFAULTS.min_liq),
    min_mc: readNum('s-minmc', SETTINGS_DEFAULTS.min_mc),
    max_mc: readNum('s-maxmc', SETTINGS_DEFAULTS.max_mc),
    priority_fee: readNum('s-prio', SETTINGS_DEFAULTS.priority_fee),
    drawdown_limit_sol: readNum('s-dd', SETTINGS_DEFAULTS.drawdown_limit_sol),
    max_correlated: readNum('s-maxpos', SETTINGS_DEFAULTS.max_correlated),
    min_vol: readNum('s-minvol', SETTINGS_DEFAULTS.min_vol),
    min_score: readNum('s-minscore', SETTINGS_DEFAULTS.min_score),
    risk_per_trade_pct: readNum('s-risk', SETTINGS_DEFAULTS.risk_per_trade_pct),
    min_holder_growth_pct: readNum('s-holders', SETTINGS_DEFAULTS.min_holder_growth_pct),
    min_narrative_score: readNum('s-narr', SETTINGS_DEFAULTS.min_narrative_score),
    min_green_lights: readNum('s-lights', SETTINGS_DEFAULTS.min_green_lights),
    min_volume_spike_mult: readNum('s-volspike', SETTINGS_DEFAULTS.min_volume_spike_mult),
    late_entry_mult: readNum('s-latemult', SETTINGS_DEFAULTS.late_entry_mult),
    nuclear_narrative_score: readNum('s-nuclear', SETTINGS_DEFAULTS.nuclear_narrative_score),
    offpeak_min_change: readNum('s-offpeak', SETTINGS_DEFAULTS.offpeak_min_change),
    max_hot_change: readNum('s-hotchg', SETTINGS_DEFAULTS.max_hot_change),
    anti_rug: !!document.getElementById('s-antirug')?.checked,
    check_holders: !!document.getElementById('s-checkholders')?.checked,
  };
}
function renderSettingsSnapshot(settings) {
  const el = document.getElementById('settings-snapshot');
  if (!el) return;
  el.innerHTML = SETTINGS_META.map(([label, getter]) => `<span class="badge bg-muted">${label}: ${getter(settings)}</span>`).join('');
  const note = document.getElementById('settings-snapshot-note');
  if (!note) return;
  note.textContent = Number(settings.min_liq || 0) > 0
    ? `Liquidity checkpoint is live at $${Number(settings.min_liq).toLocaleString()}. Save again any time you want the path to track different thresholds.`
    : 'Liquidity checkpoint is off. Keeping the value at 0 means liquidity will never reject a coin in the main path.';
}
function renderLaunchSummary(settings) {
  const el = document.getElementById('launch-summary');
  if (!el) return;
  el.innerHTML = [
    `<span class="badge bg-blue">${String(settings.preset || 'balanced')}</span>`,
    `<span class="badge bg-muted">TP1 ${Number(settings.tp1_mult || 0).toFixed(2)}x</span>`,
    `<span class="badge bg-muted">TP2 ${Number(settings.tp2_mult || 0).toFixed(2)}x</span>`,
    `<span class="badge bg-muted">Score >= ${Number(settings.min_score || 0)}</span>`,
    `<span class="badge bg-muted">GL ${Number(settings.min_green_lights || 0)}</span>`,
    `<span class="badge bg-muted">Liq ${Number(settings.min_liq || 0) > 0 ? '$' + Number(settings.min_liq || 0).toLocaleString() : 'off'}</span>`,
    `<span class="badge bg-muted">Holders ${settings.check_holders ? 'on' : 'off'}</span>`,
  ].join('');
}
function renderOperatorMap(settings) {
  const el = document.getElementById('settings-operator-map');
  if (!el) return;
  const s = { ...SETTINGS_DEFAULTS, ...(settings || {}) };
  const liqText = Number(s.min_liq || 0) > 0 ? `$${Number(s.min_liq).toLocaleString()}+` : 'off';
  const sourceFeeds = [
    ['Dex token-profiles', 'feed'],
    ['Dex new-pairs', 'feed'],
    ['Helius sniper', 'feed'],
    ['Whale tracker', 'feed'],
    ['Listing scanner', 'feed'],
  ];
  const entryStages = [
    {
      num: 'Stage 1',
      title: 'Normalize Signal',
      value: 'Every feed becomes one token candidate and enters the same evaluator.',
      meta: 'Whales and snipes do not bypass any later filter. They only surface the coin earlier.',
    },
    {
      num: 'Stage 2',
      title: 'Basic Filters',
      value: `MC $${Number(s.min_mc).toLocaleString()}-$${Number(s.max_mc).toLocaleString()} | Liq ${liqText} | Age <= ${Number(s.max_age_min).toLocaleString()}m`,
      meta: `Change must stay above 0% and under ${Number(s.max_hot_change).toLocaleString()}%. Vol must be >= $${Number(s.min_vol).toLocaleString()} and score >= ${Number(s.min_score).toLocaleString()}.`,
    },
    {
      num: 'Stage 3',
      title: 'Checklist Gates',
      value: `${Number(s.min_green_lights).toLocaleString()} green light(s) | holders >= ${Number(s.min_holder_growth_pct).toLocaleString()}% | narrative >= ${Number(s.min_narrative_score).toLocaleString()}`,
      meta: `Volume spike must be >= ${Number(s.min_volume_spike_mult).toLocaleString()}x. Late entry dies above ${Number(s.late_entry_mult).toLocaleString()}x unless narrative reaches ${Number(s.nuclear_narrative_score).toLocaleString()}.`,
    },
    {
      num: 'Stage 4',
      title: 'Safety Gates',
      value: `Authority ${s.anti_rug ? 'on' : 'off'} | holders ${s.check_holders ? 'on' : 'off'} | losing mint block 30m`,
      meta: 'The buy path can still die here on route failure, dev blacklist, drawdown, balance, or correlated-position limits.',
    },
    {
      num: 'Stage 5',
      title: 'Execution',
      value: `Quote -> simulate -> sign -> Helius Sender -> Jito tip -> confirm`,
      meta: `Priority fee ${Number(s.priority_fee).toLocaleString()} lamports. Max buy ${Number(s.max_buy_sol || 0).toFixed(2)} SOL with risk cap ${Number(s.risk_per_trade_pct || 0).toFixed(1)}%.`,
    },
  ];
  const exitRules = [
    ['TP1 partial', `${Number(s.tp1_mult || 0).toFixed(2)}x`],
    ['TP2 full', `${Number(s.tp2_mult || 0).toFixed(2)}x`],
    ['Stop loss', `${Number(s.stop_loss || 0).toFixed(2)} ratio`],
    ['Trailing stop', `${Math.round(Number(s.trail_pct || 0) * 100)}% retrace`],
    ['Time stop', `${Number(s.time_stop_min || 0).toLocaleString()} min`],
    ['Surge hold', '2.0x inside 10s -> hold until 14% drop from peak'],
    ['Listing exits', 'listing TP / SL / timeout if source was listing scanner'],
  ];
  const guardRules = [
    ['Session drawdown', `${Number(s.drawdown_limit_sol || 0).toFixed(2)} SOL`],
    ['Max correlated positions', `${Number(s.max_correlated || 0)} open names`],
    ['Recent losing mint', 'same mint blocked for 30 minutes after a red close'],
    ['Holder concentration', s.check_holders ? 'blocking' : 'disabled'],
  ];
  el.innerHTML = `
    <div class="operator-lane">
      <div class="operator-lane-head">
        <div>
          <div class="operator-lane-title">Signal Sources</div>
          <div class="operator-lane-note">These feeds are watched in parallel and all route into the same buy engine.</div>
        </div>
      </div>
      <div class="operator-chip-row">
        ${sourceFeeds.map(([label, cls]) => `<span class="operator-chip ${cls}">${label}</span>`).join('')}
      </div>
      <div class="operator-arrow-row">
        <span class="operator-arrow">all feeds -> broadcast signal -> evaluate_signal()</span>
      </div>
    </div>
    <div class="operator-lane">
      <div class="operator-lane-head">
        <div>
          <div class="operator-lane-title">Buy Path</div>
          <div class="operator-lane-note">A coin only reaches quote and buy after it survives every stage below in order.</div>
        </div>
        <div class="operator-chip-row">
          <span class="operator-chip guard">runtime gates stay live after filters</span>
        </div>
      </div>
      <div class="operator-stage-grid">
        ${entryStages.map(stage => `
          <div class="operator-stage">
            <div class="operator-stage-num">${stage.num}</div>
            <div class="operator-stage-title">${stage.title}</div>
            <div class="operator-stage-value">${stage.value}</div>
            <div class="operator-stage-meta">${stage.meta}</div>
          </div>
        `).join('')}
      </div>
    </div>
    <div class="operator-lane">
      <div class="operator-lane-head">
        <div>
          <div class="operator-lane-title">Live Guards</div>
          <div class="operator-lane-note">These rules are monitored even after a token passes the numeric checkpoint map.</div>
        </div>
      </div>
      <div class="operator-rule-list">
        ${guardRules.map(([label, value]) => `
          <div class="operator-rule">
            <span class="operator-rule-label">${label}</span>
            <span class="operator-rule-value">${value}</span>
          </div>
        `).join('')}
      </div>
    </div>
    <div class="operator-lane">
      <div class="operator-lane-head">
        <div>
          <div class="operator-lane-title">Sell + Monitor Path</div>
          <div class="operator-lane-note">Once bought, every open position is monitored on the same loop until one exit path fires.</div>
        </div>
        <div class="operator-chip-row">
          <span class="operator-chip exit">positions -> check_positions() -> sell()</span>
        </div>
      </div>
      <div class="operator-rule-list">
        ${exitRules.map(([label, value]) => `
          <div class="operator-rule">
            <span class="operator-rule-label">${label}</span>
            <span class="operator-rule-value">${value}</span>
          </div>
        `).join('')}
      </div>
    </div>
  `;
}
function renderSettingsVisuals(settings) {
  const s = { ...SETTINGS_DEFAULTS, ...(settings || {}) };
  renderLaunchSummary(s);
  renderSettingsSnapshot(s);
  renderOperatorMap(s);
  const el = document.getElementById('settings-checkpoint-path');
  if (!el) return;
  const liqRule = Number(s.min_liq || 0) > 0 ? `Liquidity must be >= $${Number(s.min_liq).toLocaleString()}` : 'Liquidity checkpoint disabled';
  const cards = [
    ['1', 'Market Cap', `$${Number(s.min_mc).toLocaleString()} to $${Number(s.max_mc).toLocaleString()}`, 'The token has to land inside the market-cap lane before any later check matters.'],
    ['2', 'Liquidity', liqRule, 'Set liquidity to 0 to leave this checkpoint open. Any non-zero value makes it a real gate again.'],
    ['3', 'Token Age', `Age must be <= ${Number(s.max_age_min).toLocaleString()} minutes`, 'Older coins die here before the signal reaches momentum or score.'],
    ['4', 'Price Change', `Change must be > 0% and <= ${Number(s.max_hot_change).toLocaleString()}%`, 'Negative change and overheated moves both fail on the same checkpoint.'],
    ['5', 'Volume', `24h volume must be >= $${Number(s.min_vol).toLocaleString()}`, 'This is the first participation proof after the raw price filters.'],
    ['6', 'AI Score', `Score must be >= ${Number(s.min_score).toLocaleString()}`, 'The score breakdown is computed first, then this floor decides if the coin survives.'],
    ['7', 'Three-Signal Checklist', `${Number(s.min_green_lights)} green lights | holders >= ${Number(s.min_holder_growth_pct)}% | narrative >= ${Number(s.min_narrative_score)} | volume spike >= ${Number(s.min_volume_spike_mult)}x`, 'These values feed the same checklist and quality gates shown in the Signals tab.'],
    ['8', 'Late Entry Guard', `Late entry <= ${Number(s.late_entry_mult)}x unless narrative >= ${Number(s.nuclear_narrative_score)}`, 'A coin that is already too extended only survives if narrative timing is strong enough to override the guard.'],
    ['9', 'Safety Checks', `Authority check ${s.anti_rug ? 'on' : 'off'} | holder concentration ${s.check_holders ? 'on' : 'off'}`, 'These can still kill the trade later, after the numeric path passes.'],
    ['10', 'Runtime Gates', `Drawdown ${Number(s.drawdown_limit_sol).toFixed(2)} SOL | max positions ${Number(s.max_correlated)} | trade risk ${Number(s.risk_per_trade_pct).toFixed(1)}%`, 'Even after the filter path passes, the bot can still skip on drawdown, position count, balance, or recent losing-mint rules.'],
  ];
  el.innerHTML = cards.map(([index, title, value, meta]) => `
    <div class="checkpoint-card">
      <div class="checkpoint-step">
        <div class="checkpoint-index">${index}</div>
        <div style="min-width:0">
          <div style="font-size:13px;font-weight:700;color:var(--t1)">${title}</div>
          <div style="font-size:12px;color:var(--t2);margin-top:4px">${value}</div>
          <div class="checkpoint-meta">${meta}</div>
        </div>
      </div>
    </div>
  `).join('');
}
function getSettingsFocusIds() {
  return SETTINGS_FIELD_IDS;
}
function selectPreset(name, applyDefaults = true) {
  setPresetChoice(name);
  const p = PRESET_SETTINGS[name];
  if (applyDefaults && name !== 'custom' && p) {
    applySettingsToForm({ ...SETTINGS_DEFAULTS, ...p, preset: name }, name);
  } else {
    renderSettingsVisuals(getSettingsFromForm());
  }
  markSettingsDirty();
}
async function saveSettings() {
  const payload = getSettingsFromForm();
  const res = await fetch('/api/settings', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  }).then(r => r.json()).catch(() => null);
  if (res && res.ok !== false) {
    _settingsDirty = false;
    document._settingsFocused = false;
    applySettingsToForm(res.settings || payload, res.preset || payload.preset);
    setSettingsSaveState('Saved to bot settings');
    showToast('\u2713 Settings saved', true);
    showConfirmModal(
      'Settings Saved',
      `Checkpoint path updated for ${String(res.preset || payload.preset)}.`,
      [
        `Preset: ${String(res.preset || payload.preset)}`,
        `Saved ${res.saved_fields?.length || 0} fields`,
        `${res.replayed_candidates ?? 0} recent coins rechecked`,
      ]
    );
  } else {
    setSettingsSaveState('Save failed', true);
    showToast('\u26a0 Save failed', false);
  }
  setTimeout(refresh, 800);
  return res;
}
function openSettingsTab() {
  const btn = [...document.querySelectorAll('.tab-btn')].find(el => (el.textContent || '').trim() === 'Settings');
  if (btn) switchTab('settings', btn);
}

async function loadAI() {
  const r = await fetch('/api/ai-recommendation').then(resp => resp.json()).catch(() => null);
  if (!r) return;
  aiSuggestion = r.suggestion || null;
  const reasonEl = document.getElementById('ai-reason');
  const logicEl = document.getElementById('ai-logic');
  if (reasonEl) reasonEl.textContent = aiSuggestion?.reason || 'No recommendation available yet.';
  if (logicEl) logicEl.textContent = r.logic || 'Uses your last 24h realized trades to map the market to a preset.';
  const stats = r.stats || {};
  const statsEl = document.getElementById('ai-stats');
  if (!statsEl) return;
  statsEl.innerHTML = `
    <div class="ai-stat"><div class="ai-stat-value">${stats.total_trades ?? '—'}</div><div class="ai-stat-label">Trades (24h)</div></div>
    <div class="ai-stat"><div class="ai-stat-value">${stats.win_rate != null ? stats.win_rate + '%' : '—'}</div><div class="ai-stat-label">Win Rate</div></div>
    <div class="ai-stat"><div class="ai-stat-value">${stats.avg_win_sol != null ? '+' + stats.avg_win_sol : '—'}</div><div class="ai-stat-label">Avg Win (SOL)</div></div>
    <div class="ai-stat"><div class="ai-stat-value">${stats.avg_loss_sol != null ? stats.avg_loss_sol : '—'}</div><div class="ai-stat-label">Avg Loss (SOL)</div></div>
  `;
}

async function applyAISuggestion() {
  if (!aiSuggestion || !aiSuggestion.preset || !PRESET_SETTINGS[aiSuggestion.preset]) {
    showToast('AI recommendation unavailable', false);
    return;
  }
  selectPreset(aiSuggestion.preset);
  await saveSettings();
}

// ── Bot state refresh ─────────────────────────────────────────────────────────
async function refresh() {
  const d = await fetch('/api/state').then(r => {
    if (r.status === 401 || r.status === 302) { window.location = '/login'; return null; }
    return r.json();
  }).catch(() => null);
  if (!d) { document.getElementById('stxt').textContent = 'Connection error \u2014 retrying\u2026'; return; }
  running = d.running;
  document.getElementById('balance').textContent   = d.balance.toFixed(4);
  document.getElementById('pos-count').textContent = d.positions.length;
  document.getElementById('wins').textContent      = d.stats.wins;
  document.getElementById('losses').textContent    = d.stats.losses;
  const wr = d.stats.win_rate;
  document.getElementById('win-rate').textContent = wr > 0 ? wr + '%' : '\u2014';
  const streak = d.stats.streak;
  const streakEl = document.getElementById('streak');
  if (streak > 0) { streakEl.textContent = '+' + streak; streakEl.className = 'sval c-grn'; }
  else if (streak < 0) { streakEl.textContent = streak; streakEl.className = 'sval c-red'; }
  else { streakEl.textContent = '\u2014'; streakEl.className = 'sval'; }
  const pnl = d.stats.total_pnl_sol;
  const pnlEl = document.getElementById('pnl');
  pnlEl.textContent = (pnl>=0?'+':'') + pnl.toFixed(4);
  pnlEl.className   = 'sval ' + (pnl>=0?'c-grn':'c-red');
  const dot = document.getElementById('sdot'), txt = document.getElementById('stxt'), btn = document.getElementById('toggle-btn');
  if (running) { dot.className='sdot sdot-on'; txt.textContent='Bot Running'; btn.textContent='\u23f8 Stop Bot'; btn.className='btn btn-danger btn-full'; }
  else          { dot.className='sdot sdot-off'; txt.textContent='Bot Stopped'; btn.textContent='\u25b6 Start Bot'; btn.className='btn btn-success btn-full'; }

  document.getElementById('pos-tbl').innerHTML = d.positions.length
    ? d.positions.map(p => {
        const cls = !p.ratio ? 'c-muted' : p.ratio>=1 ? 'c-grn' : 'c-red';
        return `<div style="display:flex;align-items:center;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--b1);font-size:12px">
          <span style="font-weight:700;cursor:pointer" onclick="openWalletTree('${p.address||''}','${p.name||''}')">${p.name}${p.tp1_hit?'<span class="badge bg-grn" style="margin-left:4px;font-size:9px">TP1</span>':''}</span>
          <span class="${cls}" style="font-weight:700;font-family:monospace">${p.pnl}</span>
        </div>`;
      }).join('')
    : '<div style="font-size:12px;color:var(--t3)">No open positions</div>';

  const logs = d.log || [];
  document.getElementById('log-count').textContent = logs.length + ' entries';
  document.getElementById('log').innerHTML = logs.map(l => {
    let c = '';
    if (l.includes('BUY')) c = 'lbuy';
    else if (l.includes('SELL')||l.includes('CASHOUT')) c = 'lsell';
    else if (l.includes('WHALE')||l.includes('COPY')) c = 'lsig';
    else if (l.includes('SNIPE')||l.includes('LISTING')) c = 'lsig';
    else if (l.includes('SCAN')||l.includes('CHECK')) c = 'lscan';
    else if (l.includes('Bot ')||l.includes('ERROR')||l.includes('WARN')) c = 'linfo';
    return `<div class="lline ${c}">${l}</div>`;
  }).join('');
  if (d.filter_log) {
    document.getElementById('filter-pipe').innerHTML = d.filter_log.map(f => `
      <div class="fp-item">
        <span class="${f.passed?'fp-pass':'fp-fail'}">${f.passed?'\u2713':'\u2717'}</span>
        <span class="fp-name">${f.name||'?'}</span>
        <span class="fp-reason">${f.reason||''}</span>
      </div>`).join('') || '<div style="font-size:11px;color:var(--t3)">Scanning\u2026</div>';
  }
  if (d.settings && Object.keys(d.settings).length) {
    const savedSettings = { ...d.settings, preset: d.preset || 'balanced' };
    if (!document._settingsFocused && !_settingsDirty) {
      applySettingsToForm(savedSettings, savedSettings.preset);
      _settingsDirty = false;
      setSettingsSaveState('Loaded from saved state');
    } else {
      renderLaunchSummary(savedSettings);
      renderSettingsSnapshot(savedSettings);
    }
  }
}

async function toggleBot() {
  if (!running && _settingsDirty) { await saveSettings(); }
  const res = await fetch(running ? '/api/stop' : '/api/start', {method:'POST'}).then(r=>r.json()).catch(()=>null);
  if (!res) { document.getElementById('stxt').textContent = '\u26a0\ufe0f Server error'; return; }
  if (!res.ok && res.msg) { document.getElementById('stxt').textContent = '\u26a0\ufe0f ' + res.msg; document.getElementById('stxt').style.color='#f23645'; return; }
  document.getElementById('stxt').style.color = '';
  setTimeout(refresh, 800);
}
async function cashout() {
  if (!confirm('Sell all open positions at market price?')) return;
  await fetch('/api/cashout', {method:'POST'});
  setTimeout(refresh, 1000);
}
function showToast(msg, ok=true) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.borderColor = ok ? 'var(--grn)' : '#f23645';
  el.style.color       = ok ? 'var(--grn)' : '#f23645';
  el.classList.add('show');
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove('show'), 3000);
}
function showConfirmModal(title, body, meta=[]) {
  const modal = document.getElementById('confirm-modal');
  const titleEl = document.getElementById('confirm-title');
  const bodyEl = document.getElementById('confirm-body');
  const metaEl = document.getElementById('confirm-meta');
  if (!modal || !titleEl || !bodyEl || !metaEl) return;
  titleEl.textContent = title || 'Saved';
  bodyEl.textContent = body || '';
  metaEl.innerHTML = (meta || []).map(item => `<span class="badge bg-blue">${item}</span>`).join('');
  modal.classList.add('show');
}
function hideConfirmModal() {
  document.getElementById('confirm-modal')?.classList.remove('show');
}

// ══════════════════════════ SIGNAL EXPLORER ══════════════════════════
async function pollSignals() {
  try {
    const [data, pattern, ops] = await Promise.all([
      fetch('/api/signal-explorer').then(r => r.json()).catch(() => null),
      fetch('/api/pattern-lab').then(r => r.json()).catch(() => null),
      fetch('/api/ops-metrics').then(r => r.json()).catch(() => null),
    ]);
    if (!data) return;
    _sigData = data.recent || [];
    _patternLab = pattern || { tokens: [], deployers: [], themes: [] };
    _opsMetrics = ops || { stats: {}, top_whales: [], threat_map: [], liquidity_risks: [], route_mix: [], failure_reasons: [] };
    document.getElementById('sig-total').textContent = data.stats.total_evaluated;
    document.getElementById('sig-pass-rate').textContent = data.stats.pass_rate + '%';
    const topR = data.stats.top_reject_reasons;
    document.getElementById('sig-top-reject').textContent = topR.length ? topR[0].reason.slice(0,30) : '\u2014';
    renderSignals();
    renderPatternLab();
    renderOpsRadar();
  } catch(e) {}
}

function renderSignals() {
  let items = _sigData;
  const query = (document.getElementById('sig-search')?.value || '').trim().toLowerCase();
  if (sigFilter === 'pass') items = items.filter(s => s.passed);
  if (sigFilter === 'fail') items = items.filter(s => !s.passed);
  if (query) items = items.filter(s => signalSearchText(s).includes(query));
  const container = document.getElementById('sig-list');
  _sigView = items;
  document.getElementById('sig-visible-count').textContent = `${_sigView.length} rows`;
  if (!_sigView.length) {
    container.innerHTML = '<div style="padding:20px;text-align:center;color:var(--t3);font-size:12px">No evaluated coins match this filter yet.</div>';
    return;
  }
  if (!_sigSelectedKey || !_sigView.some(s => signalKey(s) === _sigSelectedKey)) {
    _sigSelected = 0;
    _sigSelectedKey = signalKey(_sigView[0]);
  } else {
    _sigSelected = _sigView.findIndex(s => signalKey(s) === _sigSelectedKey);
  }
  container.innerHTML = `
    <table class="sig-table">
      <thead>
        <tr>
          <th>Time</th>
          <th>Result</th>
          <th>Token</th>
          <th>Reason</th>
          <th>3-Point Checklist</th>
          <th>Score</th>
          <th>Price</th>
          <th>1h</th>
          <th>Age</th>
          <th>Vol</th>
          <th>Liq</th>
          <th>MC</th>
          <th>Deployer</th>
          <th>Narrative</th>
          <th>Whale</th>
          <th>Threat</th>
          <th>Vol Spike</th>
          <th>Holder Growth</th>
          <th>Smart First 10</th>
        </tr>
      </thead>
      <tbody>
        ${_sigView.map((s, i) => {
          const sc = s.score?.total || 0;
          const col = scoreColor(sc);
          const intel = s.intel || {};
          return `<tr class="${_sigSelected===i?'active':''}" onclick="showSignalDetail(${i})">
            <td><div>${fmtDateTime(s.logged_at || s.ts)}</div><div class="sig-mini-muted">${s.ts || ''}</div></td>
            <td><span class="sig-badge ${s.passed?'sig-pass':'sig-fail'}">${s.passed?'PASS':'FAIL'}</span></td>
            <td class="sig-cell-token">
              <div class="sig-token-name">${s.name||'?'}</div>
              <div class="sig-token-meta">${(s.mint||'').slice(0,10)}…</div>
            </td>
            <td class="sig-reason">${s.reason || '—'}</td>
            <td><div class="sig-checks">${renderChecklistMini(s)}</div></td>
            <td>
              <div class="score-bar" style="margin-bottom:4px"><div class="score-bar-fill" style="width:${sc}%;background:${col}"></div></div>
              <div style="color:${col};font-weight:800;font-family:monospace">${sc}</div>
            </td>
            <td>${fmtPrice(s.price)}</td>
            <td class="${chgClass(s.change||0)}">${chgStr(s.change||0)}</td>
            <td>${fmtAge(s.age_min||0)}</td>
            <td>${fmtK(s.vol)}</td>
            <td>${fmtK(s.liq)}</td>
            <td>${fmtK(s.mc)}</td>
            <td><div style="font-family:monospace">${shortWallet(intel.deployer_wallet)}</div><div class="sig-mini-muted">rep ${intel.deployer_score||0}</div></td>
            <td><div>${intel.narrative_score||0}</div><div class="sig-mini-muted">${(intel.narrative_tags||[]).slice(0,2).join(' · ') || 'no tags'}</div></td>
            <td><div>${intel.whale_score||0}</div><div class="sig-mini-muted">act ${intel.whale_action_score||0} · cc ${intel.cluster_confidence||0}</div></td>
            <td><div>${intel.threat_risk_score||0}</div><div class="sig-mini-muted">${(intel.threat_flags||[]).slice(0,2).join(' · ') || 'clean'}</div></td>
            <td>${Number(intel.volume_spike_ratio || 0).toFixed(1)}x</td>
            <td>${Math.round(Number(intel.holder_growth_1h || 0))}%</td>
            <td>${intel.smart_wallet_first10||0}</td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>
  `;
  if (_sigSelected >= 0) showSignalDetail(_sigSelected, true);
}

function renderPatternLab() {
  const box = document.getElementById('pattern-lab');
  if (!box) return;
  const tokens = _patternLab.tokens || [];
  const deployers = _patternLab.deployers || [];
  const themes = _patternLab.themes || [];
  if (!tokens.length) {
    box.innerHTML = '<div style="text-align:center;color:var(--t3);font-size:12px;padding:24px 0">Pattern lab will fill as the scanner tracks deployers and runners.</div>';
    return;
  }
  box.innerHTML = `
    <div class="sec-label">Pattern Lab</div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">
      ${themes.map(t => `<span class="badge bg-blue">${t.tag} · ${t.count}</span>`).join('') || '<span style="font-size:11px;color:var(--t3)">No repeating themes yet</span>'}
    </div>
    <div style="font-size:11px;color:var(--t2);font-weight:700;margin-bottom:6px">Mapped runners</div>
    <div style="display:flex;flex-direction:column;gap:8px;margin-bottom:12px">
      ${tokens.slice(0,5).map(t => `
        <div style="padding:8px 10px;border:1px solid var(--b1);border-radius:8px;background:rgba(255,255,255,.02)">
          <div style="display:flex;justify-content:space-between;gap:8px">
            <div>
              <div style="font-size:12px;font-weight:700;color:var(--t1)">${t.name||t.symbol||'?'}</div>
              <div style="font-size:10px;color:var(--t3)">${(t.narrative_tags||[]).join(' · ') || 'no tags yet'}</div>
            </div>
            <div style="text-align:right">
              <div style="font-size:12px;font-weight:700;color:var(--gold2)">whale ${t.whale_score||0}</div>
              <div style="font-size:10px;color:var(--t3)">act ${t.whale_action_score||0} · ${(t.max_multiple||1).toFixed(2)}x</div>
            </div>
          </div>
        </div>`).join('')}
    </div>
    <div style="font-size:11px;color:var(--t2);font-weight:700;margin-bottom:6px">Top deployers</div>
    <div style="display:flex;flex-direction:column;gap:6px">
      ${deployers.slice(0,4).map(d => `
        <div style="display:flex;justify-content:space-between;gap:8px;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.04);font-size:11px">
          <span style="color:var(--t2);font-family:monospace">${shortWallet(d.wallet)}</span>
          <span style="color:var(--t1)">rep ${d.reputation_score} · ${d.best_multiple.toFixed(2)}x best</span>
        </div>`).join('')}
    </div>
  `;
}

function renderOpsRadar() {
  const box = document.getElementById('ops-radar');
  if (!box) return;
  const stats = _opsMetrics.stats || {};
  const rpc = _opsMetrics.rpc_health || {};
  const adaptive = _opsMetrics.adaptive || {};
  const whales = _opsMetrics.top_whales || [];
  const threats = _opsMetrics.threat_map || [];
  const liquidity = _opsMetrics.liquidity_risks || [];
  const routes = _opsMetrics.route_mix || [];
  const failures = _opsMetrics.failure_reasons || [];
  box.innerHTML = `
    <div class="sec-label">Ops Radar</div>
    <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-bottom:12px">
      <div style="padding:8px 10px;border:1px solid var(--b1);border-radius:8px">
        <div style="font-size:10px;color:var(--t3)">Quote Success</div>
        <div style="font-size:15px;font-weight:800;color:var(--t1)">${stats.quote_success_rate||0}%</div>
        <div style="font-size:10px;color:var(--t3)">${stats.avg_quote_ms||0} ms avg</div>
      </div>
      <div style="padding:8px 10px;border:1px solid var(--b1);border-radius:8px">
        <div style="font-size:10px;color:var(--t3)">Send Success</div>
        <div style="font-size:15px;font-weight:800;color:var(--t1)">${stats.send_success_rate||0}%</div>
        <div style="font-size:10px;color:var(--t3)">${stats.avg_send_ms||0} ms avg</div>
      </div>
      <div style="padding:8px 10px;border:1px solid var(--b1);border-radius:8px">
        <div style="font-size:10px;color:var(--t3)">Fill Slippage</div>
        <div style="font-size:15px;font-weight:800;color:var(--t1)">${stats.avg_fill_slippage_bps||0} bps</div>
        <div style="font-size:10px;color:var(--t3)">${stats.quotes_24h||0} quotes</div>
      </div>
      <div style="padding:8px 10px;border:1px solid var(--b1);border-radius:8px">
        <div style="font-size:10px;color:var(--t3)">Exit Risks</div>
        <div style="font-size:15px;font-weight:800;color:var(--t1)">${stats.no_exit_count||0}</div>
        <div style="font-size:10px;color:var(--t3)">${stats.transfer_hook_count||0} hooks</div>
      </div>
      <div style="padding:8px 10px;border:1px solid var(--b1);border-radius:8px">
        <div style="font-size:10px;color:var(--t3)">RPC Health</div>
        <div style="font-size:15px;font-weight:800;color:var(--t1)">${rpc.fail_rate||0}% fail</div>
        <div style="font-size:10px;color:var(--t3)">p95 ${rpc.p95_ms||0} ms</div>
      </div>
      <div style="padding:8px 10px;border:1px solid var(--b1);border-radius:8px">
        <div style="font-size:10px;color:var(--t3)">Adaptive Guard</div>
        <div style="font-size:15px;font-weight:800;color:var(--t1)">L${adaptive.relax_level||0}</div>
        <div style="font-size:10px;color:var(--t3)">${adaptive.zero_buy_hours||0} zero-buy hrs</div>
      </div>
    </div>
    <div style="font-size:11px;color:var(--t2);font-weight:700;margin-bottom:6px">Whale Radar</div>
    <div style="display:flex;flex-direction:column;gap:6px;margin-bottom:12px">
      ${whales.slice(0,4).map(w => `
        <div style="display:flex;justify-content:space-between;gap:8px;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.04);font-size:11px">
          <span style="color:var(--t1)">${w.name}</span>
          <span style="color:var(--t2)">w ${w.whale_score} · a ${w.whale_action_score} · cc ${w.cluster_confidence}</span>
        </div>`).join('') || '<div style="font-size:11px;color:var(--t3)">No whale entities ranked yet</div>'}
    </div>
    <div style="font-size:11px;color:var(--t2);font-weight:700;margin-bottom:6px">Threat Map</div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">
      ${threats.slice(0,4).map(t => `<span class="badge bg-red">${t.name} · ${t.threat_risk_score}</span>`).join('') || '<span style="font-size:11px;color:var(--t3)">No active threat flags</span>'}
    </div>
    <div style="font-size:11px;color:var(--t2);font-weight:700;margin-bottom:6px">Liquidity Risk</div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">
      ${liquidity.slice(0,4).map(t => `<span class="badge bg-gold">${t.name} · -${t.liquidity_drop_pct}%</span>`).join('') || '<span style="font-size:11px;color:var(--t3)">No liquidity drains detected</span>'}
    </div>
    <div style="font-size:11px;color:var(--t2);font-weight:700;margin-bottom:6px">Execution Routes</div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">
      ${routes.slice(0,4).map(r => `<span class="badge bg-blue">${r.route} · ${r.count}</span>`).join('') || '<span style="font-size:11px;color:var(--t3)">No route data yet</span>'}
    </div>
    <div style="font-size:11px;color:var(--t2);font-weight:700;margin-bottom:6px">Top Failures</div>
    <div style="display:flex;gap:6px;flex-wrap:wrap">
      ${failures.slice(0,4).map(f => `<span class="badge bg-muted">${f.reason} · ${f.count}</span>`).join('') || '<span style="font-size:11px;color:var(--t3)">No recent execution failures</span>'}
    </div>
  `;
}

function showSignalDetail(idx, fromRender=false) {
  _sigSelected = idx;
  const s = _sigView[idx];
  if (!s) return;
  _sigSelectedKey = signalKey(s);
  if (!fromRender) renderSignals();
  const sc = s.score || {};
  const intel = s.intel || {};
  const checklist = intel.checklist || [];
  const links = intel.social_links || [];
  const tags = intel.narrative_tags || [];
  const detail = document.getElementById('sig-detail');
  detail.innerHTML = `
    <div style="font-weight:700;font-size:14px;margin-bottom:4px">${s.name||'?'}</div>
    <div style="font-size:10px;color:var(--t3);font-family:monospace;margin-bottom:12px">${s.mint||''}</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
      <span class="sig-badge ${s.passed?'sig-pass':'sig-fail'}">${s.passed?'PASS':'FAIL'}</span>
      <span class="badge bg-muted">${fmtDateTime(s.logged_at || s.ts)}</span>
      <span class="badge bg-blue">${intel.green_lights||0}/3 green lights</span>
      <span class="badge bg-gold">Narrative ${intel.narrative_score||0}</span>
      <span class="badge bg-grn">Deployer ${intel.deployer_score||0}</span>
    </div>
    <div style="display:flex;gap:16px;margin-bottom:14px;flex-wrap:wrap">
      <div style="font-size:11px"><span style="color:var(--t3)">Price</span> <b>${fmtPrice(s.price)}</b></div>
      <div style="font-size:11px"><span style="color:var(--t3)">MCap</span> <b>${fmtK(s.mc)}</b></div>
      <div style="font-size:11px"><span style="color:var(--t3)">Vol</span> <b>${fmtK(s.vol)}</b></div>
      <div style="font-size:11px"><span style="color:var(--t3)">Liq</span> <b>${fmtK(s.liq)}</b></div>
      <div style="font-size:11px"><span style="color:var(--t3)">Age</span> <b>${fmtAge(s.age_min||0)}</b></div>
      <div style="font-size:11px"><span style="color:var(--t3)">Chg</span> <b class="${chgClass(s.change)}">${chgStr(s.change||0)}</b></div>
    </div>
    <div class="sec-label">Three-Signal Checklist</div>
    ${(checklist||[]).map(c => `<div class="filter-step">
      <div class="filter-dot ${c.passed?'pass':'fail'}"></div>
      <span style="flex:1">${c.name}</span>
      <span style="color:var(--t1);font-weight:600;font-size:11px">${c.value||''}</span>
      <span style="color:var(--t3);font-size:10px">${c.threshold||''}</span>
    </div>`).join('') || '<div style="font-size:11px;color:var(--t3);margin-bottom:10px">Waiting for deployer and holder intel…</div>'}
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 14px">
      <span class="badge bg-muted">${(intel.volume_spike_ratio||0).toFixed ? intel.volume_spike_ratio.toFixed(1) : intel.volume_spike_ratio || 0}x vol</span>
      <span class="badge bg-muted">${intel.holder_growth_1h||0}% holders</span>
      <span class="badge bg-muted">${intel.first_buyer_count||0} first buyers</span>
      <span class="badge bg-muted">${intel.smart_wallet_first10||0} smart first 10</span>
    </div>
    <div class="sec-label">Whale / Entity Score</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">
      <span class="badge bg-blue">Whale ${intel.whale_score||0}/100</span>
      <span class="badge bg-blue">Action ${intel.whale_action_score||0}/100</span>
      <span class="badge bg-muted">Cluster ${intel.cluster_confidence||0}</span>
      <span class="badge bg-muted">Infra penalty ${intel.infra_penalty||0}</span>
    </div>
    <div class="sec-label">Threat / Exit Risk</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">
      <span class="badge bg-red">Threat ${intel.threat_risk_score||0}/100</span>
      <span class="badge bg-muted">Program ${(intel.token_program||'').startsWith('Tokenz') ? 'Token-2022' : 'SPL Token'}</span>
      <span class="badge bg-muted">Exit ${intel.can_exit === false ? 'blocked' : intel.can_exit === true ? 'available' : 'unknown'}</span>
      <span class="badge bg-muted">Hook ${intel.transfer_hook_enabled ? 'enabled' : 'none'}</span>
      <span class="badge bg-muted">Liq drain ${Math.round(Number(intel.liquidity_drop_pct||0))}%</span>
    </div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px">
      ${(intel.threat_flags||[]).map(flag => `<span class="badge bg-red">${flag}</span>`).join('') || '<span style="font-size:11px;color:var(--t3)">No threat flags currently detected</span>'}
    </div>
    <div class="sec-label">Deployer / Social Intent</div>
    <div style="font-size:11px;color:var(--t2);margin-bottom:6px">Deployer: <span style="font-family:monospace;color:var(--t1)">${shortWallet(intel.deployer_wallet)}</span></div>
    <div style="font-size:11px;color:var(--t2);margin-bottom:6px">First buyers: <b style="color:var(--t1)">${intel.first_buyer_count||0}</b> &nbsp; Smart in first 10: <b style="color:var(--t1)">${intel.smart_wallet_first10||0}</b></div>
    <div style="font-size:11px;color:var(--t2);margin-bottom:6px">Infrastructure labels: <b style="color:var(--t1)">${(intel.infra_labels||[]).join(', ') || 'none'}</b></div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">
      ${tags.map(tag => `<span class="badge bg-blue">${tag}</span>`).join('') || '<span style="font-size:11px;color:var(--t3)">No narrative tags yet</span>'}
    </div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px">
      ${links.slice(0,4).map(link => `<a class="badge bg-muted" href="${link}" target="_blank" rel="noreferrer">${link.replace(/^https?:\\/\\//,'').slice(0,28)}</a>`).join('') || '<span style="font-size:11px;color:var(--t3)">No socials found</span>'}
    </div>
    <div class="sec-label">AI Score Breakdown (${sc.total||0}/100)</div>
    <canvas id="sig-radar" height="200" style="margin-bottom:14px"></canvas>
    <div class="sec-label">Filter Pipeline</div>
    ${(s.filters||[]).map(f => `<div class="filter-step">
      <div class="filter-dot ${f.passed?'pass':'fail'}"></div>
      <span style="flex:1">${f.name}</span>
      <span style="color:var(--t1);font-weight:600;font-size:11px">${f.value}</span>
      <span style="color:var(--t3);font-size:10px">${f.threshold}</span>
    </div>`).join('')}
    <div class="sec-label" style="margin-top:14px">Score Components</div>
    <div style="display:flex;gap:4px;height:16px;border-radius:4px;overflow:hidden">
      <div style="flex:${sc.volume||0};background:#3b82f6" title="Volume: ${sc.volume||0}"></div>
      <div style="flex:${sc.liquidity||0};background:#14c784" title="Liquidity: ${sc.liquidity||0}"></div>
      <div style="flex:${sc.age||0};background:#a855f7" title="Age: ${sc.age||0}"></div>
      <div style="flex:${Math.max(0,sc.price_change||0)};background:#f59e0b" title="Change: ${sc.price_change||0}"></div>
      <div style="flex:${sc.momentum||0};background:#06b6d4" title="Momentum: ${sc.momentum||0}"></div>
    </div>
    <div style="display:flex;gap:12px;margin-top:6px;font-size:10px;color:var(--t3);flex-wrap:wrap">
      <span style="color:#3b82f6">\u25cf Vol ${sc.volume||0}</span>
      <span style="color:#14c784">\u25cf Liq ${sc.liquidity||0}</span>
      <span style="color:#a855f7">\u25cf Age ${sc.age||0}</span>
      <span style="color:#f59e0b">\u25cf Chg ${sc.price_change||0}</span>
      <span style="color:#06b6d4">\u25cf Mom ${sc.momentum||0}</span>
    </div>
  `;
  // Draw radar chart
  setTimeout(() => {
    const canvas = document.getElementById('sig-radar');
    if (!canvas || !window.Chart) return;
    if (_charts.sigRadar) _charts.sigRadar.destroy();
    _charts.sigRadar = new Chart(canvas, {
      type: 'radar',
      data: {
        labels: ['Volume', 'Liquidity', 'Age', 'Change', 'Momentum'],
        datasets: [{
          data: [sc.volume||0, sc.liquidity||0, sc.age||0, Math.max(0,sc.price_change||0), sc.momentum||0],
          backgroundColor: 'rgba(37,99,235,.15)',
          borderColor: '#3b82f6',
          borderWidth: 2,
          pointBackgroundColor: '#3b82f6',
          pointRadius: 3,
        }]
      },
      options: {
        scales: { r: { beginAtZero: true, max: 25, ticks: { display: false }, grid: { color: 'rgba(255,255,255,.06)' }, pointLabels: { color: '#94a3b8', font: { size: 10 } } } },
        plugins: { legend: { display: false } },
        animation: { duration: 300 },
      }
    });
  }, 50);
}

// ══════════════════════════ WHALE DASHBOARD ══════════════════════════
async function pollWhales() {
  try {
    const data = await fetch('/api/whale-activity').then(r => r.json());
    // Render whale stat cards
    const cardsEl = document.getElementById('whale-cards');
    cardsEl.innerHTML = (data.whale_stats||[]).map(w => `
      <div class="whale-card">
        <div style="font-weight:700;font-size:13px;margin-bottom:4px">&#x1f40b; ${w.label}</div>
        <div style="font-size:10px;color:var(--t3);font-family:monospace;margin-bottom:8px">${w.address.slice(0,8)}\u2026</div>
        <div style="display:flex;gap:12px">
          <div style="font-size:11px"><span style="color:var(--t3)">Buys</span> <b>${w.buys_24h}</b></div>
          <div style="font-size:11px"><span style="color:var(--t3)">Avg P&L</span> <b class="${w.avg_pnl>=0?'c-grn':'c-red'}">${w.avg_pnl>0?'+':''}${w.avg_pnl}%</b></div>
        </div>
      </div>
    `).join('') || '<div style="color:var(--t3);font-size:12px;padding:12px">No whale wallets configured</div>';
    // Render whale feed
    const feed = document.getElementById('whale-feed');
    const buys = data.recent_buys || [];
    if (!buys.length) { feed.innerHTML = '<div style="padding:20px;text-align:center;color:var(--t3);font-size:12px">Waiting for whale activity\u2026</div>'; return; }
    feed.innerHTML = buys.slice(0, 30).map(b => {
      const pnlStr = b.pnl_pct != null ? `<span class="${b.pnl_pct>=0?'c-grn':'c-red'}" style="font-weight:700">${b.pnl_pct>0?'+':''}${b.pnl_pct}%</span>` : '<span style="color:var(--t3)">-</span>';
      return `<div class="whale-entry">
        <div style="width:8px;height:8px;border-radius:50%;background:var(--grn);flex-shrink:0"></div>
        <div style="flex:1;min-width:0">
          <div style="font-weight:600;font-size:12px;color:var(--t1)">${b.name||'?'}</div>
          <div style="font-size:10px;color:var(--t3)">${b.label} \u2022 ${fmtK(b.mc)} MCap</div>
        </div>
        <div style="text-align:right">
          <div style="font-size:11px">${pnlStr}</div>
          <div style="font-size:10px;color:var(--t3)">${b.ts||''}</div>
        </div>
      </div>`;
    }).join('');
  } catch(e) {}
}

// ══════════════════════════ POSITION ANALYTICS ══════════════════════════
async function pollPositions() {
  try {
    const data = await fetch('/api/position-analytics').then(r => r.json());
    const positions = data.positions || [];
    const risk = data.risk || {};
    // Render position cards
    const container = document.getElementById('pos-cards');
    if (!positions.length) { container.innerHTML = '<div style="text-align:center;color:var(--t3);font-size:13px;padding:40px">No open positions</div>'; } else {
      container.innerHTML = positions.map((p, i) => {
        const pnlCls = p.pnl_pct >= 0 ? 'c-grn' : 'c-red';
        return `<div class="pos-card">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
            <div>
              <div style="font-weight:700;font-size:14px">${p.name}${p.tp1_hit?'<span class="badge bg-grn" style="margin-left:6px;font-size:9px">TP1</span>':''}</div>
              <div style="font-size:10px;color:var(--t3)">${fmtAge(p.age_min)} old \u2022 ${p.entry_sol} SOL</div>
            </div>
            <div style="text-align:right">
              <div class="${pnlCls}" style="font-size:18px;font-weight:700">${p.pnl_pct>0?'+':''}${p.pnl_pct}%</div>
              <div style="font-size:10px;color:var(--t3)">${fmtPrice(p.current_price)}</div>
            </div>
          </div>
          <div class="pos-chart-mini"><canvas id="pos-chart-${i}" height="70"></canvas></div>
          <div style="display:flex;gap:8px;margin-top:8px">
            <div style="flex:1;font-size:10px;color:var(--t3)">Entry: ${fmtPrice(p.entry_price)}</div>
            <div style="flex:1;font-size:10px;color:var(--t3)">Peak: ${fmtPrice(p.peak_price)}</div>
            <div style="flex:1;font-size:10px;color:var(--t3)">TP1: ${fmtPrice(p.tp1_price)}</div>
            <div style="flex:1;font-size:10px;color:var(--t3)">SL: ${fmtPrice(p.sl_price)}</div>
          </div>
          ${p.dev_wallet ? '<div style="font-size:10px;color:var(--t3);margin-top:6px">Dev: <span style="font-family:monospace">'+p.dev_wallet.slice(0,12)+'\u2026</span></div>' : ''}
          <div style="display:flex;gap:6px;margin-top:10px">
            <button class="btn btn-ghost" style="flex:1;font-size:11px;padding:6px" onclick="fetch('/api/manual-sell',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mint:'${p.mint}',pct:0.5})}).then(()=>setTimeout(pollPositions,1500))">Sell 50%</button>
            <button class="btn btn-danger" style="flex:1;font-size:11px;padding:6px" onclick="fetch('/api/manual-sell',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mint:'${p.mint}',pct:1.0})}).then(()=>setTimeout(pollPositions,1500))">Sell 100%</button>
          </div>
        </div>`;
      }).join('');
      // Draw mini charts
      setTimeout(() => {
        positions.forEach((p, i) => {
          const canvas = document.getElementById('pos-chart-' + i);
          if (!canvas || !p.chart || !p.chart.length || !window.Chart) return;
          const key = 'posChart' + i;
          if (_charts[key]) _charts[key].destroy();
          const prices = p.chart.map(c => c.price);
          const labels = p.chart.map(c => '');
          _charts[key] = new Chart(canvas, {
            type: 'line',
            data: {
              labels,
              datasets: [{
                data: prices,
                borderColor: p.pnl_pct >= 0 ? '#14c784' : '#f23645',
                borderWidth: 1.5,
                fill: true,
                backgroundColor: p.pnl_pct >= 0 ? 'rgba(20,199,132,.08)' : 'rgba(248,113,113,.08)',
                pointRadius: 0, tension: 0.3,
              }]
            },
            options: {
              scales: { x: { display: false }, y: { display: false } },
              plugins: { legend: { display: false }, tooltip: { enabled: false } },
              animation: false,
              maintainAspectRatio: false,
            }
          });
        });
      }, 100);
    }
    // Render risk dashboard
    const ddPct = risk.drawdown_limit > 0 ? Math.min(100, risk.session_drawdown / risk.drawdown_limit * 100) : 0;
    const posPct = risk.max_positions > 0 ? Math.min(100, risk.positions_count / risk.max_positions * 100) : 0;
    document.getElementById('risk-dd-val').textContent = `${risk.session_drawdown} / ${risk.drawdown_limit} SOL`;
    document.getElementById('risk-dd-bar').style.width = ddPct + '%';
    document.getElementById('risk-pos-val').textContent = `${risk.positions_count} / ${risk.max_positions}`;
    document.getElementById('risk-pos-bar').style.width = posPct + '%';
    document.getElementById('risk-losses').textContent = risk.consecutive_losses;
    document.getElementById('risk-cooldown').textContent = risk.cooldown_remaining > 0 ? Math.ceil(risk.cooldown_remaining) + 's' : 'Off';
    document.getElementById('risk-peak').textContent = risk.peak_balance;
    document.getElementById('risk-now').textContent = risk.current_balance;
  } catch(e) {}
}

// ══════════════════════════ P&L CHARTS ══════════════════════════
async function loadPnl(range) {
  try {
    const data = await fetch('/api/pnl-history?days=' + range).then(r => r.json());
    const s = data.stats || {};
    const pnlTotalEl = document.getElementById('pnl-total');
    pnlTotalEl.textContent = (s.total_pnl >= 0 ? '+' : '') + s.total_pnl + ' SOL';
    pnlTotalEl.className = 'sval ' + (s.total_pnl >= 0 ? 'c-grn' : 'c-red');
    document.getElementById('pnl-winrate').textContent = s.win_rate + '%';
    document.getElementById('pnl-best').textContent = '+' + s.best_trade + ' SOL';
    document.getElementById('pnl-worst').textContent = s.worst_trade + ' SOL';
    document.getElementById('pnl-dd').textContent = '-' + s.max_drawdown + ' SOL';
    document.getElementById('pnl-trades').textContent = s.total_trades;
    // Cumulative P&L chart
    const cum = data.cumulative || [];
    if (cum.length && window.Chart) {
      const labels = cum.map(c => c.ts.split('T')[0] || '');
      const pnls = cum.map(c => c.pnl);
      const dds = cum.map(c => -c.drawdown);
      if (_charts.pnlLine) _charts.pnlLine.destroy();
      _charts.pnlLine = new Chart(document.getElementById('pnl-chart'), {
        type: 'line',
        data: {
          labels,
          datasets: [{
            label: 'Cumulative P&L (SOL)',
            data: pnls,
            borderColor: '#14c784',
            borderWidth: 2,
            fill: true,
            backgroundColor: ctx => {
              const g = ctx.chart.ctx.createLinearGradient(0, 0, 0, 220);
              g.addColorStop(0, 'rgba(20,199,132,.15)');
              g.addColorStop(1, 'rgba(20,199,132,0)');
              return g;
            },
            pointRadius: 0, tension: 0.3,
          }]
        },
        options: {
          scales: {
            x: { display: true, ticks: { color: '#475569', font: { size: 9 }, maxTicksLimit: 8 }, grid: { color: 'rgba(255,255,255,.03)' } },
            y: { ticks: { color: '#475569', font: { size: 9 } }, grid: { color: 'rgba(255,255,255,.05)' } }
          },
          plugins: { legend: { display: false }, tooltip: { mode: 'index', intersect: false } },
          interaction: { mode: 'index', intersect: false },
          animation: { duration: 400 },
        }
      });
      // Drawdown chart
      if (_charts.ddLine) _charts.ddLine.destroy();
      _charts.ddLine = new Chart(document.getElementById('dd-chart'), {
        type: 'line',
        data: {
          labels,
          datasets: [{
            label: 'Drawdown (SOL)',
            data: dds,
            borderColor: '#f23645',
            borderWidth: 1.5,
            fill: true,
            backgroundColor: 'rgba(242,54,69,.1)',
            pointRadius: 0, tension: 0.3,
          }]
        },
        options: {
          scales: {
            x: { display: true, ticks: { color: '#475569', font: { size: 9 }, maxTicksLimit: 8 }, grid: { display: false } },
            y: { ticks: { color: '#475569', font: { size: 9 } }, grid: { color: 'rgba(255,255,255,.03)' } }
          },
          plugins: { legend: { display: false } },
          animation: { duration: 400 },
        }
      });
    }
  } catch(e) {}
}

// ── Init ──────────────────────────────────────────────────────────────────────
(function() {
  const initialPreset = PRESET_SETTINGS['{{PRESET}}'] ? '{{PRESET}}' : 'balanced';
  _settingsDirty = false;
  document._settingsFocused = false;
  setSettingsSaveState('Loading saved settings…');
  applySettingsToForm({ ...SETTINGS_DEFAULTS, ...(PRESET_SETTINGS[initialPreset] || {}), preset: initialPreset }, initialPreset);
  bindSettingsInputs();
})();
window.addEventListener('resize', () => {
  if (treeCanvas && document.getElementById('tree-panel').style.display !== 'none') {
    treeCanvas.width = treeCanvas.parentElement.clientWidth;
  }
});
refresh();
setInterval(refresh, 5000);
pollFeed(); setInterval(pollFeed, 4000);
pollListings(); setInterval(pollListings, 6000);
</script>
"""


# ── Admin Page ─────────────────────────────────────────────────────────────────
ADMIN_HTML = _CSS + """
<style>
.ai-suggest{background:linear-gradient(135deg,rgba(20,199,132,.12),rgba(123,97,255,.08));border:1px solid rgba(20,199,132,.3);border-radius:12px;padding:20px;margin-bottom:20px}
.ai-suggest h3{color:#14c784;margin:0 0 8px}
.ai-suggest p{margin:0 0 12px;color:var(--t2);font-size:13px}
.setting-row{display:grid;grid-template-columns:1fr 140px 80px;gap:10px;align-items:center;padding:10px 0;border-bottom:1px solid var(--bdr)}
.setting-row:last-child{border-bottom:none}
.setting-label{font-size:13px;color:var(--t2)}
.setting-desc{font-size:10px;color:var(--t3);margin-top:2px}
.setting-input{background:var(--bg3);border:1px solid var(--bdr);color:var(--t1);padding:6px 10px;border-radius:6px;font-size:12px;width:100%;font-family:monospace}
.setting-unit{font-size:11px;color:var(--t3)}
</style>

<nav class="nav">
  <a href="/" class="logo"><div class="logo-mark">S</div>SolTrader</a>
  <div class="nav-r">
    <span class="badge bg-gold">Admin</span>
    <a href="/dashboard">My Dashboard</a>
    <a href="/logout" style="color:var(--t3)!important">Sign Out</a>
  </div>
</nav>

<div class="wrap">
  <div style="margin-bottom:24px">
    <div class="page-title">Admin Panel</div>
  </div>

  <!-- AI Suggestion Box -->
  <div class="ai-suggest" id="ai-box">
    <h3>🤖 AI Market Analysis</h3>
    <p id="ai-reason">Loading market analysis…</p>
    <div id="ai-stats" style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:12px"></div>
    <button class="btn btn-primary" onclick="applyAISuggestion()" id="ai-apply-btn">Apply AI Recommendation</button>
  </div>

  <!-- Platform Stats -->
  <div class="stats" style="margin-bottom:24px">
    <div class="stat"><div class="slabel">Total Users</div><div class="sval">{{USERS}}</div></div>
    <div class="stat"><div class="slabel">Active Bots</div><div class="sval c-grn">{{ACTIVE}}</div></div>
    <div class="stat"><div class="slabel">Perf Fees Owed</div><div class="sval c-gold">{{FEE_SOL}} SOL</div></div>
  </div>

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
    <!-- Global Preset Tuner -->
    <div class="panel">
      <div class="sec-label">Global Preset Overrides <span style="font-size:10px;color:var(--t3);font-weight:400">— affects all users on this preset</span></div>

      <div style="margin:12px 0">
        <label class="flabel">Preset to Edit</label>
        <select class="finput" id="edit-preset" onchange="loadPreset(this.value)">
          <option value="safe">Safe</option>
          <option value="balanced" selected>Balanced</option>
          <option value="aggressive">Aggressive</option>
          <option value="degen">Degen</option>
        </select>
      </div>

      <div id="preset-settings">
        <div class="setting-row">
          <div><div class="setting-label">Max Buy (SOL)</div><div class="setting-desc">SOL spent per trade</div></div>
          <input class="setting-input" id="s-max-buy" type="number" step="0.01" value="0.04">
          <div class="setting-unit">SOL</div>
        </div>
        <div class="setting-row">
          <div><div class="setting-label">Take Profit 1</div><div class="setting-desc">Sell 50% at this multiplier</div></div>
          <input class="setting-input" id="s-tp1" type="number" step="0.1" value="1.5">
          <div class="setting-unit">×</div>
        </div>
        <div class="setting-row">
          <div><div class="setting-label">Take Profit 2</div><div class="setting-desc">Sell rest at this multiplier</div></div>
          <input class="setting-input" id="s-tp2" type="number" step="0.1" value="3.0">
          <div class="setting-unit">×</div>
        </div>
        <div class="setting-row">
          <div><div class="setting-label">Stop Loss</div><div class="setting-desc">Exit if drops below this ratio</div></div>
          <input class="setting-input" id="s-sl" type="number" step="0.05" value="0.75">
          <div class="setting-unit">×</div>
        </div>
        <div class="setting-row">
          <div><div class="setting-label">Trailing Stop</div><div class="setting-desc">% drop from peak to exit</div></div>
          <input class="setting-input" id="s-trail" type="number" step="0.05" value="0.20">
          <div class="setting-unit">%</div>
        </div>
        <div class="setting-row">
          <div><div class="setting-label">Max Token Age</div><div class="setting-desc">Skip tokens older than this</div></div>
          <input class="setting-input" id="s-age" type="number" step="1" value="30">
          <div class="setting-unit">min</div>
        </div>
        <div class="setting-row">
          <div><div class="setting-label">Time Stop</div><div class="setting-desc">Exit if not profitable after</div></div>
          <input class="setting-input" id="s-tstop" type="number" step="1" value="30">
          <div class="setting-unit">min</div>
        </div>
        <div class="setting-row">
          <div><div class="setting-label">Min Liquidity</div><div class="setting-desc">Skip pools below this</div></div>
          <input class="setting-input" id="s-liq" type="number" step="1000" value="10000">
          <div class="setting-unit">USD</div>
        </div>
        <div class="setting-row">
          <div><div class="setting-label">Min Market Cap</div><div class="setting-desc">Skip tokens below this MC</div></div>
          <input class="setting-input" id="s-minmc" type="number" step="1000" value="5000">
          <div class="setting-unit">USD</div>
        </div>
        <div class="setting-row">
          <div><div class="setting-label">Max Market Cap</div><div class="setting-desc">Skip tokens above this MC</div></div>
          <input class="setting-input" id="s-maxmc" type="number" step="10000" value="150000">
          <div class="setting-unit">USD</div>
        </div>
        <div class="setting-row">
          <div><div class="setting-label">Priority Fee</div><div class="setting-desc">Lamports to outbid bots</div></div>
          <input class="setting-input" id="s-prio" type="number" step="10000" value="30000">
          <div class="setting-unit">λ</div>
        </div>
        <div class="setting-row">
          <div><div class="setting-label">Drawdown Limit</div><div class="setting-desc">Stop bot after losing this</div></div>
          <input class="setting-input" id="s-dd" type="number" step="0.1" value="0.5">
          <div class="setting-unit">SOL</div>
        </div>
        <div class="setting-row">
          <div><div class="setting-label">Max Positions</div><div class="setting-desc">Max simultaneous trades</div></div>
          <input class="setting-input" id="s-maxpos" type="number" step="1" value="3">
          <div class="setting-unit">#</div>
        </div>
        <div class="setting-row">
          <div><div class="setting-label">Min Volume</div><div class="setting-desc">Skip tokens below this 24h volume</div></div>
          <input class="setting-input" id="s-minvol" type="number" step="100" value="3000">
          <div class="setting-unit">USD</div>
        </div>
        <div class="setting-row">
          <div><div class="setting-label">Min AI Score</div><div class="setting-desc">Skip tokens scoring below this (0-100)</div></div>
          <input class="setting-input" id="s-minscore" type="number" min="0" max="100" step="5" value="30">
          <div class="setting-unit">/100</div>
        </div>
        <div class="setting-row">
          <div><div class="setting-label">Risk Per Trade</div><div class="setting-desc">Caps entry size as % of wallet</div></div>
          <input class="setting-input" id="s-risk" type="number" step="0.1" value="2.0">
          <div class="setting-unit">%</div>
        </div>
        <div class="setting-row">
          <div><div class="setting-label">Holder Growth (1h)</div><div class="setting-desc">Minimum holder growth for acceleration</div></div>
          <input class="setting-input" id="s-holders" type="number" step="5" value="50">
          <div class="setting-unit">%</div>
        </div>
        <div class="setting-row">
          <div><div class="setting-label">Min Narrative Score</div><div class="setting-desc">Required narrative timing score</div></div>
          <input class="setting-input" id="s-narr" type="number" step="1" value="20">
          <div class="setting-unit">pts</div>
        </div>
        <div class="setting-row">
          <div><div class="setting-label">Green Lights Required</div><div class="setting-desc">Minimum checklist confirmations before buy</div></div>
          <input class="setting-input" id="s-lights" type="number" min="1" max="3" step="1" value="3">
          <div class="setting-unit">#</div>
        </div>
        <div class="setting-row">
          <div><div class="setting-label">Volume Spike Multiple</div><div class="setting-desc">Minimum volume expansion for signal strength</div></div>
          <input class="setting-input" id="s-volspike" type="number" step="0.5" value="10">
          <div class="setting-unit">×</div>
        </div>
        <div class="setting-row">
          <div><div class="setting-label">Late Entry Limit</div><div class="setting-desc">Reject coins already extended beyond this multiple</div></div>
          <input class="setting-input" id="s-latemult" type="number" step="0.5" value="5.0">
          <div class="setting-unit">×</div>
        </div>
        <div class="setting-row">
          <div><div class="setting-label">Nuclear Narrative Score</div><div class="setting-desc">Narrative score needed to override late-entry guard</div></div>
          <input class="setting-input" id="s-nuclear" type="number" step="1" value="40">
          <div class="setting-unit">pts</div>
        </div>
      </div>
      <button class="btn btn-primary" style="width:100%;margin-top:16px" onclick="savePreset()">💾 Save Preset Override</button>
    </div>

    <!-- Users + Dev Blacklist -->
    <div>
      <div class="panel" style="margin-bottom:16px">
        <div class="sec-label">Registered Users</div>
        <table class="tbl">
          <thead><tr><th>Email</th><th>Plan</th><th>Bot</th><th>Joined</th></tr></thead>
          <tbody>{{USER_ROWS}}</tbody>
        </table>
      </div>

      <div class="panel" style="margin-bottom:16px">
        <div class="sec-label">Dev Blacklist <span style="font-size:10px;color:var(--t3);font-weight:400">— auto-populated on rug detection</span></div>
        <div id="blacklist-tbl"><div style="font-size:13px;color:var(--t3)">Loading…</div></div>
        <div style="display:flex;gap:8px;margin-top:12px">
          <input class="finput" id="bl-wallet" placeholder="Paste dev wallet address" style="flex:1">
          <button class="btn btn-danger" onclick="addBlacklist()">Block</button>
        </div>
      </div>

      <div class="panel">
        <div class="sec-label">Performance Fees</div>
        <table class="tbl">
          <thead><tr><th>User ID</th><th>Session PnL</th><th>Fee Owed</th><th>Date</th></tr></thead>
          <tbody>{{FEE_ROWS}}</tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<script>
const PRESET_DEFAULTS = {{PRESET_DEFAULTS}};
let aiSuggestion = null;

function loadPreset(name) {
  const p = PRESET_DEFAULTS[name];
  if (!p) return;
  document.getElementById('s-max-buy').value  = p.buy;
  document.getElementById('s-tp1').value      = p.tp1;
  document.getElementById('s-tp2').value      = p.tp2;
  document.getElementById('s-sl').value       = p.sl;
  document.getElementById('s-trail').value    = p.trail;
  document.getElementById('s-age').value      = p.age;
  document.getElementById('s-tstop').value    = p.tstop;
  document.getElementById('s-liq').value      = p.liq;
  document.getElementById('s-minmc').value    = p.minmc;
  document.getElementById('s-maxmc').value    = p.maxmc;
  document.getElementById('s-prio').value     = p.prio;
  document.getElementById('s-dd').value       = p.dd;
  document.getElementById('s-maxpos').value   = p.maxpos;
  document.getElementById('s-minvol').value   = p.minvol;
  document.getElementById('s-minscore').value = p.minscore;
  document.getElementById('s-risk').value     = p.risk;
  document.getElementById('s-holders').value  = p.holders;
  document.getElementById('s-narr').value     = p.narr;
  document.getElementById('s-lights').value   = p.lights;
  document.getElementById('s-volspike').value = p.volspike;
  document.getElementById('s-latemult').value = p.latemult;
  document.getElementById('s-nuclear').value  = p.nuclear;
}

function collectPresetSettings(preset) {
  return {
    preset,
    max_buy_sol:      parseFloat(document.getElementById('s-max-buy').value),
    tp1_mult:         parseFloat(document.getElementById('s-tp1').value),
    tp2_mult:         parseFloat(document.getElementById('s-tp2').value),
    stop_loss:        parseFloat(document.getElementById('s-sl').value),
    trail_pct:        parseFloat(document.getElementById('s-trail').value),
    max_age_min:      parseInt(document.getElementById('s-age').value),
    time_stop_min:    parseInt(document.getElementById('s-tstop').value),
    min_liq:          parseFloat(document.getElementById('s-liq').value),
    min_mc:           parseFloat(document.getElementById('s-minmc').value),
    max_mc:           parseFloat(document.getElementById('s-maxmc').value),
    priority_fee:     parseInt(document.getElementById('s-prio').value),
    drawdown_limit_sol: parseFloat(document.getElementById('s-dd').value),
    max_correlated:   parseInt(document.getElementById('s-maxpos').value),
    min_vol:          parseFloat(document.getElementById('s-minvol').value),
    min_score:        parseInt(document.getElementById('s-minscore').value),
    risk_per_trade_pct: parseFloat(document.getElementById('s-risk').value),
    min_holder_growth_pct: parseFloat(document.getElementById('s-holders').value),
    min_narrative_score: parseInt(document.getElementById('s-narr').value),
    min_green_lights: parseInt(document.getElementById('s-lights').value),
    min_volume_spike_mult: parseFloat(document.getElementById('s-volspike').value),
    late_entry_mult:  parseFloat(document.getElementById('s-latemult').value),
    nuclear_narrative_score: parseInt(document.getElementById('s-nuclear').value),
  };
}

async function savePreset() {
  const preset = document.getElementById('edit-preset').value;
  const settings = collectPresetSettings(preset);
  const r = await fetch('/api/admin/override-preset', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(settings)
  }).then(r=>r.json()).catch(()=>({ok:false}));
  alert(r.ok ? '✅ Preset saved and applied to active bots!' : '❌ Save failed');
}

async function loadAI() {
  const r = await fetch('/api/admin/suggest-settings').then(r=>r.json()).catch(()=>null);
  if (!r) return;
  aiSuggestion = r.suggestion;
  document.getElementById('ai-reason').textContent = r.suggestion.reason;
  const s = r.stats;
  if (s && s.total_trades) {
    document.getElementById('ai-stats').innerHTML = `
      <div style="background:var(--bg3);border-radius:8px;padding:10px;text-align:center">
        <div style="font-size:20px;font-weight:800;color:var(--t1)">${s.total_trades}</div>
        <div style="font-size:10px;color:var(--t3)">Trades (24h)</div>
      </div>
      <div style="background:var(--bg3);border-radius:8px;padding:10px;text-align:center">
        <div style="font-size:20px;font-weight:800;color:#14c784">${s.win_rate}%</div>
        <div style="font-size:10px;color:var(--t3)">Win Rate</div>
      </div>
      <div style="background:var(--bg3);border-radius:8px;padding:10px;text-align:center">
        <div style="font-size:20px;font-weight:800;color:#14c784">+${s.avg_win_sol}</div>
        <div style="font-size:10px;color:var(--t3)">Avg Win (SOL)</div>
      </div>
      <div style="background:var(--bg3);border-radius:8px;padding:10px;text-align:center">
        <div style="font-size:20px;font-weight:800;color:#f23645">${s.avg_loss_sol}</div>
        <div style="font-size:10px;color:var(--t3)">Avg Loss (SOL)</div>
      </div>`;
  }
}

async function applyAISuggestion() {
  if (!aiSuggestion) return;
  document.getElementById('edit-preset').value = aiSuggestion.preset;
  loadPreset(aiSuggestion.preset);
  const r = await fetch('/api/admin/override-preset', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(collectPresetSettings(aiSuggestion.preset))
  }).then(r => r.json()).catch(() => ({ok:false}));
  alert(r.ok ? '✅ AI recommendation applied globally to that preset.' : '❌ Could not apply AI recommendation');
}

async function addBlacklist() {
  const w = document.getElementById('bl-wallet').value.trim();
  if (!w) return;
  const r = await fetch('/api/admin/blacklist-dev', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({dev_wallet:w})
  }).then(r=>r.json()).catch(()=>({ok:false}));
  if (r.ok) { document.getElementById('bl-wallet').value=''; loadBlacklist(); }
}

async function loadBlacklist() {
  const r = await fetch('/api/admin/dev-blacklist').then(r=>r.json()).catch(()=>[]);
  if (!r.length) {
    document.getElementById('blacklist-tbl').innerHTML = '<div style="font-size:12px;color:var(--t3)">No blacklisted wallets yet</div>';
    return;
  }
  document.getElementById('blacklist-tbl').innerHTML = r.map(w =>
    `<div style="font-size:11px;color:var(--t2);padding:4px 0;border-bottom:1px solid var(--bdr);font-family:monospace">
      ${w.dev_wallet} <span style="color:var(--t3)">— ${w.reason}</span>
    </div>`
  ).join('');
}

loadPreset('balanced');
loadAI();
loadBlacklist();
</script>
"""

if __name__ == "__main__":
    ensure_background_workers_started()
    print(f"\n  SolTrader Platform → http://localhost:5000")
    print(f"  Admin account: {ADMIN_EMAIL}")
    print(f"  Database: {DATABASE_URL}\n")
    port = int(os.getenv("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
