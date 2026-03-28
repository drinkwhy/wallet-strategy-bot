"""
SaaS Trading Bot Platform — SolTrader
Run: python app.py
Then open: http://localhost:5000

Required .env variables:
  SECRET_KEY, FERNET_KEY, HELIUS_RPC,
  STRIPE_SECRET_KEY, STRIPE_PRICE_BASIC, STRIPE_PRICE_PRO, ADMIN_EMAIL
"""

import atexit
import html
import os, threading, time, base64, json, requests, base58, secrets, hashlib, random
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
import signal
import psycopg2
import psycopg2.extras
import psycopg2.pool
import string
from collections import deque
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo
from flask import Flask, request, redirect, url_for, session, jsonify, Response
from flask import send_file
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from cryptography.fernet import Fernet
import bcrypt
import stripe

from dotenv import load_dotenv
from backtest_engine import simulate_backtest, simulate_event_tape_backtest, simulate_policy_comparison
from edge_reporting import build_edge_report, derive_edge_guard_state, summarize_edge_report_history
from execution_controls import (
    DEFAULT_EXECUTION_CONTROL,
    determine_execution_policy,
    normalize_execution_control,
    normalize_execution_mode,
    normalize_policy_mode,
)
from learning_engine import (
    classify_flow_regime_row,
    score_feature_snapshot_with_family,
    score_recent_candidates_for_regime,
    train_regime_model_family,
)
from optimizer_engine import build_outcome_labels, summarize_feature_edges, summarize_regime_edges, sweep_entry_filters
from quant_platform import (
    CANONICAL_STRATEGIES,
    build_flow_snapshot,
    build_feature_snapshot,
    evaluate_shadow_strategy,
    shadow_position_update,
    summarize_flow_regime,
    summarize_opportunity_matrix,
)

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
ADMIN_EMAIL        = os.getenv("ADMIN_EMAIL", "founder@drinkwhy.com")
_admin_env         = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", ADMIN_EMAIL).split(",") if e.strip()}
# Hardcoded admins — always have full Elite access regardless of env config
ADMIN_EMAILS       = _admin_env | {"founder@drinkwhy.com", "racefreak5263@yahoo.com"}
PERF_FEE_FREE      = 0.25   # 25% of profits (profit-only plan, no subscription)
PERF_FEE_BASIC     = 0.15   # 15% of profits
PERF_FEE_PRO       = 0.10   # 10% of profits
PERF_FEE_ELITE     = 0.08   # 8% of profits
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
SENDGRID_API_KEY   = os.getenv("SENDGRID_API_KEY", "")
SMTP_FROM          = os.getenv("SMTP_FROM", "noreply@soltrader.app")
REFERRAL_COMMISSION = 0.10  # 10% of referred user's first month
FEE_WALLET          = os.getenv("FEE_WALLET", "")  # your SOL wallet to receive perf fees
ANKR_RPC            = os.getenv("ANKR_RPC", "").strip()
# Sanitize: Railway UI sometimes stores "Value: https://..." — strip the prefix
if ANKR_RPC.lower().startswith("value:"):
    ANKR_RPC = ANKR_RPC.split(":", 1)[1].strip()
if ANKR_RPC and not ANKR_RPC.startswith("http"):
    print(f"[WARN] ANKR_RPC ignored — not a valid URL: {ANKR_RPC[:60]}", flush=True)
    ANKR_RPC = ""

# ── Helius Enhanced API ───────────────────────────────────────────────────────
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "").strip()
if not HELIUS_API_KEY:
    # Try to extract from HELIUS_RPC URL (?api-key=...)
    try:
        from urllib.parse import parse_qs, urlparse as _urlparse
        _parsed = _urlparse(HELIUS_RPC)
        HELIUS_API_KEY = parse_qs(_parsed.query).get("api-key", [""])[0]
    except Exception:
        pass
HELIUS_SHARED_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else ""
HELIUS_SENDER_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else ""
HELIUS_REST_API   = "https://api-mainnet.helius-rpc.com/v0"
if HELIUS_API_KEY:
    print(f"[CONFIG] Helius Enhanced API loaded — key={HELIUS_API_KEY[:8]}...", flush=True)
else:
    print("[WARN] No HELIUS_API_KEY — Enhanced APIs (Priority Fee, DAS) unavailable", flush=True)

# ── Shared HTTP session for connection pooling ────────────────────────────────
import requests.adapters
_http_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=10,
    pool_maxsize=20,
    max_retries=0,
)
_http_session.mount("https://", _adapter)
_http_session.mount("http://", _adapter)

fernet        = Fernet(FERNET_KEY)
stripe.api_key = STRIPE_SECRET
SOL_MINT      = "So11111111111111111111111111111111111111112"
PUMP_PROGRAM  = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
HEADERS       = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
_http_session.headers.update(HEADERS)
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
        "max_buy_sol":0.02,"tp1_mult":1.3,"tp2_mult":3.0,
        "trail_pct":0.15,"stop_loss":0.80,"max_age_min":720,"time_stop_min":15,
        "min_liq":5000,"min_mc":3000,"max_mc":150000,"priority_fee":10000,
        "min_vol":5000,"min_score":20,"cooldown_min":15,
        "risk_per_trade_pct":2.0,"min_holder_growth_pct":8,"min_narrative_score":3,
        "min_green_lights":0,"min_volume_spike_mult":1.5,"late_entry_mult":5.0,
        "offpeak_min_change":20,
        "max_hot_change":400.0,
        "nuclear_narrative_score":42,
        "anti_rug":True,"check_holders":True,"max_correlated":5,"drawdown_limit_sol":0.3,
        "listing_sniper":True,"peak_plateau_mode":True,"tp1_sell_pct":0.50,
        "min_composite_score":20,"min_confidence":0.45,"min_buy_sell_ratio":1.2,
        "min_smart_wallet_buys":1,"min_net_flow_sol":0.5,"min_unique_buyers":3,
    },
    "balanced": {
        "label":"Balanced — Medium Risk / Steady Profit",
        "description":"Moderate positions, balanced take-profits. Best for most markets.",
        "max_buy_sol":0.04,"tp1_mult":1.3,"tp2_mult":4.0,
        "trail_pct":0.20,"stop_loss":0.75,"max_age_min":480,"time_stop_min":20,
        "min_liq":2000,"min_mc":2000,"max_mc":250000,"priority_fee":30000,
        "min_vol":3000,"min_score":15,"cooldown_min":10,
        "risk_per_trade_pct":2.0,"min_holder_growth_pct":5,"min_narrative_score":2,
        "min_green_lights":0,"min_volume_spike_mult":1.2,"late_entry_mult":5.0,
        "offpeak_min_change":18,
        "max_hot_change":400.0,
        "nuclear_narrative_score":40,
        "anti_rug":True,"check_holders":True,"max_correlated":3,"drawdown_limit_sol":0.5,
        "listing_sniper":True,"peak_plateau_mode":True,"tp1_sell_pct":0.50,
        "min_composite_score":15,"min_confidence":0.35,"min_buy_sell_ratio":1.0,
        "min_smart_wallet_buys":0,"min_net_flow_sol":0,"min_unique_buyers":0,
    },
    "aggressive": {
        "label":"Aggressive — Higher Risk / Bigger Swings",
        "description":"Larger positions, wider stops. More exposure for trending markets.",
        "max_buy_sol":0.07,"tp1_mult":1.3,"tp2_mult":6.0,
        "trail_pct":0.25,"stop_loss":0.75,"max_age_min":360,"time_stop_min":25,
        "min_liq":1000,"min_mc":2000,"max_mc":400000,"priority_fee":60000,
        "min_vol":1000,"min_score":15,"cooldown_min":7,
        "risk_per_trade_pct":2.0,"min_holder_growth_pct":3,"min_narrative_score":1,
        "min_green_lights":0,"min_volume_spike_mult":1.0,"late_entry_mult":5.0,
        "offpeak_min_change":15,
        "max_hot_change":400.0,
        "nuclear_narrative_score":38,
        "anti_rug":True,"check_holders":True,"max_correlated":5,"drawdown_limit_sol":0.8,
        "listing_sniper":True,"peak_plateau_mode":True,"tp1_sell_pct":0.50,
        "min_composite_score":15,"min_confidence":0.25,"min_buy_sell_ratio":0.8,
        "min_smart_wallet_buys":0,"min_net_flow_sol":0,"min_unique_buyers":0,
    },
    "degen": {
        "label":"Degen — High Risk / Max Profit",
        "description":"Larger positions, wide stops. For hot markets only.",
        "max_buy_sol":0.10,"tp1_mult":1.3,"tp2_mult":10.0,
        "trail_pct":0.30,"stop_loss":0.75,"max_age_min":240,"time_stop_min":30,
        "min_liq":500,"min_mc":2000,"max_mc":500000,"priority_fee":100000,
        "min_vol":500,"min_score":15,"cooldown_min":5,
        "risk_per_trade_pct":2.0,"min_holder_growth_pct":20,"min_narrative_score":0,
        "min_green_lights":0,"min_volume_spike_mult":0,"late_entry_mult":5.0,
        "offpeak_min_change":12,
        "max_hot_change":400.0,
        "nuclear_narrative_score":35,
        "anti_rug":True,"check_holders":False,"max_correlated":5,"drawdown_limit_sol":1.0,
        "listing_sniper":True,"peak_plateau_mode":True,"tp1_sell_pct":0.50,
        "min_composite_score":15,"min_confidence":0.15,"min_buy_sell_ratio":0.5,
        "min_smart_wallet_buys":0,"min_net_flow_sol":0,"min_unique_buyers":0,
    },
    "custom": {
        "label":"Custom — Manual Exit Tuning",
        "description":"Balanced entry filters with your own take-profit and stop rules.",
        "max_buy_sol":0.04,"tp1_mult":1.3,"tp2_mult":4.0,
        "trail_pct":0.20,"stop_loss":0.75,"max_age_min":480,"time_stop_min":20,
        "min_liq":2000,"min_mc":2000,"max_mc":250000,"priority_fee":30000,
        "min_vol":3000,"min_score":15,"cooldown_min":10,
        "risk_per_trade_pct":2.0,"min_holder_growth_pct":5,"min_narrative_score":2,
        "min_green_lights":0,"min_volume_spike_mult":1.2,"late_entry_mult":5.0,
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

# ── V2 Optimized Presets (shadow-test only, not live) ──────────────────
# These run in parallel with the original presets in shadow trading
# to A/B test tighter entries, faster exits, and better risk math.
# Once shadow data proves they outperform, auto-tune promotes them to live.
SHADOW_V2_PRESETS = {
    "safe_v2": {
        **PRESETS["safe"],
        "label":"Safe V2 — Optimized",
        "tp1_mult":1.15,"tp2_mult":2.5,"stop_loss":0.87,"time_stop_min":10,
        "trail_pct":0.18,"cooldown_min":10,
        "min_score":35,"min_green_lights":1,"max_hot_change":150.0,
        "tp1_sell_pct":0.70,"max_threat_score":45,
        # Moonshot: if ratio >= 1.5 (50%+ gain), widen trail to let it run
        "moonshot_trigger":1.5,"moonshot_trail_pct":0.30,
    },
    "balanced_v2": {
        **PRESETS["balanced"],
        "label":"Balanced V2 — Optimized",
        "tp1_mult":1.15,"tp2_mult":3.0,"stop_loss":0.85,"time_stop_min":14,
        "trail_pct":0.22,"cooldown_min":6,
        "min_score":25,"min_green_lights":1,"max_hot_change":150.0,
        "tp1_sell_pct":0.70,"max_threat_score":45,
        "moonshot_trigger":1.5,"moonshot_trail_pct":0.35,
    },
    "aggressive_v2": {
        **PRESETS["aggressive"],
        "label":"Aggressive V2 — Optimized",
        "tp1_mult":1.15,"tp2_mult":5.0,"stop_loss":0.83,"time_stop_min":18,
        "trail_pct":0.28,"cooldown_min":4,
        "min_score":20,"min_green_lights":1,"max_hot_change":150.0,
        "tp1_sell_pct":0.70,"max_threat_score":45,
        "moonshot_trigger":1.5,"moonshot_trail_pct":0.40,
    },
    "degen_v2": {
        **PRESETS["degen"],
        "label":"Degen V2 — Optimized",
        "tp1_mult":1.15,"tp2_mult":8.0,"stop_loss":0.80,"time_stop_min":22,
        "trail_pct":0.32,"cooldown_min":2,
        "min_score":15,"min_green_lights":1,"max_hot_change":150.0,
        "tp1_sell_pct":0.70,"max_threat_score":45,
        "moonshot_trigger":1.5,"moonshot_trail_pct":0.45,
    },
}
# V2 strategies to shadow-test alongside originals
SHADOW_V2_STRATEGIES = list(SHADOW_V2_PRESETS.keys())

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
    ("peak_plateau_mode", bool), ("tp1_sell_pct", float),
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
    return preset if (preset in PRESETS or preset in SHADOW_V2_PRESETS) else "balanced"


def strip_auto_relax_state(settings):
    if not isinstance(settings, dict):
        return {}
    cleaned = dict(settings)
    for key in AUTO_RELAX_STATE_FIELDS:
        cleaned.pop(key, None)
    return cleaned


def central_trading_window():
    now = datetime.now(CENTRAL_TZ)
    hour_utc = now.astimezone(timezone.utc).hour
    # Block low-liquidity overnight hours (UTC 23:00-10:00)
    # Data showed 2 wins / 13 losses during these hours
    if hour_utc >= 23 or hour_utc < 10:
        return now, False
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
        # drawdown_limit_sol now comes from preset (may be auto-tuned by shadow optimization)
        if row.get("custom_settings"):
            try:
                custom = json.loads(row["custom_settings"])
                if isinstance(custom, dict):
                    settings.update(custom)
            except Exception:
                pass
    return preset_name, strip_auto_relax_state(settings)


def _serialize_execution_control(control):
    normalized = normalize_execution_control(control)
    for key in ("active_policy_updated_at", "auto_promote_locked_until"):
        value = normalized.get(key)
        if hasattr(value, "isoformat"):
            normalized[key] = value.isoformat()
    return normalized


def load_user_execution_control(user_id):
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT execution_mode, decision_policy, model_threshold, auto_promote,
                   auto_promote_window_days, auto_promote_min_reports, auto_promote_lock_minutes,
                   active_policy, active_policy_source, active_policy_report_id,
                   active_policy_updated_at, auto_promote_locked_until
            FROM bot_settings
            WHERE user_id=%s
        """, (user_id,))
        row = cur.fetchone() or {}
    finally:
        db_return(conn)
    return _serialize_execution_control(row)


def persist_user_execution_control(user_id, control, only_updates=False):
    merged = normalize_execution_control(control)
    conn = db()
    try:
        cur = conn.cursor()
        if only_updates:
            cur.execute("""
                INSERT INTO bot_settings (
                    user_id, execution_mode, decision_policy, model_threshold, auto_promote,
                    auto_promote_window_days, auto_promote_min_reports, auto_promote_lock_minutes,
                    active_policy, active_policy_source, active_policy_report_id,
                    active_policy_updated_at, auto_promote_locked_until
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (user_id) DO UPDATE SET
                    active_policy = EXCLUDED.active_policy,
                    active_policy_source = EXCLUDED.active_policy_source,
                    active_policy_report_id = EXCLUDED.active_policy_report_id,
                    active_policy_updated_at = EXCLUDED.active_policy_updated_at,
                    auto_promote_locked_until = EXCLUDED.auto_promote_locked_until
            """, (
                user_id,
                merged["execution_mode"],
                merged["policy_mode"],
                merged["model_threshold"],
                1 if merged["auto_promote"] else 0,
                merged["auto_promote_window_days"],
                merged["auto_promote_min_reports"],
                merged["auto_promote_lock_minutes"],
                merged["active_policy"],
                merged["active_policy_source"],
                merged.get("active_policy_report_id"),
                merged.get("active_policy_updated_at"),
                merged.get("auto_promote_locked_until"),
            ))
        else:
            cur.execute("""
                INSERT INTO bot_settings (
                    user_id, execution_mode, decision_policy, model_threshold, auto_promote,
                    auto_promote_window_days, auto_promote_min_reports, auto_promote_lock_minutes,
                    active_policy, active_policy_source, active_policy_report_id,
                    active_policy_updated_at, auto_promote_locked_until
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (user_id) DO UPDATE SET
                    execution_mode = EXCLUDED.execution_mode,
                    decision_policy = EXCLUDED.decision_policy,
                    model_threshold = EXCLUDED.model_threshold,
                    auto_promote = EXCLUDED.auto_promote,
                    auto_promote_window_days = EXCLUDED.auto_promote_window_days,
                    auto_promote_min_reports = EXCLUDED.auto_promote_min_reports,
                    auto_promote_lock_minutes = EXCLUDED.auto_promote_lock_minutes,
                    active_policy = EXCLUDED.active_policy,
                    active_policy_source = EXCLUDED.active_policy_source,
                    active_policy_report_id = EXCLUDED.active_policy_report_id,
                    active_policy_updated_at = EXCLUDED.active_policy_updated_at,
                    auto_promote_locked_until = EXCLUDED.auto_promote_locked_until
            """, (
                user_id,
                merged["execution_mode"],
                merged["policy_mode"],
                merged["model_threshold"],
                1 if merged["auto_promote"] else 0,
                merged["auto_promote_window_days"],
                merged["auto_promote_min_reports"],
                merged["auto_promote_lock_minutes"],
                merged["active_policy"],
                merged["active_policy_source"],
                merged.get("active_policy_report_id"),
                merged.get("active_policy_updated_at"),
                merged.get("auto_promote_locked_until"),
            ))
        conn.commit()
    finally:
        db_return(conn)
    return _serialize_execution_control(merged)


def resolve_user_execution_control(user_id, guard_state=None):
    control = load_user_execution_control(user_id)
    focus_window_days = int(control.get("auto_promote_window_days") or DEFAULT_EXECUTION_CONTROL["auto_promote_window_days"])
    rows = load_quant_edge_reports(limit=36)
    history = summarize_edge_report_history(rows, focus_window_days=focus_window_days)
    state = guard_state or derive_edge_guard_state(history)
    selection = determine_execution_policy(history, state, control)
    if selection.get("db_update"):
        control = {**control, **selection["db_update"]}
        control = persist_user_execution_control(user_id, control, only_updates=True)
        selection = determine_execution_policy(history, state, control)
    return {
        **selection,
        "control": _serialize_execution_control(selection.get("control") or control),
    }


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
                item.get("source"),
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

_db_pool = None
_db_semaphore = threading.Semaphore(20)

def _create_db_pool():
    global _db_pool
    for attempt in range(1, 31):
        try:
            _db_pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1, maxconn=20,
                dsn=DATABASE_URL,
                cursor_factory=psycopg2.extras.RealDictCursor,
                connect_timeout=10,
            )
            print(f"[Startup] DB pool created (attempt {attempt})", flush=True)
            return
        except Exception as e:
            print(f"[Startup] DB pool failed (attempt {attempt}/30): {e}", flush=True)
            time.sleep(6)
    print("[Startup] DB pool could not connect — will retry on first request", flush=True)

threading.Thread(target=_create_db_pool, daemon=True, name="db-pool-init").start()

def db():
    if _db_pool is None:
        # Pool still initializing — wait up to 60s
        for _ in range(60):
            time.sleep(1)
            if _db_pool is not None:
                break
        else:
            raise RuntimeError("DB not available — Postgres still starting up")
    if not _db_semaphore.acquire(timeout=10):
        raise RuntimeError("DB connection pool exhausted (10s timeout)")
    try:
        conn = _db_pool.getconn()
    except Exception:
        _db_semaphore.release()
        raise
    conn.autocommit = False
    if getattr(conn, "closed", 0):
        try:
            _db_pool.putconn(conn, close=True)
        except Exception:
            pass
        try:
            conn = _db_pool.getconn()
        except Exception:
            _db_semaphore.release()
            raise
        conn.autocommit = False
    return conn

def db_return(conn):
    """Return a connection to the pool. Call this instead of conn.close()."""
    try:
        conn.rollback()
    except Exception:
        pass
    try:
        _db_pool.putconn(conn)
    except Exception:
        pass
    _db_semaphore.release()


def db_health_check(retries=2):
    last_error = None
    for _ in range(max(1, int(retries))):
        conn = None
        try:
            conn = db()
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            db_return(conn)
            return True, None
        except Exception as exc:
            last_error = exc
            if conn is not None:
                try:
                    _db_pool.putconn(conn, close=True)
                    _db_semaphore.release()
                except Exception:
                    pass
    return False, str(last_error or "unknown database error")

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
            max_correlated INTEGER DEFAULT 3,
            execution_mode TEXT DEFAULT 'live',
            decision_policy TEXT DEFAULT 'rules',
            model_threshold REAL DEFAULT 60,
            auto_promote INTEGER DEFAULT 0,
            auto_promote_window_days INTEGER DEFAULT 7,
            auto_promote_min_reports INTEGER DEFAULT 3,
            auto_promote_lock_minutes INTEGER DEFAULT 180,
            active_policy TEXT DEFAULT 'rules',
            active_policy_source TEXT DEFAULT 'manual',
            active_policy_report_id INTEGER,
            active_policy_updated_at TIMESTAMP,
            auto_promote_locked_until TIMESTAMP
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
            unique_buyer_count INTEGER DEFAULT 0,
            unique_seller_count INTEGER DEFAULT 0,
            total_buy_sol REAL DEFAULT 0,
            total_sell_sol REAL DEFAULT 0,
            net_flow_sol REAL DEFAULT 0,
            buy_sell_ratio REAL DEFAULT 0,
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
        cur.execute("""
        CREATE TABLE IF NOT EXISTS market_tokens (
            mint TEXT PRIMARY KEY,
            name TEXT,
            symbol TEXT,
            first_seen_at TIMESTAMP DEFAULT NOW(),
            last_seen_at TIMESTAMP DEFAULT NOW(),
            first_price REAL,
            last_price REAL,
            peak_price REAL,
            trough_price REAL,
            first_mc REAL,
            last_mc REAL,
            first_liq REAL,
            last_liq REAL,
            first_vol REAL,
            last_vol REAL,
            latest_source TEXT,
            latest_payload_json TEXT,
            observations INTEGER DEFAULT 1
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS market_events (
            id SERIAL PRIMARY KEY,
            mint TEXT NOT NULL,
            event_type TEXT NOT NULL,
            source TEXT,
            name TEXT,
            symbol TEXT,
            price REAL,
            mc REAL,
            liq REAL,
            vol REAL,
            change_pct REAL,
            age_min REAL,
            payload_json TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS wallet_flow_events (
            id SERIAL PRIMARY KEY,
            event_key TEXT UNIQUE,
            mint TEXT NOT NULL,
            signature TEXT,
            wallet TEXT NOT NULL,
            side TEXT NOT NULL,
            sol_amount REAL DEFAULT 0,
            token_amount REAL DEFAULT 0,
            smart_wallet INTEGER DEFAULT 0,
            source TEXT,
            observed_at TIMESTAMP,
            payload_json TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS liquidity_delta_events (
            id SERIAL PRIMARY KEY,
            event_key TEXT UNIQUE,
            mint TEXT NOT NULL,
            event_type TEXT NOT NULL,
            source TEXT,
            previous_liq REAL DEFAULT 0,
            current_liq REAL DEFAULT 0,
            delta_liq REAL DEFAULT 0,
            delta_pct REAL DEFAULT 0,
            previous_price REAL DEFAULT 0,
            current_price REAL DEFAULT 0,
            observed_at TIMESTAMP,
            payload_json TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS token_feature_snapshots (
            id SERIAL PRIMARY KEY,
            mint TEXT NOT NULL,
            source TEXT,
            price REAL,
            composite_score REAL,
            confidence REAL,
            ai_score INTEGER,
            green_lights INTEGER,
            narrative_score INTEGER,
            deployer_score INTEGER,
            whale_score INTEGER,
            whale_action_score INTEGER,
            holder_growth_1h REAL,
            volume_spike_ratio REAL,
            threat_risk_score INTEGER,
            feature_json TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS token_flow_snapshots (
            id SERIAL PRIMARY KEY,
            mint TEXT NOT NULL,
            source TEXT,
            price REAL,
            mc REAL,
            liq REAL,
            vol REAL,
            age_min REAL,
            holder_count INTEGER DEFAULT 0,
            holder_growth_1h REAL DEFAULT 0,
            unique_buyer_count INTEGER DEFAULT 0,
            unique_seller_count INTEGER DEFAULT 0,
            first_buyer_count INTEGER DEFAULT 0,
            smart_wallet_buys INTEGER DEFAULT 0,
            smart_wallet_first10 INTEGER DEFAULT 0,
            total_buy_sol REAL DEFAULT 0,
            total_sell_sol REAL DEFAULT 0,
            net_flow_sol REAL DEFAULT 0,
            buy_sell_ratio REAL DEFAULT 0,
            buy_pressure_pct REAL DEFAULT 0,
            liquidity_drop_pct REAL DEFAULT 0,
            threat_risk_score INTEGER DEFAULT 0,
            transfer_hook_enabled INTEGER DEFAULT 0,
            can_exit INTEGER,
            regime_label TEXT DEFAULT 'neutral',
            flow_json TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS shadow_decisions (
            id SERIAL PRIMARY KEY,
            strategy_name TEXT NOT NULL,
            mint TEXT NOT NULL,
            name TEXT,
            source TEXT,
            passed INTEGER DEFAULT 0,
            score REAL,
            confidence REAL,
            price REAL,
            pass_reasons_json TEXT,
            blocker_reasons_json TEXT,
            feature_json TEXT,
            decision_json TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS model_decisions (
            id SERIAL PRIMARY KEY,
            mode TEXT NOT NULL,
            model_key TEXT NOT NULL,
            active_regime TEXT,
            mint TEXT NOT NULL,
            name TEXT,
            passed INTEGER DEFAULT 0,
            threshold REAL DEFAULT 60,
            model_score REAL,
            raw_score REAL,
            price REAL,
            fallback_reason TEXT,
            feature_json TEXT,
            driver_json TEXT,
            decision_json TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS shadow_positions (
            id SERIAL PRIMARY KEY,
            strategy_name TEXT NOT NULL,
            mint TEXT NOT NULL,
            name TEXT,
            source TEXT,
            status TEXT DEFAULT 'open',
            opened_at TIMESTAMP DEFAULT NOW(),
            closed_at TIMESTAMP,
            entry_price REAL,
            current_price REAL,
            exit_price REAL,
            peak_price REAL,
            trough_price REAL,
            take_profit_mult REAL,
            stop_loss_ratio REAL,
            time_stop_min INTEGER,
            score REAL,
            confidence REAL,
            max_upside_pct REAL DEFAULT 0,
            max_drawdown_pct REAL DEFAULT 0,
            realized_pnl_pct REAL,
            exit_reason TEXT,
            observations INTEGER DEFAULT 1,
            last_seen_at TIMESTAMP DEFAULT NOW(),
            feature_json TEXT,
            decision_json TEXT,
            tp1_hit INTEGER DEFAULT 0,
            tp1_pnl_pct REAL DEFAULT 0
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS backtest_runs (
            id SERIAL PRIMARY KEY,
            requested_by INTEGER,
            name TEXT,
            status TEXT DEFAULT 'queued',
            days INTEGER DEFAULT 7,
            replay_mode TEXT DEFAULT 'snapshot',
            strategy_filter TEXT,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            snapshots_processed INTEGER DEFAULT 0,
            tokens_processed INTEGER DEFAULT 0,
            trades_closed INTEGER DEFAULT 0,
            summary_json TEXT,
            config_json TEXT,
            error_text TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS backtest_trades (
            id SERIAL PRIMARY KEY,
            run_id INTEGER NOT NULL,
            strategy_name TEXT NOT NULL,
            mint TEXT NOT NULL,
            name TEXT,
            opened_at TIMESTAMP,
            closed_at TIMESTAMP,
            entry_price REAL,
            exit_price REAL,
            status TEXT,
            score REAL,
            confidence REAL,
            max_upside_pct REAL,
            max_drawdown_pct REAL,
            realized_pnl_pct REAL,
            exit_reason TEXT,
            feature_json TEXT,
            decision_json TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS quant_edge_reports (
            id SERIAL PRIMARY KEY,
            report_kind TEXT DEFAULT 'scheduled',
            window_days INTEGER DEFAULT 7,
            model_threshold REAL DEFAULT 60,
            active_regime TEXT,
            summary_json TEXT,
            policy_json TEXT,
            generated_at TIMESTAMP DEFAULT NOW()
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
            "CREATE INDEX IF NOT EXISTS idx_market_events_mint_created_at ON market_events (mint, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_market_events_created_at ON market_events (created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_market_tokens_last_seen ON market_tokens (last_seen_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_wallet_flow_events_mint_observed_at ON wallet_flow_events (mint, observed_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_wallet_flow_events_wallet_observed_at ON wallet_flow_events (wallet, observed_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_liquidity_delta_events_mint_observed_at ON liquidity_delta_events (mint, observed_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_token_feature_snapshots_mint_created_at ON token_feature_snapshots (mint, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_token_flow_snapshots_mint_created_at ON token_flow_snapshots (mint, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_token_flow_snapshots_created_at ON token_flow_snapshots (created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_shadow_decisions_strategy_created_at ON shadow_decisions (strategy_name, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_shadow_decisions_mint_created_at ON shadow_decisions (mint, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_model_decisions_mode_created_at ON model_decisions (mode, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_model_decisions_mint_created_at ON model_decisions (mint, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_shadow_positions_strategy_status ON shadow_positions (strategy_name, status, opened_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_shadow_positions_mint_status ON shadow_positions (mint, status, opened_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_backtest_runs_created_at ON backtest_runs (created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_backtest_runs_status ON backtest_runs (status, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_backtest_trades_run_id ON backtest_trades (run_id, strategy_name)",
            "CREATE INDEX IF NOT EXISTS idx_quant_edge_reports_window_generated_at ON quant_edge_reports (window_days, generated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_quant_edge_reports_kind_generated_at ON quant_edge_reports (report_kind, generated_at DESC)",
        ]:
            try:
                cur.execute(_idx_sql)
                conn.commit()
            except Exception:
                conn.rollback()
    finally:
        db_return(conn)

def _init_db_with_retry(max_attempts=10, delay=6):
    """Call init_db() with retries so the app starts even if Postgres is temporarily down."""
    for attempt in range(1, max_attempts + 1):
        try:
            init_db()
            print(f"[DB] init_db OK (attempt {attempt})", flush=True)
            return
        except Exception as e:
            print(f"[DB] init_db failed (attempt {attempt}/{max_attempts}): {e}", flush=True)
            if attempt < max_attempts:
                time.sleep(delay)
    print("[DB] init_db could not complete — app will retry at next request", flush=True)

_init_db_with_retry()

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
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMP DEFAULT NOW()",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS preset TEXT DEFAULT 'steady'",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS custom_settings TEXT",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS run_mode TEXT DEFAULT 'indefinite'",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS run_duration_min INTEGER DEFAULT 0",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS profit_target_sol REAL DEFAULT 0",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS is_running INTEGER DEFAULT 0",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS drawdown_limit_sol REAL DEFAULT 0.5",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS max_correlated INTEGER DEFAULT 5",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS execution_mode TEXT DEFAULT 'live'",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS decision_policy TEXT DEFAULT 'rules'",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS model_threshold REAL DEFAULT 60",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS auto_promote INTEGER DEFAULT 0",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS auto_promote_window_days INTEGER DEFAULT 7",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS auto_promote_min_reports INTEGER DEFAULT 3",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS auto_promote_lock_minutes INTEGER DEFAULT 180",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS active_policy TEXT DEFAULT 'rules'",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS active_policy_source TEXT DEFAULT 'manual'",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS active_policy_report_id INTEGER",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS active_policy_updated_at TIMESTAMP",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS auto_promote_locked_until TIMESTAMP",
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
            "ALTER TABLE token_intel ADD COLUMN IF NOT EXISTS unique_buyer_count INTEGER DEFAULT 0",
            "ALTER TABLE token_intel ADD COLUMN IF NOT EXISTS unique_seller_count INTEGER DEFAULT 0",
            "ALTER TABLE token_intel ADD COLUMN IF NOT EXISTS total_buy_sol REAL DEFAULT 0",
            "ALTER TABLE token_intel ADD COLUMN IF NOT EXISTS total_sell_sol REAL DEFAULT 0",
            "ALTER TABLE token_intel ADD COLUMN IF NOT EXISTS net_flow_sol REAL DEFAULT 0",
            "ALTER TABLE token_intel ADD COLUMN IF NOT EXISTS buy_sell_ratio REAL DEFAULT 0",
            "ALTER TABLE backtest_runs ADD COLUMN IF NOT EXISTS replay_mode TEXT DEFAULT 'snapshot'",
            "ALTER TABLE token_flow_snapshots ADD COLUMN IF NOT EXISTS regime_label TEXT DEFAULT 'neutral'",
            "ALTER TABLE shadow_positions ADD COLUMN IF NOT EXISTS tp1_hit INTEGER DEFAULT 0",
            "ALTER TABLE shadow_positions ADD COLUMN IF NOT EXISTS tp1_pnl_pct REAL DEFAULT 0",
            "ALTER TABLE shadow_positions ADD COLUMN IF NOT EXISTS blocked_by_cooldown INTEGER DEFAULT 0",
            "ALTER TABLE shadow_positions ADD COLUMN IF NOT EXISTS cooldown_setting_min INTEGER DEFAULT 0",
            "ALTER TABLE shadow_positions ADD COLUMN IF NOT EXISTS would_have_been_winner INTEGER DEFAULT 0",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS session_budget_sol REAL DEFAULT 0",
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
                unique_buyer_count INTEGER DEFAULT 0,
                unique_seller_count INTEGER DEFAULT 0,
                total_buy_sol REAL DEFAULT 0,
                total_sell_sol REAL DEFAULT 0,
                net_flow_sol REAL DEFAULT 0,
                buy_sell_ratio REAL DEFAULT 0,
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
            cur.execute("""CREATE TABLE IF NOT EXISTS market_tokens (
                mint TEXT PRIMARY KEY,
                name TEXT,
                symbol TEXT,
                first_seen_at TIMESTAMP DEFAULT NOW(),
                last_seen_at TIMESTAMP DEFAULT NOW(),
                first_price REAL,
                last_price REAL,
                peak_price REAL,
                trough_price REAL,
                first_mc REAL,
                last_mc REAL,
                first_liq REAL,
                last_liq REAL,
                first_vol REAL,
                last_vol REAL,
                latest_source TEXT,
                latest_payload_json TEXT,
                observations INTEGER DEFAULT 1)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS market_events (
                id SERIAL PRIMARY KEY,
                mint TEXT NOT NULL,
                event_type TEXT NOT NULL,
                source TEXT,
                name TEXT,
                symbol TEXT,
                price REAL,
                mc REAL,
                liq REAL,
                vol REAL,
                change_pct REAL,
                age_min REAL,
                payload_json TEXT,
                created_at TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS wallet_flow_events (
                id SERIAL PRIMARY KEY,
                event_key TEXT UNIQUE,
                mint TEXT NOT NULL,
                signature TEXT,
                wallet TEXT NOT NULL,
                side TEXT NOT NULL,
                sol_amount REAL DEFAULT 0,
                token_amount REAL DEFAULT 0,
                smart_wallet INTEGER DEFAULT 0,
                source TEXT,
                observed_at TIMESTAMP,
                payload_json TEXT,
                created_at TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS liquidity_delta_events (
                id SERIAL PRIMARY KEY,
                event_key TEXT UNIQUE,
                mint TEXT NOT NULL,
                event_type TEXT NOT NULL,
                source TEXT,
                previous_liq REAL DEFAULT 0,
                current_liq REAL DEFAULT 0,
                delta_liq REAL DEFAULT 0,
                delta_pct REAL DEFAULT 0,
                previous_price REAL DEFAULT 0,
                current_price REAL DEFAULT 0,
                observed_at TIMESTAMP,
                payload_json TEXT,
                created_at TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS token_feature_snapshots (
                id SERIAL PRIMARY KEY,
                mint TEXT NOT NULL,
                source TEXT,
                price REAL,
                composite_score REAL,
                confidence REAL,
                ai_score INTEGER,
                green_lights INTEGER,
                narrative_score INTEGER,
                deployer_score INTEGER,
                whale_score INTEGER,
                whale_action_score INTEGER,
                holder_growth_1h REAL,
                volume_spike_ratio REAL,
                threat_risk_score INTEGER,
                feature_json TEXT,
                created_at TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS token_flow_snapshots (
                id SERIAL PRIMARY KEY,
                mint TEXT NOT NULL,
                source TEXT,
                price REAL,
                mc REAL,
                liq REAL,
                vol REAL,
                age_min REAL,
                holder_count INTEGER DEFAULT 0,
                holder_growth_1h REAL DEFAULT 0,
                unique_buyer_count INTEGER DEFAULT 0,
                unique_seller_count INTEGER DEFAULT 0,
                first_buyer_count INTEGER DEFAULT 0,
                smart_wallet_buys INTEGER DEFAULT 0,
                smart_wallet_first10 INTEGER DEFAULT 0,
                total_buy_sol REAL DEFAULT 0,
                total_sell_sol REAL DEFAULT 0,
                net_flow_sol REAL DEFAULT 0,
                buy_sell_ratio REAL DEFAULT 0,
                buy_pressure_pct REAL DEFAULT 0,
                liquidity_drop_pct REAL DEFAULT 0,
                threat_risk_score INTEGER DEFAULT 0,
                transfer_hook_enabled INTEGER DEFAULT 0,
                can_exit INTEGER,
                regime_label TEXT DEFAULT 'neutral',
                flow_json TEXT,
                created_at TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS shadow_decisions (
                id SERIAL PRIMARY KEY,
                strategy_name TEXT NOT NULL,
                mint TEXT NOT NULL,
                name TEXT,
                source TEXT,
                passed INTEGER DEFAULT 0,
                score REAL,
                confidence REAL,
                price REAL,
                pass_reasons_json TEXT,
                blocker_reasons_json TEXT,
                feature_json TEXT,
                decision_json TEXT,
                created_at TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS model_decisions (
                id SERIAL PRIMARY KEY,
                mode TEXT NOT NULL,
                model_key TEXT NOT NULL,
                active_regime TEXT,
                mint TEXT NOT NULL,
                name TEXT,
                passed INTEGER DEFAULT 0,
                threshold REAL DEFAULT 60,
                model_score REAL,
                raw_score REAL,
                price REAL,
                fallback_reason TEXT,
                feature_json TEXT,
                driver_json TEXT,
                decision_json TEXT,
                created_at TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS shadow_positions (
                id SERIAL PRIMARY KEY,
                strategy_name TEXT NOT NULL,
                mint TEXT NOT NULL,
                name TEXT,
                source TEXT,
                status TEXT DEFAULT 'open',
                opened_at TIMESTAMP DEFAULT NOW(),
                closed_at TIMESTAMP,
                entry_price REAL,
                current_price REAL,
                exit_price REAL,
                peak_price REAL,
                trough_price REAL,
                take_profit_mult REAL,
                stop_loss_ratio REAL,
                time_stop_min INTEGER,
                score REAL,
                confidence REAL,
                max_upside_pct REAL DEFAULT 0,
                max_drawdown_pct REAL DEFAULT 0,
                realized_pnl_pct REAL,
                exit_reason TEXT,
                observations INTEGER DEFAULT 1,
                last_seen_at TIMESTAMP DEFAULT NOW(),
                feature_json TEXT,
                decision_json TEXT)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS backtest_runs (
                id SERIAL PRIMARY KEY,
                requested_by INTEGER,
                name TEXT,
                status TEXT DEFAULT 'queued',
                days INTEGER DEFAULT 7,
                replay_mode TEXT DEFAULT 'snapshot',
                strategy_filter TEXT,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                snapshots_processed INTEGER DEFAULT 0,
                tokens_processed INTEGER DEFAULT 0,
                trades_closed INTEGER DEFAULT 0,
                summary_json TEXT,
                config_json TEXT,
                error_text TEXT,
                created_at TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS backtest_trades (
                id SERIAL PRIMARY KEY,
                run_id INTEGER NOT NULL,
                strategy_name TEXT NOT NULL,
                mint TEXT NOT NULL,
                name TEXT,
                opened_at TIMESTAMP,
                closed_at TIMESTAMP,
                entry_price REAL,
                exit_price REAL,
                status TEXT,
                score REAL,
                confidence REAL,
                max_upside_pct REAL,
                max_drawdown_pct REAL,
                realized_pnl_pct REAL,
                exit_reason TEXT,
                feature_json TEXT,
                decision_json TEXT,
                created_at TIMESTAMP DEFAULT NOW())""")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_token_intel_last_updated ON token_intel (last_updated DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_deployer_stats_last_seen ON deployer_stats (last_seen_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_execution_events_user_id_created_at ON execution_events (user_id, created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_market_events_mint_created_at ON market_events (mint, created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_market_events_created_at ON market_events (created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_market_tokens_last_seen ON market_tokens (last_seen_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_wallet_flow_events_mint_observed_at ON wallet_flow_events (mint, observed_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_wallet_flow_events_wallet_observed_at ON wallet_flow_events (wallet, observed_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_liquidity_delta_events_mint_observed_at ON liquidity_delta_events (mint, observed_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_token_feature_snapshots_mint_created_at ON token_feature_snapshots (mint, created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_token_flow_snapshots_mint_created_at ON token_flow_snapshots (mint, created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_token_flow_snapshots_created_at ON token_flow_snapshots (created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_shadow_decisions_strategy_created_at ON shadow_decisions (strategy_name, created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_shadow_decisions_mint_created_at ON shadow_decisions (mint, created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_model_decisions_mode_created_at ON model_decisions (mode, created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_model_decisions_mint_created_at ON model_decisions (mint, created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_shadow_positions_strategy_status ON shadow_positions (strategy_name, status, opened_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_shadow_positions_mint_status ON shadow_positions (mint, status, opened_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_backtest_runs_created_at ON backtest_runs (created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_backtest_runs_status ON backtest_runs (status, created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_backtest_trades_run_id ON backtest_trades (run_id, strategy_name)")
        except Exception:
            conn.rollback()
        conn.commit()
    finally:
        db_return(conn)
def _migrate_db_with_retry(max_attempts=10, delay=6):
    """Call migrate_db() with retries so the app starts even if Postgres is temporarily down."""
    for attempt in range(1, max_attempts + 1):
        try:
            migrate_db()
            print(f"[DB] migrate_db OK (attempt {attempt})", flush=True)
            return
        except Exception as e:
            print(f"[DB] migrate_db failed (attempt {attempt}/{max_attempts}): {e}", flush=True)
            if attempt < max_attempts:
                time.sleep(delay)
    print("[DB] migrate_db could not complete — will retry later", flush=True)

_migrate_db_with_retry()


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


def get_user_telegram_chat_id(user_id):
    if not user_id:
        return ""
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT telegram_chat_id FROM users WHERE id=%s", (user_id,))
        row = cur.fetchone() or {}
        return row.get("telegram_chat_id") or ""
    finally:
        db_return(conn)

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


# ── Blockhash cache (2-second TTL) ────────────────────────────────────────────
_blockhash_cache = {"hash": None, "ts": 0.0}
_BLOCKHASH_TTL = 2  # seconds — blockhashes valid ~60s, 2s avoids stale without re-fetching

# ── Priority fee cache (10-second TTL) ───────────────────────────────────────
_priority_fee_cache = {"key": None, "fee": None, "ts": 0.0}
_PRIORITY_FEE_TTL = 10  # seconds — avoids hammering Helius during trading bursts

# ── Standalone wallet balance lookup (no bot needed) ──────────────────────────
_wallet_balance_cache = {}  # pubkey -> (balance, timestamp)
_WALLET_BAL_TTL = 30  # cache for 30 seconds

def get_wallet_balance_standalone(pubkey):
    """Fetch SOL balance for a wallet without a running bot. Cached 30s.
    Uses RPC racing (parallel requests) and 'processed' commitment for speed."""
    if not pubkey:
        print("[WARN] get_wallet_balance_standalone: empty pubkey", flush=True)
        return 0
    cached = _wallet_balance_cache.get(pubkey)
    if cached and (time.time() - cached[1]) < _WALLET_BAL_TTL:
        return cached[0]
    # Use "processed" for non-critical balance display (faster than "confirmed")
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [pubkey, {"commitment": "processed"}]}
    _fallbacks = [("dedicated", HELIUS_RPC)]
    if HELIUS_SHARED_RPC:
        _fallbacks.append(("shared", HELIUS_SHARED_RPC))
    if ANKR_RPC:
        _fallbacks.append(("ankr", ANKR_RPC))
    _fallbacks.append(("public", "https://solana-rpc.publicnode.com"))

    # RPC racing: fire all endpoints in parallel, take first success
    def _fetch_balance(label_url):
        label, rpc_url = label_url
        r = _http_session.post(rpc_url, json=payload, timeout=5)
        data = safe_json_response(r)
        if not data:
            raise ValueError(f"{label} returned empty")
        if "error" in data:
            raise ValueError(f"{label} error: {data.get('error')}")
        val = data.get("result", {}).get("value", 0)
        return label, round(val / 1e9, 4)

    try:
        with ThreadPoolExecutor(max_workers=len(_fallbacks)) as pool:
            futures = {pool.submit(_fetch_balance, fb): fb[0] for fb in _fallbacks}
            for fut in as_completed(futures, timeout=6):
                try:
                    label, balance = fut.result()
                    _wallet_balance_cache[pubkey] = (balance, time.time())
                    print(f"[BALANCE] {pubkey[:8]}... = {balance} SOL via {label} (raced)", flush=True)
                    return balance
                except Exception as e:
                    print(f"[WARN] Balance RPC {futures[fut]} failed: {e}", flush=True)
    except Exception as e:
        print(f"[ERROR] Balance race failed: {e}", flush=True)
    print(f"[ERROR] All RPC endpoints failed for {pubkey[:8]}...", flush=True)
    return 0


# ── Per-user bot instances ─────────────────────────────────────────────────────
user_bots    = {}
user_bots_lock = threading.Lock()
seen_tokens  = set()
seen_tokens_lock = threading.Lock()
market_feed  = deque(maxlen=100)   # live token stream for market board
shadow_market_queue = deque(maxlen=400)
shadow_market_lock = threading.Lock()
_backtest_jobs = {}
_backtest_jobs_lock = threading.Lock()
MODEL_DECISION_THRESHOLD = 60.0
MODEL_TRAIN_DAYS = 14
MODEL_CACHE_REFRESH_SEC = 300
EDGE_REPORT_AUTO_WINDOWS = (
    (7, 6 * 3600),
    (14, 12 * 3600),
    (30, 24 * 3600),
)
EDGE_GUARD_REFRESH_SEC = 300
_quant_model_cache = {"built_at": 0.0, "family": None, "days": MODEL_TRAIN_DAYS}
_quant_model_cache_lock = threading.Lock()
_edge_guard_cache = {"built_at": 0.0, "state": None}
_edge_guard_cache_lock = threading.Lock()
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

_largest_accounts_cache = {}   # mint -> (timestamp, accounts_list)
_LARGEST_ACCOUNTS_TTL = 120    # seconds

def _fetch_largest_accounts(mint):
    """Fetch getTokenLargestAccounts with TTL cache to avoid duplicate RPC calls."""
    now = time.time()
    cached = _largest_accounts_cache.get(mint)
    if cached and now - cached[0] < _LARGEST_ACCOUNTS_TTL:
        return cached[1]
    accounts = None
    try:
        result = rpc_call("getTokenLargestAccounts", [mint], timeout=8)
        if isinstance(result, dict):
            accounts = result.get("value", [])
    except Exception:
        pass
    if accounts is None:
        accounts = []
    _largest_accounts_cache[mint] = (now, accounts)
    # Prune old entries
    if len(_largest_accounts_cache) > 500:
        cutoff = now - _LARGEST_ACCOUNTS_TTL * 2
        for k in list(_largest_accounts_cache):
            if _largest_accounts_cache[k][0] < cutoff:
                del _largest_accounts_cache[k]
    return accounts

def check_holder_concentration(mint):
    """Returns True if top holders don't own >50% of supply."""
    try:
        accounts = _fetch_largest_accounts(mint)
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
        bh_r = _http_session.post(HELIUS_RPC, json={
            "jsonrpc":"2.0","id":1,"method":"getLatestBlockhash",
            "params":[{"commitment":"confirmed"}]
        }, timeout=8).json()
        blockhash = SolHash.from_string(bh_r["result"]["value"]["blockhash"])
        to_pk = Pubkey.from_string(to_address)
        ix    = transfer(TransferParams(from_pubkey=keypair.pubkey(), to_pubkey=to_pk, lamports=lamports))
        msg   = MessageV0.try_compile(keypair.pubkey(), [ix], [], blockhash)
        tx    = VersionedTransaction(msg, [keypair])
        enc   = base64.b64encode(bytes(tx)).decode()
        res   = _http_session.post(HELIUS_RPC, json={
            "jsonrpc":"2.0","id":1,"method":"sendTransaction",
            "params":[enc,{"encoding":"base64","skipPreflight":False,"preflightCommitment":"confirmed"}]
        }, timeout=15).json()
        return res.get("result")
    except Exception as e:
        print(f"send_sol error: {e}")
        return None

class BotInstance:
    def __init__(self, user_id, keypair, settings, run_mode, run_duration_min, profit_target_sol, preset_name="balanced", execution_control=None):
        self.user_id          = user_id
        self.keypair          = keypair
        self.wallet           = str(keypair.pubkey())
        self.settings         = strip_auto_relax_state(dict(settings))
        self.preset_name      = normalize_preset_name(preset_name)
        self.execution_control = normalize_execution_control(execution_control or {})
        self.execution_selection = {
            "selected_policy": self.execution_control.get("active_policy") or self.execution_control.get("policy_mode") or "rules",
            "selected_policy_label": "Rules",
            "execution_mode": self.execution_control.get("execution_mode") or "live",
            "selection_source": "manual",
            "candidate_reason": "startup",
            "model_threshold": float(self.execution_control.get("model_threshold") or MODEL_DECISION_THRESHOLD),
        }
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
        self._last_buy_ts       = 0.0   # epoch of last successful buy (for cooldown_min)
        self.loss_mints         = {}    # mint -> epoch when the bot last realized a loss on that mint
        self.auto_relax_level   = 0
        self.edge_guard_state   = None
        self.edge_guard_key     = ""
        self.edge_guard_checked_at = 0.0
        self.execution_control_checked_at = 0.0
        self.execution_control_key = ""
        # ── Concurrency lock ──────────────────────────────────────────────────
        # Per-bot lock that serialises the evaluate_signal → buy path so that
        # multiple scanner threads cannot cause duplicate buys, exceeded
        # position limits, or balance overdrafts on the same BotInstance.
        self._buy_lock = threading.Lock()
        
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

    def _execution_signature(self, selection):
        if not selection:
            return ""
        return "|".join([
            str(selection.get("execution_mode") or ""),
            str(selection.get("selected_policy") or ""),
            str(selection.get("selection_source") or ""),
            str(selection.get("candidate_reason") or ""),
            str(selection.get("lock_until") or ""),
        ])

    def _notify_execution_selection_change(self, selection):
        if not selection:
            return
        mode = str(selection.get("execution_mode") or "live").upper()
        policy = selection.get("selected_policy_label") or selection.get("selected_policy") or "Rules"
        source = selection.get("selection_source") or "manual"
        reason = selection.get("candidate_reason") or "updated"
        msg = f"🧭 Execution {mode} — {policy} ({source}). Reason: {reason.replace('_', ' ')}"
        self.log_msg(msg)
        try:
            chat_id = get_user_telegram_chat_id(self.user_id)
            if chat_id and selection.get("promotion_applied"):
                send_telegram(
                    chat_id,
                    (
                        f"🧭 <b>Execution policy updated</b>\n"
                        f"Mode: {mode}\n"
                        f"Policy: {policy}\n"
                        f"Reason: {reason.replace('_', ' ')}"
                    ),
                )
        except Exception as _e:
            print(f"[ERROR] execution policy telegram failed: {_e}", flush=True)

    def _edge_guard_signature(self, state):
        if not state:
            return ""
        return "|".join([
            str(state.get("status") or ""),
            str(state.get("source_report_id") or ""),
            str(state.get("source_generated_at") or ""),
            str(state.get("reason") or ""),
        ])

    def _notify_edge_guard_change(self, state):
        if not state:
            return
        status = str(state.get("status") or "normal").upper()
        action = state.get("action_label") or "Risk update"
        reason = state.get("reason") or "No reason provided."
        size_pct = round(float(state.get("size_multiplier") or 0) * 100)
        max_positions = state.get("max_positions_cap")
        report_window = state.get("focus_window_days") or 7
        model_edge = float(state.get("model_edge_pct") or 0)
        regime_edge = float(state.get("regime_edge_pct") or 0)
        msg = (
            f"🛡️ EDGE GUARD {status} — {action}. "
            f"{reason} Window {report_window}d, model edge {model_edge:+.1f}%, regime edge {regime_edge:+.1f}%, "
            f"size {size_pct}%"
        )
        if max_positions is not None:
            msg += f", max positions {max_positions}"
        self.log_msg(msg)
        try:
            chat_id = get_user_telegram_chat_id(self.user_id)
            if chat_id:
                send_telegram(
                    chat_id,
                    (
                        f"🛡️ <b>Edge Guard {status}</b>\n"
                        f"{action}\n"
                        f"{reason}\n"
                        f"Window: {report_window}d\n"
                        f"Model edge: {model_edge:+.1f}%\n"
                        f"Regime edge: {regime_edge:+.1f}%\n"
                        f"Size: {size_pct}%"
                    ),
                )
        except Exception as _e:
            print(f"[ERROR] edge guard telegram failed: {_e}", flush=True)

    def refresh_edge_guard(self, force=False):
        now = time.time()
        if not force and self.edge_guard_state and now - self.edge_guard_checked_at < EDGE_GUARD_REFRESH_SEC:
            return self.edge_guard_state
        state = get_quant_edge_guard_state(refresh_sec=EDGE_GUARD_REFRESH_SEC, force=force)
        self.edge_guard_checked_at = now
        signature = self._edge_guard_signature(state)
        if signature != self.edge_guard_key:
            self.edge_guard_state = state
            self.edge_guard_key = signature
            self._notify_edge_guard_change(state)
        else:
            self.edge_guard_state = state
        return self.edge_guard_state

    def refresh_execution_control(self, force=False):
        now = time.time()
        if not force and self.execution_selection and now - self.execution_control_checked_at < EDGE_GUARD_REFRESH_SEC:
            return self.execution_selection
        guard_state = self.refresh_edge_guard(force=force)
        selection_bundle = resolve_user_execution_control(self.user_id, guard_state=guard_state)
        self.execution_control_checked_at = now
        self.execution_control = normalize_execution_control(selection_bundle.get("control") or self.execution_control)
        selection = {
            key: value
            for key, value in selection_bundle.items()
            if key != "control"
        }
        signature = self._execution_signature(selection)
        if signature != self.execution_control_key:
            self.execution_control_key = signature
            self.execution_selection = selection
            self._notify_execution_selection_change(selection)
        else:
            self.execution_selection = selection
        return self.execution_selection

    def resolve_live_execution_decision(self, mint, name, snapshot, flow_snapshot):
        selection = self.refresh_execution_control()
        selected_policy = normalize_policy_mode(selection.get("selected_policy"))
        threshold = max(40.0, min(float(selection.get("model_threshold") or MODEL_DECISION_THRESHOLD), 90.0))
        active_regime = (_current_regime_context(include_flow_snapshot=flow_snapshot, window_hours=48) or {}).get("regime") or "neutral"
        trained = False
        model_score = None
        driver_rows = []
        fallback_reason = ""
        effective_policy = selected_policy
        allow_trade = True

        if selected_policy in {"model_global", "model_regime_auto"}:
            bundle = get_cached_quant_model_bundle(days=MODEL_TRAIN_DAYS)
            family = bundle.get("family") or {}
            mode = "global" if selected_policy == "model_global" else "auto"
            scored = score_feature_snapshot_with_family(snapshot, family, active_regime, mode=mode)
            trained = bool(scored.get("trained"))
            model_score = float(scored.get("model_score") or 0)
            driver_rows = scored.get("top_drivers") or []
            fallback_reason = ((scored.get("selection") or {}).get("fallback_reason") or "")
            if trained:
                allow_trade = model_score >= threshold
            else:
                effective_policy = "rules"
                allow_trade = True
                fallback_reason = fallback_reason or "model_untrained_fallback_to_rules"

        return {
            "execution_mode": normalize_execution_mode(selection.get("execution_mode")),
            "policy_mode": selection.get("policy_mode") or "rules",
            "selected_policy": selected_policy,
            "effective_policy": effective_policy,
            "selected_policy_label": selection.get("selected_policy_label") or selected_policy.replace("_", " "),
            "active_regime": active_regime,
            "model_threshold": threshold,
            "model_score": model_score,
            "trained": trained,
            "top_drivers": driver_rows,
            "fallback_reason": fallback_reason,
            "allow_trade": allow_trade,
            "selection_source": selection.get("selection_source") or "manual",
            "candidate_reason": selection.get("candidate_reason") or "manual_selection",
        }

    def entry_settings(self):
        guard_state = self.refresh_edge_guard()
        return apply_edge_guard_to_settings(self.settings, guard_state)

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
        # Only consider actual Solana RPC sources — exclude DexScreener/third-party APIs
        rpc_only = [s for s in rpc_stats.get("by_source", []) if s["source"] not in ("dexscreener",)]
        rpc_total = sum(s["total"] for s in rpc_only)
        rpc_ok = sum(s["ok"] for s in rpc_only)
        rpc_fail_rate = round((1 - (rpc_ok / rpc_total)) * 100, 1) if rpc_total else 0.0
        if rpc_total >= 20 and (rpc_fail_rate >= 80 or rpc_stats["p95_ms"] >= 4500):
            return f"RPC degraded — fail {rpc_fail_rate:.0f}% / p95 {rpc_stats['p95_ms']:.0f}ms"
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
        outcome = "PASS" if passed else "FAIL"
        print(f"[FILTER U{self.user_id}] {outcome} {name} | {reason}", flush=True)
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
        # Profit target no longer auto-stops — presets handle risk management
        # if self.run_mode == "profit" and self.profit_target > 0:
        #     if self.stats["total_pnl_sol"] >= self.profit_target:
        #         self.log_msg(f"Profit target reached ({self.profit_target:.4f} SOL) — stopping")
        #         return True
        return False

    def refresh_balance(self):
        # Use "processed" for non-critical balance display (faster than "confirmed")
        payload = {"jsonrpc":"2.0","id":1,"method":"getBalance","params":[self.wallet, {"commitment":"processed"}]}
        _fallbacks = [("dedicated", HELIUS_RPC)]
        if HELIUS_SHARED_RPC:
            _fallbacks.append(("shared", HELIUS_SHARED_RPC))
        if ANKR_RPC:
            _fallbacks.append(("ankr", ANKR_RPC))
        _fallbacks.append(("public", "https://solana-rpc.publicnode.com"))

        # RPC racing: fire all endpoints in parallel, take first success
        bot_ref = self  # capture for closure

        def _fetch_balance(label_url):
            label, rpc_url = label_url
            started = time.perf_counter()
            r = _http_session.post(rpc_url, json=payload, timeout=5)
            latency = round((time.perf_counter() - started) * 1000)
            data = safe_json_response(r)
            if not data:
                record_rpc_health(label, False, latency, "getBalance")
                raise ValueError(f"{label} returned empty")
            if "error" in data:
                record_rpc_health(label, False, latency, "getBalance")
                raise ValueError(f"{label} error: {data['error']}")
            val = data.get("result", {}).get("value", 0)
            record_rpc_health(label, True, latency, "getBalance")
            return label, val / 1e9

        try:
            with ThreadPoolExecutor(max_workers=len(_fallbacks)) as pool:
                futures = {pool.submit(_fetch_balance, fb): fb[0] for fb in _fallbacks}
                for fut in as_completed(futures, timeout=6):
                    try:
                        label, balance = fut.result()
                        self.sol_balance = balance
                        if self.sol_balance > self.peak_balance:
                            self.peak_balance = self.sol_balance
                        return
                    except Exception as _e:
                        print(f"[ERROR] Balance fetch failed ({futures[fut]}): {_e}", flush=True)
        except Exception as _e:
            print(f"[ERROR] Balance race failed: {_e}", flush=True)
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
        api_key = get_helius_api_key()
        qs = f"?api-key={api_key}" if api_key else ""
        return [
            f"https://slc-sender.helius-rpc.com/fast{qs}",  # Salt Lake City
            f"https://ewr-sender.helius-rpc.com/fast{qs}",  # Newark
            f"https://lon-sender.helius-rpc.com/fast{qs}",  # London
            f"https://fra-sender.helius-rpc.com/fast{qs}",  # Frankfurt
            f"https://ams-sender.helius-rpc.com/fast{qs}",  # Amsterdam
            f"https://sg-sender.helius-rpc.com/fast{qs}",   # Singapore
            f"https://tyo-sender.helius-rpc.com/fast{qs}",  # Tokyo
        ]

    def _encode_signed_transaction(self, signed_tx):
        return base64.b64encode(bytes(signed_tx)).decode()

    def _latest_blockhash(self):
        # 2-second cache: blockhashes valid ~60s, avoid re-fetching on every call
        now = time.time()
        if _blockhash_cache["hash"] and (now - _blockhash_cache["ts"]) < _BLOCKHASH_TTL:
            return _blockhash_cache["hash"]
        result = rpc_call("getLatestBlockhash", [{"commitment": "processed"}], timeout=6) or {}
        value = result.get("value") if isinstance(result, dict) else {}
        bh = (value or {}).get("blockhash")
        if bh:
            _blockhash_cache["hash"] = bh
            _blockhash_cache["ts"] = now
        return bh

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
            r = _http_session.post(url, json={
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
            r   = _http_session.post(HELIUS_RPC, json={
                "jsonrpc":"2.0","id":1,"method":"sendTransaction",
                "params":[enc,{"encoding":"base64","skipPreflight":True,"maxRetries":3}]
            }, timeout=8)
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

    def _jupiter_swap_single(self, url, quote):
        """Hit a single Jupiter swap endpoint; return swapTransaction or None.
        Uses Helius priority fee estimate when available, falls back to user setting."""
        try:
            # Wire in dynamic priority fee from Helius (cached 10s)
            user_max = int(self.settings.get("priority_fee", 1_000_000))
            helius_fee = None
            try:
                # Use the swap's input/output mints as account keys for fee estimation
                acct_keys = [self.wallet]
                input_mint = quote.get("inputMint")
                output_mint = quote.get("outputMint")
                if input_mint:
                    acct_keys.append(input_mint)
                if output_mint:
                    acct_keys.append(output_mint)
                helius_fee = helius_get_priority_fee(account_keys=acct_keys)
            except Exception:
                pass
            # Use Helius estimate but cap at user's max setting for cost control
            if helius_fee and helius_fee > 0:
                optimal_fee = min(helius_fee, user_max)
                self.log_msg(f"Priority fee: Helius={helius_fee} µL, cap={user_max}, using={optimal_fee}")
            else:
                optimal_fee = user_max
            r = requests.post(url, json={
                "quoteResponse":quote,"userPublicKey":self.wallet,"wrapAndUnwrapSol":True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": {
                    "priorityLevelWithMaxLamports": {
                        "maxLamports": optimal_fee,
                        "priorityLevel": "veryHigh"
                    }
                },
            }, timeout=12).json()
            if r.get("swapTransaction"):
                return r["swapTransaction"]
        except Exception as e:
            self.log_msg(f"Jupiter swap failed ({url.split('/')[2]}): {e}")
        return None

    def jupiter_swap(self, quote):
        endpoints = ["https://lite-api.jup.ag/swap/v1/swap", "https://quote-api.jup.ag/v6/swap"]
        # Hit both endpoints in parallel, take first success
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(self._jupiter_swap_single, u, quote): u for u in endpoints}
            for fut in as_completed(futures, timeout=15):
                try:
                    result = fut.result()
                    if result:
                        return result
                except Exception as e:
                    self.log_msg(f"Jupiter swap failed ({futures[fut].split('/')[2]}): {e}")
        return None

    def get_token_balance(self, mint):
        r = _http_session.post(HELIUS_RPC, json={
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
            r = _http_session.post(HELIUS_RPC, json={
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

    def pre_buy_safety_check(self, mint, name="", dev_wallet=None, age_min=0, liq=0):
        """Run token-specific blocking checks in one place and return a structured result."""
        s = self.settings
        checks = {}
        timings_ms = {}
        warnings = []
        risk_score = 0

        future_map = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            if dev_wallet:
                future_map["blacklist"] = pool.submit(check_dev_blacklist, dev_wallet)
            if s.get("anti_rug") and age_min >= 30:
                future_map["authority"] = pool.submit(self.is_safe_token, mint)
            elif s.get("anti_rug"):
                checks["authority"] = None
                warnings.append("authority check skipped for token under 30m")
            if s.get("check_holders") and age_min >= 30:
                future_map["holders"] = pool.submit(check_holder_concentration, mint)
            elif s.get("check_holders"):
                checks["holders"] = None
                warnings.append("holder concentration check skipped for token under 30m")
            else:
                checks["holders"] = None
            future_map["honeypot"] = pool.submit(self.check_honeypot, mint, age_min)

            started_at = {key: time.perf_counter() for key in future_map}
            for key, fut in future_map.items():
                try:
                    checks[key] = fut.result()
                except Exception as e:
                    checks[key] = None
                    warnings.append(f"{key} check failed: {e}")
                finally:
                    timings_ms[key] = round((time.perf_counter() - started_at[key]) * 1000)

        if checks.get("blacklist") is False:
            return {
                "safe": False,
                "reason": f"Dev blacklisted ({dev_wallet[:8]}...)",
                "warnings": warnings,
                "risk_score": 100,
                "timings_ms": timings_ms,
                "checks": checks,
            }
        if checks.get("authority") is False:
            return {
                "safe": False,
                "reason": "RUG RISK — mint/freeze auth active",
                "warnings": warnings,
                "risk_score": 85,
                "timings_ms": timings_ms,
                "checks": checks,
            }
        if checks.get("honeypot") is False:
            return {
                "safe": False,
                "reason": "HONEYPOT — no sell route found via Jupiter",
                "warnings": warnings,
                "risk_score": 95,
                "timings_ms": timings_ms,
                "checks": checks,
            }
        if checks.get("holders") is False:
            return {
                "safe": False,
                "reason": "Top 5 holders own >50% of supply",
                "warnings": warnings,
                "risk_score": 70,
                "timings_ms": timings_ms,
                "checks": checks,
            }

        if checks.get("blacklist") is None:
            warnings.append("dev blacklist check unavailable")
        if checks.get("authority") is None and s.get("anti_rug") and age_min >= 30:
            warnings.append("authority check unavailable")
        if checks.get("holders") is None and s.get("check_holders") and age_min >= 30:
            warnings.append("holder concentration check unavailable")
        if checks.get("honeypot") is None:
            warnings.append("sell-route probe unavailable")

        if not checks.get("honeypot", True):
            risk_score += 40
        if checks.get("authority") is False:
            risk_score += 30
        if checks.get("holders") is False:
            risk_score += 20

        return {
            "safe": True,
            "reason": "",
            "warnings": warnings,
            "risk_score": min(100, risk_score),
            "timings_ms": timings_ms,
            "checks": checks,
        }

    def buy(self, mint, name, price, liq=0, dev_wallet=None, age_min=0, decision_context=None):
        with self._buy_lock:
            self._buy_inner(mint, name, price, liq=liq, dev_wallet=dev_wallet, age_min=age_min, decision_context=decision_context)

    def _force_buy(self, mint, name, price, liq=0):
        """Manual/force buy — bypasses edge guard, drawdown, cooldown, rate limit, safety checks."""
        with self._buy_lock:
            s = self.settings  # raw settings, no edge guard
            trade_sol = round(float(s.get("max_buy_sol") or 0.04), 4)
            if trade_sol <= 0:
                trade_sol = 0.04
            if self.sol_balance < trade_sol + 0.01:
                self.log_msg(f"FORCE-BUY SKIP {name} — Low balance ({self.sol_balance:.4f} SOL)")
                return
            if mint in self.positions:
                self.log_msg(f"FORCE-BUY SKIP {name} — already in position")
                return
            self.log_msg(f"FORCE-BUY {name} | size={trade_sol:.4f} SOL | bypassing filters")
            slippage = dynamic_slippage_bps(liq)
            quote = self.jupiter_quote(SOL_MINT, mint, int(trade_sol * 1e9), slippage)
            if not quote:
                self.log_msg(f"FORCE-BUY FAIL {name} — no Jupiter quote")
                return
            swap_tx = self.jupiter_swap(quote)
            if not swap_tx:
                self.log_msg(f"FORCE-BUY FAIL {name} — swap build failed")
                return
            send_result = self.sign_and_send(swap_tx)
            sig = send_result.get("sig")
            if sig:
                real_price = price
                expected_out = int(quote.get("outAmount", 0) or 0)
                try:
                    time.sleep(0.3)
                    tokens_received = self.get_token_balance(mint)
                    if tokens_received > 0 and expected_out > 0:
                        slip_ratio = expected_out / tokens_received if tokens_received > 0 else 1
                        real_price = price * slip_ratio
                except Exception:
                    pass
                self.positions[mint] = {
                    "name": name, "entry_price": real_price, "peak_price": real_price,
                    "timestamp": time.time(), "tp1_hit": False, "entry_sol": trade_sol,
                    "dev_wallet": None, "surge_hold_active": False,
                    "surge_peak_price": real_price,
                    "buy_reason": "manual force buy", "source": "manual",
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
                                   (self.user_id, mint, name, real_price, real_price, trade_sol, 0, None))
                        _conn.commit()
                    finally:
                        db_return(_conn)
                except Exception as e:
                    print(f"[FORCE-BUY] DB error: {e}", flush=True)
                self.sol_balance -= trade_sol
                self._last_buy_ts = time.time()
                self.log_msg(f"BUY {name} (FORCE) | {trade_sol:.4f} SOL @ ${real_price:.8f}")
                send_telegram(self.telegram_chat_id, f"<b>BUY</b> {name} (force) | {trade_sol:.4f} SOL")
            else:
                self.log_msg(f"FORCE-BUY FAIL {name} — transaction send failed")

    def _buy_inner(self, mint, name, price, liq=0, dev_wallet=None, age_min=0, decision_context=None):
        s = self.entry_settings()
        edge_guard = s.get("_edge_guard") or {}
        if not edge_guard.get("allow_new_entries", True):
            reason = edge_guard.get("reason") or "Edge guard blocked new entries"
            self.log_filter(name, mint, False, reason)
            self.log_msg(f"SKIP {name} — {reason}")
            return
        trade_sol = round(float(s.get("max_buy_sol") or 0), 4)
        # ── Risk-per-trade cap (risk_per_trade_pct setting) ───────────────────
        risk_pct = float(s.get("risk_per_trade_pct") or 0)
        if risk_pct > 0 and self.sol_balance > 0:
            risk_cap = round(self.sol_balance * risk_pct / 100.0, 4)
            if trade_sol > risk_cap:
                self.log_msg(f"Risk cap: {trade_sol:.4f} SOL -> {risk_cap:.4f} SOL ({risk_pct:.1f}% of {self.sol_balance:.4f})")
                trade_sol = risk_cap
        if trade_sol <= 0:
            reason = edge_guard.get("reason") or "Effective trade size is zero"
            self.log_filter(name, mint, False, reason)
            self.log_msg(f"SKIP {name} — {reason}")
            return
        print(f"[BUY U{self.user_id}] Attempting {name} | bal={self.sol_balance:.4f} need={trade_sol+0.01:.4f}", flush=True)

        # ── Circuit breakers ─────────────────────────────────────────────────
        cb = self.check_circuit_breakers()
        if cb:
            is_rpc_issue = "RPC degraded" in cb
            if is_rpc_issue:
                self.log_msg(f"⚠️ RPC degraded — skipping trade for {name}")
                return
            self.log_msg(f"⛔ CIRCUIT BREAKER — {cb} — halting bot")
            self.running = False
            return
        # ── Rate limiting ────────────────────────────────────────────────────
        rl = self.check_rate_limit(name, mint)
        if rl:
            self.log_filter(name, mint, False, rl)
            self.log_msg(f"SKIP {name} — {rl}")
            return
        # ── Buy cooldown (cooldown_min setting) ──────────────────────────────
        cooldown_min_setting = float(s.get("cooldown_min") or 0)
        if cooldown_min_setting > 0 and self._last_buy_ts > 0:
            elapsed_min = (time.time() - self._last_buy_ts) / 60.0
            if elapsed_min < cooldown_min_setting:
                # Check if coin is growing exponentially (>100% gain) — bypass cooldown
                price_change = decision_context.get("change", 0) if decision_context else 0
                exponential_growth_threshold = 100.0  # 100% gain
                if price_change >= exponential_growth_threshold:
                    self.log_msg(f"🚀 EXPONENTIAL GROWTH DETECTED ({price_change:+.0f}%) — bypassing cooldown for {name}")
                else:
                    remaining = round(cooldown_min_setting - elapsed_min, 1)
                    reason = f"Buy cooldown active ({remaining:.1f}m remaining of {cooldown_min_setting:.0f}m)"
                    self.log_filter(name, mint, False, reason)
                    self.log_msg(f"SKIP {name} — {reason}")
                    return
        # ── Consolidated token safety path ───────────────────────────────────
        safety = self.pre_buy_safety_check(mint, name=name, dev_wallet=dev_wallet, age_min=age_min, liq=liq)
        if not safety.get("safe"):
            reason = safety.get("reason") or "Pre-buy safety check failed"
            self.log_filter(name, mint, False, reason)
            self.log_msg(f"SKIP {name} — {reason}")
            return
        # Drawdown limit
        if s.get("drawdown_limit_sol",0) > 0 and self.session_drawdown >= s["drawdown_limit_sol"]:
            reason = f"Drawdown limit reached ({self.session_drawdown:.3f} SOL)"
            self.log_filter(name, mint, False, reason)
            self.log_msg(f"SKIP {name} — {reason}")
            return
        # Correlated position limit
        if len(self.positions) >= s.get("max_correlated", 5):
            # Check if coin is growing exponentially (>100% gain) — bypass position limit
            price_change = decision_context.get("change", 0) if decision_context else 0
            exponential_growth_threshold = 100.0  # 100% gain
            if price_change >= exponential_growth_threshold:
                self.log_msg(f"🚀 EXPONENTIAL GROWTH DETECTED ({price_change:+.0f}%) — bypassing position limit for {name}")
            else:
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
        # ── Parallel: live re-validation + Jupiter quote simultaneously ──
        slippage = dynamic_slippage_bps(liq)
        self.log_msg(f"Quoting {name} | size={trade_sol:.4f} SOL | slippage={slippage}bps ...")

        def _revalidate():
            """Returns (ok, updated_liq) — ok=False means reject."""
            try:
                _dex = dex_get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=3)
                _pairs = safe_json_response(_dex)
                if _pairs and isinstance(_pairs, dict):
                    _p = next((p for p in (_pairs.get("pairs") or []) if p.get("chainId") == "solana"), None)
                    if _p:
                        live_mc  = float(_p.get("marketCap") or 0)
                        live_liq = float((_p.get("liquidity") or {}).get("usd") or 0)
                        live_chg = float((_p.get("priceChange") or {}).get("h1") or 0)
                        min_mc   = float(s.get("min_mc", 0) or 0)
                        max_mc   = float(s.get("max_mc", 999999) or 999999)
                        _min_liq = float(s.get("min_liq", 0) or 0)
                        if live_mc > 0 and not (min_mc <= live_mc <= max_mc):
                            return False, f"Live MC ${live_mc:,.0f} outside [{min_mc:,}\u2013{max_mc:,}] (was valid at scan)", liq
                        if _min_liq > 0 and live_liq > 0 and live_liq < _min_liq:
                            return False, f"Live liquidity ${live_liq:,.0f} < min ${_min_liq:,.0f} (drained since scan)", liq
                        if live_chg <= -30:
                            return False, f"Live 1h change {live_chg:+.0f}% \u2014 dumping since scan", liq
                        return True, None, (live_liq or liq)
            except Exception as _e:
                print(f"[WARN] Live re-check failed for {name}: {_e}", flush=True)
            return True, None, liq

        quote_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=2) as _buy_pool:
            reval_future = _buy_pool.submit(_revalidate)
            quote_future = _buy_pool.submit(self.jupiter_quote, SOL_MINT, mint, int(trade_sol*1e9), slippage)
            # Check revalidation result
            reval_ok, reval_reason, liq = reval_future.result(timeout=6)
            if not reval_ok:
                self.log_filter(name, mint, False, reval_reason)
                self.log_msg(f"SKIP {name} \u2014 {reval_reason}")
                quote_future.cancel()
                return
            quote = quote_future.result(timeout=12)
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
                time.sleep(0.3)  # minimal wait for tx to land
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
                "buy_reason": (decision_context or {}).get("buy_reason", ""),
                "source": (decision_context or {}).get("source_tag", "scanner"),
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
            self._last_buy_ts        = time.time()  # for cooldown_min
            self.consecutive_losses  = 0   # reset on successful buy
            policy_note = ""
            if decision_context:
                policy_note = f" | policy={decision_context.get('effective_policy') or decision_context.get('selected_policy')}"
                if decision_context.get("model_score") is not None:
                    policy_note += f" score={float(decision_context.get('model_score') or 0):.1f}"
            self.log_filter(name, mint, True, f"BUY @ ${real_price:.8f} | slip={slippage}bps{policy_note}", score=0)
            self.log_msg(f"BUY {name} @ ${real_price:.8f} | size={trade_sol:.4f} SOL | slip={slippage}bps{policy_note} | solscan.io/tx/{sig}")
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
                if pct >= 1.0:
                    # Full exit — count combined P&L (partial TP1 + final sell)
                    combined_pnl = pnl_sol + pos.get("partial_pnl_sol", 0)
                    if combined_pnl >= 0:
                        self.stats["wins"] += 1
                        self.consecutive_losses = 0
                    else:
                        self.stats["losses"] += 1
                        self.consecutive_losses += 1
                        self.loss_mints[mint] = time.time()
                else:
                    # Partial sell — accumulate P&L for final win/loss determination
                    self.positions[mint]["partial_pnl_sol"] = pos.get("partial_pnl_sol", 0) + pnl_sol
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
        # Collect peak updates for a single batch DB write
        _peak_updates = []
        for mint in mints:
            pos = self.positions.get(mint)
            if not pos:
                continue
            cur = prices.get(mint)
            if not cur or not pos["entry_price"]:
                continue
            if cur > pos["peak_price"]:
                self.positions[mint]["peak_price"] = cur
                _peak_updates.append((cur, self.user_id, mint))
            ratio      = cur / pos["entry_price"]
            peak_ratio = pos["peak_price"] / pos["entry_price"]
            age_sec    = time.time() - pos["timestamp"]
            age_min    = (time.time() - pos["timestamp"]) / 60

            # ── Progressive trailing stop — tightens as gains increase ──
            base_trail = s["trail_pct"]
            # Moonshot detection: if coin surges 50%+, widen trail to let it ride
            moonshot_trigger = float(s.get("moonshot_trigger", 0) or 0)
            moonshot_trail = float(s.get("moonshot_trail_pct", 0) or 0)
            moonshot_active = moonshot_trigger > 0 and ratio >= moonshot_trigger

            if s.get("peak_plateau_mode"):
                # Progressive trailing that tightens at higher multiples
                if peak_ratio >= 50:
                    effective_trail = min(base_trail, 0.10)   # 10% trail above 50x — lock in mega gains
                elif peak_ratio >= 20:
                    effective_trail = min(base_trail, 0.12)   # 12% trail above 20x
                elif peak_ratio >= 10:
                    effective_trail = min(base_trail, 0.15)   # 15% trail above 10x
                elif peak_ratio >= 5:
                    effective_trail = min(base_trail, 0.20)   # 20% trail above 5x
                elif peak_ratio >= 3:
                    effective_trail = min(base_trail, 0.25)   # 25% trail above 3x
                elif moonshot_active and moonshot_trail > 0:
                    effective_trail = moonshot_trail           # 50%+ surge — wide trail, let it moon
                else:
                    effective_trail = base_trail               # Default trail early
            else:
                if moonshot_active and moonshot_trail > 0:
                    effective_trail = moonshot_trail
                else:
                    effective_trail = base_trail
            trail_line = pos["peak_price"] * (1 - effective_trail)

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
            elif s.get("peak_plateau_mode"):
                # ── Peak Plateau Mode: ride to the top, let trailing stop decide ──
                tp1_sell_pct = s.get("tp1_sell_pct", 0.50)
                if not pos["tp1_hit"] and ratio >= s["tp1_mult"]:
                    # Skim at TP1 to lock in profit
                    self.sell(mint, tp1_sell_pct, f"SKIM {ratio:.2f}x ({int(tp1_sell_pct*100)}%)")
                elif (pos["tp1_hit"] or peak_ratio >= 1.5) and cur < trail_line:
                    # Progressive trailing stop — rides to peak plateau
                    self.sell(mint, 1.0, f"PEAK EXIT {ratio:.2f}x (peak {peak_ratio:.1f}x, trail {effective_trail*100:.0f}%)")
                elif age_min >= s["time_stop_min"] and ratio < 1.05:
                    # Time stop: flat or losing after time limit
                    self.sell(mint, 1.0, f"TIME {age_min:.0f}m")
                elif age_min >= s["time_stop_min"] and ratio >= 1.05 and cur < trail_line:
                    # In profit past time limit but dropping — trail out with profit
                    self.sell(mint, 1.0, f"TIME-TRAIL {ratio:.2f}x ({age_min:.0f}m)")
            else:
                # ── Standard TP mode ──
                if not pos["tp1_hit"] and ratio >= s["tp1_mult"]:
                    self.sell(mint, 0.5, f"TP1 {ratio:.2f}x")
                elif pos["tp1_hit"] and ratio >= s["tp2_mult"]:
                    self.sell(mint, 1.0, f"TP2 {ratio:.2f}x")
                elif (pos["tp1_hit"] or peak_ratio >= 1.3) and cur < trail_line:
                    self.sell(mint, 1.0, f"TRAIL {ratio:.2f}x")
                elif age_min >= s["time_stop_min"] and ratio < 1.05:
                    self.sell(mint, 1.0, f"TIME {age_min:.0f}m")
                elif age_min >= s["time_stop_min"] and ratio >= 1.05 and cur < trail_line:
                    self.sell(mint, 1.0, f"TIME-TRAIL {ratio:.2f}x ({age_min:.0f}m)")
        # Batch-write all peak_price updates in one DB round-trip
        if _peak_updates:
            try:
                _conn = db()
                try:
                    _cur = _conn.cursor()
                    psycopg2.extras.execute_batch(
                        _cur,
                        "UPDATE open_positions SET peak_price=%s WHERE user_id=%s AND mint=%s",
                        _peak_updates,
                    )
                    _conn.commit()
                finally:
                    db_return(_conn)
            except Exception as _e:
                print(f"[ERROR] batch peak update: {_e}", flush=True)

    def evaluate_signal(self, mint, name, price, mc, vol, liq, age_min, change, source=None):
        if mint in self.positions:
            return
        s = self.settings
        min_liq = float(s.get("min_liq", 0) or 0)
        pumpfun_liq_bypass = "pumpfun" in str(source or "").lower()
        liq_filter_on = min_liq > 0 and not pumpfun_liq_bypass
        min_mc  = s.get("min_mc", 0)
        max_mc  = s.get("max_mc", 999999)
        max_age = s.get("max_age_min", 999)
        min_vol = s.get("min_vol", 0)
        min_score = s.get("min_score", 0)
        min_green_lights = int(s.get("min_green_lights", 0))
        min_holder_growth_pct = float(s.get("min_holder_growth_pct", 5))
        min_narrative_score = int(s.get("min_narrative_score", 2))
        min_volume_spike_mult = float(s.get("min_volume_spike_mult", 1.2))
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
                {"name": "Liquidity", "passed": (not liq_filter_on) or liq >= min_liq, "value": f"${liq:,.0f}", "threshold": "pumpfun bypass" if pumpfun_liq_bypass else ("off" if not liq_filter_on else f"\u2265 ${min_liq:,.0f}")},
                {"name": "Token Age", "passed": age_min <= max_age, "value": f"{age_min:.0f}m", "threshold": f"\u2264 {max_age}m"},
                {"name": "Price Change", "passed": 0 < change <= max_hot_change, "value": f"{change:+.0f}%", "threshold": f"> 0% and \u2264 {max_hot_change:.0f}%"},
                {"name": "Volume", "passed": vol >= min_vol, "value": f"${vol:,.0f}", "threshold": f"\u2265 ${min_vol:,}"},
                {"name": "AI Score", "passed": score_total >= min_score, "value": f"{score_total}/100", "threshold": f"\u2265 {min_score}"},
            ],
            "ts": time.strftime("%H:%M:%S"), "timestamp": time.time(),
        }
        liq_text = "pumpfun bypass" if pumpfun_liq_bypass else (f"min {min_liq:,.0f}" if liq_filter_on else "filter off")
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
        snapshot_info = {
            "mint": mint,
            "name": name,
            "price": price,
            "mc": mc,
            "vol": vol,
            "liq": liq,
            "age_min": age_min,
            "change": change,
            "score": score_total,
            "momentum": _sinfo.get("momentum"),
            "green_lights": checklist["green_lights"],
            "narrative_score": intel.get("narrative_score"),
            "deployer_score": intel.get("deployer_score"),
        }
        feature_snapshot = build_feature_snapshot(snapshot_info, intel)
        flow_snapshot = build_flow_snapshot(snapshot_info, intel)
        execution_decision = self.resolve_live_execution_decision(mint, name, feature_snapshot, flow_snapshot)
        if not execution_decision.get("allow_trade", True):
            score_text = execution_decision.get("model_score")
            if score_text is None:
                score_text = 0.0
            sig_entry["reason"] = (
                f"{execution_decision.get('selected_policy_label') or 'Model'} "
                f"blocked entry ({float(score_text):.1f} < {float(execution_decision.get('model_threshold') or MODEL_DECISION_THRESHOLD):.1f})"
            )
            self.log_signal_entry(sig_entry)
            self.log_filter(name, mint, False, sig_entry["reason"])
            self.log_msg(f"SKIP {name} — {sig_entry['reason']}")
            return
        sig_entry["passed"] = True
        sig_entry["execution"] = execution_decision
        execution_prefix = "Paper" if execution_decision.get("execution_mode") == "paper" else "Live"
        policy_label = execution_decision.get("selected_policy_label") or "Rules"
        sig_entry["reason"] = f"Passed all filters · {execution_prefix} · {policy_label}"
        self.log_signal_entry(sig_entry)
        self.log_msg(f"🟢 SIGNAL {name} | MC:${mc:,.0f} Liq:${liq:,.0f} Age:{age_min:.0f}m Chg:{change:+.0f}% Score:{score_total}")
        self.log_filter(name, mint, True, "Signal passed all filters")
        # ── Build human-readable buy reason for dashboard cards ────────────────
        _br = []
        if change >= 30: _br.append(f"+{change:.0f}% momentum")
        if age_min <= 5: _br.append("ultra-fresh")
        elif age_min <= 15: _br.append("early entry")
        if mc < 10000: _br.append("micro-cap")
        elif mc < 50000: _br.append("small-cap")
        if score_total >= 70: _br.append(f"score {score_total}")
        if int(intel.get("narrative_score") or 0) >= 50: _br.append("strong narrative")
        _src = source or "scanner"
        if _src == "listing": _br.append("new listing")
        elif _src not in ("scanner", "new_pairs", "helius-ws"): _br.append(_src)
        execution_decision["buy_reason"] = ", ".join(_br) if _br else "rules passed"
        execution_decision["source_tag"] = _src
        if execution_decision.get("execution_mode") == "paper":
            detail = f"{policy_label} accepted"
            if execution_decision.get("model_score") is not None:
                detail += f" @ {float(execution_decision.get('model_score') or 0):.1f}"
            self.log_msg(f"PAPER BUY {name} — {detail}")
            self.log_filter(name, mint, True, f"PAPER {detail}")
            return
        self.buy(
            mint,
            name,
            price,
            liq=liq,
            dev_wallet=intel.get("deployer_wallet"),
            age_min=age_min,
            decision_context=execution_decision,
        )

    def cashout_all(self):
        self.log_msg("CASHOUT ALL — selling all positions")
        for mint in list(self.positions.keys()):
            self.sell(mint, 1.0, "CASHOUT")

    def run(self):
        try:
            self._run_inner()
        except Exception as e:
            # Catch-all: if anything escapes the main loop, log it so the
            # watchdog can detect the dead thread and auto-restart.
            self.log_msg(f"🔴 FATAL run() crash: {e}")
            print(f"[BOT-FATAL] {getattr(self, 'user_id', '?')}: {e}", flush=True)
            import traceback; traceback.print_exc()

    def _run_inner(self):
        # Retry balance fetch on startup — RPC may be degraded
        for attempt in range(5):
            self.refresh_balance()
            if self.sol_balance > 0:
                break
            self.log_msg(f"⏳ Balance read 0.0 SOL, retrying ({attempt+1}/5)...")
            time.sleep(2)
        self.start_balance = self.sol_balance
        self.peak_balance  = self.sol_balance
        self.log_msg(f"✅ Bot started | Wallet: {self.wallet[:8]}...{self.wallet[-4:]} | Balance: {self.sol_balance:.4f} SOL")
        self.log_msg(f"📋 Preset: {self.preset_name} | Mode: {self.run_mode} | Max positions: {self.settings.get('max_correlated', 3)}")
        consecutive_errors = 0
        last_health_check = 0
        last_balance_refresh = time.time()
        while self.running:
            try:
                now = time.time()
                self.refresh_edge_guard()
                self.refresh_execution_control()
                self.maybe_relax_guards()
                if self.should_stop():
                    self.log_msg("🛑 Stop condition met — closing all positions")
                    self.cashout_all()
                    self.running = False
                    self.record_perf_fee()
                    break
                if self.positions:
                    self.check_positions()
                # Periodic health check every 5 minutes
                if now - last_health_check > 300:
                    last_health_check = now
                    alert = self.execution_health_alert()
                    if alert:
                        self.log_msg(f"⚠️ HEALTH: {alert}")
                    uptime_min = (now - self.started_at) / 60
                    pnl = self.stats["total_pnl_sol"]
                    wins = self.stats["wins"]
                    losses = self.stats["losses"]
                    total = wins + losses
                    wr = round(wins / total * 100, 1) if total else 0
                    pos_count = len(self.positions)
                    self.log_msg(
                        f"📊 Status: {uptime_min:.0f}m uptime | {pos_count} positions | "
                        f"{total} trades ({wr}% WR) | PnL: {pnl:+.4f} SOL"
                    )
                # Refresh balance periodically (every 2 min) even without trades
                if now - last_balance_refresh > 120:
                    last_balance_refresh = now
                    self.refresh_balance()
                consecutive_errors = 0  # reset on success
                time.sleep(1)
            except Exception as e:
                consecutive_errors += 1
                self.log_msg(f"⚠️ Loop error ({consecutive_errors}): {e}")
                if consecutive_errors >= 10:
                    self.log_msg("🔴 Too many consecutive errors — pausing 60s for self-healing")
                    time.sleep(60)
                    consecutive_errors = 0
                    # Try to recover: refresh balance and reconnect
                    try:
                        self.refresh_balance()
                        self.log_msg(f"🔄 Self-heal complete — balance: {self.sol_balance:.4f} SOL")
                    except Exception:
                        pass
                else:
                    time.sleep(1)

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
_volume_history = {}   # mint -> deque([(timestamp, vol), ...], maxlen=60)
_volume_history_lock = threading.Lock()
_VOLUME_HISTORY_MAX_MINTS = 500      # hard cap on tracked mints
_VOLUME_HISTORY_MAX_SAMPLES = 60     # per-mint sample limit
_VOLUME_HISTORY_TTL_SEC = 300        # drop datapoints older than 5 min
_volume_history_last_prune = 0.0

def _prune_volume_history():
    """Evict stale mints from _volume_history. Called automatically."""
    global _volume_history_last_prune
    now = time.time()
    if now - _volume_history_last_prune < 120:
        return
    _volume_history_last_prune = now
    stale = [m for m, hist in _volume_history.items()
             if not hist or now - hist[-1][0] > _VOLUME_HISTORY_TTL_SEC]
    for m in stale:
        _volume_history.pop(m, None)
    # If still over cap, drop oldest-last-seen
    if len(_volume_history) > _VOLUME_HISTORY_MAX_MINTS:
        by_age = sorted(_volume_history.items(), key=lambda kv: kv[1][-1][0] if kv[1] else 0)
        for m, _ in by_age[:len(_volume_history) - _VOLUME_HISTORY_MAX_MINTS]:
            _volume_history.pop(m, None)

def volume_velocity(mint, current_vol):
    """Returns volume acceleration score 0-100. Measures rate of volume growth."""
    now = time.time()
    with _volume_history_lock:
        _prune_volume_history()
        hist = _volume_history.get(mint)
        if hist is None:
            hist = deque(maxlen=_VOLUME_HISTORY_MAX_SAMPLES)
            _volume_history[mint] = hist
        # drop stale
        while hist and now - hist[0][0] > _VOLUME_HISTORY_TTL_SEC:
            hist.popleft()
        hist.append((now, current_vol))
        samples = list(hist)
    if len(samples) < 3:
        return 0
    # compare last third vs first third
    third = max(1, len(samples) // 3)
    early_avg = sum(v for _, v in samples[:third]) / third
    late_avg  = sum(v for _, v in samples[-third:]) / third
    if early_avg <= 0:
        return 0
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
_whale_lock  = threading.Lock()   # protects _whale_seen, _whale_mints, _whale_buys

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
    """Return the Helius API key — prefers HELIUS_API_KEY env var, falls back to URL parsing."""
    if HELIUS_API_KEY:
        return HELIUS_API_KEY
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


def helius_get_priority_fee(account_keys=None, tx_base58=None):
    """Get recommended priority fee from Helius Enhanced API.
    Returns fee in microlamports, or None on failure.
    Results are cached for 10 seconds to avoid hammering during trading bursts."""
    if not HELIUS_API_KEY:
        return None
    # 10-second cache keyed on the request identity
    cache_key = str(account_keys or tx_base58 or "")
    now = time.time()
    if (_priority_fee_cache["key"] == cache_key
            and _priority_fee_cache["fee"] is not None
            and (now - _priority_fee_cache["ts"]) < _PRIORITY_FEE_TTL):
        return _priority_fee_cache["fee"]
    try:
        params = {"options": {"recommended": True, "includeAllPriorityFeeLevels": True}}
        if tx_base58:
            params["transaction"] = tx_base58
        elif account_keys:
            params["accountKeys"] = account_keys
        else:
            return None
        resp = _http_session.post(
            HELIUS_SHARED_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": "getPriorityFeeEstimate", "params": [params]},
            timeout=5,
        )
        data = resp.json()
        result = data.get("result", {})
        levels = result.get("priorityFeeLevels", {})
        recommended = result.get("priorityFeeEstimate")
        fee = None
        if recommended:
            print(f"[HELIUS] Priority fee: recommended={recommended:.0f} µL | high={levels.get('high', '?')} veryHigh={levels.get('veryHigh', '?')}", flush=True)
            fee = int(recommended)
        else:
            fee = int(levels.get("high", 0)) or None
        # Update cache
        _priority_fee_cache["key"] = cache_key
        _priority_fee_cache["fee"] = fee
        _priority_fee_cache["ts"] = now
        return fee
    except Exception as e:
        print(f"[HELIUS] getPriorityFeeEstimate failed: {e}", flush=True)
        return None


def helius_get_asset(mint_address):
    """Fetch token metadata via Helius DAS API. Returns asset dict or None."""
    if not HELIUS_API_KEY:
        return None
    try:
        resp = _http_session.post(
            HELIUS_SHARED_RPC,
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getAsset",
                "params": {"id": mint_address, "options": {"showFungible": True}},
            },
            timeout=8,
        )
        data = resp.json()
        return data.get("result")
    except Exception as e:
        print(f"[HELIUS] getAsset({mint_address[:8]}) failed: {e}", flush=True)
        return None


def helius_get_token_accounts(owner):
    """Fetch all SPL token accounts via DAS API. Returns list of accounts or []."""
    if not HELIUS_API_KEY:
        return []
    try:
        resp = _http_session.post(
            HELIUS_SHARED_RPC,
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenAccounts",
                "params": {"owner": owner, "options": {"showZeroBalance": False}},
            },
            timeout=8,
        )
        data = resp.json()
        return (data.get("result") or {}).get("token_accounts", [])
    except Exception as e:
        print(f"[HELIUS] getTokenAccounts failed: {e}", flush=True)
        return []


def rpc_call(method, params=None, timeout=8):
    """Call Solana RPC with fallback chain: Dedicated → Shared Helius → ANKR → public."""
    _rpc_endpoints = [("helius-dedicated", HELIUS_RPC)]
    if HELIUS_SHARED_RPC:
        _rpc_endpoints.append(("helius-shared", HELIUS_SHARED_RPC))
    if ANKR_RPC:
        _rpc_endpoints.append(("ankr", ANKR_RPC))
    _rpc_endpoints.append(("public", "https://solana-rpc.publicnode.com"))

    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
    errors = []
    for label, url in _rpc_endpoints:
        started = time.perf_counter()
        try:
            resp = _http_session.post(url, json=payload, timeout=timeout)
            latency = round((time.perf_counter() - started) * 1000)
            if resp.ok:
                body = resp.json()
                if "error" not in body:
                    record_rpc_health(label, True, latency, method)
                    return body.get("result")
                errors.append(f"{label}: {body.get('error')}")
                record_rpc_health(label, False, latency, method)
            else:
                errors.append(f"{label}: HTTP {resp.status_code}")
                record_rpc_health(label, False, latency, method)
        except Exception as e:
            latency = round((time.perf_counter() - started) * 1000)
            record_rpc_health(label, False, latency, method)
            errors.append(f"{label}: {e}")
    if errors:
        print(f"[RPC] {method} all failed — {' | '.join(errors)}", flush=True)
    return None


def helius_address_transactions(address, limit=50, tx_type=None, timeout=10):
    api_key = get_helius_api_key()
    if not api_key:
        return []
    params = {"api-key": api_key, "limit": int(limit)}
    if tx_type:
        params["type"] = tx_type
    try:
        resp = _http_session.get(
            f"{HELIUS_REST_API}/addresses/{address}/transactions",
            params=params,
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
        signature = tx.get("signature") or ""
        timestamp = int(tx.get("timestamp") or 0)
        swap = (tx.get("events") or {}).get("swap") or {}
        if not fee_payer or not swap:
            continue
        token_outputs = swap.get("tokenOutputs") or []
        token_inputs = swap.get("tokenInputs") or []
        native_input = swap.get("nativeInput") or {}
        native_output = swap.get("nativeOutput") or {}
        output_leg = next((t for t in token_outputs if t.get("mint") == mint), None)
        input_leg = next((t for t in token_inputs if t.get("mint") == mint), None)
        if output_leg:
            buyers.append({
                "wallet": fee_payer,
                "signature": signature,
                "timestamp": timestamp,
                "sol": round(float(native_input.get("amount") or 0) / 1e9, 4),
                "token_amount": round(float(output_leg.get("tokenAmount") or output_leg.get("rawTokenAmount", {}).get("tokenAmount") or 0), 4),
            })
            if fee_payer not in unique_buyers:
                unique_buyers.append(fee_payer)
        elif input_leg:
            sellers.append({
                "wallet": fee_payer,
                "signature": signature,
                "timestamp": timestamp,
                "sol": round(float(native_output.get("amount") or 0) / 1e9, 4),
                "token_amount": round(float(input_leg.get("tokenAmount") or input_leg.get("rawTokenAmount", {}).get("tokenAmount") or 0), 4),
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
        rows = _fetch_largest_accounts(mint)
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
    deployer_threshold = max(1, int(settings.get("min_narrative_score", 2)) - 4)
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
        float(intel.get("volume_spike_ratio") or 0) >= float(settings.get("min_volume_spike_mult", 1.2)) or
        float(intel.get("holder_growth_1h") or 0) >= float(settings.get("min_holder_growth_pct", 5)) or
        price_momentum_override
    )
    narrative_floor = max(1, int(settings.get("min_narrative_score", 2)) - 3)
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
                f">= {settings.get('min_volume_spike_mult', 1.2)}x, "
                f">= {settings.get('min_holder_growth_pct', 5)}%, "
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
        values = (
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
            int(intel.get("unique_buyer_count") or 0),
            int(intel.get("unique_seller_count") or 0),
            float(intel.get("total_buy_sol") or 0),
            float(intel.get("total_sell_sol") or 0),
            float(intel.get("net_flow_sol") or 0),
            float(intel.get("buy_sell_ratio") or 0),
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
        )
        placeholders = ",".join(["%s"] * len(values))
        cur.execute(f"""
            INSERT INTO token_intel (
                mint, name, symbol, deployer_wallet, first_seen_at, last_seen_at,
                first_price, max_price, first_mc, max_mc, first_vol, latest_vol,
                first_liq, latest_liq, holder_count, holder_growth_1h, volume_spike_ratio,
                first_buyer_count, smart_wallet_buys, smart_wallet_first10, unique_buyer_count,
                unique_seller_count, total_buy_sol, total_sell_sol, net_flow_sol, buy_sell_ratio,
                narrative_tags, social_links, social_keys, narrative_score, deployer_score, whale_score,
                whale_action_score, cluster_confidence, infra_penalty, token_program,
                transfer_hook_enabled, can_exit, threat_risk_score, threat_flags, infra_labels, liquidity_drop_pct,
                max_multiple, green_lights, checklist_json, milestones_json, last_updated
            ) VALUES ({placeholders})
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
                unique_buyer_count=EXCLUDED.unique_buyer_count,
                unique_seller_count=EXCLUDED.unique_seller_count,
                total_buy_sol=EXCLUDED.total_buy_sol,
                total_sell_sol=EXCLUDED.total_sell_sol,
                net_flow_sol=EXCLUDED.net_flow_sol,
                buy_sell_ratio=EXCLUDED.buy_sell_ratio,
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
        """, values)
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
            "unique_buyer_count": int(cached.get("unique_buyer_count") or 0),
            "unique_seller_count": int(cached.get("unique_seller_count") or 0),
            "total_buy_sol": float(cached.get("total_buy_sol") or 0),
            "total_sell_sol": float(cached.get("total_sell_sol") or 0),
            "net_flow_sol": float(cached.get("net_flow_sol") or 0),
            "buy_sell_ratio": float(cached.get("buy_sell_ratio") or 0),
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
    persist_wallet_flow_events(mint, flow, source="helius")
    deployer_wallet = seeded.get("deployer_wallet") or resolve_deployer_wallet(mint, txns)
    if deployer_wallet and deployer_wallet != seeded.get("deployer_wallet"):
        deployer_stats = register_deployer_launch(deployer_wallet, mint)
    else:
        deployer_stats = get_deployer_stats(deployer_wallet)
    holder_count = get_token_holder_count(mint, txns)
    holder_growth = update_holder_history(mint, holder_count)
    unique_buyer_count = len({item.get("wallet") for item in (flow.get("buyers") or []) if item.get("wallet")})
    unique_seller_count = len({item.get("wallet") for item in (flow.get("sellers") or []) if item.get("wallet")})
    total_buy_sol = round(sum(float(item.get("sol") or 0) for item in (flow.get("buyers") or [])), 4)
    total_sell_sol = round(sum(float(item.get("sol") or 0) for item in (flow.get("sellers") or [])), 4)
    net_flow_sol = round(total_buy_sol - total_sell_sol, 4)
    buy_sell_ratio = round(unique_buyer_count / max(unique_seller_count, 1), 2) if unique_buyer_count else 0.0
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
        "unique_buyer_count": unique_buyer_count,
        "unique_seller_count": unique_seller_count,
        "total_buy_sol": total_buy_sol,
        "total_sell_sol": total_sell_sol,
        "net_flow_sol": net_flow_sol,
        "buy_sell_ratio": buy_sell_ratio,
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


# ── Background intel refresh pool (non-blocking) ─────────────────────────────
_intel_refresh_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="intel-bg")
_intel_refresh_pending = set()  # mints currently being refreshed in background
_intel_refresh_pending_lock = threading.Lock()

def _bg_refresh_token_intel(mint, base_info, pair, force):
    """Run refresh_token_intel in background thread, update cache when done."""
    try:
        refresh_token_intel(mint, base_info=base_info, pair=pair, force=force)
    except Exception as e:
        print(f"[INTEL-BG] refresh failed for {mint}: {e}", flush=True)
    finally:
        with _intel_refresh_pending_lock:
            _intel_refresh_pending.discard(mint)

def ensure_token_intel(mint, base_info=None, pair=None, force=False):
    if not mint:
        return {}
    with _token_intel_lock:
        cached = token_intel_payload(_token_intel_cache.get(mint) or {})
    if cached and not force:
        last_updated = _from_iso(cached.get("last_updated"))
        if last_updated and (_now_utc() - last_updated).total_seconds() < INTEL_REFRESH_SEC:
            return cached
    # If we have stale cached data, return it immediately and refresh in background
    if cached and not force:
        with _intel_refresh_pending_lock:
            if mint not in _intel_refresh_pending:
                _intel_refresh_pending.add(mint)
                try:
                    _intel_refresh_pool.submit(_bg_refresh_token_intel, mint, base_info, pair, force)
                except Exception:
                    _intel_refresh_pending.discard(mint)
        return cached
    # No cached data at all — must block to get initial intel
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

def _whale_is_sig_seen(sig):
    """Thread-safe check + add for whale signature dedup."""
    with _whale_lock:
        if sig in _whale_seen:
            return True
        _whale_seen.add(sig)
        # Cap the set so it doesn't grow forever
        if len(_whale_seen) > 20_000:
            _whale_seen.clear()
        return False

def _whale_is_mint_recent(mint, cooldown_sec=300):
    """Thread-safe dedup: returns True if mint was seen within cooldown."""
    now = time.time()
    with _whale_lock:
        if now - _whale_mints.get(mint, 0) < cooldown_sec:
            return True
        _whale_mints[mint] = now
        # Prune stale mints
        if len(_whale_mints) > 2000:
            stale = [m for m, ts in _whale_mints.items() if now - ts > 3600]
            for m in stale:
                _whale_mints.pop(m, None)
        return False

def _whale_record_buy(wallet, mint, name, price, mc, vol, liq):
    """Thread-safe append to whale_buys dashboard feed."""
    now = time.time()
    with _whale_lock:
        _whale_buys.appendleft({
            "wallet": wallet, "label": WHALE_LABELS.get(wallet, wallet[:8] + "..."),
            "mint": mint, "name": name, "price": price,
            "mc": mc, "vol": vol, "liq": liq,
            "timestamp": now, "ts": time.strftime("%H:%M:%S"),
        })

def _whale_process_dex_pair(wallet, mint, p):
    """Validate a DexScreener pair for whale-copy eligibility and broadcast to bots."""
    price = float(p.get("priceUsd") or 0)
    if not price:
        return
    mc = p.get("marketCap", 0) or 0
    vol = (p.get("volume") or {}).get("h24", 0) or 0
    liq = (p.get("liquidity") or {}).get("usd", 0) or 0
    name = p.get("baseToken", {}).get("name", "Unknown")
    created_at = p.get("pairCreatedAt")
    age_min = (time.time() * 1000 - created_at) / 60000 if created_at else 9999
    if age_min > 60 or mc > 500_000 or liq < 3000:
        return
    if _whale_is_mint_recent(mint):
        return
    _whale_record_buy(wallet, mint, name, price, mc, vol, liq)
    change = (p.get("priceChange") or {}).get("h1", 0) or 0
    with user_bots_lock:
        running_bots = [b for b in user_bots.values() if b.running]
    for bot in running_bots:
        if mint not in bot.positions:
            bot.log_msg(f"🐋 WHALE COPY: {name} ({wallet[:8]}...)")
            try:
                bot.evaluate_signal(mint, name, price, mc, vol, liq, age_min, change)
            except Exception as _e:
                print(f"[WHALE] eval error: {_e}", flush=True)

def _poll_single_whale(wallet):
    """Fetch + process one whale wallet's recent transactions. Run inside ThreadPoolExecutor."""
    try:
        enhanced_txs = helius_address_transactions(wallet, limit=8, timeout=8)
        used_enhanced = False
        for tx in enhanced_txs:
            sig = tx.get("signature")
            if not sig or _whale_is_sig_seen(sig):
                continue
            transfers = tx.get("tokenTransfers") or []
            if not isinstance(transfers, list):
                continue
            used_enhanced = True
            for transfer in transfers:
                mint = transfer.get("mint") or ""
                to_user = transfer.get("toUserAccount") or ""
                amount = float(transfer.get("tokenAmount") or 0)
                if not mint or mint == SOL_MINT or to_user != wallet or amount <= 0:
                    continue
                try:
                    _wr = dex_get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=5)
                    if _wr.status_code != 200:
                        continue
                    pairs = safe_json_response(_wr, {}).get("pairs") or []
                    if pairs:
                        _whale_process_dex_pair(wallet, mint, pairs[0])
                except Exception as _e:
                    print(f"[WHALE] dex lookup error: {_e}", flush=True)
        if used_enhanced:
            return
        # Fallback: raw RPC signatures
        r = _http_session.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": [wallet, {"limit": 5}]
        }, timeout=8)
        sigs = safe_json_response(r, {}).get("result", [])
        for sig_info in sigs:
            sig = sig_info.get("signature")
            if not sig or _whale_is_sig_seen(sig):
                continue
            tx_r = _http_session.post(HELIUS_RPC, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTransaction",
                "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
            }, timeout=8)
            tx = safe_json_response(tx_r, {}).get("result")
            if not tx:
                continue
            pre = {a["accountIndex"]: a for a in (tx.get("meta") or {}).get("preTokenBalances", [])}
            post = {a["accountIndex"]: a for a in (tx.get("meta") or {}).get("postTokenBalances", [])}
            for idx, pb in post.items():
                mint = pb.get("mint")
                if not mint or mint == SOL_MINT:
                    continue
                pre_amt = float((pre.get(idx) or {}).get("uiTokenAmount", {}).get("uiAmount") or 0)
                post_amt = float(pb.get("uiTokenAmount", {}).get("uiAmount") or 0)
                if post_amt > pre_amt * 1.5 and post_amt > 0:
                    try:
                        _wr = dex_get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=5)
                        if _wr.status_code != 200:
                            continue
                        pairs = safe_json_response(_wr, {}).get("pairs") or []
                        if pairs:
                            _whale_process_dex_pair(wallet, mint, pairs[0])
                    except Exception as _e:
                        print(f"[WHALE] dex fallback error: {_e}", flush=True)
    except Exception as _e:
        print(f"[WHALE] wallet {wallet[:8]}... error: {_e}", flush=True)

def check_whale_wallets():
    """Poll recent transactions of whale wallets in parallel and copy their buys."""
    time.sleep(15)  # let app fully start first
    while True:
        try:
            # Always poll whale wallets — dashboard shows activity even without active bots
            # Process all wallets in parallel (cuts 32s serial down to ~8s)
            with ThreadPoolExecutor(max_workers=min(len(WHALE_WALLETS), 4)) as pool:
                pool.map(_poll_single_whale, WHALE_WALLETS)
            time.sleep(20)
        except Exception as e:
            print(f"[WHALE] tracker error: {e}", flush=True)
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


def _shadow_strategy_settings(user_id=None):
    strategies = {
        strategy_name: dict(PRESETS.get(strategy_name, {}))
        for strategy_name in CANONICAL_STRATEGIES
        if strategy_name in PRESETS
    }
    # Include V2 optimized strategies for A/B shadow testing
    for v2_name, v2_settings in SHADOW_V2_PRESETS.items():
        strategies[v2_name] = dict(v2_settings)
    if user_id:
        try:
            _preset_name, live_settings = load_user_effective_settings(user_id)
            if live_settings and isinstance(live_settings, dict):
                strategies["live"] = dict(live_settings)
        except Exception as e:
            print(f"[SHADOW] failed to load live settings for user {user_id}: {e}", flush=True)
    return strategies


def _classify_market_event(previous_row, info):
    if not previous_row:
        return "token_discovered"
    current_price = float(info.get("price") or 0)
    current_liq = float(info.get("liq") or 0)
    previous_price = float(previous_row.get("last_price") or 0)
    previous_liq = float(previous_row.get("last_liq") or 0)
    if previous_liq > 0 and current_liq <= previous_liq * 0.72:
        return "liquidity_drop"
    if previous_liq > 0 and current_liq >= previous_liq * 1.28:
        return "liquidity_add"
    if previous_price > 0 and current_price >= previous_price * 1.22:
        return "price_breakout"
    if previous_price > 0 and current_price <= previous_price * 0.78:
        return "price_flush"
    return "market_tick"


def _event_timestamp_to_datetime(ts):
    try:
        ts = int(ts or 0)
        return datetime.utcfromtimestamp(ts) if ts > 0 else _now_utc()
    except Exception:
        return _now_utc()


def persist_wallet_flow_events(mint, flow, source="helius"):
    if not mint:
        return 0
    rows = []
    for side, items in (("buy", flow.get("buyers") or []), ("sell", flow.get("sellers") or [])):
        for item in items:
            wallet = item.get("wallet")
            if not wallet:
                continue
            signature = item.get("signature") or ""
            timestamp = int(item.get("timestamp") or 0)
            observed_at = _event_timestamp_to_datetime(timestamp)
            event_key = f"{mint}:{signature or timestamp}:{side}:{wallet}"
            payload = {
                "wallet": wallet,
                "signature": signature,
                "side": side,
                "sol": float(item.get("sol") or 0),
                "token_amount": float(item.get("token_amount") or 0),
                "timestamp": timestamp,
            }
            rows.append((
                event_key,
                mint,
                signature or None,
                wallet,
                side,
                float(item.get("sol") or 0),
                float(item.get("token_amount") or 0),
                1 if wallet in WHALE_WALLETS else 0,
                source,
                observed_at,
                json.dumps(payload),
            ))
    if not rows:
        return 0
    conn = db()
    try:
        cur = conn.cursor()
        psycopg2.extras.execute_batch(cur, """
            INSERT INTO wallet_flow_events (
                event_key, mint, signature, wallet, side, sol_amount, token_amount,
                smart_wallet, source, observed_at, payload_json
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (event_key) DO NOTHING
        """, rows, page_size=100)
        inserted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
        conn.commit()
        return inserted
    except Exception as e:
        conn.rollback()
        print(f"[TAPE] wallet flow persist error for {mint}: {e}", flush=True)
        return 0
    finally:
        db_return(conn)


def persist_liquidity_delta_event(mint, previous_row, info, event_type):
    previous_liq = float((previous_row or {}).get("last_liq") or 0)
    current_liq = float(info.get("liq") or 0)
    if previous_liq <= 0 or current_liq <= 0:
        return
    delta_liq = current_liq - previous_liq
    delta_pct = (delta_liq / previous_liq) * 100.0 if previous_liq else 0.0
    if abs(delta_pct) < 3:
        return
    observed_at = _event_timestamp_to_datetime(info.get("ts") or int(time.time()))
    event_key = f"{mint}:{event_type}:{int(observed_at.timestamp())}:{round(current_liq, 2)}"
    payload = {
        "mint": mint,
        "event_type": event_type,
        "previous_liq": previous_liq,
        "current_liq": current_liq,
        "delta_liq": round(delta_liq, 2),
        "delta_pct": round(delta_pct, 2),
        "previous_price": float((previous_row or {}).get("last_price") or 0),
        "current_price": float(info.get("price") or 0),
    }
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO liquidity_delta_events (
                event_key, mint, event_type, source, previous_liq, current_liq, delta_liq,
                delta_pct, previous_price, current_price, observed_at, payload_json
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (event_key) DO NOTHING
        """, (
            event_key,
            mint,
            event_type,
            info.get("source") or "scanner",
            previous_liq,
            current_liq,
            round(delta_liq, 2),
            round(delta_pct, 2),
            float((previous_row or {}).get("last_price") or 0),
            float(info.get("price") or 0),
            observed_at,
            json.dumps(payload),
        ))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[TAPE] liquidity delta persist error for {mint}: {e}", flush=True)
    finally:
        db_return(conn)


def _record_market_intelligence(info, include_strategy_decisions=True, include_model_decisions=True, user_id=None):
    intel = info.get("intel") or {}
    snapshot = build_feature_snapshot(info, intel)
    flow_snapshot = build_flow_snapshot(info, intel)
    strategies = _shadow_strategy_settings(user_id=user_id)
    conn = db()
    try:
        cur = conn.cursor()
        payload_json = json.dumps(info)
        cur.execute("""
            SELECT mint, last_price, last_liq, observations
            FROM market_tokens
            WHERE mint=%s
            LIMIT 1
        """, (info.get("mint"),))
        previous_row = cur.fetchone()
        event_type = _classify_market_event(previous_row, info)
        cur.execute("""
            INSERT INTO market_tokens (
                mint, name, symbol, first_price, last_price, peak_price, trough_price,
                first_mc, last_mc, first_liq, last_liq, first_vol, last_vol,
                latest_source, latest_payload_json, observations
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)
            ON CONFLICT (mint) DO UPDATE SET
                name = EXCLUDED.name,
                symbol = EXCLUDED.symbol,
                last_seen_at = NOW(),
                last_price = EXCLUDED.last_price,
                peak_price = GREATEST(COALESCE(market_tokens.peak_price, EXCLUDED.peak_price), EXCLUDED.peak_price),
                trough_price = LEAST(COALESCE(market_tokens.trough_price, EXCLUDED.trough_price), EXCLUDED.trough_price),
                last_mc = EXCLUDED.last_mc,
                last_liq = EXCLUDED.last_liq,
                last_vol = EXCLUDED.last_vol,
                latest_source = EXCLUDED.latest_source,
                latest_payload_json = EXCLUDED.latest_payload_json,
                observations = COALESCE(market_tokens.observations, 0) + 1
        """, (
            info.get("mint"), info.get("name"), info.get("symbol"),
            info.get("price"), info.get("price"), info.get("price"), info.get("price"),
            info.get("mc"), info.get("mc"), info.get("liq"), info.get("liq"),
            info.get("vol"), info.get("vol"), info.get("source") or "scanner", payload_json,
        ))
        cur.execute("""
            INSERT INTO market_events (
                mint, event_type, source, name, symbol, price, mc, liq, vol,
                change_pct, age_min, payload_json
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            info.get("mint"), event_type, info.get("source") or "scanner",
            info.get("name"), info.get("symbol"), info.get("price"), info.get("mc"),
            info.get("liq"), info.get("vol"), info.get("change"), info.get("age_min"), payload_json,
        ))
        persist_liquidity_delta_event(info.get("mint"), previous_row, info, event_type)
        cur.execute("""
            INSERT INTO token_feature_snapshots (
                mint, source, price, composite_score, confidence, ai_score, green_lights,
                narrative_score, deployer_score, whale_score, whale_action_score,
                holder_growth_1h, volume_spike_ratio, threat_risk_score, feature_json
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            info.get("mint"), info.get("source") or "scanner", info.get("price"),
            snapshot.get("composite_score"), snapshot.get("confidence"), snapshot.get("score"),
            snapshot.get("green_lights"), snapshot.get("narrative_score"), snapshot.get("deployer_score"),
            snapshot.get("whale_score"), snapshot.get("whale_action_score"), snapshot.get("holder_growth_1h"),
            snapshot.get("volume_spike_ratio"), snapshot.get("threat_risk_score"), json.dumps(snapshot),
        ))
        # Classify regime at write time so it's persisted with the snapshot
        row_regime = classify_flow_regime_row(flow_snapshot)
        cur.execute("""
            INSERT INTO token_flow_snapshots (
                mint, source, price, mc, liq, vol, age_min, holder_count, holder_growth_1h,
                unique_buyer_count, unique_seller_count, first_buyer_count, smart_wallet_buys,
                smart_wallet_first10, total_buy_sol, total_sell_sol, net_flow_sol, buy_sell_ratio,
                buy_pressure_pct, liquidity_drop_pct, threat_risk_score, transfer_hook_enabled,
                can_exit, regime_label, flow_json
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            info.get("mint"), info.get("source") or "scanner", flow_snapshot.get("price"),
            flow_snapshot.get("mc"), flow_snapshot.get("liq"), flow_snapshot.get("vol"), flow_snapshot.get("age_min"),
            flow_snapshot.get("holder_count"), flow_snapshot.get("holder_growth_1h"),
            flow_snapshot.get("unique_buyer_count"), flow_snapshot.get("unique_seller_count"),
            flow_snapshot.get("first_buyer_count"), flow_snapshot.get("smart_wallet_buys"),
            flow_snapshot.get("smart_wallet_first10"), flow_snapshot.get("total_buy_sol"),
            flow_snapshot.get("total_sell_sol"), flow_snapshot.get("net_flow_sol"), flow_snapshot.get("buy_sell_ratio"),
            flow_snapshot.get("buy_pressure_pct"), flow_snapshot.get("liquidity_drop_pct"),
            flow_snapshot.get("threat_risk_score"), int(bool(flow_snapshot.get("transfer_hook_enabled"))),
            None if flow_snapshot.get("can_exit") is None else int(bool(flow_snapshot.get("can_exit"))),
            row_regime,
            json.dumps(flow_snapshot),
        ))

        opened_positions = 0
        if include_strategy_decisions:
            for strategy_name, settings in strategies.items():
                decision = evaluate_shadow_strategy(strategy_name, settings, snapshot)
                decision_json = json.dumps(decision.as_dict())
                cur.execute("""
                    INSERT INTO shadow_decisions (
                        strategy_name, mint, name, source, passed, score, confidence, price,
                        pass_reasons_json, blocker_reasons_json, feature_json, decision_json
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    strategy_name, info.get("mint"), info.get("name"), info.get("source") or "scanner",
                    1 if decision.passed else 0, decision.score, decision.confidence, info.get("price"),
                    json.dumps(decision.pass_reasons), json.dumps(decision.blocker_reasons),
                    json.dumps(snapshot), decision_json,
                ))
                if not decision.passed:
                    continue
                # Allow re-entry after 60-min cooldown (not lifetime ban)
                cur.execute("""
                    SELECT id
                    FROM shadow_positions
                    WHERE strategy_name=%s AND mint=%s
                      AND (status='open' OR closed_at > NOW() - INTERVAL '60 minutes')
                    LIMIT 1
                """, (strategy_name, info.get("mint")))
                if cur.fetchone():
                    continue
                cur.execute("""
                    INSERT INTO shadow_positions (
                        strategy_name, mint, name, source, status, entry_price, current_price,
                        peak_price, trough_price, take_profit_mult, stop_loss_ratio, time_stop_min,
                        score, confidence, feature_json, decision_json
                    ) VALUES (%s,%s,%s,%s,'open',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    strategy_name, info.get("mint"), info.get("name"), info.get("source") or "scanner",
                    info.get("price"), info.get("price"), info.get("price"), info.get("price"),
                    decision.metrics.get("take_profit_mult"), decision.metrics.get("stop_loss_ratio"),
                    decision.metrics.get("time_stop_min"), decision.score, decision.confidence,
                    json.dumps(snapshot), decision_json,
                ))
                opened_positions += 1

        # ── Inline position updates: update all open positions for this mint ──
        price = float(info.get("price") or 0)
        mint = info.get("mint")
        if price > 0 and mint:
            cur.execute("""
                UPDATE shadow_positions
                SET current_price = %s,
                    peak_price = GREATEST(peak_price, %s),
                    trough_price = LEAST(trough_price, %s),
                    max_upside_pct = GREATEST(COALESCE(max_upside_pct, 0),
                        ((%s - entry_price) / NULLIF(entry_price, 0)) * 100),
                    max_drawdown_pct = LEAST(COALESCE(max_drawdown_pct, 0),
                        ((%s - entry_price) / NULLIF(entry_price, 0)) * 100),
                    observations = COALESCE(observations, 0) + 1,
                    last_seen_at = NOW()
                WHERE mint = %s AND status = 'open'
            """, (price, price, price, price, price, mint))

            # Check TP/SL/time-stop exits inline
            cur.execute("SELECT * FROM shadow_positions WHERE mint=%s AND status='open'", (mint,))
            open_rows = cur.fetchall()
            closed_inline = 0
            _all_p = _all_known_presets()
            for row in open_rows:
                settings = _all_p.get(row.get("strategy_name") or "balanced", PRESETS["balanced"])
                opened_at = row.get("opened_at")
                age_min = ((time.time() - opened_at.timestamp()) / 60.0) if opened_at else 0.0
                update = shadow_position_update(row, price, settings, age_min)
                if update["status"] == "closed":
                    cur.execute("""
                        UPDATE shadow_positions
                        SET status='closed', closed_at=NOW(), exit_price=%s,
                            exit_reason=%s, realized_pnl_pct=%s,
                            peak_price=%s, trough_price=%s,
                            max_upside_pct=%s, max_drawdown_pct=%s,
                            tp1_hit=%s, tp1_pnl_pct=%s
                        WHERE id=%s
                    """, (
                        update["current_price"], update["exit_reason"], update["realized_pnl_pct"],
                        update["peak_price"], update["trough_price"],
                        update["max_upside_pct"], update["max_drawdown_pct"],
                        1 if update.get("tp1_hit") else 0, update.get("tp1_pnl_pct", 0),
                        row["id"],
                    ))
                    closed_inline += 1
                elif update.get("tp1_hit") and not row.get("tp1_hit"):
                    # TP1 just triggered — update the flag but keep position open
                    cur.execute("""
                        UPDATE shadow_positions
                        SET tp1_hit=1, tp1_pnl_pct=%s,
                            peak_price=%s, trough_price=%s,
                            max_upside_pct=%s, max_drawdown_pct=%s
                        WHERE id=%s
                    """, (
                        update.get("tp1_pnl_pct", 0),
                        update["peak_price"], update["trough_price"],
                        update["max_upside_pct"], update["max_drawdown_pct"],
                        row["id"],
                    ))
            if closed_inline:
                print(f"[SIM] inline-closed {closed_inline} position(s) for {info.get('name')}", flush=True)

        conn.commit()
        if include_model_decisions:
            _log_model_decisions(info, snapshot, flow_snapshot)
        if opened_positions:
            print(f"[SIM] opened {opened_positions} shadow position(s) for {info.get('name')}", flush=True)
    except Exception as e:
        conn.rollback()
        print(f"[SIM] market intelligence record error: {e}", flush=True)
    finally:
        db_return(conn)


def reconcile_shadow_positions(limit=40):
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT *
            FROM shadow_positions
            WHERE status='open'
              AND (last_seen_at IS NULL OR last_seen_at < NOW() - INTERVAL '60 seconds')
            ORDER BY opened_at ASC
            LIMIT %s
        """, (limit,))
        open_rows = cur.fetchall()
    finally:
        db_return(conn)

    if not open_rows:
        return

    now = time.time()
    _all_p = _all_known_presets()
    price_cache = {}
    for row in open_rows:
        mint = row.get("mint")
        if not mint or mint in price_cache:
            continue
        try:
            resp = dex_get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=6)
            data = safe_json_response(resp, {})
            pairs = data.get("pairs") or []
            pair = pairs[0] if pairs else {}
            price_cache[mint] = float(pair.get("priceUsd") or 0) if pair else 0.0
        except Exception:
            price_cache[mint] = 0.0

    conn = db()
    try:
        cur = conn.cursor()
        for row in open_rows:
            mint = row.get("mint")
            current_price = float(price_cache.get(mint) or 0)
            if current_price <= 0:
                continue
            strategy_name = row.get("strategy_name") or "balanced"
            settings = _all_p.get(strategy_name, PRESETS["balanced"])
            opened_at = row.get("opened_at")
            age_min = ((now - opened_at.timestamp()) / 60.0) if opened_at else 0.0
            update = shadow_position_update(row, current_price, settings, age_min)
            cur.execute("""
                UPDATE shadow_positions
                SET current_price=%s,
                    peak_price=%s,
                    trough_price=%s,
                    max_upside_pct=%s,
                    max_drawdown_pct=%s,
                    observations=COALESCE(observations, 0) + 1,
                    last_seen_at=NOW(),
                    status=%s,
                    closed_at=CASE WHEN %s='closed' THEN NOW() ELSE closed_at END,
                    exit_price=CASE WHEN %s='closed' THEN %s ELSE exit_price END,
                    exit_reason=CASE WHEN %s='closed' THEN %s ELSE exit_reason END,
                    realized_pnl_pct=CASE WHEN %s='closed' THEN %s ELSE realized_pnl_pct END,
                    tp1_hit=CASE WHEN %s=1 THEN 1 ELSE tp1_hit END,
                    tp1_pnl_pct=CASE WHEN %s=1 AND tp1_hit=0 THEN %s ELSE tp1_pnl_pct END
                WHERE id=%s
            """, (
                update["current_price"], update["peak_price"], update["trough_price"],
                update["max_upside_pct"], update["max_drawdown_pct"], update["status"],
                update["status"], update["status"], update["current_price"],
                update["status"], update["exit_reason"], update["status"], update["realized_pnl_pct"],
                1 if update.get("tp1_hit") else 0,
                1 if update.get("tp1_hit") else 0, update.get("tp1_pnl_pct", 0),
                row["id"],
            ))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[SIM] shadow reconcile error: {e}", flush=True)
    finally:
        db_return(conn)


def shadow_position_monitor():
    time.sleep(20)
    while True:
        try:
            reconcile_shadow_positions(limit=60)
        except Exception as e:
            print(f"[SIM] monitor error: {e}", flush=True)
        time.sleep(45)


def execution_tape_monitor():
    time.sleep(35)
    while True:
        try:
            if not get_helius_api_key():
                time.sleep(180)
                continue
            conn = db()
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT mint, name, symbol, last_price, last_mc, last_liq, last_vol,
                           EXTRACT(EPOCH FROM (NOW() - first_seen_at)) / 60.0 AS age_min
                    FROM market_tokens
                    WHERE last_seen_at >= NOW() - INTERVAL '24 hours'
                    ORDER BY last_seen_at DESC
                    LIMIT 6
                """)
                rows = cur.fetchall()
            finally:
                db_return(conn)
            _active_uid = None
            with user_bots_lock:
                for _uid, _bot in user_bots.items():
                    if _bot.running:
                        _active_uid = _uid
                        break
            for row in rows:
                intel = refresh_token_intel(row.get("mint"), base_info={
                    "mint": row.get("mint"),
                    "name": row.get("name"),
                    "symbol": row.get("symbol"),
                    "price": float(row.get("last_price") or 0),
                    "mc": float(row.get("last_mc") or 0),
                    "liq": float(row.get("last_liq") or 0),
                    "vol": float(row.get("last_vol") or 0),
                    "age_min": float(row.get("age_min") or 0),
                }, force=True)
                first_price = float((intel or {}).get("first_price") or 0)
                current_price = float(row.get("last_price") or 0)
                change_pct = 0.0
                if first_price > 0 and current_price > 0:
                    change_pct = round(((current_price / first_price) - 1.0) * 100.0, 2)
                tape_info = {
                    "mint": row.get("mint"),
                    "name": row.get("name"),
                    "symbol": row.get("symbol"),
                    "price": current_price,
                    "mc": float(row.get("last_mc") or 0),
                    "liq": float(row.get("last_liq") or 0),
                    "vol": float(row.get("last_vol") or 0),
                    "age_min": float(row.get("age_min") or 0),
                    "change": change_pct,
                    "source": "execution_tape",
                    "momentum": volume_velocity(row.get("mint"), float(row.get("last_vol") or 0)),
                    "score": ai_score_detailed({
                        "vol": float(row.get("last_vol") or 0),
                        "liq": float(row.get("last_liq") or 0),
                        "mc": float(row.get("last_mc") or 0),
                        "age_min": float(row.get("age_min") or 0),
                        "change": change_pct,
                        "momentum": volume_velocity(row.get("mint"), float(row.get("last_vol") or 0)),
                    }).get("total", 0),
                    "intel": intel or {},
                    "green_lights": int((intel or {}).get("green_lights") or 0),
                    "deployer_score": int((intel or {}).get("deployer_score") or 0),
                    "narrative_score": int((intel or {}).get("narrative_score") or 0),
                }
                _record_market_intelligence(
                    tape_info,
                    include_strategy_decisions=True,
                    include_model_decisions=True,
                    user_id=_active_uid,
                )
                time.sleep(2)
        except Exception as e:
            print(f"[TAPE] monitor error: {e}", flush=True)
        time.sleep(90)


def _load_backtest_snapshots(days=7, strategies=None, limit=50000):
    cutoff = max(1, int(days))
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT s.mint,
                   COALESCE(mt.name, 'Unknown') AS name,
                   s.source,
                   s.price,
                   s.feature_json,
                   s.created_at
            FROM token_feature_snapshots s
            LEFT JOIN market_tokens mt ON mt.mint = s.mint
            WHERE s.created_at >= NOW() - INTERVAL %s
            ORDER BY s.created_at ASC
            LIMIT %s
        """, (f"{cutoff} days", limit))
        return cur.fetchall()
    finally:
        db_return(conn)


def _load_backtest_event_tape(days=7, limit=80000):
    cutoff = max(1, int(days))
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT mint, event_type, source, name, symbol, price, mc, liq, vol,
                   change_pct, age_min, payload_json, created_at
            FROM (
                SELECT mint, event_type, source, name, symbol, price, mc, liq, vol,
                       change_pct, age_min, payload_json, created_at
                FROM market_events
                WHERE created_at >= NOW() - INTERVAL %s
                UNION ALL
                SELECT mint,
                       CASE WHEN side='buy' THEN 'wallet_buy' ELSE 'wallet_sell' END AS event_type,
                       source,
                       NULL::TEXT AS name,
                       NULL::TEXT AS symbol,
                       NULL::REAL AS price,
                       NULL::REAL AS mc,
                       NULL::REAL AS liq,
                       NULL::REAL AS vol,
                       NULL::REAL AS change_pct,
                       NULL::REAL AS age_min,
                       payload_json,
                       COALESCE(observed_at, created_at) AS created_at
                FROM wallet_flow_events
                WHERE COALESCE(observed_at, created_at) >= NOW() - INTERVAL %s
                UNION ALL
                SELECT mint,
                       event_type,
                       source,
                       NULL::TEXT AS name,
                       NULL::TEXT AS symbol,
                       current_price AS price,
                       NULL::REAL AS mc,
                       current_liq AS liq,
                       NULL::REAL AS vol,
                       delta_pct AS change_pct,
                       NULL::REAL AS age_min,
                       payload_json,
                       COALESCE(observed_at, created_at) AS created_at
                FROM liquidity_delta_events
                WHERE COALESCE(observed_at, created_at) >= NOW() - INTERVAL %s
            ) tape
            ORDER BY created_at ASC
            LIMIT %s
        """, (f"{cutoff} days", f"{cutoff} days", f"{cutoff} days", limit))
        return cur.fetchall()
    finally:
        db_return(conn)


def _load_quant_model_rows(days=MODEL_TRAIN_DAYS, recent_hours=24, recent_limit=1000):
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT ON (s.mint)
                   s.mint,
                   COALESCE(mt.name, 'Unknown') AS name,
                   s.price,
                   s.feature_json,
                   s.created_at
            FROM token_feature_snapshots s
            JOIN market_tokens mt ON mt.mint = s.mint
            WHERE s.created_at >= NOW() - INTERVAL %s
              AND COALESCE(mt.first_price, 0) > 0
            ORDER BY s.mint, s.created_at ASC
            LIMIT 6000
        """, (f"{max(1, int(days))} days",))
        entry_rows = cur.fetchall()
        cur.execute("""
            SELECT mint,
                   COALESCE(name, 'Unknown') AS name,
                   first_price,
                   peak_price,
                   trough_price,
                   last_price
            FROM market_tokens
            WHERE last_seen_at >= NOW() - INTERVAL %s
              AND COALESCE(first_price, 0) > 0
            ORDER BY last_seen_at DESC
            LIMIT 6000
        """, (f"{max(1, int(days))} days",))
        token_rows = cur.fetchall()
        cur.execute("""
            SELECT mint, buy_sell_ratio, net_flow_sol, threat_risk_score,
                   liquidity_drop_pct, can_exit, regime_label, flow_json, created_at
            FROM token_flow_snapshots
            WHERE created_at >= NOW() - INTERVAL %s
            ORDER BY created_at ASC
            LIMIT 12000
        """, (f"{max(1, int(days))} days",))
        flow_rows = cur.fetchall()
        cur.execute("""
            SELECT DISTINCT ON (s.mint)
                   s.mint,
                   COALESCE(mt.name, 'Unknown') AS name,
                   s.price,
                   s.feature_json,
                   s.created_at
            FROM token_feature_snapshots s
            JOIN market_tokens mt ON mt.mint = s.mint
            WHERE s.created_at >= NOW() - INTERVAL %s
              AND COALESCE(mt.first_price, 0) > 0
            ORDER BY s.mint, s.created_at DESC
            LIMIT %s
        """, (f"{max(1, int(recent_hours))} hours", recent_limit))
        recent_rows = cur.fetchall()
        return {
            "entry_rows": entry_rows,
            "token_rows": token_rows,
            "flow_rows": flow_rows,
            "recent_rows": recent_rows,
        }
    finally:
        db_return(conn)


def _build_quant_model_family(days=MODEL_TRAIN_DAYS):
    rows = _load_quant_model_rows(days=days)
    outcomes = build_outcome_labels(rows["token_rows"])
    family = train_regime_model_family(rows["entry_rows"], outcomes, rows["flow_rows"])
    return {"family": family, "outcomes": outcomes, "rows": rows}


def get_cached_quant_model_bundle(days=MODEL_TRAIN_DAYS, refresh_sec=MODEL_CACHE_REFRESH_SEC, force=False):
    now = time.time()
    with _quant_model_cache_lock:
        bundle = _quant_model_cache.get("bundle")
        if (
            not force
            and bundle
            and _quant_model_cache.get("days") == days
            and now - float(_quant_model_cache.get("built_at") or 0) < refresh_sec
        ):
            return bundle
    built = _build_quant_model_family(days=days)
    with _quant_model_cache_lock:
        _quant_model_cache["bundle"] = built
        _quant_model_cache["family"] = built.get("family")
        _quant_model_cache["days"] = days
        _quant_model_cache["built_at"] = now
    return built


def _current_regime_context(include_flow_snapshot=None, limit=None, window_hours=48):
    """Determine active market regime from recent flow snapshots.

    Args:
        include_flow_snapshot: Optional live snapshot to prepend to the dataset.
        limit: Max rows to return (safety cap, default 5000).
        window_hours: Time window in hours to look back (default 48 = 2 days).
    """
    if limit is None:
        limit = 5000
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT mint, buy_sell_ratio, net_flow_sol, smart_wallet_buys, unique_buyer_count,
                   threat_risk_score, can_exit, liquidity_drop_pct, created_at
            FROM token_flow_snapshots
            WHERE created_at >= NOW() - INTERVAL '%s hours'
            ORDER BY created_at DESC
            LIMIT %s
        """, (max(1, int(window_hours)), max(1, int(limit)),))
        rows = cur.fetchall()
    finally:
        db_return(conn)
    if include_flow_snapshot:
        rows = [dict(include_flow_snapshot)] + list(rows)
    return summarize_flow_regime(rows)


def _log_model_decisions(info, snapshot, flow_snapshot):
    try:
        bundle = get_cached_quant_model_bundle(days=MODEL_TRAIN_DAYS)
        family = bundle.get("family") or {}
        regime_context = _current_regime_context(include_flow_snapshot=flow_snapshot, window_hours=48)
        active_regime = regime_context.get("regime") or "neutral"
        variants = [("global", "global"), ("regime_auto", "auto")]
        rows = []
        for mode_label, selection_mode in variants:
            scored = score_feature_snapshot_with_family(snapshot, family, active_regime, mode=selection_mode)
            selection = scored.get("selection") or {}
            passed = float(scored.get("model_score") or 0) >= MODEL_DECISION_THRESHOLD and bool(scored.get("trained"))
            decision_payload = {
                "mode": mode_label,
                "selection_mode": selection_mode,
                "passed": passed,
                "threshold": MODEL_DECISION_THRESHOLD,
                "active_regime": active_regime,
                "selection": selection,
                "score": scored.get("model_score"),
            }
            rows.append((
                mode_label,
                scored.get("model_key") or "global",
                active_regime,
                info.get("mint"),
                info.get("name"),
                1 if passed else 0,
                MODEL_DECISION_THRESHOLD,
                float(scored.get("model_score") or 0),
                float(scored.get("raw_score") or 0),
                float(info.get("price") or 0),
                selection.get("fallback_reason") or "",
                json.dumps(snapshot),
                json.dumps(scored.get("top_drivers") or []),
                json.dumps(decision_payload),
            ))
        conn = db()
        try:
            cur = conn.cursor()
            psycopg2.extras.execute_batch(cur, """
                INSERT INTO model_decisions (
                    mode, model_key, active_regime, mint, name, passed, threshold,
                    model_score, raw_score, price, fallback_reason, feature_json,
                    driver_json, decision_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, rows, page_size=50)
            conn.commit()
        finally:
            db_return(conn)
    except Exception as e:
        print(f"[MODEL] decision log error: {e}", flush=True)


def _serialize_comparison_trade(trade):
    return {
        "policy_name": trade.strategy_name,
        "mint": trade.mint,
        "name": trade.name,
        "entry_price": float(trade.entry_price or 0),
        "exit_price": float(trade.exit_price or 0),
        "realized_pnl_pct": float(trade.realized_pnl_pct or 0),
        "max_upside_pct": float(trade.max_upside_pct or 0),
        "max_drawdown_pct": float(trade.max_drawdown_pct or 0),
        "exit_reason": trade.exit_reason,
    }


def load_quant_edge_reports(limit=24, window_days=None, report_kind=None):
    conn = db()
    try:
        cur = conn.cursor()
        sql = """
            SELECT id, report_kind, window_days, model_threshold, active_regime,
                   summary_json, policy_json, generated_at
            FROM quant_edge_reports
        """
        params = []
        conditions = []
        if window_days is not None:
            conditions.append("window_days=%s")
            params.append(max(1, int(window_days)))
        if report_kind:
            conditions.append("report_kind=%s")
            params.append((report_kind or "").strip().lower())
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY generated_at DESC LIMIT %s"
        params.append(max(1, int(limit)))
        cur.execute(sql, tuple(params))
        return cur.fetchall()
    finally:
        db_return(conn)


def generate_quant_edge_report(window_days=7, report_kind="manual", model_threshold=MODEL_DECISION_THRESHOLD, persist=True):
    window_days = max(1, min(int(window_days or 7), 30))
    model_threshold = max(40.0, min(float(model_threshold or MODEL_DECISION_THRESHOLD), 90.0))
    bundle = get_cached_quant_model_bundle(days=window_days, force=(report_kind == "manual"))
    rows = bundle.get("rows") or {}
    family = bundle.get("family") or {}
    comparison = simulate_policy_comparison(
        run_id=0,
        snapshot_rows=rows.get("entry_rows") or [],
        flow_rows=rows.get("flow_rows") or [],
        rule_settings=dict(PRESETS.get("balanced", {})),
        model_family=family,
        model_threshold=model_threshold,
    )
    regime_context = _current_regime_context(window_hours=48)
    report = build_edge_report(
        report_kind=report_kind,
        window_days=window_days,
        model_threshold=model_threshold,
        active_regime=regime_context.get("regime") or "neutral",
        comparison_summary=comparison.get("summary") or {},
        top_trades=[_serialize_comparison_trade(trade) for trade in (comparison.get("trades") or [])[:12]],
    )
    if not persist:
        with _edge_guard_cache_lock:
            _edge_guard_cache["built_at"] = 0.0
        return report

    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO quant_edge_reports (
                report_kind, window_days, model_threshold, active_regime, summary_json, policy_json
            ) VALUES (%s,%s,%s,%s,%s,%s)
            RETURNING id, generated_at
        """, (
            report["report_kind"],
            report["window_days"],
            report["model_threshold"],
            report["active_regime"],
            json.dumps(report["summary"]),
            json.dumps({
                "policies": report["policies"],
                "top_trades": report["top_trades"],
            }),
        ))
        inserted = cur.fetchone() or {}
        conn.commit()
        report["id"] = inserted.get("id")
        if inserted.get("generated_at"):
            report["generated_at"] = inserted["generated_at"].isoformat()
            report["summary"]["generated_at"] = report["generated_at"]
        with _edge_guard_cache_lock:
            _edge_guard_cache["built_at"] = 0.0
            _edge_guard_cache["state"] = None
        return report
    finally:
        db_return(conn)


def quant_edge_report_monitor():
    while True:
        try:
            rows = load_quant_edge_reports(limit=60, report_kind="scheduled")
            history = summarize_edge_report_history(rows)
            latest_by_window = {
                int(item.get("window_days") or 0): item
                for item in (history.get("latest_by_window") or [])
            }
            now = datetime.utcnow()
            for window_days, max_age_sec in EDGE_REPORT_AUTO_WINDOWS:
                latest = latest_by_window.get(window_days) or {}
                generated_at = latest.get("generated_at")
                stale = True
                if generated_at:
                    try:
                        generated_dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
                        if generated_dt.tzinfo is not None:
                            generated_dt = generated_dt.astimezone(timezone.utc).replace(tzinfo=None)
                        stale = (now - generated_dt).total_seconds() >= max_age_sec
                    except Exception:
                        stale = True
                if stale:
                    generate_quant_edge_report(window_days=window_days, report_kind="scheduled")
                    time.sleep(2)
        except Exception as e:
            print(f"[REPORT] quant edge monitor error: {e}", flush=True)
        time.sleep(600)


def get_quant_edge_guard_state(refresh_sec=EDGE_GUARD_REFRESH_SEC, force=False, focus_window_days=7):
    now = time.time()
    with _edge_guard_cache_lock:
        cached_state = _edge_guard_cache.get("state")
        if not force and cached_state and now - float(_edge_guard_cache.get("built_at") or 0) < refresh_sec:
            return cached_state
    rows = load_quant_edge_reports(limit=36)
    history = summarize_edge_report_history(rows, focus_window_days=focus_window_days)
    state = derive_edge_guard_state(history)
    state["history"] = history
    with _edge_guard_cache_lock:
        _edge_guard_cache["state"] = state
        _edge_guard_cache["built_at"] = now
    return state


def apply_edge_guard_to_settings(settings, guard_state):
    base = strip_auto_relax_state(dict(settings or {}))
    state = guard_state or {}
    adjusted = dict(base)
    size_multiplier = max(0.0, float(state.get("size_multiplier") or 1.0))
    risk_multiplier = max(0.0, float(state.get("risk_multiplier") or 1.0))
    drawdown_multiplier = max(0.1, float(state.get("drawdown_multiplier") or 1.0))
    adjusted["max_buy_sol"] = round(max(0.0, float(base.get("max_buy_sol") or 0.0) * size_multiplier), 4)
    adjusted["risk_per_trade_pct"] = round(max(0.0, float(base.get("risk_per_trade_pct") or 0.0) * risk_multiplier), 2)
    if base.get("drawdown_limit_sol") is not None:
        adjusted["drawdown_limit_sol"] = round(max(0.0, float(base.get("drawdown_limit_sol") or 0.0) * drawdown_multiplier), 4)
    max_positions_cap = state.get("max_positions_cap")
    if max_positions_cap is not None:
        adjusted["max_correlated"] = min(int(base.get("max_correlated") or 0), int(max_positions_cap))
    adjusted["_edge_guard"] = {
        "status": state.get("status") or "normal",
        "action_label": state.get("action_label") or "Normal risk",
        "reason": state.get("reason") or "",
        "allow_new_entries": bool(state.get("allow_new_entries", True)),
        "size_multiplier": size_multiplier,
        "risk_multiplier": risk_multiplier,
        "max_positions_cap": max_positions_cap,
        "source_generated_at": state.get("source_generated_at"),
    }
    return adjusted


def _set_backtest_job(run_id, payload):
    with _backtest_jobs_lock:
        _backtest_jobs[run_id] = payload


def _auto_tune_from_results(requested_by, summary, run_id):
    """After a backtest/optimization completes, automatically apply the best strategy settings."""
    strategies = summary.get("strategies") or {}
    per_strategy = summary.get("per_strategy") or {}
    if not strategies and not per_strategy:
        return

    # Score each strategy: weighted combo of win_rate and avg_pnl
    scored = []
    for strat_name, data in strategies.items():
        closed = data.get("trades_closed", data.get("closed", 0)) or 0
        if closed < 3:
            continue  # need minimum trades to be meaningful
        win_rate = float(data.get("win_rate", 0))
        avg_pnl = float(data.get("avg_pnl_pct", 0))
        # Score: prioritize win rate but reward profitability
        score = (win_rate * 0.6) + (max(avg_pnl, -50) * 0.4)
        best_settings = None
        if per_strategy.get(strat_name, {}).get("best_settings"):
            best_settings = per_strategy[strat_name]["best_settings"]
        scored.append((strat_name, score, win_rate, avg_pnl, closed, best_settings))

    if not scored:
        print(f"[AUTO-TUNE] run {run_id}: no strategies with >= 3 trades, skipping", flush=True)
        return

    scored.sort(key=lambda x: x[1], reverse=True)
    best_name, best_score, best_wr, best_pnl, best_trades, best_settings = scored[0]
    print(f"[AUTO-TUNE] run {run_id}: best strategy = {best_name} "
          f"(score={best_score:.1f}, win_rate={best_wr:.1f}%, avg_pnl={best_pnl:+.1f}%, trades={best_trades})", flush=True)

    # Build the settings to apply: start from preset, overlay optimization results
    # Check both live presets and V2 shadow-test presets
    apply_settings = dict(
        SHADOW_V2_PRESETS.get(best_name) or PRESETS.get(best_name) or PRESETS["balanced"]
    )
    if best_settings and isinstance(best_settings, dict):
        # Optimization found better filter values — apply them
        for key in BOT_OVERRIDE_FIELDS:
            field_name = key[0] if isinstance(key, tuple) else key
            if field_name in best_settings:
                apply_settings[field_name] = best_settings[field_name]
        print(f"[AUTO-TUNE] applying optimized filter values from sweep", flush=True)

    # Save to DB
    try:
        persist_bot_settings(
            user_id=requested_by,
            preset=best_name,
            run_mode="indefinite",
            duration=0,
            profit=0,
            settings=apply_settings,
        )
        print(f"[AUTO-TUNE] saved preset={best_name} to bot_settings for user {requested_by}", flush=True)
    except Exception as e:
        print(f"[AUTO-TUNE] failed to save settings: {e}", flush=True)
        return

    # Hot-reload into running bot if one exists
    with user_bots_lock:
        bot = user_bots.get(requested_by)
        if bot and bot.running:
            bot.settings.update(strip_auto_relax_state(apply_settings))
            bot.preset_name = normalize_preset_name(best_name)
            bot.auto_relax_level = 0
            print(f"[AUTO-TUNE] hot-reloaded {best_name} settings into running bot", flush=True)

    # Log summary for all strategies
    for strat_name, score, wr, pnl, trades, _ in scored:
        tag = " ← APPLIED" if strat_name == best_name else ""
        print(f"[AUTO-TUNE]   {strat_name}: score={score:.1f}, wr={wr:.1f}%, pnl={pnl:+.1f}%, trades={trades}{tag}", flush=True)


def _execute_backtest_run(run_id, requested_by, days, strategy_names, name, replay_mode="snapshot"):
    _set_backtest_job(run_id, {"status": "running", "started_at": time.time()})
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE backtest_runs
            SET status='running', started_at=NOW()
            WHERE id=%s
        """, (run_id,))
        conn.commit()
    finally:
        db_return(conn)

    try:
        all_presets = _all_known_presets()
        strategies = {
            strategy_name: dict(all_presets.get(strategy_name, PRESETS["balanced"]))
            for strategy_name in (strategy_names or list(all_presets.keys()))
            if strategy_name in all_presets
        }
        normalized_mode = "event_tape" if str(replay_mode).strip().lower() in {"event_tape", "tape", "events"} else "snapshot"
        if normalized_mode == "event_tape":
            rows = _load_backtest_event_tape(days=days)
            print(f"[BACKTEST] run {run_id}: loaded {len(rows)} events over {days}d for {len(strategies)} strategies", flush=True)
            if not rows:
                print(f"[BACKTEST] ⚠️ run {run_id}: ZERO events found in market_events / wallet_flow_events / liquidity_delta_events for the last {days} day(s). "
                      f"The bot must be running and scanning tokens to populate this data.", flush=True)
            result = simulate_event_tape_backtest(run_id, rows, strategies)
        else:
            rows = _load_backtest_snapshots(days=days, strategies=strategies)
            print(f"[BACKTEST] run {run_id}: loaded {len(rows)} snapshots over {days}d for {len(strategies)} strategies", flush=True)
            if not rows:
                print(f"[BACKTEST] ⚠️ run {run_id}: ZERO snapshots found in token_feature_snapshots for the last {days} day(s). "
                      f"The bot must be running and scanning tokens to populate this data.", flush=True)
            result = simulate_backtest(run_id, rows, strategies)
        trades = result["trades"]
        summary = result["summary"]
        # Diagnostic: log why trades may be zero
        print(f"[BACKTEST] run {run_id} complete: {len(trades)} trades from {summary.get('snapshots_processed', summary.get('events_processed', 0))} snapshots/events", flush=True)
        for strat_name, strat_summary in (summary.get("strategies") or {}).items():
            decisions = strat_summary.get("decisions", 0)
            passed = strat_summary.get("passed", 0)
            blocked = strat_summary.get("blocked", 0)
            print(f"[BACKTEST]   {strat_name}: {decisions} evaluated, {passed} passed, {blocked} blocked", flush=True)

        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM backtest_trades WHERE run_id=%s", (run_id,))
            if trades:
                psycopg2.extras.execute_batch(cur, """
                    INSERT INTO backtest_trades (
                        run_id, strategy_name, mint, name, opened_at, closed_at,
                        entry_price, exit_price, status, score, confidence,
                        max_upside_pct, max_drawdown_pct, realized_pnl_pct,
                        exit_reason, feature_json, decision_json
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, [trade.as_insert_tuple() for trade in trades], page_size=200)
            cur.execute("""
                UPDATE backtest_runs
                SET status='completed',
                    completed_at=NOW(),
                    snapshots_processed=%s,
                    tokens_processed=%s,
                    trades_closed=%s,
                    summary_json=%s,
                    error_text=NULL
                WHERE id=%s
            """, (
                summary.get("snapshots_processed", summary.get("events_processed", 0)),
                summary.get("tokens_processed", 0),
                summary.get("trades_closed", 0),
                json.dumps(summary),
                run_id,
            ))
            conn.commit()
        finally:
            db_return(conn)
        # Add diagnostic hints when no trades are generated
        if not trades:
            data_count = len(rows)
            diag_hints = []
            if data_count == 0:
                diag_hints.append(f"No market data found for the last {days} day(s). The bot's scanners must be running to record data.")
                diag_hints.append("Start the bot, let it scan for at least a few hours, then re-run the backtest.")
            else:
                diag_hints.append(f"{data_count} data points were loaded but all signals were blocked by entry filters.")
                for strat_name, strat_summary in (summary.get("strategies") or {}).items():
                    if strat_summary.get("blocked", 0) > 0 and strat_summary.get("passed", 0) == 0:
                        diag_hints.append(f"Strategy '{strat_name}': {strat_summary['blocked']} blocked, 0 passed. Entry thresholds may be too strict for historical data.")
                diag_hints.append("Try running a longer backtest window (14-30 days) to capture more varied market conditions.")
            summary["diagnostic_hints"] = diag_hints
        _set_backtest_job(run_id, {
            "status": "completed",
            "completed_at": time.time(),
            "summary": summary,
            "requested_by": requested_by,
            "name": name,
            "replay_mode": normalized_mode,
        })
        # Auto-tune: apply the best strategy from this backtest to live settings
        # Check if auto_promote is enabled for this user before proceeding
        _ap_conn = db()
        try:
            _ap_cur = _ap_conn.cursor()
            _ap_cur.execute("SELECT auto_promote FROM bot_settings WHERE user_id=%s", (requested_by,))
            _ap_row = _ap_cur.fetchone()
        finally:
            db_return(_ap_conn)
        if not _ap_row or not _ap_row.get("auto_promote"):
            print(f"[AUTO-TUNE] Skipped — auto_promote disabled for U{requested_by}", flush=True)
        else:
            try:
                _auto_tune_from_results(requested_by, summary, run_id)
            except Exception as tune_err:
                print(f"[AUTO-TUNE] error after backtest {run_id}: {tune_err}", flush=True)
    except Exception as e:
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE backtest_runs
                SET status='failed', completed_at=NOW(), error_text=%s
                WHERE id=%s
            """, (str(e), run_id))
            conn.commit()
        finally:
            db_return(conn)
        _set_backtest_job(run_id, {"status": "failed", "error": str(e), "completed_at": time.time()})
        print(f"[BACKTEST] run {run_id} failed: {e}", flush=True)


# ── Optimization: sweep filter thresholds to find best settings ─────────────

# The loosest possible values for each entry filter
_SWEEP_FLOOR = {
    "min_score": 5, "min_liq": 500, "min_mc": 500, "max_mc": 2000000,
    "min_vol": 100, "min_holder_growth_pct": 0, "min_narrative_score": 0,
    "min_green_lights": 0, "min_volume_spike_mult": 0, "max_age_min": 1440,
    "min_composite_score": 15, "min_confidence": 0.15,
}
_SWEEP_LEVEL_LABELS = ["Current", "Slightly Looser", "Medium Loose", "Very Loose", "Maximum Loose"]


def _generate_sweep_levels(base_preset, num_levels=5):
    """Generate progressively looser filter variations of a preset.
    Level 0 = current settings, Level N-1 = loosest possible."""
    levels = []
    for i in range(num_levels):
        pct = i / max(num_levels - 1, 1)  # 0.0 → 1.0
        variant = dict(base_preset)
        for key, loose_val in _SWEEP_FLOOR.items():
            current = float(base_preset.get(key, loose_val))
            if key in ("max_mc", "max_age_min"):
                # These INCREASE when loosened
                variant[key] = current + (loose_val - current) * pct
            else:
                # These DECREASE when loosened
                variant[key] = current - (current - loose_val) * pct
        # Round neatly
        for k in ("min_score", "min_liq", "min_mc", "max_mc", "min_vol",
                   "min_holder_growth_pct", "min_narrative_score", "min_green_lights",
                   "min_volume_spike_mult", "max_age_min", "min_composite_score"):
            if k in variant:
                variant[k] = round(float(variant[k]), 1)
        if "min_confidence" in variant:
            variant["min_confidence"] = round(float(variant["min_confidence"]), 3)
        levels.append(variant)
    return levels


def _score_sweep_result(summary, strategy_name):
    """Score a backtest result for ranking sweep levels.
    Higher = better. Balances win rate, profit, and trade count."""
    strat = (summary.get("strategies") or {}).get(strategy_name, {})
    closed = strat.get("trades_closed", 0)
    if closed == 0:
        return -999.0
    wins = strat.get("wins", 0)
    win_rate = (wins / closed) if closed else 0
    avg_pnl = strat.get("avg_pnl_pct", 0) or 0
    # Normalize trade count (more trades = more confidence, up to ~50)
    trade_norm = min(closed / 50.0, 1.0)
    return (win_rate * 40) + (avg_pnl * 0.4) + (trade_norm * 20)


def _execute_optimization_run(run_id, requested_by, days, strategy_names, name):
    """Run a sweep backtest: for each strategy, test 5 looseness levels and find the best."""
    _set_backtest_job(run_id, {"status": "running", "started_at": time.time()})
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE backtest_runs SET status='running', started_at=NOW() WHERE id=%s", (run_id,))
        conn.commit()
    finally:
        db_return(conn)

    try:
        all_presets = _all_known_presets()
        strategy_names = strategy_names or (list(CANONICAL_STRATEGIES) + SHADOW_V2_STRATEGIES)
        # Load snapshots ONCE — reuse for all 20 runs
        rows = _load_backtest_snapshots(days=days)
        print(f"[OPTIMIZE] run {run_id}: loaded {len(rows)} snapshots over {days}d", flush=True)

        if not rows:
            raise RuntimeError(f"No snapshots found for the last {days} day(s). Start the bot to collect data first.")

        best_trades_by_strat = {}   # only store trades from the winning level
        per_strategy = {}
        total_snapshots = 0
        total_tokens = 0

        for strat_name in strategy_names:
            if strat_name not in all_presets:
                continue
            base = dict(all_presets[strat_name])
            levels = _generate_sweep_levels(base, num_levels=5)
            level_results = []
            level_trades = {}  # level_idx -> trades list (kept temporarily)

            for level_idx, variant_settings in enumerate(levels):
                label = _SWEEP_LEVEL_LABELS[level_idx] if level_idx < len(_SWEEP_LEVEL_LABELS) else f"Level {level_idx}"
                # Run simulation with this single strategy at this looseness
                strat_dict = {strat_name: variant_settings}
                result = simulate_backtest(run_id, rows, strat_dict)
                summary = result["summary"]
                strat_summary = (summary.get("strategies") or {}).get(strat_name, {})

                if level_idx == 0:
                    total_snapshots = max(total_snapshots, summary.get("snapshots_processed", 0))
                    total_tokens = max(total_tokens, summary.get("tokens_processed", 0))

                closed = strat_summary.get("trades_closed", 0)
                wins = strat_summary.get("wins", 0)
                losses = closed - wins
                avg_pnl = strat_summary.get("avg_pnl_pct", 0) or 0
                win_rate = round((wins / closed * 100) if closed else 0, 1)
                score = _score_sweep_result(summary, strat_name)

                # Extract key filter values for display (compact — no full settings blob)
                filter_summary = {
                    "min_score": variant_settings.get("min_score"),
                    "min_liq": variant_settings.get("min_liq"),
                    "min_mc": variant_settings.get("min_mc"),
                    "max_mc": variant_settings.get("max_mc"),
                    "min_vol": variant_settings.get("min_vol"),
                    "min_holder_growth_pct": variant_settings.get("min_holder_growth_pct"),
                    "min_narrative_score": variant_settings.get("min_narrative_score"),
                    "min_volume_spike_mult": variant_settings.get("min_volume_spike_mult"),
                    "min_composite_score": variant_settings.get("min_composite_score", 40),
                    "min_confidence": variant_settings.get("min_confidence", 0.35),
                }

                level_results.append({
                    "level": level_idx,
                    "label": label,
                    "trades": closed,
                    "wins": wins,
                    "losses": losses,
                    "win_rate": win_rate,
                    "avg_pnl": round(avg_pnl, 2),
                    "score": round(score, 2),
                    "decisions": strat_summary.get("decisions", 0),
                    "passed": strat_summary.get("passed", 0),
                    "blocked": strat_summary.get("blocked", 0),
                    "blocker_counts": strat_summary.get("blocker_counts", {}),
                    "filter_summary": filter_summary,
                    "settings": variant_settings,
                })
                # Keep trades temporarily to pick the best later
                level_trades[level_idx] = result["trades"] or []
                print(f"[OPTIMIZE]   {strat_name} level {level_idx} ({label}): {closed} trades, {wins} wins, avg {avg_pnl:+.1f}%, score={score:.1f}", flush=True)

            # Pick best level — ONLY keep trades from the winning level
            best = max(level_results, key=lambda l: l["score"])
            best_trades_by_strat[strat_name] = level_trades.get(best["level"], [])
            per_strategy[strat_name] = {
                "levels": level_results,
                "best_level": best["level"],
                "best_label": best["label"],
                "best_settings": best["settings"],
                "best_score": best["score"],
                "best_trades": best["trades"],
                "best_win_rate": best["win_rate"],
                "best_avg_pnl": best["avg_pnl"],
            }
            print(f"[OPTIMIZE] {strat_name} best: level {best['level']} ({best['label']}), {best['trades']} trades, {best['win_rate']}% win rate, {best['avg_pnl']:+.1f}% avg", flush=True)

        # Build summary
        opt_summary = {
            "mode": "optimize",
            "snapshots_processed": total_snapshots,
            "tokens_processed": total_tokens,
            "trades_closed": sum(ps["best_trades"] for ps in per_strategy.values()),
            "per_strategy": per_strategy,
            "strategies": {},  # Compat with normal backtest format
        }
        # Also populate the standard strategies format for backward compat
        for sname, sdata in per_strategy.items():
            best_lvl = next((l for l in sdata["levels"] if l["level"] == sdata["best_level"]), {})
            opt_summary["strategies"][sname] = {
                "decisions": best_lvl.get("decisions", 0),
                "passed": best_lvl.get("passed", 0),
                "blocked": best_lvl.get("blocked", 0),
                "trades_opened": best_lvl.get("trades", 0),
                "trades_closed": best_lvl.get("trades", 0),
                "closed": best_lvl.get("trades", 0),
                "wins": best_lvl.get("wins", 0),
                "win_rate": best_lvl.get("win_rate", 0),
                "avg_pnl_pct": best_lvl.get("avg_pnl", 0),
                "blocker_counts": best_lvl.get("blocker_counts", {}),
            }

        # Save to DB — only trades from the BEST level per strategy (not all 20 levels)
        all_best_trades = []
        for strat_trades in best_trades_by_strat.values():
            all_best_trades.extend(strat_trades)
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM backtest_trades WHERE run_id=%s", (run_id,))
            if all_best_trades:
                psycopg2.extras.execute_batch(cur, """
                    INSERT INTO backtest_trades (
                        run_id, strategy_name, mint, name, opened_at, closed_at,
                        entry_price, exit_price, status, score, confidence,
                        max_upside_pct, max_drawdown_pct, realized_pnl_pct,
                        exit_reason, feature_json, decision_json
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, [t.as_insert_tuple() for t in all_best_trades], page_size=200)
            cur.execute("""
                UPDATE backtest_runs
                SET status='completed', completed_at=NOW(),
                    snapshots_processed=%s, tokens_processed=%s, trades_closed=%s,
                    summary_json=%s, error_text=NULL
                WHERE id=%s
            """, (
                total_snapshots, total_tokens,
                opt_summary["trades_closed"],
                json.dumps(opt_summary),
                run_id,
            ))
            conn.commit()
        finally:
            db_return(conn)

        _set_backtest_job(run_id, {
            "status": "completed", "completed_at": time.time(),
            "summary": opt_summary, "requested_by": requested_by, "name": name,
            "replay_mode": "optimize",
        })
        print(f"[OPTIMIZE] run {run_id} complete: {len(all_best_trades)} trades saved (best levels only)", flush=True)

        # Auto-tune: apply the best strategy from this optimization to live settings
        # Check if auto_promote is enabled for this user before proceeding
        _ap_conn = db()
        try:
            _ap_cur = _ap_conn.cursor()
            _ap_cur.execute("SELECT auto_promote FROM bot_settings WHERE user_id=%s", (requested_by,))
            _ap_row = _ap_cur.fetchone()
        finally:
            db_return(_ap_conn)
        if not _ap_row or not _ap_row.get("auto_promote"):
            print(f"[AUTO-TUNE] Skipped — auto_promote disabled for U{requested_by}", flush=True)
        else:
            try:
                _auto_tune_from_results(requested_by, opt_summary, run_id)
            except Exception as tune_err:
                print(f"[AUTO-TUNE] error after optimization {run_id}: {tune_err}", flush=True)

    except Exception as e:
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE backtest_runs SET status='failed', completed_at=NOW(), error_text=%s WHERE id=%s", (str(e), run_id))
            conn.commit()
        finally:
            db_return(conn)
        _set_backtest_job(run_id, {"status": "failed", "error": str(e), "completed_at": time.time()})
        print(f"[OPTIMIZE] run {run_id} failed: {e}", flush=True)


def _all_known_presets():
    """Return merged dict of live presets + V2 shadow-test presets."""
    merged = dict(PRESETS)
    merged.update(SHADOW_V2_PRESETS)
    return merged


def launch_backtest_run(requested_by, days=7, strategy_names=None, name="", replay_mode="snapshot"):
    all_presets = _all_known_presets()
    all_strategy_names = list(CANONICAL_STRATEGIES) + SHADOW_V2_STRATEGIES
    strategy_names = [s for s in (strategy_names or all_strategy_names) if s in all_presets]
    if not strategy_names:
        strategy_names = ["balanced"]
    is_optimize = str(replay_mode).strip().lower() in {"optimize", "find_best", "sweep"}
    normalized_mode = "optimize" if is_optimize else (
        "event_tape" if str(replay_mode).strip().lower() in {"event_tape", "tape", "events"} else "snapshot"
    )
    config = {
        "days": max(1, int(days)),
        "strategy_names": strategy_names,
        "name": (name or (f"Find Best Settings ({max(1, int(days))}d)" if is_optimize else f"{max(1, int(days))}d replay")).strip()[:80],
        "replay_mode": normalized_mode,
    }
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO backtest_runs (requested_by, name, status, days, replay_mode, strategy_filter, config_json)
            VALUES (%s,%s,'queued',%s,%s,%s,%s)
            RETURNING id
        """, (
            requested_by,
            config["name"],
            config["days"],
            normalized_mode,
            ",".join(strategy_names),
            json.dumps(config),
        ))
        run_id = int((cur.fetchone() or {}).get("id"))
        conn.commit()
    finally:
        db_return(conn)

    _set_backtest_job(run_id, {"status": "queued", "created_at": time.time(), **config})
    if is_optimize:
        threading.Thread(
            target=_execute_optimization_run,
            args=(run_id, requested_by, config["days"], strategy_names, config["name"]),
            daemon=True,
        ).start()
    else:
        threading.Thread(
            target=_execute_backtest_run,
            args=(run_id, requested_by, config["days"], strategy_names, config["name"], normalized_mode),
            daemon=True,
        ).start()
    return run_id, config


def _broadcast_signal(info):
    """Push info to market_feed and evaluate against all running bots."""
    info.setdefault("source", "scanner")
    mint = info["mint"]
    # Non-blocking intel: use cached data if available, kick off background refresh
    with _token_intel_lock:
        cached_intel = token_intel_payload(_token_intel_cache.get(mint) or {})
    if cached_intel:
        info["intel"] = cached_intel
        info["green_lights"] = cached_intel.get("green_lights", 0)
        info["deployer_score"] = cached_intel.get("deployer_score", 0)
        info["narrative_score"] = cached_intel.get("narrative_score", 0)
    # Always kick off background refresh (non-blocking)
    try:
        _intel_refresh_pool.submit(ensure_token_intel, mint, info, None, False)
    except Exception:
        pass
    market_feed.appendleft(info)
    with shadow_market_lock:
        shadow_market_queue.appendleft({
            "mint": mint,
            "price": info.get("price"),
            "ts": int(time.time()),
            "source": info.get("source") or "scanner",
        })
    record_price(mint, info["price"])
    _active_uid = None
    with user_bots_lock:
        for _uid, _bot in user_bots.items():
            if _bot.running:
                _active_uid = _uid
                break
    _record_market_intelligence(info, user_id=_active_uid)
    # Feed momentum sniper tracker so it can detect surges across scans
    _momentum_track(
        mint, info.get("price", 0), info.get("vol", 0), info.get("mc", 0),
        info.get("liq", 0), info.get("name", ""), info.get("age_min", 9999),
        info.get("source", "scanner"),
    )
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
                info["age_min"], effective_change, info.get("source")
            )
        except Exception as _be:
            print(f"[SCAN] evaluate_signal error: {_be}", flush=True)

# ── Momentum Sniper ──────────────────────────────────────────────────────────
# Detects coins soaring fast (price velocity) and auto-buys while still rising.
# Uses a rolling price window per token; triggers when price accelerates >X% in
# a short window with rising volume — the classic "it's pumping NOW" signal.
_momentum_price_history = {}  # mint -> deque([(ts, price, vol, mc), ...])
_momentum_price_lock = threading.Lock()
_MOMENTUM_MAX_TRACKED = 400
_MOMENTUM_WINDOW_SEC = 180       # 3-min window
_MOMENTUM_MIN_SURGE_PCT = 25     # must gain >25% in the window
_MOMENTUM_MIN_VOL_USD = 8000     # needs real volume, not just one trade
_MOMENTUM_MIN_LIQ_USD = 3000     # needs real pool
_MOMENTUM_MAX_MC = 800_000       # still small-cap
_MOMENTUM_MIN_MC = 2000          # not zero
_MOMENTUM_COOLDOWN = {}          # mint -> last_buy_ts (prevent repeat buys)
_MOMENTUM_COOLDOWN_SEC = 300     # 5 min cooldown per mint

def _momentum_track(mint, price, vol, mc, liq, name, age_min, source):
    """Record a price tick for momentum tracking. Returns surge info dict or None."""
    if not mint or not price or mc > _MOMENTUM_MAX_MC or mc < _MOMENTUM_MIN_MC:
        return None
    if liq < _MOMENTUM_MIN_LIQ_USD or vol < _MOMENTUM_MIN_VOL_USD:
        return None
    now = time.time()
    with _momentum_price_lock:
        hist = _momentum_price_history.get(mint)
        if hist is None:
            hist = deque(maxlen=60)
            _momentum_price_history[mint] = hist
        # Prune old ticks
        while hist and now - hist[0][0] > _MOMENTUM_WINDOW_SEC:
            hist.popleft()
        hist.append((now, price, vol, mc))
        samples = list(hist)
        # Prune entire dict if too large
        if len(_momentum_price_history) > _MOMENTUM_MAX_TRACKED:
            oldest_mints = sorted(
                _momentum_price_history.items(),
                key=lambda kv: kv[1][-1][0] if kv[1] else 0,
            )[:len(_momentum_price_history) - _MOMENTUM_MAX_TRACKED]
            for m, _ in oldest_mints:
                _momentum_price_history.pop(m, None)
    if len(samples) < 3:
        return None
    earliest_price = samples[0][1]
    if earliest_price <= 0:
        return None
    surge_pct = ((price - earliest_price) / earliest_price) * 100
    if surge_pct < _MOMENTUM_MIN_SURGE_PCT:
        return None
    # Volume should be increasing across the window too
    first_half_vol = sum(s[2] for s in samples[:len(samples) // 2]) / max(1, len(samples) // 2)
    second_half_vol = sum(s[2] for s in samples[len(samples) // 2:]) / max(1, len(samples) - len(samples) // 2)
    vol_rising = second_half_vol >= first_half_vol * 0.8  # allow slight dip
    if not vol_rising:
        return None
    # Check cooldown
    if now - _MOMENTUM_COOLDOWN.get(mint, 0) < _MOMENTUM_COOLDOWN_SEC:
        return None
    _MOMENTUM_COOLDOWN[mint] = now
    # Clean up old cooldowns
    if len(_MOMENTUM_COOLDOWN) > 1000:
        stale = [m for m, ts in _MOMENTUM_COOLDOWN.items() if now - ts > 600]
        for m in stale:
            _MOMENTUM_COOLDOWN.pop(m, None)
    window_sec = now - samples[0][0]
    return {
        "surge_pct": round(surge_pct, 1),
        "window_sec": round(window_sec, 0),
        "earliest_price": earliest_price,
        "current_price": price,
        "vol_rising": vol_rising,
        "samples": len(samples),
    }

def momentum_sniper():
    """
    Dedicated scanner: re-checks recent market_feed tokens every 8s for rapid surges.
    If a token has gained >25% in the last 3 minutes with rising volume, it broadcasts
    a high-priority BUY signal to all active bots immediately.
    """
    time.sleep(12)
    print("[MomentumSniper] 🚀 Started — watching for fast-soaring tokens", flush=True)
    while True:
        try:
            # Pull the most recent tokens from market_feed
            feed = list(market_feed)[:50]
            with user_bots_lock:
                active_bots = [b for b in user_bots.values() if b.running]
            if not active_bots or not feed:
                time.sleep(10)
                continue
            surges_found = 0
            for item in feed:
                mint = item.get("mint")
                if not mint:
                    continue
                price = float(item.get("price") or 0)
                vol = float(item.get("vol") or 0)
                mc = float(item.get("mc") or 0)
                liq = float(item.get("liq") or 0)
                name = item.get("name") or "Unknown"
                age_min = float(item.get("age_min") or 9999)
                # Also check for fresh price data from DexScreener if the feed entry is stale
                feed_age = time.time() - float(item.get("timestamp") or time.time())
                if feed_age > 120:
                    continue  # skip tokens not seen in the last 2 min
                surge = _momentum_track(mint, price, vol, mc, liq, name, age_min, "momentum-sniper")
                if not surge:
                    continue
                surges_found += 1
                surge_pct = surge["surge_pct"]
                window_sec = surge["window_sec"]
                print(
                    f"[MomentumSniper] 🔥 SURGE DETECTED: {name} +{surge_pct:.0f}% in {window_sec:.0f}s | "
                    f"MC:${mc:,.0f} Vol:${vol:,.0f} Liq:${liq:,.0f}",
                    flush=True,
                )
                # Broadcast to all running bots with boosted change signal
                change = max(float(item.get("change") or 0), surge_pct)
                for bot in active_bots:
                    if mint in bot.positions:
                        continue
                    bot.log_msg(
                        f"🔥 SURGE: {name} +{surge_pct:.0f}% in {window_sec:.0f}s — buying on momentum"
                    )
                    try:
                        bot.evaluate_signal(
                            mint, name, price, mc, vol, liq, age_min, change,
                            source="momentum-sniper",
                        )
                    except Exception as _e:
                        print(f"[MomentumSniper] eval error: {_e}", flush=True)
            if surges_found:
                print(f"[MomentumSniper] Cycle complete — {surges_found} surge(s) fired", flush=True)
            time.sleep(8)
        except Exception as e:
            print(f"[MomentumSniper] error: {e}", flush=True)
            time.sleep(10)

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
            def _scan_token(t):
                mint = t["tokenAddress"]
                try:
                    resp = dex_get(
                        f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                        timeout=5
                    )
                    if resp.status_code != 200:
                        return
                    pairs = resp.json().get("pairs") or []
                    if not pairs:
                        return
                    info = _process_dex_pair(pairs[0])
                    if info:
                        _broadcast_signal(info)
                except Exception:
                    pass
            with ThreadPoolExecutor(max_workers=6) as pool:
                pool.map(_scan_token, new_tokens)
            time.sleep(4)
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
                    def _scan2_token(item):
                        mint = item["tokenAddress"]
                        try:
                            r2 = dex_get(
                                f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                                timeout=5
                            )
                            if r2.status_code != 200:
                                return
                            pairs = r2.json().get("pairs") or []
                            if pairs:
                                info = _process_dex_pair(pairs[0])
                                if info:
                                    _broadcast_signal(info)
                        except Exception:
                            pass
                    with ThreadPoolExecutor(max_workers=6) as pool:
                        pool.map(_scan2_token, new)
                except Exception as e2:
                    print(f"[SCANNER2] {label} error: {e2}", flush=True)
            time.sleep(30 if got_429 else 5)
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


def _sniper_process_signature(sig, source_label, seen_signatures):
    if not sig or sig in seen_signatures:
        return False
    seen_signatures.add(sig)
    try:
        tx_result = rpc_call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}], timeout=5) or {}
        meta = (tx_result.get("meta") if isinstance(tx_result, dict) else {}) or {}
        post_bals = meta.get("postTokenBalances") or []
        mints_found = set()
        for pb in post_bals:
            mint = pb.get("mint") or ""
            if mint and mint != SOL_MINT and mint not in mints_found:
                mints_found.add(mint)
        # Emit all mints in parallel
        if mints_found:
            with ThreadPoolExecutor(max_workers=min(4, len(mints_found))) as pool:
                pool.map(lambda m: _sniper_emit_token(m, source_label=source_label), mints_found)
        return True
    except Exception as e:
        print(f"[Sniper] tx parse error for {sig[:8]}...: {e}", flush=True)
        return False


def _extract_mints_from_logs(logs):
    """Try to extract token mint addresses directly from transaction logs (avoids getTransaction call)."""
    import re
    mints = set()
    # PumpFun and Raydium logs often contain base58 mint addresses
    b58_pattern = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')
    for line in logs:
        if not line:
            continue
        line_lower = line.lower()
        # Look for mint-related log lines
        if any(kw in line_lower for kw in ("mint:", "token:", "initialize", "create")):
            matches = b58_pattern.findall(line)
            for m in matches:
                if len(m) >= 32 and m != SOL_MINT and m != PUMP_FUN and m != RAYDIUM_AMM:
                    mints.add(m)
    return mints


def _run_rpc_poll_sniper(tracked_programs, seen_signatures):
    print("[Sniper] Using RPC polling fallback", flush=True)
    while True:
        try:
            for program_id, label in tracked_programs:
                result = rpc_call("getSignaturesForAddress", [program_id, {"limit": 8}], timeout=8) or []
                if not isinstance(result, list):
                    continue
                for sig_info in reversed(result):
                    sig = (sig_info or {}).get("signature") or ""
                    _sniper_process_signature(sig, f"rpc-poll:{label}", seen_signatures)
                time.sleep(0.35)
            time.sleep(3)
        except Exception as e:
            print(f"[Sniper] RPC polling error: {e}", flush=True)
            time.sleep(10)


def _run_enhanced_ws_sniper():
    """Try Helius Enhanced WebSocket (transactionSubscribe) for fastest detection.
    Returns True if it ran successfully, False if plan doesn't support it."""
    if not HELIUS_API_KEY:
        return False

    import json as _json
    ws_url = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    seen_signatures = set()
    _SEEN_SIG_MAX = 20_000
    state = {"plan_blocked": False, "connected": False, "rate_limited": False}

    print("[Sniper] Attempting Enhanced WebSocket (transactionSubscribe)...", flush=True)

    try:
        import websocket as ws_lib

        def on_open(ws):
            state["connected"] = True
            # Subscribe to PumpFun transactions
            ws.send(json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method": "transactionSubscribe",
                "params": [{
                    "accountInclude": [PUMP_FUN],
                    "type": "all",
                }, {
                    "commitment": "processed",
                    "maxSupportedTransactionVersion": 0,
                }],
            }))
            # Subscribe to Raydium transactions
            ws.send(json.dumps({
                "jsonrpc": "2.0", "id": 2,
                "method": "transactionSubscribe",
                "params": [{
                    "accountInclude": [RAYDIUM_AMM],
                    "type": "all",
                }, {
                    "commitment": "processed",
                    "maxSupportedTransactionVersion": 0,
                }],
            }))
            print("[Sniper] ⚡ Enhanced WS subscribed (transactionSubscribe) — fastest mode", flush=True)

        def on_message(ws, message):
            try:
                data = json.loads(message)
                # Check for subscription errors
                if data.get("error"):
                    err = data["error"]
                    err_msg = str(err.get("message", ""))
                    if "business" in err_msg.lower() or err.get("code") == -32403:
                        state["plan_blocked"] = True
                        print(f"[Sniper] Enhanced WS blocked: {err_msg} — falling back", flush=True)
                        ws.close()
                        return
                    print(f"[Sniper] Enhanced WS error: {err}", flush=True)
                    return

                # Extract transaction data from notification
                params = (data.get("params") or {})
                result = (params.get("result") or {})
                sig = result.get("signature") or ""
                if not sig or sig in seen_signatures:
                    return
                seen_signatures.add(sig)

                # The enhanced WS includes the full transaction — extract mints directly
                tx = result.get("transaction") or {}
                meta = tx.get("meta") or {}
                post_bals = meta.get("postTokenBalances") or []
                logs = meta.get("logMessages") or []

                # Determine source
                source_label = "helius-ews"
                joined_logs = " ".join(str(l or "").lower() for l in logs)
                if PUMP_FUN.lower() in joined_logs:
                    source_label = "helius-ews:pumpfun"
                elif RAYDIUM_AMM.lower() in joined_logs:
                    source_label = "helius-ews:raydium"

                # Filter: only process relevant transactions
                if not any(kw in joined_logs for kw in ("initialize", "create", "buy", "swap")):
                    return

                # Extract mints from postTokenBalances (no extra RPC call needed!)
                mints = set()
                for pb in post_bals:
                    mint = pb.get("mint") or ""
                    if mint and mint != SOL_MINT:
                        mints.add(mint)

                if mints:
                    with ThreadPoolExecutor(max_workers=min(4, len(mints))) as pool:
                        pool.map(lambda m: _sniper_emit_token(m, source_label=source_label), mints)

                # Prune cache
                if len(seen_signatures) > _SEEN_SIG_MAX:
                    seen_signatures.clear()
            except Exception as e:
                print(f"[Sniper] Enhanced WS message error: {e}", flush=True)

        def on_error(ws, err):
            err_text = str(err or "")
            if "403" in err_text or "business" in err_text.lower() or "-32403" in err_text:
                state["plan_blocked"] = True
                print(f"[Sniper] Enhanced WS plan blocked — falling back to logsSubscribe", flush=True)
                try:
                    ws.close()
                except Exception:
                    pass
                return
            if "429" in err_text or "too many" in err_text.lower():
                state["rate_limited"] = True
                try:
                    ws.close()
                except Exception:
                    pass
                return
            print(f"[Sniper] Enhanced WS error: {err}", flush=True)

        def on_close(ws, *a):
            if not state["plan_blocked"]:
                print("[Sniper] Enhanced WS closed", flush=True)

        ws_app = ws_lib.WebSocketApp(
            ws_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        ws_app.run_forever(ping_interval=30, ping_timeout=15)

        if state["plan_blocked"]:
            return False  # Signal caller to fall back
        if state["rate_limited"]:
            time.sleep(30)
        return True  # Was running, just disconnected — retry
    except Exception as e:
        print(f"[Sniper] Enhanced WS failed: {e}", flush=True)
        return False


def helius_pool_sniper():
    """Listen for fresh Pump.fun and Raydium transactions and feed them into the main signal path.
    Tries Enhanced WS (transactionSubscribe) first for fastest detection,
    falls back to logsSubscribe, then RPC polling."""
    import json as _json

    # ── Try Enhanced WebSocket first (no getTransaction call needed) ──────
    if HELIUS_API_KEY:
        for _attempt in range(3):
            result = _run_enhanced_ws_sniper()
            if result is False:
                break  # Plan doesn't support it, fall back
            # True = was running but disconnected, retry
            time.sleep(2)
        if result is not False:
            return  # Enhanced WS ran successfully

    print("[Sniper] Using logsSubscribe (standard WebSocket)", flush=True)

    # ── Fall back to logsSubscribe ────────────────────────────────────────
    ws_url = HELIUS_RPC.replace("https://", "wss://").replace("http://", "ws://")
    # Also try the shared RPC WS if dedicated doesn't have WS support
    if HELIUS_API_KEY:
        ws_url = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    tracked_programs = [
        (PUMP_FUN, "pumpfun"),
        (RAYDIUM_AMM, "raydium"),
    ]
    seen_signatures = set()
    timeout_state = {"streak": 0}
    max_timeout_streak = 3
    reconnect_attempts = 0          # track consecutive reconnects for backoff
    _MAX_RECONNECT_BACKOFF = 120    # cap backoff at 2 minutes
    _SEEN_SIG_MAX = 20_000          # prune in-memory signature cache

    while True:
        try:
            import websocket as ws_lib
            wss = ws_url
            error_state = {"plan_blocked": False, "reason": "", "fallback": False,
                           "rate_limited": False}

            def on_open(ws):
                nonlocal reconnect_attempts
                reconnect_attempts = 0  # successful connect — reset backoff
                for idx, (program_id, label) in enumerate(tracked_programs, start=1):
                    ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": idx,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [program_id]},
                            {"commitment": "processed"},
                        ],
                    }))
                    print(f"[Sniper] Subscribed to {label} logs (processed)", flush=True)

            def on_message(ws, message):
                try:
                    timeout_state["streak"] = 0
                    data = json.loads(message)
                    value = (((data or {}).get("params") or {}).get("result") or {}).get("value") or {}
                    logs = value.get("logs") or []
                    sig = value.get("signature") or ""
                    if not any(token in (line or "").lower() for line in logs for token in ("initialize", "create", "buy", "swap")):
                        return
                    source_label = "helius-ws"
                    joined_logs = " ".join(str(line or "").lower() for line in logs)
                    if PUMP_FUN.lower() in joined_logs:
                        source_label = "helius-ws:pumpfun"
                    elif RAYDIUM_AMM.lower() in joined_logs:
                        source_label = "helius-ws:raydium"

                    # Try to extract mints from logs first (skip getTransaction if possible)
                    log_mints = _extract_mints_from_logs(logs)
                    if log_mints:
                        with ThreadPoolExecutor(max_workers=min(4, len(log_mints))) as pool:
                            pool.map(lambda m: _sniper_emit_token(m, source_label=source_label), log_mints)
                    else:
                        # Fall back to getTransaction for mint extraction
                        _sniper_process_signature(sig, source_label, seen_signatures)

                    # Prune seen_signatures to prevent unbounded memory growth
                    if len(seen_signatures) > _SEEN_SIG_MAX:
                        seen_signatures.clear()
                except Exception as e:
                    print(f"[Sniper] message error: {e}", flush=True)

            def on_error(ws, err):
                err_text = str(err or "")
                err_lower = err_text.lower()
                if (
                    "403 Forbidden" in err_text and (
                        "only available for business plans" in err_lower or
                        "code': -32403" in err_text or
                        '"code":-32403' in err_text.replace(" ", "")
                    )
                ):
                    error_state["plan_blocked"] = True
                    error_state["fallback"] = True
                    error_state["reason"] = "websocket stream not available on current Helius plan"
                    print(f"[Sniper] Plan gate hit: {error_state['reason']} — falling back to RPC polling", flush=True)
                    try:
                        ws.close()
                    except Exception:
                        pass
                    return
                # Rate-limited (429) — flag it so the outer loop backs off
                if "429" in err_text or "too many" in err_lower or "max usage" in err_lower:
                    error_state["rate_limited"] = True
                    print(f"[Sniper] WS rate-limited (429) — will back off before retry", flush=True)
                    try:
                        ws.close()
                    except Exception:
                        pass
                    return
                if "ping/pong timed out" in err_lower:
                    timeout_state["streak"] += 1
                    print(
                        f"[Sniper] WS keepalive timeout ({timeout_state['streak']}/{max_timeout_streak})",
                        flush=True,
                    )
                    if timeout_state["streak"] >= max_timeout_streak:
                        error_state["fallback"] = True
                        error_state["reason"] = "repeated websocket keepalive timeouts"
                        print(
                            f"[Sniper] Falling back to RPC polling after {timeout_state['streak']} timeout(s)",
                            flush=True,
                        )
                        try:
                            ws.close()
                        except Exception:
                            pass
                        return
                else:
                    timeout_state["streak"] = 0
                print(f"[Sniper] WS error: {err}", flush=True)

            def on_close(ws, *a):
                if error_state["fallback"] or error_state["rate_limited"]:
                    return
                print("[Sniper] WS closed — reconnecting...", flush=True)

            ws_app = ws_lib.WebSocketApp(
                wss,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws_app.run_forever(ping_interval=45, ping_timeout=20)
            if error_state["fallback"]:
                _run_rpc_poll_sniper(tracked_programs, seen_signatures)
                return
            # ── Exponential backoff on reconnect ──────────────────────────
            reconnect_attempts += 1
            if error_state["rate_limited"]:
                # 429: aggressive backoff — 30s, 60s, 120s
                backoff = min(_MAX_RECONNECT_BACKOFF, 30 * (2 ** (reconnect_attempts - 1)))
                print(f"[Sniper] 429 backoff: waiting {backoff}s before retry (attempt {reconnect_attempts})", flush=True)
                time.sleep(backoff)
            elif timeout_state["streak"] > 0:
                backoff = min(30, 2 * timeout_state["streak"])
                print(f"[Sniper] WS reconnect backoff {backoff}s", flush=True)
                time.sleep(backoff)
            else:
                # Normal disconnect: gentle backoff — 2s, 4s, 8s … 30s
                backoff = min(30, 2 * reconnect_attempts)
                if backoff > 2:
                    print(f"[Sniper] Reconnect backoff {backoff}s (attempt {reconnect_attempts})", flush=True)
                time.sleep(backoff)
        except ImportError:
            print("[Sniper] websocket-client not installed — using RPC polling fallback", flush=True)
            _run_rpc_poll_sniper(tracked_programs, seen_signatures)
            return
        except Exception as e:
            print(f"[Sniper] websocket path failed ({e}) — using RPC polling fallback", flush=True)
            _run_rpc_poll_sniper(tracked_programs, seen_signatures)
            return

def _start_bot_from_row(row):
    """Create a BotInstance from a DB row and start its thread. Returns (uid, bot) or raises."""
    uid = row["user_id"]
    kp = Keypair.from_bytes(base58.b58decode(decrypt_key(row["encrypted_key"])))
    preset_name = normalize_preset_name(row["preset"])
    settings = dict(PRESETS.get(preset_name, PRESETS["balanced"]))
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
    max_sol = PLAN_LIMITS.get(effective_plan(row["plan"], row.get("email", "")), PLAN_LIMITS["basic"])["max_buy_sol"]
    settings["max_buy_sol"] = min(settings["max_buy_sol"], max_sol)
    bot = BotInstance(uid, kp, settings,
        run_mode=row["run_mode"],
        run_duration_min=row["run_duration_min"],
        profit_target_sol=row["profit_target_sol"],
        preset_name=preset_name,
        execution_control=row)
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
            print(f"[AutoRestart] ✅ U{uid}: restored {len(saved_pos)} position(s)", flush=True)
    except Exception as _e:
        print(f"[AutoRestart] ⚠️ U{uid}: could not reload positions: {_e}", flush=True)
    t = threading.Thread(target=bot.run, daemon=True)
    t.start()
    bot.thread = t
    return uid, bot

def auto_restart_bots():
    """Monitor for dead bot threads (user must click Start Bot manually)."""
    time.sleep(5)  # wait for app to fully initialize
    # Phase 1: Clear stale is_running flags — bots only start when user clicks Start Bot
    try:
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE bot_settings SET is_running = 0 WHERE is_running = 1")
            cleared = cur.rowcount
            conn.commit()
        finally:
            db_return(conn)
        if cleared:
            print(f"[Startup] 🛑 Cleared is_running flag for {cleared} bot(s) — waiting for manual start", flush=True)
        else:
            print(f"[Startup] ✅ No stale bot flags to clear", flush=True)
    except Exception as e:
        print(f"[Startup] error clearing bot flags: {e}", flush=True)

    # Phase 2: Dead-thread watchdog — check every 60s for crashed bot threads
    while True:
        try:
            time.sleep(60)
            with user_bots_lock:
                bots_snapshot = list(user_bots.items())
            for uid, bot in bots_snapshot:
                if not bot.running:
                    continue
                if bot.thread and not bot.thread.is_alive():
                    print(f"[Watchdog] 🔴 U{uid} thread died — attempting auto-restart", flush=True)
                    bot.log_msg("🔄 Bot thread crashed — auto-restarting...")
                    try:
                        t = threading.Thread(target=bot.run, daemon=True)
                        t.start()
                        bot.thread = t
                        bot.log_msg("✅ Auto-restart successful")
                    except Exception as _e:
                        bot.running = False
                        bot.log_msg(f"❌ Auto-restart failed: {_e}")
                        print(f"[Watchdog] ❌ U{uid} auto-restart failed: {_e}", flush=True)
        except Exception as e:
            print(f"[Watchdog] error: {e}", flush=True)
            time.sleep(30)

def warm_sender_connections():
    """Ping Helius Sender regional nodes every 30s to reduce cold-start latency."""
    api_key = get_helius_api_key()
    qs = f"?api-key={api_key}" if api_key else ""
    endpoints = [
        f"https://slc-sender.helius-rpc.com/fast{qs}",
        f"https://ewr-sender.helius-rpc.com/fast{qs}",
    ]
    while True:
        for url in endpoints:
            try:
                _http_session.get(url.replace("/fast", "/ping"), timeout=4)
            except Exception as _e:
                pass  # silent — just warming connections
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


# cleanup registered via atexit in _graceful_shutdown_cleanup below

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


def _self_ping_keepalive():
    """Ping our own /health endpoint every 4 minutes to prevent Railway from
    idling the service when there is no browser traffic (e.g. overnight)."""
    import gc
    _PING_INTERVAL = 240  # 4 minutes
    _MEM_LOG_INTERVAL = 900  # log memory every 15 min
    _last_mem_log = 0
    # Wait for server to be fully ready
    time.sleep(30)
    port = int(os.getenv("PORT", 5000))
    url = f"http://127.0.0.1:{port}/health"
    consecutive_failures = 0
    while True:
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                print(f"[KeepAlive] ⚠️ /health returned {resp.status_code} ({consecutive_failures}x)", flush=True)
        except Exception as e:
            consecutive_failures += 1
            print(f"[KeepAlive] ⚠️ ping failed ({consecutive_failures}x): {e}", flush=True)

        # Periodic memory monitoring
        now = time.time()
        if now - _last_mem_log > _MEM_LOG_INTERVAL:
            _last_mem_log = now
            try:
                import resource
                mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            except ImportError:
                # Windows / fallback
                try:
                    import psutil
                    mem_mb = psutil.Process().memory_info().rss / (1024 * 1024)
                except ImportError:
                    mem_mb = 0
            if mem_mb > 0:
                print(f"[KeepAlive] 📊 Memory: {mem_mb:.0f} MB | Threads: {threading.active_count()}", flush=True)
                # Force GC if memory is getting high (>450 MB)
                if mem_mb > 450:
                    gc.collect()
                    print(f"[KeepAlive] 🧹 Forced GC (memory was {mem_mb:.0f} MB)", flush=True)

        time.sleep(_PING_INTERVAL)


def _acquire_background_worker_lock():
    global _background_lock_conn
    if _background_lock_conn is not None:
        return True
    try:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (BACKGROUND_WORKER_LOCK_ID,))
            locked = (cur.fetchone() or [False])[0]
        if not locked:
            conn.close()
            return False
        _background_lock_conn = conn
        return True
    except Exception as e:
        print(f"[WARN] Background worker lock failed: {e}")
        return False


# ── Auto-prune DB to prevent disk exhaustion ──────────────────────────────────
_DB_PRUNE_INTERVAL = 600   # every 10 minutes
_DB_ROW_LIMITS = {
    # table: (max_rows, id_column)  — keep tight to prevent disk fill
    "shadow_decisions":       (2000, "id"),
    "wallet_flow_events":     (2000, "id"),
    "model_decisions":        (2000, "id"),
    "signal_explorer_log":    (2000, "id"),
    "market_events":          (2000, "id"),
    "token_feature_snapshots":(2000, "id"),
    "token_flow_snapshots":   (2000, "id"),
    "filter_log":             (1000, "id"),
    "liquidity_delta_events": (1000, "id"),
    "shadow_positions":       (5000, "id"),
    "token_intel":            (3000, "id"),
    "execution_events":       (2000, "id"),
}

# Time-based max ages — rows older than this are always deleted regardless of count
_DB_AGE_LIMITS = {
    # table: (age_interval_sql, timestamp_col)
    "shadow_decisions":       ("3 days",  "created_at"),
    "wallet_flow_events":     ("7 days",  "created_at"),
    "token_flow_snapshots":   ("7 days",  "created_at"),
    "filter_log":             ("3 days",  "ts"),
    "signal_explorer_log":    ("3 days",  "ts"),
    "market_events":          ("7 days",  "created_at"),
    "token_feature_snapshots":("7 days",  "created_at"),
    "liquidity_delta_events": ("7 days",  "created_at"),
    "shadow_positions":       ("14 days", "opened_at"),
    "execution_events":       ("7 days",  "created_at"),
}


def _run_db_prune():
    """Execute one full prune cycle: age-based then row-count-based cleanup, then VACUUM."""
    try:
        conn = db()
        try:
            cur = conn.cursor()
            total_deleted = 0

            # 1. Age-based pruning — delete rows older than N days regardless of count
            for table, (interval, ts_col) in _DB_AGE_LIMITS.items():
                try:
                    cur.execute(f"DELETE FROM {table} WHERE {ts_col} < NOW() - INTERVAL %s", (interval,))
                    deleted = cur.rowcount
                    total_deleted += deleted
                    if deleted > 0:
                        print(f"[DB-PRUNE] {table}: deleted {deleted} rows older than {interval}", flush=True)
                except Exception as te:
                    conn.rollback()
                    print(f"[DB-PRUNE] {table} age-prune error: {te}", flush=True)

            # 2. Row-count-based pruning — cap tables that don't have a ts column or still exceed limit
            for table, (limit, id_col) in _DB_ROW_LIMITS.items():
                try:
                    cur.execute(f"SELECT COUNT(*) as c FROM {table}")
                    count = cur.fetchone()["c"]
                    if count > limit:
                        cur.execute(
                            f"DELETE FROM {table} WHERE {id_col} NOT IN "
                            f"(SELECT {id_col} FROM {table} ORDER BY {id_col} DESC LIMIT %s)",
                            (limit,)
                        )
                        deleted = cur.rowcount
                        total_deleted += deleted
                        if deleted > 0:
                            print(f"[DB-PRUNE] {table}: capped {count} -> {limit} ({deleted} pruned)", flush=True)
                except Exception as te:
                    conn.rollback()
                    print(f"[DB-PRUNE] {table} count-prune error: {te}", flush=True)

            if total_deleted > 0:
                conn.commit()
                print(f"[DB-PRUNE] Total: {total_deleted} rows removed", flush=True)
            else:
                conn.commit()  # commit any partial work

        finally:
            db_return(conn)

        # VACUUM to reclaim dead tuple space (run outside pool connection)
        try:
            _vconn = psycopg2.connect(DATABASE_URL)
            _vconn.autocommit = True
            with _vconn.cursor() as _vc:
                _vc.execute("VACUUM ANALYZE")
            _vconn.close()
            if total_deleted > 0:
                print(f"[DB-PRUNE] VACUUM ANALYZE complete", flush=True)
        except Exception as _ve:
            print(f"[DB-PRUNE] VACUUM skipped: {_ve}", flush=True)

    except Exception as e:
        print(f"[DB-PRUNE] Error: {e}", flush=True)


def _auto_prune_db():
    """Background worker: prune large tables on startup then every 10 minutes."""
    # Run immediately on startup — don't wait for first interval
    print(f"[DB-PRUNE] Startup prune running...", flush=True)
    _run_db_prune()
    while True:
        time.sleep(_DB_PRUNE_INTERVAL)
        _run_db_prune()


_SHADOW_TUNE_INTERVAL = 3600  # auto-tune from shadow results every 1 hour
_last_shadow_tune_at = 0

# ---------- Coin evaluation: sweep all recorded tokens to find optimal filter thresholds ----------

# Map sweep feature names → bot setting names so optimized thresholds can be applied
_SWEEP_FEATURE_TO_SETTING = {
    "composite_score": ("min_score", int),
    "volume_spike_ratio": ("min_volume_spike_mult", float),
    "holder_growth_1h": ("min_holder_growth_pct", float),
    "confidence": ("min_confidence", float),
    "buy_sell_ratio": ("min_buy_sell_ratio", float),
    "smart_wallet_buys": ("min_smart_wallet_buys", int),
    "net_flow_sol": ("min_net_flow_sol", float),
    "unique_buyer_count": ("min_unique_buyers", int),
}

# Expanded threshold plan covering more granular values than the default
_TUNE_THRESHOLD_PLAN = {
    "composite_score": [30, 40, 50, 60, 70, 80],
    "confidence": [0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
    "buy_sell_ratio": [0.8, 1.0, 1.5, 2.0, 2.5, 3.0],
    "smart_wallet_buys": [0, 1, 2, 3, 4],
    "net_flow_sol": [0.0, 1.0, 2.0, 5.0, 10.0],
    "volume_spike_ratio": [1.0, 1.5, 2.0, 2.5, 3.0, 4.0],
    "holder_growth_1h": [10, 20, 30, 40, 50],
    "unique_buyer_count": [5, 10, 15, 20, 30],
}


def _evaluate_all_recorded_coins(days=7):
    """Load all recorded token data, label outcomes, sweep filter thresholds, and return optimized settings.

    Returns a dict with:
      - optimized_settings: dict of bot setting overrides derived from best sweep thresholds
      - sweep_results: full sweep results for logging
      - feature_edges: top features separating winners from rugs
      - outcome_summary: counts of each outcome label
    """
    try:
        model_rows = _load_quant_model_rows(days=days)
        token_rows = model_rows.get("token_rows") or []
        entry_rows = model_rows.get("entry_rows") or []

        if len(token_rows) < 10:
            print(f"[COIN-EVAL] only {len(token_rows)} tokens recorded, need >= 10 for meaningful analysis", flush=True)
            return None

        # Step 1: label every token's outcome (winner / rug / volatile_winner / survivor / neutral)
        outcomes = build_outcome_labels(token_rows)
        if not outcomes:
            print(f"[COIN-EVAL] no outcome labels generated, skipping", flush=True)
            return None

        outcome_counts = {}
        for item in outcomes:
            outcome_counts[item["label"]] = outcome_counts.get(item["label"], 0) + 1
        total_labeled = len(outcomes)
        winner_count = outcome_counts.get("winner", 0) + outcome_counts.get("volatile_winner", 0)
        rug_count = outcome_counts.get("rug", 0)

        print(f"[COIN-EVAL] labeled {total_labeled} tokens: "
              f"{winner_count} winners, {rug_count} rugs, "
              f"{outcome_counts.get('survivor', 0)} survivors, "
              f"{outcome_counts.get('neutral', 0)} neutral", flush=True)

        if winner_count < 3 or rug_count < 3:
            print(f"[COIN-EVAL] need >= 3 winners AND >= 3 rugs for sweep (got {winner_count}w/{rug_count}r), skipping", flush=True)
            return None

        flow_rows = model_rows.get("flow_rows") or []

        # Step 2: sweep every feature × threshold to find optimal entry filters
        sweep_results = sweep_entry_filters(entry_rows, outcomes, threshold_plan=_TUNE_THRESHOLD_PLAN)

        # Step 3: identify top feature edges (which features best separate winners from rugs)
        feature_edges = summarize_feature_edges(entry_rows, outcomes, top_n=8)

        # Step 4: per-regime edge analysis — identify which features matter in each regime
        regime_edges = {}
        try:
            regime_edges = summarize_regime_edges(entry_rows, outcomes, flow_rows, top_n=6)
            if regime_edges:
                print(f"[COIN-EVAL] per-regime edge analysis:", flush=True)
                for regime_name, data in regime_edges.items():
                    if data.get("insufficient_data"):
                        print(f"[COIN-EVAL]   {regime_name}: {data['token_count']} tokens "
                              f"({data['winner_count']}w/{data['rug_count']}r) — not enough data", flush=True)
                    else:
                        top_edge = data["edges"][0] if data["edges"] else {}
                        print(f"[COIN-EVAL]   {regime_name}: {data['token_count']} tokens "
                              f"({data['winner_count']}w/{data['rug_count']}r) — "
                              f"top edge: {top_edge.get('label', '?')} ({top_edge.get('edge', 0):+.1f})", flush=True)
        except Exception as e:
            print(f"[COIN-EVAL] per-regime edge error: {e}", flush=True)

        # Step 5: extract the best threshold per feature and map to bot settings
        optimized_settings = {}
        best_by_feature = sweep_results.get("best_by_feature") or []

        for best in best_by_feature:
            feat_name = best.get("feature")
            threshold = best.get("threshold")
            selected = best.get("selected", 0)
            edge_score = best.get("edge_score", 0)
            winner_rate = best.get("winner_rate_pct", 0)
            rug_rate = best.get("rug_rate_pct", 0)

            # Only apply if the sweep had enough samples and a positive edge
            if selected < 5 or edge_score <= 0:
                continue

            # Only apply if rug rate dropped meaningfully (filter is actually helping)
            if rug_rate > 40:
                continue

            mapping = _SWEEP_FEATURE_TO_SETTING.get(feat_name)
            if mapping:
                setting_name, cast_fn = mapping
                try:
                    optimized_settings[setting_name] = cast_fn(threshold)
                    print(f"[COIN-EVAL]   {feat_name} >= {threshold} → {setting_name}={cast_fn(threshold)} "
                          f"(edge={edge_score:.1f}, wr={winner_rate:.0f}%, rr={rug_rate:.0f}%, n={selected})", flush=True)
                except Exception:
                    pass

        # Log the top feature edges for insight
        if feature_edges:
            print(f"[COIN-EVAL] top feature edges (winner vs rug):", flush=True)
            for edge in feature_edges[:5]:
                print(f"[COIN-EVAL]   {edge['label']}: winner_avg={edge['winner_avg']}, "
                      f"rug_avg={edge['rug_avg']}, edge={edge['edge']}", flush=True)

        # Determine current regime for context
        current_regime = "neutral"
        try:
            regime_ctx = _current_regime_context(window_hours=48)
            current_regime = regime_ctx.get("regime", "neutral")
        except Exception:
            pass

        return {
            "optimized_settings": optimized_settings,
            "sweep_results": sweep_results,
            "feature_edges": feature_edges,
            "regime_edges": regime_edges,
            "current_regime": current_regime,
            "outcome_summary": outcome_counts,
            "total_tokens": total_labeled,
        }

    except Exception as e:
        print(f"[COIN-EVAL] error evaluating recorded coins: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return None


def _analyze_cooldown_optimization(days=2):
    """Analyze shadow position timing to calculate optimal cooldown_min per strategy.

    Returns a dict with:
      - per_strategy: {strategy_name: {optimal_cooldown: minutes, regret_count: int, regret_pnl: float}}
      - summary: overall cooldown analysis results
    """
    try:
        conn = db()
        try:
            cur = conn.cursor()
            # Get all closed shadow positions in the last N days
            cur.execute("""
                SELECT strategy_name, mint, opened_at, closed_at, realized_pnl_pct, max_upside_pct
                FROM shadow_positions
                WHERE status='closed'
                  AND closed_at >= NOW() - INTERVAL '%d days'
                ORDER BY strategy_name, mint, closed_at
            """ % (days,))
            all_positions = cur.fetchall()
        finally:
            db_return(conn)

        if not all_positions:
            print(f"[COOLDOWN-OPT] no closed shadow positions in last {days} days", flush=True)
            return None

        # Group by strategy
        by_strategy = {}
        for pos in all_positions:
            sname = pos.get("strategy_name")
            if sname not in by_strategy:
                by_strategy[sname] = []
            by_strategy[sname].append(pos)

        results = {}

        for sname, positions in by_strategy.items():
            # Sort by closed_at for each strategy/mint combination
            position_dict = {}
            for pos in positions:
                mint = pos.get("mint")
                if mint not in position_dict:
                    position_dict[mint] = []
                position_dict[mint].append(pos)

            # For each mint, sort positions by closed time
            for mint in position_dict:
                position_dict[mint].sort(key=lambda x: x.get("closed_at") or 0)

            # Calculate time gaps between consecutive positions for the same mint
            # and track which would have been winners
            regret_count = 0  # Positions missed due to cooldown
            regret_pnl = 0.0  # PnL that would have been made
            gap_times = []  # Minutes between consecutive positions
            winning_gaps = []  # Gaps where next position was a winner

            for mint, sorted_pos in position_dict.items():
                for i in range(len(sorted_pos) - 1):
                    closed_pos = sorted_pos[i]
                    next_pos = sorted_pos[i + 1]

                    # Calculate time gap in minutes
                    closed_at = closed_pos.get("closed_at")
                    next_opened_at = next_pos.get("opened_at")

                    if closed_at and next_opened_at:
                        from datetime import datetime
                        gap_min = (next_opened_at - closed_at).total_seconds() / 60
                        gap_times.append(gap_min)

                        # Check if next position was a winner (>100% profit)
                        next_pnl = float(next_pos.get("realized_pnl_pct") or 0)
                        if next_pnl > 100:  # >100% gain = exponential growth
                            winning_gaps.append(gap_min)
                            # If gap is small, this was a regret (opportunity cost)
                            if gap_min < 15:  # If gap < 15 min, typical cooldown would block it
                                regret_count += 1
                                regret_pnl += next_pnl

            # Determine optimal cooldown
            optimal_cooldown = 5  # Default minimum
            if winning_gaps:
                # Optimal cooldown is the 80th percentile of winning gaps
                # This allows entry for most big winners while avoiding whipsaws
                winning_gaps.sort()
                percentile_idx = min(int(len(winning_gaps) * 0.80), len(winning_gaps) - 1)
                optimal_cooldown = max(2, int(winning_gaps[percentile_idx]))
            elif gap_times:
                # No winners yet, use median of all gaps as a safe default
                gap_times.sort()
                percentile_idx = len(gap_times) // 2
                optimal_cooldown = max(2, int(gap_times[percentile_idx] * 0.5))  # Use 50% of median

            results[sname] = {
                "optimal_cooldown": optimal_cooldown,
                "regret_count": regret_count,
                "regret_pnl": round(regret_pnl, 1),
                "gap_count": len(gap_times),
                "winner_gap_count": len(winning_gaps),
                "avg_gap": round(sum(gap_times) / len(gap_times), 1) if gap_times else 0,
            }

            print(f"[COOLDOWN-OPT] {sname}: optimal={optimal_cooldown}min, "
                  f"regrets={regret_count}, regret_pnl={regret_pnl:.1f}%, "
                  f"gaps={len(gap_times)}, winner_gaps={len(winning_gaps)}", flush=True)

        return {
            "per_strategy": results,
            "summary": {
                "strategies_analyzed": len(results),
                "total_regrets": sum(r["regret_count"] for r in results.values()),
                "total_regret_pnl": sum(r["regret_pnl"] for r in results.values()),
            }
        }

    except Exception as e:
        print(f"[COOLDOWN-OPT] error analyzing cooldown optimization: {e}", flush=True)
        return None


def _shadow_auto_tune():
    """Background worker: periodically evaluate closed shadow positions and auto-tune bot settings.

    Enhanced pipeline:
      1. Evaluate shadow position performance per strategy (pick the best preset)
      2. Evaluate ALL recorded coins to find optimal filter thresholds
      3. Merge optimized thresholds into the best strategy's settings before applying
    """
    global _last_shadow_tune_at
    time.sleep(300)  # wait 5 min after startup for data to accumulate
    while True:
        try:
            conn = db()
            try:
                cur = conn.cursor()
                # Get performance stats per strategy from recent closed shadow positions (last 48h)
                cur.execute("""
                    SELECT strategy_name,
                           COUNT(*) AS closed_trades,
                           SUM(CASE WHEN realized_pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
                           ROUND(AVG(realized_pnl_pct)::numeric, 2) AS avg_pnl,
                           ROUND(AVG(max_upside_pct)::numeric, 2) AS avg_upside,
                           ROUND(AVG(max_drawdown_pct)::numeric, 2) AS avg_drawdown
                    FROM shadow_positions
                    WHERE status='closed'
                      AND closed_at >= NOW() - INTERVAL '48 hours'
                    GROUP BY strategy_name
                    ORDER BY avg_pnl DESC NULLS LAST
                """)
                rows = cur.fetchall()
            finally:
                db_return(conn)

            if not rows:
                print(f"[SHADOW-TUNE] no closed shadow trades in last 48h, skipping", flush=True)
                time.sleep(_SHADOW_TUNE_INTERVAL)
                continue

            # Build a summary dict matching the format _auto_tune_from_results expects
            strategies = {}
            for row in rows:
                sname = row.get("strategy_name")
                closed = int(row.get("closed_trades") or 0)
                wins = int(row.get("wins") or 0)
                strategies[sname] = {
                    "trades_closed": closed,
                    "closed": closed,
                    "wins": wins,
                    "win_rate": round((wins / closed * 100) if closed else 0, 1),
                    "avg_pnl_pct": float(row.get("avg_pnl") or 0),
                    "avg_upside_pct": float(row.get("avg_upside") or 0),
                    "avg_drawdown_pct": float(row.get("avg_drawdown") or 0),
                }

            # --- NEW: Evaluate all recorded coins to find optimal filter thresholds ---
            print(f"[SHADOW-TUNE] running coin evaluation on all recorded tokens...", flush=True)
            coin_eval = _evaluate_all_recorded_coins(days=7)

            # --- NEW: Analyze cooldown optimization to reduce regret and catch exponential growers ---
            print(f"[SHADOW-TUNE] analyzing cooldown optimization across strategies...", flush=True)
            cooldown_opt = _analyze_cooldown_optimization(days=2)

            # Build per_strategy with optimized settings from coin evaluation
            per_strategy = {}
            if coin_eval and coin_eval.get("optimized_settings"):
                opt = coin_eval["optimized_settings"]
                print(f"[SHADOW-TUNE] coin evaluation produced {len(opt)} optimized filter overrides", flush=True)
                # Apply the optimized filters to ALL strategies so whichever wins gets them
                for sname in strategies:
                    per_strategy[sname] = {"best_settings": dict(opt)}
            else:
                print(f"[SHADOW-TUNE] coin evaluation returned no filter overrides (not enough data or no improvement)", flush=True)

            # Add cooldown optimizations to per_strategy settings
            if cooldown_opt and cooldown_opt.get("per_strategy"):
                for sname, cooldown_data in cooldown_opt["per_strategy"].items():
                    if sname not in per_strategy:
                        per_strategy[sname] = {}
                    if "best_settings" not in per_strategy[sname]:
                        per_strategy[sname]["best_settings"] = {}
                    optimal_cd = cooldown_data["optimal_cooldown"]
                    per_strategy[sname]["best_settings"]["cooldown_min"] = optimal_cd
                    print(f"[SHADOW-TUNE] {sname}: adjusted cooldown_min → {optimal_cd} min (regrets={cooldown_data['regret_count']})", flush=True)

            summary = {"strategies": strategies, "per_strategy": per_strategy}

            # Find ALL users who have auto_promote enabled
            conn = db()
            try:
                cur = conn.cursor()
                cur.execute("SELECT user_id FROM bot_settings WHERE auto_promote = 1")
                auto_promote_rows = cur.fetchall()
            finally:
                db_return(conn)

            auto_promote_user_ids = [r["user_id"] for r in auto_promote_rows if r.get("user_id")]

            print(f"[SHADOW-TUNE] evaluating {len(rows)} strategies from live shadow data (48h window)", flush=True)
            for sname, data in strategies.items():
                print(f"[SHADOW-TUNE]   {sname}: {data['trades_closed']} trades, "
                      f"{data['win_rate']}% win, {data['avg_pnl_pct']:+.1f}% avg pnl", flush=True)

            if coin_eval:
                oc = coin_eval.get("outcome_summary", {})
                print(f"[SHADOW-TUNE] coin outcomes: {coin_eval.get('total_tokens', 0)} tokens — "
                      f"winners={oc.get('winner', 0)+oc.get('volatile_winner', 0)}, "
                      f"rugs={oc.get('rug', 0)}, survivors={oc.get('survivor', 0)}, "
                      f"neutral={oc.get('neutral', 0)}", flush=True)
                print(f"[SHADOW-TUNE] current regime: {coin_eval.get('current_regime', 'unknown')}", flush=True)
                # Log per-regime edge insights
                for regime_name, rdata in (coin_eval.get("regime_edges") or {}).items():
                    if not rdata.get("insufficient_data") and rdata.get("edges"):
                        top = rdata["edges"][0]
                        print(f"[SHADOW-TUNE]   {regime_name} regime: {rdata['token_count']} tokens, "
                              f"top edge = {top['label']} ({top['edge']:+.1f})", flush=True)

            # Tune each auto_promote user independently
            if not auto_promote_user_ids:
                print(f"[AUTO-TUNE] Skipped — no users have auto_promote enabled", flush=True)
            else:
                print(f"[AUTO-TUNE] tuning {len(auto_promote_user_ids)} user(s) with auto_promote: {auto_promote_user_ids}", flush=True)
                for uid in auto_promote_user_ids:
                    try:
                        _auto_tune_from_results(uid, summary, f"shadow-live-{int(time.time())}")
                        print(f"[AUTO-TUNE] U{uid} tuned successfully", flush=True)
                    except Exception as tune_err:
                        print(f"[AUTO-TUNE] U{uid} tune failed: {tune_err}", flush=True)
            _last_shadow_tune_at = time.time()

        except Exception as e:
            print(f"[SHADOW-TUNE] error: {e}", flush=True)
        time.sleep(_SHADOW_TUNE_INTERVAL)


_background_worker_threads = {}  # {func_name: (thread, target_func)}

def _background_worker_watchdog():
    """Monitor all background worker threads and restart any that have died.
    This is the last line of defence — if any worker (including auto_restart_bots)
    crashes, this will bring it back."""
    time.sleep(120)  # let everything stabilize first
    while True:
        try:
            dead = []
            for name, (thread, target) in list(_background_worker_threads.items()):
                if not thread.is_alive():
                    dead.append((name, target))
            for name, target in dead:
                print(f"[WorkerWatchdog] 🔴 '{name}' died — restarting", flush=True)
                try:
                    t = threading.Thread(target=target, daemon=True, name=f"bg-{name}")
                    t.start()
                    _background_worker_threads[name] = (t, target)
                    print(f"[WorkerWatchdog] ✅ '{name}' restarted", flush=True)
                except Exception as e:
                    print(f"[WorkerWatchdog] ❌ '{name}' restart failed: {e}", flush=True)
        except Exception as e:
            print(f"[WorkerWatchdog] error: {e}", flush=True)
        time.sleep(90)  # check every 90s


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
            momentum_sniper,
            auto_restart_bots,
            warm_sender_connections,
            listing_scanner,
            process_listing_alerts,
            shadow_position_monitor,
            execution_tape_monitor,
            quant_edge_report_monitor,
            _prune_seen_tokens,
            _auto_prune_db,
            _shadow_auto_tune,
            _self_ping_keepalive,
        ]
        for target in worker_targets:
            t = threading.Thread(target=target, daemon=True, name=f"bg-{target.__name__}")
            t.start()
            _background_worker_threads[target.__name__] = (t, target)
        # Start the watchdog last — it monitors everything above
        wd = threading.Thread(target=_background_worker_watchdog, daemon=True, name="bg-worker-watchdog")
        wd.start()
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
    return "<h1>500 — Internal Server Error</h1>", 500


def _graceful_shutdown_cleanup():
    """atexit handler: stop all running bots and release the advisory lock."""
    print("[SHUTDOWN] 🛑 Stopping all bots...", flush=True)
    with user_bots_lock:
        bots = list(user_bots.values())
    stopped = 0
    for bot in bots:
        try:
            if bot.running:
                bot.running = False
                stopped += 1
        except Exception:
            pass
    _release_background_lock()
    print(f"[SHUTDOWN] ✅ {stopped} bot(s) stopped. Exiting.", flush=True)

atexit.register(_graceful_shutdown_cleanup)


@app.route("/healthz")
def _healthz():
    """Lightweight health check — no DB, no auth, instant response."""
    return "ok", 200


@app.before_request
def _before_request_security():
    if request.path == "/healthz":
        return
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
    """Lightweight health check — always 200 so Railway keeps the service alive.
    Use /health/db for a deep check that includes the database."""
    return jsonify({"status": "ok"}), 200

@app.route("/health/db")
def health_check_db():
    """Deep health check that verifies the database connection."""
    ok, error = db_health_check(retries=2)
    if ok:
        return jsonify({"status": "ok", "db": "ok"}), 200
    return jsonify({"status": "error", "db": error}), 503


@app.route("/favicon.ico")
def favicon():
    return send_file("static/icon-48.png", mimetype="image/png", max_age=86400)

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        # Update last_active timestamp
        user_id = session.get("user_id")
        try:
            conn = db()
            cur = conn.cursor()
            cur.execute("UPDATE users SET last_active = NOW() WHERE id = %s", (user_id,))
            conn.commit()
            db_return(conn)
        except Exception:
            pass
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
                    print(f"[ERROR] Signup: {e}", flush=True)
                    error = "An unexpected error occurred. Please try again."
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
            print(f"[ERROR] Login: {e}", flush=True)
            error = "An unexpected error occurred. Please try again."
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
            print(f"[SETUP] User {uid} set wallet: {pub}", flush=True)
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
            print(f"[ERROR] Setup invalid key: {e}", flush=True)
            error = "Invalid private key. Please check and try again."
    return Response(SETUP_HTML.replace("{{ERROR}}", html.escape(error)), mimetype="text/html")

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
            .replace("{{EMAIL}}", html.escape(user.get("email", "")))
            .replace("{{PLAN_LABEL}}", html.escape(plan_info.get("label", "")))
            .replace("{{UPGRADE_BTN}}", upgrade_btn)
            .replace("{{PLAN}}", html.escape(plan_info.get("label", "")))
            .replace("{{WALLET}}", html.escape(wallet.get("public_key", "")))
            .replace("{{PRESET}}", html.escape(normalize_preset_name((bsettings or {}).get("preset", "balanced"))))
            .replace("{{PLAN_NAME}}", html.escape(plan))
            .replace("{{PRESET_SETTINGS}}", preset_settings_json),
            mimetype="text/html"
        )
    except Exception as e:
        print(f"[ERROR] Dashboard route: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return Response("<h1>Dashboard Error</h1><p>An unexpected error occurred.</p>", status=500, mimetype="text/html")

# ── API ────────────────────────────────────────────────────────────────────────
@app.route("/api/state")
@login_required
def api_state():
    uid = session["user_id"]
    bot = user_bots.get(uid)
    pos_list = []
    # Grab wallet pubkey for balance lookup even when bot is stopped
    _state_pubkey = ""
    try:
        _conn = db()
        try:
            _c = _conn.cursor()
            _c.execute("SELECT public_key FROM wallets WHERE user_id=%s", (uid,))
            _w = _c.fetchone()
            if _w:
                _state_pubkey = _w["public_key"]
                print(f"[STATE] Fetched pubkey for uid {uid}: {_state_pubkey[:8]}...", flush=True)
            else:
                print(f"[WARN] No wallet found for uid {uid}", flush=True)
        finally:
            db_return(_conn)
    except Exception as e:
        print(f"[ERROR] Failed to fetch pubkey for uid {uid}: {e}", flush=True)
        pass
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
            cur.execute("SELECT preset, custom_settings, max_correlated, drawdown_limit_sol, profit_target_sol, session_budget_sol FROM bot_settings WHERE user_id=%s", (uid,))
            row = cur.fetchone()
            if row:
                preset_name = normalize_preset_name(row.get("preset", "balanced"))
                db_settings = dict(PRESETS.get(preset_name, PRESETS["balanced"]))
                if row.get("max_correlated") is not None:
                    db_settings["max_correlated"] = row["max_correlated"]
                # Session limits removed — drawdown_limit falls back to preset values
                # which may be auto-tuned by shadow trading optimization results
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
    telegram_chat_id = get_user_telegram_chat_id(uid)
    try:
        execution_bundle = bot.refresh_execution_control() if bot else resolve_user_execution_control(uid)
    except Exception:
        execution_bundle = {
            "selected_policy": "rules",
            "selected_policy_label": "Rules",
            "execution_mode": "live",
            "selection_source": "manual",
            "candidate_reason": "unavailable",
            "model_threshold": MODEL_DECISION_THRESHOLD,
            "control": load_user_execution_control(uid),
        }
    
    return jsonify({
        "running":    bot.running if bot else False,
        "preset":     preset_name,
        "balance":    get_wallet_balance_standalone(_state_pubkey) or (round(bot.sol_balance, 4) if bot else 0),
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
        "execution_control": execution_bundle,
        "telegram_chat_id": telegram_chat_id,
        "telegram_connected": bool(telegram_chat_id),
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
        # Skip custom_settings entirely — use pure preset values for shadow trading optimization
        # Custom settings could override tp1_mult, tp2_mult, stop_loss, etc. which breaks shadow trading
    max_sol  = PLAN_LIMITS.get(plan, PLAN_LIMITS["basic"])["max_buy_sol"]
    settings["max_buy_sol"] = min(settings["max_buy_sol"], max_sol)
    bot = BotInstance(
        uid, kp, settings,
        run_mode          = bs["run_mode"] if bs else "indefinite",
        run_duration_min  = bs["run_duration_min"] if bs else 0,
        profit_target_sol = bs["profit_target_sol"] if bs else 0,
        preset_name       = preset,
        execution_control = bs or {},
    )
    with user_bots_lock:
        user_bots[uid] = bot
    # Reload open positions from DB so they survive stopping/restarting
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
            print(f"[START] ✅ U{uid}: restored {len(saved_pos)} position(s)", flush=True)
    except Exception as _e:
        print(f"[START] ⚠️ U{uid}: could not reload positions: {_e}", flush=True)
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

@app.route("/api/shadow-trading-mode", methods=["POST"])
@login_required
def api_shadow_trading_mode():
    """Apply optimal shadow trading settings (V2 balanced preset)."""
    uid = session["user_id"]
    try:
        conn = db()
        try:
            cur = conn.cursor()
            # Set to balanced_v2 preset which is optimal for shadow trading
            cur.execute("UPDATE bot_settings SET preset=%s, custom_settings=NULL WHERE user_id=%s", ("balanced_v2", uid))
            conn.commit()
        finally:
            db_return(conn)
        # Also update running bot if it exists
        bot = user_bots.get(uid)
        if bot:
            settings = dict(SHADOW_V2_PRESETS.get("balanced_v2", SHADOW_V2_PRESETS["balanced_v2"]))
            bot.settings = settings
            bot.preset_name = "balanced_v2"
        return jsonify({"ok": True, "msg": "✅ Shadow trading mode activated — V2 balanced preset applied"})
    except Exception as e:
        print(f"[ERROR] Could not apply shadow trading mode: {e}", flush=True)
        return jsonify({"ok": False, "msg": "Failed to apply shadow trading mode"})

@app.route("/api/set-max-positions", methods=["POST"])
@login_required
def api_set_max_positions():
    uid = session["user_id"]
    data = request.get_json(force=True) or {}
    max_pos = int(data.get("max_positions", 5))
    max_pos = max(1, min(20, max_pos))  # Clamp between 1 and 20
    bot = user_bots.get(uid)
    if bot:
        bot.settings["max_correlated"] = max_pos
    # Save to database max_correlated column
    try:
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE bot_settings SET max_correlated=%s WHERE user_id=%s", (max_pos, uid))
            conn.commit()
        finally:
            db_return(conn)
    except Exception as e:
        print(f"[ERROR] Could not save max_positions: {e}", flush=True)
    return jsonify({"ok": True, "max_positions": max_pos})

@app.route("/api/set-session-budget", methods=["POST"])
@login_required
def api_set_session_budget():
    """Session budget/limits are now managed by preset optimization.
    This endpoint is kept for backward compat but no longer overrides preset drawdown limits."""
    uid = session["user_id"]
    data = request.get_json(force=True) or {}
    profit = float(data.get("profit_target_sol", 0) or 0)
    try:
        conn = db()
        try:
            cur = conn.cursor()
            # Only persist profit target — drawdown comes from preset/optimization
            cur.execute(
                "UPDATE bot_settings SET session_budget_sol=0, profit_target_sol=%s WHERE user_id=%s",
                (profit, uid)
            )
            conn.commit()
        finally:
            db_return(conn)
    except Exception as e:
        print(f"[ERROR] Could not save session budget: {e}", flush=True)
    bot = user_bots.get(uid)
    if bot:
        bot.profit_target = profit
        # drawdown_limit_sol stays from preset — not overridden
    return jsonify({"ok": True})

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
    current_execution_control = load_user_execution_control(uid)
    execution_control = normalize_execution_control({
        **current_execution_control,
        "execution_mode": data.get("execution_mode", current_execution_control.get("execution_mode")),
        "policy_mode": data.get("decision_policy", data.get("policy_mode", current_execution_control.get("policy_mode"))),
        "model_threshold": data.get("model_threshold", current_execution_control.get("model_threshold")),
        "auto_promote": data.get("auto_promote", current_execution_control.get("auto_promote")),
        "auto_promote_window_days": data.get("auto_promote_window_days", current_execution_control.get("auto_promote_window_days")),
        "auto_promote_min_reports": data.get("auto_promote_min_reports", current_execution_control.get("auto_promote_min_reports")),
        "auto_promote_lock_minutes": data.get("auto_promote_lock_minutes", current_execution_control.get("auto_promote_lock_minutes")),
        "active_policy": data.get("active_policy", current_execution_control.get("active_policy")),
        "active_policy_source": current_execution_control.get("active_policy_source"),
        "active_policy_report_id": current_execution_control.get("active_policy_report_id"),
        "active_policy_updated_at": current_execution_control.get("active_policy_updated_at"),
        "auto_promote_locked_until": current_execution_control.get("auto_promote_locked_until"),
    })
    if execution_control["policy_mode"] != "auto":
        execution_control["active_policy"] = execution_control["policy_mode"]
        execution_control["active_policy_source"] = "manual"
        execution_control["active_policy_report_id"] = None
        execution_control["active_policy_updated_at"] = datetime.utcnow().isoformat()
        execution_control["auto_promote_locked_until"] = None
    overrides = build_bot_overrides(data)
    if preset == "custom":
        _, base_settings = load_user_effective_settings(uid)
        if not base_settings:
            base_settings = dict(PRESETS["custom"])
    else:
        base_settings = dict(PRESETS.get(preset, PRESETS["balanced"]))
    base_settings.update(overrides)
    persist_bot_settings(uid, preset, run_mode, duration, profit, base_settings)
    persist_user_execution_control(uid, execution_control)
    bot = user_bots.get(uid)
    replayed = 0
    if bot:
        bot.settings.update(base_settings)
        bot.preset_name       = preset
        bot.run_mode         = run_mode
        bot.run_duration_min = duration
        bot.profit_target    = profit
        bot.execution_control = execution_control
        bot.execution_control_checked_at = 0.0
        bot.execution_control_key = ""
        bot.refresh_execution_control(force=True)
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
        "execution_control": bot.execution_selection if bot else resolve_user_execution_control(uid),
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

@app.route("/api/paper-prices", methods=["POST"])
@login_required
def api_paper_prices():
    """Return LIVE prices for paper positions via DexScreener (not cached scanner feed)."""
    mints = (request.json or {}).get("mints", [])
    if not isinstance(mints, list) or len(mints) > 30:
        return jsonify({})
    wanted = set(mints)
    prices = {}
    # Always fetch live prices from DexScreener for paper trading accuracy
    if wanted:
        try:
            mint_str = ",".join(list(wanted)[:30])
            resp = dex_get(f"https://api.dexscreener.com/latest/dex/tokens/{mint_str}", timeout=6)
            if resp.status_code == 200:
                pairs = resp.json().get("pairs") or []
                for pair in pairs:
                    ba = pair.get("baseToken", {}).get("address", "")
                    if ba in wanted and ba not in prices:
                        p = float(pair.get("priceUsd") or 0)
                        if p > 0:
                            prices[ba] = {
                                "price": p,
                                "mc": float(pair.get("marketCap") or pair.get("fdv") or 0),
                                "name": pair.get("baseToken", {}).get("name", ""),
                                "symbol": pair.get("baseToken", {}).get("symbol", ""),
                            }
        except Exception:
            pass
    # Fallback: fill any mints DexScreener missed from scanner cache
    missing = wanted - set(prices.keys())
    if missing:
        for item in list(market_feed):
            m = item.get("mint")
            if m in missing and m not in prices:
                p = item.get("price", 0)
                if p and p > 0:
                    prices[m] = {
                        "price": p,
                        "mc": item.get("mc", 0),
                        "name": item.get("name", ""),
                        "symbol": item.get("symbol", ""),
                    }
    return jsonify(prices)

@app.route("/api/manual-buy", methods=["POST"])
@login_required
def api_manual_buy():
    uid  = session["user_id"]
    conn_check = db()
    try:
        cur_check = conn_check.cursor()
        cur_check.execute("SELECT plan, email FROM users WHERE id=%s", (uid,))
        u_check = cur_check.fetchone()
    finally:
        db_return(conn_check)
    if u_check and effective_plan(u_check["plan"], u_check.get("email")) == "free":
        return jsonify({"ok": False, "msg": "Quick Buy requires a Basic plan or higher. Upgrade at /subscribe/basic"})
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
    # get current price + liquidity from DexScreener
    try:
        resp_data = dex_get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                        timeout=5).json()
        pairs = resp_data.get("pairs") or []
        price = float(pairs[0].get("priceUsd") or 0) if pairs else 0
        liq = float((pairs[0].get("liquidity") or {}).get("usd") or 0) if pairs else 0
    except Exception as _e:
        print(f"[ERROR] {_e}", flush=True)
        price = 0
        liq = 0
    if not price:
        return jsonify({"ok": False, "msg": "Could not fetch price"})
    # Force buy — bypass edge guard / drawdown / cooldown blockers
    def _force_buy():
        try:
            bot._force_buy(mint, name, price, liq=liq)
        except Exception as e:
            print(f"[FORCE-BUY] error: {e}", flush=True)
    threading.Thread(target=_force_buy, daemon=True).start()
    return jsonify({"ok": True, "msg": f"Force buy triggered for {name}"})

@app.route("/api/force-buy", methods=["POST"])
@login_required
def api_force_buy():
    """Force buy a token — bypasses all filters. For user-submitted buys from Approved Coins."""
    uid = session["user_id"]
    data = request.get_json(force=True) or {}
    mint = (data.get("mint") or "").strip()
    name = (data.get("name") or "Unknown").strip()[:64]
    if not mint or len(mint) < 32 or len(mint) > 64:
        return jsonify({"ok": False, "msg": "Invalid mint address"})
    if not all(c in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz" for c in mint):
        return jsonify({"ok": False, "msg": "Invalid mint address format"})
    bot = user_bots.get(uid)
    if not bot or not bot.running:
        return jsonify({"ok": False, "msg": "Bot not running — start bot first"})
    if mint in bot.positions:
        return jsonify({"ok": False, "msg": "Already in position"})
    # Fetch live price from DexScreener
    try:
        resp = dex_get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=5)
        pairs = resp.json().get("pairs") or []
        pair = next((p for p in pairs if p.get("chainId") == "solana"), None)
        price = float(pair.get("priceUsd") or 0) if pair else 0
        liq = float((pair.get("liquidity") or {}).get("usd") or 0) if pair else 0
    except Exception as _e:
        print(f"[ERROR] Force buy price fetch: {_e}", flush=True)
        price = 0
        liq = 0
    if not price:
        return jsonify({"ok": False, "msg": "Could not fetch price"})
    bot.log_msg(f"⚡ FORCE BUY {name} — user-submitted (bypassing filters)")
    bot.log_filter(name, mint, True, "Force buy — user submitted")
    threading.Thread(
        target=bot.buy,
        args=(mint, name, price),
        kwargs={"liq": liq, "dev_wallet": None, "decision_context": {
            "execution_mode": "live",
            "selected_policy_label": "Force Buy",
            "buy_reason": "user force buy",
            "source_tag": "force_buy",
            "allow_trade": True,
        }},
        daemon=True,
    ).start()
    return jsonify({"ok": True, "msg": f"⚡ Force buy sent for {name}"})

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


@app.route("/api/evaluation-feed")
@login_required
def api_evaluation_feed():
    """Rich evaluation feed for the Scan tab — pulls from PostgreSQL with full intel."""
    uid = session["user_id"]
    limit = min(int(request.args.get("limit", 60)), 200)
    filter_mode = request.args.get("filter", "all")  # all, passed, rejected
    conn = db()
    try:
        cur = conn.cursor()
        # Main evaluation entries from signal_explorer_log
        where_clause = "WHERE user_id=%s"
        params = [uid]
        if filter_mode == "passed":
            where_clause += " AND passed=1"
        elif filter_mode == "rejected":
            where_clause += " AND passed=0"
        cur.execute(f"""
            SELECT id, mint, name, passed, reason, payload_json, ts
            FROM signal_explorer_log
            {where_clause}
            ORDER BY ts DESC
            LIMIT %s
        """, (*params, limit))
        rows = cur.fetchall()

        evaluations = []
        for row in rows:
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except Exception:
                payload = {}
            intel = payload.get("intel") or {}
            checklist = intel.get("checklist") or payload.get("checklist") or []
            ts_raw = row.get("ts")
            ts_str = ts_raw.isoformat() if hasattr(ts_raw, "isoformat") else str(ts_raw or "")
            evaluations.append({
                "id": row.get("id"),
                "mint": row.get("mint") or payload.get("mint", ""),
                "name": row.get("name") or payload.get("name", "?"),
                "passed": bool(row.get("passed")),
                "reason": row.get("reason") or payload.get("reason", ""),
                "reason_plain": _plain_reason(row.get("reason") or payload.get("reason", "")),
                "score": payload.get("score") if isinstance(payload.get("score"), (int, float)) else (payload.get("score", {}).get("total", 0) if isinstance(payload.get("score"), dict) else 0),
                "price": payload.get("price", 0),
                "mc": payload.get("mc", 0),
                "liq": payload.get("liq", 0),
                "vol": payload.get("vol", 0),
                "age_min": payload.get("age_min", 0),
                "change": payload.get("change", 0),
                "source": payload.get("source", ""),
                "ts": ts_str,
                "checklist": checklist[:3],
                "green_lights": intel.get("green_lights", 0),
                "threat_score": intel.get("threat_risk_score", 0),
                "narrative_score": intel.get("narrative_score", 0),
                "deployer_score": intel.get("deployer_score", 0),
                "whale_score": intel.get("whale_score", 0),
                "whale_action": intel.get("whale_action_score", 0),
                "holder_growth": intel.get("holder_growth_1h", 0),
                "volume_spike": intel.get("volume_spike_ratio", 0),
                "narrative_tags": intel.get("narrative_tags") or [],
                "deployer": (intel.get("deployer_wallet") or "")[:8],
                "smart_first10": intel.get("smart_wallet_first10", 0),
                "can_exit": intel.get("can_exit"),
            })

        # Pipeline stats
        cur.execute("SELECT COUNT(*) AS n FROM signal_explorer_log WHERE user_id=%s", (uid,))
        total = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute("SELECT COUNT(*) AS n FROM signal_explorer_log WHERE user_id=%s AND passed=1", (uid,))
        passed_count = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute("""
            SELECT reason, COUNT(*) AS cnt
            FROM signal_explorer_log
            WHERE user_id=%s AND passed=0 AND reason IS NOT NULL AND reason != ''
            GROUP BY reason ORDER BY cnt DESC LIMIT 5
        """, (uid,))
        top_rejects = [{"reason": r["reason"], "reason_plain": _plain_reason(r["reason"]), "count": r["cnt"]} for r in cur.fetchall()]

        # Recent approved coins with enriched intel
        cur.execute("""
            SELECT mint, name, payload_json, ts
            FROM signal_explorer_log
            WHERE user_id=%s AND passed=1
            ORDER BY ts DESC LIMIT 10
        """, (uid,))
        approved = []
        for row in cur.fetchall():
            try:
                p = json.loads(row.get("payload_json") or "{}")
            except Exception:
                p = {}
            intel = p.get("intel") or {}
            ts_raw = row.get("ts")
            approved.append({
                "mint": row.get("mint", ""),
                "name": row.get("name") or p.get("name", "?"),
                "score": p.get("score") if isinstance(p.get("score"), (int, float)) else (p.get("score", {}).get("total", 0) if isinstance(p.get("score"), dict) else 0),
                "price": p.get("price", 0),
                "mc": p.get("mc", 0),
                "liq": p.get("liq", 0),
                "vol": p.get("vol", 0),
                "change": p.get("change", 0),
                "age_min": p.get("age_min", 0),
                "green_lights": intel.get("green_lights", 0),
                "checklist": (intel.get("checklist") or p.get("checklist") or [])[:3],
                "narrative_tags": intel.get("narrative_tags") or [],
                "whale_score": intel.get("whale_score", 0),
                "deployer_score": intel.get("deployer_score", 0),
                "threat_score": intel.get("threat_risk_score", 0),
                "source": p.get("source", ""),
                "ts": ts_raw.isoformat() if hasattr(ts_raw, "isoformat") else str(ts_raw or ""),
            })
    finally:
        db_return(conn)

    return jsonify({
        "evaluations": evaluations,
        "approved": approved,
        "pipeline": {
            "total_evaluated": total,
            "total_passed": passed_count,
            "pass_rate": round(passed_count / total * 100, 1) if total > 0 else 0,
            "top_rejects": top_rejects,
        },
    })


# ══════════════════════════════════════════════════════════════════════════════
# QUANT DATA PLATFORM API ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/quant/overview")
@login_required
def api_quant_overview():
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM market_tokens")
        token_count = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute("SELECT COUNT(*) AS n FROM market_events")
        event_count = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute("SELECT COUNT(*) AS n FROM wallet_flow_events")
        wallet_flow_count = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute("SELECT COUNT(*) AS n FROM liquidity_delta_events")
        liquidity_delta_count = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute("SELECT COUNT(*) AS n FROM token_feature_snapshots")
        snapshot_count = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute("SELECT COUNT(*) AS n FROM token_flow_snapshots")
        flow_snapshot_count = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute("SELECT COUNT(*) AS n FROM shadow_decisions")
        decision_count = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute("SELECT COUNT(*) AS n FROM model_decisions")
        model_decision_count = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute("SELECT COUNT(*) AS n FROM quant_edge_reports")
        edge_report_count = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute("SELECT COUNT(*) AS n FROM shadow_positions WHERE status='open'")
        open_count = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute("SELECT COUNT(*) AS n FROM shadow_positions WHERE status='closed'")
        closed_count = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute("""
            SELECT strategy_name,
                   COUNT(*) AS trades,
                   ROUND(AVG(realized_pnl_pct)::numeric, 2) AS avg_pnl,
                   ROUND(MAX(realized_pnl_pct)::numeric, 2) AS best_pnl,
                   ROUND(MIN(realized_pnl_pct)::numeric, 2) AS worst_pnl,
                   ROUND(AVG(max_upside_pct)::numeric, 2) AS avg_upside,
                   ROUND(AVG(max_drawdown_pct)::numeric, 2) AS avg_drawdown
            FROM shadow_positions
            WHERE status='closed'
            GROUP BY strategy_name
            ORDER BY avg_pnl DESC NULLS LAST, trades DESC
        """)
        strategy_rows = cur.fetchall()
        cur.execute("""
            SELECT mint, name, strategy_name, status, entry_price, current_price, score, confidence,
                   opened_at, max_upside_pct, max_drawdown_pct
            FROM shadow_positions
            ORDER BY opened_at DESC
            LIMIT 12
        """)
        recent_positions = cur.fetchall()
        cur.execute("""
            SELECT mint, price, unique_buyer_count, unique_seller_count, smart_wallet_buys,
                   net_flow_sol, buy_sell_ratio, liquidity_drop_pct, threat_risk_score, can_exit, created_at
            FROM token_flow_snapshots
            ORDER BY created_at DESC
            LIMIT 20
        """)
        recent_flow_rows = cur.fetchall()
        cur.execute("""
            SELECT mint, wallet, side, sol_amount, token_amount, smart_wallet, source, observed_at
            FROM wallet_flow_events
            ORDER BY observed_at DESC NULLS LAST, created_at DESC
            LIMIT 12
        """)
        recent_execution_rows = cur.fetchall()
        cur.execute("""
            SELECT DISTINCT ON (sd.strategy_name, sd.mint)
                   sd.strategy_name,
                   sd.mint,
                   COALESCE(sd.name, mt.name, 'Unknown') AS name,
                   sd.passed,
                   sd.price,
                   sd.blocker_reasons_json AS blocker_reasons,
                   mt.first_price,
                   mt.peak_price,
                   mt.trough_price,
                   mt.last_price
            FROM shadow_decisions sd
            JOIN market_tokens mt ON mt.mint = sd.mint
            WHERE mt.first_price IS NOT NULL
            ORDER BY sd.strategy_name, sd.mint, sd.created_at ASC
            LIMIT 4000
        """)
        opportunity_rows = cur.fetchall()
    finally:
        db_return(conn)

    summaries = []
    for row in strategy_rows:
        trades = int(row.get("trades") or 0)
        avg_pnl = float(row.get("avg_pnl") or 0)
        summaries.append({
            "strategy_name": row.get("strategy_name"),
            "trades": trades,
            "avg_pnl_pct": avg_pnl,
            "best_pnl_pct": float(row.get("best_pnl") or 0),
            "worst_pnl_pct": float(row.get("worst_pnl") or 0),
            "avg_upside_pct": float(row.get("avg_upside") or 0),
            "avg_drawdown_pct": float(row.get("avg_drawdown") or 0),
        })
    flow_regime = summarize_flow_regime(recent_flow_rows)
    opportunity_map = summarize_opportunity_matrix(opportunity_rows)

    return jsonify({
        "dataset": {
            "tracked_tokens": token_count,
            "market_events": event_count,
            "wallet_flow_events": wallet_flow_count,
            "liquidity_delta_events": liquidity_delta_count,
            "feature_snapshots": snapshot_count,
            "flow_snapshots": flow_snapshot_count,
            "shadow_decisions": decision_count,
            "model_decisions": model_decision_count,
            "edge_reports": edge_report_count,
            "open_shadow_positions": open_count,
            "closed_shadow_positions": closed_count,
        },
        "flow_regime": flow_regime,
        "recent_flow": [{
            "mint": row.get("mint"),
            "price": float(row.get("price") or 0),
            "unique_buyer_count": int(row.get("unique_buyer_count") or 0),
            "unique_seller_count": int(row.get("unique_seller_count") or 0),
            "smart_wallet_buys": int(row.get("smart_wallet_buys") or 0),
            "net_flow_sol": float(row.get("net_flow_sol") or 0),
            "buy_sell_ratio": float(row.get("buy_sell_ratio") or 0),
            "liquidity_drop_pct": float(row.get("liquidity_drop_pct") or 0),
            "threat_risk_score": int(row.get("threat_risk_score") or 0),
            "can_exit": None if row.get("can_exit") is None else bool(row.get("can_exit")),
            "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
        } for row in recent_flow_rows[:8]],
        "recent_execution_tape": [{
            "mint": row.get("mint"),
            "wallet": row.get("wallet"),
            "side": row.get("side"),
            "sol_amount": float(row.get("sol_amount") or 0),
            "token_amount": float(row.get("token_amount") or 0),
            "smart_wallet": bool(row.get("smart_wallet")),
            "source": row.get("source"),
            "observed_at": row.get("observed_at").isoformat() if row.get("observed_at") else None,
        } for row in recent_execution_rows[:8]],
        "opportunity_map": opportunity_map,
        "strategy_summaries": summaries,
        "recent_positions": [{
            "mint": row.get("mint"),
            "name": row.get("name"),
            "strategy_name": row.get("strategy_name"),
            "status": row.get("status"),
            "entry_price": float(row.get("entry_price") or 0),
            "current_price": float(row.get("current_price") or 0),
            "score": float(row.get("score") or 0),
            "confidence": float(row.get("confidence") or 0),
            "opened_at": row.get("opened_at").isoformat() if row.get("opened_at") else None,
            "max_upside_pct": float(row.get("max_upside_pct") or 0),
            "max_drawdown_pct": float(row.get("max_drawdown_pct") or 0),
        } for row in recent_positions],
    })


@app.route("/api/quant/optimizer")
@login_required
def api_quant_optimizer():
    days = max(1, min(int(request.args.get("days") or 7), 30))
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT ON (s.mint)
                   s.mint,
                   COALESCE(mt.name, 'Unknown') AS name,
                   s.price,
                   s.feature_json,
                   s.created_at
            FROM token_feature_snapshots s
            JOIN market_tokens mt ON mt.mint = s.mint
            WHERE s.created_at >= NOW() - INTERVAL %s
              AND COALESCE(mt.first_price, 0) > 0
            ORDER BY s.mint, s.created_at ASC
            LIMIT 6000
        """, (f"{days} days",))
        snapshot_rows = cur.fetchall()
        cur.execute("""
            SELECT mint,
                   COALESCE(name, 'Unknown') AS name,
                   first_price,
                   peak_price,
                   trough_price,
                   last_price
            FROM market_tokens
            WHERE last_seen_at >= NOW() - INTERVAL %s
              AND COALESCE(first_price, 0) > 0
            ORDER BY last_seen_at DESC
            LIMIT 6000
        """, (f"{days} days",))
        token_rows = cur.fetchall()
        cur.execute("""
            SELECT mint, buy_sell_ratio, net_flow_sol, threat_risk_score,
                   liquidity_drop_pct, can_exit, regime_label, flow_json, created_at
            FROM token_flow_snapshots
            WHERE created_at >= NOW() - INTERVAL %s
            ORDER BY created_at ASC
            LIMIT 12000
        """, (f"{days} days",))
        flow_rows = cur.fetchall()
    finally:
        db_return(conn)

    outcomes = build_outcome_labels(token_rows)
    sweeps = sweep_entry_filters(snapshot_rows, outcomes)
    feature_edges = summarize_feature_edges(snapshot_rows, outcomes)

    # Per-regime edge analysis
    regime_edges_raw = summarize_regime_edges(snapshot_rows, outcomes, flow_rows, top_n=6)
    regime_edges = {}
    for regime_name, data in regime_edges_raw.items():
        regime_edges[regime_name] = {
            "token_count": data.get("token_count", 0),
            "winner_count": data.get("winner_count", 0),
            "rug_count": data.get("rug_count", 0),
            "insufficient_data": data.get("insufficient_data", True),
            "edges": data.get("edges", []),
        }

    label_counts = {}
    for row in outcomes:
        key = row.get("label") or "unknown"
        label_counts[key] = label_counts.get(key, 0) + 1

    regime_context = _current_regime_context(window_hours=48)
    top_outcomes = sorted(outcomes, key=lambda item: item.get("peak_return_pct") or 0, reverse=True)[:6]
    return jsonify({
        "window_days": days,
        "dataset": {
            "entry_snapshots": len(snapshot_rows),
            "labeled_tokens": len(outcomes),
            "label_counts": label_counts,
        },
        "best_thresholds": sweeps.get("best_by_feature") or [],
        "top_sweeps": (sweeps.get("all") or [])[:10],
        "feature_edges": feature_edges,
        "regime_edges": regime_edges,
        "current_regime": regime_context.get("regime", "neutral"),
        "top_outcomes": top_outcomes,
    })


@app.route("/api/quant/model")
@login_required
def api_quant_model():
    days = max(1, min(int(request.args.get("days") or 7), 30))
    mode = (request.args.get("mode") or "auto").strip().lower()
    bundle = get_cached_quant_model_bundle(days=days)
    rows = bundle.get("rows") or {}
    model_family = bundle.get("family") or {}
    regime_context = _current_regime_context(window_hours=48)
    active_regime = regime_context.get("regime") or "neutral"
    scored = score_recent_candidates_for_regime(
        rows.get("recent_rows") or [],
        model_family,
        active_regime=active_regime,
        mode=mode,
        top_n=8,
    )
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT mode, model_key, active_regime, mint, name, passed, threshold, model_score,
                   fallback_reason, driver_json, created_at
            FROM model_decisions
            ORDER BY created_at DESC
            LIMIT 16
        """)
        decision_rows = cur.fetchall()
    finally:
        db_return(conn)
    return jsonify({
        "window_days": days,
        "mode": mode,
        "regime_context": regime_context,
        "model_family": model_family,
        "selection": scored.get("selection") or {},
        "ranked_candidates": scored.get("ranked_candidates") or [],
        "recent_decisions": [{
            "mode": row.get("mode"),
            "model_key": row.get("model_key"),
            "active_regime": row.get("active_regime"),
            "mint": row.get("mint"),
            "name": row.get("name"),
            "passed": bool(row.get("passed")),
            "threshold": float(row.get("threshold") or 0),
            "model_score": float(row.get("model_score") or 0),
            "fallback_reason": row.get("fallback_reason"),
            "drivers": _json_load(row.get("driver_json"), []),
            "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
        } for row in decision_rows],
    })


@app.route("/api/quant/comparison")
@login_required
def api_quant_comparison():
    days = max(1, min(int(request.args.get("days") or 7), 30))
    model_threshold = max(40.0, min(float(request.args.get("threshold") or MODEL_DECISION_THRESHOLD), 90.0))
    bundle = get_cached_quant_model_bundle(days=days)
    rows = bundle.get("rows") or {}
    family = bundle.get("family") or {}
    balanced_settings = dict(PRESETS.get("balanced", {}))
    result = simulate_policy_comparison(
        run_id=0,
        snapshot_rows=rows.get("entry_rows") or [],
        flow_rows=rows.get("flow_rows") or [],
        rule_settings=balanced_settings,
        model_family=family,
        model_threshold=model_threshold,
    )
    top_trades = result.get("trades") or []
    return jsonify({
        "window_days": days,
        "model_threshold": model_threshold,
        "summary": result.get("summary") or {},
        "top_trades": [{
            "policy_name": trade.strategy_name,
            "mint": trade.mint,
            "name": trade.name,
            "entry_price": float(trade.entry_price or 0),
            "exit_price": float(trade.exit_price or 0),
            "realized_pnl_pct": float(trade.realized_pnl_pct or 0),
            "max_upside_pct": float(trade.max_upside_pct or 0),
            "max_drawdown_pct": float(trade.max_drawdown_pct or 0),
            "exit_reason": trade.exit_reason,
        } for trade in top_trades[:12]],
    })


@app.route("/api/quant/reports", methods=["GET", "POST"])
@login_required
def api_quant_reports():
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        days = max(1, min(int(data.get("days") or 7), 30))
        threshold = max(40.0, min(float(data.get("threshold") or MODEL_DECISION_THRESHOLD), 90.0))
        report = generate_quant_edge_report(
            window_days=days,
            report_kind="manual",
            model_threshold=threshold,
            persist=True,
        )
        return jsonify({"ok": True, "report": report, "guard_state": get_quant_edge_guard_state(force=True, focus_window_days=days)})

    days = max(1, min(int(request.args.get("days") or 7), 30))
    rows = load_quant_edge_reports(limit=36)
    if not rows:
        try:
            generate_quant_edge_report(window_days=days, report_kind="bootstrap", persist=True)
            rows = load_quant_edge_reports(limit=36)
        except Exception as e:
            print(f"[REPORT] bootstrap report generation failed: {e}", flush=True)
    summary = summarize_edge_report_history(rows, focus_window_days=days)
    summary["guard_state"] = get_quant_edge_guard_state(force=True, focus_window_days=days)
    return jsonify(summary)


@app.route("/api/quant/shadow-performance")
@login_required
def api_quant_shadow_performance():
    strategy = (request.args.get("strategy") or "").strip().lower()
    conn = db()
    try:
        cur = conn.cursor()
        sql = """
            SELECT strategy_name,
                   COUNT(*) AS closed_trades,
                   SUM(CASE WHEN realized_pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
                   ROUND(AVG(realized_pnl_pct)::numeric, 2) AS avg_pnl,
                   ROUND(AVG(max_upside_pct)::numeric, 2) AS avg_upside,
                   ROUND(AVG(max_drawdown_pct)::numeric, 2) AS avg_drawdown,
                   ROUND(MAX(realized_pnl_pct)::numeric, 2) AS best_trade,
                   ROUND(MIN(realized_pnl_pct)::numeric, 2) AS worst_trade,
                   ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY realized_pnl_pct))::numeric, 2) AS median_pnl
            FROM shadow_positions
            WHERE status='closed'
        """
        params = []
        if strategy:
            sql += " AND strategy_name=%s"
            params.append(strategy)
        sql += " GROUP BY strategy_name ORDER BY avg_pnl DESC NULLS LAST, closed_trades DESC"
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
        # Before/After optimization comparison
        # "before" = trades opened before the optimization deploy
        # "after" = trades opened after (uses NOW() as baseline on first deploy, then real data accumulates)
        optimize_cutoff = "2026-03-24 12:00:00"  # approximate time of optimization deploy
        for label, where_clause in [
            ("before", f"closed_at < '{optimize_cutoff}'"),
            ("after",  f"opened_at >= '{optimize_cutoff}'"),
        ]:
            cur.execute(f"""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN realized_pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
                       ROUND(AVG(realized_pnl_pct)::numeric, 2) AS avg_pnl,
                       ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY realized_pnl_pct))::numeric, 2) AS median_pnl,
                       ROUND(MAX(realized_pnl_pct)::numeric, 2) AS best,
                       ROUND(MIN(realized_pnl_pct)::numeric, 2) AS worst,
                       SUM(CASE WHEN tp1_hit = 1 THEN 1 ELSE 0 END) AS tp1_hits,
                       ROUND(AVG(max_upside_pct)::numeric, 2) AS avg_upside,
                       ROUND(AVG(max_drawdown_pct)::numeric, 2) AS avg_drawdown
                FROM shadow_positions
                WHERE status='closed' AND {where_clause}
            """)
            if label == "before":
                before_stats = cur.fetchone() or {}
            else:
                after_stats = cur.fetchone() or {}
    finally:
        db_return(conn)
    def _era_stats(s):
        t = int(s.get("total") or 0)
        w = int(s.get("wins") or 0)
        return {
            "total": t, "wins": w,
            "win_rate": round(w / t * 100, 1) if t else 0,
            "avg_pnl_pct": float(s.get("avg_pnl") or 0),
            "median_pnl_pct": float(s.get("median_pnl") or 0),
            "best_pct": float(s.get("best") or 0),
            "worst_pct": float(s.get("worst") or 0),
            "tp1_hits": int(s.get("tp1_hits") or 0),
            "avg_upside_pct": float(s.get("avg_upside") or 0),
            "avg_drawdown_pct": float(s.get("avg_drawdown") or 0),
        }
    return jsonify({
        "strategies": [{
            "strategy_name": row.get("strategy_name"),
            "closed_trades": int(row.get("closed_trades") or 0),
            "wins": int(row.get("wins") or 0),
            "win_rate": round((int(row.get("wins") or 0) / int(row.get("closed_trades") or 1)) * 100, 1) if int(row.get("closed_trades") or 0) else 0,
            "avg_pnl_pct": float(row.get("avg_pnl") or 0),
            "avg_upside_pct": float(row.get("avg_upside") or 0),
            "avg_drawdown_pct": float(row.get("avg_drawdown") or 0),
            "best_trade_pct": float(row.get("best_trade") or 0),
            "worst_trade_pct": float(row.get("worst_trade") or 0),
            "median_pnl_pct": float(row.get("median_pnl") or 0),
        } for row in rows],
        "before_optimization": _era_stats(before_stats),
        "after_optimization": _era_stats(after_stats),
    })


@app.route("/api/quant/shadow-equity")
@login_required
def api_quant_shadow_equity():
    """Cumulative PnL over time per strategy — for equity curve charts."""
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT strategy_name,
                   DATE_TRUNC('hour', closed_at) AS hour,
                   COUNT(*) AS trades,
                   ROUND(SUM(realized_pnl_pct)::numeric, 2) AS pnl_sum,
                   SUM(CASE WHEN realized_pnl_pct > 0 THEN 1 ELSE 0 END) AS wins
            FROM shadow_positions
            WHERE status='closed' AND closed_at IS NOT NULL
            GROUP BY strategy_name, DATE_TRUNC('hour', closed_at)
            ORDER BY hour
        """)
        rows = cur.fetchall()
    finally:
        db_return(conn)

    # Build cumulative equity per strategy
    equity = {}  # strategy -> [(hour, cum_pnl, trades, wins)]
    running = {}  # strategy -> cumulative pnl
    for row in rows:
        strat = row.get("strategy_name") or "unknown"
        pnl = float(row.get("pnl_sum") or 0)
        running[strat] = running.get(strat, 0) + pnl
        if strat not in equity:
            equity[strat] = []
        equity[strat].append({
            "hour": row["hour"].isoformat() if row.get("hour") else None,
            "trades": int(row.get("trades") or 0),
            "wins": int(row.get("wins") or 0),
            "period_pnl": pnl,
            "cumulative_pnl": round(running[strat], 2),
        })
    return jsonify(equity)


@app.route("/api/quant/shadow-activity")
@login_required
def api_quant_shadow_activity():
    """Detailed shadow trading activity feed — open positions + recent closed trades."""
    conn = db()
    try:
        cur = conn.cursor()
        # Open positions
        cur.execute("""
            SELECT id, strategy_name, mint, name, entry_price, current_price, peak_price, trough_price,
                   opened_at, max_upside_pct, max_drawdown_pct,
                   EXTRACT(EPOCH FROM (NOW() - opened_at)) / 60.0 AS age_min
            FROM shadow_positions
            WHERE status='open'
            ORDER BY opened_at DESC
            LIMIT 50
        """)
        open_rows = cur.fetchall()
        # Recent closed
        cur.execute("""
            SELECT id, strategy_name, mint, name, entry_price, current_price, peak_price, trough_price,
                   opened_at, closed_at, exit_reason, realized_pnl_pct, max_upside_pct, max_drawdown_pct,
                   EXTRACT(EPOCH FROM (closed_at - opened_at)) / 60.0 AS hold_min
            FROM shadow_positions
            WHERE status='closed' AND closed_at IS NOT NULL
            ORDER BY closed_at DESC
            LIMIT 50
        """)
        closed_rows = cur.fetchall()
        # Aggregate stats
        cur.execute("""
            SELECT COUNT(*) AS total_open FROM shadow_positions WHERE status='open'
        """)
        total_open = int((cur.fetchone() or {}).get("total_open") or 0)
        cur.execute("""
            SELECT COUNT(*) AS total_closed,
                   SUM(CASE WHEN realized_pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
                   ROUND(AVG(realized_pnl_pct)::numeric, 2) AS avg_pnl,
                   ROUND(MAX(realized_pnl_pct)::numeric, 2) AS best_pnl,
                   ROUND(MIN(realized_pnl_pct)::numeric, 2) AS worst_pnl,
                   ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY realized_pnl_pct)::numeric, 2) AS median_pnl
            FROM shadow_positions WHERE status='closed'
        """)
        agg = cur.fetchone() or {}
        # Stats excluding the single best trade (to show how concentrated returns are)
        cur.execute("""
            WITH ranked AS (
                SELECT realized_pnl_pct,
                       ROW_NUMBER() OVER (ORDER BY realized_pnl_pct DESC) AS rn
                FROM shadow_positions WHERE status='closed'
            )
            SELECT ROUND(AVG(realized_pnl_pct)::numeric, 2) AS avg_excl_best,
                   SUM(CASE WHEN realized_pnl_pct > 0 THEN 1 ELSE 0 END) AS wins_excl_best,
                   COUNT(*) AS count_excl_best
            FROM ranked WHERE rn > 1
        """)
        excl = cur.fetchone() or {}
    finally:
        db_return(conn)

    def fmt_pos(row, is_open=True):
        entry = float(row.get("entry_price") or 0)
        current = float(row.get("current_price") or 0)
        pnl = round(((current / entry) - 1) * 100, 2) if entry > 0 and current > 0 else 0
        base = {
            "id": row.get("id"),
            "strategy": row.get("strategy_name"),
            "mint": row.get("mint"),
            "name": row.get("name") or (row.get("mint") or "")[:8],
            "entry_price": entry,
            "current_price": current,
            "peak_price": float(row.get("peak_price") or 0),
            "trough_price": float(row.get("trough_price") or 0),
            "unrealized_pnl_pct": pnl if is_open else None,
            "realized_pnl_pct": float(row.get("realized_pnl_pct") or 0) if not is_open else None,
            "max_upside_pct": float(row.get("max_upside_pct") or 0),
            "max_drawdown_pct": float(row.get("max_drawdown_pct") or 0),
            "opened_at": row["opened_at"].isoformat() if row.get("opened_at") else None,
        }
        if is_open:
            base["age_min"] = round(float(row.get("age_min") or 0), 1)
        else:
            base["closed_at"] = row["closed_at"].isoformat() if row.get("closed_at") else None
            base["exit_reason"] = row.get("exit_reason")
            base["hold_min"] = round(float(row.get("hold_min") or 0), 1)
        return base

    total_closed = int(agg.get("total_closed") or 0)
    total_wins = int(agg.get("wins") or 0)
    return jsonify({
        "open_positions": [fmt_pos(r, True) for r in open_rows],
        "recent_closed": [fmt_pos(r, False) for r in closed_rows],
        "stats": {
            "total_open": total_open,
            "total_closed": total_closed,
            "total_wins": total_wins,
            "win_rate": round((total_wins / total_closed) * 100, 1) if total_closed > 0 else 0,
            "avg_pnl_pct": float(agg.get("avg_pnl") or 0),
            "median_pnl_pct": float(agg.get("median_pnl") or 0),
            "best_pnl_pct": float(agg.get("best_pnl") or 0),
            "worst_pnl_pct": float(agg.get("worst_pnl") or 0),
            "avg_excl_best_pct": float(excl.get("avg_excl_best") or 0),
            "wins_excl_best": int(excl.get("wins_excl_best") or 0),
            "disclaimer": "Shadow results are simulated. They do not include slippage, fees, or real execution constraints.",
        },
    })


@app.route("/api/quant/opportunity-settings", methods=["POST"])
@login_required
def api_quant_opportunity_settings():
    """Apply opportunity-hunter preset — optimized for catching large expansions."""
    user_id = session["user_id"]
    data = request.get_json(silent=True) or {}
    aggression = (data.get("aggression") or "aggressive").strip().lower()
    if aggression not in ("aggressive", "degen"):
        aggression = "aggressive"

    # Build opportunity-optimized settings from the base preset
    base = dict(PRESETS.get(aggression, PRESETS["aggressive"]))
    opportunity_overrides = {
        # ── Peak Plateau Mode: ride to the top, only exit on trailing stop ──
        "peak_plateau_mode": True,           # Enable progressive trailing exit
        "tp1_mult": 3.0,                     # Skim 25% at 3x to secure some profit
        "tp1_sell_pct": 0.25,                # Only sell 25% at TP1 — keep 75% riding
        "tp2_mult": 9999.0,                  # Effectively disabled — trailing stop handles exit
        "trail_pct": 0.30,                   # Wide 30% base trail (tightens progressively)
                                             #   3x+  → 25% trail
                                             #   5x+  → 20% trail
                                             #   10x+ → 15% trail
                                             #   20x+ → 12% trail
                                             #   50x+ → 10% trail — locks mega gains
        "stop_loss": 0.55,                   # 45% stop — protect capital on duds
        "time_stop_min": 360,                # 6 hours — moonshots need time to develop
        "max_age_min": 120,                  # Focus on fresh tokens with most upside
        "min_liq": max(base.get("min_liq", 500), 800),  # Minimum exit liquidity
        "max_mc": 800000,                    # Wide MC range for moonshot potential
        "min_score": 12,                     # Lower score floor — let volume/flow decide
        "min_volume_spike_mult": 2.5,        # Require strong volume surge (confirmation)
        "min_holder_growth_pct": 15,         # Loose holder growth — fresh tokens
        "offpeak_min_change": 25,            # Higher bar for offpeak to avoid noise
        "max_hot_change": 5000.0,            # DO NOT block hot runners — let 2000%+ through
        "nuclear_narrative_score": 50,       # Strong narrative = let it ride
        "max_correlated": max(base.get("max_correlated", 3), 5),
        "drawdown_limit_sol": base.get("drawdown_limit_sol", 0.8) * 2.0,
    }
    merged = {**base, **opportunity_overrides}
    merged["label"] = f"Opportunity Hunter ({aggression.title()})"
    merged["description"] = "Peak Plateau Rider — holds until momentum dies. Progressive trailing tightens as gains grow. No fixed TP2 cap."

    # Persist using the same function the settings tab uses
    _, current_settings = load_user_effective_settings(user_id)
    persist_bot_settings(user_id, "custom", "indefinite", 0, 0, merged)

    return jsonify({"ok": True, "preset": "custom", "settings": merged, "message": f"Opportunity Hunter ({aggression.title()}) settings saved. Restart bot to apply."})


@app.route("/api/quant/auto-tune-status")
@login_required
def api_quant_auto_tune_status():
    """Show current auto-tune state: what strategy is active, last tune time, shadow stats."""
    user_id = session["user_id"]
    preset_name, settings = load_user_effective_settings(user_id)

    conn = db()
    try:
        cur = conn.cursor()
        # Recent shadow performance per strategy (48h)
        cur.execute("""
            SELECT strategy_name,
                   COUNT(*) AS closed,
                   SUM(CASE WHEN realized_pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
                   ROUND(AVG(realized_pnl_pct)::numeric, 2) AS avg_pnl
            FROM shadow_positions
            WHERE status='closed' AND closed_at >= NOW() - INTERVAL '48 hours'
            GROUP BY strategy_name ORDER BY avg_pnl DESC NULLS LAST
        """)
        shadow_stats = [{
            "strategy": r["strategy_name"],
            "closed": int(r["closed"] or 0),
            "wins": int(r["wins"] or 0),
            "win_rate": round((int(r["wins"] or 0) / int(r["closed"] or 1)) * 100, 1),
            "avg_pnl": float(r["avg_pnl"] or 0),
        } for r in cur.fetchall()]
    finally:
        db_return(conn)

    # Include coin evaluation insight if available
    coin_eval_summary = None
    try:
        # Quick check — pull outcome counts from market_tokens without running full sweep
        cur2 = conn if not conn else None
        conn2 = db()
        try:
            cur2 = conn2.cursor()
            cur2.execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN COALESCE(first_price,0) > 0 AND peak_price / NULLIF(first_price,0) >= 2.2 THEN 1 ELSE 0 END) AS winners,
                       SUM(CASE WHEN COALESCE(first_price,0) > 0 AND (trough_price / NULLIF(first_price,0) - 1) <= -0.45 THEN 1 ELSE 0 END) AS rugs
                FROM market_tokens
                WHERE last_seen_at >= NOW() - INTERVAL '7 days'
                  AND COALESCE(first_price, 0) > 0
            """)
            cr = cur2.fetchone()
            if cr:
                coin_eval_summary = {
                    "total_tracked": int(cr.get("total", 0)),
                    "approx_winners": int(cr.get("winners", 0)),
                    "approx_rugs": int(cr.get("rugs", 0)),
                    "evaluation_active": True,
                }
        finally:
            db_return(conn2)
    except Exception:
        pass

    return jsonify({
        "active_preset": preset_name,
        "last_shadow_tune_at": _last_shadow_tune_at or None,
        "tune_interval_sec": _SHADOW_TUNE_INTERVAL,
        "shadow_48h_stats": shadow_stats,
        "coin_evaluation": coin_eval_summary,
    })


@app.route("/api/quant/coin-evaluation")
@login_required
def api_quant_coin_evaluation():
    """Run or return cached coin evaluation: outcome labels + sweep analysis for all recorded tokens."""
    days = int(request.args.get("days", 7))
    result = _evaluate_all_recorded_coins(days=days)
    if not result:
        return jsonify({"error": "Not enough data for coin evaluation", "min_tokens": 10, "min_winners": 3, "min_rugs": 3})

    # Format sweep results for the dashboard
    best_by_feature = result.get("sweep_results", {}).get("best_by_feature", [])
    top_sweeps = result.get("sweep_results", {}).get("all", [])[:20]
    feature_edges = result.get("feature_edges", [])

    # Format regime edges for dashboard (serialize cleanly)
    regime_edges = {}
    for regime_name, data in (result.get("regime_edges") or {}).items():
        regime_edges[regime_name] = {
            "token_count": data.get("token_count", 0),
            "winner_count": data.get("winner_count", 0),
            "rug_count": data.get("rug_count", 0),
            "insufficient_data": data.get("insufficient_data", True),
            "edges": data.get("edges", []),
        }

    return jsonify({
        "total_tokens": result.get("total_tokens", 0),
        "outcome_summary": result.get("outcome_summary", {}),
        "optimized_settings": result.get("optimized_settings", {}),
        "best_by_feature": best_by_feature,
        "top_sweeps": top_sweeps,
        "feature_edges": feature_edges,
        "regime_edges": regime_edges,
        "current_regime": result.get("current_regime", "neutral"),
    })


@app.route("/api/quant/shadow-decisions")
@login_required
def api_quant_shadow_decisions():
    strategy = (request.args.get("strategy") or "").strip().lower()
    passed = request.args.get("passed")
    mint = (request.args.get("mint") or "").strip()
    conn = db()
    try:
        cur = conn.cursor()
        sql = """
            SELECT strategy_name, mint, name, source, passed, score, confidence, price,
                   pass_reasons_json, blocker_reasons_json, feature_json, decision_json, created_at
            FROM shadow_decisions
            WHERE 1=1
        """
        params = []
        if strategy:
            sql += " AND strategy_name=%s"
            params.append(strategy)
        if mint:
            sql += " AND mint=%s"
            params.append(mint)
        if passed in {"0", "1"}:
            sql += " AND passed=%s"
            params.append(int(passed))
        sql += " ORDER BY created_at DESC LIMIT 120"
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    finally:
        db_return(conn)

    decisions = []
    for row in rows:
        try:
            features = json.loads(row.get("feature_json") or "{}")
        except Exception:
            features = {}
        try:
            decision_json = json.loads(row.get("decision_json") or "{}")
        except Exception:
            decision_json = {}
        try:
            pass_reasons = json.loads(row.get("pass_reasons_json") or "[]")
        except Exception:
            pass_reasons = []
        try:
            blocker_reasons = json.loads(row.get("blocker_reasons_json") or "[]")
        except Exception:
            blocker_reasons = []
        decisions.append({
            "strategy_name": row.get("strategy_name"),
            "mint": row.get("mint"),
            "name": row.get("name"),
            "source": row.get("source"),
            "passed": bool(row.get("passed")),
            "score": float(row.get("score") or 0),
            "confidence": float(row.get("confidence") or 0),
            "price": float(row.get("price") or 0),
            "pass_reasons": pass_reasons,
            "blocker_reasons": blocker_reasons,
            "features": features,
            "decision": decision_json,
            "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
        })
    return jsonify(decisions)


@app.route("/api/quant/backtests", methods=["GET", "POST"])
@login_required
def api_quant_backtests():
    uid = session["user_id"]
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        days = int(data.get("days") or 7)
        replay_mode = (data.get("replay_mode") or "snapshot").strip().lower()
        strategies = data.get("strategies") or (list(CANONICAL_STRATEGIES) + SHADOW_V2_STRATEGIES)
        if not isinstance(strategies, list):
            strategies = [str(strategies)]
        run_id, config = launch_backtest_run(
            requested_by=uid,
            days=days,
            strategy_names=[str(s).strip().lower() for s in strategies],
            name=(data.get("name") or "").strip(),
            replay_mode=replay_mode,
        )
        return jsonify({"ok": True, "run_id": run_id, "config": config})

    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, requested_by, name, status, days, replay_mode, strategy_filter, started_at, completed_at,
                   snapshots_processed, tokens_processed, trades_closed, summary_json, config_json,
                   error_text, created_at
            FROM backtest_runs
            WHERE requested_by=%s
            ORDER BY created_at DESC
            LIMIT 30
        """, (uid,))
        rows = cur.fetchall()
    finally:
        db_return(conn)

    runs = []
    for row in rows:
        try:
            summary = json.loads(row.get("summary_json") or "{}")
        except Exception:
            summary = {}
        try:
            config = json.loads(row.get("config_json") or "{}")
        except Exception:
            config = {}
        runs.append({
            "id": int(row.get("id") or 0),
            "requested_by": row.get("requested_by"),
            "name": row.get("name"),
            "status": row.get("status"),
            "days": int(row.get("days") or 0),
            "replay_mode": row.get("replay_mode") or config.get("replay_mode") or "snapshot",
            "strategy_filter": row.get("strategy_filter"),
            "started_at": row.get("started_at").isoformat() if row.get("started_at") else None,
            "completed_at": row.get("completed_at").isoformat() if row.get("completed_at") else None,
            "snapshots_processed": int(row.get("snapshots_processed") or 0),
            "tokens_processed": int(row.get("tokens_processed") or 0),
            "trades_closed": int(row.get("trades_closed") or 0),
            "summary": summary,
            "config": config,
            "error_text": row.get("error_text"),
            "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
        })
    return jsonify(runs)


@app.route("/api/quant/backtests/<int:run_id>")
@login_required
def api_quant_backtest_detail(run_id):
    uid = session["user_id"]
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, requested_by, name, status, days, replay_mode, strategy_filter, started_at, completed_at,
                   snapshots_processed, tokens_processed, trades_closed, summary_json, config_json,
                   error_text, created_at
            FROM backtest_runs
            WHERE id=%s
            LIMIT 1
        """, (run_id,))
        run_row = cur.fetchone()
        cur.execute("""
            SELECT strategy_name, mint, name, opened_at, closed_at, entry_price, exit_price,
                   status, score, confidence, max_upside_pct, max_drawdown_pct,
                   realized_pnl_pct, exit_reason
            FROM backtest_trades
            WHERE run_id=%s
            ORDER BY realized_pnl_pct DESC, closed_at DESC
            LIMIT 200
        """, (run_id,))
        trade_rows = cur.fetchall()
    finally:
        db_return(conn)

    if not run_row:
        return jsonify({"ok": False, "msg": "Backtest run not found"}), 404

    try:
        summary = json.loads(run_row.get("summary_json") or "{}")
    except Exception:
        summary = {}
    try:
        config = json.loads(run_row.get("config_json") or "{}")
    except Exception:
        config = {}

    return jsonify({
        "run": {
            "id": int(run_row.get("id") or 0),
            "requested_by": run_row.get("requested_by"),
            "name": run_row.get("name"),
            "status": run_row.get("status"),
            "days": int(run_row.get("days") or 0),
            "replay_mode": run_row.get("replay_mode") or config.get("replay_mode") or "snapshot",
            "strategy_filter": run_row.get("strategy_filter"),
            "started_at": run_row.get("started_at").isoformat() if run_row.get("started_at") else None,
            "completed_at": run_row.get("completed_at").isoformat() if run_row.get("completed_at") else None,
            "snapshots_processed": int(run_row.get("snapshots_processed") or 0),
            "tokens_processed": int(run_row.get("tokens_processed") or 0),
            "trades_closed": int(run_row.get("trades_closed") or 0),
            "summary": summary,
            "config": config,
            "error_text": run_row.get("error_text"),
            "created_at": run_row.get("created_at").isoformat() if run_row.get("created_at") else None,
        },
        "trades": [{
            "strategy_name": row.get("strategy_name"),
            "mint": row.get("mint"),
            "name": row.get("name"),
            "opened_at": row.get("opened_at").isoformat() if row.get("opened_at") else None,
            "closed_at": row.get("closed_at").isoformat() if row.get("closed_at") else None,
            "entry_price": float(row.get("entry_price") or 0),
            "exit_price": float(row.get("exit_price") or 0),
            "status": row.get("status"),
            "score": float(row.get("score") or 0),
            "confidence": float(row.get("confidence") or 0),
            "max_upside_pct": float(row.get("max_upside_pct") or 0),
            "max_drawdown_pct": float(row.get("max_drawdown_pct") or 0),
            "realized_pnl_pct": float(row.get("realized_pnl_pct") or 0),
            "exit_reason": row.get("exit_reason"),
        } for row in trade_rows],
    })


# ── Database cleanup (free space) ──────────────────────────────────────────────

@app.route("/api/db-cleanup", methods=["POST"])
@admin_required
def api_db_cleanup():
    """Delete old data to free up Postgres disk space."""
    conn = db()
    freed = {}
    try:
        cur = conn.cursor()
        # 1. Delete old backtest trades (keep last 3 runs)
        cur.execute("""
            DELETE FROM backtest_trades
            WHERE run_id NOT IN (
                SELECT id FROM backtest_runs ORDER BY created_at DESC LIMIT 3
            )
        """)
        freed["backtest_trades_deleted"] = cur.rowcount

        # 2. Delete old backtest runs (keep last 5)
        cur.execute("""
            DELETE FROM backtest_runs
            WHERE id NOT IN (
                SELECT id FROM backtest_runs ORDER BY created_at DESC LIMIT 5
            )
        """)
        freed["backtest_runs_deleted"] = cur.rowcount

        # 3. Trim shadow_decisions older than 7 days
        cur.execute("DELETE FROM shadow_decisions WHERE created_at < NOW() - INTERVAL '7 days'")
        freed["shadow_decisions_deleted"] = cur.rowcount

        # 4. Trim shadow_positions older than 7 days that are closed
        cur.execute("DELETE FROM shadow_positions WHERE status='closed' AND closed_at < NOW() - INTERVAL '90 days'")
        freed["shadow_positions_deleted"] = cur.rowcount

        # 5. Trim token_feature_snapshots older than 14 days
        cur.execute("DELETE FROM token_feature_snapshots WHERE created_at < NOW() - INTERVAL '14 days'")
        freed["snapshots_deleted"] = cur.rowcount

        # 6. Trim signal_explorer_log older than 7 days
        try:
            cur.execute("DELETE FROM signal_explorer_log WHERE created_at < NOW() - INTERVAL '7 days'")
            freed["signal_log_deleted"] = cur.rowcount
        except Exception:
            conn.rollback()
            freed["signal_log_deleted"] = 0

        # 7. VACUUM to reclaim disk space
        conn.commit()
        conn.autocommit = True
        cur.execute("VACUUM")
        conn.autocommit = False
        freed["vacuumed"] = True

        return jsonify({"ok": True, "freed": freed})
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "msg": str(e), "freed": freed}), 500
    finally:
        db_return(conn)


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
            "edge_guard": bot.refresh_edge_guard(),
            "execution_control": bot.refresh_execution_control(),
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
                payload["logged_at"] = row["ts"].isoformat() if hasattr(row["ts"], "isoformat") else str(row["ts"])
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


# ──────────────────────────────────────────────────────────────────────────────
# SIMPLIFIED DASHBOARD API ENDPOINTS
# ──────────────────────────────────────────────────────────────────────────────

_BLOCKER_PLAIN_ENGLISH = {
    "market_cap_above_ceiling": "Market cap too high",
    "market_cap_below_floor": "Market cap too low",
    "liquidity_below_floor": "Not enough buyers/sellers",
    "volume_below_floor": "Trading volume too low",
    "threat_risk_too_high": "Looks risky (possible scam)",
    "holder_growth_below_threshold": "Not enough new buyers",
    "volume_spike_below_threshold": "No sudden interest spike",
    "age_too_old": "Token too old",
    "age_too_young": "Token too new",
    "price_dropping": "Price was going down",
    "score_below_threshold": "Score too low",
    "confidence_below_threshold": "Not confident enough",
    "hot_change_above_ceiling": "Price pumped too much already",
    "anti_rug_fail": "Failed safety check",
    "blacklisted_dev": "Developer is blacklisted",
    "duplicate_position": "Already holding this coin",
    "cooldown_active": "Recently checked, waiting",
    "max_positions_reached": "Too many open trades",
    "drawdown_limit_hit": "Loss limit reached",
    "narrative_below_floor": "No clear story/hype",
    "deployer_below_floor": "Developer not trusted",
    "green_lights_below_floor": "Not enough positive signals",
}

def _plain_reason(reason):
    """Convert a technical blocker reason to plain English."""
    if not reason:
        return "Didn't pass checks"
    r = str(reason).strip()
    if r in _BLOCKER_PLAIN_ENGLISH:
        return _BLOCKER_PLAIN_ENGLISH[r]
    # Try partial match
    for key, val in _BLOCKER_PLAIN_ENGLISH.items():
        if key in r:
            return val
    # Fallback: make the string readable
    return r.replace("_", " ").capitalize()


@app.route("/api/market-watch")
@login_required
def api_market_watch():
    """New coins being evaluated right now — newest first, no dedup."""
    uid = session["user_id"]
    bot = user_bots.get(uid)
    now_ts = time.time()

    # Gather evaluations from in-memory log first, then DB fallbacks
    entries = []
    if bot and bot.signal_explorer_log:
        entries = list(bot.signal_explorer_log)
    if not entries:
        # Fallback 1: signal_explorer_log table (user-specific)
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT payload_json, ts
                FROM signal_explorer_log
                WHERE user_id=%s ORDER BY ts DESC LIMIT 120
            """, (uid,))
            for row in cur.fetchall():
                try:
                    p = json.loads(row.get("payload_json") or "{}")
                    if isinstance(p, dict) and p:
                        entries.append(p)
                except Exception:
                    pass
        finally:
            db_return(conn)
    if not entries:
        # Fallback 2: shadow_decisions table (global — newest evaluations)
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT strategy_name, mint, name, passed, score, confidence, price,
                       blocker_reasons_json, created_at
                FROM shadow_decisions
                ORDER BY created_at DESC
                LIMIT 200
            """)
            for row in cur.fetchall():
                blockers = []
                try:
                    blockers = json.loads(row.get("blocker_reasons_json") or "[]")
                except Exception:
                    pass
                reason = blockers[0] if blockers else ""
                entries.append({
                    "mint": row.get("mint"),
                    "name": row.get("name") or "Unknown",
                    "passed": bool(row.get("passed")),
                    "score": float(row.get("score") or 0),
                    "price": float(row.get("price") or 0),
                    "reason": reason,
                    "timestamp": row["created_at"].timestamp() if row.get("created_at") and hasattr(row["created_at"], "timestamp") else 0,
                    "filters": [],
                    "strategy": row.get("strategy_name", ""),
                })
        finally:
            db_return(conn)

    # Sort newest first (no dedup — show each evaluation as it happens)
    def _get_ts(e):
        t = e.get("timestamp") or e.get("ts") or 0
        if isinstance(t, (int, float)):
            return t
        return 0
    entries.sort(key=_get_ts, reverse=True)

    # Collect unique mints for price fetching
    unique_mints = list(dict.fromkeys(e.get("mint") for e in entries if e.get("mint")))
    current_prices = {}
    for mint in unique_mints:
        hist = _price_history.get(mint)
        if hist:
            current_prices[mint] = hist[-1][1]

    # Batch-fetch missing prices (max 20 to avoid API hammering)
    missing = [m for m in unique_mints if m not in current_prices][:20]
    if missing and bot:
        try:
            fetched = bot._batch_token_prices(missing)
            current_prices.update(fetched)
        except Exception:
            pass

    coins = []
    total_bought = 0
    total_skipped = 0
    best_missed = None
    worst_avoided = None

    for e in entries[:100]:  # Cap at 100 recent evaluations
        mint = e.get("mint", "")
        name = e.get("name", "Unknown")
        passed = e.get("passed", False)
        score = e.get("score", 0)
        price_then = e.get("price", 0) or 0
        price_now = current_prices.get(mint, 0)

        # Compute hypothetical change
        change_pct = 0
        if price_then > 0 and price_now > 0:
            change_pct = round(((price_now / price_then) - 1) * 100, 1)

        # Determine reason
        reason = ""
        if not passed:
            total_skipped += 1
            reason = e.get("reason", "")
            if not reason:
                # Check filters list
                filters = e.get("filters", [])
                for f in filters:
                    if isinstance(f, dict) and not f.get("passed"):
                        reason = f.get("name", "")
                        break
            reason = _plain_reason(reason)
        else:
            total_bought += 1

        # Track best missed / worst avoided (unique per mint)
        if not passed and price_then > 0 and price_now > 0:
            if best_missed is None or change_pct > best_missed["change_pct"]:
                best_missed = {"name": name, "change_pct": change_pct}
            if worst_avoided is None or change_pct < worst_avoided["change_pct"]:
                worst_avoided = {"name": name, "change_pct": change_pct}

        # Time formatting — both clock time and "X min ago"
        ts_raw = _get_ts(e)
        eval_time = ""
        time_ago = ""
        if ts_raw > 1000000000:
            try:
                eval_time = datetime.fromtimestamp(ts_raw).strftime("%I:%M %p")
            except Exception:
                eval_time = ""
            ago_sec = now_ts - ts_raw
            if ago_sec < 60:
                time_ago = "just now"
            elif ago_sec < 3600:
                time_ago = f"{int(ago_sec / 60)}m ago"
            elif ago_sec < 86400:
                time_ago = f"{int(ago_sec / 3600)}h ago"
            else:
                time_ago = f"{int(ago_sec / 86400)}d ago"
        elif isinstance(e.get("ts"), str):
            eval_time = e.get("ts", "")

        coins.append({
            "name": name,
            "mint": mint,
            "evaluated_at": eval_time,
            "time_ago": time_ago,
            "price_then": price_then,
            "price_now": price_now,
            "change_pct": change_pct,
            "rating": score,
            "verdict": "Bought" if passed else "Skipped",
            "skip_reason": reason,
            "volume": e.get("vol", 0),
            "market_cap": e.get("mc", 0),
            "is_new": (ts_raw > 1000000000 and (now_ts - ts_raw) < 300),
        })

    return jsonify({
        "coins": coins,
        "summary": {
            "total_checked": len(coins),
            "total_bought": total_bought,
            "total_skipped": total_skipped,
            "best_missed": best_missed,
            "worst_avoided": worst_avoided,
        }
    })


@app.route("/api/my-trades")
@login_required
def api_my_trades():
    """Simplified view of open trades + recent sells + totals."""
    uid = session["user_id"]
    bot = user_bots.get(uid)

    open_trades = []
    if bot:
        pos_snapshot = dict(bot.positions)
        for mint, p in pos_snapshot.items():
            try:
                cur_price = bot.get_token_price(mint)
                entry = p["entry_price"]
                profit_pct = round(((cur_price / entry) - 1) * 100, 1) if cur_price and entry else 0
                age_sec = time.time() - p["timestamp"]
                if age_sec < 3600:
                    held_for = f"{int(age_sec/60)}m"
                else:
                    held_for = f"{age_sec/3600:.1f}h"
                open_trades.append({
                    "name": p["name"],
                    "mint": mint,
                    "bought_at_price": entry,
                    "current_price": cur_price,
                    "profit_pct": profit_pct,
                    "bought_when": time.strftime("%I:%M %p", time.localtime(p["timestamp"])),
                    "held_for": held_for,
                    "amount_sol": p.get("sol_in", 0),
                })
            except Exception:
                pass

    # Recent sells from DB
    recent_sells = []
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT mint, name, action, price, pnl_sol, timestamp
            FROM trades WHERE user_id=%s AND action ILIKE 'sell%%'
            ORDER BY timestamp DESC LIMIT 20
        """, (uid,))
        for row in cur.fetchall():
            pnl = row.get("pnl_sol") or 0
            ts = row.get("timestamp")
            ts_str = ""
            if ts:
                try:
                    ts_str = ts.strftime("%I:%M %p") if hasattr(ts, "strftime") else str(ts)
                except Exception:
                    ts_str = str(ts)
            recent_sells.append({
                "name": row.get("name", "Unknown"),
                "mint": row.get("mint", ""),
                "result": "Won" if pnl > 0 else "Lost",
                "profit_sol": round(pnl, 4),
                "sold_when": ts_str,
            })
    finally:
        db_return(conn)

    stats = bot.stats if bot else {"wins": 0, "losses": 0, "total_pnl_sol": 0}
    total_t = stats["wins"] + stats["losses"]

    return jsonify({
        "open_trades": open_trades,
        "recent_sells": recent_sells,
        "totals": {
            "balance_sol": round(bot.sol_balance, 4) if bot else 0,
            "wins": stats["wins"],
            "losses": stats["losses"],
            "win_rate": round(stats["wins"] / total_t * 100) if total_t else 0,
            "total_profit_sol": round(stats["total_pnl_sol"], 4),
        }
    })


@app.route("/api/scan-detail")
@login_required
def api_scan_detail():
    """Extended scan-tab data: rich positions, trade history wins/losses, optimizer."""
    uid = session["user_id"]
    bot = user_bots.get(uid)

    # ── Positions with full metadata ──────────────────────────────────────────
    pos_list = []
    if bot:
        pos_snapshot = dict(bot.positions)
        for mint, p in pos_snapshot.items():
            try:
                cur_price = bot.get_token_price(mint)
                entry = p.get("entry_price") or 0
                ratio = (cur_price / entry) if cur_price and entry else None
                peak_ratio = (p.get("peak_price", entry) / entry) if entry else None
                pnl_pct = round((ratio - 1) * 100, 1) if ratio else None
                pnl_sol = round(p.get("entry_sol", 0) * (ratio - 1), 4) if ratio else 0
                age_sec = time.time() - p.get("timestamp", time.time())
                pos_list.append({
                    "address":     mint,
                    "name":        p["name"],
                    "entry_price": entry,
                    "cur_price":   cur_price,
                    "ratio":       ratio,
                    "peak_ratio":  peak_ratio,
                    "pnl_pct":     pnl_pct,
                    "pnl_sol":     pnl_sol,
                    "age_min":     round(age_sec / 60, 1),
                    "entry_sol":   p.get("entry_sol", 0),
                    "tp1_hit":     p.get("tp1_hit", False),
                    "buy_reason":  p.get("buy_reason", ""),
                    "source":      p.get("source", "scanner"),
                })
            except Exception:
                pass

    # ── Trade history: recent wins and losses from DB ─────────────────────────
    winners, losers = [], []
    try:
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT mint, name, action, pnl_sol, timestamp
                FROM trades WHERE user_id=%s AND action ILIKE 'SELL%%'
                ORDER BY timestamp DESC LIMIT 80
            """, (uid,))
            for row in cur.fetchall():
                pnl = float(row.get("pnl_sol") or 0)
                ts = row.get("timestamp")
                ts_str = ts.strftime("%H:%M") if ts and hasattr(ts, "strftime") else ""
                action = row.get("action", "")
                close_reason = action.replace("SELL-", "", 1) if action.startswith("SELL-") else action
                entry = {
                    "name":         row.get("name", "?"),
                    "mint":         row.get("mint", ""),
                    "pnl_sol":      round(pnl, 4),
                    "close_reason": close_reason[:30],
                    "ts":           ts_str,
                }
                if pnl >= 0:
                    winners.append(entry)
                else:
                    losers.append(entry)
        finally:
            db_return(conn)
    except Exception:
        pass

    # ── Optimizer: win rate by buy_reason tag from live positions + bot stats ─
    optimizer = []
    try:
        conn = db()
        try:
            cur = conn.cursor()
            # Close reason breakdown from trades
            cur.execute("""
                SELECT action,
                       COUNT(*) AS total,
                       SUM(CASE WHEN pnl_sol > 0 THEN 1 ELSE 0 END) AS wins
                FROM trades WHERE user_id=%s AND action ILIKE 'SELL%%'
                GROUP BY action ORDER BY total DESC LIMIT 8
            """, (uid,))
            for row in cur.fetchall():
                total = int(row.get("total") or 0)
                wins = int(row.get("wins") or 0)
                label = (row.get("action") or "").replace("SELL-", "", 1)[:20]
                if total > 0:
                    optimizer.append({
                        "label":    label,
                        "trades":   total,
                        "wins":     wins,
                        "win_rate": round(wins / total * 100),
                    })
        finally:
            db_return(conn)
    except Exception:
        pass

    stats = bot.stats if bot else {"wins": 0, "losses": 0, "total_pnl_sol": 0}
    return jsonify({
        "positions": pos_list,
        "winners":   winners[:25],
        "losers":    losers[:25],
        "optimizer": optimizer,
        "stats":     stats,
    })


@app.route("/api/backtest-results")
@login_required
def api_backtest_results():
    """Simplified backtest results: strategy performance + settings for each.
    Pulls from ALL data sources: shadow_positions, backtest_trades, backtest_runs.
    """
    uid = session["user_id"]
    conn = db()
    try:
        cur = conn.cursor()

        # ── 1. Strategy performance from SHADOW positions (live shadow trading) ──
        cur.execute("""
            SELECT strategy_name,
                   COUNT(*) AS closed_trades,
                   SUM(CASE WHEN realized_pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
                   ROUND(AVG(realized_pnl_pct)::numeric, 2) AS avg_pnl,
                   ROUND(MAX(realized_pnl_pct)::numeric, 2) AS best_trade,
                   ROUND(MIN(realized_pnl_pct)::numeric, 2) AS worst_trade,
                   ROUND(AVG(max_upside_pct)::numeric, 2) AS avg_upside,
                   ROUND(AVG(max_drawdown_pct)::numeric, 2) AS avg_drawdown
            FROM shadow_positions
            WHERE status='closed'
            GROUP BY strategy_name
            ORDER BY avg_pnl DESC NULLS LAST
        """)
        shadow_perf_rows = cur.fetchall()

        # ── 2. Strategy performance from BACKTEST trades (from backtest runs) ──
        cur.execute("""
            SELECT bt.strategy_name,
                   COUNT(*) AS closed_trades,
                   SUM(CASE WHEN bt.realized_pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
                   ROUND(AVG(bt.realized_pnl_pct)::numeric, 2) AS avg_pnl,
                   ROUND(MAX(bt.realized_pnl_pct)::numeric, 2) AS best_trade,
                   ROUND(MIN(bt.realized_pnl_pct)::numeric, 2) AS worst_trade,
                   ROUND(AVG(bt.max_upside_pct)::numeric, 2) AS avg_upside,
                   ROUND(AVG(bt.max_drawdown_pct)::numeric, 2) AS avg_drawdown
            FROM backtest_trades bt
            JOIN backtest_runs br ON br.id = bt.run_id
            WHERE bt.status='closed' AND br.status='completed'
            GROUP BY bt.strategy_name
            ORDER BY avg_pnl DESC NULLS LAST
        """)
        bt_perf_rows = cur.fetchall()

        # ── 3. All backtest runs (no user filter — global data) ──
        cur.execute("""
            SELECT id, name, status, days, replay_mode, strategy_filter, trades_closed,
                   summary_json, config_json, created_at, snapshots_processed, tokens_processed
            FROM backtest_runs
            ORDER BY created_at DESC
            LIMIT 15
        """)
        run_rows = cur.fetchall()

        # ── 4. Recent trades from BOTH sources combined ──
        # Shadow trades
        cur.execute("""
            SELECT 'shadow' AS source, strategy_name, mint, name, entry_price, exit_price,
                   realized_pnl_pct, exit_reason, closed_at, score
            FROM shadow_positions
            WHERE status='closed' AND realized_pnl_pct IS NOT NULL
            ORDER BY closed_at DESC NULLS LAST
            LIMIT 20
        """)
        shadow_trade_rows = cur.fetchall()

        # Backtest trades
        cur.execute("""
            SELECT 'backtest' AS source, bt.strategy_name, bt.mint, bt.name, bt.entry_price,
                   bt.exit_price, bt.realized_pnl_pct, bt.exit_reason, bt.closed_at, bt.score
            FROM backtest_trades bt
            JOIN backtest_runs br ON br.id = bt.run_id
            WHERE bt.status='closed' AND br.status='completed' AND bt.realized_pnl_pct IS NOT NULL
            ORDER BY bt.closed_at DESC NULLS LAST
            LIMIT 20
        """)
        bt_trade_rows = cur.fetchall()

        # ── 5. Open shadow positions (still tracking) ──
        cur.execute("""
            SELECT strategy_name, mint, name, entry_price, current_price, score,
                   max_upside_pct, max_drawdown_pct, opened_at, observations
            FROM shadow_positions
            WHERE status='open'
            ORDER BY opened_at DESC NULLS LAST
            LIMIT 20
        """)
        open_shadow_rows = cur.fetchall()

        # ── 6. Data source health: count rows in each table ──
        data_health = {}
        for tbl in ["shadow_decisions", "shadow_positions", "backtest_trades", "backtest_runs"]:
            try:
                cur.execute(f"SELECT COUNT(*) AS n FROM {tbl}")
                data_health[tbl] = int(cur.fetchone().get("n", 0))
            except Exception:
                data_health[tbl] = 0
        # Also check how many shadow_positions are open vs closed
        try:
            cur.execute("SELECT status, COUNT(*) AS n FROM shadow_positions GROUP BY status")
            for row2 in cur.fetchall():
                data_health[f"shadow_positions_{row2['status']}"] = int(row2.get("n", 0))
        except Exception:
            pass
        # Check backtest_runs statuses
        try:
            cur.execute("SELECT status, COUNT(*) AS n FROM backtest_runs GROUP BY status")
            for row2 in cur.fetchall():
                data_health[f"backtest_runs_{row2['status']}"] = int(row2.get("n", 0))
        except Exception:
            pass
        # Per-strategy shadow_decisions breakdown (the ALL 4 strategies data)
        decision_breakdown = []
        try:
            cur.execute("""
                SELECT strategy_name,
                       COUNT(*) AS total,
                       SUM(CASE WHEN passed THEN 1 ELSE 0 END) AS passed,
                       SUM(CASE WHEN NOT passed THEN 1 ELSE 0 END) AS blocked,
                       ROUND(AVG(score)::numeric, 1) AS avg_score,
                       ROUND(AVG(confidence)::numeric, 2) AS avg_confidence,
                       MIN(created_at) AS first_eval,
                       MAX(created_at) AS last_eval
                FROM shadow_decisions
                GROUP BY strategy_name
                ORDER BY strategy_name
            """)
            for row2 in cur.fetchall():
                first_eval = row2.get("first_eval")
                last_eval = row2.get("last_eval")
                decision_breakdown.append({
                    "strategy": row2.get("strategy_name", ""),
                    "total": int(row2.get("total") or 0),
                    "passed": int(row2.get("passed") or 0),
                    "blocked": int(row2.get("blocked") or 0),
                    "avg_score": float(row2.get("avg_score") or 0),
                    "avg_confidence": float(row2.get("avg_confidence") or 0),
                    "first_eval": first_eval.strftime("%b %d, %I:%M %p") if first_eval and hasattr(first_eval, "strftime") else "",
                    "last_eval": last_eval.strftime("%b %d, %I:%M %p") if last_eval and hasattr(last_eval, "strftime") else "",
                })
        except Exception:
            pass
        # Top blocker reasons across ALL strategies
        top_blockers = []
        try:
            cur.execute("""
                SELECT blocker_reasons_json
                FROM shadow_decisions
                WHERE NOT passed
                ORDER BY created_at DESC
                LIMIT 500
            """)
            blocker_counter = {}
            for row2 in cur.fetchall():
                try:
                    reasons = json.loads(row2.get("blocker_reasons_json") or "[]")
                    for r in reasons:
                        blocker_counter[r] = blocker_counter.get(r, 0) + 1
                except Exception:
                    pass
            for reason, count in sorted(blocker_counter.items(), key=lambda x: -x[1])[:10]:
                top_blockers.append({"reason": reason, "count": count})
        except Exception:
            pass

    finally:
        db_return(conn)

    # Plain-English strategy names
    strategy_labels = {
        "safe": "Careful (Low Risk)",
        "balanced": "Balanced (Medium Risk)",
        "aggressive": "Aggressive (Higher Risk)",
        "degen": "Full Send (Max Risk)",
        "safe_v2": "Careful V2 (Optimized)",
        "balanced_v2": "Balanced V2 (Optimized)",
        "aggressive_v2": "Aggressive V2 (Optimized)",
        "degen_v2": "Full Send V2 (Optimized)",
    }
    exit_labels = {
        "take_profit": "Hit profit target",
        "stop_loss": "Hit loss limit",
        "time_stop": "Ran out of time",
    }

    # ── Merge strategy performance from both sources ──
    perf_map = {}
    for row in shadow_perf_rows:
        name = row.get("strategy_name", "")
        perf_map[name] = {
            "closed_trades": int(row.get("closed_trades") or 0),
            "wins": int(row.get("wins") or 0),
            "avg_pnl": float(row.get("avg_pnl") or 0),
            "best_trade": float(row.get("best_trade") or 0),
            "worst_trade": float(row.get("worst_trade") or 0),
            "avg_upside": float(row.get("avg_upside") or 0),
            "avg_drawdown": float(row.get("avg_drawdown") or 0),
            "source": "shadow",
        }
    # Backtest results override shadow if they have more trades
    for row in bt_perf_rows:
        name = row.get("strategy_name", "")
        bt_trades = int(row.get("closed_trades") or 0)
        existing = perf_map.get(name)
        if not existing or bt_trades > existing["closed_trades"]:
            perf_map[name] = {
                "closed_trades": bt_trades,
                "wins": int(row.get("wins") or 0),
                "avg_pnl": float(row.get("avg_pnl") or 0),
                "best_trade": float(row.get("best_trade") or 0),
                "worst_trade": float(row.get("worst_trade") or 0),
                "avg_upside": float(row.get("avg_upside") or 0),
                "avg_drawdown": float(row.get("avg_drawdown") or 0),
                "source": "backtest",
            }

    strategies = []
    for name, p in sorted(perf_map.items(), key=lambda x: -x[1]["avg_pnl"]):
        trades = p["closed_trades"]
        wins = p["wins"]
        preset = SHADOW_V2_PRESETS.get(name) or PRESETS.get(name, {})
        strategies.append({
            "name": name,
            "label": strategy_labels.get(name, preset.get("label", name.title())),
            "data_source": "Backtest Simulation" if p["source"] == "backtest" else "Live Shadow Trading",
            "total_trades": trades,
            "wins": wins,
            "losses": trades - wins,
            "win_rate": round(wins / trades * 100) if trades else 0,
            "avg_profit_pct": p["avg_pnl"],
            "best_trade_pct": p["best_trade"],
            "worst_trade_pct": p["worst_trade"],
            "avg_best_gain_pct": p["avg_upside"],
            "avg_worst_drop_pct": p["avg_drawdown"],
            "settings": {
                "buy_amount": preset.get("max_buy_sol", 0),
                "first_sell_target": preset.get("tp1_mult", 0),
                "second_sell_target": preset.get("tp2_mult", 0),
                "cut_losses_at": preset.get("stop_loss", 0),
                "time_limit_min": preset.get("time_stop_min", 0),
                "trailing_stop": preset.get("trail_pct", 0),
                "max_trades": preset.get("max_correlated", 0),
                "max_loss_sol": preset.get("drawdown_limit_sol", 0),
                "min_liquidity": preset.get("min_liq", 0),
                "min_market_cap": preset.get("min_mc", 0),
                "max_market_cap": preset.get("max_mc", 0),
                "min_volume": preset.get("min_vol", 0),
                "min_score": preset.get("min_score", 0),
            },
        })

    # If no closed trades, try to build strategy cards from shadow_decisions pass/fail stats
    if not strategies:
        conn2 = db()
        try:
            cur2 = conn2.cursor()
            cur2.execute("""
                SELECT strategy_name,
                       COUNT(*) AS total,
                       SUM(CASE WHEN passed THEN 1 ELSE 0 END) AS passed,
                       ROUND(AVG(score)::numeric, 1) AS avg_score,
                       ROUND(AVG(confidence)::numeric, 1) AS avg_confidence
                FROM shadow_decisions
                GROUP BY strategy_name
                ORDER BY avg_score DESC
            """)
            for row in cur2.fetchall():
                sname = row.get("strategy_name", "")
                total = int(row.get("total") or 0)
                passed = int(row.get("passed") or 0)
                preset = SHADOW_V2_PRESETS.get(sname) or PRESETS.get(sname, {})
                strategies.append({
                    "name": sname,
                    "label": strategy_labels.get(sname, preset.get("label", sname.title())),
                    "data_source": f"Shadow Evaluations ({total} coins checked, {passed} would buy)",
                    "total_trades": passed,
                    "wins": 0, "losses": 0,
                    "win_rate": round(passed / total * 100) if total else 0,
                    "avg_profit_pct": 0,
                    "best_trade_pct": float(row.get("avg_score") or 0),
                    "worst_trade_pct": 0,
                    "avg_best_gain_pct": float(row.get("avg_confidence") or 0),
                    "avg_worst_drop_pct": 0,
                    "settings": {
                        "buy_amount": preset.get("max_buy_sol", 0),
                        "first_sell_target": preset.get("tp1_mult", 0),
                        "second_sell_target": preset.get("tp2_mult", 0),
                        "cut_losses_at": preset.get("stop_loss", 0),
                        "time_limit_min": preset.get("time_stop_min", 0),
                        "trailing_stop": preset.get("trail_pct", 0),
                        "max_trades": preset.get("max_correlated", 0),
                        "max_loss_sol": preset.get("drawdown_limit_sol", 0),
                        "min_liquidity": preset.get("min_liq", 0),
                        "min_market_cap": preset.get("min_mc", 0),
                        "max_market_cap": preset.get("max_mc", 0),
                        "min_volume": preset.get("min_vol", 0),
                        "min_score": preset.get("min_score", 0),
                    },
                })
        finally:
            db_return(conn2)

    # If STILL no data, show all preset cards (originals + V2) as placeholders
    if not strategies:
        _placeholder_presets = _all_known_presets()
        for name in list(CANONICAL_STRATEGIES) + SHADOW_V2_STRATEGIES:
            preset = _placeholder_presets.get(name, {})
            strategies.append({
                "name": name,
                "label": strategy_labels.get(name, name.title()),
                "data_source": "No data yet — start the bot to begin collecting",
                "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "avg_profit_pct": 0, "best_trade_pct": 0, "worst_trade_pct": 0,
                "avg_best_gain_pct": 0, "avg_worst_drop_pct": 0,
                "settings": {
                    "buy_amount": preset.get("max_buy_sol", 0),
                    "first_sell_target": preset.get("tp1_mult", 0),
                    "second_sell_target": preset.get("tp2_mult", 0),
                    "cut_losses_at": preset.get("stop_loss", 0),
                    "time_limit_min": preset.get("time_stop_min", 0),
                    "trailing_stop": preset.get("trail_pct", 0),
                    "max_trades": preset.get("max_correlated", 0),
                    "max_loss_sol": preset.get("drawdown_limit_sol", 0),
                    "min_liquidity": preset.get("min_liq", 0),
                    "min_market_cap": preset.get("min_mc", 0),
                    "max_market_cap": preset.get("max_mc", 0),
                    "min_volume": preset.get("min_vol", 0),
                    "min_score": preset.get("min_score", 0),
                },
            })

    # ── Runs ──
    runs = []
    for row in run_rows:
        try:
            summary = json.loads(row.get("summary_json") or "{}")
        except Exception:
            summary = {}
        try:
            config = json.loads(row.get("config_json") or "{}")
        except Exception:
            config = {}
        # Pull per-strategy results from summary if available
        strat_results = []
        if isinstance(summary, dict):
            for sname, sdata in summary.items():
                if isinstance(sdata, dict) and "closed" in sdata:
                    strat_results.append({
                        "strategy": strategy_labels.get(sname, sname.title()),
                        "trades": int(sdata.get("closed") or 0),
                        "wins": int(sdata.get("wins") or 0),
                        "avg_pnl": round(float(sdata.get("avg_pnl_pct") or 0), 1),
                        "win_rate": round(float(sdata.get("win_rate") or 0), 0),
                    })
        runs.append({
            "id": int(row.get("id") or 0),
            "name": row.get("name") or f"Run #{row.get('id')}",
            "status": row.get("status"),
            "days": int(row.get("days") or 0),
            "mode": row.get("replay_mode") or "snapshot",
            "trades": int(row.get("trades_closed") or 0),
            "snapshots": int(row.get("snapshots_processed") or 0),
            "tokens": int(row.get("tokens_processed") or 0),
            "strategies": row.get("strategy_filter") or "all",
            "strategy_results": strat_results,
            "when": row.get("created_at").strftime("%b %d, %I:%M %p") if row.get("created_at") and hasattr(row["created_at"], "strftime") else str(row.get("created_at") or ""),
        })

    # ── Merge recent trades from both sources ──
    all_trade_rows = list(shadow_trade_rows) + list(bt_trade_rows)
    # Sort by closed_at desc
    all_trade_rows.sort(key=lambda r: r.get("closed_at") or datetime.min, reverse=True)

    recent_trades = []
    for row in all_trade_rows[:30]:
        exit_r = row.get("exit_reason") or ""
        src = row.get("source", "")
        recent_trades.append({
            "source": "Backtest" if src == "backtest" else "Shadow",
            "strategy": strategy_labels.get(row.get("strategy_name", ""), row.get("strategy_name", "")),
            "coin": row.get("name") or "Unknown",
            "profit_pct": float(row.get("realized_pnl_pct") or 0),
            "exit_reason": exit_labels.get(exit_r, exit_r.replace("_", " ").title() if exit_r else "Unknown"),
            "score": float(row.get("score") or 0),
            "entry_price": float(row.get("entry_price") or 0),
            "exit_price": float(row.get("exit_price") or 0),
        })

    # ── Open shadow positions ──
    open_positions = []
    for row in open_shadow_rows:
        entry = float(row.get("entry_price") or 0)
        current = float(row.get("current_price") or 0)
        pnl_pct = round(((current / entry) - 1) * 100, 1) if entry > 0 and current > 0 else 0
        open_positions.append({
            "strategy": strategy_labels.get(row.get("strategy_name", ""), row.get("strategy_name", "")),
            "coin": row.get("name") or "Unknown",
            "entry_price": entry,
            "current_price": current,
            "pnl_pct": pnl_pct,
            "best_gain_pct": float(row.get("max_upside_pct") or 0),
            "worst_drop_pct": float(row.get("max_drawdown_pct") or 0),
            "checks": int(row.get("observations") or 0),
        })

    return jsonify({
        "strategies": strategies,
        "runs": runs,
        "recent_trades": recent_trades,
        "open_positions": open_positions,
        "data_health": data_health,
        "decision_breakdown": decision_breakdown,
        "top_blockers": top_blockers,
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
    edge_guard = bot.refresh_edge_guard() if bot else get_quant_edge_guard_state()
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
        "edge_guard": edge_guard,
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
    best_t = 0
    worst_t = 0
    # Group sells by mint to count win/loss per trade, not per partial sell
    mint_pnl = {}  # mint -> total pnl_sol across all partial + final sells
    mint_closed = set()  # mints with a full exit (action contains TP2, SL, Trail, or 100%)
    for t in trades:
        pnl = t["pnl_sol"] or 0
        action = str(t.get("action") or "").upper()
        mint = t.get("mint", "")
        if action.startswith("SELL"):
            running_pnl += pnl
            mint_pnl[mint] = mint_pnl.get(mint, 0) + pnl
            best_t = max(best_t, pnl)
            worst_t = min(worst_t, pnl)
            # Mark as fully closed if this is a full exit (not a TP1/SKIM partial)
            if "TP1" not in action and "SKIM" not in action:
                mint_closed.add(mint)
        peak_pnl = max(peak_pnl, running_pnl)
        dd = peak_pnl - running_pnl
        max_dd = max(max_dd, dd)
        cumulative.append({
            "ts": t["timestamp"].isoformat() if hasattr(t["timestamp"], "isoformat") else str(t["timestamp"]),
            "pnl": round(running_pnl, 4),
            "drawdown": round(dd, 4),
        })
    # Count wins/losses per fully closed trade (combined partial + final P&L)
    wins = 0
    losses = 0
    for mint in mint_closed:
        if mint_pnl.get(mint, 0) >= 0:
            wins += 1
        else:
            losses += 1
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

@app.route("/api/admin/change-plan", methods=["POST"])
@admin_required
def api_admin_change_plan():
    """Admin: change a user's plan directly."""
    data = request.get_json(silent=True) or {}
    target_user_id = data.get("user_id")
    new_plan = (data.get("plan") or "").strip().lower()
    if not target_user_id or new_plan not in ("free", "trial", "basic", "pro", "elite"):
        return jsonify({"ok": False, "msg": "Invalid user_id or plan"})
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, email, plan FROM users WHERE id=%s", (int(target_user_id),))
        user = cur.fetchone()
        if not user:
            return jsonify({"ok": False, "msg": "User not found"})
        old_plan = user.get("plan", "")
        cur.execute("UPDATE users SET plan=%s WHERE id=%s", (new_plan, int(target_user_id)))
        conn.commit()
    finally:
        db_return(conn)
    # If the user has a running bot, update their max_buy_sol live
    bot = user_bots.get(int(target_user_id))
    if bot:
        max_sol = PLAN_LIMITS.get(new_plan, PLAN_LIMITS["free"])["max_buy_sol"]
        bot.settings["max_buy_sol"] = min(bot.settings.get("max_buy_sol", max_sol), max_sol)
    print(f"[ADMIN] Plan change: user {target_user_id} ({user.get('email')}) {old_plan} -> {new_plan}", flush=True)
    return jsonify({"ok": True, "msg": f"Changed {user.get('email')} from {old_plan} to {new_plan}"})


@app.route("/api/admin/shadow-analysis")
@admin_required
def api_admin_shadow_analysis():
    """Deep analysis of shadow trade performance for optimization."""
    conn = db()
    try:
        cur = conn.cursor()
        # Exit reason distribution
        cur.execute("""
            SELECT exit_reason, COUNT(*) AS cnt,
                   ROUND(AVG(realized_pnl_pct)::numeric, 2) AS avg_pnl,
                   SUM(CASE WHEN realized_pnl_pct > 0 THEN 1 ELSE 0 END) AS wins
            FROM shadow_positions WHERE status='closed'
            GROUP BY exit_reason ORDER BY cnt DESC
        """)
        exit_reasons = [dict(r) for r in cur.fetchall()]
        # Peak upside distribution (how high did tokens go before exit?)
        cur.execute("""
            SELECT
                SUM(CASE WHEN max_upside_pct < 10 THEN 1 ELSE 0 END) AS pct_0_10,
                SUM(CASE WHEN max_upside_pct >= 10 AND max_upside_pct < 25 THEN 1 ELSE 0 END) AS pct_10_25,
                SUM(CASE WHEN max_upside_pct >= 25 AND max_upside_pct < 50 THEN 1 ELSE 0 END) AS pct_25_50,
                SUM(CASE WHEN max_upside_pct >= 50 AND max_upside_pct < 100 THEN 1 ELSE 0 END) AS pct_50_100,
                SUM(CASE WHEN max_upside_pct >= 100 AND max_upside_pct < 200 THEN 1 ELSE 0 END) AS pct_100_200,
                SUM(CASE WHEN max_upside_pct >= 200 AND max_upside_pct < 500 THEN 1 ELSE 0 END) AS pct_200_500,
                SUM(CASE WHEN max_upside_pct >= 500 THEN 1 ELSE 0 END) AS pct_500plus,
                COUNT(*) AS total
            FROM shadow_positions WHERE status='closed'
        """)
        peak = dict(cur.fetchone() or {})
        # Max drawdown distribution (how low did they go?)
        cur.execute("""
            SELECT
                SUM(CASE WHEN max_drawdown_pct > -10 THEN 1 ELSE 0 END) AS dd_0_10,
                SUM(CASE WHEN max_drawdown_pct <= -10 AND max_drawdown_pct > -20 THEN 1 ELSE 0 END) AS dd_10_20,
                SUM(CASE WHEN max_drawdown_pct <= -20 AND max_drawdown_pct > -30 THEN 1 ELSE 0 END) AS dd_20_30,
                SUM(CASE WHEN max_drawdown_pct <= -30 AND max_drawdown_pct > -50 THEN 1 ELSE 0 END) AS dd_30_50,
                SUM(CASE WHEN max_drawdown_pct <= -50 THEN 1 ELSE 0 END) AS dd_50plus,
                COUNT(*) AS total
            FROM shadow_positions WHERE status='closed'
        """)
        dd = dict(cur.fetchone() or {})
        # What if TP1 was lower? Simulate different TP1 levels
        cur.execute("""
            SELECT
                SUM(CASE WHEN max_upside_pct >= 30 THEN 1 ELSE 0 END) AS would_hit_1_3x,
                SUM(CASE WHEN max_upside_pct >= 50 THEN 1 ELSE 0 END) AS would_hit_1_5x,
                SUM(CASE WHEN max_upside_pct >= 75 THEN 1 ELSE 0 END) AS would_hit_1_75x,
                SUM(CASE WHEN max_upside_pct >= 100 THEN 1 ELSE 0 END) AS would_hit_2x,
                SUM(CASE WHEN max_upside_pct >= 200 THEN 1 ELSE 0 END) AS would_hit_3x,
                SUM(CASE WHEN max_upside_pct >= 500 THEN 1 ELSE 0 END) AS would_hit_6x,
                COUNT(*) AS total
            FROM shadow_positions WHERE status='closed'
        """)
        tp_sim = dict(cur.fetchone() or {})
        # Avg hold time for winners vs losers
        cur.execute("""
            SELECT
                ROUND(AVG(CASE WHEN realized_pnl_pct > 0 THEN EXTRACT(EPOCH FROM (closed_at - opened_at))/60.0 END)::numeric, 1) AS avg_win_hold_min,
                ROUND(AVG(CASE WHEN realized_pnl_pct <= 0 THEN EXTRACT(EPOCH FROM (closed_at - opened_at))/60.0 END)::numeric, 1) AS avg_loss_hold_min,
                ROUND(AVG(EXTRACT(EPOCH FROM (closed_at - opened_at))/60.0)::numeric, 1) AS avg_hold_min
            FROM shadow_positions WHERE status='closed' AND closed_at IS NOT NULL AND opened_at IS NOT NULL
        """)
        hold = dict(cur.fetchone() or {})
    finally:
        db_return(conn)
    return jsonify({
        "exit_reasons": exit_reasons,
        "peak_distribution": peak,
        "drawdown_distribution": dd,
        "tp_level_simulation": tp_sim,
        "hold_times": hold,
    })


@app.route("/api/admin/recalc-shadow", methods=["POST"])
@admin_required
def api_admin_recalc_shadow():
    """Retroactively apply TP1/TP2 logic to all closed shadow trades.
    For each trade: if peak_price hit TP1 threshold, the trade would have
    partially sold at TP1, so recalculate realized_pnl_pct as weighted combo."""
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, strategy_name, entry_price, exit_price, peak_price,
                   realized_pnl_pct, exit_reason, tp1_hit, tp1_pnl_pct
            FROM shadow_positions WHERE status='closed' AND entry_price > 0
        """)
        rows = cur.fetchall()
        updated = 0
        tp1_applied = 0
        for row in rows:
            entry = float(row.get("entry_price") or 0)
            exit_p = float(row.get("exit_price") or 0)
            peak = float(row.get("peak_price") or 0)
            if entry <= 0 or exit_p <= 0:
                continue
            strat = row.get("strategy_name") or "balanced"
            s = _all_known_presets().get(strat, PRESETS["balanced"])
            tp1_mult = float(s.get("tp1_mult", 2.0))
            tp1_sell_pct = float(s.get("tp1_sell_pct", 0.50))
            peak_hit_tp1 = peak >= entry * tp1_mult
            if peak_hit_tp1:
                # TP1 would have triggered — calc what partial sell captured
                tp1_price = entry * tp1_mult  # conservative: assume sold exactly at TP1
                tp1_pnl = ((tp1_price / entry) - 1.0) * 100.0 * tp1_sell_pct
                remaining_pnl = ((exit_p / entry) - 1.0) * 100.0 * (1.0 - tp1_sell_pct)
                new_pnl = round(tp1_pnl + remaining_pnl, 2)
                cur.execute("""
                    UPDATE shadow_positions
                    SET realized_pnl_pct=%s, tp1_hit=1, tp1_pnl_pct=%s
                    WHERE id=%s
                """, (new_pnl, round(tp1_pnl, 2), row["id"]))
                tp1_applied += 1
            updated += 1
        conn.commit()
        # Get new aggregate stats
        cur.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN realized_pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
                   ROUND(AVG(realized_pnl_pct)::numeric, 2) AS avg_pnl,
                   ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY realized_pnl_pct))::numeric, 2) AS median_pnl,
                   SUM(CASE WHEN tp1_hit = 1 THEN 1 ELSE 0 END) AS tp1_hits
            FROM shadow_positions WHERE status='closed'
        """)
        agg = cur.fetchone() or {}
    finally:
        db_return(conn)
    total = int(agg.get("total") or 0)
    wins = int(agg.get("wins") or 0)
    return jsonify({
        "ok": True,
        "trades_checked": updated,
        "tp1_retroactively_applied": tp1_applied,
        "new_stats": {
            "total": total,
            "wins": wins,
            "win_rate": round(wins / total * 100, 1) if total else 0,
            "avg_pnl_pct": float(agg.get("avg_pnl") or 0),
            "median_pnl_pct": float(agg.get("median_pnl") or 0),
            "tp1_hits": int(agg.get("tp1_hits") or 0),
        },
        "msg": f"Recalculated {updated} trades. TP1 applied to {tp1_applied} trades that hit peak >= tp1_mult."
    })


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
        count = (cur.fetchone() or {}).get("c", 0)
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
        print(f"[ERROR] Stripe: {e}", flush=True)
        return "An error occurred while processing your subscription. Please try again.", 500

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

@app.route("/manage-subscription")
@login_required
def manage_subscription():
    uid = session["user_id"]
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT stripe_customer_id FROM users WHERE id=%s", (uid,))
        user = cur.fetchone()
    finally:
        db_return(conn)
    customer_id = (user or {}).get("stripe_customer_id")
    if not customer_id:
        return redirect(url_for("dashboard"))
    try:
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=request.host_url.rstrip("/") + url_for("dashboard"),
        )
        return redirect(portal.url)
    except Exception as e:
        print(f"[ERROR] Stripe portal: {e}", flush=True)
        return redirect(url_for("dashboard"))

# ── Admin ──────────────────────────────────────────────────────────────────────

@app.route("/api/admin/revenue")
@admin_required
def api_admin_revenue():
    """Live revenue and user stats for admin dashboard."""
    conn = db()
    try:
        cur = conn.cursor()
        # All users with plan info
        cur.execute("""
            SELECT u.id, u.email, u.plan, u.created_at, u.trial_ends,
                   u.stripe_customer_id, u.stripe_subscription_id,
                   COALESCE(t.trade_count, 0) AS trade_count,
                   COALESCE(t.total_pnl, 0) AS total_pnl_sol,
                   COALESCE(t.wins, 0) AS wins,
                   COALESCE(t.losses, 0) AS losses,
                   u.last_active
            FROM users u
            LEFT JOIN LATERAL (
                SELECT COUNT(*) AS trade_count,
                       ROUND(SUM(COALESCE(pnl_sol, 0))::numeric, 4) AS total_pnl,
                       SUM(CASE WHEN pnl_sol > 0 THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN pnl_sol < 0 THEN 1 ELSE 0 END) AS losses,
                       MAX(timestamp) AS last_trade
                FROM trades WHERE trades.user_id = u.id
            ) t ON true
            ORDER BY u.created_at DESC
        """)
        users = cur.fetchall()

        # Revenue from subscriptions (count by plan)
        cur.execute("""
            SELECT plan, COUNT(*) AS cnt
            FROM users
            WHERE plan IN ('basic', 'pro', 'elite')
              AND stripe_subscription_id IS NOT NULL
            GROUP BY plan
        """)
        sub_rows = cur.fetchall()

        # Performance fees
        cur.execute("""
            SELECT COALESCE(SUM(fee_sol), 0) AS total_fees,
                   COALESCE(SUM(CASE WHEN charged = 1 THEN fee_sol ELSE 0 END), 0) AS collected_fees,
                   COALESCE(SUM(CASE WHEN charged = 0 OR charged IS NULL THEN fee_sol ELSE 0 END), 0) AS pending_fees,
                   COUNT(*) AS fee_count
            FROM perf_fees
        """)
        fee_agg = cur.fetchone() or {}

        # Recent performance fees (last 50)
        cur.execute("""
            SELECT pf.id, pf.user_id, u.email, pf.pnl_sol, pf.fee_sol, pf.charged, pf.created_at
            FROM perf_fees pf
            LEFT JOIN users u ON u.id = pf.user_id
            ORDER BY pf.created_at DESC
            LIMIT 50
        """)
        recent_fees = cur.fetchall()

        # Platform totals
        cur.execute("SELECT COUNT(*) AS total FROM users")
        total_users = int((cur.fetchone() or {}).get("total", 0))

    finally:
        db_return(conn)

    # Calculate MRR from active subscriptions
    price_map = {"basic": 49, "pro": 99, "elite": 199}
    mrr = 0
    sub_counts = {}
    for r in sub_rows:
        plan_name = r.get("plan", "")
        cnt = int(r.get("cnt", 0))
        sub_counts[plan_name] = cnt
        mrr += price_map.get(plan_name, 0) * cnt

    # Active bots right now
    active_bot_count = sum(1 for b in user_bots.values() if b.running)

    return jsonify({
        "mrr": mrr,
        "total_users": total_users,
        "active_bots": active_bot_count,
        "subscriptions": sub_counts,
        "fees": {
            "total": round(float(fee_agg.get("total_fees", 0)), 4),
            "collected": round(float(fee_agg.get("collected_fees", 0)), 4),
            "pending": round(float(fee_agg.get("pending_fees", 0)), 4),
            "count": int(fee_agg.get("fee_count", 0)),
        },
        "users": [{
            "id": u["id"],
            "email": u["email"],
            "plan": u["plan"],
            "is_active": u["id"] in user_bots and user_bots[u["id"]].running,
            "trade_count": int(u.get("trade_count", 0)),
            "total_pnl_sol": float(u.get("total_pnl_sol", 0)),
            "wins": int(u.get("wins", 0)),
            "losses": int(u.get("losses", 0)),
            "has_stripe": bool(u.get("stripe_subscription_id")),
            "created_at": u["created_at"].isoformat() if u.get("created_at") else None,
            "last_active": u["last_active"].isoformat() if u.get("last_active") else None,
        } for u in users],
        "recent_fees": [{
            "user_id": f["user_id"],
            "email": f.get("email"),
            "pnl_sol": round(float(f.get("pnl_sol", 0)), 4),
            "fee_sol": round(float(f.get("fee_sol", 0)), 4),
            "charged": bool(f.get("charged")),
            "created_at": f["created_at"].isoformat() if f.get("created_at") else None,
        } for f in recent_fees],
    })


@app.route("/admin")
@login_required
def admin():
    if not is_admin():
        return redirect(url_for("dashboard"))
    # Render admin page — data loaded via /api/admin/revenue
    return Response(ADMIN_HTML, mimetype="text/html")

# ── HTML Templates ─────────────────────────────────────────────────────────────
_CSS = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SolTrader — AI-Powered Solana Trading Bot | Signal Explorer, Whale Tracking & Paper Trading</title>
<meta name="description" content="The most transparent Solana trading bot. See exactly why every token is accepted or rejected. Whale copy trading, multi-stage take profits, paper trading, and AI-driven strategies. Free to start.">
<meta name="keywords" content="solana trading bot, solana sniper bot, meme coin bot, whale tracking, paper trading, AI trading, crypto bot, jupiter swap, helius, jito, CEX listing sniper">
<meta name="robots" content="index, follow">
<link rel="canonical" href="https://soltrader.trade/">
<meta property="og:type" content="website">
<meta property="og:url" content="https://soltrader.trade/">
<meta property="og:title" content="SolTrader — AI Solana Trading Bot">
<meta property="og:description" content="See WHY every token passes or fails. Whale copy trading, paper trading, multi-stage TP, and 4 AI strategies. Free tier available.">
<meta property="og:image" content="https://soltrader.trade/static/og-card.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="SolTrader — AI Solana Trading Bot">
<meta name="twitter:description" content="The only bot that shows you WHY. Signal Explorer, Whale Tracking, Paper Trading. Free to start.">
<meta name="twitter:image" content="https://soltrader.trade/static/og-card.png">
<meta name="theme-color" content="#07101E">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="SolTrader">
<link rel="manifest" href="/manifest.json">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Manrope:wght@400;500;600;700;800&family=Space+Grotesk:wght@500;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script>if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');</script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#07101E;--surf:#0B1524;--card:#111F33;--b1:#1B3049;--b2:#25415D;
  --bg2:#0C1829;--bg3:#13243A;--bdr:#1B3049;
  --t1:#ECF4FF;--t2:#A7B8CD;--t3:#64748B;
  --blue:#2F6BFF;--blue2:#60A5FA;--blue3:#1D4ED8;
  --grn:#14C784;--grn2:#22C55E;--red:#DC2626;--red2:#FB7185;--gold:#D97706;--gold2:#FBBF24;
  --shadow:0 20px 50px rgba(2,8,23,.35);
}
html,body{min-height:100vh}
body{
  background:
    radial-gradient(circle at top left, rgba(47,107,255,.18), transparent 28%),
    radial-gradient(circle at top right, rgba(20,199,132,.14), transparent 24%),
    linear-gradient(180deg, #09111d 0%, #07101e 48%, #08111b 100%);
  color:var(--t1);
  font-family:'Manrope',-apple-system,BlinkMacSystemFont,sans-serif;
  font-size:14px;
  line-height:1.5;
  -webkit-font-smoothing:antialiased;
}
body::before,body::after{content:"";position:fixed;pointer-events:none;z-index:-1;border-radius:999px;filter:blur(80px);opacity:.3}
body::before{width:280px;height:280px;left:-80px;top:120px;background:rgba(47,107,255,.25)}
body::after{width:260px;height:260px;right:-70px;top:280px;background:rgba(20,199,132,.18)}
a{color:var(--blue2);text-decoration:none}
a:hover{color:var(--t1)}
code,.tree-wallet-addr,.price-val,.num-val,.sig-token-meta,.setting-input,.lline,.tree-tok-mint,.tok-sym{font-family:'JetBrains Mono','SF Mono','Courier New',monospace}
.nav{
  background:rgba(7,16,30,.82);
  border-bottom:1px solid rgba(255,255,255,.06);
  padding:0 28px;
  height:62px;
  display:flex;
  align-items:center;
  justify-content:space-between;
  position:sticky;
  top:0;
  z-index:100;
  backdrop-filter:blur(18px);
  box-shadow:0 10px 35px rgba(2,8,23,.22);
}
.logo{
  font-family:'Space Grotesk','Manrope',sans-serif;
  font-size:15px;
  font-weight:700;
  color:var(--t1);
  display:flex;
  align-items:center;
  gap:10px;
  letter-spacing:-.3px;
  text-decoration:none;
}
.logo-mark{width:30px;height:30px;background:linear-gradient(135deg,#2F6BFF,#7DD3FC);border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:800;color:#fff;flex-shrink:0;box-shadow:0 12px 26px rgba(37,99,235,.3)}
.nav-r{display:flex;align-items:center;gap:20px}
.nav-r a{color:var(--t2);font-size:13px;font-weight:500;transition:.15s;text-decoration:none}
.nav-r a:hover{color:var(--t1)}
.nbtn{background:var(--blue) !important;color:#fff !important;padding:7px 16px;border-radius:6px;font-weight:600 !important}
.nbtn:hover{background:var(--blue3) !important;color:#fff !important}
.center-page{display:flex;align-items:center;justify-content:center;min-height:calc(100vh - 58px);padding:32px 16px}
.wrap{max-width:980px;margin:0 auto;padding:34px 24px}
.card,.panel,.stat{
  background:linear-gradient(180deg, rgba(17,31,51,.94), rgba(11,21,36,.9));
  border:1px solid rgba(255,255,255,.07);
  box-shadow:var(--shadow);
}
.card{border-radius:20px;padding:28px}
.panel{border-radius:16px;padding:18px;margin-bottom:14px}
.page-title{font-family:'Space Grotesk','Manrope',sans-serif;font-size:22px;font-weight:700;letter-spacing:-.5px;margin-bottom:4px}
.sec-label{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:1.2px;color:var(--t3);margin-bottom:12px}
.fgroup{margin-bottom:14px}
.flabel{display:block;font-size:12px;font-weight:500;color:var(--t2);margin-bottom:5px}
.finput{width:100%;background:rgba(7,16,29,.7);border:1px solid rgba(255,255,255,.08);color:var(--t1);border-radius:11px;padding:11px 13px;font-size:13.5px;font-family:inherit;transition:.2s;display:block}
.finput:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px rgba(37,99,235,.15)}
.finput::placeholder{color:var(--t3)}
select.finput{cursor:pointer}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;border:none;border-radius:12px;padding:10px 18px;font-size:13px;font-weight:700;font-family:inherit;cursor:pointer;transition:.18s;text-decoration:none;line-height:1;box-shadow:0 14px 28px rgba(2,8,23,.18)}
.btn:hover{transform:translateY(-1px)}
.btn-full{width:100%}
.btn-primary{background:linear-gradient(135deg,var(--blue),#4F8CFF);color:#fff}.btn-primary:hover{background:linear-gradient(135deg,#2558D8,#3C7AF6)}
.btn-success{background:linear-gradient(135deg,var(--grn),#0FA56F);color:#fff}.btn-success:hover{background:linear-gradient(135deg,#0FA56F,#0B8258)}
.btn-danger{background:linear-gradient(135deg,var(--red),#B91C1C);color:#fff}.btn-danger:hover{background:linear-gradient(135deg,#B91C1C,#991B1B)}
.btn-ghost{background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.08);color:var(--t2)}.btn-ghost:hover{color:var(--t1);border-color:rgba(96,165,250,.35);background:rgba(96,165,250,.08)}
.btn-outline{background:transparent;border:1px solid var(--blue);color:var(--blue2)}.btn-outline:hover{background:var(--blue);color:#fff}
.btn-gold{background:var(--gold);color:#fff}.btn-gold:hover{background:#B45309}
.alert{padding:10px 14px;border-radius:7px;font-size:13px;margin-bottom:14px}
.alert:empty{display:none}
.alert-error{background:rgba(220,38,38,.1);border:1px solid rgba(220,38,38,.25);color:#FCA5A5}
.alert-info{background:rgba(37,99,235,.08);border:1px solid rgba(37,99,235,.2);color:#93C5FD}
.alert-success{background:rgba(5,150,105,.1);border:1px solid rgba(5,150,105,.25);color:#6EE7B7}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-bottom:16px}
.stat{border-radius:16px;padding:16px 17px;transition:.18s}
.stat:hover{border-color:rgba(96,165,250,.26);transform:translateY(-2px)}
.slabel{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.8px;color:var(--t3);margin-bottom:7px}
.sval{font-family:'Space Grotesk','Manrope',sans-serif;font-size:22px;font-weight:700;letter-spacing:-.7px;color:var(--t1)}
.ssub{font-size:11px;color:var(--t3);margin-top:2px}
.c-grn{color:var(--grn2)}.c-red{color:var(--red2)}.c-gold{color:var(--gold2)}.c-blue{color:var(--blue2)}.c-muted{color:var(--t3)}
.status{display:flex;align-items:center;gap:7px}
.sdot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.sdot-on{background:var(--grn2);box-shadow:0 0 8px var(--grn2);animation:blink 2s infinite}
.sdot-off{background:var(--t3)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.stxt{font-size:12px;font-weight:500;color:var(--t2)}
.tbl{width:100%;border-collapse:collapse}
.tbl th{font-size:10px;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:.9px;padding:9px 10px;border-bottom:1px solid rgba(255,255,255,.07);text-align:left}
.tbl td{padding:10px;border-bottom:1px solid rgba(26,46,69,.38);font-size:12.5px;color:var(--t2)}
.tbl tr:last-child td{border:none}
.tbl tbody tr:hover td{background:rgba(255,255,255,.02)}
.log{background:var(--surf);border:1px solid var(--b1);border-radius:10px;padding:14px;height:210px;overflow-y:auto}
.lline{font-size:12px;padding:4px 6px;border-bottom:1px solid rgba(255,255,255,.04);color:#94a3b8;font-family:'SF Mono','Courier New',monospace;line-height:1.65;border-radius:3px}
.lline:hover{background:rgba(255,255,255,.03)}
.lbuy{color:#4ade80!important;font-weight:600}.lsell{color:#f87171!important;font-weight:600}.lsig{color:#fbbf24!important;font-weight:600}.linfo{color:#60a5fa!important}.lscan{color:#818cf8}
.badge{display:inline-flex;padding:4px 9px;border-radius:999px;font-size:10px;font-weight:700;letter-spacing:.2px}
.bg-grn{background:rgba(5,150,105,.15);color:var(--grn2)}
.bg-red{background:rgba(220,38,38,.15);color:var(--red2)}
.bg-blue{background:rgba(37,99,235,.15);color:var(--blue2)}
.bg-gold{background:rgba(217,119,6,.15);color:var(--gold2)}
.bg-muted{background:rgba(71,85,105,.15);color:var(--t3)}
.divider{border:none;border-top:1px solid rgba(255,255,255,.08);margin:16px 0}
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
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:rgba(100,116,139,.35);border-radius:999px}
::-webkit-scrollbar-track{background:rgba(255,255,255,.02)}
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

<style>
/* ═══════════ ANIMATED PROMO BANNERS ═══════════ */
@keyframes promoGradient{0%{background-position:0% 50%}25%{background-position:100% 50%}50%{background-position:0% 50%}75%{background-position:100% 0%}100%{background-position:0% 50%}}
@keyframes promoShimmer{0%{left:-100%}100%{left:200%}}
@keyframes promoFloat{0%,100%{transform:translateY(0)}50%{transform:translateY(-6px)}}
@keyframes promoNumber{0%{opacity:0;transform:translateY(14px) scale(.85)}60%{opacity:1;transform:translateY(-2px) scale(1.04)}100%{opacity:1;transform:translateY(0) scale(1)}}
@keyframes promoPulse{0%,100%{box-shadow:0 0 20px rgba(20,199,132,.3)}50%{box-shadow:0 0 40px rgba(20,199,132,.6),0 0 80px rgba(20,199,132,.2)}}
@keyframes promoSpark{0%{opacity:1;transform:translate(0,0) scale(1)}100%{opacity:0;transform:translate(var(--sx),var(--sy)) scale(0)}}
@keyframes promoBorderRun{0%{background-position:0% 0%}100%{background-position:200% 0%}}
@keyframes promoSlotRoll{0%{transform:translateY(100%);opacity:0}20%{transform:translateY(0);opacity:1}80%{transform:translateY(0);opacity:1}100%{transform:translateY(-100%);opacity:0}}
@keyframes heroFireFlicker{0%,100%{text-shadow:0 0 10px rgba(245,158,11,.6),0 0 20px rgba(245,158,11,.3)}50%{text-shadow:0 0 20px rgba(245,158,11,.9),0 0 40px rgba(245,158,11,.5),0 0 60px rgba(245,158,11,.2)}}
@keyframes promoBounceIn{0%{opacity:0;transform:scale(.3)}50%{opacity:1;transform:scale(1.05)}70%{transform:scale(.95)}100%{transform:scale(1)}}
@keyframes promoTypewriter{from{width:0}to{width:100%}}
@keyframes promoCursor{0%,100%{border-color:transparent}50%{border-color:#14c784}}

.promo-topbar{
  position:relative;overflow:hidden;
  background:linear-gradient(90deg,#14c784,#3b82f6,#a855f7,#f59e0b,#14c784);
  background-size:300% 100%;animation:promoGradient 6s ease infinite;
  padding:10px 0;text-align:center;
}
.promo-topbar-text{font-size:12px;font-weight:800;color:#000;letter-spacing:.06em;text-transform:uppercase}
.promo-topbar-text a{color:#000;text-decoration:underline;font-weight:900}

.promo-hero-banner{
  position:relative;overflow:hidden;
  background:linear-gradient(135deg,rgba(8,18,38,.98),rgba(12,24,48,.96),rgba(6,14,30,.98));
  border:1px solid rgba(20,199,132,.25);border-radius:24px;
  padding:40px 32px;margin:0 auto 32px;max-width:900px;
  animation:promoPulse 3s ease-in-out infinite;
}
.promo-hero-banner::before{
  content:"";position:absolute;top:0;left:-100%;width:60%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.04),transparent);
  animation:promoShimmer 3s ease-in-out infinite;
}
.promo-hero-banner::after{
  content:"";position:absolute;inset:-2px;border-radius:26px;padding:2px;
  background:linear-gradient(90deg,#14c784,#3b82f6,#a855f7,#f59e0b,#14c784);
  background-size:200% 100%;animation:promoBorderRun 3s linear infinite;
  -webkit-mask:linear-gradient(#fff 0 0) content-box,linear-gradient(#fff 0 0);
  mask:linear-gradient(#fff 0 0) content-box,linear-gradient(#fff 0 0);
  -webkit-mask-composite:xor;mask-composite:exclude;
  pointer-events:none;
}
.promo-hero-inner{position:relative;z-index:2;text-align:center}
.promo-hero-kicker{font-size:11px;font-weight:800;letter-spacing:.2em;text-transform:uppercase;color:#14c784;margin-bottom:12px}
.promo-hero-title{font-size:36px;font-weight:900;letter-spacing:-1.2px;line-height:1.1;color:#fff;margin-bottom:8px;font-family:'Space Grotesk','Manrope',sans-serif}
.promo-hero-subtitle{font-size:14px;color:rgba(255,255,255,.6);margin-bottom:24px}
.promo-hero-stats{display:flex;justify-content:center;gap:32px;flex-wrap:wrap;margin-bottom:24px}
.promo-hero-stat{text-align:center}
.promo-hero-stat-val{font-size:42px;font-weight:900;letter-spacing:-1.5px;font-family:'Space Grotesk','Manrope',sans-serif;animation:promoNumber 1s ease forwards;line-height:1}
.promo-hero-stat-lbl{font-size:10px;font-weight:700;letter-spacing:.15em;text-transform:uppercase;color:rgba(255,255,255,.45);margin-top:6px}
.promo-fire{animation:heroFireFlicker 1.5s ease-in-out infinite;display:inline}

.promo-feat-strip{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin-bottom:20px}
.promo-feat-chip{
  display:inline-flex;align-items:center;gap:6px;
  padding:8px 16px;border-radius:24px;font-size:11px;font-weight:700;
  background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);
  color:rgba(255,255,255,.75);animation:promoFloat 3s ease-in-out infinite;
}
.promo-feat-chip:nth-child(2){animation-delay:.4s}
.promo-feat-chip:nth-child(3){animation-delay:.8s}
.promo-feat-chip:nth-child(4){animation-delay:1.2s}
.promo-feat-chip:nth-child(5){animation-delay:1.6s}

.promo-cta-row{display:flex;gap:14px;justify-content:center;flex-wrap:wrap}
.promo-cta{
  padding:14px 32px;border-radius:14px;font-size:14px;font-weight:800;letter-spacing:.02em;
  text-decoration:none;transition:all .3s;position:relative;overflow:hidden;
}
.promo-cta-primary{
  background:linear-gradient(135deg,#14c784,#0fa968);color:#000;
  box-shadow:0 4px 24px rgba(20,199,132,.4);
}
.promo-cta-primary:hover{transform:translateY(-2px);box-shadow:0 8px 32px rgba(20,199,132,.6)}
.promo-cta-ghost{background:transparent;border:1px solid rgba(255,255,255,.15);color:#fff}
.promo-cta-ghost:hover{background:rgba(255,255,255,.05);border-color:rgba(255,255,255,.25)}

/* Spark particles */
.promo-spark{position:absolute;width:4px;height:4px;border-radius:50%;background:#14c784;animation:promoSpark 1.5s ease-out forwards;pointer-events:none}

/* Rolling number slot machine */
.promo-slot{display:inline-block;overflow:hidden;height:1.1em;vertical-align:bottom}
.promo-slot-inner{animation:promoSlotRoll 4s ease-in-out infinite}

/* Dashboard promo ribbon */
.promo-ribbon{
  position:relative;overflow:hidden;
  background:linear-gradient(90deg,rgba(20,199,132,.08),rgba(59,130,246,.08),rgba(168,85,247,.08));
  border:1px solid rgba(20,199,132,.15);border-radius:14px;
  padding:14px 20px;margin-bottom:16px;
  display:flex;align-items:center;justify-content:space-between;gap:16px;
}
.promo-ribbon::before{
  content:"";position:absolute;top:0;left:-100%;width:50%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.03),transparent);
  animation:promoShimmer 4s ease-in-out infinite;
}
.promo-ribbon-text{font-size:12px;font-weight:700;color:var(--t1);position:relative;z-index:2}
.promo-ribbon-sub{font-size:10px;color:var(--t3);margin-top:3px}
.promo-ribbon-stat{
  display:flex;align-items:center;gap:12px;position:relative;z-index:2;
}
.promo-ribbon-val{font-size:20px;font-weight:900;font-family:'Space Grotesk','Manrope',sans-serif;color:#14c784;letter-spacing:-.5px}
.promo-ribbon-lbl{font-size:9px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--t3)}
</style>
</head><body>

<!-- Animated Top Bar -->
<div class="promo-topbar">
  <div class="promo-topbar-text">AI-Powered Solana Trading &nbsp;·&nbsp; See Why Every Trade Happens &nbsp;·&nbsp; Paper Trade Risk-Free &nbsp;·&nbsp; <a href="/signup">Start Free</a></div>
</div>

<nav class="nav">
  <a href="/" class="logo"><div class="logo-mark">S</div>SolTrader</a>
  <div class="nav-r">
    <a href="#pricing">Pricing</a>
    <a href="/login">Sign In</a>
    <a href="/signup" class="nbtn">Get Started Free</a>
  </div>
</nav>

<!-- Animated Hero Banner -->
<div style="padding:60px 24px 0;max-width:960px;margin:0 auto">
  <div class="promo-hero-banner">
    <div class="promo-hero-inner">
      <div class="promo-hero-kicker">Transparent AI Trading</div>
      <div class="promo-hero-title">
        The Only Bot That Shows You<br>
        <span class="promo-fire">Why.</span>
      </div>
      <div class="promo-hero-subtitle">Every token scored. Every rejection explained. Paper trade first, go live when ready.</div>
      <div class="promo-hero-stats">
        <div class="promo-hero-stat">
          <div class="promo-hero-stat-val" style="color:#14c784" id="promo-stat-1">4</div>
          <div class="promo-hero-stat-lbl">AI Strategies</div>
        </div>
        <div class="promo-hero-stat">
          <div class="promo-hero-stat-val" style="color:#3b82f6" id="promo-stat-2">24/7</div>
          <div class="promo-hero-stat-lbl">Shadow Testing</div>
        </div>
        <div class="promo-hero-stat">
          <div class="promo-hero-stat-val" style="color:#a855f7" id="promo-stat-3">12+</div>
          <div class="promo-hero-stat-lbl">Safety Filters</div>
        </div>
        <div class="promo-hero-stat">
          <div class="promo-hero-stat-val" style="color:#f59e0b" id="promo-stat-4">$0</div>
          <div class="promo-hero-stat-lbl">To Start</div>
        </div>
      </div>
      <div class="promo-feat-strip">
        <div class="promo-feat-chip">Progressive Trailing Stop</div>
        <div class="promo-feat-chip">Whale Detection</div>
        <div class="promo-feat-chip">Regime-Aware Models</div>
        <div class="promo-feat-chip">Anti-Rug Engine</div>
        <div class="promo-feat-chip">Shadow Paper Trading</div>
      </div>
      <div class="promo-cta-row">
        <a href="/signup" class="promo-cta promo-cta-primary">Start Trading Free</a>
        <a href="#pricing" class="promo-cta promo-cta-ghost">View Plans</a>
      </div>
    </div>
  </div>
</div>

<!-- Original Hero (simplified) -->
<div style="text-align:center;padding:20px 24px 60px;max-width:680px;margin:0 auto">
  <h1 style="font-size:44px;font-weight:800;line-height:1.1;letter-spacing:-1.5px;margin-bottom:18px">
    The Most Advanced<br>Solana Trading Bot
  </h1>
  <p style="font-size:15.5px;color:var(--t2);max-width:480px;margin:0 auto 38px;line-height:1.75">
    See WHY every token is accepted or rejected. Track whale wallets in real time. Visualize your P&amp;L with interactive charts. No other bot shows you this.
  </p>
  <div style="display:flex;gap:12px;justify-content:center;flex-wrap:wrap">
    <a href="/signup" class="btn btn-primary" style="padding:14px 36px;font-size:15px">Start Trading in 2 Minutes</a>
    <a href="#pricing" class="btn btn-ghost" style="padding:14px 26px;font-size:15px">View Pricing</a>
  </div>
  <div class="trust" style="margin-top:30px">
    <div class="titem">AES-256 Keys</div>
    <div class="titem">Helius Sender</div>
    <div class="titem">Jito Tips</div>
    <div class="titem">CEX Sniper</div>
    <div class="titem">Cancel Anytime</div>
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
    err_html    = f'<div class="alert alert-error">{html.escape(error)}</div>' if error else ""
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
</body></html>
"""

# ── Dashboard Page ─────────────────────────────────────────────────────────────
DASHBOARD_HTML = _CSS + """
<style>
.dashboard-shell{max-width:1520px;padding-top:18px}
/* ticker-strip removed */
.dashboard-hero{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(320px,.85fr);gap:18px;margin-bottom:18px}
.hero-panel,.glass,.settings-card{
  background:linear-gradient(180deg, rgba(17,31,51,.94), rgba(10,19,32,.9));
  border:1px solid rgba(255,255,255,.07);
  border-radius:20px;
  box-shadow:var(--shadow);
  backdrop-filter:blur(16px);
}
.hero-panel{padding:22px}
.glass{padding:18px}
.plan-gate-overlay{position:absolute;top:0;left:0;right:0;bottom:0;z-index:80;display:flex;align-items:center;justify-content:center;background:rgba(7,14,23,.75);backdrop-filter:blur(6px);border-radius:16px;min-height:400px}
.plan-gate-card{text-align:center;padding:40px 32px;border-radius:20px;background:linear-gradient(180deg,rgba(17,31,51,.98),rgba(10,19,32,.95));border:1px solid rgba(47,107,255,.2);max-width:420px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
.plan-gate-card h3{font-size:20px;font-weight:800;margin:0 0 8px;color:var(--t1)}
.plan-gate-card p{font-size:13px;color:var(--t2);margin:0 0 20px;line-height:1.6}
.plan-gate-btn{display:inline-block;padding:12px 32px;font-size:14px;font-weight:700;color:#fff;background:linear-gradient(135deg,#2F6BFF,#14C784);border:none;border-radius:12px;cursor:pointer;text-decoration:none;transition:transform .15s,box-shadow .15s}
.plan-gate-btn:hover{transform:translateY(-2px);box-shadow:0 8px 24px rgba(47,107,255,.3)}
.plan-features{display:flex;flex-direction:column;gap:6px;text-align:left;margin:16px 0 20px;padding:12px;border-radius:10px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06)}
.plan-features span{font-size:12px;color:var(--t2)}
.hero-panel-primary{position:relative;overflow:hidden}
.hero-panel-primary::after{content:"";position:absolute;right:-60px;top:-60px;width:220px;height:220px;border-radius:50%;background:radial-gradient(circle, rgba(96,165,250,.18), transparent 65%)}
.hero-kicker,.tab-kicker,.panel-kicker{font-size:10px;font-weight:800;letter-spacing:.18em;text-transform:uppercase;color:var(--blue2);margin-bottom:10px}
.hero-title{font-family:'Space Grotesk','Manrope',sans-serif;font-size:36px;line-height:1.05;letter-spacing:-1.4px;max-width:760px;margin-bottom:12px}
.hero-copy{font-size:14px;color:var(--t2);max-width:720px;line-height:1.7}
.hero-chip-row,.settings-echo,.intel-alerts,.shortcut-row{display:flex;gap:8px;flex-wrap:wrap}
.hero-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
.hero-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:18px}
.hero-mini{padding:12px 14px;border-radius:16px;background:rgba(7,14,23,.42);border:1px solid rgba(255,255,255,.06)}
.hero-mini-label{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--t3);margin-bottom:5px}
.hero-mini-value{font-family:'Space Grotesk','Manrope',sans-serif;font-size:20px;letter-spacing:-.6px;color:var(--t1)}
.hero-mini-copy{font-size:11px;color:var(--t2);margin-top:3px;line-height:1.45}
.hero-side-stack{display:flex;flex-direction:column;gap:14px}
.account-grid,.intel-metrics,.scanner-summary-grid,.pnl-summary-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.account-cell,.intel-metric,.scanner-summary-card,.pnl-summary-card{
  padding:12px 14px;
  border-radius:16px;
  background:rgba(7,14,23,.42);
  border:1px solid rgba(255,255,255,.06);
}
.account-label,.intel-label,.scanner-summary-label,.pnl-summary-label{font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.1em;margin-bottom:5px}
.account-value,.intel-value,.scanner-summary-value,.pnl-summary-value{font-size:14px;font-weight:800;color:var(--t1)}
.account-copy,.intel-copy,.scanner-summary-copy,.pnl-summary-copy{font-size:11px;color:var(--t2);margin-top:3px;line-height:1.45}
.shortcut-row .badge{background:rgba(255,255,255,.04);color:var(--t2);border:1px solid rgba(255,255,255,.08)}
.overview-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
.overview-strip .stat{min-height:102px}
.tab-bar{
  display:flex;
  gap:10px;
  padding:10px;
  margin-bottom:16px;
  background:rgba(9,17,29,.78);
  border:1px solid rgba(255,255,255,.06);
  border-radius:18px;
  overflow-x:auto;
  -webkit-overflow-scrolling:touch;
  backdrop-filter:blur(16px);
  position:sticky;
  top:74px;
  z-index:70;
}
.tab-btn{
  min-width:170px;
  padding:14px 16px;
  font-size:12px;
  font-weight:700;
  color:var(--t3);
  border:none;
  background:rgba(255,255,255,.02);
  cursor:pointer;
  white-space:nowrap;
  border-radius:14px;
  transition:.18s;
  letter-spacing:.2px;
  text-align:left;
}
.tab-btn:hover{color:var(--t2);background:rgba(255,255,255,.04)}
.tab-btn.active{color:var(--t1);background:linear-gradient(135deg, rgba(47,107,255,.22), rgba(96,165,250,.12));box-shadow:inset 0 0 0 1px rgba(96,165,250,.25)}
.tab-btn-label{display:block;font-family:'Space Grotesk','Manrope',sans-serif;font-size:15px;letter-spacing:-.3px}
.tab-btn-meta{display:block;font-size:11px;color:var(--t3);margin-top:4px}
.tab-btn.active .tab-btn-meta{color:var(--t2)}
.tab-pane{display:none;padding:4px 0 0}
.tab-pane.active{display:block}
.tab-pane-header{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.tab-pane-title{font-family:'Space Grotesk','Manrope',sans-serif;font-size:26px;letter-spacing:-.8px}
.tab-pane-copy{font-size:13px;color:var(--t2);max-width:780px;line-height:1.65}
.scanner-top-grid{display:grid;grid-template-columns:minmax(0,1.08fr) minmax(320px,.92fr);gap:14px;margin-bottom:14px}
.scanner-layout{display:grid;grid-template-columns:minmax(280px,320px) minmax(0,1fr);gap:14px}
.scanner-sidebar,.settings-stack{display:flex;flex-direction:column;gap:14px}
.scanner-sidebar{position:sticky;top:148px;align-self:start}
.scanner-main{min-width:0;display:flex;flex-direction:column;gap:14px}
.panel-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:14px;flex-wrap:wrap}
.panel-title{font-family:'Space Grotesk','Manrope',sans-serif;font-size:18px;letter-spacing:-.5px}
.panel-copy{font-size:12px;color:var(--t2);line-height:1.6;max-width:720px}
.panel-actions{display:flex;gap:8px;flex-wrap:wrap}
.control-note,.launch-note,.helper-note{font-size:11px;line-height:1.6;color:var(--t2)}
.control-action-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-top:14px}
.scanner-header-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.scanner-search{background:rgba(7,16,29,.72);border:1px solid rgba(255,255,255,.08);color:var(--t1);padding:8px 11px;border-radius:10px;font-size:11px;width:180px}
.scanner-search:focus{outline:none;border-color:rgba(96,165,250,.35);box-shadow:0 0 0 3px rgba(37,99,235,.15)}
.dex-row{cursor:pointer;transition:.12s}.dex-row:hover td{background:rgba(255,255,255,.03)}.dex-row.selected td{background:rgba(37,99,235,.08)}
.tok-icon{width:34px;height:34px;border-radius:11px;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:800;margin-right:10px;flex-shrink:0}
.tok-name{font-size:12px;font-weight:700;color:var(--t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:150px}
.tok-sym{font-size:10px;color:var(--t3)}
.chg-pos{color:var(--grn);font-weight:700;font-size:11px}.chg-neg{color:var(--red2);font-weight:700;font-size:11px}.chg-0{color:var(--t3);font-size:11px}
.price-val,.num-val{font-size:11px;color:var(--t2)}
.score-mini{width:52px;height:6px;background:rgba(255,255,255,.06);border-radius:999px;overflow:hidden}.score-fill{height:100%;border-radius:999px}
.sort-pill{padding:6px 11px;font-size:10px;border-radius:999px;border:1px solid rgba(255,255,255,.08);background:rgba(255,255,255,.02);color:var(--t3);cursor:pointer;font-weight:700}
.sort-pill.active{background:var(--blue);border-color:var(--blue);color:#fff}
.buy-btn-mini{padding:5px 10px;font-size:10px;font-weight:800;border:1px solid rgba(20,199,132,.35);background:rgba(20,199,132,.1);color:var(--grn);border-radius:8px;cursor:pointer;white-space:nowrap}
.buy-btn-mini:hover{background:var(--grn);color:#03140d}
.settings-pane-grid{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(320px,.85fr);gap:16px;align-items:start}
.settings-section-title{font-size:12px;font-weight:800;color:var(--t1);letter-spacing:.08em;text-transform:uppercase;margin-bottom:12px}
.setting-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(116px,160px) 72px;gap:10px;align-items:center;padding:11px 0;border-bottom:1px solid rgba(255,255,255,.06)}
.setting-row:last-child{border-bottom:none}
.setting-label{font-size:13px;color:var(--t2);font-weight:800}
.setting-desc{font-size:10px;color:var(--t3);margin-top:2px;line-height:1.45}
.setting-input{background:rgba(7,16,29,.72);border:1px solid rgba(255,255,255,.08);color:var(--t1);padding:9px 10px;border-radius:10px;font-size:12px;width:100%}
.setting-unit{font-size:11px;color:var(--t3);text-align:right}
.setting-toggle-row{display:flex;justify-content:space-between;align-items:center;padding:11px 0;border-bottom:1px solid rgba(255,255,255,.06);gap:14px}
.setting-toggle-row:last-child{border-bottom:none}
.toggle-wrap{display:flex;align-items:center;gap:10px}
.checkpoint-path{display:flex;flex-direction:column;gap:10px}
.checkpoint-card{border:1px solid rgba(59,130,246,.18);background:linear-gradient(180deg,rgba(8,16,26,.96),rgba(10,18,29,.82));border-radius:14px;padding:12px 14px}
.checkpoint-step{display:flex;align-items:flex-start;gap:10px}
.checkpoint-index{width:24px;height:24px;border-radius:999px;background:rgba(59,130,246,.14);border:1px solid rgba(59,130,246,.28);color:var(--blue2);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:800;flex-shrink:0}
.checkpoint-meta{font-size:10px;color:var(--t3);margin-top:4px;line-height:1.45}
.operator-map{display:flex;flex-direction:column;gap:14px}
.operator-lane{border:1px solid rgba(255,255,255,.06);background:linear-gradient(180deg,rgba(8,16,26,.92),rgba(11,21,34,.82));border-radius:16px;padding:14px}
.operator-lane-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:10px;flex-wrap:wrap}
.operator-lane-title{font-size:12px;font-weight:800;color:var(--t1);text-transform:uppercase;letter-spacing:.08em}
.operator-lane-note{font-size:11px;color:var(--t3);line-height:1.45;max-width:520px}
.operator-chip-row{display:flex;flex-wrap:wrap;gap:8px}
.operator-chip{padding:7px 10px;border-radius:999px;border:1px solid rgba(59,130,246,.24);background:rgba(59,130,246,.08);font-size:11px;color:var(--t2);font-weight:700}
.operator-chip.feed{border-color:rgba(20,199,132,.22);background:rgba(20,199,132,.08);color:var(--grn)}
.operator-chip.guard{border-color:rgba(245,158,11,.24);background:rgba(245,158,11,.08);color:var(--gold2)}
.operator-chip.exit{border-color:rgba(220,38,38,.24);background:rgba(220,38,38,.08);color:#ff8b8b}
.operator-stage-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.operator-stage{border:1px solid rgba(59,130,246,.12);background:rgba(14,23,36,.82);border-radius:14px;padding:12px}
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
.save-pill{padding:7px 12px;border-radius:999px;border:1px solid rgba(20,199,132,.24);background:rgba(20,199,132,.08);font-size:11px;color:var(--grn);font-weight:700}
.save-pill.pending{border-color:rgba(245,158,11,.24);background:rgba(245,158,11,.08);color:var(--gold2)}
.ai-panel{background:linear-gradient(135deg,rgba(20,199,132,.14),rgba(59,130,246,.08));border:1px solid rgba(20,199,132,.24);border-radius:18px;padding:18px;margin-bottom:18px}
.ai-panel h3{margin:0 0 6px;font-size:16px;color:var(--t1)}
.ai-panel p{margin:0;color:var(--t2);font-size:12px;line-height:1.6}
.ai-stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin:14px 0}
.ai-stat{background:rgba(7,13,23,.45);border:1px solid rgba(255,255,255,.07);border-radius:12px;padding:12px}
.ai-stat-value{font-size:20px;font-weight:800;color:var(--t1)}
.ai-stat-label{font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.08em;margin-top:4px}
.signals-layout{display:grid;grid-template-columns:minmax(0,1fr) 420px;gap:16px;align-items:start}
.signals-side{display:flex;flex-direction:column;gap:14px;position:sticky;top:148px}
.sig-table-wrap{max-height:660px;overflow:auto}
.sig-table{width:100%;border-collapse:collapse;font-size:11px;min-width:1520px}
.sig-table th{position:sticky;top:0;background:#0c1522;color:var(--t3);font-size:10px;text-transform:uppercase;letter-spacing:.08em;padding:10px 8px;border-bottom:1px solid rgba(255,255,255,.08);z-index:1}
.sig-table td{padding:10px 8px;border-bottom:1px solid rgba(26,46,69,.5);vertical-align:top}
.sig-table tbody tr{cursor:pointer;transition:.12s}
.sig-table tbody tr:hover{background:rgba(255,255,255,.025)}
.sig-table tbody tr.active{background:rgba(37,99,235,.08)}
.sig-cell-token{min-width:180px}
.sig-token-name{font-weight:700;color:var(--t1);font-size:12px}
.sig-token-meta{font-size:10px;color:var(--t3)}
.sig-checks{display:flex;gap:4px;flex-wrap:wrap;min-width:180px}
.sig-check{padding:3px 6px;border-radius:999px;font-size:9px;font-weight:700;border:1px solid transparent;white-space:nowrap}
.sig-check.pass{color:var(--grn);border-color:rgba(20,199,132,.35);background:rgba(20,199,132,.08)}
.sig-check.fail{color:var(--red2);border-color:rgba(220,38,38,.35);background:rgba(220,38,38,.08)}
.sig-reason{max-width:300px;color:var(--t2);line-height:1.35}
.sig-mini-muted{font-size:10px;color:var(--t3)}
.sig-badge{padding:2px 8px;border-radius:12px;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.3px}
.sig-pass{background:rgba(20,199,132,.15);color:var(--grn)}.sig-fail{background:rgba(220,38,38,.15);color:var(--red2)}
.score-bar{height:6px;border-radius:999px;background:rgba(255,255,255,.06);overflow:hidden;width:60px;flex-shrink:0}
.score-bar-fill{height:100%;border-radius:999px}
.filter-step{display:flex;align-items:center;gap:8px;padding:5px 0;font-size:12px;color:var(--t2)}
.filter-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.filter-dot.pass{background:var(--grn)}.filter-dot.fail{background:var(--red2)}
.whale-card-row{display:flex;gap:12px;margin-bottom:16px;overflow-x:auto;padding-bottom:4px}
.whale-entry{display:flex;align-items:center;gap:12px;padding:12px 14px;border-bottom:1px solid rgba(255,255,255,.06);animation:wslide .3s ease}
@keyframes wslide{from{opacity:0;transform:translateX(-16px)}to{opacity:1;transform:translateX(0)}}
.whale-card{border-radius:18px;padding:16px;min-width:220px}
.positions-layout{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:16px}
.pos-card{border-radius:18px;padding:16px;margin-bottom:12px;transition:.2s}
.pos-card:hover{border-color:rgba(96,165,250,.28)}
.risk-bar{height:8px;border-radius:999px;background:rgba(255,255,255,.06);overflow:hidden;margin-top:4px}
.risk-fill{height:100%;border-radius:999px;transition:width .3s}
.pnl-layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(300px,.75fr);gap:16px}
.tree-wallet-row{display:flex;align-items:center;gap:8px;padding:8px 10px;border-bottom:1px solid rgba(255,255,255,.06);font-size:11px;cursor:pointer;transition:.1s}
.tree-wallet-row:hover{background:rgba(255,255,255,.03)}
.tree-wallet-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.tree-wallet-addr{flex:1;color:var(--t2);font-size:10px;overflow:hidden;text-overflow:ellipsis}
.tree-wallet-sol{color:var(--t1);font-weight:700;font-size:10px}
.fp-item{display:flex;align-items:center;gap:6px;padding:4px 0;font-size:11px}
.fp-pass{color:var(--grn);font-weight:700;font-size:10px}.fp-fail{color:var(--red2);font-weight:700;font-size:10px}
.fp-name{color:var(--t2);font-weight:500}.fp-reason{color:var(--t3);font-size:10px;flex:1;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.confirm-modal{position:fixed;inset:0;background:rgba(2,6,23,.76);display:none;align-items:center;justify-content:center;z-index:260;padding:20px}
.confirm-modal.show{display:flex}
.confirm-card{width:min(420px,100%);background:linear-gradient(180deg,#0d1726,#08101a);border:1px solid rgba(59,130,246,.28);border-radius:20px;padding:20px;box-shadow:0 24px 80px rgba(0,0,0,.45)}
.confirm-title{font-family:'Space Grotesk','Manrope',sans-serif;font-size:18px;font-weight:700;color:var(--t1);margin-bottom:8px}
.confirm-body{font-size:13px;line-height:1.6;color:var(--t2);margin-bottom:16px}
.confirm-meta{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}
/* ── Floating Portfolio FAB ─────────────────────────────────── */
#portfolio-fab{
  position:fixed;bottom:226px;right:24px;z-index:200;
  width:52px;height:52px;border-radius:50%;
  background:linear-gradient(135deg,#2F6BFF,#14C784);
  display:flex;align-items:center;justify-content:center;
  cursor:pointer;box-shadow:0 6px 24px rgba(20,199,132,.35);
  transition:transform .2s,box-shadow .2s;
  color:#fff;
}
#portfolio-fab:hover{transform:scale(1.1);box-shadow:0 8px 32px rgba(20,199,132,.5)}
.fab-count{
  position:absolute;top:-4px;right:-4px;
  min-width:20px;height:20px;padding:0 5px;
  border-radius:999px;font-size:10px;font-weight:800;
  background:#f23645;color:#fff;
  display:flex;align-items:center;justify-content:center;
  border:2px solid rgba(6,13,23,.9);
}
.fab-count.zero{background:rgba(100,116,139,.5)}
.portfolio-panel{
  position:fixed;bottom:226px;right:24px;z-index:199;
  width:360px;max-height:480px;
  background:linear-gradient(180deg,rgba(13,23,38,.98),rgba(8,16,26,.98));
  border:1px solid rgba(47,107,255,.25);
  border-radius:20px;
  box-shadow:0 20px 60px rgba(0,0,0,.5),0 0 40px rgba(47,107,255,.1);
  backdrop-filter:blur(20px);
  display:none;
  flex-direction:column;
  overflow:hidden;
  animation:panelSlideUp .25s ease;
}
@keyframes panelSlideUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}
.portfolio-panel.open{display:flex}
.portfolio-header{
  display:flex;justify-content:space-between;align-items:flex-start;
  padding:16px 18px 12px;
  border-bottom:1px solid rgba(255,255,255,.06);
}
.portfolio-coins{
  flex:1;overflow-y:auto;padding:6px 0;
  max-height:380px;
}
.portfolio-coin{
  display:flex;align-items:center;gap:12px;
  padding:12px 18px;
  border-bottom:1px solid rgba(255,255,255,.04);
  transition:background .12s;
  cursor:default;
}
.portfolio-coin:hover{background:rgba(255,255,255,.03)}
.portfolio-coin:last-child{border-bottom:none}
.coin-icon{
  width:36px;height:36px;border-radius:12px;
  display:flex;align-items:center;justify-content:center;
  font-size:13px;font-weight:800;color:#fff;
  flex-shrink:0;
}
.coin-info{flex:1;min-width:0}
.coin-name{font-size:13px;font-weight:700;color:var(--t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.coin-meta{font-size:10px;color:var(--t3);margin-top:2px}
.coin-pnl{text-align:right;flex-shrink:0}
.coin-pnl-pct{font-size:16px;font-weight:800}
.coin-pnl-sol{font-size:10px;color:var(--t3);margin-top:2px}
.portfolio-empty{
  text-align:center;padding:40px 20px;color:var(--t3);font-size:12px;line-height:1.6;
}
.portfolio-total-row{
  display:flex;justify-content:space-between;align-items:center;
  padding:12px 18px;
  border-top:1px solid rgba(255,255,255,.08);
  background:rgba(7,14,23,.5);
}

.activity-shell{position:fixed;bottom:0;left:0;right:0;height:210px;background:rgba(6,13,23,.92);border-top:1px solid rgba(255,255,255,.06);display:flex;flex-direction:column;z-index:90;backdrop-filter:blur(18px);transition:height .2s;box-shadow:0 -18px 42px rgba(2,8,23,.25)}
.activity-head{display:flex;justify-content:space-between;align-items:center;padding:8px 16px;border-bottom:1px solid rgba(255,255,255,.06);flex-shrink:0}
.activity-title{display:flex;align-items:center;gap:8px;font-size:11px;font-weight:700;color:var(--t2)}
.activity-actions{display:flex;align-items:center;gap:8px}
.activity-log{flex:1;overflow-y:auto;display:flex;flex-direction:column-reverse;padding:4px 0}
@media(max-width:1220px){
  .dashboard-hero,.scanner-top-grid,.signals-layout,.positions-layout,.pnl-layout,.settings-pane-grid{grid-template-columns:1fr}
  .signals-side,.scanner-sidebar{position:static}
}
@media(max-width:860px){
  .hero-title{font-size:30px}
  .hero-grid,.account-grid,.intel-metrics,.scanner-summary-grid,.pnl-summary-grid,.launch-grid{grid-template-columns:1fr}
  .scanner-layout{grid-template-columns:1fr}
  .tab-bar{top:70px;padding:8px}
  .tab-btn{min-width:150px;padding:12px 14px}
}
@media(max-width:640px){
  .dashboard-shell{padding-top:12px}
  .wrap{padding-left:16px;padding-right:16px}
  .hero-panel,.glass,.settings-card{padding:16px}
  .hero-actions,.panel-actions,.scanner-header-actions{flex-direction:column;align-items:stretch}
  .scanner-search{width:100%}
  .setting-row{grid-template-columns:1fr}
  .setting-unit{text-align:left}
  .activity-shell{height:190px}
}
</style>
</head><body>

<nav class="nav">
  <a href="/" class="logo"><div class="logo-mark">S</div>SolTrader</a>
  <div class="nav-r">
    <div class="status"><div id="sdot" class="sdot sdot-off"></div><span id="stxt" class="stxt">Loading…</span></div>
    <a href="/logout">Sign Out</a>
  </div>
</nav>

<!-- ticker strip removed -->

<div class="wrap dashboard-shell">

  <!-- Animated Promo Ribbon -->
  <style>
  @keyframes dashPromoSlide{0%{transform:translateX(0)}100%{transform:translateX(-50%)}}
  .dash-promo-banner{
    position:relative;overflow:hidden;
    background:linear-gradient(135deg,rgba(8,18,38,.95),rgba(14,28,52,.92));
    border:1px solid rgba(20,199,132,.2);border-radius:16px;
    padding:0;margin-bottom:16px;
  }
  .dash-promo-banner::after{
    content:"";position:absolute;inset:-1px;border-radius:17px;padding:1px;
    background:linear-gradient(90deg,#14c784,#3b82f6,#a855f7,#f59e0b,#14c784);
    background-size:200% 100%;animation:promoBorderRun 4s linear infinite;
    -webkit-mask:linear-gradient(#fff 0 0) content-box,linear-gradient(#fff 0 0);
    mask:linear-gradient(#fff 0 0) content-box,linear-gradient(#fff 0 0);
    -webkit-mask-composite:xor;mask-composite:exclude;pointer-events:none;
  }
  .dash-promo-inner{
    position:relative;z-index:2;
    display:grid;grid-template-columns:1fr auto;align-items:center;gap:20px;
    padding:18px 24px;
  }
  .dash-promo-inner::before{
    content:"";position:absolute;top:0;left:-100%;width:50%;height:100%;
    background:linear-gradient(90deg,transparent,rgba(255,255,255,.03),transparent);
    animation:promoShimmer 5s ease-in-out infinite;
  }
  .dash-promo-headline{font-size:15px;font-weight:900;color:var(--t1);font-family:'Space Grotesk','Manrope',sans-serif;letter-spacing:-.3px}
  .dash-promo-sub{font-size:11px;color:var(--t3);margin-top:4px;line-height:1.5}
  .dash-promo-stats{display:flex;gap:20px;align-items:center}
  .dash-promo-stat{text-align:center}
  .dash-promo-stat-val{font-size:20px;font-weight:900;font-family:'Space Grotesk','Manrope',sans-serif;letter-spacing:-.5px;animation:promoNumber .8s ease forwards}
  .dash-promo-stat-lbl{font-size:8px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--t3);margin-top:2px}
  .dash-promo-scroll{
    overflow:hidden;border-top:1px solid rgba(255,255,255,.04);padding:8px 0;position:relative;z-index:2;
  }
  .dash-promo-scroll-inner{
    display:flex;gap:32px;white-space:nowrap;
    animation:dashPromoSlide 25s linear infinite;
    font-size:11px;font-weight:600;color:var(--t3);
  }
  .dash-promo-scroll-item{display:inline-flex;align-items:center;gap:6px}
  .dash-promo-scroll-dot{width:6px;height:6px;border-radius:50%}
  </style>
  <div class="dash-promo-banner" id="dash-promo-banner">
    <div class="dash-promo-inner">
      <div>
        <div class="dash-promo-headline">Peak Plateau Rider <span style="color:#14c784">Active</span></div>
        <div class="dash-promo-sub">Progressive trailing locks gains at the peak. No fixed TP2 cap — ride 2000%+ runners to their plateau.</div>
      </div>
      <div class="dash-promo-stats">
        <div class="dash-promo-stat">
          <div class="dash-promo-stat-val" style="color:#14c784" id="dp-trades">0</div>
          <div class="dash-promo-stat-lbl">Shadow Trades</div>
        </div>
        <div class="dash-promo-stat">
          <div class="dash-promo-stat-val" style="color:#3b82f6" id="dp-winrate">—</div>
          <div class="dash-promo-stat-lbl">Win Rate</div>
        </div>
        <div class="dash-promo-stat">
          <div class="dash-promo-stat-val" style="color:#a855f7" id="dp-best">—</div>
          <div class="dash-promo-stat-lbl">Best Trade</div>
        </div>
      </div>
    </div>
    <div class="dash-promo-scroll">
      <div class="dash-promo-scroll-inner">
        <span class="dash-promo-scroll-item"><span class="dash-promo-scroll-dot" style="background:#14c784"></span>30% trail under 3x</span>
        <span class="dash-promo-scroll-item"><span class="dash-promo-scroll-dot" style="background:#14c784"></span>25% trail at 3x</span>
        <span class="dash-promo-scroll-item"><span class="dash-promo-scroll-dot" style="background:#3b82f6"></span>20% trail at 5x</span>
        <span class="dash-promo-scroll-item"><span class="dash-promo-scroll-dot" style="background:#3b82f6"></span>15% trail at 10x</span>
        <span class="dash-promo-scroll-item"><span class="dash-promo-scroll-dot" style="background:#a855f7"></span>12% trail at 20x</span>
        <span class="dash-promo-scroll-item"><span class="dash-promo-scroll-dot" style="background:#f59e0b"></span>10% trail at 50x — locks mega gains</span>
        <span class="dash-promo-scroll-item"><span class="dash-promo-scroll-dot" style="background:#14c784"></span>AI Regime Detection</span>
        <span class="dash-promo-scroll-item"><span class="dash-promo-scroll-dot" style="background:#a855f7"></span>Whale Exit Signals</span>
        <span class="dash-promo-scroll-item"><span class="dash-promo-scroll-dot" style="background:#f59e0b"></span>Anti-Rug Protection</span>
        <!-- duplicate for seamless scroll -->
        <span class="dash-promo-scroll-item"><span class="dash-promo-scroll-dot" style="background:#14c784"></span>30% trail under 3x</span>
        <span class="dash-promo-scroll-item"><span class="dash-promo-scroll-dot" style="background:#14c784"></span>25% trail at 3x</span>
        <span class="dash-promo-scroll-item"><span class="dash-promo-scroll-dot" style="background:#3b82f6"></span>20% trail at 5x</span>
        <span class="dash-promo-scroll-item"><span class="dash-promo-scroll-dot" style="background:#3b82f6"></span>15% trail at 10x</span>
        <span class="dash-promo-scroll-item"><span class="dash-promo-scroll-dot" style="background:#a855f7"></span>12% trail at 20x</span>
        <span class="dash-promo-scroll-item"><span class="dash-promo-scroll-dot" style="background:#f59e0b"></span>10% trail at 50x — locks mega gains</span>
        <span class="dash-promo-scroll-item"><span class="dash-promo-scroll-dot" style="background:#14c784"></span>AI Regime Detection</span>
        <span class="dash-promo-scroll-item"><span class="dash-promo-scroll-dot" style="background:#a855f7"></span>Whale Exit Signals</span>
        <span class="dash-promo-scroll-item"><span class="dash-promo-scroll-dot" style="background:#f59e0b"></span>Anti-Rug Protection</span>
      </div>
    </div>
  </div>

  <div class="dashboard-hero">
    <div class="hero-panel hero-panel-primary">
      <div class="hero-kicker">Trading Cockpit</div>
      <div class="hero-title">One workspace for discovery, execution, and risk control.</div>
      <div class="hero-copy">
        Scan new Solana flow, inspect every rejection, watch whale activity, and manage live positions without jumping between disconnected panels.
      </div>
      <div class="hero-chip-row" style="margin-top:14px">
        <span class="badge bg-blue" id="hero-preset-badge">Preset loading…</span>
        <span class="badge bg-muted">{{PLAN_LABEL}}</span>
        <span class="badge bg-muted" id="hero-live-state">Connecting…</span>
        <span class="badge bg-muted" id="hero-execution-badge">Execution lane loading…</span>
        <span class="badge bg-muted">Operator {{EMAIL}}</span>
      </div>
      <div class="hero-actions">
        <button id="toggle-btn" class="btn btn-success" onclick="toggleBot()">▶ Start Bot</button>
        <button class="btn btn-blue" type="button" onclick="applyShadowTrading()" title="Apply optimal shadow trading settings">⚙️ Shadow Trading Mode</button>
        <div style="display:flex;align-items:center;gap:8px">
          <label style="font-size:12px;color:var(--t3)">Max Open Buys:</label>
          <input type="number" id="max-positions-input" min="1" max="20" value="5" style="width:50px;padding:4px 8px;border:1px solid var(--border);border-radius:4px;background:var(--bg1);color:var(--t1);font-family:monospace;font-size:12px">
          <button class="btn btn-ghost" type="button" onclick="updateMaxPositions()" style="padding:4px 12px;font-size:12px">Apply</button>
        </div>
        <button class="btn btn-ghost" type="button" onclick="refreshNow()">Refresh Now</button>
        <button class="btn btn-ghost" type="button" onclick="openSettingsTab()">Tune Settings</button>
        <button class="btn btn-ghost" type="button" onclick="focusActivityLog()">Activity Log</button>
        <button class="btn btn-danger" type="button" onclick="cashout()">Emergency Cashout</button>
      </div>
      <div class="hero-grid">
        <div class="hero-mini">
          <div class="hero-mini-label">Saved Launch Profile</div>
          <div class="hero-mini-value" id="hero-launch-mode">Balanced</div>
          <div class="hero-mini-copy">Persisted settings drive the engine every time you start it.</div>
        </div>
        <div class="hero-mini">
          <div class="hero-mini-label">Last Sync</div>
          <div class="hero-mini-value" id="hero-sync-time">Waiting…</div>
          <div class="hero-mini-copy">Market feed, scanner state, and risk panels update continuously.</div>
        </div>
      </div>
    </div>

    <div class="hero-side-stack">
      <div class="hero-panel">
        <div class="panel-kicker">Account & Access</div>
        <div class="account-grid">
          <div class="account-cell">
            <div class="account-label">Plan</div>
            <div class="account-value">{{PLAN_LABEL}}</div>
            <div class="account-copy">Upgrade paths stay available without leaving the dashboard.</div>
          </div>
          <div class="account-cell">
            <div class="account-label">Wallet</div>
            <div class="account-value" style="font-size:13px;word-break:break-all">{{WALLET}}</div>
            <div class="account-copy">Execution signs directly from your configured Solana wallet.</div>
          </div>
        </div>
        <div class="panel-actions" style="margin-top:14px">
          {{UPGRADE_BTN}}
          <a href="/setup" class="btn btn-ghost">Wallet Setup</a>
          <a href="/manage-subscription" style="display:inline-block;margin-top:6px;font-size:11px;color:var(--t3);text-decoration:underline">Manage Subscription</a>
        </div>
        <div style="margin-top:16px;padding-top:14px;border-top:1px solid var(--b1)">
          <div style="font-size:12px;font-weight:700;color:var(--t1);margin-bottom:6px">Telegram Alerts</div>
          <div style="font-size:11px;color:var(--t3);line-height:1.6;margin-bottom:10px">Paste your Telegram `chat_id` once. Saving here sends a confirmation message and enables buy, sell, and edge-guard alerts.</div>
          <div class="field-row" style="margin-bottom:8px">
            <div class="fgroup" style="margin:0">
              <label class="flabel">Chat ID</label>
              <input class="finput" id="telegram-chat-id" type="text" placeholder="e.g. 6803261718">
            </div>
          </div>
          <div class="shortcut-row" style="margin-bottom:10px">
            <span class="badge bg-muted" id="telegram-status-badge">Checking Telegram status…</span>
          </div>
          <div class="panel-actions">
            <button class="btn btn-primary" type="button" onclick="saveTelegramChat()">Save Telegram</button>
            <button class="btn btn-ghost" type="button" onclick="clearTelegramChat()">Clear</button>
          </div>
        </div>
      </div>

      <div class="hero-panel">
        <div class="panel-kicker">Advanced Telemetry</div>
        <div style="font-size:13px;color:var(--t2);line-height:1.65;margin-bottom:12px" id="enhanced-summary">
          Checking enhanced risk, MEV, and observability services…
        </div>
        <div class="intel-metrics" id="enhanced-metrics">
          <div class="intel-metric">
            <div class="intel-label">System</div>
            <div class="intel-value">Loading…</div>
            <div class="intel-copy">Waiting for telemetry</div>
          </div>
          <div class="intel-metric">
            <div class="intel-label">Alerts</div>
            <div class="intel-value">—</div>
            <div class="intel-copy">No data yet</div>
          </div>
        </div>
        <div class="intel-alerts" id="enhanced-alerts" style="margin-top:12px">
          <span class="badge bg-muted">Telemetry initializing</span>
        </div>
        <div class="shortcut-row" style="margin-top:14px">
          <span class="badge">`1-6` switch tabs</span>
          <span class="badge">`/` focus search</span>
          <span class="badge">`R` refresh</span>
          <span class="badge">`S` settings</span>
        </div>
      </div>
    </div>
  </div>

  <div class="overview-strip">
    <div class="stat"><div class="slabel">Balance</div><div class="sval"><span id="balance">—</span> <span style="font-size:12px;color:var(--t3)">SOL</span></div></div>
    <div class="stat"><div class="slabel">Positions</div><div class="sval" id="pos-count">0</div></div>
    <div class="stat"><div class="slabel">Wins</div><div class="sval c-grn" id="wins">0</div></div>
    <div class="stat"><div class="slabel">Losses</div><div class="sval c-red" id="losses">0</div></div>
    <div class="stat"><div class="slabel">Win Rate</div><div class="sval c-blue" id="win-rate">—</div></div>
    <div class="stat"><div class="slabel">Session P&L</div><div class="sval" id="pnl">—</div></div>
    <div class="stat"><div class="slabel">Streak</div><div class="sval" id="streak">—</div></div>
  </div>

  <!-- ═══════════ LIVE SHADOW PERFORMANCE BANNER ═══════════ -->
  <style>
  @keyframes livePulse{0%,100%{box-shadow:0 0 6px rgba(20,199,132,.4)}50%{box-shadow:0 0 18px rgba(20,199,132,.8)}}
  @keyframes fadeSlideIn{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:translateY(0)}}
  @keyframes countUp{from{opacity:.5;transform:scale(.92)}to{opacity:1;transform:scale(1)}}
  @keyframes flashGreen{0%{background:rgba(20,199,132,.35)}100%{background:rgba(20,199,132,.06)}}
  @keyframes flashRed{0%{background:rgba(239,68,68,.35)}100%{background:rgba(239,68,68,.06)}}
  @keyframes gradientShift{0%{background-position:0% 50%}50%{background-position:100% 50%}100%{background-position:0% 50%}}
  .live-banner{
    position:relative;overflow:hidden;
    background:linear-gradient(135deg,rgba(10,22,40,.95),rgba(14,28,50,.92),rgba(8,18,34,.95));
    background-size:200% 200%;animation:gradientShift 8s ease infinite;
    border:1px solid rgba(20,199,132,.2);border-radius:20px;padding:16px 20px;margin-bottom:16px;
    display:grid;grid-template-columns:auto 1fr auto;gap:16px;align-items:center;
  }
  .live-banner::before{content:"";position:absolute;top:-50%;left:-50%;width:200%;height:200%;background:radial-gradient(circle at 20% 50%,rgba(20,199,132,.06),transparent 50%),radial-gradient(circle at 80% 50%,rgba(59,130,246,.06),transparent 50%);pointer-events:none}
  .live-dot-wrap{display:flex;align-items:center;gap:8px}
  .live-dot{width:10px;height:10px;border-radius:50%;background:#14c784;animation:livePulse 1.5s ease-in-out infinite}
  .live-label{font-size:11px;font-weight:800;letter-spacing:.15em;text-transform:uppercase;color:#14c784}
  .live-stats{display:flex;gap:20px;flex-wrap:wrap;align-items:center}
  .live-stat{display:flex;flex-direction:column;align-items:center;min-width:80px}
  .live-stat-val{font-family:'Space Grotesk','Manrope',sans-serif;font-size:22px;font-weight:800;letter-spacing:-.5px;transition:all .3s}
  .live-stat-val.updating{animation:countUp .4s ease}
  .live-stat-lbl{font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:var(--t3);margin-top:2px}
  .live-stat-val.pos{color:#14c784}.live-stat-val.neg{color:#ef4444}.live-stat-val.neu{color:var(--blue2)}
  .live-divider{width:1px;height:32px;background:rgba(255,255,255,.08)}
  .live-feed{display:flex;flex-direction:column;gap:4px;max-height:64px;overflow:hidden}
  .live-feed-item{font-size:11px;padding:4px 10px;border-radius:8px;display:flex;align-items:center;gap:6px;animation:fadeSlideIn .4s ease}
  .live-feed-item.win{background:rgba(20,199,132,.06);border:1px solid rgba(20,199,132,.15);color:#14c784}
  .live-feed-item.loss{background:rgba(239,68,68,.06);border:1px solid rgba(239,68,68,.15);color:#ef4444}
  .live-feed-icon{font-size:13px}.live-feed-name{font-weight:700;max-width:100px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .live-feed-pnl{font-weight:800;margin-left:auto}
  .shadow-auto-tag{display:inline-flex;align-items:center;gap:4px;font-size:9px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;padding:3px 8px;border-radius:6px;background:rgba(59,130,246,.12);border:1px solid rgba(59,130,246,.25);color:var(--blue2);margin-left:8px}
  </style>

  <div class="live-banner" id="live-banner">
    <div class="live-dot-wrap">
      <div class="live-dot"></div>
      <div>
        <div class="live-label">Shadow Trading Live</div>
        <div style="font-size:10px;color:var(--t3);margin-top:2px">AI auto-tuning filters in real time</div>
      </div>
    </div>
    <div class="live-stats">
      <div class="live-stat">
        <div class="live-stat-val neu" id="lv-shadow-trades">0</div>
        <div class="live-stat-lbl">Shadow Trades</div>
      </div>
      <div class="live-divider"></div>
      <div class="live-stat">
        <div class="live-stat-val neu" id="lv-shadow-wr">—</div>
        <div class="live-stat-lbl">Win Rate</div>
      </div>
      <div class="live-divider"></div>
      <div class="live-stat">
        <div class="live-stat-val neu" id="lv-shadow-pnl">0%</div>
        <div class="live-stat-lbl">Avg P&L</div>
      </div>
      <div class="live-divider"></div>
      <div class="live-stat">
        <div class="live-stat-val neu" id="lv-shadow-best">—</div>
        <div class="live-stat-lbl">Best Strategy</div>
      </div>
      <span class="shadow-auto-tag" id="lv-auto-tune-tag">Auto-Tune Active</span>
    </div>
    <div class="live-feed" id="live-feed">
      <div class="live-feed-item win"><span class="live-feed-icon">+</span><span style="color:var(--t3)">Waiting for trades...</span></div>
    </div>
    <div id="lv-compare" style="display:none;border-top:1px solid rgba(255,255,255,.06);padding:8px 18px;font-size:11px">
      <div style="display:flex;gap:20px;align-items:center;flex-wrap:wrap">
        <span style="font-weight:800;color:var(--t3);text-transform:uppercase;letter-spacing:.06em;font-size:9px">Before vs After Optimization</span>
        <div style="display:flex;gap:16px" id="lv-compare-stats"></div>
      </div>
    </div>
  </div>
  <!-- ═══════════ END LIVE BANNER ═══════════ -->

  <!-- ═══════════ SHADOW ACTIVITY WINDOW ═══════════ -->
  <style>
  .shadow-activity{background:rgba(8,16,32,.92);border:1px solid rgba(20,199,132,.12);border-radius:16px;padding:0;margin-bottom:16px;overflow:hidden}
  .shadow-activity-header{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid rgba(255,255,255,.06);cursor:pointer;user-select:none}
  .shadow-activity-header:hover{background:rgba(20,199,132,.04)}
  .shadow-activity-title{display:flex;align-items:center;gap:10px}
  .shadow-activity-title h3{margin:0;font-size:14px;font-weight:800;color:var(--t1);font-family:'Space Grotesk','Manrope',sans-serif}
  .shadow-activity-badges{display:flex;gap:6px;flex-wrap:wrap}
  .shadow-activity-chevron{transition:transform .25s;font-size:16px;color:var(--t3)}
  .shadow-activity-chevron.open{transform:rotate(180deg)}
  .shadow-activity-body{display:none;padding:0 18px 18px}
  .shadow-activity-body.open{display:block}
  .shadow-activity-tabs{display:flex;gap:0;border-bottom:1px solid rgba(255,255,255,.06);margin-bottom:14px}
  .shadow-activity-tab{padding:10px 16px;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--t3);cursor:pointer;border-bottom:2px solid transparent;transition:all .2s}
  .shadow-activity-tab.active{color:#14c784;border-bottom-color:#14c784}
  .shadow-activity-tab:hover{color:var(--t1)}
  .shadow-pos-card{padding:12px 14px;border:1px solid rgba(255,255,255,.06);border-radius:12px;background:rgba(255,255,255,.02);margin-bottom:8px;transition:border-color .2s}
  .shadow-pos-card:hover{border-color:rgba(20,199,132,.2)}
  .shadow-pos-row{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
  .shadow-pos-name{font-size:12px;font-weight:800;color:var(--t1)}
  .shadow-pos-meta{font-size:10px;color:var(--t3);margin-top:3px}
  .shadow-pos-pnl{font-size:14px;font-weight:800;letter-spacing:-.3px}
  .shadow-stats-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:10px;margin-bottom:14px}
  .shadow-stat-card{text-align:center;padding:10px;border:1px solid rgba(255,255,255,.06);border-radius:10px;background:rgba(255,255,255,.02)}
  .shadow-stat-card .val{font-size:18px;font-weight:800;font-family:'Space Grotesk','Manrope',sans-serif}
  .shadow-stat-card .lbl{font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--t3);margin-top:3px}
  </style>
  <div class="shadow-activity" id="shadow-activity-panel">
    <div class="shadow-activity-header" onclick="toggleShadowActivity()">
      <div class="shadow-activity-title">
        <h3>Shadow Trading Activity</h3>
        <span class="badge bg-grn" id="sa-status">Live</span>
        <span class="badge bg-muted" id="sa-open-count">0 open</span>
        <span class="badge bg-muted" id="sa-closed-count">0 closed</span>
      </div>
      <span class="shadow-activity-chevron" id="sa-chevron">&#9660;</span>
    </div>
    <div class="shadow-activity-body" id="sa-body">
      <div class="shadow-stats-strip" id="sa-stats-strip">
        <div class="shadow-stat-card"><div class="val neu" id="sa-wr">—</div><div class="lbl">Win Rate</div></div>
        <div class="shadow-stat-card"><div class="val neu" id="sa-avg-pnl">—</div><div class="lbl">Avg P&L</div></div>
        <div class="shadow-stat-card"><div class="val c-grn" id="sa-best">—</div><div class="lbl">Best Trade</div></div>
        <div class="shadow-stat-card"><div class="val c-red" id="sa-worst">—</div><div class="lbl">Worst Trade</div></div>
        <div class="shadow-stat-card"><div class="val neu" id="sa-total-pnl">—</div><div class="lbl">Median P&L</div></div>
        <div class="shadow-stat-card"><div class="val neu" id="sa-total-closed">0</div><div class="lbl">Trades Closed</div></div>
      </div>
        <div style="font-size:10px;color:var(--t3);padding:4px 0 10px;line-height:1.5;font-style:italic">
          * Shadow results are simulated — no slippage, fees, or execution constraints. Not indicative of real trading performance.
        </div>
      <div class="shadow-activity-tabs">
        <div class="shadow-activity-tab active" onclick="switchShadowTab('open')">Open Positions</div>
        <div class="shadow-activity-tab" onclick="switchShadowTab('closed')">Recent Closed</div>
      </div>
      <div id="sa-open-list">
        <div style="text-align:center;color:var(--t3);font-size:12px;padding:20px 0">Loading shadow positions...</div>
      </div>
      <div id="sa-closed-list" style="display:none">
        <div style="text-align:center;color:var(--t3);font-size:12px;padding:20px 0">Loading closed trades...</div>
      </div>
    </div>
  </div>
  <!-- ═══════════ END SHADOW ACTIVITY ═══════════ -->

  <!-- Tab Bar -->
  <div class="tab-bar">
    <button class="tab-btn active" data-tab="scanner" onclick="activateTab('scanner')"><span class="tab-btn-label">Scan</span><span class="tab-btn-meta">Evaluation ramp &amp; approved coins</span></button>
    <button class="tab-btn" data-tab="settings" onclick="activateTab('settings')"><span class="tab-btn-label">Settings</span><span class="tab-btn-meta">Saved checkpoint controls</span></button>
    <button class="tab-btn" data-tab="signals" onclick="activateTab('signals')"><span class="tab-btn-label">Signals</span><span class="tab-btn-meta">Why tokens passed or failed</span></button>
    <button class="tab-btn" data-tab="whales" onclick="activateTab('whales')"><span class="tab-btn-label">Whales</span><span class="tab-btn-meta">Tracked smart money flow</span></button>
    <button class="tab-btn" data-tab="positions" onclick="activateTab('positions')"><span class="tab-btn-label">Positions</span><span class="tab-btn-meta">Open trades and risk posture</span></button>
    <button class="tab-btn" data-tab="pnl" onclick="activateTab('pnl')"><span class="tab-btn-label">P&L</span><span class="tab-btn-meta">Equity curve and drawdown</span></button>
    <button class="tab-btn" data-tab="quant" onclick="activateTab('quant')"><span class="tab-btn-label">Quant</span><span class="tab-btn-meta">Replay runs and strategy research</span></button>
    <button class="tab-btn" data-tab="paper" onclick="activateTab('paper')"><span class="tab-btn-label">Paper</span><span class="tab-btn-meta">Simulated trades, no real money</span></button>
  </div>

  <!-- ═══════════════════════ SCAN TAB — EVALUATION RAMP ═══════════════════════ -->
  <style>
  .pos{color:#14c784}.neg{color:#ef4444}.neu{color:var(--blue2)}

  /* ── Pipeline funnel strip ───────────────────────────── */
  .ev-pipeline{display:flex;align-items:center;gap:0;margin-bottom:16px;background:rgba(8,16,32,.72);border:1px solid rgba(255,255,255,.06);border-radius:14px;padding:6px 8px;overflow-x:auto}
  .ev-pipe-stage{flex:1;min-width:100px;text-align:center;padding:10px 8px;position:relative}
  .ev-pipe-stage+.ev-pipe-stage::before{content:"";position:absolute;left:-1px;top:20%;height:60%;width:1px;background:rgba(255,255,255,.08)}
  .ev-pipe-num{font-size:22px;font-weight:900;font-family:'Space Grotesk','Manrope',sans-serif;letter-spacing:-.5px;color:var(--t1)}
  .ev-pipe-lbl{font-size:8px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--t3);margin-top:3px}
  .ev-pipe-stage.pass .ev-pipe-num{color:#14c784}
  .ev-pipe-stage.fail .ev-pipe-num{color:#ef4444}
  .ev-pipe-stage.rate .ev-pipe-num{color:#f59e0b}
  .ev-pipe-arrow{font-size:14px;color:rgba(255,255,255,.12);flex-shrink:0;padding:0 2px}

  /* ── Top rejection bar ───────────────────────────────── */
  .ev-reject-bar{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px}
  .ev-reject-chip{display:flex;align-items:center;gap:5px;padding:5px 10px;border-radius:8px;font-size:10px;font-weight:700;background:rgba(239,68,68,.06);border:1px solid rgba(239,68,68,.12);color:var(--t2)}
  .ev-reject-chip .cnt{color:#ef4444;font-family:'Space Grotesk',sans-serif;font-weight:900}

  /* ── Main layout ─────────────────────────────────────── */
  .ev-layout{display:grid;grid-template-columns:1fr 340px;gap:14px;align-items:start}
  @media(max-width:960px){.ev-layout{grid-template-columns:1fr}}

  /* ── Evaluation log ──────────────────────────────────── */
  .ev-log-wrap{padding:0;overflow:hidden}
  .ev-log-head{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid rgba(255,255,255,.06);gap:10px;flex-wrap:wrap}
  .ev-log-title{display:flex;align-items:center;gap:7px;font-size:15px;font-weight:800;font-family:'Space Grotesk','Manrope',sans-serif}
  .ev-log-sub{font-size:11px;color:var(--t3);margin-top:2px}
  .ev-pulse{width:7px;height:7px;border-radius:50%;background:#14c784;animation:livePulse 1.5s ease-in-out infinite;flex-shrink:0}
  .ev-filters{display:flex;gap:5px}
  .ev-fbtn{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:20px;color:var(--t2);font-size:10px;font-weight:700;padding:4px 11px;cursor:pointer;transition:.15s}
  .ev-fbtn.active{background:rgba(20,199,132,.15);border-color:rgba(20,199,132,.4);color:#14c784}
  .ev-fbtn:hover{border-color:rgba(255,255,255,.2);color:var(--t1)}
  .ev-log-list{max-height:600px;overflow-y:auto;padding:4px 0}

  /* ── Individual evaluation row ───────────────────────── */
  .ev-row{display:grid;grid-template-columns:28px 1fr 90px 180px 70px;gap:8px;align-items:center;padding:8px 14px;border-bottom:1px solid rgba(255,255,255,.03);transition:background .12s;cursor:pointer}
  .ev-row:hover{background:rgba(255,255,255,.025)}
  @media(max-width:1100px){.ev-row{grid-template-columns:28px 1fr 90px 70px}}
  @media(max-width:700px){.ev-row{grid-template-columns:28px 1fr 70px}}
  .ev-verdict{width:24px;height:24px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:900;flex-shrink:0}
  .ev-verdict.pass{background:rgba(20,199,132,.12);border:1px solid rgba(20,199,132,.3);color:#14c784}
  .ev-verdict.fail{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.25);color:#ef4444}
  .ev-info{min-width:0}
  .ev-name{font-size:12px;font-weight:800;color:var(--t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .ev-name .ev-src{font-size:9px;font-weight:600;color:var(--t3);margin-left:6px;letter-spacing:.04em}
  .ev-reason{font-size:10px;color:var(--t3);margin-top:1px;line-height:1.4;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .ev-reason.pass-reason{color:rgba(20,199,132,.7)}
  .ev-score{display:flex;align-items:center;gap:5px}
  .ev-score-bar{height:5px;width:50px;border-radius:99px;background:rgba(255,255,255,.06);overflow:hidden}
  .ev-score-fill{height:100%;border-radius:99px}
  .ev-score-num{font-size:10px;font-weight:800;font-family:monospace}
  .ev-checks{display:flex;gap:3px;flex-wrap:nowrap}
  .ev-ck{width:22px;height:22px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:900;border:1px solid transparent}
  .ev-ck.p{background:rgba(20,199,132,.1);border-color:rgba(20,199,132,.25);color:#14c784}
  .ev-ck.f{background:rgba(239,68,68,.08);border-color:rgba(239,68,68,.2);color:#ef4444}
  .ev-time{font-size:10px;color:var(--t3);font-family:monospace;text-align:right}
  .ev-tags{display:flex;gap:3px;flex-wrap:wrap;margin-top:3px}
  .ev-tag{font-size:8px;font-weight:700;padding:1px 5px;border-radius:4px;background:rgba(168,85,247,.1);border:1px solid rgba(168,85,247,.2);color:#c084fc;text-transform:uppercase;letter-spacing:.05em}
  .ev-empty{padding:40px 16px;text-align:center;color:var(--t3);font-size:13px;line-height:1.7}

  /* ── Expanded detail panel (click a row) ─────────────── */
  .ev-detail{display:none;background:rgba(8,16,32,.96);border:1px solid rgba(59,130,246,.2);border-radius:14px;padding:16px;margin:0 14px 10px;animation:fadeSlideIn .25s ease}
  .ev-detail.open{display:block}
  .ev-detail-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
  .ev-detail-title{font-size:14px;font-weight:800;color:var(--t1);font-family:'Space Grotesk',sans-serif}
  .ev-detail-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:8px;margin-bottom:12px}
  .ev-detail-cell{padding:8px;border-radius:8px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.05)}
  .ev-detail-lbl{font-size:8px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--t3)}
  .ev-detail-val{font-size:14px;font-weight:800;color:var(--t1);margin-top:2px;font-family:'Space Grotesk',sans-serif}
  .ev-checklist-row{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.04);font-size:12px}
  .ev-checklist-row:last-child{border-bottom:none}
  .ev-checklist-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
  .ev-checklist-dot.p{background:#14c784}.ev-checklist-dot.f{background:#ef4444}
  .ev-checklist-name{color:var(--t2);font-weight:600}
  .ev-checklist-status{margin-left:auto;font-weight:800;font-size:11px}

  /* ── Approved coins sidebar ──────────────────────────── */
  .ev-sidebar{display:flex;flex-direction:column;gap:12px;position:sticky;top:148px}
  .ev-approved-title{font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.12em;color:#14c784;padding:14px 16px 0}
  .ev-approved-sub{font-size:10px;color:var(--t3);padding:0 16px 8px;border-bottom:1px solid rgba(255,255,255,.04)}
  .ev-approved-list{max-height:440px;overflow-y:auto;padding:6px 0}
  .ev-coin-card{padding:10px 14px;border-bottom:1px solid rgba(255,255,255,.03);transition:background .12s;cursor:pointer}
  .ev-coin-card:hover{background:rgba(20,199,132,.04)}
  .ev-coin-top{display:flex;align-items:center;justify-content:space-between;gap:8px}
  .ev-coin-icon{width:28px;height:28px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:900;flex-shrink:0}
  .ev-coin-name{font-size:12px;font-weight:800;color:var(--t1)}
  .ev-coin-meta{font-size:10px;color:var(--t3);margin-top:1px}
  .ev-coin-score{font-size:16px;font-weight:900;font-family:'Space Grotesk',sans-serif}
  .ev-coin-metrics{display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px;margin-top:6px}
  .ev-coin-m{font-size:9px;color:var(--t3)}.ev-coin-mv{font-weight:800;color:var(--t2)}
  .ev-coin-checks{display:flex;gap:3px;margin-top:5px}

  /* ── Session controls sidebar card ───────────────────── */
  .ev-ctrl-title{font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.1em;color:var(--t3);padding:12px 14px 6px}
  .ev-ctrl-row{display:flex;justify-content:space-between;padding:5px 14px;font-size:11px}
  .ev-ctrl-lbl{color:var(--t3)}.ev-ctrl-val{font-weight:700;color:var(--t1)}
  .ev-input-row{padding:4px 14px}
  .ev-input-lbl{display:block;font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:var(--t3);margin-bottom:3px}

  /* ── Scan balance strip ──────────────────────────────── */
  .scan-bal-strip{display:flex;gap:0;align-items:stretch;background:rgba(8,16,32,.85);border:1px solid rgba(255,255,255,.06);border-radius:12px;margin-bottom:14px;overflow:hidden;backdrop-filter:blur(8px)}
  .scan-bal-item{flex:1;padding:10px 14px;border-right:1px solid rgba(255,255,255,.05);display:flex;flex-direction:column;gap:2px;min-width:90px}
  .scan-bal-item:last-child{border-right:none}
  .scan-bal-lbl{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--t3)}
  .scan-bal-val{font-size:17px;font-weight:900;font-family:'Space Grotesk','Manrope',sans-serif;letter-spacing:-.3px;color:var(--t1)}

  /* ── Position cards ──────────────────────────────────── */
  .pos-cards{display:flex;flex-direction:column;gap:8px;padding:8px 14px 12px}
  .pos-card{background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.05);border-radius:10px;padding:10px 12px;cursor:pointer;transition:border-color .15s}
  .pos-card.winning{border-color:rgba(20,199,132,.2)}
  .pos-card.losing {border-color:rgba(220,38,38,.2)}
  .pos-card:hover{border-color:rgba(96,165,250,.25)}
  .pc-top{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}
  .pc-name{font-size:12px;font-weight:800;color:var(--t1)}
  .pc-reason{font-size:9px;color:var(--t3);margin-top:1px;line-height:1.3}
  .pc-pnl{font-size:16px;font-weight:900;font-family:'Space Grotesk',sans-serif;text-align:right;line-height:1.1}
  .pc-pnl-sub{font-size:10px;font-weight:600;text-align:right;color:var(--t3);margin-top:1px}
  .pc-meta{display:flex;gap:8px;margin-top:6px;flex-wrap:wrap}
  .pc-meta span{font-size:10px;color:var(--t3)}
  .pc-meta .pc-badge{font-size:9px;padding:1px 5px;border-radius:4px;background:rgba(20,199,132,.1);border:1px solid rgba(20,199,132,.2);color:var(--grn)}
  .pc-bar-wrap{height:3px;background:rgba(255,255,255,.05);border-radius:99px;margin-top:7px;overflow:hidden}
  .pc-bar{height:3px;border-radius:99px;background:var(--grn);transition:width .6s}
  .pc-bar.red{background:var(--red2)}

  /* ── Winners / Losers history ────────────────────────── */
  .scan-hist-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px}
  @media(max-width:700px){.scan-hist-row{grid-template-columns:1fr}}
  .scan-hist-card{background:rgba(8,16,32,.6);border:1px solid rgba(255,255,255,.06);border-radius:12px;overflow:hidden}
  .scan-hist-head{padding:10px 14px 6px;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.1em;border-bottom:1px solid rgba(255,255,255,.04)}
  .scan-hist-head.win{color:var(--grn)}.scan-hist-head.loss{color:var(--red2)}
  .scan-hist-list{max-height:260px;overflow-y:auto}
  .scan-hist-item{display:flex;align-items:center;gap:8px;padding:6px 14px;border-bottom:1px solid rgba(255,255,255,.03);font-size:11px}
  .scan-hist-item .shi-name{flex:1;font-weight:700;color:var(--t1);min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .scan-hist-item .shi-reason{font-size:10px;color:var(--t3);max-width:90px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .scan-hist-item .shi-pnl{font-weight:800;font-family:monospace;white-space:nowrap}
  .scan-hist-item .shi-ts{font-size:9px;color:var(--t3);white-space:nowrap}

  /* ── Live optimizer ──────────────────────────────────── */
  .scan-opt-wrap{margin-top:14px;background:rgba(8,16,32,.6);border:1px solid rgba(255,255,255,.06);border-radius:12px;overflow:hidden}
  .scan-opt-head{padding:10px 14px 6px;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.1em;color:var(--blue2);border-bottom:1px solid rgba(255,255,255,.04)}
  .scan-opt-body{padding:8px 14px 10px}
  .scan-opt-row{display:flex;align-items:center;gap:10px;padding:5px 0;font-size:11px;border-bottom:1px solid rgba(255,255,255,.03)}
  .scan-opt-row:last-child{border-bottom:none}
  .scan-opt-lbl{width:130px;color:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:10px}
  .scan-opt-bar-wrap{flex:1;height:5px;background:rgba(255,255,255,.05);border-radius:99px;overflow:hidden}
  .scan-opt-bar{height:5px;border-radius:99px;background:rgba(168,85,247,.6);transition:width .5s}
  .scan-opt-pct{width:38px;text-align:right;font-weight:800;color:#c084fc;font-size:11px}
  .scan-opt-cnt{width:38px;text-align:right;color:var(--t3);font-size:10px}

  /* ── Budget % slider ─────────────────────────────────── */
  input[type=range].budget-slider{width:100%;accent-color:var(--blue2);cursor:pointer}
  </style>

  <div id="tab-scanner" class="tab-pane active">

    <!-- Balance Strip -->
    <div class="scan-bal-strip" id="scan-bal-strip">
      <div class="scan-bal-item"><div class="scan-bal-lbl">Balance</div><div class="scan-bal-val" id="sbs-balance">—</div></div>
      <div class="scan-bal-item"><div class="scan-bal-lbl">Open</div><div class="scan-bal-val" id="sbs-open">0</div></div>
      <div class="scan-bal-item"><div class="scan-bal-lbl">Session P&amp;L</div><div class="scan-bal-val" id="sbs-pnl">—</div></div>
      <div class="scan-bal-item"><div class="scan-bal-lbl">Wins</div><div class="scan-bal-val" id="sbs-wins" style="color:var(--grn)">0</div></div>
      <div class="scan-bal-item"><div class="scan-bal-lbl">Losses</div><div class="scan-bal-val" id="sbs-losses" style="color:var(--red2)">0</div></div>
      <div class="scan-bal-item"><div class="scan-bal-lbl">Win Rate</div><div class="scan-bal-val" id="sbs-wr">—</div></div>
    </div>

    <!-- Pipeline Funnel -->
    <div class="ev-pipeline" id="ev-pipeline">
      <div class="ev-pipe-stage"><div class="ev-pipe-num" id="evp-scanned">0</div><div class="ev-pipe-lbl">Scanned</div></div>
      <div class="ev-pipe-arrow">&rsaquo;</div>
      <div class="ev-pipe-stage"><div class="ev-pipe-num" id="evp-evaluated">0</div><div class="ev-pipe-lbl">Evaluated</div></div>
      <div class="ev-pipe-arrow">&rsaquo;</div>
      <div class="ev-pipe-stage pass"><div class="ev-pipe-num" id="evp-passed">0</div><div class="ev-pipe-lbl">Approved</div></div>
      <div class="ev-pipe-arrow">&rsaquo;</div>
      <div class="ev-pipe-stage fail"><div class="ev-pipe-num" id="evp-rejected">0</div><div class="ev-pipe-lbl">Rejected</div></div>
      <div class="ev-pipe-arrow">&rsaquo;</div>
      <div class="ev-pipe-stage rate"><div class="ev-pipe-num" id="evp-rate">0%</div><div class="ev-pipe-lbl">Pass Rate</div></div>
    </div>

    <!-- Top Rejection Reasons -->
    <div class="ev-reject-bar" id="ev-reject-bar"></div>

    <!-- Main Layout -->
    <div class="ev-layout">
      <!-- Evaluation Log -->
      <div class="glass ev-log-wrap">
        <div class="ev-log-head">
          <div>
            <div class="ev-log-title"><span class="ev-pulse"></span> Evaluation Ramp</div>
            <div class="ev-log-sub" id="ev-log-sub">Every coin the bot sees flows through here</div>
          </div>
          <div class="ev-filters">
            <button class="ev-fbtn active" data-evf="all" onclick="evSetFilter(this)">All</button>
            <button class="ev-fbtn" data-evf="passed" onclick="evSetFilter(this)">Approved</button>
            <button class="ev-fbtn" data-evf="rejected" onclick="evSetFilter(this)">Rejected</button>
          </div>
        </div>
        <div id="ev-log-list" class="ev-log-list">
          <div class="ev-empty">Start the bot to see coin evaluations flow through the pipeline</div>
        </div>
      </div>

      <!-- Sidebar -->
      <div class="ev-sidebar">
        <!-- Approved Coins -->
        <div class="glass" style="padding:0;overflow:hidden">
          <div class="ev-approved-title">Approved Coins</div>
          <div class="ev-approved-sub">Tokens that passed all filters</div>
          <!-- Force Buy input -->
          <div style="padding:8px 12px;border-bottom:1px solid rgba(255,255,255,.04)">
            <div style="display:flex;gap:6px">
              <input id="force-buy-mint" type="text" placeholder="Paste mint address…" style="flex:1;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.08);border-radius:8px;padding:7px 10px;font-size:11px;color:var(--t1);outline:none">
              <button class="btn" style="font-size:10px;padding:7px 14px;white-space:nowrap;background:linear-gradient(135deg,#14c784,#0ea5e9);border:none;border-radius:8px;color:#fff;font-weight:700;cursor:pointer" onclick="submitForceBuy()">⚡ Force Buy</button>
            </div>
          </div>
          <div id="ev-approved-list" class="ev-approved-list">
            <div style="padding:20px;text-align:center;font-size:11px;color:var(--t3)">No approved coins yet</div>
          </div>
        </div>

        <!-- Session Controls -->
        <div class="glass" style="padding:0;overflow:hidden">
          <div class="ev-ctrl-title">Session</div>
          <div class="ev-ctrl-row"><span class="ev-ctrl-lbl">Balance</span><span class="ev-ctrl-val" id="sc-wallet-bal">—</span></div>
          <div class="ev-ctrl-row"><span class="ev-ctrl-lbl">Open Trades</span><span class="ev-ctrl-val" id="sc-open-trades">0</span></div>
          <div class="ev-ctrl-row"><span class="ev-ctrl-lbl">Session P&L</span><span class="ev-ctrl-val" id="sc-session-pnl" style="color:#14c784">+0.00</span></div>
          <div class="ev-ctrl-row"><span class="ev-ctrl-lbl">Win Rate</span><span class="ev-ctrl-val" id="sc-session-wr">—</span></div>
          <div class="ev-ctrl-row"><span class="ev-ctrl-lbl">Preset</span><span class="ev-ctrl-val" id="sc-preset-name">—</span></div>
          <div style="padding:6px 14px 10px">
            <button class="btn btn-ghost" style="width:100%;font-size:10px" onclick="activateTab('settings')">Settings &rarr;</button>
          </div>
        </div>

        <!-- Risk Controls (from preset) -->
        <div class="glass" style="padding:0;overflow:hidden">
          <div class="ev-ctrl-title">Risk Controls</div>
          <div class="ev-ctrl-row"><span class="ev-ctrl-lbl">Wallet</span><span class="ev-ctrl-val" id="sc-wallet-bal2">— SOL</span></div>
          <div class="ev-ctrl-row"><span class="ev-ctrl-lbl">Drawdown Limit</span><span class="ev-ctrl-val" id="sc-dd-limit">—</span></div>
          <div class="ev-ctrl-row"><span class="ev-ctrl-lbl">Max Positions</span><span class="ev-ctrl-val" id="sc-max-pos">—</span></div>
          <div class="ev-ctrl-row"><span class="ev-ctrl-lbl">Stop Loss</span><span class="ev-ctrl-val" id="sc-stop-loss">—</span></div>
          <div class="ev-ctrl-row"><span class="ev-ctrl-lbl">Trail %</span><span class="ev-ctrl-val" id="sc-trail-pct">—</span></div>
        </div>

        <!-- Open Positions — rich cards -->
        <div class="glass" style="padding:0;overflow:hidden">
          <div class="ev-ctrl-title">Open Positions</div>
          <div id="pos-tbl" style="display:none"></div><!-- compat stub -->
          <div id="pos-cards" class="pos-cards">
            <div style="font-size:11px;color:var(--t3);padding:4px 0">No open positions</div>
          </div>
        </div>

        <!-- hidden stubs for backward-compat JS references -->
        <div style="display:none">
          <div id="filter-pipe"></div>
          <div id="listing-feed"></div>
          <span id="listing-stat">0</span>
          <span id="listing-count-badge"></span>
          <span id="scanner-filter-live">0</span>
          <span id="scanner-position-live">0</span>
          <span id="scanner-token-live">0</span>
          <span id="scanner-listing-live">0</span>
          <div id="launch-summary"></div>
          <span id="enhanced-status-copy"></span>
          <span id="scanner-last-market"></span>
          <span id="scanner-last-listing"></span>
          <span id="scanner-sync-copy"></span>
          <span id="scanner-active-tab"></span>
          <span id="sc-shadow-preset"></span>
          <span id="sc-shadow-wr"></span>
          <span id="sc-shadow-pnl2"></span>
          <span id="sc-shadow-trades2"></span>
          <span id="sc-budget-display"></span>
          <div id="sc-settings-grid"></div>
          <div id="sc-activity-feed"></div>
          <div id="sc-feed-sub"></div>
          <span id="token-count">0</span>
          <div id="token-rows"></div>
          <span id="ticker-inner"></span>
          <input id="scan-search" type="hidden">
        </div>
      </div>
    </div>

    <!-- Winners / Losers History -->
    <div class="scan-hist-row">
      <div class="scan-hist-card">
        <div class="scan-hist-head win">Winners</div>
        <div id="scan-winners" class="scan-hist-list">
          <div style="padding:14px;font-size:11px;color:var(--t3)">No winners yet this session</div>
        </div>
      </div>
      <div class="scan-hist-card">
        <div class="scan-hist-head loss">Losers</div>
        <div id="scan-losers" class="scan-hist-list">
          <div style="padding:14px;font-size:11px;color:var(--t3)">No losses yet this session</div>
        </div>
      </div>
    </div>

    <!-- Live Close-Reason Optimizer -->
    <div class="scan-opt-wrap">
      <div class="scan-opt-head">Close Reason Win Rate</div>
      <div id="scan-optimizer" class="scan-opt-body">
        <div style="font-size:11px;color:var(--t3)">Accumulating trade data…</div>
      </div>
    </div>

  </div>

  <!-- ═══════════════════════ SETTINGS TAB ═══════════════════════ -->
  <div id="tab-settings" class="tab-pane">
    <!-- Hover Tooltip System -->
    <style>
    .htip{position:fixed;z-index:9999;max-width:280px;padding:12px 16px;border-radius:12px;background:rgba(8,16,32,.97);border:1px solid rgba(20,199,132,.25);box-shadow:0 8px 32px rgba(0,0,0,.5);font-size:12px;color:var(--t2);line-height:1.6;pointer-events:none;opacity:0;transition:opacity .2s;backdrop-filter:blur(12px)}
    .htip.visible{opacity:1}
    .htip-title{font-size:11px;font-weight:800;color:#14c784;letter-spacing:.06em;text-transform:uppercase;margin-bottom:6px}
    .htip-body{color:rgba(255,255,255,.75)}
    [data-tip]{cursor:help}
    .settings-simple{max-width:700px}
    .settings-simple .settings-card{margin-bottom:12px}
    .s-autotune-status{padding:14px 18px;border-radius:14px;background:rgba(20,199,132,.06);border:1px solid rgba(20,199,132,.15);margin-bottom:16px}
    .s-autotune-status .live-dot{width:8px;height:8px}
    .s-section-toggle{display:flex;align-items:center;justify-content:space-between;padding:10px 0;cursor:pointer;user-select:none}
    .s-section-toggle:hover .setting-label{color:var(--t1)}
    .s-section-body{display:none;padding-bottom:8px}
    .s-section-body.open{display:block}
    .s-section-chev{font-size:12px;color:var(--t3);transition:transform .2s}
    .s-section-chev.open{transform:rotate(180deg)}
    </style>

    <div class="tab-pane-header">
      <div>
        <div class="tab-kicker">Configuration</div>
        <div class="tab-pane-title">Bot Settings</div>
        <div class="tab-pane-copy">Pick a strategy, adjust the key numbers, and save. The AI auto-tunes these from shadow trading results every hour.</div>
      </div>
      <div class="shortcut-row">
        <span class="badge bg-muted" id="settings-header-preset">Preset loading…</span>
        <span class="badge bg-blue" id="settings-save-state-copy">Waiting…</span>
      </div>
    </div>

    <div class="settings-pane-grid">
      <div class="settings-stack settings-simple">

        <!-- Auto-Tune Status -->
        <div class="s-autotune-status" data-tip="The bot watches shadow trades (paper trades using real prices) and every hour picks the strategy that performed best over the last 48 hours. It then automatically updates your settings so you're always running the winning strategy.">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
            <div class="live-dot"></div>
            <div style="font-size:13px;font-weight:800;color:#14c784">AI Auto-Tune Active</div>
          </div>
          <div style="font-size:11px;color:var(--t2);line-height:1.6">
            Shadow trading runs all 4 strategies on real market data. Every hour, the AI picks the best-performing one and applies it to your bot automatically. You can still override below.
          </div>
          <div class="shortcut-row" style="margin-top:10px">
            <span class="badge bg-muted" id="s-tune-preset">—</span>
            <span class="badge bg-muted" id="s-tune-last">Last tune: —</span>
            <span class="badge bg-muted">Interval: 1 hour</span>
          </div>
        </div>

        <!-- Strategy Preset -->
        <div class="settings-card" data-tip="This is your base strategy template. Safe uses small buys and tight stops, Balanced is the default middle ground, Aggressive uses bigger positions, Degen goes all-in on high-risk plays. The AI auto-tune will switch between these based on what's actually working.">
          <div class="settings-save-row">
            <div>
              <div class="settings-section-title" style="margin-bottom:6px">Strategy</div>
              <div style="font-size:12px;color:var(--t2)">Pick a base profile. AI auto-tune may change this based on shadow results.</div>
            </div>
            <div id="settings-save-state" class="save-pill">Loaded</div>
          </div>
          <div style="height:12px"></div>
          <div class="setting-row">
            <div><div class="setting-label">Strategy Mode</div></div>
            <select class="setting-input" id="s-preset" onchange="selectPreset(this.value)">
              <option value="safe">Safe — Small buys, tight stops</option>
              <option value="balanced">Balanced — Default for most markets</option>
              <option value="aggressive">Aggressive — Bigger positions, wider stops</option>
              <option value="degen">Degen — Max risk, max reward</option>
              <option value="custom">Custom — Manual control</option>
            </select>
            <div class="setting-unit">preset</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Live or Paper?</div></div>
            <select class="setting-input" id="s-execution-mode">
              <option value="live">Live — Real money</option>
              <option value="paper">Paper — Learn only, no buys</option>
            </select>
            <div class="setting-unit">mode</div>
          </div>
        </div>

        <!-- Core Settings (always visible) -->
        <div class="settings-card" data-tip="These are the main numbers that control how much you buy, when you take profit, and when you cut losses. The bot uses these exact values for every trade.">
          <div class="settings-section-title">Position Management</div>
          <div class="setting-row">
            <div><div class="setting-label">Buy Size</div><div class="setting-desc">How much SOL to spend per trade</div></div>
            <input class="setting-input" id="s-max-buy" type="number" step="0.01">
            <div class="setting-unit">SOL</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Take Profit 1</div><div class="setting-desc">Sell half when price hits this multiple (e.g. 2x = double)</div></div>
            <input class="setting-input" id="s-tp1" type="number" step="0.01">
            <div class="setting-unit">x</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Take Profit 2</div><div class="setting-desc">Sell the rest at this multiple (set 9999 for peak plateau mode)</div></div>
            <input class="setting-input" id="s-tp2" type="number" step="0.01">
            <div class="setting-unit">x</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Stop Loss</div><div class="setting-desc">Exit if price drops to this ratio (0.70 = -30% max loss)</div></div>
            <input class="setting-input" id="s-sl" type="number" step="0.01">
            <div class="setting-unit">ratio</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Trailing Stop</div><div class="setting-desc">How far price can drop from peak before selling (0.20 = 20%)</div></div>
            <input class="setting-input" id="s-trail" type="number" step="0.01">
            <div class="setting-unit">ratio</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Time Limit</div><div class="setting-desc">Auto-exit if trade goes nowhere after this many minutes</div></div>
            <input class="setting-input" id="s-tstop" type="number" step="1">
            <div class="setting-unit">min</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Max Open Trades</div><div class="setting-desc">Won't open new positions beyond this many at once</div></div>
            <input class="setting-input" id="s-maxpos" type="number" step="1">
            <div class="setting-unit">max</div>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">Session Loss Limit</div><div class="setting-desc">Bot pauses if total losses exceed this amount</div></div>
            <input class="setting-input" id="s-dd" type="number" step="0.01">
            <div class="setting-unit">SOL</div>
          </div>
        </div>

        <!-- Entry Filters (collapsible) -->
        <div class="settings-card" data-tip="These filters decide which coins the bot is allowed to buy. Tokens must pass ALL of these checks before a buy happens. Tighter filters = fewer trades but higher quality. Looser filters = more trades but more risk.">
          <div class="s-section-toggle" onclick="toggleSettingsSection('s-filters')">
            <div>
              <div class="settings-section-title" style="margin-bottom:0">Entry Filters</div>
              <div style="font-size:11px;color:var(--t3);margin-top:3px">Which coins are allowed through — click to expand</div>
            </div>
            <span class="s-section-chev" id="s-filters-chev">&#9660;</span>
          </div>
          <div class="s-section-body" id="s-filters-body">
            <div class="setting-row">
              <div><div class="setting-label">Min Liquidity</div><div class="setting-desc">Pool must have at least this much USD to trade</div></div>
              <input class="setting-input" id="s-liq" type="number" step="100">
              <div class="setting-unit">USD</div>
            </div>
            <div class="setting-row">
              <div><div class="setting-label">Market Cap Range</div><div class="setting-desc">Only buy tokens within this market cap range</div></div>
              <div style="display:flex;gap:6px;align-items:center">
                <input class="setting-input" id="s-minmc" type="number" step="100" style="width:100px" placeholder="Min">
                <span style="color:var(--t3)">to</span>
                <input class="setting-input" id="s-maxmc" type="number" step="1000" style="width:100px" placeholder="Max">
              </div>
              <div class="setting-unit">USD</div>
            </div>
            <div class="setting-row">
              <div><div class="setting-label">Max Token Age</div><div class="setting-desc">Skip older coins — focus on fresh launches</div></div>
              <input class="setting-input" id="s-age" type="number" step="1">
              <div class="setting-unit">min</div>
            </div>
            <div class="setting-row">
              <div><div class="setting-label">Min Volume</div><div class="setting-desc">Skip tokens with less than this in 24h trading volume</div></div>
              <input class="setting-input" id="s-minvol" type="number" step="100">
              <div class="setting-unit">USD</div>
            </div>
            <div class="setting-row">
              <div><div class="setting-label">Min AI Score</div><div class="setting-desc">The bot's AI scores each token 0-100 — reject below this</div></div>
              <input class="setting-input" id="s-minscore" type="number" step="1">
              <div class="setting-unit">/100</div>
            </div>
            <div class="setting-row">
              <div><div class="setting-label">Max Price Pump</div><div class="setting-desc">Skip if token already pumped more than this in 1 hour</div></div>
              <input class="setting-input" id="s-hotchg" type="number" step="1">
              <div class="setting-unit">%</div>
            </div>
          </div>
        </div>

        <!-- Advanced (collapsible) -->
        <div class="settings-card" data-tip="Expert-level controls for fine-tuning the signal pipeline. Most users should leave these at default — the AI auto-tune handles optimization automatically.">
          <div class="s-section-toggle" onclick="toggleSettingsSection('s-advanced')">
            <div>
              <div class="settings-section-title" style="margin-bottom:0">Advanced</div>
              <div style="font-size:11px;color:var(--t3);margin-top:3px">Expert settings — usually auto-tuned</div>
            </div>
            <span class="s-section-chev" id="s-advanced-chev">&#9660;</span>
          </div>
          <div class="s-section-body" id="s-advanced-body">
            <div class="setting-row">
              <div><div class="setting-label">Decision Policy</div><div class="setting-desc">How the bot decides to buy: rules, AI model, or auto-switching</div></div>
              <select class="setting-input" id="s-policy-mode">
                <option value="rules">Rules — Filter checklist</option>
                <option value="auto">Auto — AI picks best</option>
                <option value="model_global">Global Model</option>
                <option value="model_regime_auto">Regime Model</option>
              </select>
              <div class="setting-unit">policy</div>
            </div>
            <div class="setting-row">
              <div><div class="setting-label">Model Threshold</div><div class="setting-desc">AI model score required to approve a buy</div></div>
              <input class="setting-input" id="s-model-threshold" type="number" min="40" max="90" step="0.5">
              <div class="setting-unit">score</div>
            </div>
            <div class="setting-row">
              <div><div class="setting-label">Priority Fee</div><div class="setting-desc">Extra fee to land transactions faster on Solana</div></div>
              <input class="setting-input" id="s-prio" type="number" step="1000">
              <div class="setting-unit">lamports</div>
            </div>
            <div class="setting-row">
              <div><div class="setting-label">Green Lights</div><div class="setting-desc">How many quality checks must pass before buying</div></div>
              <input class="setting-input" id="s-lights" type="number" step="1">
              <div class="setting-unit">count</div>
            </div>
            <div class="setting-row">
              <div><div class="setting-label">Holder Growth</div><div class="setting-desc">Required % growth in token holders</div></div>
              <input class="setting-input" id="s-holders" type="number" step="1">
              <div class="setting-unit">%</div>
            </div>
            <div class="setting-row">
              <div><div class="setting-label">Narrative Score</div><div class="setting-desc">How well the token's story/theme is trending</div></div>
              <input class="setting-input" id="s-narr" type="number" step="1">
              <div class="setting-unit">pts</div>
            </div>
            <div class="setting-row">
              <div><div class="setting-label">Volume Spike</div><div class="setting-desc">Volume must be this multiple above average</div></div>
              <input class="setting-input" id="s-volspike" type="number" step="0.1">
              <div class="setting-unit">x</div>
            </div>
            <div class="setting-row">
              <div><div class="setting-label">Late Entry Limit</div><div class="setting-desc">Reject coins already pumped this much from launch</div></div>
              <input class="setting-input" id="s-latemult" type="number" step="0.1">
              <div class="setting-unit">x</div>
            </div>
            <div class="setting-row">
              <div><div class="setting-label">Nuclear Narrative</div><div class="setting-desc">Override late-entry rejection if narrative is this strong</div></div>
              <input class="setting-input" id="s-nuclear" type="number" step="1">
              <div class="setting-unit">pts</div>
            </div>
            <div class="setting-row">
              <div><div class="setting-label">Off-Peak Floor</div><div class="setting-desc">Min price change required during quiet hours</div></div>
              <input class="setting-input" id="s-offpeak" type="number" step="1">
              <div class="setting-unit">%</div>
            </div>
            <div class="setting-toggle-row">
              <div><div class="setting-label">Auto Promote Leader</div><div class="setting-desc">Let the AI auto-switch policies based on edge reports</div></div>
              <label class="toggle-wrap"><input id="s-auto-promote" type="checkbox"> <span style="color:var(--t2);font-size:12px">On</span></label>
            </div>
            <div class="setting-row">
              <div><div class="setting-label">Promote Window</div><div class="setting-desc">Days of data to evaluate before promoting</div></div>
              <input class="setting-input" id="s-auto-window" type="number" min="3" max="30" step="1">
              <div class="setting-unit">days</div>
            </div>
            <div class="setting-row">
              <div><div class="setting-label">Min Reports</div><div class="setting-desc">Reports needed before auto-promote trusts the data</div></div>
              <input class="setting-input" id="s-auto-min-reports" type="number" min="2" max="12" step="1">
              <div class="setting-unit">count</div>
            </div>
            <div class="setting-row">
              <div><div class="setting-label">Promote Lock</div><div class="setting-desc">Cooldown between auto policy switches</div></div>
              <input class="setting-input" id="s-auto-lock" type="number" min="30" max="10080" step="30">
              <div class="setting-unit">min</div>
            </div>
            <div class="setting-row">
              <div><div class="setting-label">Risk Per Trade</div><div class="setting-desc">Max % of wallet to use per position</div></div>
              <input class="setting-input" id="s-risk" type="number" step="0.1">
              <div class="setting-unit">%</div>
            </div>
          </div>
        </div>

        <!-- Safety -->
        <div class="settings-card" data-tip="Protection features that block suspicious tokens. Rug check stops tokens with unsafe mint/freeze authority. Holder check blocks tokens where a few wallets own most of the supply — a common dump setup.">
          <div class="settings-section-title">Safety</div>
          <div class="setting-toggle-row">
            <div><div class="setting-label">Rug Protection</div><div class="setting-desc">Block tokens with unsafe authority settings</div></div>
            <label class="toggle-wrap"><input id="s-antirug" type="checkbox"> <span style="color:var(--t2);font-size:12px">On</span></label>
          </div>
          <div class="setting-toggle-row">
            <div><div class="setting-label">Whale Concentration Check</div><div class="setting-desc">Block tokens where a few wallets own too much</div></div>
            <label class="toggle-wrap"><input id="s-checkholders" type="checkbox"> <span style="color:var(--t2);font-size:12px">On</span></label>
          </div>
        </div>

        <!-- Save -->
        <div class="settings-card">
          <div class="settings-save-row">
            <button class="btn btn-primary" type="button" onclick="saveSettings()">Save Settings</button>
            <button class="btn btn-ghost" type="button" onclick="refresh()">Reset to Saved</button>
          </div>
        </div>

        <div id="execution-control-summary" class="settings-echo" style="margin-top:0">
          <span class="badge bg-muted">Execution lane loading…</span>
        </div>
      </div>

      <div class="settings-stack">
        <div class="settings-card" data-tip="The exact journey every token takes from discovery to buy or reject. Each gate uses YOUR current settings.">
          <div class="settings-section-title">Token Journey</div>
          <div style="font-size:12px;color:var(--t2);margin-bottom:12px">Every coin follows this exact path — fail any gate and it's rejected.</div>
          <div id="settings-token-journey" class="checkpoint-path"></div>
        </div>
        <div class="settings-card">
          <div class="s-section-toggle" onclick="toggleSettingsSection('s-snapshot')">
            <div>
              <div class="settings-section-title" style="margin-bottom:0">Raw Settings</div>
              <div style="font-size:11px;color:var(--t3);margin-top:3px">Technical view of saved values</div>
            </div>
            <span class="s-section-chev" id="s-snapshot-chev">&#9660;</span>
          </div>
          <div class="s-section-body" id="s-snapshot-body">
            <div id="settings-snapshot" class="settings-echo">
              <span class="badge bg-muted">Waiting for saved settings…</span>
            </div>
            <div id="settings-snapshot-note" style="font-size:11px;color:var(--t3);margin-top:10px;line-height:1.5">Exact persisted values the bot uses.</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- ═══════════════════════ SIGNALS TAB ═══════════════════════ -->
  <div id="tab-signals" class="tab-pane">
    <div class="tab-pane-header">
      <div>
        <div class="tab-kicker">Decision Trace</div>
        <div class="tab-pane-title">Signal Explorer</div>
        <div class="tab-pane-copy">Every evaluated token stays here with the exact reason it passed or failed, the checklist scores behind it, and the downstream operational context around that decision.</div>
      </div>
      <div class="shortcut-row">
        <span class="badge bg-muted">Row click opens full checklist</span>
        <span class="badge bg-muted">Search matches token, mint, tags, and reason</span>
      </div>
    </div>
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
    <div class="signals-layout">
      <div style="min-width:0">
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
      <div class="signals-side">
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
    <div class="tab-pane-header">
      <div>
        <div class="tab-kicker">Smart Money</div>
        <div class="tab-pane-title">Whale Activity</div>
        <div class="tab-pane-copy">Tracked whale wallets, recent buys, and post-entry performance stay grouped here so you can see who is active and whether their flow is still worth following.</div>
      </div>
      <div class="shortcut-row">
        <span class="badge bg-muted">Live feed auto-refreshes</span>
      </div>
    </div>
    <div class="whale-card-row" id="whale-cards"></div>
    <div class="glass" style="padding:0">
      <div style="padding:12px 16px;border-bottom:1px solid var(--b1);font-weight:700;font-size:13px">Live Whale Feed</div>
      <div id="whale-feed" style="max-height:500px;overflow-y:auto">
        <div style="padding:20px;text-align:center;color:var(--t3);font-size:12px">Waiting for whale activity…</div>
      </div>
    </div>
  </div>

  <!-- ═══════════════════════ POSITIONS TAB ═══════════════════════ -->
  <div id="tab-positions" class="tab-pane">
    <div class="tab-pane-header">
      <div>
        <div class="tab-kicker">Execution Risk</div>
        <div class="tab-pane-title">Position Monitor</div>
        <div class="tab-pane-copy">Open positions, mini charts, manual exits, and drawdown posture live together here so you can manage active exposure without losing market context.</div>
      </div>
      <div class="shortcut-row">
        <span class="badge bg-muted">Manual partial exits available per card</span>
      </div>
    </div>
    <div class="positions-layout">
      <div id="pos-cards">
        <div style="text-align:center;color:var(--t3);font-size:13px;padding:40px">No open positions</div>
      </div>
      <div>
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
    <div class="tab-pane-header">
      <div>
        <div class="tab-kicker">Performance</div>
        <div class="tab-pane-title">P&L Curves</div>
        <div class="tab-pane-copy">Track realized performance, compare time windows, and inspect drawdown behavior with a cleaner equity view.</div>
      </div>
      <div class="shortcut-row">
        <span class="badge bg-muted">24h, 7d, and all-time views</span>
      </div>
    </div>
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
    <div class="pnl-layout">
      <div class="glass" style="margin-bottom:14px">
        <div style="font-weight:700;font-size:15px;margin-bottom:8px;font-family:'Space Grotesk','Manrope',sans-serif">Cumulative P&L</div>
        <canvas id="pnl-chart" height="220"></canvas>
      </div>
      <div class="glass">
        <div style="font-weight:700;font-size:15px;margin-bottom:8px;font-family:'Space Grotesk','Manrope',sans-serif">Drawdown</div>
        <canvas id="dd-chart" height="120"></canvas>
      </div>
    </div>
  </div>

  <!-- ═══════════════════════ QUANT TAB ═══════════════════════ -->
  <style>
  .quant-section{background:var(--card);border:1px solid var(--b1);border-radius:16px;margin-bottom:12px;overflow:hidden}
  .quant-section-header{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;cursor:pointer;user-select:none;transition:background .15s}
  .quant-section-header:hover{background:rgba(255,255,255,.02)}
  .quant-section-header h3{margin:0;font-size:14px;font-weight:800;color:var(--t1);font-family:'Space Grotesk','Manrope',sans-serif}
  .quant-section-header .sub{font-size:10px;color:var(--t3);margin-top:2px}
  .quant-section-chevron{transition:transform .25s;font-size:14px;color:var(--t3)}
  .quant-section-chevron.open{transform:rotate(180deg)}
  .quant-section-body{display:none;padding:0 18px 18px;border-top:1px solid rgba(255,255,255,.04)}
  .quant-section-body.open{display:block}
  .quant-section-nav{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px;padding:14px 18px 0}
  .quant-section-nav-btn{padding:6px 14px;border-radius:8px;border:1px solid rgba(255,255,255,.08);background:rgba(255,255,255,.02);color:var(--t2);font-size:11px;font-weight:700;cursor:pointer;transition:all .2s;letter-spacing:.04em}
  .quant-section-nav-btn:hover{background:rgba(20,199,132,.06);border-color:rgba(20,199,132,.2);color:var(--t1)}
  .quant-section-nav-btn.active{background:rgba(20,199,132,.12);border-color:rgba(20,199,132,.3);color:#14c784}
  .opp-xy-map{position:relative;width:100%;height:320px;border:1px solid rgba(255,255,255,.08);border-radius:12px;background:rgba(0,0,0,.3);overflow:hidden;margin-bottom:14px}
  .opp-xy-axis-x{position:absolute;bottom:6px;left:50%;transform:translateX(-50%);font-size:9px;color:var(--t3);letter-spacing:.08em;text-transform:uppercase}
  .opp-xy-axis-y{position:absolute;left:6px;top:50%;transform:translateY(-50%) rotate(-90deg);font-size:9px;color:var(--t3);letter-spacing:.08em;text-transform:uppercase}
  .opp-xy-quadrant{position:absolute;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;opacity:.35}
  .opp-xy-dot{position:absolute;width:8px;height:8px;border-radius:50%;transform:translate(-50%,-50%);transition:all .3s;cursor:default}
  .opp-xy-dot:hover{transform:translate(-50%,-50%) scale(2);z-index:10}
  .opp-xy-crosshair-h{position:absolute;left:0;right:0;height:1px;background:rgba(255,255,255,.06);top:50%}
  .opp-xy-crosshair-v{position:absolute;top:0;bottom:0;width:1px;background:rgba(255,255,255,.06);left:50%}
  .opp-settings-panel{background:rgba(20,199,132,.04);border:1px solid rgba(20,199,132,.15);border-radius:14px;padding:18px;margin-top:14px}
  .opp-settings-panel h4{margin:0 0 10px;font-size:13px;font-weight:800;color:#14c784}
  </style>
  <div id="tab-quant" class="tab-pane">
    <div class="tab-pane-header">
      <div>
        <div class="tab-kicker">Research Loop</div>
        <div class="tab-pane-title">Replay Lab</div>
        <div class="tab-pane-copy">Run historical replays over captured feature snapshots, compare strategy behavior, and inspect simulated trades without touching live capital.</div>
      </div>
      <div class="shortcut-row">
        <span class="badge bg-muted">Backtests run asynchronously</span>
        <button class="btn btn-ghost" type="button" onclick="pollQuant()" style="margin-left:8px">Refresh All</button>
      </div>
    </div>

    <!-- Section nav pills at top -->
    <div class="quant-section-nav">
      <button class="quant-section-nav-btn active" onclick="toggleQuantSection('qs-dataset',this)">Dataset</button>
      <button class="quant-section-nav-btn active" onclick="toggleQuantSection('qs-backtest',this)">Backtest</button>
      <button class="quant-section-nav-btn active" onclick="toggleQuantSection('qs-regime',this)">Flow Regime</button>
      <button class="quant-section-nav-btn active" onclick="toggleQuantSection('qs-opportunity',this)">Opportunity Map</button>
      <button class="quant-section-nav-btn active" onclick="toggleQuantSection('qs-optimizer',this)">Optimizer</button>
      <button class="quant-section-nav-btn active" onclick="toggleQuantSection('qs-model',this)">Model Desk</button>
      <button class="quant-section-nav-btn active" onclick="toggleQuantSection('qs-comparison',this)">Rules vs Models</button>
      <button class="quant-section-nav-btn active" onclick="toggleQuantSection('qs-reports',this)">Edge Reports</button>
      <button class="quant-section-nav-btn active" onclick="toggleQuantSection('qs-runs',this)">Runs</button>
    </div>

    <!-- 1. Dataset -->
    <div class="quant-section" id="qs-dataset">
      <div class="quant-section-header" onclick="toggleQuantDropdown('qs-dataset')">
        <div><h3>Quant Dataset</h3><div class="sub">Captured tokens, market ticks, feature snapshots, and simulated positions</div></div>
        <span class="quant-section-chevron open" id="qs-dataset-chev">&#9660;</span>
      </div>
      <div class="quant-section-body open" id="qs-dataset-body">
        <div class="control-action-grid" id="quant-overview-grid" style="margin-top:14px">
          <div class="scanner-summary-card"><div class="scanner-summary-label">Tracked Tokens</div><div class="scanner-summary-value">—</div><div class="scanner-summary-copy">Loading dataset</div></div>
        </div>
      </div>
    </div>

    <!-- 2. Backtest -->
    <div class="quant-section" id="qs-backtest">
      <div class="quant-section-header" onclick="toggleQuantDropdown('qs-backtest')">
        <div><h3>Launch Backtest</h3><div class="sub">Replay the saved feature stream across strategy sets</div></div>
        <span class="quant-section-chevron open" id="qs-backtest-chev">&#9660;</span>
      </div>
      <div class="quant-section-body open" id="qs-backtest-body">
        <div class="field-row" style="margin:14px 0 12px">
          <div class="fgroup">
            <label class="flabel">Window</label>
            <select class="finput" id="quant-days">
              <option value="1">Last 1 day</option>
              <option value="3">Last 3 days</option>
              <option value="7" selected>Last 7 days</option>
              <option value="14">Last 14 days</option>
            </select>
          </div>
          <div class="fgroup">
            <label class="flabel">Label</label>
            <input class="finput" id="quant-run-name" type="text" placeholder="e.g. Weekly replay">
          </div>
        </div>
        <div class="field-row" style="margin-bottom:12px">
          <div class="fgroup">
            <label class="flabel">Replay Mode</label>
            <select class="finput" id="quant-replay-mode">
              <option value="snapshot">Snapshot Replay</option>
              <option value="event_tape">Event Tape Replay</option>
            </select>
          </div>
        </div>
        <div class="helper-note">Strategies replayed: safe, balanced, aggressive, degen. Snapshot replay uses saved feature states. Event tape replay walks the classified market event stream in chronological order.</div>
        <div class="panel-actions" style="margin-top:14px">
          <button class="btn btn-primary" type="button" onclick="runQuantBacktest()">Run Replay</button>
        </div>
      </div>
    </div>

    <!-- 3. Flow Regime -->
    <div class="quant-section" id="qs-regime">
      <div class="quant-section-header" onclick="toggleQuantDropdown('qs-regime')">
        <div><h3>Flow Regime Analytics</h3><div class="sub">Wallet participation, smart-money presence, and liquidity health</div></div>
        <span class="quant-section-chevron open" id="qs-regime-chev">&#9660;</span>
      </div>
      <div class="quant-section-body open" id="qs-regime-body">
        <div id="quant-flow-regime" style="margin-top:14px">
          <div style="text-align:center;color:var(--t3);font-size:12px;padding:32px 0">Flow regime analytics loading…</div>
        </div>
      </div>
    </div>

    <!-- 4. Opportunity Map -->
    <div class="quant-section" id="qs-opportunity">
      <div class="quant-section-header" onclick="toggleQuantDropdown('qs-opportunity')">
        <div><h3>Opportunity Map</h3><div class="sub">X/Y visualization of captured vs missed winners and losers avoided</div></div>
        <span class="quant-section-chevron open" id="qs-opportunity-chev">&#9660;</span>
      </div>
      <div class="quant-section-body open" id="qs-opportunity-body">
        <div id="quant-opportunity-map" style="margin-top:14px">
          <div style="text-align:center;color:var(--t3);font-size:12px;padding:32px 0">Opportunity map loading…</div>
        </div>
      </div>
    </div>

    <!-- 5. Optimizer -->
    <div class="quant-section" id="qs-optimizer">
      <div class="quant-section-header" onclick="toggleQuantDropdown('qs-optimizer')">
        <div><h3>Optimizer Sweep</h3><div class="sub">Best thresholds, feature edges, and top outcomes</div></div>
        <span class="quant-section-chevron open" id="qs-optimizer-chev">&#9660;</span>
      </div>
      <div class="quant-section-body open" id="qs-optimizer-body">
        <div id="quant-optimizer" style="margin-top:14px">
          <div style="text-align:center;color:var(--t3);font-size:12px;padding:32px 0">Optimizer sweep loading…</div>
        </div>
      </div>
    </div>

    <!-- 6. Model Desk -->
    <div class="quant-section" id="qs-model">
      <div class="quant-section-header" onclick="toggleQuantDropdown('qs-model')">
        <div><h3>Regime Model Desk</h3><div class="sub">Auto-selecting learned ranking models from live tape regime</div></div>
        <span class="quant-section-chevron open" id="qs-model-chev">&#9660;</span>
      </div>
      <div class="quant-section-body open" id="qs-model-body">
        <div id="quant-model" style="margin-top:14px">
          <div style="text-align:center;color:var(--t3);font-size:12px;padding:32px 0">Model scoring loading…</div>
        </div>
      </div>
    </div>

    <!-- 7. Rules vs Models -->
    <div class="quant-section" id="qs-comparison">
      <div class="quant-section-header" onclick="toggleQuantDropdown('qs-comparison')">
        <div><h3>Rules vs Models</h3><div class="sub">Side-by-side comparison using identical exit assumptions</div></div>
        <span class="quant-section-chevron open" id="qs-comparison-chev">&#9660;</span>
      </div>
      <div class="quant-section-body open" id="qs-comparison-body">
        <div id="quant-comparison" style="margin-top:14px">
          <div style="text-align:center;color:var(--t3);font-size:12px;padding:32px 0">Rules vs models comparison loading…</div>
        </div>
      </div>
    </div>

    <!-- 8. Edge Reports -->
    <div class="quant-section" id="qs-reports">
      <div class="quant-section-header" onclick="toggleQuantDropdown('qs-reports')">
        <div><h3>Edge Reports</h3><div class="sub">Persistent comparison snapshots and automated scorecards</div></div>
        <span class="quant-section-chevron open" id="qs-reports-chev">&#9660;</span>
      </div>
      <div class="quant-section-body open" id="qs-reports-body">
        <div id="quant-reports" style="margin-top:14px">
          <div style="text-align:center;color:var(--t3);font-size:12px;padding:32px 0">Persistent edge reports loading…</div>
        </div>
      </div>
    </div>

    <!-- 9. Runs -->
    <div class="quant-section" id="qs-runs">
      <div class="quant-section-header" onclick="toggleQuantDropdown('qs-runs')">
        <div><h3>Backtest Runs</h3><div class="sub">Recent replay runs with tokens processed and closed simulated trades</div></div>
        <span class="quant-section-chevron open" id="qs-runs-chev">&#9660;</span>
      </div>
      <div class="quant-section-body open" id="qs-runs-body">
        <div class="signals-layout" style="margin-top:14px">
          <div style="min-width:0">
            <div class="glass" style="padding:0;overflow:hidden">
              <div style="display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid var(--b1)">
                <div>
                  <div style="font-weight:700;font-size:13px;color:var(--t1)">Backtest Runs</div>
                  <div style="font-size:11px;color:var(--t3)">Recent replay runs with tokens processed, snapshots used, and closed simulated trades.</div>
                </div>
                <div id="quant-run-count" style="font-size:11px;color:var(--t3)">0 runs</div>
              </div>
              <div id="quant-runs" style="padding:14px">
                <div style="text-align:center;color:var(--t3);font-size:12px;padding:24px 0">No backtest runs yet.</div>
              </div>
            </div>
          </div>
          <div class="signals-side">
            <div class="glass" id="quant-run-detail">
              <div style="text-align:center;color:var(--t3);font-size:12px;padding:40px 0">Select a replay run to inspect strategy summaries and top simulated trades.</div>
            </div>
          </div>
        </div>
      </div>
    </div>

    </div><!-- close tab-quant -->

    <!-- ── Paper Trading Tab ─────────────────────────────────────────────── -->
    <div id="tab-paper" class="tab-pane">
      <div style="max-width:900px;margin:0 auto">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap">
          <h2 style="margin:0;font-size:18px;color:var(--t1)">Paper Trading</h2>
          <span style="font-size:12px;color:var(--t3)">Simulated trades using your current bot settings &mdash; no real money</span>
        </div>
        <!-- Controls -->
        <div class="glass" style="display:flex;gap:16px;align-items:center;flex-wrap:wrap;padding:12px 16px;margin-bottom:12px">
          <div style="display:flex;align-items:center;gap:6px">
            <label style="font-size:12px;color:var(--t2)">Starting SOL</label>
            <input id="paper-balance-input" type="number" step="0.1" min="0.1" value="10" style="width:80px;background:var(--bg2);border:1px solid var(--b1);color:var(--t1);border-radius:6px;padding:4px 8px;font-size:13px">
          </div>
          <div style="display:flex;align-items:center;gap:6px">
            <label style="font-size:12px;color:var(--t2)">Per trade</label>
            <input id="paper-trade-size" type="number" step="0.01" min="0.01" value="0.1" style="width:70px;background:var(--bg2);border:1px solid var(--b1);color:var(--t1);border-radius:6px;padding:4px 8px;font-size:13px">
            <span style="font-size:11px;color:var(--t3)">SOL</span>
          </div>
          <button onclick="paperStart()" style="background:var(--grn);color:#000;border:none;border-radius:6px;padding:6px 16px;font-size:12px;font-weight:700;cursor:pointer">Start Session</button>
          <button onclick="paperReset()" style="background:var(--bg2);color:var(--t2);border:1px solid var(--b1);border-radius:6px;padding:6px 16px;font-size:12px;cursor:pointer">Reset</button>
          <span id="paper-status" style="font-size:11px;color:var(--t3)"></span>
        </div>
        <!-- Strategy Settings -->
        <div class="glass" style="padding:14px 16px;margin-bottom:12px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
            <div style="font-size:13px;font-weight:700;color:var(--t1)">Strategy Settings</div>
            <div style="display:flex;gap:6px">
              <button id="paper-preset-degen" onclick="paperApplyPreset('degen')" style="padding:4px 14px;border-radius:6px;font-size:11px;font-weight:700;cursor:pointer;border:1px solid rgba(239,68,68,.4);background:rgba(239,68,68,.1);color:#f87171;transition:all .15s">Degen</button>
              <button id="paper-preset-balanced" onclick="paperApplyPreset('balanced')" style="padding:4px 14px;border-radius:6px;font-size:11px;font-weight:700;cursor:pointer;border:1px solid rgba(20,199,132,.4);background:rgba(20,199,132,.1);color:#14c784;transition:all .15s">Balanced</button>
              <button id="paper-preset-conservative" onclick="paperApplyPreset('conservative')" style="padding:4px 14px;border-radius:6px;font-size:11px;font-weight:700;cursor:pointer;border:1px solid rgba(59,130,246,.4);background:rgba(59,130,246,.1);color:#60a5fa;transition:all .15s">Conservative</button>
            </div>
          </div>
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px">
            <div>
              <label style="font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.08em;display:block;margin-bottom:4px">Take Profit 1</label>
              <div style="display:flex;align-items:center;gap:4px">
                <input id="paper-tp1" type="number" step="5" min="5" max="500" value="50" onchange="paperSettingsChanged()" style="width:60px;background:var(--bg2);border:1px solid var(--b1);color:var(--grn);border-radius:6px;padding:4px 8px;font-size:13px;font-weight:700">
                <span style="font-size:11px;color:var(--t3)">%</span>
              </div>
              <div style="font-size:9px;color:var(--t3);margin-top:2px">Sell 50% at this target</div>
            </div>
            <div>
              <label style="font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.08em;display:block;margin-bottom:4px">Take Profit 2</label>
              <div style="display:flex;align-items:center;gap:4px">
                <input id="paper-tp2" type="number" step="10" min="10" max="2000" value="150" onchange="paperSettingsChanged()" style="width:60px;background:var(--bg2);border:1px solid var(--b1);color:var(--grn);border-radius:6px;padding:4px 8px;font-size:13px;font-weight:700">
                <span style="font-size:11px;color:var(--t3)">%</span>
              </div>
              <div style="font-size:9px;color:var(--t3);margin-top:2px">Sell remainder here</div>
            </div>
            <div>
              <label style="font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.08em;display:block;margin-bottom:4px">Stop Loss</label>
              <div style="display:flex;align-items:center;gap:4px">
                <span style="font-size:11px;color:var(--red)">-</span>
                <input id="paper-sl" type="number" step="5" min="5" max="90" value="30" onchange="paperSettingsChanged()" style="width:60px;background:var(--bg2);border:1px solid var(--b1);color:var(--red);border-radius:6px;padding:4px 8px;font-size:13px;font-weight:700">
                <span style="font-size:11px;color:var(--t3)">%</span>
              </div>
              <div style="font-size:9px;color:var(--t3);margin-top:2px">Exit to cut losses</div>
            </div>
            <div>
              <label style="font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.08em;display:block;margin-bottom:4px">Trail Stop</label>
              <div style="display:flex;align-items:center;gap:4px">
                <input id="paper-trail" type="number" step="5" min="10" max="80" value="40" onchange="paperSettingsChanged()" style="width:60px;background:var(--bg2);border:1px solid var(--b1);color:var(--t1);border-radius:6px;padding:4px 8px;font-size:13px;font-weight:700">
                <span style="font-size:11px;color:var(--t3)">%</span>
              </div>
              <div style="font-size:9px;color:var(--t3);margin-top:2px">% drop from peak</div>
            </div>
            <div>
              <label style="font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.08em;display:block;margin-bottom:4px">Max Open</label>
              <div style="display:flex;align-items:center;gap:4px">
                <input id="paper-max-pos" type="number" step="1" min="1" max="50" value="10" onchange="paperSettingsChanged()" style="width:60px;background:var(--bg2);border:1px solid var(--b1);color:var(--t1);border-radius:6px;padding:4px 8px;font-size:13px;font-weight:700">
              </div>
              <div style="font-size:9px;color:var(--t3);margin-top:2px">Max simultaneous</div>
            </div>
          </div>
          <div id="paper-preset-label" style="font-size:10px;color:var(--t3);margin-top:10px;font-style:italic">Custom settings</div>
        </div>
        <!-- Summary cards -->
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:12px">
          <div class="glass" style="text-align:center;padding:12px">
            <div style="font-size:11px;color:var(--t3)">Balance</div>
            <div id="paper-bal" style="font-size:18px;font-weight:800;color:var(--t1)">10.000</div>
          </div>
          <div class="glass" style="text-align:center;padding:12px">
            <div style="font-size:11px;color:var(--t3)">Open Positions</div>
            <div id="paper-open-count" style="font-size:18px;font-weight:800;color:var(--t1)">0</div>
          </div>
          <div class="glass" style="text-align:center;padding:12px">
            <div style="font-size:11px;color:var(--t3)">Total P&amp;L</div>
            <div id="paper-pnl" style="font-size:18px;font-weight:800;color:var(--grn)">0.000 SOL</div>
          </div>
          <div class="glass" style="text-align:center;padding:12px">
            <div style="font-size:11px;color:var(--t3)">Win Rate</div>
            <div id="paper-wr" style="font-size:18px;font-weight:800;color:var(--t1)">-</div>
          </div>
        </div>
        <!-- Open positions -->
        <div class="glass" style="margin-bottom:12px;padding:12px 16px">
          <div style="font-size:13px;font-weight:700;color:var(--t1);margin-bottom:8px">Open Positions</div>
          <div id="paper-positions" style="font-size:12px;color:var(--t3)">No open positions</div>
        </div>
        <!-- Trade history -->
        <div class="glass" style="padding:12px 16px">
          <div style="font-size:13px;font-weight:700;color:var(--t1);margin-bottom:8px">Trade History</div>
          <div id="paper-history" style="font-size:12px;color:var(--t3)">No trades yet &mdash; start a session and the bot will paper-trade signals that pass your filters</div>
        </div>
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

<!-- Floating Portfolio Widget -->
<div id="portfolio-fab" onclick="togglePortfolioPanel()" title="My Positions">
  <div class="fab-icon">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12V7H5a2 2 0 0 1 0-4h14v4"/><path d="M3 5v14a2 2 0 0 0 2 2h16v-5"/><path d="M18 12a2 2 0 0 0 0 4h4v-4Z"/></svg>
  </div>
  <span id="fab-count" class="fab-count">0</span>
</div>

<div id="portfolio-panel" class="portfolio-panel">
  <div class="portfolio-header">
    <div>
      <div style="font-size:15px;font-weight:800;color:var(--t1)">Active Positions</div>
      <div id="portfolio-summary" style="font-size:11px;color:var(--t2);margin-top:2px">Loading...</div>
    </div>
    <button onclick="togglePortfolioPanel()" style="background:none;border:none;color:var(--t3);font-size:18px;cursor:pointer;padding:2px 6px">&times;</button>
  </div>
  <div id="portfolio-coins" class="portfolio-coins"></div>
</div>

<!-- Activity Bar -->
<div id="activity-bar" class="activity-shell">
  <div class="activity-head">
    <div class="activity-title">
      <span style="width:6px;height:6px;border-radius:50%;background:#14c784;display:inline-block;animation:blink 2s infinite"></span>
      <span>Activity Log</span>
      <span id="log-count" style="font-size:10px;color:var(--t3)"></span>
    </div>
    <div class="activity-actions">
      <button onclick="toggleLogBar()" id="log-toggle-btn" style="background:none;border:none;color:var(--t3);font-size:11px;cursor:pointer;padding:2px 6px">▼ collapse</button>
    </div>
  </div>
  <div id="log" class="activity-log"></div>
</div>

<style>
.lline{font-size:12px;padding:3px 16px;border-bottom:1px solid rgba(255,255,255,.03);color:#64748b;font-family:'SF Mono','Courier New',monospace;line-height:1.65}
.lline:hover{background:rgba(255,255,255,.02)}
.lbuy{color:#4ade80!important;font-weight:600}.lsell{color:#f87171!important;font-weight:600}
.lsig{color:#fbbf24!important;font-weight:600}.linfo{color:#60a5fa!important}.lscan{color:#818cf8}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}

/* ── Onboarding Tour ───────────────────────────────────────────── */
.tour-overlay{position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:9998;transition:opacity .3s}
.tour-highlight{position:absolute;z-index:9999;box-shadow:0 0 0 4000px rgba(0,0,0,.6);border-radius:12px;border:2px solid #14c784;pointer-events:none;transition:all .35s cubic-bezier(.4,0,.2,1)}
.tour-tooltip{position:absolute;z-index:10000;background:linear-gradient(145deg,rgba(18,28,48,.98),rgba(12,20,38,.98));border:1px solid rgba(20,199,132,.35);border-radius:16px;padding:20px 22px;max-width:340px;min-width:260px;box-shadow:0 20px 60px rgba(0,0,0,.5),0 0 30px rgba(20,199,132,.15);transition:all .35s cubic-bezier(.4,0,.2,1);animation:tourFadeIn .35s ease}
@keyframes tourFadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.tour-tooltip::before{content:"";position:absolute;width:12px;height:12px;background:linear-gradient(145deg,rgba(18,28,48,.98),rgba(12,20,38,.98));border:1px solid rgba(20,199,132,.35);transform:rotate(45deg)}
.tour-tooltip.arrow-top::before{top:-7px;left:28px;border-right:none;border-bottom:none}
.tour-tooltip.arrow-bottom::before{bottom:-7px;left:28px;border-left:none;border-top:none}
.tour-tooltip.arrow-left::before{left:-7px;top:20px;border-right:none;border-top:none}
.tour-step-num{display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border-radius:50%;background:rgba(20,199,132,.2);border:1px solid rgba(20,199,132,.4);color:#14c784;font-size:11px;font-weight:800;margin-right:8px}
.tour-title{font-size:15px;font-weight:800;color:var(--t1);margin-bottom:8px;display:flex;align-items:center}
.tour-desc{font-size:12.5px;color:var(--t2);line-height:1.6;margin-bottom:16px}
.tour-desc strong{color:#14c784}
.tour-actions{display:flex;justify-content:space-between;align-items:center}
.tour-btn{padding:7px 18px;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;border:none;transition:all .15s}
.tour-btn-next{background:#14c784;color:#000}.tour-btn-next:hover{background:#12b575;transform:scale(1.02)}
.tour-btn-skip{background:transparent;color:var(--t3);border:1px solid rgba(255,255,255,.1)}.tour-btn-skip:hover{color:var(--t1);border-color:rgba(255,255,255,.2)}
.tour-btn-done{background:linear-gradient(135deg,#14c784,#0ea5e9);color:#000;padding:9px 24px;font-size:13px}.tour-btn-done:hover{transform:scale(1.03)}
.tour-progress{display:flex;gap:4px;align-items:center}
.tour-dot{width:6px;height:6px;border-radius:50%;background:rgba(255,255,255,.15);transition:all .2s}
.tour-dot.active{background:#14c784;width:18px;border-radius:3px}
.tour-dot.done{background:rgba(20,199,132,.4)}
</style>

<script>
// Plan-based feature gating
window.__PLAN = '{{PLAN_NAME}}';
const PLAN_TIER = {free: 0, trial: 0, basic: 1, pro: 2, elite: 3};
function planAtLeast(required) {
  return (PLAN_TIER[window.__PLAN] || 0) >= (PLAN_TIER[required] || 0);
}
const TAB_PLAN_GATES = {
  scanner: 'free', settings: 'free', signals: 'basic', whales: 'pro',
  positions: 'free', pnl: 'free', quant: 'pro', paper: 'basic',
};

// Deferred — .wrap doesn't exist yet at parse time
document.addEventListener('DOMContentLoaded', function() {
  var w = document.querySelector('.wrap'); if (w) w.style.paddingBottom = '214px';
});

// ── State ─────────────────────────────────────────────────────────────────────
let running = false, allTokens = [], sortCol = 'score', feedSince = 0;
let selectedMint = null, logBarExpanded = true;
let treeCanvas = null, treeCtx = null;
let _charts = {};
let _activeTab = 'scanner';
let _sigData = [], _sigView = [], _sigSelected = null, _sigSelectedKey = null, sigFilter = 'all';
let _patternLab = { tokens: [], deployers: [], themes: [] };
let _opsMetrics = { stats: {}, top_whales: [], threat_map: [], liquidity_risks: [], route_mix: [], failure_reasons: [] };
let _quantOverview = null, _quantOptimizer = null, _quantModel = null, _quantComparison = null, _quantReports = null, _quantModelMode = 'auto', _quantRuns = [], _quantSelectedRunId = null;
let _tabPollers = {};
let _settingsDirty = false;
let _enhancedData = null;
let aiSuggestion = null;

// ── Tab System ────────────────────────────────────────────────────────────────
function activateTab(tab) {
  const btn = document.querySelector(`.tab-btn[data-tab="${tab}"]`);
  switchTab(tab, btn);
}
function showUpgradeOverlay(tab, requiredPlan) {
  const pane = document.getElementById('tab-' + tab);
  if (!pane) return;
  // Show the tab pane (blurred content visible behind overlay)
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  pane.classList.add('active');
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset && b.dataset.tab === tab));
  if (pane.querySelector('.plan-gate-overlay')) return;
  const labels = {basic: 'Basic', pro: 'Pro', elite: 'Elite'};
  const prices = {basic: '$49/mo', pro: '$99/mo', elite: '$199/mo'};
  const features = {
    basic: ['Signal Explorer — see why tokens pass or fail', 'Paper Trading — practice risk-free', 'Quick Buy — one-click entries from scanner', 'Full settings control'],
    pro: ['Whale Tracking — follow smart money', 'Quant Replay Lab — backtest strategies', 'Flow Regime Analytics', 'Everything in Basic'],
    elite: ['Advanced Optimizer & Model Desk', 'Edge Reports & Scorecards', 'Full Telemetry Dashboard', 'Everything in Pro'],
  };
  const planFeats = features[requiredPlan] || features.basic;
  const overlay = document.createElement('div');
  overlay.className = 'plan-gate-overlay';
  overlay.innerHTML =
    '<div class="plan-gate-card">' +
      '<div style="font-size:36px;margin-bottom:12px">&#128274;</div>' +
      '<h3>Unlock ' + (labels[requiredPlan] || 'Premium') + ' Features</h3>' +
      '<p>Upgrade to the ' + (labels[requiredPlan] || '') + ' plan (' + (prices[requiredPlan] || '') + ') to access this section.</p>' +
      '<div class="plan-features">' + planFeats.map(f => '<span>&#10003; ' + f + '</span>').join('') + '</div>' +
      '<a href="/subscribe/' + requiredPlan + '" class="plan-gate-btn">Upgrade to ' + (labels[requiredPlan] || 'Premium') + '</a>' +
      '<div style="margin-top:12px;font-size:11px;color:var(--t3)">Cancel anytime · Instant activation</div>' +
    '</div>';
  pane.style.position = 'relative';
  pane.appendChild(overlay);
}
function switchTab(tab, btn) {
  var requiredPlan = TAB_PLAN_GATES[tab] || 'free';
  if (!planAtLeast(requiredPlan)) {
    showUpgradeOverlay(tab, requiredPlan);
    return;
  }
  _activeTab = tab;
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.getElementById('tab-' + tab).classList.add('active');
  if (btn) btn.classList.add('active');
  const scannerActive = document.getElementById('scanner-active-tab');
  if (scannerActive) scannerActive.textContent = `${(btn?.querySelector('.tab-btn-label')?.textContent || tab)} focus`;
  // Start tab-specific polling
  Object.values(_tabPollers).forEach(id => clearInterval(id));
  _tabPollers = {};
  if (tab === 'scanner') { pollEvaluationFeed(); _tabPollers.ev = setInterval(pollEvaluationFeed, 6000); pollScanDetail(); _tabPollers.sd = setInterval(pollScanDetail, 7000); }
  if (tab === 'signals') { pollSignals(); _tabPollers.sig = setInterval(pollSignals, 6000); }
  if (tab === 'whales') { pollWhales(); _tabPollers.whale = setInterval(pollWhales, 8000); }
  if (tab === 'positions') { pollPositions(); _tabPollers.pos = setInterval(pollPositions, 5000); }
  if (tab === 'pnl') {
    const activeRange = document.querySelector('[data-range].sort-pill.active')?.dataset.range || '1';
    loadPnl(activeRange);
  }
  if (tab === 'quant') { pollQuant(); _tabPollers.quant = setInterval(pollQuant, 12000); }
  if (tab === 'paper') { paperTick(); _tabPollers.paper = setInterval(paperTick, 3000); }
}

// ── Activity bar ──────────────────────────────────────────────────────────────
function toggleLogBar() {
  const bar = document.getElementById('activity-bar');
  const btn = document.getElementById('log-toggle-btn');
  logBarExpanded = !logBarExpanded;
  bar.style.height = logBarExpanded ? '210px' : '38px';
  btn.textContent  = logBarExpanded ? '▼ collapse' : '▲ expand';
}
function focusActivityLog() {
  if (!logBarExpanded) toggleLogBar();
  document.getElementById('log')?.scrollTo({ top: 0, behavior: 'smooth' });
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
function prettyPolicyName(name) {
  return (name || 'unknown').replaceAll('_', ' ');
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
function fmtClock(ts = Date.now()) {
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}
function titleCase(value) {
  return String(value || '')
    .replace(/[_-]+/g, ' ')
    .replace(/\\b\\w/g, m => m.toUpperCase());
}
function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}
function firstValue(obj, keys, fallback='—') {
  for (const key of keys) {
    if (obj && obj[key] !== undefined && obj[key] !== null && obj[key] !== '') return obj[key];
  }
  return fallback;
}
function firstBool(obj, keys) {
  for (const key of keys) {
    if (obj && typeof obj[key] === 'boolean') return obj[key];
  }
  return null;
}
function compactValue(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') return value ? 'On' : 'Off';
  if (typeof value === 'number') return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(1);
  return String(value);
}
function renderEnhancedDashboard(data) {
  const summaryEl = document.getElementById('enhanced-summary');
  const metricsEl = document.getElementById('enhanced-metrics');
  const alertsEl = document.getElementById('enhanced-alerts');
  const statusEl = document.getElementById('enhanced-status-copy');
  if (!summaryEl || !metricsEl || !alertsEl || !statusEl) return;

  if (!data || !data.enabled) {
    statusEl.textContent = 'Core mode active';
    summaryEl.textContent = 'Enhanced risk, MEV, and observability services are not active for this bot session, so the dashboard is showing the core trading surface only.';
    metricsEl.innerHTML = `
      <div class="intel-metric"><div class="intel-label">System</div><div class="intel-value">Core</div><div class="intel-copy">Standard trading loop</div></div>
      <div class="intel-metric"><div class="intel-label">Alerts</div><div class="intel-value">0</div><div class="intel-copy">No enhanced alerts</div></div>
      <div class="intel-metric"><div class="intel-label">MEV Guard</div><div class="intel-value">Standby</div><div class="intel-copy">No advanced route data</div></div>
      <div class="intel-metric"><div class="intel-label">Circuit</div><div class="intel-value">Armed</div><div class="intel-copy">Base drawdown controls only</div></div>
    `;
    alertsEl.innerHTML = '<span class="badge bg-muted">Enhanced telemetry unavailable</span>';
    return;
  }

  const health = data.system_health || {};
  const trading = data.trading_stats || {};
  const mev = data.mev_stats || {};
  const circuit = data.circuit_breaker || {};
  const edgeGuard = data.edge_guard || {};
  const alerts = Array.isArray(data.active_alerts) ? data.active_alerts : [];
  const circuitActive = firstBool(circuit, ['active', 'triggered', 'tripped', 'halted']);
  const circuitLabel = circuitActive === true ? 'Triggered' : circuitActive === false ? 'Armed' : compactValue(firstValue(circuit, ['status', 'state'], 'Armed'));
  const mevLabel = compactValue(firstValue(mev, ['status', 'strategy', 'mode', 'submission_strategy'], firstBool(mev, ['enabled', 'protection_enabled']) ? 'Protected' : 'Monitoring'));
  const healthLabel = compactValue(firstValue(health, ['overall_status', 'status', 'summary'], 'Monitoring'));
  const tradeLabel = compactValue(firstValue(trading, ['total_trades', 'trades_24h', 'executions_24h', 'total_executions'], '—'));
  const edgeLabel = compactValue(firstValue(edgeGuard, ['action_label', 'status'], 'Normal risk'));
  const edgeStatus = String(edgeGuard.status || '').toLowerCase();

  statusEl.textContent = `Enhanced systems ${alerts.length ? 'live' : 'stable'}`;
  summaryEl.textContent = `Observability, risk, and MEV telemetry are online. ${alerts.length ? alerts.length + ' active alert' + (alerts.length === 1 ? '' : 's') + ' need attention.' : 'No active enhanced alerts are currently open.'}`;
  metricsEl.innerHTML = `
    <div class="intel-metric"><div class="intel-label">System</div><div class="intel-value">${healthLabel}</div><div class="intel-copy">Health summary</div></div>
    <div class="intel-metric"><div class="intel-label">Alerts</div><div class="intel-value">${alerts.length}</div><div class="intel-copy">Active alert count</div></div>
    <div class="intel-metric"><div class="intel-label">MEV Guard</div><div class="intel-value">${mevLabel}</div><div class="intel-copy">Execution protection mode</div></div>
    <div class="intel-metric"><div class="intel-label">Circuit</div><div class="intel-value">${circuitLabel}</div><div class="intel-copy">Break-glass state</div></div>
    <div class="intel-metric"><div class="intel-label">Edge Guard</div><div class="intel-value">${edgeLabel}</div><div class="intel-copy">${edgeGuard.reason || 'Report-driven sizing posture'}</div></div>
  `;
  alertsEl.innerHTML = alerts.length
    ? alerts.slice(0, 4).map(a => `<span class="badge ${String(a.severity || '').toLowerCase().includes('high') ? 'bg-red' : 'bg-gold'}">${titleCase(a.severity || 'Alert')}: ${a.title || a.message || 'Unnamed alert'}</span>`).join('')
    : `<span class="badge bg-blue">Trades tracked ${tradeLabel}</span><span class="badge bg-muted">Health ${healthLabel}</span>${edgeStatus && edgeStatus !== 'normal' ? `<span class="badge ${edgeStatus === 'halted' ? 'bg-red' : edgeStatus === 'defensive' ? 'bg-gold' : 'bg-blue'}">Edge guard ${edgeStatus}</span>` : ''}`;
}
async function pollEnhancedDashboard() {
  try {
    const data = await fetch('/api/enhanced/dashboard').then(r => r.json()).catch(() => null);
    _enhancedData = data || null;
    renderEnhancedDashboard(_enhancedData);
  } catch (e) {}
}
async function refreshNow() {
  await refresh();
  if (_activeTab === 'signals') await pollSignals();
  if (_activeTab === 'whales') await pollWhales();
  if (_activeTab === 'positions') await pollPositions();
  if (_activeTab === 'pnl') {
    const activeRange = document.querySelector('[data-range].sort-pill.active')?.dataset.range || '7';
    await loadPnl(activeRange);
  }
  if (_activeTab === 'quant') await pollQuant();
  await pollEnhancedDashboard();
  showToast('Dashboard refreshed');
}
function registerKeyboardShortcuts() {
  document.addEventListener('keydown', event => {
    const tag = document.activeElement?.tagName || '';
    const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(tag);
    if (event.key === '/' && !typing) {
      event.preventDefault();
      const target = _activeTab === 'signals' ? document.getElementById('sig-search') : document.getElementById('scan-search');
      target?.focus();
      target?.select?.();
      return;
    }
    if (typing) {
      if (event.key === 'Escape') document.activeElement?.blur?.();
      return;
    }
    const tabMap = { '1': 'scanner', '2': 'settings', '3': 'signals', '4': 'whales', '5': 'positions', '6': 'pnl', '7': 'quant', '8': 'paper' };
    if (tabMap[event.key]) {
      event.preventDefault();
      activateTab(tabMap[event.key]);
      return;
    }
    if (event.key.toLowerCase() === 'r') {
      event.preventDefault();
      refreshNow();
    }
    if (event.key.toLowerCase() === 's') {
      event.preventDefault();
      openSettingsTab();
    }
  });
}

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
  setText('scanner-token-live', String(tokens.length));
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
        ${planAtLeast('basic') ? '<td><button class="buy-btn-mini" onclick="event.stopPropagation();quickBuy(\\x27'+t.mint+'\\x27,\\x27'+nameSafe+'\\x27,this)">&#9889; Buy</button></td>' : '<td><span style="font-size:9px;color:var(--t3)">Basic+</span></td>'}
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

// ── Force Buy (from Approved Coins) ──────────────────────────────────────────
async function forceBuyCoin(mint, name, btn) {
  const orig = btn.textContent;
  btn.textContent = '…'; btn.disabled = true;
  const res = await fetch('/api/force-buy', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({mint, name})
  }).then(r => r.json()).catch(() => ({ok:false, msg:'Failed'}));
  btn.textContent = res.ok ? '✅' : '❌';
  showToast(res.ok ? '⚡ Force buy sent!' : '⚠ ' + (res.msg||'Buy failed'), res.ok);
  setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 3000);
  if (res.ok) setTimeout(refresh, 2000);
}

async function submitForceBuy() {
  const input = document.getElementById('force-buy-mint');
  const mint = (input.value || '').trim();
  if (!mint || mint.length < 32) {
    showToast('⚠ Paste a valid Solana mint address', false);
    return;
  }
  // Try to get the name from DexScreener
  let name = mint.slice(0, 8) + '…';
  try {
    const dex = await fetch('https://api.dexscreener.com/latest/dex/tokens/' + mint).then(r => r.json());
    const pair = (dex.pairs || []).find(p => p.chainId === 'solana');
    if (pair && pair.baseToken) name = pair.baseToken.symbol || pair.baseToken.name || name;
  } catch(e) {}
  const res = await fetch('/api/force-buy', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({mint, name})
  }).then(r => r.json()).catch(() => ({ok:false, msg:'Failed'}));
  showToast(res.ok ? `⚡ Force buy sent for ${name}!` : '⚠ ' + (res.msg||'Buy failed'), res.ok);
  if (res.ok) { input.value = ''; setTimeout(refresh, 2000); }
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
function closeTree() { selectedMint = null; const tp = document.getElementById('tree-panel'); if (tp) tp.style.display = 'none'; renderTokenRows(); }
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
      paperUpdatePrices();
      setText('scanner-last-market', `Feed ${fmtClock()}`);
    }
  } catch(e) {}
}

function updateTicker() {
  // Ticker strip removed — no-op stub
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
      setText('scanner-listing-live', String(listingCatchCount));
      setText('scanner-last-listing', `Listings ${fmtClock()}`);
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
  's-execution-mode', 's-policy-mode', 's-model-threshold', 's-auto-promote',
  's-auto-window', 's-auto-min-reports', 's-auto-lock',
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
  min_score: 15,
  risk_per_trade_pct: 2.0,
  min_holder_growth_pct: 5,
  min_narrative_score: 2,
  min_green_lights: 0,
  min_volume_spike_mult: 1.2,
  late_entry_mult: 5.0,
  nuclear_narrative_score: 40,
  offpeak_min_change: 18,
  max_hot_change: 400,
  anti_rug: true,
  check_holders: true,
  execution_mode: 'live',
  decision_policy: 'rules',
  model_threshold: 60,
  auto_promote: false,
  auto_promote_window_days: 7,
  auto_promote_min_reports: 3,
  auto_promote_lock_minutes: 180,
};
const SETTINGS_META = [
  ['Mode', s => String(s.preset || 'balanced')],
  ['Execution', s => String(s.execution_mode || 'live')],
  ['Policy', s => String(s.decision_policy || 'rules').replaceAll('_', ' ')],
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
  if (el) {
    el.textContent = text;
    el.classList.toggle('pending', pending);
  }
  const copy = document.getElementById('settings-save-state-copy');
  if (copy) copy.textContent = text;
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
  window._lastSettings = s;
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
  setVal('s-execution-mode', s.execution_mode ?? SETTINGS_DEFAULTS.execution_mode);
  setVal('s-policy-mode', s.decision_policy ?? s.policy_mode ?? SETTINGS_DEFAULTS.decision_policy);
  setVal('s-model-threshold', s.model_threshold ?? SETTINGS_DEFAULTS.model_threshold);
  setVal('s-auto-window', s.auto_promote_window_days ?? SETTINGS_DEFAULTS.auto_promote_window_days);
  setVal('s-auto-min-reports', s.auto_promote_min_reports ?? SETTINGS_DEFAULTS.auto_promote_min_reports);
  setVal('s-auto-lock', s.auto_promote_lock_minutes ?? SETTINGS_DEFAULTS.auto_promote_lock_minutes);
  setChecked('s-antirug', s.anti_rug ?? SETTINGS_DEFAULTS.anti_rug);
  setChecked('s-checkholders', s.check_holders ?? SETTINGS_DEFAULTS.check_holders);
  setChecked('s-auto-promote', s.auto_promote ?? SETTINGS_DEFAULTS.auto_promote);
  renderSettingsVisuals({ ...s, preset: presetName || s.preset || 'balanced' });
}
// ── Evaluation Ramp Tab ──────────────────────────────────────────
let _evFilter = 'all';
let _evData = { evaluations: [], approved: [], pipeline: {} };
let _evExpandedId = null;
let _scFilter = 'all';  // backward compat
let _scEvents = [];       // backward compat

function evSetFilter(btn) {
  _evFilter = btn.dataset.evf;
  document.querySelectorAll('.ev-fbtn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  pollEvaluationFeed();
}

function scSetFilter() {} // stub

async function pollEvaluationFeed() {
  try {
    const data = await fetch('/api/evaluation-feed?limit=60&filter=' + _evFilter).then(r => r.json()).catch(() => null);
    if (data) {
      _evData = data;
      renderEvPipeline(data.pipeline || {});
      renderEvRejectBar(data.pipeline?.top_rejects || []);
      renderEvLog();
      renderEvApproved(data.approved || []);
    }
  } catch(e) {}
}

function renderEvPipeline(p) {
  const total = p.total_evaluated || 0;
  const passed = p.total_passed || 0;
  const rejected = total - passed;
  setText('evp-scanned', total.toLocaleString());
  setText('evp-evaluated', total.toLocaleString());
  setText('evp-passed', passed.toLocaleString());
  setText('evp-rejected', rejected.toLocaleString());
  setText('evp-rate', (p.pass_rate || 0).toFixed(1) + '%');
}

function renderEvRejectBar(rejects) {
  const el = document.getElementById('ev-reject-bar');
  if (!el) return;
  if (!rejects.length) { el.innerHTML = ''; return; }
  el.innerHTML = rejects.slice(0, 5).map(r =>
    `<div class="ev-reject-chip"><span class="cnt">${r.count}</span> ${r.reason_plain || r.reason}</div>`
  ).join('');
}

function renderEvLog() {
  const list = document.getElementById('ev-log-list');
  if (!list) return;
  const evals = _evData.evaluations || [];
  const sub = document.getElementById('ev-log-sub');
  if (sub) sub.textContent = evals.length + ' evaluations loaded from database';

  if (!evals.length) {
    list.innerHTML = '<div class="ev-empty">No evaluations yet. Start the bot to see coins flow through the pipeline.<br><span style="font-size:11px;color:var(--t3);margin-top:8px;display:block">The bot scans for new tokens, evaluates them through filters, and logs every decision here.</span></div>';
    return;
  }

  list.innerHTML = evals.map(ev => {
    const sc = typeof ev.score === 'number' ? ev.score : 0;
    const col = scoreColor(sc);
    const checks = (ev.checklist || []).slice(0, 3);
    const checksHtml = checks.length
      ? checks.map(c => `<div class="ev-ck ${c.passed ? 'p' : 'f'}" title="${c.name || ''}">${c.passed ? '&#10003;' : '&#10007;'}</div>`).join('')
      : '<div class="ev-ck f">?</div><div class="ev-ck f">?</div><div class="ev-ck f">?</div>';
    const tagsHtml = (ev.narrative_tags || []).slice(0, 3).map(t =>
      `<span class="ev-tag">${t}</span>`
    ).join('');
    const ts = ev.ts ? new Date(ev.ts).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '';
    const isExpanded = _evExpandedId === ev.id;

    let detailHtml = '';
    if (isExpanded) {
      detailHtml = `<div class="ev-detail open">
        <div class="ev-detail-head">
          <div class="ev-detail-title">${ev.name || '?'} <span style="font-size:11px;color:var(--t3);font-weight:400;margin-left:8px">${ev.mint ? ev.mint.slice(0,12)+'...' : ''}</span></div>
          <a href="https://dexscreener.com/solana/${ev.mint || ''}" target="_blank" style="font-size:10px;color:var(--blue2);text-decoration:none">DexScreener &rarr;</a>
        </div>
        <div class="ev-detail-grid">
          <div class="ev-detail-cell"><div class="ev-detail-lbl">Score</div><div class="ev-detail-val" style="color:${col}">${sc}</div></div>
          <div class="ev-detail-cell"><div class="ev-detail-lbl">Price</div><div class="ev-detail-val">${fmtPrice(ev.price)}</div></div>
          <div class="ev-detail-cell"><div class="ev-detail-lbl">MC</div><div class="ev-detail-val">${fmtK(ev.mc)}</div></div>
          <div class="ev-detail-cell"><div class="ev-detail-lbl">Liquidity</div><div class="ev-detail-val">${fmtK(ev.liq)}</div></div>
          <div class="ev-detail-cell"><div class="ev-detail-lbl">Volume</div><div class="ev-detail-val">${fmtK(ev.vol)}</div></div>
          <div class="ev-detail-cell"><div class="ev-detail-lbl">Age</div><div class="ev-detail-val">${ev.age_min ? fmtAge(ev.age_min) : '—'}</div></div>
          <div class="ev-detail-cell"><div class="ev-detail-lbl">Change</div><div class="ev-detail-val ${(ev.change||0) >= 0 ? 'pos' : 'neg'}">${chgStr(ev.change||0)}</div></div>
          <div class="ev-detail-cell"><div class="ev-detail-lbl">Green Lights</div><div class="ev-detail-val">${ev.green_lights || 0}/3</div></div>
          <div class="ev-detail-cell"><div class="ev-detail-lbl">Threat</div><div class="ev-detail-val" style="color:${(ev.threat_score||0) >= 30 ? '#ef4444' : 'var(--t1)'}">${ev.threat_score || 0}</div></div>
          <div class="ev-detail-cell"><div class="ev-detail-lbl">Whale</div><div class="ev-detail-val">${ev.whale_score || 0}</div></div>
          <div class="ev-detail-cell"><div class="ev-detail-lbl">Deployer</div><div class="ev-detail-val">${ev.deployer_score || 0}</div></div>
          <div class="ev-detail-cell"><div class="ev-detail-lbl">Narrative</div><div class="ev-detail-val">${ev.narrative_score || 0}</div></div>
        </div>
        <div style="font-size:11px;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">Three-Signal Checklist</div>
        ${checks.length ? checks.map(c => `
          <div class="ev-checklist-row">
            <div class="ev-checklist-dot ${c.passed ? 'p' : 'f'}"></div>
            <span class="ev-checklist-name">${c.name || 'Check'}</span>
            <span class="ev-checklist-status" style="color:${c.passed ? '#14c784' : '#ef4444'}">${c.passed ? 'PASS' : 'FAIL'}</span>
          </div>
        `).join('') : '<div style="font-size:11px;color:var(--t3)">Checklist data not available</div>'}
        <div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap">
          ${(ev.narrative_tags||[]).map(t => `<span class="ev-tag">${t}</span>`).join('')}
          ${ev.source ? `<span style="font-size:9px;color:var(--t3);padding:2px 6px;border:1px solid rgba(255,255,255,.06);border-radius:4px">${ev.source}</span>` : ''}
        </div>
      </div>`;
    }

    return `<div class="ev-row" onclick="evToggleDetail(${ev.id})" title="Click for details">
      <div class="ev-verdict ${ev.passed ? 'pass' : 'fail'}">${ev.passed ? '&#10003;' : '&#10007;'}</div>
      <div class="ev-info">
        <div class="ev-name">${ev.name || '?'}${ev.source ? '<span class="ev-src">' + ev.source + '</span>' : ''}</div>
        <div class="ev-reason ${ev.passed ? 'pass-reason' : ''}">${ev.passed ? 'Approved — score ' + sc : (ev.reason_plain || ev.reason || 'Rejected')}</div>
        ${tagsHtml ? '<div class="ev-tags">' + tagsHtml + '</div>' : ''}
      </div>
      <div class="ev-score">
        <div class="ev-score-bar"><div class="ev-score-fill" style="width:${Math.min(sc,100)}%;background:${col}"></div></div>
        <span class="ev-score-num" style="color:${col}">${sc}</span>
      </div>
      <div class="ev-checks">${checksHtml}</div>
      <div class="ev-time">${ts}</div>
    </div>${detailHtml}`;
  }).join('');
}

function evToggleDetail(id) {
  _evExpandedId = _evExpandedId === id ? null : id;
  renderEvLog();
}

function renderEvApproved(approved) {
  const el = document.getElementById('ev-approved-list');
  if (!el) return;
  if (!approved.length) {
    el.innerHTML = '<div style="padding:20px;text-align:center;font-size:11px;color:var(--t3)">No approved coins yet</div>';
    return;
  }
  el.innerHTML = approved.map(c => {
    const sc = typeof c.score === 'number' ? c.score : 0;
    const col = tokColor(c.name || '?');
    const checks = (c.checklist || []).slice(0, 3);
    const ts = c.ts ? new Date(c.ts).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : '';
    const mintShort = (c.mint||'').slice(0,8) + '…';
    return `<div class="ev-coin-card">
      <div class="ev-coin-top" onclick="window.open('https://dexscreener.com/solana/${c.mint||''}','_blank')" style="cursor:pointer">
        <div style="display:flex;align-items:center;gap:8px">
          <div class="ev-coin-icon" style="background:${col}1a;color:${col}">${(c.name||'?').charAt(0)}</div>
          <div>
            <div class="ev-coin-name">${c.name || '?'}</div>
            <div class="ev-coin-meta">${fmtK(c.mc)} MC · ${fmtAge(c.age_min||0)} · ${ts}</div>
          </div>
        </div>
        <div class="ev-coin-score" style="color:${scoreColor(sc)}">${sc}</div>
      </div>
      <div class="ev-coin-metrics">
        <div class="ev-coin-m">Liq <span class="ev-coin-mv">${fmtK(c.liq)}</span></div>
        <div class="ev-coin-m">Vol <span class="ev-coin-mv">${fmtK(c.vol)}</span></div>
        <div class="ev-coin-m">GL <span class="ev-coin-mv">${c.green_lights||0}/3</span></div>
      </div>
      <div class="ev-coin-checks" style="margin-top:5px">
        ${checks.map(ck => `<div class="ev-ck ${ck.passed?'p':'f'}" title="${ck.name||''}">${ck.passed?'&#10003;':'&#10007;'}</div>`).join('')}
        ${(c.narrative_tags||[]).slice(0,2).map(t => `<span class="ev-tag">${t}</span>`).join('')}
      </div>
      <div style="margin-top:6px;display:flex;gap:6px">
        <button class="btn" style="flex:1;font-size:9px;padding:5px 0;background:linear-gradient(135deg,#14c784,#0ea5e9);border:none;border-radius:6px;color:#fff;font-weight:700;cursor:pointer" onclick="event.stopPropagation();forceBuyCoin('${c.mint||''}','${(c.name||'?').replace(/'/g,"\\'")}',this)">⚡ Force Buy</button>
        <button class="btn btn-ghost" style="flex:1;font-size:9px;padding:5px 0;border-radius:6px" onclick="event.stopPropagation();window.open('https://dexscreener.com/solana/${c.mint||''}','_blank')">Chart →</button>
      </div>
    </div>`;
  }).join('');
}

function renderScEvents() {} // stub
function buildScEvents() { return []; } // stub

function buildScEvents(logLines, filterLog) {
  const events = [];

  // Parse filter log: each entry is a pass/fail decision
  if (filterLog && filterLog.length) {
    filterLog.forEach(f => {
      events.push({
        type: f.passed ? 'bought' : 'skipped',
        name: f.name || '?',
        reason: f.passed ? `✓ Passed — score ${f.score || '?'}` : `✗ ${f.reason || 'Filtered'}`,
        pnl: null,
        ts: f.ts || '',
        _raw: f,
      });
    });
  }

  // Parse activity log lines for BUY/SELL events not already covered
  if (logLines && logLines.length) {
    logLines.forEach(line => {
      if (line.includes('BUY')) {
        const nameMatch = line.match(/BUY\s+([A-Za-z0-9$\s]+?)(?:\s+@|\s+–|$)/);
        const name = nameMatch ? nameMatch[1].trim() : '?';
        const tsMatch = line.match(/\[(\d{2}:\d{2}:\d{2})\]/);
        events.push({ type: 'bought', name, reason: line.replace(/^\[.*?\]\s*/, '').substring(0, 80), pnl: null, ts: tsMatch ? tsMatch[1] : '' });
      } else if (line.includes('SELL') || line.includes('CASHOUT')) {
        const pnlMatch = line.match(/([+-]?\d+\.?\d*)%/);
        const tsMatch = line.match(/\[(\d{2}:\d{2}:\d{2})\]/);
        const pnl = pnlMatch ? parseFloat(pnlMatch[1]) : null;
        events.push({ type: 'sold', name: line.replace(/^\[.*?\]\s*/, '').split(' ').slice(1, 3).join(' '), reason: line.replace(/^\[.*?\]\s*/, '').substring(0, 80), pnl, ts: tsMatch ? tsMatch[1] : '' });
      } else if (line.includes('Auto-tune') || line.includes('[BOT]') || line.includes('Bot started') || line.includes('strategy')) {
        const tsMatch = line.match(/\[(\d{2}:\d{2}:\d{2})\]/);
        events.push({ type: 'system', name: line.replace(/^\[.*?\]\s*/, '').substring(0, 60), reason: '', pnl: null, ts: tsMatch ? tsMatch[1] : '' });
      }
    });
  }

  // Sort newest first
  return events.reverse();
}

function renderScannerTab(d) {
  if (!d) return;
  // Update sidebar session info from /api/state
  const bal = d.balance ?? 0;
  const positions = d.positions || [];
  setText('sc-wallet-bal', bal.toFixed(4));
  setText('sc-wallet-bal2', bal.toFixed(4) + ' SOL');
  setText('sc-open-trades', positions.length);

  const stats = d.stats || {};
  const wins = parseInt(stats.wins || 0);
  const losses = parseInt(stats.losses || 0);
  const total = wins + losses;
  const wr = total > 0 ? Math.round(wins / total * 100) : null;
  setText('sc-session-wr', wr != null ? wr + '%' : '—');

  const pnl = parseFloat(stats.total_pnl_sol || 0);
  const pnlEl = document.getElementById('sc-session-pnl');
  if (pnlEl) {
    pnlEl.textContent = (pnl >= 0 ? '+' : '') + pnl.toFixed(4);
    pnlEl.style.color = pnl >= 0 ? '#14c784' : '#ef4444';
  }

  const presetName = d.preset || (d.settings || {}).preset || 'balanced';
  setText('sc-preset-name', presetName);

  // Risk controls from preset (no manual session limits)
  const s = d.settings || {};
  renderRiskControls(s);

  // Poll evaluation feed when scanner tab active
  if (_activeTab === 'scanner') pollEvaluationFeed();
  if (_activeTab === 'scanner') pollScanDetail();
}

// ── Scan Balance Strip ────────────────────────────────────────────────────────
function renderScanBalStrip(d) {
  const bal = d.balance || 0;
  const stats = d.stats || {};
  const pnl = stats.total_pnl_sol || 0;
  const wins = stats.wins || 0;
  const losses = stats.losses || 0;
  const total = wins + losses;
  const wr = total > 0 ? Math.round(wins / total * 100) : null;
  setText('sbs-balance', bal.toFixed(4) + ' SOL');
  setText('sbs-open', d.positions?.length ?? 0);
  const pnlEl = document.getElementById('sbs-pnl');
  if (pnlEl) { pnlEl.textContent = (pnl >= 0 ? '+' : '') + pnl.toFixed(4); pnlEl.style.color = pnl >= 0 ? 'var(--grn)' : 'var(--red2)'; }
  setText('sbs-wins', wins);
  setText('sbs-losses', losses);
  setText('sbs-wr', wr !== null ? wr + '%' : '—');
}

// ── Scan Detail Poller (positions cards, history, optimizer) ──────────────────
async function pollScanDetail() {
  const d = await fetch('/api/scan-detail').then(r => r.json()).catch(() => null);
  if (!d) return;

  // ── Position Cards ──────────────────────────────────────────────────────────
  const cards = document.getElementById('pos-cards');
  if (cards) {
    if (d.positions && d.positions.length) {
      const tp1target = (window._lastSettings && window._lastSettings.tp1_mult) || 1.5;
      cards.innerHTML = d.positions.map(p => {
        const pnl = p.pnl_pct;
        const won = pnl != null && pnl >= 0;
        const pnlCls = pnl == null ? '' : won ? 'c-grn' : 'c-red';
        const cardCls = pnl == null ? '' : won ? 'winning' : 'losing';
        const pnlTxt = pnl == null ? '?' : (pnl >= 0 ? '+' : '') + pnl.toFixed(1) + '%';
        const solTxt = (p.pnl_sol >= 0 ? '+' : '') + (p.pnl_sol || 0).toFixed(4) + ' SOL';
        const tp1 = p.tp1_hit ? '<span class="pc-badge">TP1✓</span>' : '';
        const barPct = p.ratio ? Math.min(100, Math.max(0, ((p.ratio - 1) / (tp1target - 1)) * 100)) : 0;
        const barCls = pnl != null && pnl < 0 ? 'red' : '';
        return `<div class="pos-card ${cardCls}" onclick="window.open('https://dexscreener.com/solana/${p.address}','_blank')">
          <div class="pc-top">
            <div><div class="pc-name">${p.name}${tp1}</div><div class="pc-reason">${p.buy_reason || p.source || 'signal'}</div></div>
            <div><div class="pc-pnl ${pnlCls}">${pnlTxt}</div><div class="pc-pnl-sub">${solTxt}</div></div>
          </div>
          <div class="pc-meta">
            <span>${p.ratio != null ? p.ratio.toFixed(3) + 'x' : ''}</span>
            <span>peak ${p.peak_ratio != null ? p.peak_ratio.toFixed(2) + 'x' : '—'}</span>
            <span>${p.age_min}m</span>
            <span>${(p.entry_sol || 0).toFixed(3)} SOL in</span>
          </div>
          <div class="pc-bar-wrap"><div class="pc-bar ${barCls}" style="width:${barPct}%"></div></div>
        </div>`;
      }).join('');
    } else {
      cards.innerHTML = '<div style="font-size:11px;color:var(--t3);padding:4px 0">No open positions</div>';
    }
  }

  // ── Winners ─────────────────────────────────────────────────────────────────
  function histHTML(list) {
    if (!list || !list.length) return '<div style="padding:14px;font-size:11px;color:var(--t3)">None yet</div>';
    return list.map(t => `<div class="scan-hist-item">
      <span class="shi-name">${t.name}</span>
      <span class="shi-reason" title="${t.close_reason}">${t.close_reason}</span>
      <span class="shi-pnl ${t.pnl_sol >= 0 ? 'c-grn' : 'c-red'}">${t.pnl_sol >= 0 ? '+' : ''}${t.pnl_sol.toFixed(4)}</span>
      <span class="shi-ts">${t.ts}</span>
    </div>`).join('');
  }
  const wEl = document.getElementById('scan-winners');
  const lEl = document.getElementById('scan-losers');
  if (wEl) wEl.innerHTML = histHTML(d.winners);
  if (lEl) lEl.innerHTML = histHTML(d.losers);

  // ── Optimizer ───────────────────────────────────────────────────────────────
  const optEl = document.getElementById('scan-optimizer');
  if (optEl) {
    if (d.optimizer && d.optimizer.length) {
      optEl.innerHTML = d.optimizer.map(o => `<div class="scan-opt-row">
        <span class="scan-opt-lbl" title="${o.label}">${o.label}</span>
        <div class="scan-opt-bar-wrap"><div class="scan-opt-bar" style="width:${o.win_rate}%"></div></div>
        <span class="scan-opt-pct">${o.win_rate}%</span>
        <span class="scan-opt-cnt">${o.wins}/${o.trades}</span>
      </div>`).join('');
    } else {
      optEl.innerHTML = '<div style="font-size:11px;color:var(--t3)">Accumulating trade data…</div>';
    }
  }
}

// Risk controls are read-only from preset — no manual session limits
function renderRiskControls(s) {
  setText('sc-dd-limit', (s.drawdown_limit_sol || 0).toFixed(2) + ' SOL');
  setText('sc-max-pos', s.max_correlated || '—');
  setText('sc-stop-loss', s.stop_loss ? (s.stop_loss * 100).toFixed(0) + '%' : '—');
  setText('sc-trail-pct', s.trail_pct ? (s.trail_pct * 100).toFixed(0) + '%' : '—');
}

// (removed: saveBudgetSettings — session limits now come from preset)
['placeholder-no-op'].forEach(id => {
  document.addEventListener('DOMContentLoaded', () => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', () => { el._dirty = true; });
  });
});
// ── End Scanner Activity Tab ─────────────────────────────────────

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
    execution_mode: document.getElementById('s-execution-mode')?.value || SETTINGS_DEFAULTS.execution_mode,
    decision_policy: document.getElementById('s-policy-mode')?.value || SETTINGS_DEFAULTS.decision_policy,
    model_threshold: readNum('s-model-threshold', SETTINGS_DEFAULTS.model_threshold),
    auto_promote: !!document.getElementById('s-auto-promote')?.checked,
    auto_promote_window_days: readNum('s-auto-window', SETTINGS_DEFAULTS.auto_promote_window_days),
    auto_promote_min_reports: readNum('s-auto-min-reports', SETTINGS_DEFAULTS.auto_promote_min_reports),
    auto_promote_lock_minutes: readNum('s-auto-lock', SETTINGS_DEFAULTS.auto_promote_lock_minutes),
  };
}
function prettyExecutionPolicy(name) {
  const key = String(name || 'rules');
  if (key === 'model_global') return 'Global model';
  if (key === 'model_regime_auto') return 'Regime model';
  if (key === 'auto') return 'Auto';
  return 'Rules';
}
function renderExecutionControlSummary(bundle) {
  const el = document.getElementById('execution-control-summary');
  if (!el) return;
  const control = bundle?.control || {};
  const policyMode = control.policy_mode || control.decision_policy || 'rules';
  const selected = bundle?.selected_policy || control.active_policy || policyMode || 'rules';
  const executionMode = bundle?.execution_mode || control.execution_mode || 'live';
  const badges = [
    `<span class="badge ${executionMode === 'paper' ? 'bg-gold' : 'bg-blue'}">${executionMode === 'paper' ? 'Paper mode' : 'Live capital'}</span>`,
    `<span class="badge bg-muted">Policy ${prettyExecutionPolicy(policyMode)}</span>`,
    `<span class="badge bg-muted">Active ${prettyExecutionPolicy(selected)}</span>`,
    `<span class="badge bg-muted">Threshold ${Number(bundle?.model_threshold ?? control.model_threshold ?? 60).toFixed(1)}</span>`,
  ];
  if (bundle?.selection_source) badges.push(`<span class="badge bg-muted">${bundle.selection_source}</span>`);
  if (bundle?.lock_until) badges.push(`<span class="badge bg-muted">Locked until ${new Date(bundle.lock_until).toLocaleString()}</span>`);
  el.innerHTML = badges.join('');
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
  const presetLabel = titleCase(settings.preset || 'balanced');
  setText('hero-preset-badge', `${presetLabel} preset`);
  setText('hero-launch-mode', presetLabel);
  setText('settings-header-preset', `${presetLabel} profile`);
  el.innerHTML = [
    `<span class="badge bg-blue">${presetLabel}</span>`,
    `<span class="badge bg-muted">TP1 ${Number(settings.tp1_mult || 0).toFixed(2)}x</span>`,
    `<span class="badge bg-muted">TP2 ${Number(settings.tp2_mult || 0).toFixed(2)}x</span>`,
    `<span class="badge bg-muted">Score >= ${Number(settings.min_score || 0)}</span>`,
    `<span class="badge bg-muted">GL ${Number(settings.min_green_lights || 0)}</span>`,
    `<span class="badge bg-muted">Liq ${Number(settings.min_liq || 0) > 0 ? '$' + Number(settings.min_liq || 0).toLocaleString() : 'off'}</span>`,
    `<span class="badge bg-muted">Holders ${settings.check_holders ? 'on' : 'off'}</span>`,
  ].join('');
}
function renderTokenJourney(settings) {
  const el = document.getElementById('settings-token-journey');
  if (!el) return;
  const s = { ...SETTINGS_DEFAULTS, ...(settings || {}) };
  const liqText = Number(s.min_liq || 0) > 0 ? `>= $${Number(s.min_liq).toLocaleString()}` : 'off';
  const stages = [
    {
      icon: '1', phase: 'DISCOVER', title: 'Scanner Finds Token',
      desc: 'DexScreener profiles, new pairs, Helius sniper, whale tracker, and listing scanner all feed into the same evaluator.',
      gate: 'All sources \u2192 one path',
    },
    {
      icon: '2', phase: 'FILTER', title: 'Market Cap',
      desc: 'Must land inside your MC range.',
      gate: `$${Number(s.min_mc).toLocaleString()} \u2013 $${Number(s.max_mc).toLocaleString()}`,
    },
    {
      icon: '3', phase: 'FILTER', title: 'Liquidity',
      desc: Number(s.min_liq || 0) > 0 ? 'Pool liquidity must meet your minimum.' : 'Liquidity filter is disabled.',
      gate: liqText,
    },
    {
      icon: '4', phase: 'FILTER', title: 'Token Age',
      desc: 'Older tokens are rejected to catch early momentum.',
      gate: `<= ${Number(s.max_age_min).toLocaleString()} min`,
    },
    {
      icon: '5', phase: 'FILTER', title: 'Price Change',
      desc: 'Must be gaining but not overextended.',
      gate: `> 0% and <= ${Number(s.max_hot_change).toLocaleString()}%`,
    },
    {
      icon: '6', phase: 'FILTER', title: '24h Volume',
      desc: 'Minimum trading activity to prove real participation.',
      gate: `>= $${Number(s.min_vol).toLocaleString()}`,
    },
    {
      icon: '7', phase: 'FILTER', title: 'AI Score',
      desc: 'Composite score from volume, liquidity, age, change, and momentum signals.',
      gate: `>= ${Number(s.min_score).toLocaleString()} / 100`,
    },
    {
      icon: '8', phase: 'INTEL', title: 'Green Lights Checklist',
      desc: 'Tracked wallet edge, holder acceleration, and narrative timing must align.',
      gate: `${Number(s.min_green_lights)} of 3 lights | holders >= ${Number(s.min_holder_growth_pct)}% | narrative >= ${Number(s.min_narrative_score)}`,
    },
    {
      icon: '9', phase: 'INTEL', title: 'Late Entry Guard',
      desc: 'Blocks tokens that already pumped too far \u2014 unless narrative is nuclear.',
      gate: `<= ${Number(s.late_entry_mult)}x unless narrative >= ${Number(s.nuclear_narrative_score)}`,
    },
    {
      icon: '10', phase: 'INTEL', title: 'Threat & Exit Check',
      desc: 'Threat score, transfer hooks, exit route availability.',
      gate: 'Threat < 60 | exit route exists | no transfer hook',
    },
    {
      icon: '11', phase: 'DECISION', title: 'Model / Policy Gate',
      desc: 'Rules-based, ML model, or auto-promoted policy decides final entry.',
      gate: `Policy: ${s.decision_policy || s.policy_mode || 'rules'}`,
    },
    {
      icon: '12', phase: 'BUY LOCK', title: 'Acquire Buy Lock',
      desc: 'Serializes the buy so concurrent scanners cannot cause duplicate entries.',
      gate: 'One buy at a time per bot',
    },
    {
      icon: '13', phase: 'RUNTIME', title: 'Balance & Position Limits',
      desc: 'Checks live balance, max open positions, and session drawdown.',
      gate: `Max ${Number(s.max_correlated)} positions | Drawdown ${Number(s.drawdown_limit_sol || 0).toFixed(2)} SOL`,
    },
    {
      icon: '14', phase: 'RUNTIME', title: 'Cooldown & Rate Limits',
      desc: 'Blocks re-buying a recently lost mint and enforces buy cooldown.',
      gate: `Cooldown ${Number(s.cooldown_min || 0)}m | lost mint blocked 30m`,
    },
    {
      icon: '15', phase: 'RUNTIME', title: 'Live Market Re-check',
      desc: 'Fresh DexScreener lookup confirms MC, liquidity, and price have not collapsed since the scan.',
      gate: 'MC still in range | liq still there | not dumping > -30%',
    },
    {
      icon: '16', phase: 'EXECUTE', title: 'Jupiter Quote \u2192 Swap',
      desc: 'Gets best route, signs transaction, sends via Helius Sender with Jito tip.',
      gate: `${Number(s.max_buy_sol || 0).toFixed(2)} SOL | risk cap ${Number(s.risk_per_trade_pct || 0).toFixed(1)}% | fee ${Number(s.priority_fee || 0).toLocaleString()} lamports`,
    },
    {
      icon: '17', phase: 'SELL', title: 'Exit Monitoring',
      desc: 'Position is tracked every loop cycle. First matching exit rule fires the sell.',
      gate: `TP1 ${Number(s.tp1_mult||0).toFixed(1)}x \u2192 TP2 ${Number(s.tp2_mult||0).toFixed(1)}x | SL ${Number(s.stop_loss||0).toFixed(2)} | Trail ${Math.round(Number(s.trail_pct||0)*100)}% | Time ${Number(s.time_stop_min||0)}m`,
    },
  ];
  const phaseColors = {
    'DISCOVER': '#6366f1', 'FILTER': '#3b82f6', 'INTEL': '#a855f7',
    'DECISION': '#f59e0b', 'BUY LOCK': '#64748b', 'RUNTIME': '#ef4444',
    'EXECUTE': '#14c784', 'SELL': '#f97316',
  };
  el.innerHTML = stages.map((st, i) => {
    const color = phaseColors[st.phase] || '#64748b';
    const isLast = i === stages.length - 1;
    return `
      <div class="checkpoint-card" style="border-left:3px solid ${color}">
        <div class="checkpoint-step">
          <div class="checkpoint-index" style="background:${color};font-size:14px">${st.icon}</div>
          <div style="min-width:0;flex:1">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:2px">
              <span style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:${color}">${st.phase}</span>
              <span style="font-size:13px;font-weight:700;color:var(--t1)">${st.title}</span>
            </div>
            <div style="font-size:12px;color:var(--t2);margin-bottom:4px">${st.desc}</div>
            <div style="font-size:11px;color:var(--t1);font-weight:600;background:rgba(255,255,255,.04);border-radius:6px;padding:4px 8px;display:inline-block">${st.gate}</div>
          </div>
        </div>
      </div>
      ${!isLast ? '<div style="text-align:center;color:var(--t3);font-size:14px;margin:-4px 0">\u25bc</div>' : ''}
    `;
  }).join('');
}
function renderSettingsVisuals(settings) {
  const s = { ...SETTINGS_DEFAULTS, ...(settings || {}) };
  renderLaunchSummary(s);
  renderSettingsSnapshot(s);
  renderTokenJourney(s);
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
    const mergedSettings = {
      ...(res.settings || payload),
      ...(res.execution_control?.control || {}),
      decision_policy: res.execution_control?.control?.policy_mode || payload.decision_policy,
      execution_mode: res.execution_control?.control?.execution_mode || payload.execution_mode,
      model_threshold: res.execution_control?.control?.model_threshold || payload.model_threshold,
    };
    applySettingsToForm(mergedSettings, res.preset || payload.preset);
    renderExecutionControlSummary(res.execution_control || { control: mergedSettings });
    setSettingsSaveState('Saved to bot settings');
    showToast('\u2713 Settings saved', true);
    showConfirmModal(
      'Settings Saved',
      `Checkpoint path updated for ${String(res.preset || payload.preset)}.`,
      [
        `Preset: ${String(res.preset || payload.preset)}`,
        `Execution: ${String(payload.execution_mode || 'live')}`,
        `Policy: ${prettyExecutionPolicy(payload.decision_policy || 'rules')}`,
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
  activateTab('settings');
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
  // Update max positions input with current setting
  const maxPos = d.settings && d.settings.max_correlated ? d.settings.max_correlated : 5;
  document.getElementById('max-positions-input').value = maxPos;
  setText('hero-sync-time', fmtClock());
  setText('scanner-sync-copy', `Synced ${fmtClock()}`);
  setText('scanner-listing-live', String(listingCatchCount));
  setText('hero-preset-badge', `${titleCase(d.preset || 'balanced')} preset`);
  document.getElementById('balance').textContent   = d.balance.toFixed(4);
  document.getElementById('pos-count').textContent = d.positions.length;
  setText('scanner-position-live', String(d.positions.length));
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
  if (running) {
    dot.className='sdot sdot-on'; txt.textContent='Bot Running'; btn.textContent='\u23f8 Stop Bot'; btn.className='btn btn-danger';
    setText('hero-live-state', 'Execution live');
  } else {
    dot.className='sdot sdot-off'; txt.textContent='Bot Stopped'; btn.textContent='\u25b6 Start Bot'; btn.className='btn btn-success';
    setText('hero-live-state', 'Bot parked');
  }

  // pos-tbl compat stub (hidden; pos-cards used instead)
  document.getElementById('pos-tbl').innerHTML = '';

  // Update scanner activity tab
  renderScannerTab(d);
  // Update balance strip from state data
  renderScanBalStrip(d);

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
    setText('scanner-filter-live', String(d.filter_log.length || 0));
    document.getElementById('filter-pipe').innerHTML = d.filter_log.map(f => `
      <div class="fp-item">
        <span class="${f.passed?'fp-pass':'fp-fail'}">${f.passed?'\u2713':'\u2717'}</span>
        <span class="fp-name">${f.name||'?'}</span>
        <span class="fp-reason">${f.reason||''}</span>
      </div>`).join('') || '<div style="font-size:11px;color:var(--t3)">Scanning\u2026</div>';
    paperProcessFilterLog(d.filter_log);
  }
  if (d.settings && Object.keys(d.settings).length) {
    const savedSettings = {
      ...d.settings,
      ...(d.execution_control?.control || {}),
      decision_policy: d.execution_control?.control?.policy_mode || d.execution_control?.policy_mode || 'rules',
      execution_mode: d.execution_control?.control?.execution_mode || d.execution_control?.execution_mode || 'live',
      preset: d.preset || 'balanced',
    };
    if (!document._settingsFocused && !_settingsDirty) {
      applySettingsToForm(savedSettings, savedSettings.preset);
      _settingsDirty = false;
      setSettingsSaveState('Loaded from saved state');
    } else {
      renderLaunchSummary(savedSettings);
      renderSettingsSnapshot(savedSettings);
    }
    renderExecutionControlSummary(d.execution_control);
  }
  const executionMode = d.execution_control?.execution_mode || d.execution_control?.control?.execution_mode || 'live';
  const selectedPolicy = d.execution_control?.selected_policy || d.execution_control?.control?.active_policy || 'rules';
  setText('hero-execution-badge', `${executionMode === 'paper' ? 'Paper' : 'Live'} · ${prettyExecutionPolicy(selectedPolicy)}`);
  if (running) {
    setText('hero-live-state', executionMode === 'paper' ? 'Paper mode' : 'Execution live');
  } else if (executionMode === 'paper') {
    setText('hero-live-state', 'Paper lane armed');
  }
  renderTelegramStatus(d.telegram_chat_id || '');
}

async function toggleBot() {
  if (!running && _settingsDirty) { await saveSettings(); }
  const res = await fetch(running ? '/api/stop' : '/api/start', {method:'POST'}).then(r=>r.json()).catch(()=>null);
  if (!res) { document.getElementById('stxt').textContent = '\u26a0\ufe0f Server error'; return; }
  if (!res.ok && res.msg) { document.getElementById('stxt').textContent = '\u26a0\ufe0f ' + res.msg; document.getElementById('stxt').style.color='#f23645'; return; }
  document.getElementById('stxt').style.color = '';
  setTimeout(refresh, 800);
}
async function applyShadowTrading() {
  if (!confirm('Apply optimal shadow trading settings (balanced preset)? Bot will restart with new settings.')) return;
  const res = await fetch('/api/shadow-trading-mode', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'}
  }).then(r=>r.json()).catch(()=>null);
  if (res && res.ok) {
    showToast(res.msg, true);
    setTimeout(refresh, 500);
  } else {
    showToast(res?.msg || 'Failed to apply shadow trading mode', false);
  }
}
async function updateMaxPositions() {
  const input = document.getElementById('max-positions-input');
  const val = parseInt(input.value) || 5;
  const clamped = Math.max(1, Math.min(20, val));
  input.value = clamped;
  const res = await fetch('/api/set-max-positions', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({max_positions: clamped})
  }).then(r=>r.json()).catch(()=>null);
  if (res && res.ok) {
    showToast(`Max open buys set to ${clamped}`, true);
  } else {
    showToast('Failed to update max positions', false);
  }
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

function renderTelegramStatus(chatId) {
  const input = document.getElementById('telegram-chat-id');
  const badge = document.getElementById('telegram-status-badge');
  if (!input || !badge) return;
  const active = document.activeElement === input;
  if (!active) input.value = chatId || '';
  if (chatId) {
    badge.className = 'badge bg-grn';
    badge.textContent = `Connected ${chatId}`;
  } else {
    badge.className = 'badge bg-muted';
    badge.textContent = 'Not connected';
  }
}

async function saveTelegramChat() {
  const input = document.getElementById('telegram-chat-id');
  const raw = (input?.value || '').trim();
  if (!raw) {
    showToast('Enter a Telegram chat ID first', false);
    return;
  }
  if (!/^-?[0-9]+$/.test(raw)) {
    showToast('Chat ID must be numeric', false);
    return;
  }
  const res = await fetch('/api/telegram', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_id: raw }),
  }).then(r => r.json()).catch(() => null);
  if (!res || !res.ok) {
    showToast('Telegram save failed', false);
    return;
  }
  renderTelegramStatus(raw);
  showToast('Telegram connected. Check for the confirmation message.');
  refresh();
}

async function clearTelegramChat() {
  const res = await fetch('/api/telegram', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_id: '' }),
  }).then(r => r.json()).catch(() => null);
  if (!res || !res.ok) {
    showToast('Telegram clear failed', false);
    return;
  }
  renderTelegramStatus('');
  showToast('Telegram alerts disconnected');
  refresh();
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
  const edgeGuard = _opsMetrics.edge_guard || {};
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
      <div style="padding:8px 10px;border:1px solid var(--b1);border-radius:8px">
        <div style="font-size:10px;color:var(--t3)">Edge Guard</div>
        <div style="font-size:15px;font-weight:800;color:var(--t1)">${edgeGuard.action_label||edgeGuard.status||'Normal risk'}</div>
        <div style="font-size:10px;color:var(--t3)">size ${Math.round((edgeGuard.size_multiplier ?? 1)*100)}%</div>
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
      ${links.slice(0,4).map(link => `<a class="badge bg-muted" href="${link}" target="_blank" rel="noreferrer">${String(link || '').replace('https://','').replace('http://','').slice(0,28)}</a>`).join('') || '<span style="font-size:11px;color:var(--t3)">No socials found</span>'}
    </div>
    <div class="sec-label">AI Score Breakdown (${sc.total||0}/100)</div>
    <canvas id="sig-radar" height="200" style="margin-bottom:14px"></canvas>
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

// ══════════════════════════ QUANT REPLAY LAB ══════════════════════════
async function pollQuant() {
  try {
    const days = parseInt(document.getElementById('quant-days')?.value || '7', 10);
    const [overview, optimizer, model, comparison, reports, runs] = await Promise.all([
      fetch('/api/quant/overview').then(r => r.json()).catch(() => null),
      fetch('/api/quant/optimizer?days=' + days).then(r => r.json()).catch(() => null),
      fetch('/api/quant/model?days=' + days + '&mode=' + encodeURIComponent(_quantModelMode)).then(r => r.json()).catch(() => null),
      fetch('/api/quant/comparison?days=' + days).then(r => r.json()).catch(() => null),
      fetch('/api/quant/reports?days=' + days).then(r => r.json()).catch(() => null),
      fetch('/api/quant/backtests').then(r => r.json()).catch(() => []),
    ]);
    if (overview) _quantOverview = overview;
    if (optimizer) _quantOptimizer = optimizer;
    if (model) _quantModel = model;
    if (comparison) _quantComparison = comparison;
    if (reports) _quantReports = reports;
    _quantRuns = Array.isArray(runs) ? runs : [];
    renderQuantOverview();
    renderQuantRuns();
    renderQuantOptimizer();
    renderQuantModel();
    renderQuantComparison();
    renderQuantReports();
    if (_quantSelectedRunId) {
      await showQuantRunDetail(_quantSelectedRunId, true);
    }
  } catch (e) {}
}

function setQuantModelMode(mode) {
  _quantModelMode = mode || 'auto';
  pollQuant();
}

function renderQuantOverview() {
  const grid = document.getElementById('quant-overview-grid');
  if (!grid || !_quantOverview) return;
  const dataset = _quantOverview.dataset || {};
  const strategies = _quantOverview.strategy_summaries || [];
  const flow = _quantOverview.flow_regime || {};
  const opportunity = _quantOverview.opportunity_map || {};
  const best = strategies[0] || {};
  grid.innerHTML = `
    <div class="scanner-summary-card"><div class="scanner-summary-label">Tracked Tokens</div><div class="scanner-summary-value">${dataset.tracked_tokens || 0}</div><div class="scanner-summary-copy">Observed in persistent market storage</div></div>
    <div class="scanner-summary-card"><div class="scanner-summary-label">Market Events</div><div class="scanner-summary-value">${dataset.market_events || 0}</div><div class="scanner-summary-copy">Append-only event log</div></div>
    <div class="scanner-summary-card"><div class="scanner-summary-label">Wallet Flow Events</div><div class="scanner-summary-value">${dataset.wallet_flow_events || 0}</div><div class="scanner-summary-copy">Raw wallet-side swap legs</div></div>
    <div class="scanner-summary-card"><div class="scanner-summary-label">Liquidity Deltas</div><div class="scanner-summary-value">${dataset.liquidity_delta_events || 0}</div><div class="scanner-summary-copy">First-class liquidity change records</div></div>
    <div class="scanner-summary-card"><div class="scanner-summary-label">Feature Snapshots</div><div class="scanner-summary-value">${dataset.feature_snapshots || 0}</div><div class="scanner-summary-copy">Replay-ready scoring inputs</div></div>
    <div class="scanner-summary-card"><div class="scanner-summary-label">Flow Snapshots</div><div class="scanner-summary-value">${dataset.flow_snapshots || 0}</div><div class="scanner-summary-copy">Wallet pressure and liquidity tape</div></div>
    <div class="scanner-summary-card"><div class="scanner-summary-label">Shadow Decisions</div><div class="scanner-summary-value">${dataset.shadow_decisions || 0}</div><div class="scanner-summary-copy">Would-buy versus blocked decisions</div></div>
    <div class="scanner-summary-card"><div class="scanner-summary-label">Model Decisions</div><div class="scanner-summary-value">${dataset.model_decisions || 0}</div><div class="scanner-summary-copy">Logged global and regime model picks</div></div>
    <div class="scanner-summary-card"><div class="scanner-summary-label">Edge Reports</div><div class="scanner-summary-value">${dataset.edge_reports || 0}</div><div class="scanner-summary-copy">Persistent weekly and monthly scorecards</div></div>
    <div class="scanner-summary-card"><div class="scanner-summary-label">Flow Regime</div><div class="scanner-summary-value">${flow.regime || '—'}</div><div class="scanner-summary-copy">${flow.sample_size || 0} recent snapshots · ${flow.avg_net_flow_sol > 0 ? '+' : ''}${flow.avg_net_flow_sol || 0} SOL net</div></div>
    <div class="scanner-summary-card"><div class="scanner-summary-label">Missed Winners</div><div class="scanner-summary-value">${(opportunity.totals || {}).missed_winners || 0}</div><div class="scanner-summary-copy">Shadow blocks that later ran ${opportunity.winner_threshold_pct || 120}%+</div></div>
    <div class="scanner-summary-card"><div class="scanner-summary-label">Open Shadow Positions</div><div class="scanner-summary-value">${dataset.open_shadow_positions || 0}</div><div class="scanner-summary-copy">Currently simulated live book</div></div>
    <div class="scanner-summary-card"><div class="scanner-summary-label">Best Replay Avg</div><div class="scanner-summary-value">${best.strategy_name ? best.strategy_name + ' ' + (best.avg_pnl_pct > 0 ? '+' : '') + best.avg_pnl_pct + '%' : '—'}</div><div class="scanner-summary-copy">Current top strategy from completed replays</div></div>
  `;
  renderQuantFlowRegime();
  renderQuantOpportunityMap();
}

function renderQuantFlowRegime() {
  const box = document.getElementById('quant-flow-regime');
  if (!box || !_quantOverview) return;
  const flow = _quantOverview.flow_regime || {};
  const recent = Array.isArray(_quantOverview.recent_flow) ? _quantOverview.recent_flow : [];
  const executionTape = Array.isArray(_quantOverview.recent_execution_tape) ? _quantOverview.recent_execution_tape : [];
  box.innerHTML = `
    <div class="panel-head" style="margin-bottom:12px">
      <div>
        <div class="panel-title">Flow Regime</div>
        <div class="panel-copy">Recent wallet participation, smart-money presence, and liquidity health across the live capture tape.</div>
      </div>
      <span class="badge ${flow.regime === 'accumulation' ? 'bg-grn' : flow.regime === 'defensive' || flow.regime === 'distribution' ? 'bg-red' : 'bg-muted'}">${flow.regime || 'neutral'}</span>
    </div>
    <div class="shortcut-row" style="margin-bottom:12px">
      <span class="badge bg-muted">Buy/Sell ${flow.avg_buy_sell_ratio || 0}</span>
      <span class="badge bg-muted">Net ${flow.avg_net_flow_sol > 0 ? '+' : ''}${flow.avg_net_flow_sol || 0} SOL</span>
      <span class="badge bg-muted">Smart ${flow.avg_smart_wallet_buys || 0}</span>
      <span class="badge bg-muted">Risk ${flow.high_risk_share_pct || 0}%</span>
    </div>
    <div class="operator-rule-list" style="margin-bottom:12px">
      <div class="operator-rule"><div><div class="operator-rule-label">Liquidity Drain Share</div><div style="font-size:10px;color:var(--t3);margin-top:3px">Tokens with 35%+ liquidity loss</div></div><div class="operator-rule-value">${flow.liquidity_drain_share_pct || 0}%</div></div>
      <div class="operator-rule"><div><div class="operator-rule-label">Exit Blocked Share</div><div style="font-size:10px;color:var(--t3);margin-top:3px">Tokens with no reliable exit route</div></div><div class="operator-rule-value">${flow.exit_blocked_share_pct || 0}%</div></div>
      <div class="operator-rule"><div><div class="operator-rule-label">Avg Unique Buyers</div><div style="font-size:10px;color:var(--t3);margin-top:3px">Breadth in the recent sample</div></div><div class="operator-rule-value">${flow.avg_unique_buyers || 0}</div></div>
    </div>
    <div class="sec-label">Recent Tape</div>
    <div style="display:flex;flex-direction:column;gap:8px">
      ${recent.slice(0, 5).map(item => `
        <div style="padding:10px 12px;border:1px solid rgba(255,255,255,.06);border-radius:12px;background:rgba(255,255,255,.02)">
          <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">
            <div style="font-size:11px;font-weight:700;color:var(--t1)">${item.mint ? item.mint.slice(0, 6) + '...' + item.mint.slice(-4) : 'Unknown'}</div>
            <div style="font-size:11px;font-weight:700;color:${item.net_flow_sol >= 0 ? 'var(--grn)' : 'var(--red2)'}">${item.net_flow_sol >= 0 ? '+' : ''}${(item.net_flow_sol || 0).toFixed(2)} SOL</div>
          </div>
          <div class="shortcut-row" style="margin-top:8px">
            <span class="badge bg-muted">${item.unique_buyer_count || 0} buyers</span>
            <span class="badge bg-muted">${item.unique_seller_count || 0} sellers</span>
            <span class="badge bg-muted">Smart ${item.smart_wallet_buys || 0}</span>
            <span class="badge ${item.can_exit === false ? 'bg-red' : 'bg-muted'}">${item.can_exit === false ? 'Exit risk' : 'Exit ok'}</span>
          </div>
        </div>
      `).join('') || '<div style="font-size:11px;color:var(--t3)">No flow samples captured yet.</div>'}
    </div>
    <div class="sec-label" style="margin-top:12px">Latest Wallet Flow</div>
    <div style="display:flex;flex-direction:column;gap:8px">
      ${executionTape.slice(0, 4).map(item => `
        <div style="padding:10px 12px;border:1px solid rgba(255,255,255,.06);border-radius:12px;background:rgba(255,255,255,.02)">
          <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">
            <div>
              <div style="font-size:11px;font-weight:700;color:var(--t1)">${item.mint ? item.mint.slice(0, 6) + '...' + item.mint.slice(-4) : 'Unknown'}</div>
              <div style="font-size:10px;color:var(--t3);margin-top:3px">${item.side} · ${item.wallet ? item.wallet.slice(0, 6) + '...' + item.wallet.slice(-4) : 'wallet'}</div>
            </div>
            <div class="shortcut-row">
              <span class="badge ${item.side === 'buy' ? 'bg-grn' : 'bg-red'}">${item.side}</span>
              ${item.smart_wallet ? '<span class="badge bg-gold">Smart</span>' : ''}
            </div>
          </div>
          <div class="shortcut-row" style="margin-top:8px">
            <span class="badge bg-muted">${(item.sol_amount || 0).toFixed(2)} SOL</span>
            <span class="badge bg-muted">${(item.token_amount || 0).toFixed(2)} tokens</span>
            <span class="badge bg-muted">${item.source || 'helius'}</span>
          </div>
        </div>
      `).join('') || '<div style="font-size:11px;color:var(--t3)">Execution tape is still warming up.</div>'}
    </div>
  `;
}

function renderQuantOpportunityMap() {
  const box = document.getElementById('quant-opportunity-map');
  if (!box || !_quantOverview) return;
  const opportunity = _quantOverview.opportunity_map || {};
  const totals = opportunity.totals || {};
  const blockers = Array.isArray(opportunity.top_blockers) ? opportunity.top_blockers : [];
  const samples = opportunity.samples || {};
  const allSamples = [];
  // Build dots for the X/Y map from all categories
  (samples.captured_winners || []).forEach(s => allSamples.push({...s, category: 'captured_winner'}));
  (samples.missed_winners || []).forEach(s => allSamples.push({...s, category: 'missed_winner'}));
  (samples.false_positives || []).forEach(s => allSamples.push({...s, category: 'false_positive'}));
  (samples.avoided_losers || []).forEach(s => allSamples.push({...s, category: 'avoided_loser'}));

  // Build scatter dots — X = score (0-100), Y = peak_return_pct (-100 to 500+)
  const maxReturn = Math.max(200, ...allSamples.map(s => Math.abs(s.peak_return_pct || 0)));
  const dotColors = {
    captured_winner: '#14c784',
    missed_winner: '#f59e0b',
    false_positive: '#ef4444',
    avoided_loser: '#3b82f6',
  };
  const dotLabels = {
    captured_winner: 'Captured Winner',
    missed_winner: 'Missed Winner',
    false_positive: 'False Positive',
    avoided_loser: 'Avoided Loser',
  };
  const dots = allSamples.slice(0, 80).map(s => {
    const score = Math.min(100, Math.max(0, s.score || s.composite_score || 50));
    const ret = s.peak_return_pct || 0;
    const xPct = (score / 100) * 90 + 5; // 5%-95%
    const yPct = 50 - (ret / maxReturn) * 45; // Center at 50%, winners above, losers below
    const clampY = Math.max(3, Math.min(97, yPct));
    return '<div class="opp-xy-dot" style="left:' + xPct + '%;top:' + clampY + '%;background:' + (dotColors[s.category] || '#666') + '" title="' + (s.name || s.mint || '') + ' | ' + (dotLabels[s.category] || '') + ' | Score: ' + score + ' | Return: ' + (ret > 0 ? '+' : '') + ret.toFixed(1) + '%"></div>';
  }).join('');

  box.innerHTML = `
    <div class="panel-head" style="margin-bottom:12px">
      <div>
        <div class="panel-title">Opportunity Map</div>
        <div class="panel-copy">X/Y scatter of signal score vs realized return. Green = captured winners, yellow = missed, red = false positives, blue = avoided losers.</div>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <span class="badge bg-muted">Winner threshold ${opportunity.winner_threshold_pct || 120}%</span>
        <button class="btn btn-ghost" type="button" onclick="toggleOpportunitySettings()" style="font-size:11px">Settings</button>
      </div>
    </div>
    <div class="opp-xy-map">
      <div class="opp-xy-crosshair-h"></div>
      <div class="opp-xy-crosshair-v"></div>
      <div class="opp-xy-quadrant" style="top:8px;right:12px;color:#14c784">Winners + High Score</div>
      <div class="opp-xy-quadrant" style="top:8px;left:12px;color:#f59e0b">Winners + Low Score (Missed)</div>
      <div class="opp-xy-quadrant" style="bottom:8px;right:12px;color:#ef4444">Losers + High Score (False Pos)</div>
      <div class="opp-xy-quadrant" style="bottom:8px;left:12px;color:#3b82f6">Losers + Low Score (Avoided)</div>
      <div class="opp-xy-axis-x">Signal Score</div>
      <div class="opp-xy-axis-y">Return %</div>
      ${dots || '<div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:11px;color:var(--t3)">Waiting for data...</div>'}
    </div>
    <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px">
      <div style="display:flex;align-items:center;gap:5px"><div style="width:10px;height:10px;border-radius:50%;background:#14c784"></div><span style="font-size:10px;color:var(--t2)">Captured Winners</span></div>
      <div style="display:flex;align-items:center;gap:5px"><div style="width:10px;height:10px;border-radius:50%;background:#f59e0b"></div><span style="font-size:10px;color:var(--t2)">Missed Winners</span></div>
      <div style="display:flex;align-items:center;gap:5px"><div style="width:10px;height:10px;border-radius:50%;background:#ef4444"></div><span style="font-size:10px;color:var(--t2)">False Positives</span></div>
      <div style="display:flex;align-items:center;gap:5px"><div style="width:10px;height:10px;border-radius:50%;background:#3b82f6"></div><span style="font-size:10px;color:var(--t2)">Avoided Losers</span></div>
    </div>
    <div class="control-action-grid" style="grid-template-columns:repeat(2,minmax(0,1fr));margin-bottom:12px">
      <div class="scanner-summary-card"><div class="scanner-summary-label">Captured Winners</div><div class="scanner-summary-value">${totals.captured_winners || 0}</div><div class="scanner-summary-copy">Passed and later ran</div></div>
      <div class="scanner-summary-card"><div class="scanner-summary-label">False Positives</div><div class="scanner-summary-value">${totals.false_positives || 0}</div><div class="scanner-summary-copy">Passed and later broke down</div></div>
      <div class="scanner-summary-card"><div class="scanner-summary-label">Missed Winners</div><div class="scanner-summary-value">${totals.missed_winners || 0}</div><div class="scanner-summary-copy">Blocked before major expansion</div></div>
      <div class="scanner-summary-card"><div class="scanner-summary-label">Avoided Losers</div><div class="scanner-summary-value">${totals.avoided_losers || 0}</div><div class="scanner-summary-copy">Blocked before collapse</div></div>
    </div>
    <div class="sec-label">Top Blockers On Missed Winners</div>
    <div class="shortcut-row" style="margin-bottom:12px">
      ${blockers.map(item => `<span class="badge bg-muted">${item.reason.replaceAll('_', ' ')} · ${item.count}</span>`).join('') || '<span class="badge bg-muted">No blocker data yet</span>'}
    </div>
    <div class="sec-label">Missed Winners Sample</div>
    <div style="display:flex;flex-direction:column;gap:8px">
      ${(samples.missed_winners || []).slice(0, 4).map(item => `
        <div style="padding:10px 12px;border:1px solid rgba(255,255,255,.06);border-radius:12px;background:rgba(255,255,255,.02)">
          <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">
            <div>
              <div style="font-size:12px;font-weight:800;color:var(--t1)">${item.name || item.mint}</div>
              <div style="font-size:10px;color:var(--t3);margin-top:3px">${item.strategy_name} · ${(item.blocker_reasons || []).join(', ') || 'blocked'}</div>
            </div>
            <div style="font-size:12px;font-weight:800;color:var(--grn)">+${(item.peak_return_pct || 0).toFixed(1)}%</div>
          </div>
        </div>
      `).join('') || '<div style="font-size:11px;color:var(--t3)">No missed-winner sample yet.</div>'}
    </div>
    <!-- Opportunity Settings Panel -->
    <div class="opp-settings-panel" id="opp-settings-panel" style="display:none">
      <h4>Opportunity Hunter Settings</h4>
      <div style="font-size:11px;color:var(--t2);margin-bottom:12px">Peak Plateau Rider — holds positions until momentum dies. No fixed TP2 cap. Progressive trailing stop tightens as gains grow, locking in profit at the peak without selling early on moonshots.</div>
      <div class="field-row" style="margin-bottom:12px">
        <div class="fgroup">
          <label class="flabel">Aggression Level</label>
          <select class="finput" id="opp-aggression">
            <option value="aggressive">Aggressive — Bigger positions, strong volume gates</option>
            <option value="degen">Degen — Max exposure, widest trail for mega runners</option>
          </select>
        </div>
      </div>
      <div style="font-size:10px;color:var(--t3);margin-bottom:12px;line-height:1.6">
        <strong>How it works:</strong><br>
        · Skims only 25% at 3x to lock some profit — keeps 75% riding<br>
        · TP2 disabled — no ceiling on gains, trailing stop is the only exit<br>
        · Progressive trailing tightens as gains grow: 30% early → 25% at 3x → 20% at 5x → 15% at 10x → 12% at 20x → 10% at 50x+<br>
        · 6-hour time stop (only if position is flat/losing)<br>
        · Hot runner ceiling raised to 5000% — won't block massive pumps<br>
        · Max MC $800K — wide range for moonshot potential<br>
        <strong>Applied on:</strong> Next bot restart. Saved as Custom preset.
      </div>
      <div class="panel-actions">
        <button class="btn btn-primary" type="button" onclick="applyOpportunitySettings()">Apply Opportunity Settings</button>
        <button class="btn btn-ghost" type="button" onclick="toggleOpportunitySettings()">Cancel</button>
      </div>
    </div>
  `;
}

function renderQuantOptimizer() {
  const box = document.getElementById('quant-optimizer');
  if (!box || !_quantOptimizer) return;
  const dataset = _quantOptimizer.dataset || {};
  const bestThresholds = Array.isArray(_quantOptimizer.best_thresholds) ? _quantOptimizer.best_thresholds : [];
  const featureEdges = Array.isArray(_quantOptimizer.feature_edges) ? _quantOptimizer.feature_edges : [];
  const topOutcomes = Array.isArray(_quantOptimizer.top_outcomes) ? _quantOptimizer.top_outcomes : [];
  const counts = dataset.label_counts || {};
  box.innerHTML = `
    <div class="panel-head" style="margin-bottom:12px">
      <div>
        <div class="panel-title">Optimizer Sweep</div>
        <div class="panel-copy">Uses first captured decision snapshots and realized token paths to rank thresholds that increase winner density while suppressing rugs.</div>
      </div>
      <span class="badge bg-muted">${_quantOptimizer.window_days || 7}d window</span>
    </div>
    <div class="shortcut-row" style="margin-bottom:12px">
      <span class="badge bg-muted">${dataset.entry_snapshots || 0} entry snapshots</span>
      <span class="badge bg-muted">${dataset.labeled_tokens || 0} labeled tokens</span>
      <span class="badge bg-grn">${counts.winner || 0} winners</span>
      <span class="badge bg-red">${counts.rug || 0} rugs</span>
      <span class="badge bg-blue">${counts.volatile_winner || 0} volatile winners</span>
    </div>
    <div class="scanner-top-grid">
      <div>
        <div class="sec-label">Best Thresholds</div>
        <div class="operator-rule-list">
          ${bestThresholds.map(item => `
            <div class="operator-rule">
              <div>
                <div class="operator-rule-label">${item.feature.replaceAll('_', ' ')}</div>
                <div style="font-size:10px;color:var(--t3);margin-top:3px">${item.selected} tokens · ${item.winner_rate_pct}% winners · ${item.rug_rate_pct}% rugs</div>
              </div>
              <div class="operator-rule-value">>= ${Number(item.threshold).toFixed(item.threshold % 1 ? 2 : 0)}</div>
            </div>
          `).join('') || '<div style="font-size:11px;color:var(--t3)">Optimizer data not ready yet.</div>'}
        </div>
      </div>
      <div>
        <div class="sec-label">Feature Edge</div>
        <div class="shortcut-row" style="margin-bottom:10px">
          ${featureEdges.map(item => `<span class="badge bg-muted">${item.label} ${item.edge > 0 ? '+' : ''}${item.edge}</span>`).join('') || '<span class="badge bg-muted">No feature edge yet</span>'}
        </div>
        <div class="sec-label">Top Outcomes</div>
        <div style="display:flex;flex-direction:column;gap:8px">
          ${topOutcomes.slice(0, 4).map(item => `
            <div style="padding:10px 12px;border:1px solid rgba(255,255,255,.06);border-radius:12px;background:rgba(255,255,255,.02)">
              <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">
                <div>
                  <div style="font-size:12px;font-weight:800;color:var(--t1)">${item.name || item.mint}</div>
                  <div style="font-size:10px;color:var(--t3);margin-top:3px">${item.label.replaceAll('_', ' ')}</div>
                </div>
                <div style="font-size:12px;font-weight:800;color:${item.label === 'rug' ? 'var(--red2)' : 'var(--grn)'}">${item.peak_return_pct >= 0 ? '+' : ''}${item.peak_return_pct.toFixed(1)}%</div>
              </div>
            </div>
          `).join('') || '<div style="font-size:11px;color:var(--t3)">Outcome labels are still warming up.</div>'}
        </div>
      </div>
    </div>
  `;
}

function renderQuantModel() {
  const box = document.getElementById('quant-model');
  if (!box || !_quantModel) return;
  const family = _quantModel.model_family || {};
  const globalModel = family.global || {};
  const regimeModels = family.regimes || {};
  const selection = _quantModel.selection || {};
  const regimeContext = _quantModel.regime_context || {};
  const ranked = Array.isArray(_quantModel.ranked_candidates) ? _quantModel.ranked_candidates : [];
  const selectedModel = selection.selected_model || globalModel || {};
  const weights = Array.isArray(selectedModel.weights) ? selectedModel.weights : [];
  const selectedKey = selection.selected_key || 'global';
  const activeRegime = selection.active_regime || regimeContext.regime || 'neutral';
  const selectedLabel = selectedKey === 'global' ? 'global' : selectedKey;
  const recentDecisions = Array.isArray(_quantModel.recent_decisions) ? _quantModel.recent_decisions : [];
  box.innerHTML = `
    <div class="panel-head" style="margin-bottom:12px">
      <div>
        <div class="panel-title">Regime Model Desk</div>
        <div class="panel-copy">Auto-selects a learned ranking model from the current tape regime and falls back to the global model when a regime-specific sample is too thin.</div>
      </div>
      <div class="shortcut-row">
        <select class="finput" style="max-width:190px;height:36px" onchange="setQuantModelMode(this.value)">
          <option value="auto" ${_quantModelMode === 'auto' ? 'selected' : ''}>Auto (Recommended)</option>
          <option value="global" ${_quantModelMode === 'global' ? 'selected' : ''}>Global</option>
          <option value="accumulation" ${_quantModelMode === 'accumulation' ? 'selected' : ''}>Accumulation</option>
          <option value="distribution" ${_quantModelMode === 'distribution' ? 'selected' : ''}>Distribution</option>
          <option value="defensive" ${_quantModelMode === 'defensive' ? 'selected' : ''}>Defensive</option>
          <option value="neutral" ${_quantModelMode === 'neutral' ? 'selected' : ''}>Neutral</option>
        </select>
        <span class="badge ${selectedModel.trained ? 'bg-grn' : 'bg-red'}">${selectedModel.trained ? `${selectedLabel} active` : 'untrained'}</span>
      </div>
    </div>
    <div class="shortcut-row" style="margin-bottom:12px">
      <span class="badge bg-muted">${_quantModel.window_days || 7}d train window</span>
      <span class="badge bg-muted">Active regime ${activeRegime}</span>
      <span class="badge bg-muted">${globalModel.winner_count || 0} global winners</span>
      <span class="badge bg-muted">${globalModel.rug_count || 0} global rugs</span>
      <span class="badge bg-blue">${selectedModel.accuracy_pct || 0}% ${selectedLabel} accuracy</span>
      ${selection.using_global_fallback ? `<span class="badge bg-gold">Fallback ${selection.fallback_reason || ''}</span>` : ''}
    </div>
    <div class="scanner-top-grid">
      <div>
        <div class="sec-label">Model Family</div>
        <div class="operator-rule-list" style="margin-bottom:12px">
          <div class="operator-rule">
            <div>
              <div class="operator-rule-label">global</div>
              <div style="font-size:10px;color:var(--t3);margin-top:3px">${globalModel.token_count || 0} tokens · ${globalModel.winner_count || 0} winners · ${globalModel.rug_count || 0} rugs</div>
            </div>
            <div class="operator-rule-value">${globalModel.trained ? (globalModel.accuracy_pct || 0) + '%' : 'warmup'}</div>
          </div>
          ${Object.entries(regimeModels).map(([name, row]) => `
            <div class="operator-rule">
              <div>
                <div class="operator-rule-label">${name}</div>
                <div style="font-size:10px;color:var(--t3);margin-top:3px">${row.token_count || 0} tokens · ${row.winner_count || 0} winners · ${row.rug_count || 0} rugs</div>
              </div>
              <div class="operator-rule-value">${row.trained ? (row.accuracy_pct || 0) + '%' : 'warmup'}</div>
            </div>
          `).join('')}
        </div>
        <div class="sec-label">Selected Model Weights</div>
        <div class="operator-rule-list">
          ${weights.slice(0, 6).map(item => `
            <div class="operator-rule">
              <div>
                <div class="operator-rule-label">${item.label}</div>
                <div style="font-size:10px;color:var(--t3);margin-top:3px">Winner ${item.winner_mean} · Rug ${item.rug_mean}</div>
              </div>
              <div class="operator-rule-value">${item.weight > 0 ? '+' : ''}${item.weight}</div>
            </div>
          `).join('') || '<div style="font-size:11px;color:var(--t3)">Model weights unavailable.</div>'}
        </div>
      </div>
      <div>
        <div class="sec-label">Ranked Candidates</div>
        <div style="display:flex;flex-direction:column;gap:8px">
          ${ranked.slice(0, 5).map(item => `
            <div style="padding:10px 12px;border:1px solid rgba(255,255,255,.06);border-radius:12px;background:rgba(255,255,255,.02)">
              <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">
                <div>
                  <div style="font-size:12px;font-weight:800;color:var(--t1)">${item.name || item.mint}</div>
                  <div style="font-size:10px;color:var(--t3);margin-top:3px">${item.mint || ''}</div>
                </div>
                <div style="font-size:13px;font-weight:800;color:${item.model_score >= 60 ? 'var(--grn)' : item.model_score >= 45 ? 'var(--gold)' : 'var(--red2)'}">${item.model_score.toFixed(1)}</div>
              </div>
              <div class="shortcut-row" style="margin-top:8px">
                <span class="badge bg-muted">Comp ${item.features?.composite_score ?? 0}</span>
                <span class="badge bg-muted">Conf ${Math.round((item.features?.confidence || 0) * 100)}%</span>
                <span class="badge bg-muted">B/S ${item.features?.buy_sell_ratio ?? 0}</span>
                <span class="badge bg-muted">Net ${item.features?.net_flow_sol ?? 0} SOL</span>
                <span class="badge bg-muted">${item.model_key || selectedKey}</span>
              </div>
              <div class="shortcut-row" style="margin-top:8px">
                ${(item.top_drivers || []).slice(0, 3).map(driver => `<span class="badge bg-muted">${driver.label} ${driver.contribution > 0 ? '+' : ''}${driver.contribution}</span>`).join('')}
              </div>
            </div>
          `).join('') || '<div style="font-size:11px;color:var(--t3)">Not enough trained candidates yet.</div>'}
        </div>
        <div class="sec-label" style="margin-top:12px">Recent Live Model Picks</div>
        <div style="display:flex;flex-direction:column;gap:8px">
          ${recentDecisions.slice(0, 4).map(item => `
            <div style="padding:10px 12px;border:1px solid rgba(255,255,255,.06);border-radius:12px;background:rgba(255,255,255,.02)">
              <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">
                <div>
                  <div style="font-size:12px;font-weight:800;color:var(--t1)">${item.name || item.mint}</div>
                  <div style="font-size:10px;color:var(--t3);margin-top:3px">${item.mode} · ${item.model_key} · ${item.active_regime || 'neutral'}</div>
                </div>
                <div style="font-size:12px;font-weight:800;color:${item.model_score >= item.threshold ? 'var(--grn)' : 'var(--red2)'}">${item.model_score.toFixed(1)}</div>
              </div>
              <div class="shortcut-row" style="margin-top:8px">
                <span class="badge ${item.passed ? 'bg-grn' : 'bg-red'}">${item.passed ? 'pass' : 'block'}</span>
                ${(item.drivers || []).slice(0, 2).map(driver => `<span class="badge bg-muted">${driver.label} ${driver.contribution > 0 ? '+' : ''}${driver.contribution}</span>`).join('')}
              </div>
            </div>
          `).join('') || '<div style="font-size:11px;color:var(--t3)">No live model picks logged yet.</div>'}
        </div>
      </div>
    </div>
  `;
}

function renderQuantComparison() {
  const box = document.getElementById('quant-comparison');
  if (!box || !_quantComparison) return;
  const summary = _quantComparison.summary || {};
  const policies = Array.isArray(summary.policies) ? summary.policies : [];
  const topTrades = Array.isArray(_quantComparison.top_trades) ? _quantComparison.top_trades : [];
  box.innerHTML = `
    <div class="panel-head" style="margin-bottom:12px">
      <div>
        <div class="panel-title">Rules vs Models</div>
        <div class="panel-copy">Compares the same captured entry snapshots under the balanced rule set, the global model, and the regime-auto model using identical exit assumptions.</div>
      </div>
      <span class="badge bg-muted">${_quantComparison.window_days || 7}d · threshold ${_quantComparison.model_threshold || 60}</span>
    </div>
    <div class="control-action-grid" style="grid-template-columns:repeat(3,minmax(0,1fr));margin-bottom:12px">
      ${policies.map(item => `
        <div class="scanner-summary-card">
          <div class="scanner-summary-label">${item.policy_name.replaceAll('_', ' ')}</div>
          <div class="scanner-summary-value">${item.avg_pnl_pct > 0 ? '+' : ''}${item.avg_pnl_pct || 0}%</div>
          <div class="scanner-summary-copy">${item.closed_trades || 0} trades · ${item.win_rate || 0}% win rate</div>
        </div>
      `).join('') || '<div class="scanner-summary-card"><div class="scanner-summary-label">Comparison</div><div class="scanner-summary-value">—</div><div class="scanner-summary-copy">No comparison data yet</div></div>'}
    </div>
    <div class="scanner-top-grid">
      <div>
        <div class="sec-label">Policy Summary</div>
        <div class="operator-rule-list">
          ${policies.map(item => `
            <div class="operator-rule">
              <div>
                <div class="operator-rule-label">${item.policy_name.replaceAll('_', ' ')}</div>
                <div style="font-size:10px;color:var(--t3);margin-top:3px">${item.decisions || 0} decisions · ${item.passed || 0} entries · ${item.closed_trades || 0} closed</div>
              </div>
              <div class="operator-rule-value">${item.avg_pnl_pct > 0 ? '+' : ''}${item.avg_pnl_pct || 0}%</div>
            </div>
          `).join('') || '<div style="font-size:11px;color:var(--t3)">Waiting for comparison data.</div>'}
        </div>
      </div>
      <div>
        <div class="sec-label">Top Comparison Trades</div>
        <div style="display:flex;flex-direction:column;gap:8px">
          ${topTrades.slice(0, 5).map(item => `
            <div style="padding:10px 12px;border:1px solid rgba(255,255,255,.06);border-radius:12px;background:rgba(255,255,255,.02)">
              <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">
                <div>
                  <div style="font-size:12px;font-weight:800;color:var(--t1)">${item.name || item.mint}</div>
                  <div style="font-size:10px;color:var(--t3);margin-top:3px">${item.policy_name.replaceAll('_', ' ')} · ${item.exit_reason || 'exit'}</div>
                </div>
                <div style="font-size:12px;font-weight:800;color:${item.realized_pnl_pct >= 0 ? 'var(--grn)' : 'var(--red2)'}">${item.realized_pnl_pct >= 0 ? '+' : ''}${item.realized_pnl_pct.toFixed(1)}%</div>
              </div>
              <div class="shortcut-row" style="margin-top:8px">
                <span class="badge bg-muted">Up ${item.max_upside_pct.toFixed(1)}%</span>
                <span class="badge bg-muted">DD ${item.max_drawdown_pct.toFixed(1)}%</span>
              </div>
            </div>
          `).join('') || '<div style="font-size:11px;color:var(--t3)">No comparison trades yet.</div>'}
        </div>
      </div>
    </div>
  `;
}

async function runQuantReport() {
  const days = parseInt(document.getElementById('quant-days')?.value || '7', 10);
  const res = await fetch('/api/quant/reports', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ days, threshold: _quantComparison?.model_threshold || 60 }),
  }).then(r => r.json()).catch(() => null);
  if (!res || !res.ok) {
    showToast('Edge report generation failed', false);
    return;
  }
  showToast(`Edge report captured for ${days}d window`);
  await pollQuant();
}

function renderQuantReports() {
  const box = document.getElementById('quant-reports');
  if (!box || !_quantReports) return;
  const focus = _quantReports.focus_report || {};
  const focusSummary = focus.summary || {};
  const guard = _quantReports.guard_state || {};
  const latestByWindow = Array.isArray(_quantReports.latest_by_window) ? _quantReports.latest_by_window : [];
  const trendCards = Array.isArray(_quantReports.trend_cards) ? _quantReports.trend_cards : [];
  const leaderboard = Array.isArray(_quantReports.leaderboard) ? _quantReports.leaderboard : [];
  const recentReports = Array.isArray(_quantReports.recent_reports) ? _quantReports.recent_reports : [];
  box.innerHTML = `
    <div class="panel-head" style="margin-bottom:12px">
      <div>
        <div class="panel-title">Edge Reports</div>
        <div class="panel-copy">Persistent comparison snapshots and automated scorecards so you can see whether rules, the global model, or the regime model are actually improving over time.</div>
      </div>
      <div class="panel-actions">
        <button class="btn btn-ghost" type="button" onclick="runQuantReport()">Generate Report</button>
      </div>
    </div>
    <div class="shortcut-row" style="margin-bottom:12px">
      <span class="badge bg-muted">${_quantReports.report_count || 0} stored reports</span>
      <span class="badge bg-muted">Focus ${_quantReports.focus_window_days || 7}d</span>
      <span class="badge bg-muted">Leader ${prettyPolicyName(focusSummary.leader_policy)}</span>
      <span class="badge ${guard.status === 'halted' ? 'bg-red' : guard.status === 'defensive' ? 'bg-gold' : guard.status === 'throttled' ? 'bg-blue' : 'bg-muted'}">${guard.action_label || prettyPolicyName(guard.status || 'normal')}</span>
      <span class="badge bg-muted">Last ${fmtDateTime(focus.generated_at || focusSummary.generated_at)}</span>
    </div>
    <div class="operator-rule-list" style="margin-bottom:12px">
      <div class="operator-rule">
        <div>
          <div class="operator-rule-label">Live Safety Gate</div>
          <div style="font-size:10px;color:var(--t3);margin-top:3px">${guard.reason || 'Using normal report-driven posture.'}</div>
        </div>
        <div class="operator-rule-value">${guard.allow_new_entries === false ? 'blocked' : 'size ' + Math.round((guard.size_multiplier ?? 1) * 100) + '%'}</div>
      </div>
    </div>
    <div class="control-action-grid" style="grid-template-columns:repeat(3,minmax(0,1fr));margin-bottom:12px">
      ${latestByWindow.map(item => `
        <div class="scanner-summary-card">
          <div class="scanner-summary-label">${item.window_days}d latest</div>
          <div class="scanner-summary-value">${item.leader_avg_pnl_pct > 0 ? '+' : ''}${(item.leader_avg_pnl_pct || 0).toFixed(1)}%</div>
          <div class="scanner-summary-copy">${prettyPolicyName(item.leader_policy)} · edge ${(item.model_edge_pct || 0) > 0 ? '+' : ''}${(item.model_edge_pct || 0).toFixed(1)}%</div>
        </div>
      `).join('') || '<div class="scanner-summary-card"><div class="scanner-summary-label">Reports</div><div class="scanner-summary-value">—</div><div class="scanner-summary-copy">No persistent reports yet</div></div>'}
    </div>
    <div class="scanner-top-grid">
      <div>
        <div class="sec-label">Trend Deltas</div>
        <div class="operator-rule-list">
          ${trendCards.map(item => `
            <div class="operator-rule">
              <div>
                <div class="operator-rule-label">${item.window_days}d · ${prettyPolicyName(item.leader_policy)}</div>
                <div style="font-size:10px;color:var(--t3);margin-top:3px">
                  Leader ${(item.delta_leader_avg_pnl_pct ?? 0) >= 0 ? '+' : ''}${item.delta_leader_avg_pnl_pct == null ? 'n/a' : item.delta_leader_avg_pnl_pct + '%'}
                  · Regime edge ${(item.delta_regime_edge_pct ?? 0) >= 0 ? '+' : ''}${item.delta_regime_edge_pct == null ? 'n/a' : item.delta_regime_edge_pct + '%'}
                </div>
              </div>
              <div class="operator-rule-value">${(item.model_edge_pct || 0) >= 0 ? '+' : ''}${(item.model_edge_pct || 0).toFixed(1)}%</div>
            </div>
          `).join('') || '<div style="font-size:11px;color:var(--t3)">Need at least one persisted report before trends appear.</div>'}
        </div>
      </div>
      <div>
        <div class="sec-label">Policy Leaderboard</div>
        <div style="display:flex;flex-direction:column;gap:8px">
          ${leaderboard.slice(0, 5).map(item => `
            <div style="padding:10px 12px;border:1px solid rgba(255,255,255,.06);border-radius:12px;background:rgba(255,255,255,.02)">
              <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">
                <div>
                  <div style="font-size:12px;font-weight:800;color:var(--t1)">${prettyPolicyName(item.policy_name)}</div>
                  <div style="font-size:10px;color:var(--t3);margin-top:3px">${item.windows || 0} latest-window snapshots</div>
                </div>
                <div style="font-size:12px;font-weight:800;color:${item.avg_pnl_pct >= 0 ? 'var(--grn)' : 'var(--red2)'}">${item.avg_pnl_pct >= 0 ? '+' : ''}${item.avg_pnl_pct.toFixed(1)}%</div>
              </div>
              <div class="shortcut-row" style="margin-top:8px">
                <span class="badge bg-muted">Win ${item.avg_win_rate.toFixed(1)}%</span>
              </div>
            </div>
          `).join('') || '<div style="font-size:11px;color:var(--t3)">Leaderboard will populate after reports are generated.</div>'}
        </div>
      </div>
    </div>
    <div class="sec-label" style="margin-top:12px">Recent Snapshots</div>
    <div style="display:flex;flex-direction:column;gap:8px">
      ${recentReports.slice(0, 5).map(item => `
        <div style="padding:10px 12px;border:1px solid rgba(255,255,255,.06);border-radius:12px;background:rgba(255,255,255,.02)">
          <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">
            <div>
              <div style="font-size:12px;font-weight:800;color:var(--t1)">${item.window_days}d · ${prettyPolicyName(item.summary?.leader_policy)}</div>
              <div style="font-size:10px;color:var(--t3);margin-top:3px">${item.report_kind} · ${item.active_regime || 'neutral'} · ${fmtDateTime(item.generated_at)}</div>
            </div>
            <div style="font-size:12px;font-weight:800;color:${(item.summary?.model_edge_pct || 0) >= 0 ? 'var(--grn)' : 'var(--red2)'}">${(item.summary?.model_edge_pct || 0) >= 0 ? '+' : ''}${(item.summary?.model_edge_pct || 0).toFixed(1)}%</div>
          </div>
          <div class="shortcut-row" style="margin-top:8px">
            <span class="badge bg-muted">Rule ${(item.summary?.rule_avg_pnl_pct || 0).toFixed(1)}%</span>
            <span class="badge bg-muted">Regime ${(item.summary?.regime_edge_pct || 0) >= 0 ? '+' : ''}${(item.summary?.regime_edge_pct || 0).toFixed(1)}%</span>
            <span class="badge bg-muted">${(item.policies || []).length} policies</span>
          </div>
        </div>
      `).join('') || '<div style="font-size:11px;color:var(--t3)">No report history stored yet.</div>'}
    </div>
  `;
}

function renderQuantRuns() {
  const box = document.getElementById('quant-runs');
  if (!box) return;
  document.getElementById('quant-run-count').textContent = `${_quantRuns.length} runs`;
  if (!_quantRuns.length) {
    box.innerHTML = '<div style="text-align:center;color:var(--t3);font-size:12px;padding:24px 0">No backtest runs yet.</div>';
    return;
  }
  box.innerHTML = _quantRuns.map(run => {
    const active = _quantSelectedRunId === run.id;
    const strategies = Object.keys((run.summary || {}).strategies || {});
    const replayMode = run.replay_mode || run.config?.replay_mode || 'snapshot';
    const dataPoints = replayMode === 'event_tape'
      ? ((run.summary || {}).events_processed || run.snapshots_processed || 0)
      : (run.snapshots_processed || 0);
    return `
      <div onclick="showQuantRunDetail(${run.id})" style="padding:12px 14px;border:1px solid ${active ? 'rgba(96,165,250,.32)' : 'rgba(255,255,255,.06)'};border-radius:14px;background:${active ? 'rgba(47,107,255,.08)' : 'rgba(255,255,255,.02)'};margin-bottom:10px;cursor:pointer">
        <div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start;flex-wrap:wrap">
          <div>
            <div style="font-size:13px;font-weight:800;color:var(--t1)">${run.name || ('Run #' + run.id)}</div>
            <div style="font-size:10px;color:var(--t3);margin-top:3px">${run.days}d · ${replayMode === 'event_tape' ? 'event tape' : 'snapshots'} · ${run.strategy_filter || 'all strategies'} · ${run.created_at ? new Date(run.created_at).toLocaleString() : 'queued'}</div>
          </div>
          <div class="shortcut-row">
            <span class="badge bg-muted">${replayMode === 'event_tape' ? 'Tape' : 'Snapshot'}</span>
            <span class="badge ${run.status === 'completed' ? 'bg-grn' : run.status === 'failed' ? 'bg-red' : 'bg-gold'}">${run.status}</span>
          </div>
        </div>
        <div class="shortcut-row" style="margin-top:10px">
          <span class="badge bg-muted">${run.tokens_processed || 0} tokens</span>
          <span class="badge bg-muted">${dataPoints} ${replayMode === 'event_tape' ? 'events' : 'snapshots'}</span>
          <span class="badge bg-muted">${run.trades_closed || 0} trades</span>
          <span class="badge bg-muted">${strategies.length} strategies</span>
        </div>
      </div>
    `;
  }).join('');
}

async function runQuantBacktest() {
  const days = parseInt(document.getElementById('quant-days')?.value || '7', 10);
  const name = (document.getElementById('quant-run-name')?.value || '').trim();
  const replayMode = (document.getElementById('quant-replay-mode')?.value || 'snapshot').trim();
  const res = await fetch('/api/quant/backtests', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      days,
      name,
      replay_mode: replayMode,
      strategies: ['safe', 'balanced', 'aggressive', 'degen', 'safe_v2', 'balanced_v2', 'aggressive_v2', 'degen_v2'],
    }),
  }).then(r => r.json()).catch(() => null);
  if (!res || !res.ok) {
    showToast('Backtest launch failed', false);
    return;
  }
  _quantSelectedRunId = res.run_id;
  showToast(`${replayMode === 'event_tape' ? 'Tape replay' : 'Snapshot replay'} queued (#${res.run_id})`);
  pollQuant();
}

async function showQuantRunDetail(runId, silent=false) {
  _quantSelectedRunId = runId;
  renderQuantRuns();
  const box = document.getElementById('quant-run-detail');
  if (!box) return;
  if (!silent) {
    box.innerHTML = '<div style="text-align:center;color:var(--t3);font-size:12px;padding:24px 0">Loading replay detail…</div>';
  }
  const data = await fetch('/api/quant/backtests/' + runId).then(r => r.json()).catch(() => null);
  if (!data || !data.run) {
    box.innerHTML = '<div style="text-align:center;color:var(--t3);font-size:12px;padding:24px 0">Replay detail unavailable.</div>';
    return;
  }
  const run = data.run;
  const strategies = run.summary?.strategies || {};
  const replayMode = run.replay_mode || run.config?.replay_mode || 'snapshot';
  const sourceCounts = run.summary?.source_counts || {};
  const eventTypeCounts = run.summary?.event_type_counts || {};
  const topTrades = (data.trades || []).slice(0, 10);
  box.innerHTML = `
    <div class="sec-label">Replay Detail</div>
    <div style="font-size:17px;font-weight:800;color:var(--t1);font-family:'Space Grotesk','Manrope',sans-serif;margin-bottom:6px">${run.name || ('Run #' + run.id)}</div>
    <div style="font-size:11px;color:var(--t2);line-height:1.6;margin-bottom:12px">${run.status} · ${run.days}d window · ${replayMode === 'event_tape' ? 'event tape' : 'snapshot'} replay · ${run.tokens_processed || 0} tokens · ${(replayMode === 'event_tape' ? (run.summary?.events_processed || run.snapshots_processed || 0) : (run.snapshots_processed || 0))} ${replayMode === 'event_tape' ? 'events' : 'snapshots'} · ${run.trades_closed || 0} closed trades</div>
    ${replayMode === 'event_tape' ? `
      <div class="shortcut-row" style="margin-bottom:12px">
        ${Object.entries(eventTypeCounts).slice(0, 4).map(([name, count]) => `<span class="badge bg-muted">${name.replaceAll('_', ' ')} · ${count}</span>`).join('')}
        ${Object.entries(sourceCounts).slice(0, 3).map(([name, count]) => `<span class="badge bg-muted">${name} · ${count}</span>`).join('')}
      </div>
    ` : ''}
    <div class="operator-rule-list" style="margin-bottom:12px">
      ${Object.entries(strategies).map(([name, summary]) => `
        <div class="operator-rule">
          <div>
            <div class="operator-rule-label">${name}</div>
            <div style="font-size:10px;color:var(--t3);margin-top:3px">${summary.closed_trades || 0} trades · ${summary.win_rate || 0}% win rate</div>
          </div>
          <div class="operator-rule-value">${(summary.avg_pnl_pct || 0) > 0 ? '+' : ''}${summary.avg_pnl_pct || 0}% avg</div>
        </div>
      `).join('') || '<div style="font-size:11px;color:var(--t3)">No strategy summary available yet.</div>'}
    </div>
    <div class="sec-label">Top Simulated Trades</div>
    <div style="display:flex;flex-direction:column;gap:8px">
      ${topTrades.map(trade => `
        <div style="padding:10px 12px;border:1px solid rgba(255,255,255,.06);border-radius:12px;background:rgba(255,255,255,.02)">
          <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">
            <div>
              <div style="font-size:12px;font-weight:800;color:var(--t1)">${trade.name || trade.mint}</div>
              <div style="font-size:10px;color:var(--t3);margin-top:3px">${trade.strategy_name} · ${trade.exit_reason || trade.status}</div>
            </div>
            <div style="font-size:12px;font-weight:800;color:${trade.realized_pnl_pct >= 0 ? 'var(--grn)' : 'var(--red2)'}">${trade.realized_pnl_pct >= 0 ? '+' : ''}${trade.realized_pnl_pct.toFixed(2)}%</div>
          </div>
          <div class="shortcut-row" style="margin-top:8px">
            <span class="badge bg-muted">Score ${trade.score.toFixed(1)}</span>
            <span class="badge bg-muted">Conf ${(trade.confidence * 100).toFixed(0)}%</span>
            <span class="badge bg-muted">Up ${trade.max_upside_pct.toFixed(1)}%</span>
            <span class="badge bg-muted">DD ${trade.max_drawdown_pct.toFixed(1)}%</span>
          </div>
        </div>
      `).join('') || '<div style="font-size:11px;color:var(--t3)">No trades recorded yet.</div>'}
    </div>
  `;
}

// ── Init ──────────────────────────────────────────────────────────────────────
(function() {
  const initialPreset = PRESET_SETTINGS['{{PRESET}}'] ? '{{PRESET}}' : 'balanced';
  _settingsDirty = false;
  document._settingsFocused = false;
  setSettingsSaveState('Loading saved settings…');
  applySettingsToForm({ ...SETTINGS_DEFAULTS, ...(PRESET_SETTINGS[initialPreset] || {}), preset: initialPreset }, initialPreset);
  bindSettingsInputs();
  registerKeyboardShortcuts();
  renderEnhancedDashboard(null);
})();
window.addEventListener('resize', () => {
  if (treeCanvas && document.getElementById('tree-panel')?.style.display !== 'none') {
    treeCanvas.width = treeCanvas.parentElement.clientWidth;
  }
});
refresh();
setInterval(refresh, 5000);

// ══════════════════════════ FLOATING PORTFOLIO WIDGET ══════════════════════════
let _portfolioOpen = false;
function togglePortfolioPanel() {
  _portfolioOpen = !_portfolioOpen;
  const panel = document.getElementById('portfolio-panel');
  const fab = document.getElementById('portfolio-fab');
  if (_portfolioOpen) {
    panel.classList.add('open');
    fab.style.display = 'none';
    pollPortfolioWidget();
  } else {
    panel.classList.remove('open');
    fab.style.display = 'flex';
  }
}

const _coinColors = ['#2563eb','#7c3aed','#0891b2','#059669','#d97706','#dc2626','#4f46e5','#0d9488','#c026d3','#ea580c'];
function coinColor(name) {
  let h = 0;
  for (let i = 0; i < (name||'').length; i++) h = ((h << 5) - h) + name.charCodeAt(i);
  return _coinColors[Math.abs(h) % _coinColors.length];
}

async function pollPortfolioWidget() {
  try {
    const data = await fetch('/api/my-trades').then(r => r.json());
    const trades = data.open_trades || [];
    const totals = data.totals || {};
    const countEl = document.getElementById('fab-count');
    countEl.textContent = trades.length;
    countEl.className = 'fab-count' + (trades.length === 0 ? ' zero' : '');

    const summaryEl = document.getElementById('portfolio-summary');
    if (trades.length === 0) {
      summaryEl.textContent = 'No open positions';
    } else {
      const totalPnl = trades.reduce((s, t) => s + (t.profit_pct || 0), 0);
      const avgPnl = totalPnl / trades.length;
      summaryEl.innerHTML = `<span style="color:var(--t1);font-weight:700">${trades.length}</span> coin${trades.length>1?'s':''} &middot; avg <span class="${avgPnl>=0?'c-grn':'c-red'}" style="font-weight:700">${avgPnl>=0?'+':''}${avgPnl.toFixed(1)}%</span>`;
    }

    const container = document.getElementById('portfolio-coins');
    if (!trades.length) {
      container.innerHTML = '<div class="portfolio-empty">No active positions yet.<br>The bot will show your coins here when it buys.</div>';
      return;
    }

    container.innerHTML = trades.map(t => {
      const pnl = t.profit_pct || 0;
      const pnlCls = pnl >= 0 ? 'c-grn' : 'c-red';
      const pnlSol = t.amount_sol ? (t.amount_sol * pnl / 100) : 0;
      const bg = coinColor(t.name);
      const initial = (t.name||'?')[0].toUpperCase();
      return `<div class="portfolio-coin">
        <div class="coin-icon" style="background:${bg}">${initial}</div>
        <div class="coin-info">
          <div class="coin-name">${t.name||'Unknown'}</div>
          <div class="coin-meta">${t.held_for} &middot; ${t.amount_sol||0} SOL &middot; ${t.bought_when||''}</div>
        </div>
        <div class="coin-pnl">
          <div class="coin-pnl-pct ${pnlCls}">${pnl>0?'+':''}${pnl.toFixed(1)}%</div>
          <div class="coin-pnl-sol">${pnlSol>=0?'+':''}${pnlSol.toFixed(4)} SOL</div>
        </div>
      </div>`;
    }).join('') + `<div class="portfolio-total-row">
      <div style="font-size:11px;color:var(--t2);font-weight:700">Balance</div>
      <div style="font-size:13px;font-weight:800;color:var(--t1)">${(totals.balance_sol||0).toFixed(4)} SOL</div>
    </div>
    <div class="portfolio-total-row" style="border-top:none;padding-top:4px">
      <div style="font-size:11px;color:var(--t2);font-weight:700">Session P&L</div>
      <div class="${(totals.total_profit_sol||0)>=0?'c-grn':'c-red'}" style="font-size:13px;font-weight:800">${(totals.total_profit_sol||0)>=0?'+':''}${(totals.total_profit_sol||0).toFixed(4)} SOL</div>
    </div>`;
  } catch(e) { console.error('portfolio widget error', e); }
}
// Poll portfolio data
pollPortfolioWidget();
setInterval(pollPortfolioWidget, 6000);

pollFeed(); setInterval(pollFeed, 8000);
pollEvaluationFeed(); setInterval(pollEvaluationFeed, 6000);
pollScanDetail(); setInterval(pollScanDetail, 7000);
pollListings(); setInterval(pollListings, 6000);
pollEnhancedDashboard(); setInterval(pollEnhancedDashboard, 15000);

// ═══════════ LIVE SHADOW BANNER ═══════════
let _shadowFeedItems = [];
async function pollShadowBanner() {
  try {
    const [perf, equity] = await Promise.all([
      fetch('/api/quant/shadow-performance').then(r=>r.json()).catch(()=>[]),
      fetch('/api/quant/shadow-equity').then(r=>r.json()).catch(()=>({})),
    ]);
    // Handle both old array format and new {strategies, since_fix} format
    var strategies = Array.isArray(perf) ? perf : (perf.strategies || []);
    if (!strategies.length) return;
    let totalTrades = 0, totalWins = 0, totalPnl = 0, bestStrat = '', bestScore = -999;
    strategies.forEach(s => {
      const t = s.closed_trades || 0;
      totalTrades += t;
      totalWins += s.wins || 0;
      totalPnl += (s.avg_pnl_pct || 0) * t;
      const score = (s.win_rate || 0) * 0.6 + (s.avg_pnl_pct || 0) * 0.4;
      if (score > bestScore) { bestScore = score; bestStrat = s.strategy_name; }
    });
    const wr = totalTrades > 0 ? ((totalWins / totalTrades) * 100).toFixed(1) : '—';
    const avgPnl = totalTrades > 0 ? (totalPnl / totalTrades).toFixed(1) : '0';

    // Animate value changes
    _animateShadowVal('lv-shadow-trades', totalTrades.toString());
    _animateShadowVal('lv-shadow-wr', wr + '%');
    const pnlEl = document.getElementById('lv-shadow-pnl');
    if (pnlEl) {
      pnlEl.textContent = (avgPnl > 0 ? '+' : '') + avgPnl + '%';
      pnlEl.className = 'live-stat-val ' + (avgPnl > 0 ? 'pos' : avgPnl < 0 ? 'neg' : 'neu');
      pnlEl.classList.add('updating');
      setTimeout(() => pnlEl.classList.remove('updating'), 400);
    }
    const bestEl = document.getElementById('lv-shadow-best');
    if (bestEl && bestStrat) {
      bestEl.textContent = bestStrat.charAt(0).toUpperCase() + bestStrat.slice(1);
      bestEl.className = 'live-stat-val pos';
    }
    // Sync scanner tab shadow strip
    setText('sc-shadow-wr', wr !== '—' ? wr + '%' : '—');
    setText('sc-shadow-pnl2', (avgPnl > 0 ? '+' : '') + avgPnl + '%');
    setText('sc-shadow-trades2', totalTrades.toString());
    if (bestStrat) setText('sc-shadow-preset', bestStrat);

    // Build trade feed from equity data
    const allPoints = [];
    Object.entries(equity).forEach(([strat, points]) => {
      if (Array.isArray(points)) {
        points.forEach(p => allPoints.push({...p, strategy: strat}));
      }
    });
    allPoints.sort((a,b) => (b.hour||'').localeCompare(a.hour||''));
    const feedEl = document.getElementById('live-feed');
    if (feedEl && allPoints.length > 0) {
      const items = allPoints.slice(0, 3).map(p => {
        const isWin = (p.period_pnl || 0) >= 0;
        const cls = isWin ? 'win' : 'loss';
        const icon = isWin ? '+' : '-';
        const pnl = (p.period_pnl || 0) > 0 ? '+' + p.period_pnl + '%' : p.period_pnl + '%';
        const name = p.strategy || 'unknown';
        return '<div class="live-feed-item ' + cls + '"><span class="live-feed-icon">' + icon + '</span><span class="live-feed-name">' + name + '</span><span style="color:var(--t3)">' + (p.trades||0) + ' trades</span><span class="live-feed-pnl">' + pnl + '</span></div>';
      });
      feedEl.innerHTML = items.join('');
    }
    // Before/After comparison strip
    var before = perf.before_optimization;
    var after = perf.after_optimization;
    var cmpEl = document.getElementById('lv-compare');
    var cmpStats = document.getElementById('lv-compare-stats');
    if (cmpEl && cmpStats && after && after.total > 0) {
      cmpEl.style.display = 'block';
      function cmpStat(label, bVal, aVal, suffix, better) {
        var arrow = better === 'higher' ? (aVal > bVal ? '#14c784' : '#ef4444') : (aVal < bVal ? '#14c784' : '#ef4444');
        if (bVal === 0 && aVal === 0) arrow = 'var(--t3)';
        return '<div style="display:flex;flex-direction:column;align-items:center;gap:1px">' +
          '<div style="color:var(--t3);font-size:9px;text-transform:uppercase">' + label + '</div>' +
          '<div style="display:flex;gap:6px;align-items:center">' +
            '<span style="color:var(--t3)">' + bVal + suffix + '</span>' +
            '<span style="color:var(--t3)">→</span>' +
            '<span style="color:' + arrow + ';font-weight:800">' + aVal + suffix + '</span>' +
          '</div></div>';
      }
      cmpStats.innerHTML =
        cmpStat('Trades', before.total, after.total, '', 'higher') +
        cmpStat('Win Rate', before.win_rate, after.win_rate, '%', 'higher') +
        cmpStat('Avg P&L', before.avg_pnl_pct, after.avg_pnl_pct, '%', 'higher') +
        cmpStat('Median', before.median_pnl_pct, after.median_pnl_pct, '%', 'higher') +
        cmpStat('TP1 Hits', before.tp1_hits, after.tp1_hits, '', 'higher');
    }
  } catch(e) {}
}
function _animateShadowVal(id, newVal) {
  const el = document.getElementById(id);
  if (!el) return;
  if (el.textContent !== newVal) {
    el.textContent = newVal;
    el.classList.add('updating');
    setTimeout(() => el.classList.remove('updating'), 400);
  }
}
pollShadowBanner(); setInterval(pollShadowBanner, 8000);

// ═══════════ HOVER TOOLTIP SYSTEM ═══════════
(function() {
  const tip = document.createElement('div');
  tip.className = 'htip';
  tip.innerHTML = '<div class="htip-title"></div><div class="htip-body"></div>';
  document.body.appendChild(tip);
  let hoverTimer = null;
  let currentTarget = null;

  document.addEventListener('mouseover', function(e) {
    const el = e.target.closest('[data-tip]');
    if (!el) return;
    if (el === currentTarget) return;
    currentTarget = el;
    clearTimeout(hoverTimer);
    // Show after 5 seconds of hovering
    hoverTimer = setTimeout(function() {
      const text = el.getAttribute('data-tip');
      if (!text) return;
      // Get title from nearest section title or setting label
      const titleEl = el.querySelector('.settings-section-title, .setting-label, .s-autotune-status div, h3');
      const titleText = titleEl ? titleEl.textContent.trim() : 'Info';
      tip.querySelector('.htip-title').textContent = titleText;
      tip.querySelector('.htip-body').textContent = text;
      // Position near mouse
      const rect = el.getBoundingClientRect();
      let top = rect.bottom + 8;
      let left = rect.left + 20;
      // Keep on screen
      if (top + 200 > window.innerHeight) top = rect.top - 180;
      if (left + 290 > window.innerWidth) left = window.innerWidth - 300;
      tip.style.top = top + 'px';
      tip.style.left = left + 'px';
      tip.classList.add('visible');
    }, 5000);
  });

  document.addEventListener('mouseout', function(e) {
    const el = e.target.closest('[data-tip]');
    if (el === currentTarget) {
      clearTimeout(hoverTimer);
      currentTarget = null;
      tip.classList.remove('visible');
    }
  });

  // Also hide on scroll/click
  document.addEventListener('scroll', function() { tip.classList.remove('visible'); clearTimeout(hoverTimer); currentTarget = null; }, true);
  document.addEventListener('click', function() { tip.classList.remove('visible'); clearTimeout(hoverTimer); currentTarget = null; });
})();

// ═══════════ SETTINGS SECTION TOGGLES ═══════════
function toggleSettingsSection(id) {
  const body = document.getElementById(id + '-body');
  const chev = document.getElementById(id + '-chev');
  if (!body) return;
  const isOpen = body.classList.contains('open');
  body.classList.toggle('open', !isOpen);
  if (chev) chev.classList.toggle('open', !isOpen);
}

// ═══════════ AUTO-TUNE STATUS IN SETTINGS ═══════════
async function updateAutoTuneStatus() {
  try {
    const data = await fetch('/api/quant/auto-tune-status').then(r=>r.json()).catch(()=>null);
    if (!data) return;
    const presetEl = document.getElementById('s-tune-preset');
    const lastEl = document.getElementById('s-tune-last');
    if (presetEl) presetEl.textContent = 'Active: ' + (data.active_preset || 'balanced');
    if (lastEl) {
      if (data.last_shadow_tune_at) {
        const ago = Math.round((Date.now()/1000 - data.last_shadow_tune_at) / 60);
        lastEl.textContent = 'Last tune: ' + (ago < 60 ? ago + 'min ago' : Math.round(ago/60) + 'h ago');
      } else {
        lastEl.textContent = 'Last tune: waiting for data';
      }
    }
  } catch(e){}
}
updateAutoTuneStatus(); setInterval(updateAutoTuneStatus, 30000);

// ═══════════ DASHBOARD PROMO BANNER STATS ═══════════
async function updatePromoBanner() {
  try {
    const perfRaw = await fetch('/api/quant/shadow-performance').then(r=>r.json()).catch(()=>[]);
    var perfArr = Array.isArray(perfRaw) ? perfRaw : (perfRaw.strategies || []);
    if (!perfArr.length) return;
    let totalTrades = 0, totalWins = 0, bestTrade = 0;
    perfArr.forEach(s => {
      totalTrades += s.closed_trades || 0;
      totalWins += s.wins || 0;
      if ((s.best_trade_pct || 0) > bestTrade) bestTrade = s.best_trade_pct || 0;
    });
    const wr = totalTrades > 0 ? ((totalWins / totalTrades) * 100).toFixed(1) + '%' : '—';
    const dpTrades = document.getElementById('dp-trades');
    const dpWr = document.getElementById('dp-winrate');
    const dpBest = document.getElementById('dp-best');
    if (dpTrades) dpTrades.textContent = totalTrades;
    if (dpWr) dpWr.textContent = wr;
    if (dpBest) dpBest.textContent = bestTrade > 5000 ? '+5,000%+' : bestTrade > 0 ? '+' + bestTrade.toFixed(0) + '%' : '—';
  } catch(e){}
}
updatePromoBanner(); setInterval(updatePromoBanner, 12000);

// ═══════════ SHADOW ACTIVITY WINDOW ═══════════
let _shadowActivityOpen = false;
let _shadowActivityTab = 'open';

function toggleShadowActivity() {
  _shadowActivityOpen = !_shadowActivityOpen;
  const body = document.getElementById('sa-body');
  const chev = document.getElementById('sa-chevron');
  if (body) body.classList.toggle('open', _shadowActivityOpen);
  if (chev) chev.classList.toggle('open', _shadowActivityOpen);
  if (_shadowActivityOpen) pollShadowActivity();
}

function switchShadowTab(tab) {
  _shadowActivityTab = tab;
  document.querySelectorAll('.shadow-activity-tab').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.shadow-activity-tab').forEach(el => {
    if ((tab === 'open' && el.textContent.includes('Open')) || (tab === 'closed' && el.textContent.includes('Closed')))
      el.classList.add('active');
  });
  const openList = document.getElementById('sa-open-list');
  const closedList = document.getElementById('sa-closed-list');
  if (openList) openList.style.display = tab === 'open' ? 'block' : 'none';
  if (closedList) closedList.style.display = tab === 'closed' ? 'block' : 'none';
}

async function pollShadowActivity() {
  try {
    const data = await fetch('/api/quant/shadow-activity').then(r => r.json()).catch(() => null);
    if (!data) return;
    const stats = data.stats || {};
    // Update badges
    const openBadge = document.getElementById('sa-open-count');
    const closedBadge = document.getElementById('sa-closed-count');
    if (openBadge) openBadge.textContent = (stats.total_open || 0) + ' open';
    if (closedBadge) closedBadge.textContent = (stats.total_closed || 0) + ' closed';

    // Stats strip
    const wrEl = document.getElementById('sa-wr');
    if (wrEl) { wrEl.textContent = stats.win_rate ? stats.win_rate + '%' : '—'; wrEl.className = 'val ' + (stats.win_rate >= 50 ? 'c-grn' : stats.win_rate > 0 ? 'c-red' : 'neu'); }
    const avgEl = document.getElementById('sa-avg-pnl');
    if (avgEl) { avgEl.textContent = stats.avg_pnl_pct ? (stats.avg_pnl_pct > 0 ? '+' : '') + stats.avg_pnl_pct + '%' : '—'; avgEl.className = 'val ' + (stats.avg_pnl_pct > 0 ? 'c-grn' : stats.avg_pnl_pct < 0 ? 'c-red' : 'neu'); }
    const bestEl = document.getElementById('sa-best');
    if (bestEl) bestEl.textContent = stats.best_pnl_pct ? '+' + stats.best_pnl_pct + '%' : '—';
    const worstEl = document.getElementById('sa-worst');
    if (worstEl) worstEl.textContent = stats.worst_pnl_pct ? stats.worst_pnl_pct + '%' : '—';
    const totalPnlEl = document.getElementById('sa-total-pnl');
    if (totalPnlEl) { totalPnlEl.textContent = stats.median_pnl_pct ? (stats.median_pnl_pct > 0 ? '+' : '') + stats.median_pnl_pct + '%' : '—'; totalPnlEl.className = 'val ' + (stats.median_pnl_pct > 0 ? 'c-grn' : stats.median_pnl_pct < 0 ? 'c-red' : 'neu'); }
    const totalClosedEl = document.getElementById('sa-total-closed');
    if (totalClosedEl) totalClosedEl.textContent = stats.total_closed || 0;

    // Open positions list
    const openList = document.getElementById('sa-open-list');
    if (openList) {
      const openPositions = data.open_positions || [];
      if (openPositions.length === 0) {
        openList.innerHTML = '<div style="text-align:center;color:var(--t3);font-size:12px;padding:20px 0">No open shadow positions right now.</div>';
      } else {
        openList.innerHTML = openPositions.map(p => {
          const pnl = p.unrealized_pnl_pct || 0;
          const pnlColor = pnl >= 0 ? 'var(--grn)' : 'var(--red2)';
          return '<div class="shadow-pos-card">' +
            '<div class="shadow-pos-row">' +
              '<div><div class="shadow-pos-name">' + (p.name || p.mint) + '</div>' +
              '<div class="shadow-pos-meta">' + (p.strategy || '') + ' · ' + (p.age_min || 0).toFixed(0) + 'min old · Entry $' + (p.entry_price || 0).toPrecision(4) + '</div></div>' +
              '<div class="shadow-pos-pnl" style="color:' + pnlColor + '">' + (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + '%</div>' +
            '</div>' +
            '<div class="shortcut-row" style="margin-top:8px">' +
              '<span class="badge bg-muted">Peak +' + (p.max_upside_pct || 0).toFixed(1) + '%</span>' +
              '<span class="badge bg-muted">DD ' + (p.max_drawdown_pct || 0).toFixed(1) + '%</span>' +
              '<span class="badge bg-muted">Now $' + (p.current_price || 0).toPrecision(4) + '</span>' +
              '<span class="badge bg-muted">' + (p.mint || '').slice(0, 6) + '...' + (p.mint || '').slice(-4) + '</span>' +
            '</div>' +
          '</div>';
        }).join('');
      }
    }

    // Closed trades list
    const closedList = document.getElementById('sa-closed-list');
    if (closedList) {
      const closedTrades = data.recent_closed || [];
      if (closedTrades.length === 0) {
        closedList.innerHTML = '<div style="text-align:center;color:var(--t3);font-size:12px;padding:20px 0">No closed shadow trades yet.</div>';
      } else {
        closedList.innerHTML = closedTrades.map(t => {
          const pnl = t.realized_pnl_pct || 0;
          const pnlColor = pnl >= 0 ? 'var(--grn)' : 'var(--red2)';
          const isWin = pnl > 0;
          return '<div class="shadow-pos-card" style="border-left:3px solid ' + (isWin ? 'rgba(20,199,132,.4)' : 'rgba(239,68,68,.4)') + '">' +
            '<div class="shadow-pos-row">' +
              '<div><div class="shadow-pos-name">' + (t.name || t.mint) + '</div>' +
              '<div class="shadow-pos-meta">' + (t.strategy || '') + ' · ' + (t.exit_reason || 'closed') + ' · held ' + (t.hold_min || 0).toFixed(0) + 'min</div></div>' +
              '<div class="shadow-pos-pnl" style="color:' + pnlColor + '">' + (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + '%</div>' +
            '</div>' +
            '<div class="shortcut-row" style="margin-top:8px">' +
              '<span class="badge ' + (isWin ? 'bg-grn' : 'bg-red') + '">' + (isWin ? 'WIN' : 'LOSS') + '</span>' +
              '<span class="badge bg-muted">Peak +' + (t.max_upside_pct || 0).toFixed(1) + '%</span>' +
              '<span class="badge bg-muted">DD ' + (t.max_drawdown_pct || 0).toFixed(1) + '%</span>' +
              '<span class="badge bg-muted">Entry $' + (t.entry_price || 0).toPrecision(4) + '</span>' +
              '<span class="badge bg-muted">' + (t.mint || '').slice(0, 6) + '...' + (t.mint || '').slice(-4) + '</span>' +
            '</div>' +
          '</div>';
        }).join('');
      }
    }
  } catch(e) {}
}
setInterval(() => { if (_shadowActivityOpen) pollShadowActivity(); }, 10000);

// ═══════════ QUANT SECTION TOGGLES ═══════════
function toggleQuantDropdown(sectionId) {
  const body = document.getElementById(sectionId + '-body');
  const chev = document.getElementById(sectionId + '-chev');
  if (!body) return;
  const isOpen = body.classList.contains('open');
  body.classList.toggle('open', !isOpen);
  if (chev) chev.classList.toggle('open', !isOpen);
}

function toggleQuantSection(sectionId, btn) {
  const section = document.getElementById(sectionId);
  if (!section) return;
  const isHidden = section.style.display === 'none';
  section.style.display = isHidden ? '' : 'none';
  if (btn) btn.classList.toggle('active', isHidden);
}

// ═══════════ OPPORTUNITY SETTINGS ═══════════
function toggleOpportunitySettings() {
  const panel = document.getElementById('opp-settings-panel');
  if (panel) panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
}

async function applyOpportunitySettings() {
  const aggression = document.getElementById('opp-aggression')?.value || 'aggressive';
  try {
    const res = await fetch('/api/quant/opportunity-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ aggression }),
    }).then(r => r.json());
    if (res && res.ok) {
      showToast(res.message || 'Opportunity settings applied!');
      toggleOpportunitySettings();
    } else {
      showToast('Failed to apply opportunity settings', false);
    }
  } catch(e) {
    showToast('Error applying settings', false);
  }
}

// ── Paper Trading Engine ──────────────────────────────────────────────────────
let _paperActive = false;
let _paperBal = 10;
let _paperStartBal = 10;
let _paperTradeSize = 0.1;
let _paperPositions = {};  // mint -> {name, symbol, entryPrice, entryTime, solSize, tokensHeld, currentPrice, peakPrice, tp1Hit}
let _paperHistory = [];    // closed trades
let _paperSeenMints = {};  // mint -> true, prevent re-buying same token
let _paperStats = { wins: 0, losses: 0, totalPnl: 0 };
let _paperSettings = { tp1: 50, tp2: 150, sl: 30, trail: 40, maxPos: 10, preset: 'balanced' };

const PAPER_PRESETS = {
  degen:        { tp1: 80,  tp2: 300, sl: 50, trail: 35, maxPos: 20, label: 'Degen \u2014 High risk, big swings. Wide SL, aggressive targets, many positions.' },
  balanced:     { tp1: 50,  tp2: 150, sl: 30, trail: 40, maxPos: 10, label: 'Balanced \u2014 Moderate risk. Reasonable TP/SL, controlled exposure.' },
  conservative: { tp1: 25,  tp2: 60,  sl: 15, trail: 50, maxPos: 5,  label: 'Conservative \u2014 Tight stops, quick profits, limited positions.' },
};

function paperLoadState() {
  try {
    const s = localStorage.getItem('paperState');
    if (!s) return;
    const d = JSON.parse(s);
    _paperActive = d.active || false;
    _paperBal = d.bal || 10;
    _paperStartBal = d.startBal || 10;
    _paperTradeSize = d.tradeSize || 0.1;
    _paperPositions = d.positions || {};
    _paperHistory = d.history || [];
    _paperSeenMints = d.seen || {};
    _paperStats = d.stats || { wins: 0, losses: 0, totalPnl: 0 };
    if (d.settings) _paperSettings = d.settings;
    document.getElementById('paper-balance-input').value = _paperStartBal;
    document.getElementById('paper-trade-size').value = _paperTradeSize;
    paperSyncSettingsUI();
  } catch(e) {}
}

function paperSaveState() {
  try {
    localStorage.setItem('paperState', JSON.stringify({
      active: _paperActive, bal: _paperBal, startBal: _paperStartBal,
      tradeSize: _paperTradeSize, positions: _paperPositions,
      history: _paperHistory, seen: _paperSeenMints, stats: _paperStats,
      settings: _paperSettings
    }));
  } catch(e) {}
}

function paperSyncSettingsUI() {
  document.getElementById('paper-tp1').value = _paperSettings.tp1;
  document.getElementById('paper-tp2').value = _paperSettings.tp2;
  document.getElementById('paper-sl').value = _paperSettings.sl;
  document.getElementById('paper-trail').value = _paperSettings.trail;
  document.getElementById('paper-max-pos').value = _paperSettings.maxPos;
  paperUpdatePresetHighlight();
}

function paperSettingsChanged() {
  _paperSettings.tp1 = parseFloat(document.getElementById('paper-tp1').value) || 50;
  _paperSettings.tp2 = parseFloat(document.getElementById('paper-tp2').value) || 150;
  _paperSettings.sl = parseFloat(document.getElementById('paper-sl').value) || 30;
  _paperSettings.trail = parseFloat(document.getElementById('paper-trail').value) || 40;
  _paperSettings.maxPos = parseInt(document.getElementById('paper-max-pos').value) || 10;
  _paperSettings.preset = 'custom';
  paperUpdatePresetHighlight();
  paperSaveState();
}

function paperApplyPreset(name) {
  const p = PAPER_PRESETS[name];
  if (!p) return;
  _paperSettings = { tp1: p.tp1, tp2: p.tp2, sl: p.sl, trail: p.trail, maxPos: p.maxPos, preset: name };
  paperSyncSettingsUI();
  paperSaveState();
  showToast(name.charAt(0).toUpperCase() + name.slice(1) + ' preset applied');
}

function paperUpdatePresetHighlight() {
  var presetName = _paperSettings.preset || 'custom';
  ['degen', 'balanced', 'conservative'].forEach(function(n) {
    var btn = document.getElementById('paper-preset-' + n);
    if (!btn) return;
    if (n === presetName) {
      btn.style.boxShadow = '0 0 12px ' + (n === 'degen' ? 'rgba(239,68,68,.4)' : n === 'balanced' ? 'rgba(20,199,132,.4)' : 'rgba(59,130,246,.4)');
      btn.style.fontWeight = '800';
    } else {
      btn.style.boxShadow = 'none';
      btn.style.fontWeight = '700';
    }
  });
  var label = document.getElementById('paper-preset-label');
  if (label) {
    var p = PAPER_PRESETS[presetName];
    label.textContent = p ? p.label : 'Custom settings \u2014 manually configured TP/SL values.';
  }
}

function paperStart() {
  _paperStartBal = parseFloat(document.getElementById('paper-balance-input').value) || 10;
  _paperTradeSize = parseFloat(document.getElementById('paper-trade-size').value) || 0.1;
  if (_paperTradeSize > _paperStartBal) { showToast('Trade size cannot exceed balance', false); return; }
  if (!_paperActive) {
    _paperBal = _paperStartBal;
    _paperPositions = {};
    _paperHistory = [];
    _paperSeenMints = {};
    _paperStats = { wins: 0, losses: 0, totalPnl: 0 };
  }
  _paperActive = true;
  paperSaveState();
  renderPaper();
  showToast('Paper session started with ' + _paperStartBal.toFixed(2) + ' SOL');
}

function paperReset() {
  _paperActive = false;
  _paperBal = parseFloat(document.getElementById('paper-balance-input').value) || 10;
  _paperStartBal = _paperBal;
  _paperPositions = {};
  _paperHistory = [];
  _paperSeenMints = {};
  _paperStats = { wins: 0, losses: 0, totalPnl: 0 };
  paperSaveState();
  renderPaper();
  showToast('Paper session reset');
}

function paperBuy(mint, name, symbol, price) {
  if (!_paperActive || !price || price <= 0) return;
  if (_paperPositions[mint] || _paperSeenMints[mint]) return;
  if (Object.keys(_paperPositions).length >= (_paperSettings.maxPos || 10)) return;
  const size = Math.min(_paperTradeSize, _paperBal);
  if (size < 0.001) return;
  _paperBal -= size;
  const tokens = size / price;
  _paperPositions[mint] = {
    name: name || symbol || '?', symbol: symbol || '?',
    entryPrice: price, entryTime: Date.now(), solSize: size,
    tokensHeld: tokens, currentPrice: price, peakPrice: price,
    tp1Hit: false
  };
  _paperSeenMints[mint] = true;
  paperSaveState();
  if (_activeTab === 'paper') renderPaper();
}

function paperSellPartial(mint, fraction, reason) {
  // Sell a fraction (0-1) of a position — used for TP1
  const pos = _paperPositions[mint];
  if (!pos) return;
  const exitPrice = pos.currentPrice || pos.entryPrice;
  const sellTokens = pos.tokensHeld * fraction;
  const sellValue = sellTokens * exitPrice;
  const sellCost = pos.solSize * fraction;
  const pnl = sellValue - sellCost;
  _paperBal += sellValue;
  _paperStats.totalPnl += pnl;
  // Reduce position
  pos.tokensHeld -= sellTokens;
  pos.solSize -= sellCost;
  pos.tp1Hit = true;
  pos.tp1Pnl = pnl;
  _paperHistory.unshift({
    name: pos.name, symbol: pos.symbol, mint: mint,
    entryPrice: pos.entryPrice, exitPrice: exitPrice,
    solSize: sellCost, pnl: pnl,
    pnlPct: ((exitPrice / pos.entryPrice) - 1) * 100,
    holdTime: Date.now() - pos.entryTime,
    reason: reason || 'TP1 partial', ts: Date.now()
  });
  if (_paperHistory.length > 50) _paperHistory.pop();
  paperSaveState();
  if (_activeTab === 'paper') renderPaper();
}

function paperSell(mint, reason) {
  const pos = _paperPositions[mint];
  if (!pos) return;
  const exitPrice = pos.currentPrice || pos.entryPrice;
  const exitValue = pos.tokensHeld * exitPrice;
  const pnl = exitValue - pos.solSize;
  _paperBal += exitValue;
  // Win/loss based on combined P&L (TP1 partial + final exit)
  const combinedPnl = pnl + (pos.tp1Pnl || 0);
  if (combinedPnl >= 0) _paperStats.wins++; else _paperStats.losses++;
  _paperStats.totalPnl += pnl;
  _paperHistory.unshift({
    name: pos.name, symbol: pos.symbol, mint: mint,
    entryPrice: pos.entryPrice, exitPrice: exitPrice,
    solSize: pos.solSize, pnl: pnl,
    pnlPct: ((exitPrice / pos.entryPrice) - 1) * 100,
    holdTime: Date.now() - pos.entryTime,
    reason: reason || 'manual', ts: Date.now()
  });
  if (_paperHistory.length > 50) _paperHistory.pop();
  delete _paperPositions[mint];
  paperSaveState();
  if (_activeTab === 'paper') renderPaper();
}

function paperCheckExits(mint, price) {
  // Centralized exit logic using custom settings
  const pos = _paperPositions[mint];
  if (!pos || !price || price <= 0) return;
  pos.currentPrice = price;
  if (price > pos.peakPrice) pos.peakPrice = price;
  const gainPct = ((price / pos.entryPrice) - 1) * 100;
  const dropFromPeak = (1 - price / pos.peakPrice) * 100;
  var tp1 = _paperSettings.tp1 || 50;
  var tp2 = _paperSettings.tp2 || 150;
  var sl = _paperSettings.sl || 30;
  var trail = _paperSettings.trail || 40;
  // Stop loss
  if (gainPct <= -sl) { paperSell(mint, 'SL -' + sl + '%'); return; }
  // TP2 — full exit
  if (gainPct >= tp2) { paperSell(mint, 'TP2 +' + tp2 + '%'); return; }
  // TP1 — sell 50% if not already hit
  if (!pos.tp1Hit && gainPct >= tp1) { paperSellPartial(mint, 0.5, 'TP1 +' + tp1 + '%'); return; }
  // Trailing stop — only after TP1 hit (or if gain > 10%)
  if (pos.tp1Hit && dropFromPeak >= trail) { paperSell(mint, 'Trail -' + trail + '%'); return; }
  if (!pos.tp1Hit && gainPct > 10 && dropFromPeak >= trail) { paperSell(mint, 'Trail -' + trail + '%'); return; }
}

function paperProcessFilterLog(logs) {
  if (!_paperActive || !logs || !logs.length) return;
  logs.forEach(f => {
    if (!f.passed || !f.mint) return;
    if (_paperPositions[f.mint] || _paperSeenMints[f.mint]) return;
    // Find price from allTokens
    const tok = allTokens.find(t => t.mint === f.mint);
    if (!tok || !tok.price || tok.price <= 0) return;
    paperBuy(f.mint, tok.name || f.name, tok.symbol, tok.price);
  });
}

function paperUpdatePrices() {
  if (!_paperActive) return;
  const openMints = Object.keys(_paperPositions);
  if (!openMints.length) return;
  openMints.forEach(mint => {
    const tok = allTokens.find(t => t.mint === mint);
    if (tok && tok.price > 0) {
      paperCheckExits(mint, tok.price);
    }
  });
  paperSaveState();
  if (_activeTab === 'paper') renderPaper();
}

async function paperTick() {
  if (!_paperActive) return;
  const openMints = Object.keys(_paperPositions);
  if (!openMints.length) { renderPaper(); return; }
  try {
    const prices = await fetch('/api/paper-prices', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mints: openMints})
    }).then(r => r.json());
    openMints.forEach(mint => {
      const p = prices[mint];
      if (p && p.price > 0) {
        paperCheckExits(mint, p.price);
      }
    });
    paperSaveState();
  } catch(e) {}
  renderPaper();
}

function fmtHold(ms) {
  const s = Math.floor(ms/1000);
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s/60) + 'm';
  return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
}

function renderPaper() {
  // Status
  const status = document.getElementById('paper-status');
  if (_paperActive) {
    status.innerHTML = '<span style="color:var(--grn)">&#x25cf; Session active</span> <span style="font-size:10px;color:var(--t3)">(' + Object.keys(_paperPositions).length + '/' + (_paperSettings.maxPos||10) + ' positions)</span>';
  } else {
    status.textContent = 'Click Start to begin';
  }
  // Summary cards
  document.getElementById('paper-bal').textContent = _paperBal.toFixed(3);
  document.getElementById('paper-open-count').textContent = Object.keys(_paperPositions).length;
  const pnlVal = _paperBal - _paperStartBal + Object.values(_paperPositions).reduce((s, p) => s + (p.tokensHeld * p.currentPrice - p.solSize), 0);
  const pnlEl = document.getElementById('paper-pnl');
  pnlEl.textContent = (pnlVal >= 0 ? '+' : '') + pnlVal.toFixed(4) + ' SOL';
  pnlEl.style.color = pnlVal >= 0 ? 'var(--grn)' : 'var(--red)';
  const total = _paperStats.wins + _paperStats.losses;
  document.getElementById('paper-wr').textContent = total > 0 ? Math.round((_paperStats.wins / total) * 100) + '%' : '-';

  // Open positions
  const posDiv = document.getElementById('paper-positions');
  const openEntries = Object.entries(_paperPositions);
  if (!openEntries.length) {
    posDiv.innerHTML = '<div style="color:var(--t3);font-size:12px">No open positions' + (_paperActive ? ' &#8212; waiting for signals (' + Object.keys(_paperPositions).length + '/' + (_paperSettings.maxPos||10) + ' slots)' : '') + '</div>';
  } else {
    posDiv.innerHTML = '<table style="width:100%;border-collapse:collapse;font-size:12px"><tr style="color:var(--t3);border-bottom:1px solid var(--b1)">' +
      '<th style="text-align:left;padding:4px">Token</th><th>Entry</th><th>Current</th><th>P&L</th><th>Hold</th><th>Status</th><th></th></tr>' +
      openEntries.map(([mint, p]) => {
        const pnl = ((p.currentPrice / p.entryPrice) - 1) * 100;
        const pnlCol = pnl >= 0 ? 'var(--grn)' : 'var(--red)';
        const tp1Tag = p.tp1Hit ? '<span style="color:var(--grn);font-size:9px;margin-left:4px">TP1&#10003;</span>' : '';
        const statusText = p.tp1Hit ? '<span style="font-size:10px;color:var(--grn)">50% sold</span>' : '<span style="font-size:10px;color:var(--t3)">holding</span>';
        return '<tr style="border-bottom:1px solid var(--b1)">' +
          '<td style="padding:4px;font-weight:600;color:var(--t1)">' + (p.symbol || p.name) + tp1Tag + '</td>' +
          '<td style="text-align:center;font-family:monospace">' + fmtPrice(p.entryPrice) + '</td>' +
          '<td style="text-align:center;font-family:monospace">' + fmtPrice(p.currentPrice) + '</td>' +
          '<td style="text-align:center;font-weight:700;color:' + pnlCol + '">' + (pnl >= 0 ? '+' : '') + pnl.toFixed(1) + '%</td>' +
          '<td style="text-align:center;color:var(--t3)">' + fmtHold(Date.now() - p.entryTime) + '</td>' +
          '<td style="text-align:center">' + statusText + '</td>' +
          '<td><button onclick="paperSell(&#39;' + mint + '&#39;,&#39;manual&#39;)" style="background:var(--red);color:#fff;border:none;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer">Sell</button></td></tr>';
      }).join('') + '</table>';
  }

  // Trade history
  const histDiv = document.getElementById('paper-history');
  if (!_paperHistory.length) {
    histDiv.innerHTML = '<div style="color:var(--t3);font-size:12px">No trades yet</div>';
  } else {
    histDiv.innerHTML = '<table style="width:100%;border-collapse:collapse;font-size:12px"><tr style="color:var(--t3);border-bottom:1px solid var(--b1)">' +
      '<th style="text-align:left;padding:4px">Token</th><th>Entry</th><th>Exit</th><th>P&L</th><th>SOL</th><th>Hold</th><th>Exit</th></tr>' +
      _paperHistory.slice(0, 20).map(t => {
        const pnlCol = t.pnl >= 0 ? 'var(--grn)' : 'var(--red)';
        return '<tr style="border-bottom:1px solid var(--b1)">' +
          '<td style="padding:4px;font-weight:600;color:var(--t1)">' + (t.symbol || t.name) + '</td>' +
          '<td style="text-align:center;font-family:monospace">' + fmtPrice(t.entryPrice) + '</td>' +
          '<td style="text-align:center;font-family:monospace">' + fmtPrice(t.exitPrice) + '</td>' +
          '<td style="text-align:center;font-weight:700;color:' + pnlCol + '">' + (t.pnlPct >= 0 ? '+' : '') + t.pnlPct.toFixed(1) + '%</td>' +
          '<td style="text-align:center;color:' + pnlCol + '">' + (t.pnl >= 0 ? '+' : '') + t.pnl.toFixed(4) + '</td>' +
          '<td style="text-align:center;color:var(--t3)">' + fmtHold(t.holdTime) + '</td>' +
          '<td style="text-align:center;color:var(--t3)">' + (t.reason || '') + '</td></tr>';
      }).join('') + '</table>';
  }
}

// Load paper state on page load
paperLoadState();

// ── Onboarding Tour ───────────────────────────────────────────────────────────
(function() {
  const TOUR_KEY = 'soltrader_tour_v1';
  if (localStorage.getItem(TOUR_KEY)) return; // already completed

  const steps = [
    {
      target: '#toggle-btn',
      title: 'Start Your Bot',
      desc: 'This is your <strong>main control</strong>. Click here to start or stop the trading bot. When running, it scans Solana tokens in real-time using your configured strategy.',
      arrow: 'top'
    },
    {
      target: '.tab-btn[data-tab="scanner"]',
      title: 'Scanner Feed',
      desc: 'The <strong>Scanner</strong> shows every new token the bot discovers. You can see scores, market cap, volume, and age — all updating live. This is your discovery hub.',
      arrow: 'top'
    },
    {
      target: '.tab-btn[data-tab="settings"]',
      title: 'Tune Your Strategy',
      desc: '<strong>Settings</strong> let you control buy size, score thresholds, filters, take-profit, stop-loss, and more. Tweak these to match your risk tolerance before going live.',
      arrow: 'top'
    },
    {
      target: '.tab-btn[data-tab="positions"]',
      title: 'Active Positions',
      desc: 'Track all <strong>open trades</strong> here. See entry price, current P&L, hold time, and manually close any position. The bot also auto-sells based on your TP/SL settings.',
      arrow: 'top'
    },
    {
      target: '.tab-btn[data-tab="paper"]',
      title: 'Practice Risk-Free',
      desc: '<strong>Paper Trading</strong> lets you simulate trades with fake SOL. Start a session, and the bot will paper-buy tokens that pass your filters — watch results without risking real money.',
      arrow: 'top'
    },
    {
      target: '.tab-btn[data-tab="pnl"]',
      title: 'Track Performance',
      desc: 'The <strong>P&L tab</strong> shows your equity curve, drawdown, and trade history. Use it to see if your strategy is profitable over time.',
      arrow: 'top'
    },
    {
      target: '#activity-bar',
      title: 'Activity Log',
      desc: 'The <strong>activity bar</strong> at the bottom shows live bot events — scans, buys, sells, rejections. Expand it to see what the bot is doing in real-time.',
      arrow: 'bottom'
    },
    {
      target: null,
      title: 'All Set! \\u{1F680}',
      desc: 'Recommended flow:<br><br><strong>1.</strong> Go to <strong>Settings</strong> and configure your buy size &amp; filters<br><strong>2.</strong> Try <strong>Paper Trading</strong> to test your strategy<br><strong>3.</strong> When confident, hit <strong>Start Bot</strong> to go live<br><br>Good luck, and trade safe!',
      arrow: 'none',
      center: true
    }
  ];

  let currentStep = 0;
  let overlayEl, highlightEl, tooltipEl;

  function createElements() {
    overlayEl = document.createElement('div');
    overlayEl.className = 'tour-overlay';
    overlayEl.onclick = function() {}; // block clicks
    document.body.appendChild(overlayEl);

    highlightEl = document.createElement('div');
    highlightEl.className = 'tour-highlight';
    document.body.appendChild(highlightEl);

    tooltipEl = document.createElement('div');
    tooltipEl.className = 'tour-tooltip';
    document.body.appendChild(tooltipEl);
  }

  function positionTooltip(step) {
    const s = steps[step];

    // Progress dots
    var dots = '';
    for (var i = 0; i < steps.length; i++) {
      var cls = 'tour-dot';
      if (i < step) cls += ' done';
      if (i === step) cls += ' active';
      dots += '<div class="' + cls + '"></div>';
    }

    var isLast = step === steps.length - 1;
    tooltipEl.innerHTML =
      '<div class="tour-title"><span class="tour-step-num">' + (step + 1) + '</span>' + s.title + '</div>' +
      '<div class="tour-desc">' + s.desc + '</div>' +
      '<div class="tour-actions">' +
        '<div class="tour-progress">' + dots + '</div>' +
        '<div style="display:flex;gap:8px">' +
          (isLast ? '' : '<button class="tour-btn tour-btn-skip" onclick="window._tourSkip()">Skip Tour</button>') +
          (isLast
            ? '<button class="tour-btn tour-btn-done" onclick="window._tourDone()">Let\\x27s Go!</button>'
            : '<button class="tour-btn tour-btn-next" onclick="window._tourNext()">Next →</button>') +
        '</div>' +
      '</div>';

    tooltipEl.className = 'tour-tooltip';

    if (s.center || !s.target) {
      // Centered overlay — no target element
      highlightEl.style.display = 'none';
      tooltipEl.style.top = '50%';
      tooltipEl.style.left = '50%';
      tooltipEl.style.transform = 'translate(-50%, -50%)';
      tooltipEl.style.maxWidth = '400px';
      return;
    }

    tooltipEl.style.transform = '';
    highlightEl.style.display = 'block';

    var el = document.querySelector(s.target);
    if (!el) { nextStep(); return; }

    // Scroll into view
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });

    setTimeout(function() {
      var rect = el.getBoundingClientRect();
      var pad = 8;
      highlightEl.style.top = (rect.top + window.scrollY - pad) + 'px';
      highlightEl.style.left = (rect.left + window.scrollX - pad) + 'px';
      highlightEl.style.width = (rect.width + pad * 2) + 'px';
      highlightEl.style.height = (rect.height + pad * 2) + 'px';

      tooltipEl.classList.add('arrow-' + (s.arrow || 'top'));

      if (s.arrow === 'bottom') {
        tooltipEl.style.bottom = (window.innerHeight - rect.top - window.scrollY + 16) + 'px';
        tooltipEl.style.top = 'auto';
        tooltipEl.style.left = Math.max(16, rect.left + window.scrollX - 20) + 'px';
      } else {
        tooltipEl.style.top = (rect.bottom + window.scrollY + 16) + 'px';
        tooltipEl.style.bottom = 'auto';
        tooltipEl.style.left = Math.max(16, Math.min(rect.left + window.scrollX - 20, window.innerWidth - 370)) + 'px';
      }
    }, 350);
  }

  function nextStep() {
    currentStep++;
    if (currentStep >= steps.length) { finishTour(); return; }
    positionTooltip(currentStep);
  }

  function finishTour() {
    localStorage.setItem(TOUR_KEY, '1');
    if (overlayEl) overlayEl.remove();
    if (highlightEl) highlightEl.remove();
    if (tooltipEl) tooltipEl.remove();
  }

  window._tourNext = nextStep;
  window._tourSkip = finishTour;
  window._tourDone = finishTour;

  // Start tour after DOM is ready and a brief delay
  document.addEventListener('DOMContentLoaded', function() {
    setTimeout(function() {
      createElements();
      positionTooltip(0);
    }, 1200);
  });
})();

</script>
  <div style="max-width:1520px;margin:40px auto 0;padding:20px;text-align:center;border-top:1px solid rgba(255,255,255,.06)">
    <p style="font-size:11px;color:var(--t3);line-height:1.6;max-width:800px;margin:0 auto">
      <strong>Risk Disclaimer:</strong> SolTrader is an automated trading tool. Cryptocurrency trading carries substantial risk of loss.
      Past performance, including simulated shadow trading results, does not guarantee future results.
      Shadow trading metrics do not account for slippage, transaction fees, or real execution constraints.
      This is not financial advice. Only trade with funds you can afford to lose.
    </p>
    <p style="font-size:10px;color:var(--t3);margin-top:8px">&copy; 2026 SolTrader. All rights reserved.</p>
  </div>
</body></html>
"""


# ── Admin Page ─────────────────────────────────────────────────────────────────
ADMIN_HTML = _CSS + """
<style>
.adm-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:24px}
.adm-card{background:var(--card);border:1px solid var(--bdr);border-radius:14px;padding:18px 16px;text-align:center}
.adm-card .val{font-family:'Space Grotesk','Manrope',sans-serif;font-size:28px;font-weight:800;letter-spacing:-.5px}
.adm-card .lbl{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--t3);margin-top:4px}
.adm-card .sub{font-size:11px;color:var(--t3);margin-top:2px}
.adm-tbl{width:100%;border-collapse:collapse;font-size:12px}
.adm-tbl th{text-align:left;padding:8px 10px;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--t3);border-bottom:1px solid var(--bdr)}
.adm-tbl td{padding:8px 10px;border-bottom:1px solid rgba(255,255,255,.04);color:var(--t2)}
.adm-tbl tr:hover td{background:rgba(47,107,255,.04)}
.adm-badge{display:inline-block;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:700;letter-spacing:.05em;text-transform:uppercase}
.adm-badge.free{background:rgba(100,116,139,.15);color:#94a3b8}.adm-badge.trial{background:rgba(217,119,6,.15);color:#fbbf24}
.adm-badge.basic{background:rgba(47,107,255,.15);color:#60a5fa}.adm-badge.pro{background:rgba(20,199,132,.15);color:#14c784}
.adm-badge.elite{background:rgba(168,85,247,.15);color:#c084fc}
.adm-dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:4px}
.adm-dot.on{background:#14c784}.adm-dot.off{background:#475569}
.adm-section{background:var(--card);border:1px solid var(--bdr);border-radius:16px;padding:20px;margin-bottom:16px}
.adm-section-title{font-size:14px;font-weight:800;color:var(--t1);margin-bottom:14px;display:flex;align-items:center;gap:8px}
.adm-pnl-pos{color:#14c784}.adm-pnl-neg{color:#dc2626}.adm-pnl-zero{color:var(--t3)}
.adm-tabs{display:flex;gap:0;border-bottom:1px solid var(--bdr);margin-bottom:16px}
.adm-tab{padding:10px 18px;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--t3);cursor:pointer;border-bottom:2px solid transparent}
.adm-tab.active{color:#14c784;border-bottom-color:#14c784}
.adm-tab:hover{color:var(--t1)}
</style>
</head><body>

<nav class="nav">
  <a href="/" class="logo"><div class="logo-mark">S</div>SolTrader</a>
  <div class="nav-r">
    <span class="badge bg-gold" style="font-size:11px;padding:4px 12px">Admin</span>
    <a href="/dashboard">My Dashboard</a>
    <a href="/logout" style="color:var(--t3)!important">Sign Out</a>
  </div>
</nav>

<div class="wrap">
  <div style="margin-bottom:24px">
    <div style="font-size:10px;letter-spacing:.15em;text-transform:uppercase;color:var(--t3);margin-bottom:4px">Operator Console</div>
    <div class="page-title" style="font-size:28px;font-weight:800">Revenue & Users</div>
    <div style="font-size:12px;color:var(--t3);margin-top:4px">Last refreshed: <span id="adm-refresh-time">loading...</span></div>
  </div>

  <!-- Revenue Cards -->
  <div class="adm-grid" id="adm-stats">
    <div class="adm-card"><div class="val c-grn" id="adm-mrr">$0</div><div class="lbl">Monthly Recurring</div><div class="sub" id="adm-mrr-sub">0 subscribers</div></div>
    <div class="adm-card"><div class="val" style="color:var(--gold2)" id="adm-fees-total">0 SOL</div><div class="lbl">Perf Fees Earned</div><div class="sub" id="adm-fees-sub">0 collected, 0 pending</div></div>
    <div class="adm-card"><div class="val" style="color:var(--blue2)" id="adm-users">0</div><div class="lbl">Total Users</div><div class="sub" id="adm-users-sub">0 active bots</div></div>
    <div class="adm-card"><div class="val c-grn" id="adm-active">0</div><div class="lbl">Active Bots</div><div class="sub">running right now</div></div>
  </div>

  <!-- Plan Breakdown -->
  <div class="adm-grid" style="grid-template-columns:repeat(4,1fr)">
    <div class="adm-card"><div class="val" style="font-size:22px;color:#94a3b8" id="adm-plan-free">0</div><div class="lbl">Free</div><div class="sub">$0/mo each</div></div>
    <div class="adm-card"><div class="val" style="font-size:22px;color:#60a5fa" id="adm-plan-basic">0</div><div class="lbl">Basic</div><div class="sub">$49/mo each</div></div>
    <div class="adm-card"><div class="val" style="font-size:22px;color:#14c784" id="adm-plan-pro">0</div><div class="lbl">Pro</div><div class="sub">$99/mo each</div></div>
    <div class="adm-card"><div class="val" style="font-size:22px;color:#c084fc" id="adm-plan-elite">0</div><div class="lbl">Elite</div><div class="sub">$199/mo each</div></div>
  </div>

  <!-- Tabs: Users | Fees | Tools -->
  <div class="adm-tabs">
    <div class="adm-tab active" onclick="switchAdminTab('users',this)">Users</div>
    <div class="adm-tab" onclick="switchAdminTab('fees',this)">Performance Fees</div>
    <div class="adm-tab" onclick="switchAdminTab('tools',this)">Tools</div>
  </div>

  <!-- Users Tab -->
  <div id="adm-tab-users" class="adm-section">
    <div class="adm-section-title">All Users <span style="font-size:11px;color:var(--t3);font-weight:400" id="adm-user-count-label"></span></div>
    <div style="overflow-x:auto;max-height:600px;overflow-y:auto">
      <table class="adm-tbl" id="adm-user-table">
        <thead><tr>
          <th>Status</th><th>Email</th><th>Plan</th><th>Change Plan</th><th>Stripe</th><th>Trades</th><th>P&L (SOL)</th><th>Win Rate</th><th>Joined</th><th>Last Active</th>
        </tr></thead>
        <tbody id="adm-user-rows"><tr><td colspan="10" style="text-align:center;color:var(--t3);padding:30px">Loading users...</td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- Fees Tab -->
  <div id="adm-tab-fees" class="adm-section" style="display:none">
    <div class="adm-section-title">Recent Performance Fees</div>
    <div style="overflow-x:auto;max-height:500px;overflow-y:auto">
      <table class="adm-tbl">
        <thead><tr><th>User</th><th>Session PnL</th><th>Fee Owed</th><th>Status</th><th>Date</th></tr></thead>
        <tbody id="adm-fee-rows"><tr><td colspan="5" style="text-align:center;color:var(--t3);padding:30px">Loading fees...</td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- Tools Tab -->
  <div id="adm-tab-tools" style="display:none">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
      <div class="adm-section">
        <div class="adm-section-title">Dev Blacklist</div>
        <div id="blacklist-tbl"><div style="font-size:13px;color:var(--t3)">Loading...</div></div>
        <div style="display:flex;gap:8px;margin-top:12px">
          <input class="finput" id="bl-wallet" placeholder="Paste dev wallet address" style="flex:1">
          <button class="btn btn-danger" onclick="addBlacklist()">Block</button>
        </div>
      </div>
      <div class="adm-section">
        <div class="adm-section-title">Database Cleanup</div>
        <p style="font-size:12px;color:var(--t3);margin-bottom:12px">Delete old shadow decisions, market events, and expired data to free Postgres disk.</p>
        <button class="btn btn-primary" onclick="runCleanup()">Run Cleanup Now</button>
        <div id="cleanup-result" style="margin-top:8px;font-size:11px;color:var(--t3)"></div>
      </div>
    </div>
  </div>

</div>

<script>
function switchAdminTab(tab, btn) {
  document.querySelectorAll('.adm-tab').forEach(t => t.classList.remove('active'));
  if (btn) btn.classList.add('active');
  ['users','fees','tools'].forEach(t => {
    const el = document.getElementById('adm-tab-' + t);
    if (el) el.style.display = t === tab ? '' : 'none';
  });
}

function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleDateString('en-US', {month:'short',day:'numeric'}) + ' ' + d.toLocaleTimeString('en-US', {hour:'2-digit',minute:'2-digit'});
}

async function loadAdminData() {
  try {
    const data = await fetch('/api/admin/revenue').then(r => r.json());

    // Revenue cards
    document.getElementById('adm-mrr').textContent = '$' + data.mrr.toLocaleString();
    const totalSubs = Object.values(data.subscriptions || {}).reduce((a,b) => a + b, 0);
    document.getElementById('adm-mrr-sub').textContent = totalSubs + ' active subscriber' + (totalSubs !== 1 ? 's' : '');

    document.getElementById('adm-fees-total').textContent = data.fees.total.toFixed(4) + ' SOL';
    document.getElementById('adm-fees-sub').textContent = data.fees.collected.toFixed(4) + ' collected, ' + data.fees.pending.toFixed(4) + ' pending';

    document.getElementById('adm-users').textContent = data.total_users;
    document.getElementById('adm-users-sub').textContent = data.active_bots + ' active bot' + (data.active_bots !== 1 ? 's' : '');
    document.getElementById('adm-active').textContent = data.active_bots;

    // Plan breakdown
    const planCounts = {free: 0, basic: 0, pro: 0, elite: 0, trial: 0};
    (data.users || []).forEach(u => { planCounts[u.plan] = (planCounts[u.plan] || 0) + 1; });
    document.getElementById('adm-plan-free').textContent = (planCounts.free || 0) + (planCounts.trial || 0);
    document.getElementById('adm-plan-basic').textContent = planCounts.basic || 0;
    document.getElementById('adm-plan-pro').textContent = planCounts.pro || 0;
    document.getElementById('adm-plan-elite').textContent = planCounts.elite || 0;

    // Users table
    const users = data.users || [];
    document.getElementById('adm-user-count-label').textContent = '(' + users.length + ' total)';
    document.getElementById('adm-user-rows').innerHTML = users.map(u => {
      const pnl = u.total_pnl_sol || 0;
      const pnlClass = pnl > 0 ? 'adm-pnl-pos' : pnl < 0 ? 'adm-pnl-neg' : 'adm-pnl-zero';
      const wr = (u.wins + u.losses) > 0 ? ((u.wins / (u.wins + u.losses)) * 100).toFixed(1) + '%' : '—';
      const plans = ['free','trial','basic','pro','elite'];
      const planSelect = '<select class="finput" style="font-size:11px;padding:3px 6px;min-width:80px" onchange="changePlan(' + u.id + ',this.value,this)" data-original="' + u.plan + '">' +
        plans.map(p => '<option value="' + p + '"' + (p === u.plan ? ' selected' : '') + '>' + p + '</option>').join('') +
        '</select>';
      return '<tr>' +
        '<td><span class="adm-dot ' + (u.is_active ? 'on' : 'off') + '"></span>' + (u.is_active ? 'Live' : 'Off') + '</td>' +
        '<td style="font-weight:600;color:var(--t1)">' + u.email + '</td>' +
        '<td><span class="adm-badge ' + u.plan + '">' + u.plan + '</span></td>' +
        '<td>' + planSelect + '</td>' +
        '<td>' + (u.has_stripe ? '<span style="color:#14c784">Paying</span>' : '<span style="color:var(--t3)">No</span>') + '</td>' +
        '<td>' + u.trade_count + '</td>' +
        '<td class="' + pnlClass + '" style="font-weight:700;font-family:monospace">' + (pnl > 0 ? '+' : '') + pnl.toFixed(4) + '</td>' +
        '<td>' + wr + '</td>' +
        '<td style="font-size:11px;color:var(--t3)">' + fmtDate(u.created_at) + '</td>' +
        '<td style="font-size:11px;color:var(--t3)">' + fmtDate(u.last_active) + '</td>' +
      '</tr>';
    }).join('') || '<tr><td colspan="10" style="text-align:center;color:var(--t3);padding:20px">No users yet</td></tr>';

    // Fees table
    const fees = data.recent_fees || [];
    document.getElementById('adm-fee-rows').innerHTML = fees.map(f => {
      return '<tr>' +
        '<td style="font-weight:600;color:var(--t1)">' + (f.email || 'User #' + f.user_id) + '</td>' +
        '<td style="font-family:monospace">' + (f.pnl_sol > 0 ? '+' : '') + f.pnl_sol.toFixed(4) + ' SOL</td>' +
        '<td style="font-family:monospace;color:var(--gold2);font-weight:700">' + f.fee_sol.toFixed(4) + ' SOL</td>' +
        '<td>' + (f.charged ? '<span style="color:#14c784">Collected</span>' : '<span style="color:var(--gold)">Pending</span>') + '</td>' +
        '<td style="font-size:11px;color:var(--t3)">' + fmtDate(f.created_at) + '</td>' +
      '</tr>';
    }).join('') || '<tr><td colspan="5" style="text-align:center;color:var(--t3);padding:20px">No fees recorded yet</td></tr>';

    document.getElementById('adm-refresh-time').textContent = new Date().toLocaleTimeString();
  } catch(e) {
    console.error('Admin data load error:', e);
  }
}

async function changePlan(userId, newPlan, selectEl) {
  const original = selectEl.getAttribute('data-original');
  if (newPlan === original) return;
  if (!confirm('Change user #' + userId + ' to ' + newPlan.toUpperCase() + ' plan?')) {
    selectEl.value = original;
    return;
  }
  selectEl.disabled = true;
  selectEl.style.opacity = '0.5';
  try {
    const r = await fetch('/api/admin/change-plan', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({user_id: userId, plan: newPlan})
    }).then(r => r.json());
    if (r.ok) {
      selectEl.setAttribute('data-original', newPlan);
      selectEl.style.background = 'rgba(20,199,132,.15)';
      setTimeout(() => { selectEl.style.background = ''; }, 1500);
      loadAdminData();
    } else {
      alert('Failed: ' + (r.msg || 'Unknown error'));
      selectEl.value = original;
    }
  } catch(e) {
    alert('Error changing plan');
    selectEl.value = original;
  }
  selectEl.disabled = false;
  selectEl.style.opacity = '1';
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
    '<div style="font-size:11px;color:var(--t2);padding:4px 0;border-bottom:1px solid var(--bdr);font-family:monospace">' +
      w.dev_wallet + ' <span style="color:var(--t3)"> — ' + w.reason + '</span></div>'
  ).join('');
}

async function runCleanup() {
  const el = document.getElementById('cleanup-result');
  el.textContent = 'Running...';
  const r = await fetch('/api/db-cleanup', {method:'POST'}).then(r=>r.json()).catch(()=>({ok:false}));
  el.textContent = r.ok ? 'Cleanup complete: ' + JSON.stringify(r.freed || {}) : 'Cleanup failed';
}

loadAdminData();
loadBlacklist();
setInterval(loadAdminData, 15000);
</script>
</body></html>
"""

if __name__ == "__main__":
    ensure_background_workers_started()
    print(f"\n  SolTrader Platform → http://localhost:5000")
    print(f"  Admin account: {ADMIN_EMAIL}")
    print(f"  Database: {DATABASE_URL}\n")
    port = int(os.getenv("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
