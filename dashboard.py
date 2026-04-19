"""
Run with:  python dashboard.py
Then open: http://localhost:5000
"""

import threading
import time
import os
import base64
import json
import secrets
import requests
import base58
import websocket
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from wallet_scanner import start_scanner, get_copy_list, get_copy_addresses, stop_scanner
from copy_engine import (
    start_engine, stop_engine, get_mode, set_mode, get_trades, CopyMode,
)
from flask import Flask, Response, jsonify, request


def load_environment():
    env_paths = [
        os.path.join(os.path.dirname(__file__), ".env"),
        os.path.join(os.path.expanduser("~"), "Desktop", ".env"),
    ]
    for env_path in env_paths:
        if os.path.exists(env_path):
            load_dotenv(dotenv_path=env_path, override=False)


def require_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} environment variable is required.")
    return value


load_environment()

PRIVATE_KEY = require_env("PRIVATE_KEY")
HELIUS_RPC  = require_env("HELIUS_RPC")
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "5000"))
DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "").strip()
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip()

keypair = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY))
wallet  = str(keypair.pubkey())

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
PUMP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://pump.fun/",
    "Origin": "https://pump.fun",
}
SOL_MINT = "So11111111111111111111111111111111111111112"
# Pump.fun bonding curve program — holding mint authority here is SAFE (not a rug)
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# ── Preset configurations ──────────────────────────────────────────────────────
PRESETS = {
    "steady": {
        "label":         "Steady Profit",
        "max_buy_sol":   0.03,
        "tp1_mult":      1.5,
        "tp2_mult":      3.0,
        "trail_pct":     0.20,
        "stop_loss":     0.75,   # -25%
        "max_age_min":   30,
        "time_stop_min": 30,
        "min_vol_mc":    0.5,
        "min_liq":       10000,
        "min_mc":        5000,
        "max_mc":        100000,
        "min_vol":       3000,
        "min_score":     30,
        "priority_fee":  10000,  # lamports
        "anti_rug":      True,
        "pump_scan":     True,
    },
    "max": {
        "label":         "Max Profit",
        "max_buy_sol":   0.10,
        "tp1_mult":      2.0,
        "tp2_mult":      10.0,
        "trail_pct":     0.30,
        "stop_loss":     0.60,   # -40%  (wider to survive volatility)
        "max_age_min":   15,     # ultra-early only
        "time_stop_min": 60,
        "min_vol_mc":    0.3,
        "min_liq":       5000,
        "min_mc":        2000,
        "max_mc":        200000,
        "min_vol":       500,
        "min_score":     15,
        "priority_fee":  100000, # pay more to land first
        "anti_rug":      True,
        "pump_scan":     True,
    },
}

# ── Live settings (start with steady) ─────────────────────────────────────────
settings = dict(PRESETS["steady"])

# ── Shared state ───────────────────────────────────────────────────────────────
seen_tokens  = set()
positions    = {}   # {mint: {name, entry_price, peak_price, timestamp, tp1_hit, buy_reason, entry_sol, source}}
trade_log    = []
sol_balance  = 0.0
bot_running  = True
stats        = {"wins": 0, "losses": 0, "total_pnl_sol": 0.0}
trade_history= []   # [{name, mint, entry_sol, pnl_sol, pnl_pct, reason, close_reason, ts}]

# ── Budget / risk controls ──────────────────────────────────────────────────────
budget_pct      = 1.0   # fraction of balance to use (0.1 – 1.0)
loss_limit_sol  = 0.0   # if >0, auto-pause bot when session loss exceeds this
_session_start_balance = None   # set on first balance refresh

def log(msg):
    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    trade_log.insert(0, entry)
    if len(trade_log) > 500:
        trade_log.pop()
    print(entry)

# ── Solana helpers ─────────────────────────────────────────────────────────────
def refresh_balance():
    global sol_balance, bot_running, _session_start_balance
    try:
        r = requests.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]
        }, timeout=5)
        sol_balance = r.json().get("result", {}).get("value", 0) / 1e9
        if _session_start_balance is None:
            _session_start_balance = sol_balance
        # Loss-limit auto-shutoff
        if loss_limit_sol > 0 and bot_running:
            session_loss = _session_start_balance - sol_balance
            if session_loss >= loss_limit_sol:
                bot_running = False
                log(f"LOSS LIMIT HIT — session loss {session_loss:.4f} SOL >= limit {loss_limit_sol:.4f} SOL — bot PAUSED")
    except:
        pass

def sign_and_send(swap_tx_b64):
    raw    = base64.b64decode(swap_tx_b64)
    tx     = VersionedTransaction.from_bytes(raw)
    signed = VersionedTransaction(tx.message, [keypair])
    enc    = base64.b64encode(bytes(signed)).decode()
    r = requests.post(HELIUS_RPC, json={
        "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
        "params": [enc, {"encoding": "base64", "skipPreflight": False,
                         "preflightCommitment": "confirmed", "maxRetries": 3}]
    }, timeout=15)
    res = r.json()
    if "result" in res:
        return res["result"]
    log(f"RPC error: {res.get('error')}")
    return None

def jupiter_quote(input_mint, output_mint, amount_lamports):
    r = requests.get(
        f"https://lite-api.jup.ag/swap/v1/quote"
        f"?inputMint={input_mint}&outputMint={output_mint}"
        f"&amount={amount_lamports}&slippageBps=1500",
        timeout=10
    ).json()
    return None if "error" in r else r

def jupiter_swap(quote, priority_fee_lamports=10000):
    r = requests.post("https://lite-api.jup.ag/swap/v1/swap", json={
        "quoteResponse":            quote,
        "userPublicKey":            wallet,
        "wrapAndUnwrapSol":         True,
        "prioritizationFeeLamports": int(priority_fee_lamports),
    }, timeout=10).json()
    return r.get("swapTransaction")

def get_token_balance(mint):
    r = requests.post(HELIUS_RPC, json={
        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
        "params": [wallet, {"mint": mint}, {"encoding": "jsonParsed"}]
    }, timeout=5)
    accounts = r.json().get("result", {}).get("value", [])
    if not accounts:
        return 0
    return int(accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])

def get_token_price(mint):
    try:
        pairs = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            headers=HEADERS, timeout=5
        ).json().get("pairs")
        if pairs:
            return float(pairs[0].get("priceUsd") or 0)
    except:
        pass
    return None

# ── Anti-rug checks ────────────────────────────────────────────────────────────
def is_safe_token(mint):
    """
    Returns True if token passes rug checks:
    - Mint authority is revoked OR held by pump.fun bonding curve (safe)
    - Freeze authority is revoked (can't freeze your wallet)
    - Top holder owns < 25% of supply
    """
    try:
        r = requests.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [mint, {"encoding": "jsonParsed"}]
        }, timeout=5).json()
        info = r.get("result", {}).get("value", {})
        if not info:
            return True  # can't verify — allow through rather than block everything
        parsed = info.get("data", {}).get("parsed", {}).get("info", {})
        mint_auth   = parsed.get("mintAuthority")
        freeze_auth = parsed.get("freezeAuthority")

        # Mint authority: only block if held by an unknown wallet (not pump.fun program)
        if mint_auth is not None and mint_auth != PUMP_FUN_PROGRAM:
            return False

        # Freeze authority: must be revoked
        if freeze_auth is not None:
            return False

        # Top holder concentration check
        r2 = requests.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts",
            "params": [mint]
        }, timeout=5).json()
        accounts = r2.get("result", {}).get("value", [])
        if accounts:
            supply_r = requests.post(HELIUS_RPC, json={
                "jsonrpc": "2.0", "id": 1, "method": "getTokenSupply",
                "params": [mint]
            }, timeout=5).json()
            total = float(supply_r.get("result", {}).get("value", {}).get("amount", 1) or 1)
            top   = float(accounts[0].get("amount", 0))
            if total > 0 and (top / total) > 0.25:
                return False  # top holder owns >25%

        return True
    except:
        return True  # on RPC error, allow through

# ── Market data / scanners ─────────────────────────────────────────────────────
def get_dex_info(mint):
    try:
        pairs = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            headers=HEADERS, timeout=5
        ).json().get("pairs")
        if not pairs:
            return None
        p = pairs[0]
        created_at = p.get("pairCreatedAt")
        age_min = (time.time() * 1000 - created_at) / 60000 if created_at else 9999
        return {
            "name":    p.get("baseToken", {}).get("name", "Unknown"),
            "symbol":  p.get("baseToken", {}).get("symbol", "?"),
            "price":   float(p.get("priceUsd") or 0),
            "mc":      p.get("marketCap", 0) or 0,
            "vol":     (p.get("volume") or {}).get("h24", 0) or 0,
            "liq":     (p.get("liquidity") or {}).get("usd", 0) or 0,
            "age_min": age_min,
            "change":  (p.get("priceChange") or {}).get("h1", 0) or 0,
        }
    except:
        return None

def scan_dexscreener():
    """Scan DexScreener token profiles feed for new Solana tokens."""
    try:
        tokens = requests.get(
            "https://api.dexscreener.com/token-profiles/latest/v1",
            headers=HEADERS, timeout=10
        ).json()
        for t in (tokens if isinstance(tokens, list) else []):
            if t.get("chainId") == "solana":
                yield t.get("tokenAddress")
    except Exception as e:
        log(f"DexScreener scan error: {e}")

# ── Helius WebSocket — on-chain pump.fun monitor ───────────────────────────────
# Watches the pump.fun program directly via our own Helius RPC WebSocket.
# No third-party API needed — fires on every new token creation transaction.
pump_ws_queue = []

def _fetch_mint_from_tx(signature):
    """Pull the new mint address out of a pump.fun create transaction."""
    try:
        r = requests.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getTransaction",
            "params": [signature, {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
                "commitment": "confirmed"
            }]
        }, timeout=8).json()
        tx = r.get("result")
        if not tx:
            return
        # New mint shows up in postTokenBalances
        for bal in (tx.get("meta") or {}).get("postTokenBalances", []):
            mint = bal.get("mint")
            if mint and mint not in seen_tokens:
                pump_ws_queue.append(mint)
                return
    except:
        pass

def _pump_ws_thread():
    # WebSocket disabled — DexScreener polling covers signal detection
    log("Pump scanner: using DexScreener feed (WebSocket disabled)")
    return

# ── Trade logic ────────────────────────────────────────────────────────────────
def evaluate_and_buy(mint, name, price, mc, vol, liq, age_min, change, source="dex"):
    if mint in positions or mint in seen_tokens:
        return
    seen_tokens.add(mint)

    s = settings
    vol_mc = vol / mc if mc > 0 else 0

    # Filter checks
    if not (s["min_mc"] <= mc <= s["max_mc"]):
        return
    if liq < s["min_liq"]:
        return
    if age_min > s["max_age_min"]:
        return
    if vol_mc < s["min_vol_mc"] and vol < 5000:
        return
    if change < 0 and source != "grad":
        return  # skip downtrending (except graduation plays)

    # Build human-readable buy reason
    reasons = []
    if change >= 20:   reasons.append(f"+{change:.0f}% momentum")
    if vol_mc >= 1.0:  reasons.append("high vol/MC")
    if age_min <= 5:   reasons.append("ultra-fresh")
    elif age_min <= 15: reasons.append("early entry")
    if mc < 10000:     reasons.append("micro-cap")
    buy_reason = ", ".join(reasons) if reasons else f"{source.upper()} signal"

    log(f"SIGNAL [{source.upper()}] {name} | MC:${mc:,.0f} Vol:${vol:,.0f} Liq:${liq:,.0f} | Age:{age_min:.0f}m {change:+.0f}% | {buy_reason}")

    # Anti-rug check
    if s.get("anti_rug"):
        if not is_safe_token(mint):
            log(f"  RUG RISK — skipping {name} (mint/freeze authority or top holder)")
            return

    buy_token(mint, name, price, source=source, buy_reason=buy_reason)

def buy_token(mint, name, price, source="dex", buy_reason="signal"):
    global budget_pct
    s = settings
    spend_sol = round(sol_balance * budget_pct * s["max_buy_sol"] / max(s["max_buy_sol"], 0.001), 6)
    spend_sol = min(spend_sol, s["max_buy_sol"])
    spend_sol = max(spend_sol, 0.001)
    if sol_balance < spend_sol + 0.01:
        log(f"Low balance ({sol_balance:.4f} SOL) — skip {name}")
        return

    lamports = int(spend_sol * 1e9)
    quote = jupiter_quote(SOL_MINT, mint, lamports)
    if not quote:
        log(f"No buy route for {name}")
        return

    swap_tx = jupiter_swap(quote, s["priority_fee"])
    if not swap_tx:
        log(f"No swap tx for {name}")
        return

    sig = sign_and_send(swap_tx)
    if sig:
        positions[mint] = {
            "name":        name,
            "entry_price": price,
            "peak_price":  price,
            "timestamp":   time.time(),
            "tp1_hit":     False,
            "entry_sol":   spend_sol,
            "source":      source,
            "buy_reason":  buy_reason,
        }
        log(f"BUY  {name} @ ${price:.8f} | {spend_sol} SOL | {buy_reason} | solscan.io/tx/{sig}")
        refresh_balance()

def sell_partial(mint, pct, reason):
    pos = positions.get(mint)
    if not pos:
        return False
    try:
        total = get_token_balance(mint)
        if total == 0:
            log(f"Zero balance — removing {pos['name']}")
            del positions[mint]
            return False

        amount = int(total * pct)
        if amount == 0:
            return False

        quote = jupiter_quote(mint, SOL_MINT, amount)
        if not quote:
            log(f"No sell route for {pos['name']}")
            return False

        swap_tx = jupiter_swap(quote, settings["priority_fee"])
        if not swap_tx:
            return False

        sig = sign_and_send(swap_tx)
        if sig:
            cur = get_token_price(mint)
            pnl_pct = (cur / pos["entry_price"] - 1) * 100 if cur and pos["entry_price"] else 0
            pnl_sol = pos["entry_sol"] * pct * (pnl_pct / 100)
            log(f"SELL {pos['name']} {int(pct*100)}% — {reason} | PnL: {pnl_pct:+.1f}% ({pnl_sol:+.4f} SOL) | solscan.io/tx/{sig}")

            if pct >= 1.0:
                won = pnl_pct >= 0
                if won:
                    stats["wins"] += 1
                else:
                    stats["losses"] += 1
                stats["total_pnl_sol"] += pnl_sol
                trade_history.insert(0, {
                    "name":         pos["name"],
                    "mint":         mint,
                    "entry_sol":    pos["entry_sol"],
                    "pnl_sol":      round(pnl_sol, 4),
                    "pnl_pct":      round(pnl_pct, 1),
                    "buy_reason":   pos.get("buy_reason", "signal"),
                    "close_reason": reason,
                    "won":          won,
                    "ts":           time.strftime("%H:%M:%S"),
                })
                if len(trade_history) > 200:
                    trade_history.pop()
                del positions[mint]
            else:
                positions[mint]["tp1_hit"] = True
                stats["wins"] += 1
                stats["total_pnl_sol"] += pnl_sol

            refresh_balance()
            return True
    except Exception as e:
        log(f"Sell error {pos['name']}: {e}")
    return False

# ── Position monitor ───────────────────────────────────────────────────────────
def check_positions():
    s = settings
    for mint in list(positions.keys()):
        pos = positions[mint]
        cur = get_token_price(mint)
        if not cur or not pos["entry_price"]:
            continue

        if cur > pos["peak_price"]:
            positions[mint]["peak_price"] = cur

        ratio      = cur / pos["entry_price"]
        peak_ratio = pos["peak_price"] / pos["entry_price"]
        age_min    = (time.time() - pos["timestamp"]) / 60
        trail_line = pos["peak_price"] * (1 - s["trail_pct"])

        # Hard stop
        if ratio <= s["stop_loss"]:
            log(f"STOP {pos['name']} {ratio:.2f}x after {age_min:.0f}m")
            sell_partial(mint, 1.0, f"SL {ratio:.2f}x")
            continue

        # Time stop (flat / losing after X min)
        if age_min >= s["time_stop_min"] and ratio < 1.10:
            log(f"TIME {pos['name']} {ratio:.2f}x after {age_min:.0f}m")
            sell_partial(mint, 1.0, f"TIME {age_min:.0f}m")
            continue

        # TP1 — sell half
        if not pos["tp1_hit"] and ratio >= s["tp1_mult"]:
            log(f"TP1  {pos['name']} {ratio:.2f}x")
            sell_partial(mint, 0.5, f"TP1 {ratio:.2f}x")
            continue

        # TP2 — sell rest
        if pos["tp1_hit"] and ratio >= s["tp2_mult"]:
            log(f"TP2  {pos['name']} {ratio:.2f}x")
            sell_partial(mint, 1.0, f"TP2 {ratio:.2f}x")
            continue

        # Trailing stop (activates after TP1 or after peak > 1.3x)
        if (pos["tp1_hit"] or peak_ratio >= 1.3) and cur < trail_line:
            log(f"TRAIL {pos['name']} peak={peak_ratio:.2f}x now={ratio:.2f}x")
            sell_partial(mint, 1.0, f"TRAIL {ratio:.2f}x")
            continue

        pnl = (ratio - 1) * 100
        log(f"HOLD {pos['name']} {ratio:.2f}x ({pnl:+.1f}%) peak={peak_ratio:.2f}x {age_min:.0f}m")

# ── Main scanner ───────────────────────────────────────────────────────────────
def run_scan():
    # 1. DexScreener feed (polled every 10s)
    for mint in scan_dexscreener():
        if not mint or mint in seen_tokens:
            continue
        info = get_dex_info(mint)
        if info:
            evaluate_and_buy(
                mint, info["name"], info["price"],
                info["mc"], info["vol"], info["liq"],
                info["age_min"], info["change"], source="dex"
            )

    # 2. Pump.fun WebSocket queue (real-time, no polling needed)
    if settings.get("pump_scan") and pump_ws_queue:
        # Drain the queue collected since last loop
        batch = pump_ws_queue[:]
        pump_ws_queue.clear()
        for mint in batch:
            if mint in seen_tokens:
                continue
            info = get_dex_info(mint)
            if info and info["price"]:
                evaluate_and_buy(
                    mint, info["name"], info["price"],
                    info["mc"], info["vol"], info["liq"],
                    info["age_min"], info["change"], source="pump"
                )
            else:
                # Not on DexScreener yet — brand new, skip (no price data to trade)
                seen_tokens.add(mint)

# ── Bot loop ───────────────────────────────────────────────────────────────────
def bot_loop():
    log(f"Wallet: {wallet}")
    refresh_balance()
    log(f"Balance: {sol_balance:.4f} SOL | Ready")
    while True:
        try:
            if not bot_running:
                time.sleep(2)
                continue
            if positions:
                check_positions()
            run_scan()
            time.sleep(10)
        except Exception as e:
            log(f"Loop error: {e}")
            time.sleep(10)

def _is_loopback_request():
    remote = request.remote_addr or ""
    return remote in {"127.0.0.1", "::1", "localhost"}


def _authorized_dashboard_request():
    if DASHBOARD_USERNAME and DASHBOARD_PASSWORD:
        auth = request.authorization
        return bool(
            auth
            and secrets.compare_digest(auth.username or "", DASHBOARD_USERNAME)
            and secrets.compare_digest(auth.password or "", DASHBOARD_PASSWORD)
        )
    return _is_loopback_request()


# ── Flask API ──────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.before_request
def _dashboard_auth():
    if _authorized_dashboard_request():
        return None
    resp = Response("Authentication required", status=401)
    if DASHBOARD_USERNAME and DASHBOARD_PASSWORD:
        resp.headers["WWW-Authenticate"] = 'Basic realm="SolTrader Dashboard"'
    return resp


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp

@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")

@app.route("/state")
def state():
    pos_list = []
    for mint, p in positions.items():
        cur        = get_token_price(mint)
        ratio      = (cur / p["entry_price"]) if cur and p["entry_price"] else None
        peak_ratio = p["peak_price"] / p["entry_price"] if p["entry_price"] else None
        pos_list.append({
            "address":       mint,
            "name":          p["name"],
            "entry_price":   p["entry_price"],
            "current_price": cur,
            "ratio":         ratio,
            "peak_ratio":    peak_ratio,
            "pnl":           f"{(ratio-1)*100:+.1f}%" if ratio else "?",
            "age_min":       round((time.time() - p["timestamp"]) / 60, 1),
            "tp1_hit":       p["tp1_hit"],
        })
    return jsonify({
        "balance":   round(sol_balance, 4),
        "wallet":    wallet,
        "positions": pos_list,
        "log":       trade_log[:80],
        "running":   bot_running,
        "settings":  settings,
        "stats":     stats,
        "presets":   {k: v["label"] for k, v in PRESETS.items()},
    })

@app.route("/stop",  methods=["POST"])
def stop_bot():
    global bot_running
    bot_running = False
    log("Bot PAUSED")
    return jsonify({"ok": True})

@app.route("/start", methods=["POST"])
def start_bot():
    global bot_running
    bot_running = True
    log("Bot RESUMED")
    return jsonify({"ok": True})

@app.route("/cashout", methods=["POST"])
def cashout():
    if not positions:
        return jsonify({"ok": True})
    log(f"CASHOUT ALL — {len(positions)} position(s)")
    for mint in list(positions.keys()):
        sell_partial(mint, 1.0, "CASHOUT")
    return jsonify({"ok": True})

@app.route("/preset/<name>", methods=["POST"])
def load_preset(name):
    if name not in PRESETS:
        return jsonify({"ok": False, "error": "Unknown preset"})
    settings.update(PRESETS[name])
    log(f"Loaded preset: {PRESETS[name]['label']}")
    return jsonify({"ok": True, "settings": settings})

@app.route("/settings", methods=["POST"])
def update_settings():
    data = request.json
    for key in ["max_buy_sol","tp1_mult","tp2_mult","trail_pct","stop_loss",
                 "max_age_min","time_stop_min","min_vol_mc","min_liq",
                 "min_mc","max_mc","priority_fee","anti_rug","pump_scan"]:
        if key in data:
            settings[key] = data[key]
    log(f"Custom settings applied")
    return jsonify({"ok": True, "settings": settings})

@app.route("/risk", methods=["POST"])
def update_risk():
    global budget_pct, loss_limit_sol
    data = request.json or {}
    if "budget_pct" in data:
        budget_pct = max(0.01, min(1.0, float(data["budget_pct"])))
    if "loss_limit_sol" in data:
        loss_limit_sol = max(0.0, float(data["loss_limit_sol"]))
    log(f"Risk updated: budget={budget_pct*100:.0f}% loss_limit={loss_limit_sol:.4f} SOL")
    return jsonify({"ok": True, "budget_pct": budget_pct, "loss_limit_sol": loss_limit_sol})

@app.route("/trade_detail")
def trade_detail():
    """Extended data for the new dashboard: positions with full metadata, trade history, optimizer hints."""
    pos_list = []
    for mint, p in positions.items():
        cur        = get_token_price(mint)
        ratio      = (cur / p["entry_price"]) if cur and p["entry_price"] else None
        peak_ratio = p["peak_price"] / p["entry_price"] if p["entry_price"] else None
        pnl_sol    = p["entry_sol"] * ((ratio - 1) if ratio else 0)
        pos_list.append({
            "address":       mint,
            "name":          p["name"],
            "entry_price":   p["entry_price"],
            "current_price": cur,
            "ratio":         ratio,
            "peak_ratio":    peak_ratio,
            "pnl_pct":       round((ratio - 1) * 100, 1) if ratio else None,
            "pnl_sol":       round(pnl_sol, 4),
            "age_min":       round((time.time() - p["timestamp"]) / 60, 1),
            "tp1_hit":       p["tp1_hit"],
            "entry_sol":     p["entry_sol"],
            "source":        p.get("source", "dex"),
            "buy_reason":    p.get("buy_reason", "signal"),
        })

    # Optimizer hints: win-rate by source, best/worst close reason
    wins_by_source   = {}
    trades_by_source = {}
    for t in trade_history:
        src = t.get("buy_reason", "signal")[:12]
        trades_by_source[src] = trades_by_source.get(src, 0) + 1
        if t["won"]:
            wins_by_source[src] = wins_by_source.get(src, 0) + 1
    optimizer = []
    for src, total in sorted(trades_by_source.items(), key=lambda x: -x[1])[:5]:
        wins = wins_by_source.get(src, 0)
        optimizer.append({
            "label":    src,
            "trades":   total,
            "wins":     wins,
            "win_rate": round(wins / total * 100) if total else 0,
        })

    # Session drawdown
    session_loss = round((_session_start_balance or sol_balance) - sol_balance, 4)

    return jsonify({
        "positions":      pos_list,
        "trade_history":  trade_history[:60],
        "optimizer":      optimizer,
        "budget_pct":     budget_pct,
        "loss_limit_sol": loss_limit_sol,
        "session_loss":   session_loss,
        "start_balance":  round(_session_start_balance or sol_balance, 4),
    })

# ── Copy Bot Routes ────────────────────────────────────────────────────────────

@app.route("/copy/state")
def copy_state():
    copy_list = get_copy_list()
    return jsonify({
        "mode": get_mode().value,
        "leaderboard": [
            {
                "address":      e.address[:8] + "…" + e.address[-4:],
                "full_address": e.address,
                "score":        round(e.score, 3),
                "win_rate":     round(e.win_rate * 100, 1),
                "avg_roi":      round(e.avg_roi * 100, 1),
            }
            for e in copy_list
        ],
        "trades": get_trades(),
    })


@app.route("/copy/mode", methods=["POST"])
def copy_set_mode():
    data = request.json or {}
    raw = data.get("mode", "shadow")
    try:
        mode = CopyMode(raw)
    except ValueError:
        return jsonify({"ok": False, "error": f"Unknown mode: {raw}"}), 400
    set_mode(mode)
    log(f"Copy bot mode → {mode.value.upper()}")
    return jsonify({"ok": True, "mode": mode.value})


@app.route("/copy/add_wallet", methods=["POST"])
def copy_add_wallet():
    data = request.json or {}
    addr = (data.get("address") or "").strip()
    if len(addr) < 32:
        return jsonify({"ok": False, "error": "Invalid address"}), 400
    from whale_detection import SMART_MONEY_WALLETS
    SMART_MONEY_WALLETS.add(addr)
    log(f"Copy bot: added wallet {addr[:8]}… to seed list")
    return jsonify({"ok": True, "address": addr})


@app.route("/copy/remove_wallet", methods=["POST"])
def copy_remove_wallet():
    data = request.json or {}
    addr = (data.get("address") or "").strip()
    from whale_detection import SMART_MONEY_WALLETS
    SMART_MONEY_WALLETS.discard(addr)
    log(f"Copy bot: removed wallet {addr[:8]}… from seed list")
    return jsonify({"ok": True})


# ── Dashboard HTML ─────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"><title>SOL Bot</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d0d;color:#e0e0e0;font-family:'Courier New',monospace;padding-top:92px}
/* ── Tab bar ── */
#tab-bar{position:fixed;top:52px;left:0;right:0;z-index:99;background:#0f0f0f;border-bottom:1px solid #1e1e1e;display:flex;height:40px;padding:0 18px;gap:4px;align-items:center}
.tab-btn{background:none;border:none;border-bottom:2px solid transparent;color:#555;font-family:'Courier New',monospace;font-size:12px;cursor:pointer;padding:0 14px;height:100%;text-transform:uppercase;letter-spacing:1px;transition:color .15s,border-color .15s}
.tab-btn:hover{color:#aaa}
.tab-btn.active{color:#9945FF;border-bottom-color:#9945FF}
.tab-pane{display:none}.tab-pane.active{display:block}
/* ── Sticky balance bar ── */
#balance-bar{position:fixed;top:0;left:0;right:0;z-index:100;background:#111;border-bottom:1px solid #252525;
  display:flex;align-items:center;gap:0;height:52px;padding:0 18px;overflow:hidden}
#balance-bar .bb-item{display:flex;flex-direction:column;justify-content:center;padding:0 16px;border-right:1px solid #222;height:100%}
#balance-bar .bb-item:last-child{border-right:none;margin-left:auto}
#balance-bar .bb-label{font-size:9px;color:#444;text-transform:uppercase;letter-spacing:1px}
#balance-bar .bb-val{font-size:16px;font-weight:bold;line-height:1.2}
#balance-bar .bb-status{display:flex;align-items:center;gap:8px}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#14F195;animation:pulse 1.5s infinite}
.dot.red{background:#FF4444;animation:none}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
/* ── Layout ── */
.wrap{padding:14px 18px}
h1{color:#9945FF;margin-bottom:2px;font-size:20px}
.sub{color:#444;font-size:11px;margin-bottom:14px}
.row{display:flex;gap:12px;margin-bottom:12px;flex-wrap:wrap}
.card{background:#141414;border:1px solid #222;border-radius:8px;padding:13px;flex:1;min-width:150px}
.card h2{font-size:10px;color:#444;text-transform:uppercase;letter-spacing:1px;margin-bottom:9px}
.big{font-size:24px;color:#14F195;font-weight:bold}
.sm{font-size:11px;color:#444;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:12px}
th{color:#444;font-weight:normal;text-align:left;padding:4px 8px;border-bottom:1px solid #1e1e1e;font-size:10px;text-transform:uppercase}
td{padding:5px 8px;border-bottom:1px solid #191919}
.green{color:#14F195}.red{color:#FF4444}.yellow{color:#FFD700}.purple{color:#9945FF}.gray{color:#444}.white{color:#e0e0e0}
.log-box{background:#141414;border:1px solid #222;border-radius:8px;padding:13px;height:240px;overflow-y:auto}
.log-line{font-size:11px;color:#555;line-height:1.9;border-bottom:1px solid #161616}
.log-line.buy{color:#e0e0e0}.log-line.sell-win{color:#14F195}.log-line.sell-loss{color:#FF4444}
.log-line.signal{color:#FFD700}.log-line.hold{color:#333}.log-line.rug{color:#FF4444}
.btn{border:none;border-radius:6px;padding:7px 14px;font-family:monospace;font-size:12px;cursor:pointer;font-weight:bold;transition:opacity .15s}
.btn:hover{opacity:.8}
.btn-red{background:#FF4444;color:#fff}.btn-green{background:#14F195;color:#000}
.btn-yellow{background:#FFD700;color:#000}.btn-gray{background:#222;color:#aaa}
.btn-purple{background:#9945FF;color:#fff}.btn-blue{background:#1E90FF;color:#fff}
input[type=number],input[type=text]{background:#0e0e0e;border:1px solid #2a2a2a;color:#e0e0e0;border-radius:4px;padding:4px 6px;font-family:monospace;font-size:12px;width:80px}
input[type=range]{width:140px;accent-color:#9945FF;cursor:pointer;vertical-align:middle}
input[type=checkbox]{width:16px;height:16px;cursor:pointer}
label{font-size:11px;color:#666;display:block;margin-bottom:3px}
.field{margin-right:12px;margin-bottom:8px;display:inline-block;vertical-align:top}
/* ── Position cards ── */
.pos-grid{display:flex;flex-wrap:wrap;gap:10px}
.pos-card{background:#181818;border:1px solid #252525;border-radius:8px;padding:12px;min-width:220px;flex:1;max-width:320px;position:relative}
.pos-card.winning{border-color:#14F19540}
.pos-card.losing{border-color:#FF444440}
.pos-card .pc-name{font-size:14px;font-weight:bold;margin-bottom:2px}
.pos-card .pc-reason{font-size:10px;color:#555;margin-bottom:8px}
.pos-card .pc-pnl{font-size:22px;font-weight:bold;margin-bottom:2px}
.pos-card .pc-meta{display:flex;gap:12px;font-size:11px;color:#555;margin-top:6px;flex-wrap:wrap}
.pos-card .pc-badge{font-size:9px;padding:2px 5px;border-radius:3px;background:#1e2a1e;color:#14F195;margin-left:4px}
.pos-card .pc-badge.pump{background:#2a1e2a;color:#9945FF}
.pos-card .pc-bar-wrap{height:4px;background:#222;border-radius:2px;margin-top:8px;overflow:hidden}
.pos-card .pc-bar{height:4px;border-radius:2px;background:#14F195;transition:width .5s}
.pos-card .pc-bar.red{background:#FF4444}
.pos-card .pc-link{position:absolute;top:10px;right:10px;font-size:10px;color:#444}
.pos-card .pc-link:hover{color:#9945FF}
/* ── History lists ── */
.hist-list{max-height:320px;overflow-y:auto}
.hist-item{display:flex;align-items:center;gap:8px;padding:5px 8px;border-bottom:1px solid #191919;font-size:11px}
.hist-item .hi-icon{font-size:14px;width:20px;text-align:center}
.hist-item .hi-name{flex:1;font-weight:bold}
.hist-item .hi-reason{color:#444;font-size:10px;max-width:110px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.hist-item .hi-pnl{font-weight:bold;white-space:nowrap}
.hist-item .hi-ts{color:#333;font-size:10px;white-space:nowrap}
/* ── Optimizer ── */
.opt-row{display:flex;align-items:center;gap:10px;padding:5px 0;border-bottom:1px solid #191919;font-size:11px}
.opt-row .or-label{width:120px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;color:#888}
.opt-row .or-bar-wrap{flex:1;height:6px;background:#1a1a1a;border-radius:3px;overflow:hidden}
.opt-row .or-bar{height:6px;border-radius:3px;background:#9945FF;transition:width .5s}
.opt-row .or-pct{width:40px;text-align:right;color:#9945FF;font-weight:bold}
.opt-row .or-count{width:40px;text-align:right;color:#444}
/* ── Budget/risk panel ── */
.risk-row{display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap}
.risk-row label{display:inline;margin:0;min-width:110px}
.risk-val{font-size:13px;font-weight:bold;color:#9945FF;min-width:40px}
/* ── Misc ── */
.badge{font-size:10px;padding:2px 5px;border-radius:3px;background:#1e2a1e;color:#14F195;margin-left:4px}
.badge.pump{background:#2a1e2a;color:#9945FF}
.preset-active{outline:2px solid #14F195}
.divider{border:none;border-top:1px solid #1e1e1e;margin:11px 0}
.stat-row{display:flex;gap:18px;margin-top:5px}
.stat .val{font-size:18px;font-weight:bold}
.stat .sm{font-size:11px;color:#444}
a{color:#9945FF;text-decoration:none}
.empty{color:#333;font-size:12px;padding:8px 0}
</style>
</head>
<body>

<!-- ── Sticky Balance Bar ── -->
<div id="balance-bar">
  <div class="bb-item">
    <span class="bb-label">Balance</span>
    <span class="bb-val green" id="bb-balance">…</span>
  </div>
  <div class="bb-item">
    <span class="bb-label">Start</span>
    <span class="bb-val white" id="bb-start">…</span>
  </div>
  <div class="bb-item">
    <span class="bb-label">Session P&amp;L</span>
    <span class="bb-val" id="bb-pnl">…</span>
  </div>
  <div class="bb-item">
    <span class="bb-label">Positions</span>
    <span class="bb-val yellow" id="bb-pos">0</span>
  </div>
  <div class="bb-item">
    <span class="bb-label">W / L</span>
    <span class="bb-val" id="bb-wl">0 / 0</span>
  </div>
  <div class="bb-item" style="margin-left:auto;border-right:none">
    <div class="bb-status">
      <span class="dot" id="sdot"></span>
      <span id="stxt" style="font-size:12px;color:#777">Live</span>
      <button class="btn btn-yellow" id="tbtn" onclick="toggleBot()" style="padding:4px 10px;font-size:11px">⏸ Pause</button>
      <button class="btn btn-red"    onclick="cashout()"             style="padding:4px 10px;font-size:11px">💸 Cashout</button>
    </div>
  </div>
</div>

<!-- ── Tab Bar ── -->
<div id="tab-bar">
  <button class="tab-btn active" id="tbtn-trading" onclick="switchTab('trading')">Trading</button>
  <button class="tab-btn" id="tbtn-copy" onclick="switchTab('copy')">Copy Bot</button>
</div>

<!-- ══ TAB: Trading ══ -->
<div id="tab-trading" class="tab-pane active">
<div class="wrap">
<h1>SOL Trading Bot</h1>
<p class="sub">refreshes every 5s</p>

<!-- ── Position Cards ── -->
<div class="card" style="margin-bottom:12px">
  <h2>Open Positions</h2>
  <div id="pos-grid" class="pos-grid"><p class="empty">No open positions</p></div>
</div>

<!-- ── Budget / Risk Controls ── -->
<div class="card" style="margin-bottom:12px">
  <h2>Risk Controls</h2>
  <div class="risk-row">
    <label>Trade Budget</label>
    <input type="range" id="r-budget" min="5" max="100" step="5" value="100" oninput="document.getElementById('r-budget-val').textContent=this.value+'%'">
    <span class="risk-val" id="r-budget-val">100%</span>
    <span class="sm">of max_buy_sol per trade</span>
  </div>
  <div class="risk-row">
    <label>Loss Limit (SOL)</label>
    <input type="number" id="r-loss" step="0.01" min="0" value="0" style="width:80px">
    <span class="sm">0 = disabled — auto-pause when session loss hits this</span>
  </div>
  <div class="risk-row">
    <span id="r-session-loss" class="sm"></span>
    <button class="btn btn-gray" onclick="saveRisk()" style="margin-left:auto">Apply</button>
  </div>
</div>

<!-- ── Strategy Presets + Settings ── -->
<div class="card" style="margin-bottom:12px">
  <h2>Strategy Presets</h2>
  <div style="display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap">
    <button class="btn btn-green"  id="preset-steady" onclick="loadPreset('steady')">📈 Steady Profit</button>
    <button class="btn btn-purple" id="preset-max"    onclick="loadPreset('max')">🚀 Max Profit</button>
    <span style="color:#444;font-size:11px;align-self:center">— or customize below then Apply</span>
  </div>
  <hr class="divider">
  <div style="margin-top:10px">
    <div class="field"><label>Buy (SOL)</label><input type="number" id="s-buy"   step="0.01" min="0.01"></div>
    <div class="field"><label>TP1 (x)</label><input type="number"   id="s-tp1"   step="0.1"  min="1.1"></div>
    <div class="field"><label>TP2 (x)</label><input type="number"   id="s-tp2"   step="0.5"  min="1.5"></div>
    <div class="field"><label>Trail Stop (%)</label><input type="number" id="s-trail" step="1" min="5" max="60"></div>
    <div class="field"><label>Hard SL (%)</label><input type="number"    id="s-sl"    step="1" min="5" max="80"></div>
    <div class="field"><label>Max Age (min)</label><input type="number"  id="s-age"   step="5" min="1"></div>
    <div class="field"><label>Time Stop (min)</label><input type="number" id="s-ts"   step="5" min="5"></div>
    <div class="field"><label>Min Liq ($)</label><input type="number"    id="s-liq"   step="500" min="500"></div>
    <div class="field"><label>Min MC ($)</label><input type="number"     id="s-minmc" step="500" min="500"></div>
    <div class="field"><label>Max MC ($)</label><input type="number"     id="s-maxmc" step="5000" min="1000"></div>
    <div class="field"><label>Priority Fee (lamports)</label><input type="number" id="s-fee" step="10000" min="0"></div>
    <div class="field"><label>Anti-Rug</label><input type="checkbox" id="s-rug"></div>
    <div class="field"><label>Pump.fun Scan</label><input type="checkbox" id="s-pump"></div>
    <button class="btn btn-gray" onclick="saveSettings()">Apply Custom</button>
  </div>
</div>

<!-- ── Winners / Losers History ── -->
<div class="row">
  <div class="card">
    <h2 class="green">Winners</h2>
    <div id="hist-wins" class="hist-list"><p class="empty">No winners yet</p></div>
  </div>
  <div class="card">
    <h2 class="red">Losers</h2>
    <div id="hist-losses" class="hist-list"><p class="empty">No losses yet</p></div>
  </div>
</div>

<!-- ── Live Parameter Optimizer ── -->
<div class="card" style="margin-bottom:12px">
  <h2>Live Signal Optimizer — Win Rate by Buy Reason</h2>
  <div id="optimizer"><p class="empty">Accumulating trade data…</p></div>
</div>

<!-- ── Activity Log ── -->
<div class="log-box" style="margin-bottom:12px">
  <h2 style="margin-bottom:8px;font-size:10px;color:#444;text-transform:uppercase;letter-spacing:1px">Activity Log</h2>
  <div id="log"></div>
</div>

</div><!-- /wrap trading -->
</div><!-- /tab-trading -->

<!-- ══ TAB: Copy Bot ══ -->
<div id="tab-copy" class="tab-pane">
<div class="wrap">
<h1 style="color:#14F195">Copy Bot</h1>
<p class="sub">Mirrors top-performing wallets • refreshes every 10s</p>

<div class="card" style="margin-bottom:12px" id="copy-bot-card">
  <h2>Copy Bot</h2>
  <div style="display:flex;gap:8px;margin-bottom:14px;align-items:center">
    <span style="font-size:11px;color:#888;margin-right:4px">MODE:</span>
    <button id="copy-btn-shadow" onclick="setCopyMode('shadow')" style="background:#7c6f00;color:#ffe;border:none;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:12px">SHADOW</button>
    <button id="copy-btn-live"   onclick="setCopyMode('live')"   style="background:#1a4d1a;color:#afe;border:none;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:12px">LIVE</button>
    <button id="copy-btn-off"    onclick="setCopyMode('off')"    style="background:#4d1a1a;color:#faa;border:none;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:12px">OFF</button>
    <span id="copy-mode-label" style="margin-left:10px;font-weight:bold;font-size:13px"></span>
  </div>
  <div style="display:flex;gap:6px;margin-bottom:14px">
    <input id="copy-wallet-input" type="text" placeholder="Add wallet address to track…" style="flex:1;padding:5px 8px;background:#111;color:#eee;border:1px solid #333;border-radius:4px;font-size:12px">
    <button onclick="addCopyWallet()" style="background:#222;color:#eee;border:1px solid #555;padding:5px 12px;border-radius:4px;cursor:pointer;font-size:12px">Add</button>
  </div>
  <h3 style="font-size:11px;color:#888;margin-bottom:6px;text-transform:uppercase;letter-spacing:1px">Top Wallets (by composite score)</h3>
  <div id="copy-leaderboard" style="margin-bottom:14px"><p class="empty">Scoring wallets…</p></div>
  <h3 style="font-size:11px;color:#888;margin-bottom:6px;text-transform:uppercase;letter-spacing:1px">Copy Trade Log</h3>
  <div id="copy-trade-log"><p class="empty">No copied trades yet</p></div>
</div>

<!-- ── Activity Log (inside Copy Bot tab) ── -->
<div class="log-box" style="margin-bottom:12px">
  <h2 style="margin-bottom:8px;font-size:10px;color:#444;text-transform:uppercase;letter-spacing:1px">Activity Log</h2>
  <div id="log-copy"></div>
</div>
</div><!-- /wrap copy -->
</div><!-- /tab-copy -->

<script>
let running=true, st={};

function fmt(v,d=4){return v==null?'?':v.toFixed(d);}
function pnlCls(v){return v==null?'':v>=0?'green':'red';}
function sign(v){return v>=0?'+':'';}

function switchTab(name){
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  document.getElementById('tbtn-'+name).classList.add('active');
  localStorage.setItem('activeTab',name);
}
// Restore last tab on load
(function(){const t=localStorage.getItem('activeTab');if(t)switchTab(t);})();


async function refresh(){
  const [d,td]=await Promise.all([
    fetch('/state').then(r=>r.json()).catch(()=>null),
    fetch('/trade_detail').then(r=>r.json()).catch(()=>null)
  ]);
  if(!d||!td)return;
  running=d.running; st=d.settings;

  // ── Balance bar ──
  document.getElementById('bb-balance').textContent=fmt(d.balance,4)+' SOL';
  document.getElementById('bb-start').textContent=fmt(td.start_balance,4)+' SOL';
  const pnlSol=d.stats.total_pnl_sol;
  const bbPnl=document.getElementById('bb-pnl');
  bbPnl.textContent=(pnlSol>=0?'+':'')+fmt(pnlSol,4)+' SOL';
  bbPnl.className='bb-val '+(pnlSol>=0?'green':'red');
  document.getElementById('bb-pos').textContent=td.positions.length;
  document.getElementById('bb-wl').textContent=`${d.stats.wins} / ${d.stats.losses}`;

  const dot=document.getElementById('sdot'),txt=document.getElementById('stxt'),btn=document.getElementById('tbtn');
  if(running){dot.className='dot';txt.textContent='Running';btn.textContent='⏸ Pause';btn.className='btn btn-yellow';}
  else{dot.className='dot red';txt.textContent='Paused';btn.textContent='▶ Resume';btn.className='btn btn-green';}

  // ── Risk panel ──
  const rl=document.getElementById('r-loss');
  if(document.activeElement!==rl) rl.value=td.loss_limit_sol;
  const rb=document.getElementById('r-budget');
  if(document.activeElement!==rb){
    rb.value=Math.round(td.budget_pct*100);
    document.getElementById('r-budget-val').textContent=Math.round(td.budget_pct*100)+'%';
  }
  const sl_el=document.getElementById('r-session-loss');
  if(td.session_loss>0){
    sl_el.innerHTML=`Session drawdown: <span class="red">${fmt(td.session_loss,4)} SOL</span>`
      +(td.loss_limit_sol>0?` / limit ${fmt(td.loss_limit_sol,4)} SOL`:'');
  } else { sl_el.textContent='No session drawdown'; }

  // ── Settings fields ──
  document.getElementById('s-buy').value   =st.max_buy_sol;
  document.getElementById('s-tp1').value   =st.tp1_mult;
  document.getElementById('s-tp2').value   =st.tp2_mult;
  document.getElementById('s-trail').value =Math.round(st.trail_pct*100);
  document.getElementById('s-sl').value    =Math.round((1-st.stop_loss)*100);
  document.getElementById('s-age').value   =st.max_age_min;
  document.getElementById('s-ts').value    =st.time_stop_min;
  document.getElementById('s-liq').value   =st.min_liq;
  document.getElementById('s-minmc').value =st.min_mc;
  document.getElementById('s-maxmc').value =st.max_mc;
  document.getElementById('s-fee').value   =st.priority_fee;
  document.getElementById('s-rug').checked =!!st.anti_rug;
  document.getElementById('s-pump').checked=!!st.pump_scan;

  // ── Position cards ──
  const grid=document.getElementById('pos-grid');
  if(td.positions.length){
    grid.innerHTML=td.positions.map(p=>{
      const pnl=p.pnl_pct;
      const cls=pnl==null?'':pnl>=0?'winning':'losing';
      const pnlTxt=pnl==null?'?':(pnl>=0?'+':'')+pnl.toFixed(1)+'%';
      const pnlSolTxt=(p.pnl_sol>=0?'+':'')+fmt(p.pnl_sol,4)+' SOL';
      const pnlCl=pnl==null?'white':pnl>=0?'green':'red';
      const tp1=p.tp1_hit?'<span class="pc-badge">TP1✓</span>':'';
      const srcBadge=p.source==='pump'?'<span class="pc-badge pump">pump.fun</span>':'';
      // Progress bar: ratio 1.0→tp1 = 0→100%
      const tp1target=st.tp1_mult||1.5;
      const barPct=p.ratio?Math.min(100,Math.max(0,((p.ratio-1)/(tp1target-1))*100)):0;
      const barCls=pnl!=null&&pnl<0?'red':'';
      return `<div class="pos-card ${cls}">
        <a class="pc-link" href="https://dexscreener.com/solana/${p.address}" target="_blank">chart ↗</a>
        <div class="pc-name">${p.name}${tp1}${srcBadge}</div>
        <div class="pc-reason">${p.buy_reason||'signal'}</div>
        <div class="pc-pnl ${pnlCl}">${pnlTxt} <span style="font-size:13px">${pnlSolTxt}</span></div>
        <div class="pc-meta">
          <span>${p.ratio!=null?p.ratio.toFixed(3)+'x':''}</span>
          <span class="yellow">peak ${p.peak_ratio!=null?p.peak_ratio.toFixed(2)+'x':''}</span>
          <span>${p.age_min}m</span>
          <span>${fmt(p.entry_sol,3)} SOL in</span>
        </div>
        <div class="pc-bar-wrap"><div class="pc-bar ${barCls}" style="width:${barPct}%"></div></div>
      </div>`;
    }).join('');
  } else {
    grid.innerHTML='<p class="empty">No open positions</p>';
  }

  // ── Trade history: winners / losers ──
  const wins=td.trade_history.filter(t=>t.won);
  const losses=td.trade_history.filter(t=>!t.won);
  function histHTML(list){
    if(!list.length) return '<p class="empty">None yet</p>';
    return list.slice(0,30).map(t=>{
      const cls=t.won?'green':'red';
      const icon=t.won?'✅':'❌';
      return `<div class="hist-item">
        <span class="hi-icon">${icon}</span>
        <span class="hi-name ${cls}">${t.name}</span>
        <span class="hi-reason" title="${t.buy_reason}">${t.buy_reason}</span>
        <span class="hi-pnl ${cls}">${sign(t.pnl_pct)}${t.pnl_pct}% (${sign(t.pnl_sol)}${fmt(t.pnl_sol,4)})</span>
        <span class="hi-reason" style="color:#333">${t.close_reason}</span>
        <span class="hi-ts">${t.ts}</span>
      </div>`;
    }).join('');
  }
  document.getElementById('hist-wins').innerHTML=histHTML(wins);
  document.getElementById('hist-losses').innerHTML=histHTML(losses);

  // ── Optimizer ──
  const optEl=document.getElementById('optimizer');
  if(td.optimizer&&td.optimizer.length){
    optEl.innerHTML=td.optimizer.map(o=>{
      return `<div class="opt-row">
        <span class="or-label" title="${o.label}">${o.label}</span>
        <div class="or-bar-wrap"><div class="or-bar" style="width:${o.win_rate}%"></div></div>
        <span class="or-pct">${o.win_rate}%</span>
        <span class="or-count">${o.wins}/${o.trades}</span>
      </div>`;
    }).join('');
  } else {
    optEl.innerHTML='<p class="empty">Accumulating trade data…</p>';
  }

  // ── Log ──
  const logEl=document.getElementById('log');
  const logCopyEl=document.getElementById('log-copy');
  const logHTML=(d.log||[]).map(l=>{
    const cls=l.includes('BUY')?'buy':l.includes('WIN')||l.includes('PROFIT')?'sell-win':l.includes('LOSS')||l.includes('SL')?'sell-loss':l.includes('signal')||l.includes('SIGNAL')?'signal':'hold';
    return `<div class="log-line ${cls}">${l}</div>`;
  }).join('');
  if(logEl) logEl.innerHTML=logHTML||'<p class="empty">No activity yet</p>';
  if(logCopyEl) logCopyEl.innerHTML=logHTML||'<p class="empty">No activity yet</p>';
}

async function cashout(){
  if(!confirm('Sell ALL open positions now?'))return;
  await fetch('/cashout',{method:'POST'});
  setTimeout(refresh,1000);
}

async function toggleBot(){
  await fetch(running?'/stop':'/start',{method:'POST'});
  setTimeout(refresh,400);
}

async function loadPreset(name){
  await fetch('/preset/'+name,{method:'POST'});
  document.querySelectorAll('[id^=preset-]').forEach(b=>b.classList.remove('preset-active'));
  document.getElementById('preset-'+name)?.classList.add('preset-active');
  setTimeout(refresh,400);
}

async function saveSettings(){
  const sl=parseFloat(document.getElementById('s-sl').value);
  const trail=parseFloat(document.getElementById('s-trail').value);
  document.querySelectorAll('[id^=preset-]').forEach(b=>b.classList.remove('preset-active'));
  await fetch('/settings',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      max_buy_sol:  parseFloat(document.getElementById('s-buy').value),
      tp1_mult:     parseFloat(document.getElementById('s-tp1').value),
      tp2_mult:     parseFloat(document.getElementById('s-tp2').value),
      trail_pct:    trail/100,
      stop_loss:    (100-sl)/100,
      max_age_min:  parseFloat(document.getElementById('s-age').value),
      time_stop_min:parseFloat(document.getElementById('s-ts').value),
      min_liq:      parseFloat(document.getElementById('s-liq').value),
      min_mc:       parseFloat(document.getElementById('s-minmc').value),
      max_mc:       parseFloat(document.getElementById('s-maxmc').value),
      priority_fee: parseFloat(document.getElementById('s-fee').value),
      anti_rug:     document.getElementById('s-rug').checked,
      pump_scan:    document.getElementById('s-pump').checked,
    })
  });
  setTimeout(refresh,300);
}

async function saveRisk(){
  const pct=parseFloat(document.getElementById('r-budget').value)/100;
  const loss=parseFloat(document.getElementById('r-loss').value)||0;
  await fetch('/risk',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({budget_pct:pct,loss_limit_sol:loss})
  });
  setTimeout(refresh,300);
}

refresh();
setInterval(refresh,5000);

// ── Copy Bot ──────────────────────────────────────────────────────────────────
async function refreshCopyBot() {
  const d = await fetch('/copy/state').then(r=>r.json()).catch(()=>null);
  if (!d) return;
  const modeLabel = document.getElementById('copy-mode-label');
  const modeColors = {shadow:'#ffe566',live:'#66ffaa',off:'#ff6666'};
  modeLabel.textContent = d.mode.toUpperCase();
  modeLabel.style.color = modeColors[d.mode] || '#eee';
  ['shadow','live','off'].forEach(m => {
    document.getElementById('copy-btn-'+m).style.opacity = d.mode===m ? '1' : '0.45';
  });
  const lb = document.getElementById('copy-leaderboard');
  if (!d.leaderboard.length) {
    lb.innerHTML = '<p class="empty">No wallets scored yet — add seed wallets above or wait for scan</p>';
  } else {
    lb.innerHTML = '<table><thead><tr><th>Wallet</th><th>Score</th><th>Win%</th><th>Avg ROI</th><th></th></tr></thead><tbody>' +
      d.leaderboard.map(w=>`<tr>
        <td style="font-family:monospace;font-size:11px">${w.address}</td>
        <td>${w.score}</td>
        <td class="${w.win_rate>=50?'green':'red'}">${w.win_rate}%</td>
        <td class="${w.avg_roi>=0?'green':'red'}">${w.avg_roi>=0?'+':''}${w.avg_roi}%</td>
        <td><button onclick="removeCopyWallet('${w.full_address}')" style="background:none;border:none;color:#f66;cursor:pointer;font-size:11px">✕</button></td>
      </tr>`).join('')+'</tbody></table>';
  }
  const tl = document.getElementById('copy-trade-log');
  if (!d.trades.length) {
    tl.innerHTML = '<p class="empty">No copied trades yet</p>';
  } else {
    tl.innerHTML = '<table><thead><tr><th>Time</th><th>Source</th><th>Token</th><th>Action</th><th>Size</th><th>P&L</th><th>Mode</th></tr></thead><tbody>' +
      d.trades.slice(0,30).map(t=>{
        const ts=new Date(t.timestamp*1000).toLocaleTimeString();
        const pnlCl=t.pnl_sol>=0?'green':'red';
        return `<tr style="opacity:${t.shadow?'0.7':'1'}">
          <td style="font-size:10px">${ts}</td>
          <td style="font-family:monospace;font-size:10px">${t.source_wallet}</td>
          <td style="font-family:monospace;font-size:10px">${t.token_name}</td>
          <td class="${t.action==='BUY'?'green':'red'}">${t.action}</td>
          <td>${t.size_sol} SOL</td>
          <td class="${pnlCl}">${t.pnl_sol>=0?'+':''}${t.pnl_sol}</td>
          <td style="font-size:10px;color:${t.shadow?'#aaa':'#6f6'}">${t.shadow?'SHADOW':'LIVE'}</td>
        </tr>`;
      }).join('')+'</tbody></table>';
  }
}
async function setCopyMode(mode) {
  await fetch('/copy/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})});
  refreshCopyBot();
}
async function addCopyWallet() {
  const input=document.getElementById('copy-wallet-input');
  const addr=input.value.trim();
  if(!addr) return;
  await fetch('/copy/add_wallet',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({address:addr})});
  input.value='';
  refreshCopyBot();
}
async function removeCopyWallet(addr) {
  await fetch('/copy/remove_wallet',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({address:addr})});
  refreshCopyBot();
}
setInterval(refreshCopyBot,10000);
refreshCopyBot();
</script>
</body>
</html>"""

if __name__ == "__main__":
    # Pump.fun WebSocket listener (real-time token creation events)
    ws_thread = threading.Thread(target=_pump_ws_thread, daemon=True)
    ws_thread.start()

    # Main bot loop
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()

    # Copy Bot startup
    from whale_detection import SMART_MONEY_WALLETS
    _seed_wallets = list(SMART_MONEY_WALLETS) or []
    if _seed_wallets:
        start_scanner(_seed_wallets)
        start_engine(
            get_addresses_fn=get_copy_addresses,
            get_balance_fn=lambda: sol_balance,
            execute_swap_fn=lambda mint, sol: buy_token(mint, mint[:8], get_token_price(mint) or 0, source="copy"),
            get_price_fn=get_token_price,
        )

    print(f"\n  Dashboard → http://{DASHBOARD_HOST}:{DASHBOARD_PORT}\n")
    app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT, debug=False)
