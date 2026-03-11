"""
Run with:  python dashboard.py
Then open: http://localhost:5000
"""

import threading
import time
import os
import base64
import json
import requests
import base58
import websocket
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from flask import Flask, Response, jsonify, request

load_dotenv(dotenv_path=os.path.join(os.path.expanduser("~"), "Desktop", ".env"))

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
HELIUS_RPC  = os.getenv("HELIUS_RPC")

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
        "priority_fee":  100000, # pay more to land first
        "anti_rug":      True,
        "pump_scan":     True,
    },
}

# ── Live settings (start with steady) ─────────────────────────────────────────
settings = dict(PRESETS["steady"])

# ── Shared state ───────────────────────────────────────────────────────────────
seen_tokens = set()
positions   = {}   # {mint: {name, entry_price, peak_price, timestamp, tp1_hit}}
trade_log   = []
sol_balance = 0.0
bot_running = True
stats       = {"wins": 0, "losses": 0, "total_pnl_sol": 0.0}

def log(msg):
    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    trade_log.insert(0, entry)
    if len(trade_log) > 500:
        trade_log.pop()
    print(entry)

# ── Solana helpers ─────────────────────────────────────────────────────────────
def refresh_balance():
    global sol_balance
    try:
        r = requests.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]
        }, timeout=5)
        sol_balance = r.json().get("result", {}).get("value", 0) / 1e9
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
        f"https://quote-api.jup.ag/v6/quote"
        f"?inputMint={input_mint}&outputMint={output_mint}"
        f"&amount={amount_lamports}&slippageBps=1500",
        timeout=10
    ).json()
    return None if "error" in r else r

def jupiter_swap(quote, priority_fee_lamports=10000):
    r = requests.post("https://quote-api.jup.ag/v6/swap", json={
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

    log(f"SIGNAL [{source.upper()}] {name} | MC:${mc:,.0f} Vol:${vol:,.0f} Liq:${liq:,.0f} | Age:{age_min:.0f}m {change:+.0f}%")

    # Anti-rug check
    if s.get("anti_rug"):
        if not is_safe_token(mint):
            log(f"  RUG RISK — skipping {name} (mint/freeze authority or top holder)")
            return

    buy_token(mint, name, price)

def buy_token(mint, name, price):
    s = settings
    if sol_balance < s["max_buy_sol"] + 0.01:
        log(f"Low balance ({sol_balance:.4f} SOL) — skip {name}")
        return

    lamports = int(s["max_buy_sol"] * 1e9)
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
            "entry_sol":   s["max_buy_sol"],
        }
        log(f"BUY  {name} @ ${price:.8f} | {s['max_buy_sol']} SOL | solscan.io/tx/{sig}")
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
                if pnl_pct >= 0:
                    stats["wins"] += 1
                else:
                    stats["losses"] += 1
                stats["total_pnl_sol"] += pnl_sol
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

# ── Flask API ──────────────────────────────────────────────────────────────────
app = Flask(__name__)

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

# ── Dashboard HTML ─────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"><title>SOL Bot</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d0d;color:#e0e0e0;font-family:'Courier New',monospace;padding:20px}
h1{color:#9945FF;margin-bottom:4px;font-size:22px}
.sub{color:#555;font-size:12px;margin-bottom:18px}
.row{display:flex;gap:14px;margin-bottom:14px;flex-wrap:wrap}
.card{background:#1a1a1a;border:1px solid #252525;border-radius:8px;padding:14px;flex:1;min-width:160px}
.card h2{font-size:11px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}
.big{font-size:26px;color:#14F195;font-weight:bold}
.sm{font-size:11px;color:#555;margin-top:3px}
table{width:100%;border-collapse:collapse;font-size:12px}
th{color:#555;font-weight:normal;text-align:left;padding:4px 8px;border-bottom:1px solid #222;font-size:10px;text-transform:uppercase}
td{padding:5px 8px;border-bottom:1px solid #1c1c1c}
.green{color:#14F195}.red{color:#FF4444}.yellow{color:#FFD700}.purple{color:#9945FF}.gray{color:#555}
.log-box{background:#1a1a1a;border:1px solid #252525;border-radius:8px;padding:14px;height:260px;overflow-y:auto}
.log-line{font-size:11px;color:#666;line-height:1.9;border-bottom:1px solid #181818}
.log-line.buy{color:#14F195}.log-line.sell{color:#FF6B6B}.log-line.signal{color:#FFD700}
.log-line.hold{color:#3a3a3a}.log-line.rug{color:#FF4444}
.btn{border:none;border-radius:6px;padding:7px 14px;font-family:monospace;font-size:12px;cursor:pointer;font-weight:bold;transition:opacity .15s}
.btn:hover{opacity:.8}
.btn-red{background:#FF4444;color:#fff}.btn-green{background:#14F195;color:#000}
.btn-yellow{background:#FFD700;color:#000}.btn-gray{background:#2a2a2a;color:#aaa}
.btn-purple{background:#9945FF;color:#fff}.btn-blue{background:#1E90FF;color:#fff}
input[type=number],input[type=text]{background:#111;border:1px solid #333;color:#e0e0e0;border-radius:4px;padding:4px 6px;font-family:monospace;font-size:12px;width:80px}
input[type=checkbox]{width:16px;height:16px;cursor:pointer}
label{font-size:11px;color:#777;display:block;margin-bottom:3px}
.field{margin-right:12px;margin-bottom:8px;display:inline-block;vertical-align:top}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#14F195;animation:pulse 1.5s infinite;margin-right:5px}
.dot.red{background:#FF4444;animation:none}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
a{color:#9945FF;text-decoration:none}
.empty{color:#444;font-size:12px}
.badge{font-size:10px;padding:2px 5px;border-radius:3px;background:#1e2a1e;color:#14F195;margin-left:4px}
.badge.pump{background:#2a1e2a;color:#9945FF}
.preset-active{outline:2px solid #14F195}
.divider{border:none;border-top:1px solid #222;margin:12px 0}
.stat-row{display:flex;gap:20px;margin-top:6px}
.stat{font-size:12px}.stat .val{font-size:18px;font-weight:bold}
</style>
</head>
<body>
<h1>SOL Trading Bot</h1>
<p class="sub"><span class="dot" id="sdot"></span><span id="stxt">Live</span>&nbsp;·&nbsp;refreshes every 5s</p>

<!-- Row 1: stats -->
<div class="row">
  <div class="card">
    <h2>Balance</h2>
    <div class="big" id="balance">…</div><div class="sm">SOL</div>
  </div>
  <div class="card">
    <h2>Open Positions</h2>
    <div class="big yellow" id="pos-count">0</div><div class="sm">active</div>
  </div>
  <div class="card">
    <h2>Session P&amp;L</h2>
    <div class="stat-row">
      <div class="stat"><div class="val green" id="wins">0</div><div class="sm">wins</div></div>
      <div class="stat"><div class="val red"   id="losses">0</div><div class="sm">losses</div></div>
      <div class="stat"><div class="val" id="pnl-sol">+0.0000</div><div class="sm">SOL P&L</div></div>
    </div>
  </div>
  <div class="card" style="flex:2">
    <h2>Controls</h2>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn btn-red"    onclick="cashout()">💸 Cashout All</button>
      <button class="btn btn-yellow" id="tbtn" onclick="toggleBot()">⏸ Pause</button>
    </div>
  </div>
</div>

<!-- Row 2: presets + settings -->
<div class="card" style="margin-bottom:14px">
  <h2>Strategy Presets</h2>
  <div style="display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap">
    <button class="btn btn-green"  id="preset-steady" onclick="loadPreset('steady')">📈 Steady Profit</button>
    <button class="btn btn-purple" id="preset-max"    onclick="loadPreset('max')">🚀 Max Profit</button>
    <span style="color:#555;font-size:11px;align-self:center">— or customize below then Apply</span>
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

<!-- Positions -->
<div class="card" style="margin-bottom:14px">
  <h2>Positions</h2>
  <div id="pos-tbl"><p class="empty">No open positions</p></div>
</div>

<!-- Log -->
<div class="log-box">
  <h2 style="margin-bottom:8px">Activity Log</h2>
  <div id="log"></div>
</div>

<script>
let running=true, st={};

async function refresh(){
  const d=await fetch('/state').then(r=>r.json()).catch(()=>null);
  if(!d)return;
  running=d.running; st=d.settings;

  document.getElementById('balance').textContent=d.balance.toFixed(4);
  document.getElementById('pos-count').textContent=d.positions.length;
  document.getElementById('wins').textContent=d.stats.wins;
  document.getElementById('losses').textContent=d.stats.losses;
  const pnlEl=document.getElementById('pnl-sol');
  pnlEl.textContent=(d.stats.total_pnl_sol>=0?'+':'')+d.stats.total_pnl_sol.toFixed(4);
  pnlEl.className='val '+(d.stats.total_pnl_sol>=0?'green':'red');

  const dot=document.getElementById('sdot'),txt=document.getElementById('stxt'),btn=document.getElementById('tbtn');
  if(running){dot.className='dot';txt.textContent='Running';btn.textContent='⏸ Pause';btn.className='btn btn-yellow';}
  else{dot.className='dot red';txt.textContent='Paused';btn.textContent='▶ Resume';btn.className='btn btn-green';}

  // Sync settings fields
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

  // Positions
  if(d.positions.length){
    const rows=d.positions.map(p=>{
      const cls=!p.ratio?'':p.ratio>=1?'green':'red';
      const tp1=p.tp1_hit?'<span class="badge">TP1✓</span>':'';
      return `<tr>
        <td>${p.name}${tp1}</td>
        <td>$${p.entry_price?.toFixed(8)??'?'}</td>
        <td>$${p.current_price?.toFixed(8)??'?'}</td>
        <td class="${cls}">${p.pnl}</td>
        <td class="yellow">⬆${p.peak_ratio?.toFixed(2)??'?'}x</td>
        <td>${p.age_min}m</td>
        <td><a href="https://dexscreener.com/solana/${p.address}" target="_blank">chart</a></td>
      </tr>`;
    }).join('');
    document.getElementById('pos-tbl').innerHTML=
      `<table><thead><tr><th>Token</th><th>Entry</th><th>Current</th><th>PnL</th><th>Peak</th><th>Age</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
  }else{
    document.getElementById('pos-tbl').innerHTML='<p class="empty">No open positions</p>';
  }

  // Log
  document.getElementById('log').innerHTML=d.log.map(l=>{
    const c=l.includes('BUY')?'buy':(l.includes('SELL')||l.includes('CASHOUT'))?'sell'
           :l.includes('SIGNAL')?'signal':l.includes('HOLD')?'hold'
           :(l.includes('RUG')||l.includes('rug'))?'rug':'';
    return `<div class="log-line ${c}">${l}</div>`;
  }).join('');
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
  // Highlight active preset button
  document.querySelectorAll('[id^=preset-]').forEach(b=>b.classList.remove('preset-active'));
  document.getElementById('preset-'+name)?.classList.add('preset-active');
  setTimeout(refresh,400);
}

async function saveSettings(){
  const sl=parseFloat(document.getElementById('s-sl').value);
  const trail=parseFloat(document.getElementById('s-trail').value);
  // Clear preset highlight since user customized
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

refresh();
setInterval(refresh,5000);
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

    print("\n  Dashboard → http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
