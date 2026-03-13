"""
SaaS Trading Bot Platform — SolTrader
Run: python app.py
Then open: http://localhost:5000

Required .env variables:
  SECRET_KEY, FERNET_KEY, HELIUS_RPC,
  STRIPE_SECRET_KEY, STRIPE_PRICE_BASIC, STRIPE_PRICE_PRO, ADMIN_EMAIL
"""

import os, threading, time, base64, json, requests, base58, secrets, hashlib
import psycopg2
import psycopg2.extras
import string
from collections import deque
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, redirect, url_for, session, jsonify, Response
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from cryptography.fernet import Fernet
import bcrypt
import stripe

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.expanduser("~"), "Desktop", ".env"))

# ── Config ─────────────────────────────────────────────────────────────────────
SECRET_KEY         = os.getenv("SECRET_KEY", secrets.token_hex(32))
_fkey              = os.getenv("FERNET_KEY")
FERNET_KEY         = _fkey.encode() if _fkey else Fernet.generate_key()
HELIUS_RPC         = os.getenv("HELIUS_RPC", "")
STRIPE_SECRET      = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_BASIC = os.getenv("STRIPE_PRICE_BASIC", "")
STRIPE_PRICE_PRO   = os.getenv("STRIPE_PRICE_PRO", "")
ADMIN_EMAIL        = os.getenv("ADMIN_EMAIL", "admin@admin.com")
ADMIN_EMAILS       = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", ADMIN_EMAIL).split(",") if e.strip()}
PERF_FEE_BASIC     = 0.15   # 15% of profits
PERF_FEE_PRO       = 0.10   # 10% of profits
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

RAYDIUM_AMM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
PUMP_FUN    = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

PLAN_LIMITS = {
    "basic": {"max_buy_sol": 0.1,  "label": "Basic Plan — $29/mo"},
    "pro":   {"max_buy_sol": 1.0,  "label": "Pro Plan — $49/mo"},
    "trial": {"max_buy_sol": 0.05, "label": "Free Trial (7 days)"},
}

PRESETS = {
    "safe": {
        "label":"Safe — Low Risk / Consistent",
        "description":"Small positions, tight stops. Capital preservation first.",
        "max_buy_sol":0.02,"tp1_mult":1.3,"tp2_mult":2.0,
        "trail_pct":0.15,"stop_loss":0.85,"max_age_min":20,"time_stop_min":20,
        "min_liq":25000,"min_mc":10000,"max_mc":80000,"priority_fee":10000,
        "anti_rug":True,"check_holders":True,"max_correlated":2,"drawdown_limit_sol":0.3,
    },
    "balanced": {
        "label":"Balanced — Medium Risk / Steady Profit",
        "description":"Moderate positions, balanced take-profits. Best for most markets.",
        "max_buy_sol":0.04,"tp1_mult":1.5,"tp2_mult":3.0,
        "trail_pct":0.20,"stop_loss":0.75,"max_age_min":30,"time_stop_min":30,
        "min_liq":10000,"min_mc":5000,"max_mc":150000,"priority_fee":30000,
        "anti_rug":True,"check_holders":True,"max_correlated":3,"drawdown_limit_sol":0.5,
    },
    "degen": {
        "label":"Degen — High Risk / Max Profit",
        "description":"Larger positions, wide stops. For hot markets only.",
        "max_buy_sol":0.10,"tp1_mult":2.0,"tp2_mult":10.0,
        "trail_pct":0.30,"stop_loss":0.60,"max_age_min":10,"time_stop_min":60,
        "min_liq":5000,"min_mc":2000,"max_mc":250000,"priority_fee":100000,
        "anti_rug":True,"check_holders":False,"max_correlated":5,"drawdown_limit_sol":1.0,
    },
}
# keep backward compat
PRESETS["steady"] = PRESETS["balanced"]
PRESETS["max"]    = PRESETS["degen"]

# ── Database ───────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set. Add a PostgreSQL database to your Railway project.")

def db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn

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
            mint TEXT, name TEXT,
            passed INTEGER DEFAULT 0,
            reason TEXT,
            score INTEGER DEFAULT 0,
            ts TIMESTAMP DEFAULT NOW()
        )""")
        conn.commit()
    finally:
        conn.close()

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
        ]
        for m in migrations:
            try:
                cur.execute(m)
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
        conn.commit()
    finally:
        conn.close()
migrate_db()

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
    except:
        pass

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
                conn.close()
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
                    conn.close()
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

threading.Thread(target=send_daily_summaries, daemon=True).start()

# ── Encryption ─────────────────────────────────────────────────────────────────
def encrypt_key(private_key_b58: str) -> str:
    return fernet.encrypt(private_key_b58.encode()).decode()

def decrypt_key(encrypted: str) -> str:
    return fernet.decrypt(encrypted.encode()).decode()

# ── Per-user bot instances ─────────────────────────────────────────────────────
user_bots    = {}
seen_tokens  = set()
market_feed  = deque(maxlen=100)   # live token stream for market board

def ai_score(info):
    """Score a token 0-100 based on multiple signals."""
    score = 0
    vol   = info.get("vol", 0)
    liq   = info.get("liq", 0)
    mc    = info.get("mc", 0)
    age   = info.get("age_min", 9999)
    chg   = info.get("change", 0)
    mom   = info.get("momentum", 0)
    # Volume score (0-25)
    if vol > 100000: score += 25
    elif vol > 50000: score += 18
    elif vol > 10000: score += 10
    elif vol > 1000:  score += 5
    # Liquidity score (0-20)
    if liq > 50000: score += 20
    elif liq > 20000: score += 14
    elif liq > 5000:  score += 8
    elif liq > 1000:  score += 3
    # Age score — fresh tokens score higher (0-20)
    if age < 5:    score += 20
    elif age < 15: score += 14
    elif age < 30: score += 8
    elif age < 60: score += 3
    # Price change (0-20)
    if chg > 100: score += 20
    elif chg > 50: score += 15
    elif chg > 20: score += 10
    elif chg > 5:  score += 5
    elif chg < -20: score -= 10
    # Momentum (0-15)
    score += int(mom * 0.15)
    return max(0, min(100, score))

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
    except:
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
        conn.close()
    return row is None

def blacklist_dev(dev_wallet, reason="rugged"):
    try:
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("INSERT INTO dev_blacklist (dev_wallet,reason) VALUES (%s,%s) ON CONFLICT DO NOTHING", (dev_wallet, reason))
            conn.commit()
        finally:
            conn.close()
    except: pass

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
            conn.close()
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
    wr = stats.get("win_rate", 50)
    if wr >= 60:
        return {"preset":"degen","reason":f"Win rate is {wr}% — market is hot. Degen mode for max returns."}
    elif wr >= 45:
        return {"preset":"balanced","reason":f"Win rate is {wr}% — steady market. Balanced preset recommended."}
    else:
        return {"preset":"safe","reason":f"Win rate is {wr}% — choppy market. Safe preset to protect capital."}

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
    def __init__(self, user_id, keypair, settings, run_mode, run_duration_min, profit_target_sol):
        self.user_id          = user_id
        self.keypair          = keypair
        self.wallet           = str(keypair.pubkey())
        self.settings         = dict(settings)
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
        self.session_drawdown = 0.0
        self.filter_log       = deque(maxlen=50)   # real-time filter results for UI

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
                    "INSERT INTO filter_log (mint,name,passed,reason,score) VALUES (%s,%s,%s,%s,%s)",
                    (mint, name, int(passed), reason, score)
                )
                conn.commit()
            finally:
                conn.close()
        except: pass

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
        try:
            r = requests.post(HELIUS_RPC, json={
                "jsonrpc":"2.0","id":1,"method":"getBalance","params":[self.wallet]
            }, timeout=5)
            self.sol_balance = r.json().get("result",{}).get("value",0) / 1e9
        except:
            pass

    def sign_and_send(self, swap_tx_b64):
        raw    = base64.b64decode(swap_tx_b64)
        tx     = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(tx.message, [self.keypair])
        enc    = base64.b64encode(bytes(signed)).decode()
        r = requests.post(HELIUS_RPC, json={
            "jsonrpc":"2.0","id":1,"method":"sendTransaction",
            "params":[enc,{"encoding":"base64","skipPreflight":False,
                           "preflightCommitment":"confirmed","maxRetries":3}]
        }, timeout=15)
        res = r.json()
        return res.get("result") if "result" in res else None

    def jupiter_quote(self, input_mint, output_mint, amount, slippage_bps=1500):
        r = requests.get(
            f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint}"
            f"&outputMint={output_mint}&amount={amount}&slippageBps={slippage_bps}",
            timeout=10
        ).json()
        return None if "error" in r else r

    def jupiter_swap(self, quote):
        r = requests.post("https://quote-api.jup.ag/v6/swap", json={
            "quoteResponse":quote,"userPublicKey":self.wallet,"wrapAndUnwrapSol":True,
            "prioritizationFeeLamports":int(self.settings.get("priority_fee",10000)),
        }, timeout=10).json()
        return r.get("swapTransaction")

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
            pairs = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                headers=HEADERS, timeout=5
            ).json().get("pairs")
            if pairs:
                return float(pairs[0].get("priceUsd") or 0)
        except:
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
        except:
            return True

    def buy(self, mint, name, price, liq=0, dev_wallet=None):
        s = self.settings
        # Drawdown limit
        if s.get("drawdown_limit_sol",0) > 0 and self.session_drawdown >= s["drawdown_limit_sol"]:
            self.log_filter(name, mint, False, f"Drawdown limit reached ({self.session_drawdown:.3f} SOL)")
            return
        # Correlated position limit
        if len(self.positions) >= s.get("max_correlated", 5):
            self.log_filter(name, mint, False, f"Max correlated positions ({len(self.positions)})")
            return
        if self.sol_balance < s["max_buy_sol"] + 0.01:
            self.log_filter(name, mint, False, f"Low balance ({self.sol_balance:.4f} SOL)")
            return
        if mint in self.positions:
            return
        # Anti-rug / mint auth check
        if s.get("anti_rug") and not self.is_safe_token(mint):
            self.log_filter(name, mint, False, "RUG RISK — mint/freeze auth active")
            self.log_msg(f"RUG RISK — skip {name}")
            return
        # Dev blacklist
        if dev_wallet and not check_dev_blacklist(dev_wallet):
            self.log_filter(name, mint, False, f"Dev blacklisted ({dev_wallet[:8]}...)")
            return
        # Holder concentration
        if s.get("check_holders") and not check_holder_concentration(mint):
            self.log_filter(name, mint, False, "Top 5 holders own >50% of supply")
            self.log_msg(f"HOLDER RISK — skip {name}")
            return
        # Dynamic slippage
        slippage = dynamic_slippage_bps(liq)
        quote = self.jupiter_quote(SOL_MINT, mint, int(s["max_buy_sol"]*1e9), slippage)
        if not quote:
            self.log_filter(name, mint, False, "No Jupiter quote available")
            return
        swap_tx = self.jupiter_swap(quote)
        if not swap_tx:
            self.log_filter(name, mint, False, "Jupiter swap build failed")
            return
        sig = self.sign_and_send(swap_tx)
        if sig:
            self.positions[mint] = {
                "name":name,"entry_price":price,"peak_price":price,
                "timestamp":time.time(),"tp1_hit":False,"entry_sol":s["max_buy_sol"],
                "dev_wallet": dev_wallet,
            }
            self.log_filter(name, mint, True, f"BUY @ ${price:.8f} | slip={slippage}bps", score=0)
            self.log_msg(f"BUY {name} @ ${price:.8f} | slip={slippage}bps | solscan.io/tx/{sig}")
            self.refresh_balance()
            try:
                conn = db()
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT telegram_chat_id FROM users WHERE id=%s", (self.user_id,))
                    u = cur.fetchone()
                finally:
                    conn.close()
                if u and u["telegram_chat_id"]:
                    send_telegram(u["telegram_chat_id"],
                        f"🟢 <b>BUY</b> {name}\n💰 ${price:.8f}\n📊 {s['max_buy_sol']} SOL\n🔗 solscan.io/tx/{sig}")
            except: pass
        else:
            self.log_filter(name, mint, False, "Transaction failed to send")

    def sell(self, mint, pct, reason):
        pos = self.positions.get(mint)
        if not pos:
            return
        try:
            total = self.get_token_balance(mint)
            if total == 0:
                del self.positions[mint]
                return
            amount = int(total * pct)
            if not amount:
                return
            quote = self.jupiter_quote(mint, SOL_MINT, amount)
            if not quote:
                return
            swap_tx = self.jupiter_swap(quote)
            if not swap_tx:
                return
            sig = self.sign_and_send(swap_tx)
            if sig:
                cur     = self.get_token_price(mint) or pos["entry_price"]
                pnl_pct = (cur / pos["entry_price"] - 1) * 100 if pos["entry_price"] else 0
                pnl_sol = pos["entry_sol"] * pct * (pnl_pct / 100)
                self.log_msg(f"SELL {pos['name']} {int(pct*100)}% — {reason} | {pnl_pct:+.1f}% ({pnl_sol:+.4f} SOL)")
                self.stats["total_pnl_sol"] += pnl_sol
                if pnl_sol < 0:
                    self.session_drawdown += abs(pnl_sol)
                # blacklist dev if rugged (big loss, fast)
                pos_age = (time.time() - pos.get("timestamp", time.time())) / 60
                if pnl_sol < -0.05 and pos_age < 5 and pos.get("dev_wallet"):
                    blacklist_dev(pos["dev_wallet"], "auto-rug-detected")
                    self.log_msg(f"⛔ Dev blacklisted: {pos['dev_wallet'][:8]}...")
                if pnl_pct >= 0:
                    self.stats["wins"] += 1
                else:
                    self.stats["losses"] += 1
                # Telegram alert
                try:
                    conn = db()
                    try:
                        _cur = conn.cursor()
                        _cur.execute("SELECT telegram_chat_id FROM users WHERE id=%s", (self.user_id,))
                        u = _cur.fetchone()
                    finally:
                        conn.close()
                    if u and u["telegram_chat_id"]:
                        emoji = "🟢" if pnl_pct >= 0 else "🔴"
                        send_telegram(u["telegram_chat_id"],
                            f"{emoji} <b>SELL</b> {pos['name']} {int(pct*100)}%\n"
                            f"📈 {pnl_pct:+.1f}% | {pnl_sol:+.4f} SOL\n"
                            f"📝 {reason}")
                except:
                    pass
                conn = db()
                try:
                    _cur = conn.cursor()
                    _cur.execute(
                        "INSERT INTO trades (user_id,mint,name,action,price,pnl_sol) VALUES (%s,%s,%s,%s,%s,%s)",
                        (self.user_id, mint, pos["name"], f"SELL-{reason}", cur, pnl_sol)
                    )
                    conn.commit()
                finally:
                    conn.close()
                if pct >= 1.0:
                    del self.positions[mint]
                else:
                    self.positions[mint]["tp1_hit"] = True
                self.refresh_balance()
        except Exception as e:
            self.log_msg(f"Sell error {pos['name']}: {e}")

    def check_positions(self):
        s = self.settings
        for mint in list(self.positions.keys()):
            pos = self.positions[mint]
            cur = self.get_token_price(mint)
            if not cur or not pos["entry_price"]:
                continue
            if cur > pos["peak_price"]:
                self.positions[mint]["peak_price"] = cur
            ratio      = cur / pos["entry_price"]
            peak_ratio = pos["peak_price"] / pos["entry_price"]
            age_min    = (time.time() - pos["timestamp"]) / 60
            trail_line = pos["peak_price"] * (1 - s["trail_pct"])

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
        if mint in self.positions or mint in seen_tokens:
            return
        s = self.settings
        if not (s.get("min_mc",0) <= mc <= s.get("max_mc",999999)):
            return
        if liq < s.get("min_liq",0):
            return
        if age_min > s.get("max_age_min",999):
            return
        if change < 0:
            return
        self.log_msg(f"SIGNAL {name} | MC:${mc:,.0f} Vol:${vol:,.0f} Liq:${liq:,.0f} | Age:{age_min:.0f}m +{change:.0f}%")
        self.log_filter(name, mint, True, f"Signal passed all filters | score={self.settings.get('_last_score',0)}")
        self.buy(mint, name, price, liq=liq, dev_wallet=None)

    def cashout_all(self):
        self.log_msg("CASHOUT ALL — selling all positions")
        for mint in list(self.positions.keys()):
            self.sell(mint, 1.0, "CASHOUT")

    def run(self):
        self.refresh_balance()
        self.start_balance = self.sol_balance
        self.log_msg(f"Bot started | Wallet: {self.wallet} | Balance: {self.sol_balance:.4f} SOL")
        while self.running:
            try:
                if self.should_stop():
                    self.cashout_all()
                    self.running = False
                    self.record_perf_fee()
                    break
                if self.positions:
                    self.check_positions()
                time.sleep(10)
            except Exception as e:
                self.log_msg(f"Loop error: {e}")
                time.sleep(10)

    def record_perf_fee(self):
        pnl = self.stats["total_pnl_sol"]
        if pnl <= 0:
            return
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT plan FROM users WHERE id=%s", (self.user_id,))
            row = cur.fetchone()
            plan = row["plan"] if row else "basic"
        finally:
            conn.close()
        pct = PERF_FEE_PRO if plan == "pro" else PERF_FEE_BASIC
        fee_sol = pnl * pct
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO perf_fees (user_id,pnl_sol,fee_sol) VALUES (%s,%s,%s)",
                (self.user_id, pnl, fee_sol)
            )
            conn.commit()
        finally:
            conn.close()
        self.log_msg(f"Performance fee: {fee_sol:.4f} SOL ({int(pct*100)}% of {pnl:.4f} SOL profit) — collecting…")
        if FEE_WALLET:
            sig = send_sol(self.keypair, FEE_WALLET, fee_sol)
            if sig:
                self.log_msg(f"✅ Fee collected: {fee_sol:.4f} SOL → solscan.io/tx/{sig}")
                conn = db()
                try:
                    cur = conn.cursor()
                    cur.execute("UPDATE perf_fees SET charged=1 WHERE user_id=%s AND charged=0", (self.user_id,))
                    conn.commit()
                finally:
                    conn.close()
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
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",  # known Solana alpha wallet
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",  # top SOL trader
    "Hx6LbkMHe69DBeXDM9RMqMJG41J9YGfxsGFm8BhcPXb",  # meme coin sniper
    "5tzFkiKscXHK5ZXCGbGuykB2NZuNQn8aVJHsZcFKpNGe",  # dex whale
]
_whale_seen = set()

def check_whale_wallets():
    """Poll recent transactions of whale wallets and copy their buys."""
    time.sleep(15)  # let app fully start first
    while True:
        try:
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
                                    pairs = requests.get(
                                        f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                                        headers=HEADERS, timeout=5
                                    ).json().get("pairs")
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
                                    for bot in list(user_bots.values()):
                                        if bot.running and mint not in bot.positions:
                                            bot.log_msg(f"🐋 WHALE COPY: {name} ({wallet[:8]}...)")
                                            try:
                                                bot.evaluate_signal(mint, name, price, mc, vol, liq, age_min, 0)
                                            except:
                                                pass
                                except:
                                    pass
                except:
                    pass
            time.sleep(20)
        except Exception as e:
            print(f"Whale tracker error: {e}")
            time.sleep(30)

threading.Thread(target=check_whale_wallets, daemon=True).start()

def global_scanner():
    time.sleep(10)  # let app fully start first
    while True:
        try:
            r = requests.get(
                "https://api.dexscreener.com/token-profiles/latest/v1",
                headers=HEADERS, timeout=10
            )
            if r.status_code != 200:
                time.sleep(10)
                continue
            tokens = r.json() if isinstance(r.json(), list) else []
            for t in tokens:
                if t.get("chainId") != "solana":
                    continue
                mint = t.get("tokenAddress")
                if not mint or mint in seen_tokens:
                    continue
                seen_tokens.add(mint)
                try:
                    pairs = requests.get(
                        f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                        headers=HEADERS, timeout=5
                    ).json().get("pairs")
                    if not pairs:
                        continue
                    p = pairs[0]
                    created_at = p.get("pairCreatedAt")
                    age_min = (time.time()*1000 - created_at)/60000 if created_at else 9999
                    vol    = (p.get("volume") or {}).get("h24", 0) or 0
                    change = (p.get("priceChange") or {}).get("h1", 0) or 0
                    momentum = volume_velocity(mint, vol)
                    info = {
                        "name":     p.get("baseToken",{}).get("name","Unknown"),
                        "symbol":   p.get("baseToken",{}).get("symbol","?"),
                        "price":    float(p.get("priceUsd") or 0),
                        "mc":       p.get("marketCap",0) or 0,
                        "vol":      vol,
                        "liq":      (p.get("liquidity") or {}).get("usd",0) or 0,
                        "age_min":  age_min,
                        "change":   change,
                        "momentum": momentum,
                        "social":   check_social_signals(p),
                    }
                    if not info["price"]:
                        continue
                    info["mint"] = mint
                    info["score"] = ai_score(info)
                    info["score"] = min(100, info["score"] + check_social_signals(str(p)))
                    info["ts"] = int(time.time())
                    market_feed.appendleft(info)
                    for bot in list(user_bots.values()):
                        if bot.running:
                            try:
                                # boost signal on high momentum tokens
                                effective_change = info["change"]
                                if momentum >= 60:
                                    bot.log_msg(f"⚡ MOMENTUM SPIKE: {info['name']} score={momentum} (velocity)")
                                    effective_change = max(effective_change, 25)
                                bot.evaluate_signal(
                                    mint, info["name"], info["price"],
                                    info["mc"], info["vol"], info["liq"],
                                    info["age_min"], effective_change
                                )
                            except:
                                pass
                except:
                    pass
            time.sleep(10)
        except Exception as e:
            print(f"Scanner error: {e}")
            time.sleep(10)

threading.Thread(target=global_scanner, daemon=True).start()

# ── Helius websocket pool sniping ──────────────────────────────────────────────
def helius_pool_sniper():
    """Listen for new Raydium/Pump.fun pool creation via Helius logs subscription."""
    import json as _json
    ws_url = HELIUS_RPC.replace("https://", "wss://").replace("http://", "ws://")
    # Extract API key from RPC URL if present
    api_key = ""
    if "api-key=" in HELIUS_RPC:
        api_key = HELIUS_RPC.split("api-key=")[-1]
    elif "?" in HELIUS_RPC:
        api_key = HELIUS_RPC.split("?")[-1].replace("api-key=","")

    while True:
        try:
            import websocket as ws_lib
            wss = f"wss://atlas-mainnet.helius-rpc.com/?api-key={api_key}" if api_key else ws_url

            def on_open(ws):
                # Subscribe to logs mentioning Raydium AMM (new pool creation)
                sub = _json.dumps({
                    "jsonrpc":"2.0","id":1,"method":"logsSubscribe",
                    "params":[
                        {"mentions": [RAYDIUM_AMM]},
                        {"commitment": "confirmed"}
                    ]
                })
                ws.send(sub)
                print("[Sniper] Subscribed to Raydium pool creation logs")

            def on_message(ws, message):
                try:
                    data = _json.loads(message)
                    result = data.get("params",{}).get("result",{})
                    value  = result.get("value",{})
                    logs   = value.get("logs",[])
                    sig    = value.get("signature","")
                    # Check if this is a new pool init
                    if any("initialize" in l.lower() or "initializemint" in l.lower() for l in logs):
                        # Extract mint from account keys (simplified)
                        tx_r = requests.post(HELIUS_RPC, json={
                            "jsonrpc":"2.0","id":1,"method":"getTransaction",
                            "params":[sig,{"encoding":"jsonParsed","maxSupportedTransactionVersion":0}]
                        }, timeout=8)
                        tx = tx_r.json().get("result")
                        if not tx: return
                        # Get new token mints from post balances
                        post_bals = (tx.get("meta") or {}).get("postTokenBalances",[])
                        for pb in post_bals:
                            mint = pb.get("mint","")
                            if not mint or mint == SOL_MINT or mint in seen_tokens:
                                continue
                            seen_tokens.add(mint)
                            # Fetch info and add to market feed immediately
                            try:
                                pairs = requests.get(
                                    f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                                    headers=HEADERS, timeout=5
                                ).json().get("pairs")
                                if not pairs: return
                                p = pairs[0]
                                created_at = p.get("pairCreatedAt")
                                age_min = (time.time()*1000 - created_at)/60000 if created_at else 0
                                vol    = (p.get("volume") or {}).get("h24",0) or 0
                                change = (p.get("priceChange") or {}).get("h1",0) or 0
                                liq    = (p.get("liquidity") or {}).get("usd",0) or 0
                                info = {
                                    "name":   p.get("baseToken",{}).get("name","Unknown"),
                                    "symbol": p.get("baseToken",{}).get("symbol","?"),
                                    "price":  float(p.get("priceUsd") or 0),
                                    "mc":     p.get("marketCap",0) or 0,
                                    "vol":    vol,"liq":liq,"age_min":age_min,
                                    "change": change,"momentum":0,"mint":mint,
                                    "sniped": True,  # flag as websocket-sniped
                                }
                                info["score"] = ai_score(info)
                                info["ts"]    = int(time.time())
                                market_feed.appendleft(info)
                                for bot in list(user_bots.values()):
                                    if bot.running:
                                        bot.log_msg(f"⚡ SNIPE CANDIDATE: {info['name']} (on-chain detect)")
                                        try:
                                            bot.evaluate_signal(mint,info["name"],info["price"],
                                                info["mc"],info["vol"],liq,age_min,change)
                                        except: pass
                            except: pass
                except: pass

            def on_error(ws, err): print(f"[Sniper] WS error: {err}")
            def on_close(ws, *a): print("[Sniper] WS closed — reconnecting...")

            ws_app = ws_lib.WebSocketApp(wss, on_open=on_open, on_message=on_message,
                                          on_error=on_error, on_close=on_close)
            ws_app.run_forever(ping_interval=30, ping_timeout=10)
        except ImportError:
            print("[Sniper] websocket-client not installed — skipping WS sniper")
            break
        except Exception as e:
            print(f"[Sniper] reconnecting in 10s: {e}")
            time.sleep(10)

threading.Thread(target=helius_pool_sniper, daemon=True).start()

def auto_restart_bots():
    """Restart bots that were running before a server restart."""
    time.sleep(5)  # wait for app to fully initialize
    try:
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT bs.user_id, bs.preset, bs.run_mode, bs.run_duration_min, bs.profit_target_sol,
                       w.encrypted_key, u.plan
                FROM bot_settings bs
                JOIN wallets w ON w.user_id = bs.user_id
                JOIN users u ON u.id = bs.user_id
                WHERE bs.is_running = 1
            """)
            rows = cur.fetchall()
        finally:
            conn.close()
        for row in rows:
            uid = row["user_id"]
            try:
                kp = Keypair.from_bytes(base58.b58decode(decrypt_key(row["encrypted_key"])))
                settings = dict(PRESETS.get(row["preset"], PRESETS["balanced"]))
                max_sol = PLAN_LIMITS.get(row["plan"], PLAN_LIMITS["trial"])["max_buy_sol"]
                settings["max_buy_sol"] = min(settings["max_buy_sol"], max_sol)
                bot = BotInstance(uid, kp, settings,
                    run_mode=row["run_mode"],
                    run_duration_min=row["run_duration_min"],
                    profit_target_sol=row["profit_target_sol"])
                user_bots[uid] = bot
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
                    conn.close()
    except Exception:
        pass

threading.Thread(target=auto_restart_bots, daemon=True).start()

# ── Flask app ──────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static", static_url_path="/static")
app.secret_key = SECRET_KEY

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("email","").lower() not in ADMIN_EMAILS:
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated

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
            hashed     = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            trial_ends = (datetime.utcnow() + timedelta(days=7)).isoformat()
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
                    conn.close()
            try:
                conn = db()
                try:
                    cur = conn.cursor()
                    cur.execute(
                        "INSERT INTO users (email, password_hash, plan, trial_ends, referral_code, referred_by) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                        (email, hashed, 'trial', trial_ends, ref_code, ref_by)
                    )
                    user_id = cur.fetchone()["id"]
                    cur.execute("INSERT INTO bot_settings (user_id) VALUES (%s)", (user_id,))
                    if ref_by:
                        cur.execute("INSERT INTO referrals (referrer_id,referred_id) VALUES (%s,%s)",
                                     (ref_by, user_id))
                    conn.commit()
                finally:
                    conn.close()
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
                conn.close()
            if user and bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
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
        preset      = request.form.get("preset","steady")
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
                conn.close()
            return redirect(url_for("dashboard"))
        except Exception as e:
            error = f"Invalid private key: {e}"
    return Response(SETUP_HTML.replace("{{ERROR}}", error), mimetype="text/html")

# ── Dashboard ──────────────────────────────────────────────────────────────────
@app.route("/dashboard")
@login_required
def dashboard():
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
        conn.close()
    if not user:
        session.clear()
        return redirect(url_for("login"))
    if not wallet:
        return redirect(url_for("setup"))
    plan_info = PLAN_LIMITS.get(user["plan"], PLAN_LIMITS["trial"])
    return Response(DASHBOARD_HTML
        .replace("{{EMAIL}}", user["email"])
        .replace("{{PLAN}}", plan_info["label"])
        .replace("{{WALLET}}", wallet["public_key"])
        .replace("{{PRESET}}", bsettings["preset"] if bsettings else "steady"),
        mimetype="text/html"
    )

# ── API ────────────────────────────────────────────────────────────────────────
@app.route("/api/state")
@login_required
def api_state():
    uid = session["user_id"]
    bot = user_bots.get(uid)
    pos_list = []
    if bot:
        for mint, p in bot.positions.items():
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
    return jsonify({
        "running":    bot.running if bot else False,
        "balance":    round(bot.sol_balance, 4) if bot else 0,
        "positions":  pos_list,
        "log":        bot.log[:60] if bot else [],
        "stats":      bot.stats if bot else {"wins":0,"losses":0,"total_pnl_sol":0},
        "filter_log": list(bot.filter_log)[:10] if bot else [],
    })

@app.route("/api/start", methods=["POST"])
@login_required
def api_start():
    uid = session["user_id"]
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
        conn.close()
    if not w:
        return jsonify({"ok":False,"msg":"Wallet not configured"})
    if u["plan"] == "trial":
        trial_ends = datetime.fromisoformat(u["trial_ends"]) if u["trial_ends"] else datetime.utcnow()
        if datetime.utcnow() > trial_ends:
            return jsonify({"ok":False,"msg":"Trial expired — please subscribe"})
    kp       = Keypair.from_bytes(base58.b58decode(decrypt_key(w["encrypted_key"])))
    preset   = bs["preset"] if bs else "balanced"
    settings = dict(PRESETS.get(preset, PRESETS["balanced"]))
    max_sol  = PLAN_LIMITS.get(u["plan"],PLAN_LIMITS["trial"])["max_buy_sol"]
    settings["max_buy_sol"] = min(settings["max_buy_sol"], max_sol)
    bot = BotInstance(
        uid, kp, settings,
        run_mode          = bs["run_mode"] if bs else "indefinite",
        run_duration_min  = bs["run_duration_min"] if bs else 0,
        profit_target_sol = bs["profit_target_sol"] if bs else 0,
    )
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
        conn.close()
    return jsonify({"ok":True})

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
            conn.close()
    return jsonify({"ok":True})

@app.route("/api/cashout", methods=["POST"])
@login_required
def api_cashout():
    uid = session["user_id"]
    bot = user_bots.get(uid)
    if bot:
        bot.cashout_all()
    return jsonify({"ok":True})

@app.route("/api/settings", methods=["POST"])
@login_required
def api_settings():
    uid  = session["user_id"]
    data = request.json
    preset   = data.get("preset","balanced")
    run_mode = data.get("run_mode","indefinite")
    duration = int(data.get("run_duration_min",0) or 0)
    profit   = float(data.get("profit_target_sol",0) or 0)
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE bot_settings SET preset=%s,run_mode=%s,run_duration_min=%s,profit_target_sol=%s
            WHERE user_id=%s
        """, (preset, run_mode, duration, profit, uid))
        conn.commit()
    finally:
        conn.close()
    bot = user_bots.get(uid)
    if bot:
        bot.settings.update(PRESETS.get(preset, bot.settings))
        bot.run_mode         = run_mode
        bot.run_duration_min = duration
        bot.profit_target    = profit
    return jsonify({"ok":True})

@app.route("/api/market-feed")
@login_required
def api_market_feed():
    """SSE stream of live market tokens."""
    def generate():
        last = 0
        while True:
            tokens = [t for t in market_feed if t.get("ts", 0) > last]
            if tokens:
                last = tokens[0].get("ts", last)
                yield f"data: {json.dumps(tokens[:20])}\n\n"
            time.sleep(3)
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/manual-buy", methods=["POST"])
@login_required
def api_manual_buy():
    uid  = session["user_id"]
    mint = (request.json or {}).get("mint", "")
    name = (request.json or {}).get("name", "Unknown")
    if not mint:
        return jsonify({"ok": False, "msg": "No mint provided"})
    bot = user_bots.get(uid)
    if not bot or not bot.running:
        return jsonify({"ok": False, "msg": "Bot not running — start bot first"})
    if mint in bot.positions:
        return jsonify({"ok": False, "msg": "Already in position"})
    # get current price
    try:
        pairs = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                             headers=HEADERS, timeout=5).json().get("pairs")
        price = float(pairs[0].get("priceUsd") or 0) if pairs else 0
    except:
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
        conn.close()
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
        cur.execute("SELECT * FROM filter_log ORDER BY id DESC LIMIT 30")
        rows = cur.fetchall()
    finally:
        conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/admin/suggest-settings")
@admin_required
def api_suggest_settings():
    stats = get_market_stats()
    suggestion = ai_suggest_settings(stats)
    return jsonify({"stats": stats, "suggestion": suggestion})

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
        conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/admin/override-preset", methods=["POST"])
@admin_required
def api_override_preset():
    data   = request.json or {}
    preset = data.get("preset","balanced")
    if preset in PRESETS:
        # merge incoming settings into PRESETS dict in memory
        for k, v in data.items():
            if k != "preset":
                PRESETS[preset][k] = v
        # propagate to running bots on this preset
        conn = db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM bot_settings WHERE preset=%s AND is_running=1", (preset,))
            rows = cur.fetchall()
        finally:
            conn.close()
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
        conn.close()
    code = u["referral_code"] or ""
    link = f"https://soltrader-production.up.railway.app/ref/{code}"
    return jsonify({"ok": True, "code": code, "link": link,
                    "referrals": count, "earnings_sol": u["referral_earnings_sol"] or 0})

# ── Stripe ─────────────────────────────────────────────────────────────────────
@app.route("/subscribe/<plan>")
@login_required
def subscribe(plan):
    uid      = session["user_id"]
    email    = session["email"]
    price_id = STRIPE_PRICE_BASIC if plan=="basic" else STRIPE_PRICE_PRO
    if not price_id or not STRIPE_SECRET:
        return "Stripe not configured.", 500
    try:
        checkout = stripe.checkout.Session.create(
            customer_email=email,
            payment_method_types=["card"],
            line_items=[{"price":price_id,"quantity":1}],
            mode="subscription",
            success_url=request.host_url+"subscribe/success?plan="+plan,
            cancel_url=request.host_url+"dashboard",
        )
        return redirect(checkout.url)
    except Exception as e:
        return f"Stripe error: {e}", 500

@app.route("/subscribe/success")
@login_required
def subscribe_success():
    uid  = session["user_id"]
    plan = request.args.get("plan","basic")
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET plan=%s WHERE id=%s", (plan, uid))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("dashboard"))

# ── Admin ──────────────────────────────────────────────────────────────────────
@app.route("/admin")
@login_required
@admin_required
def admin():
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users ORDER BY created_at DESC")
        users = cur.fetchall()
        cur.execute("SELECT * FROM trades ORDER BY timestamp DESC LIMIT 100")
        trades = cur.fetchall()
        cur.execute("SELECT * FROM perf_fees ORDER BY created_at DESC")
        fees = cur.fetchall()
    finally:
        conn.close()
    active        = sum(1 for b in user_bots.values() if b.running)
    total_fee_sol = sum(f["fee_sol"] for f in fees)
    return Response(
        ADMIN_HTML
            .replace("{{USERS}}", str(len(users)))
            .replace("{{ACTIVE}}", str(active))
            .replace("{{FEE_SOL}}", f"{total_fee_sol:.4f}")
            .replace("{{USER_ROWS}}", "".join(
                f"<tr><td>{u['email']}</td><td>{u['plan']}</td>"
                f"<td>{'<span class=\"badge bg-grn\">Running</span>' if user_bots.get(u['id']) and user_bots[u['id']].running else '<span class=\"badge bg-muted\">Idle</span>'}</td>"
                f"<td>{str(u['created_at'])[:10]}</td></tr>"
                for u in users
            ))
            .replace("{{FEE_ROWS}}", "".join(
                f"<tr><td>{f['user_id']}</td><td>{f['pnl_sol']:.4f}</td>"
                f"<td class=\"c-gold\">{f['fee_sol']:.4f}</td><td>{str(f['created_at'])[:10]}</td></tr>"
                for f in fees
            ) or "<tr><td colspan='4' style='color:var(--t3);padding:16px 10px'>No fees recorded yet</td></tr>"),
        mimetype="text/html"
    )

# ── HTML Templates ─────────────────────────────────────────────────────────────
_CSS = """<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#07101E">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="SolTrader">
<link rel="manifest" href="/manifest.json">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
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
.lline{font-size:11px;padding:3px 0;border-bottom:1px solid rgba(255,255,255,.03);color:var(--t3);font-family:'SF Mono','Courier New',monospace;line-height:1.7}
.lbuy{color:var(--grn2)}.lsell{color:#F87171}.lsig{color:var(--gold2)}.linfo{color:var(--blue2)}
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
<nav class="nav">
  <a href="/" class="logo"><div class="logo-mark">S</div>SolTrader</a>
  <div class="nav-r">
    <a href="/login">Sign In</a>
    <a href="/signup" class="nbtn">Get Started Free</a>
  </div>
</nav>

<div style="text-align:center;padding:72px 24px 52px;max-width:640px;margin:0 auto">
  <div style="display:inline-flex;align-items:center;gap:7px;background:rgba(37,99,235,.1);border:1px solid rgba(37,99,235,.22);color:var(--blue2);padding:5px 14px;border-radius:20px;font-size:11.5px;font-weight:600;margin-bottom:24px;letter-spacing:.3px">
    NON-CUSTODIAL &nbsp;·&nbsp; YOUR KEYS, YOUR FUNDS
  </div>
  <h1 style="font-size:40px;font-weight:700;line-height:1.15;letter-spacing:-.8px;margin-bottom:16px">Automated Solana<br>Trading, Done Right</h1>
  <p style="font-size:15px;color:var(--t2);max-width:440px;margin:0 auto 36px;line-height:1.7">
    Institutional-grade trading algorithms with anti-rug protection, multi-stage exits, and real-time P&L tracking. Your private key never leaves your control.
  </p>
  <div style="display:flex;gap:12px;justify-content:center;flex-wrap:wrap">
    <a href="/signup" class="btn btn-primary" style="padding:13px 32px;font-size:15px">Start Free 7-Day Trial</a>
    <a href="#pricing" class="btn btn-ghost" style="padding:13px 24px;font-size:15px">View Pricing</a>
  </div>
  <div class="trust" style="margin-top:28px">
    <div class="titem">🔒 AES-256 Encrypted Keys</div>
    <div class="titem">🛡️ Anti-Rug Detection</div>
    <div class="titem">📊 Real-Time Dashboard</div>
    <div class="titem">⚡ Jupiter V6 Routing</div>
    <div class="titem">✅ Cancel Anytime</div>
  </div>
</div>

<div style="max-width:820px;margin:0 auto;padding:0 24px 52px">
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-bottom:56px">
    <div class="panel" style="text-align:center;padding:26px 20px">
      <div style="width:44px;height:44px;background:rgba(37,99,235,.12);border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:22px;margin:0 auto 12px">🎯</div>
      <div style="font-weight:600;font-size:14px;margin-bottom:6px">Smart Signal Detection</div>
      <div style="font-size:12.5px;color:var(--t2);line-height:1.6">Scans new Solana token launches with market cap, liquidity, and volume filters to find high-probability entries</div>
    </div>
    <div class="panel" style="text-align:center;padding:26px 20px">
      <div style="width:44px;height:44px;background:rgba(5,150,105,.12);border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:22px;margin:0 auto 12px">🛡️</div>
      <div style="font-weight:600;font-size:14px;margin-bottom:6px">Anti-Rug Protection</div>
      <div style="font-size:12.5px;color:var(--t2);line-height:1.6">Verifies mint authority, freeze authority, and holder concentration on every token before executing any buy</div>
    </div>
    <div class="panel" style="text-align:center;padding:26px 20px">
      <div style="width:44px;height:44px;background:rgba(217,119,6,.12);border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:22px;margin:0 auto 12px">📈</div>
      <div style="font-weight:600;font-size:14px;margin-bottom:6px">Multi-Stage Exits</div>
      <div style="font-size:12.5px;color:var(--t2);line-height:1.6">TP1, TP2, trailing stop-loss, and hard stop-loss. Systematically lock in profits while cutting losing trades fast</div>
    </div>
  </div>

  <div id="pricing" style="text-align:center;margin-bottom:32px">
    <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1.5px;color:var(--t3);margin-bottom:12px">Pricing</div>
    <h2 style="font-size:26px;font-weight:700;letter-spacing:-.4px;color:var(--t1);margin-bottom:8px">Simple, Performance-Based Pricing</h2>
    <p style="font-size:13px;color:var(--t2)">We only earn when you profit. Start with a 7-day free trial — no credit card required.</p>
  </div>

  <div class="plans" style="margin-bottom:48px">
    <div class="plan">
      <div class="plan-name">Basic</div>
      <div class="plan-price">$29<sub>/mo</sub></div>
      <div class="plan-fee">+ 15% performance fee on profits</div>
      <ul class="features">
        <li>Steady Profit strategy</li>
        <li>Up to 0.1 SOL per trade</li>
        <li>Anti-rug protection</li>
        <li>Live trading dashboard</li>
        <li>7-day free trial</li>
      </ul>
      <a href="/signup" class="btn btn-outline btn-full" style="margin-top:20px;padding:11px">Start Free Trial</a>
    </div>
    <div class="plan hot">
      <div class="plan-tag">Most Popular</div>
      <div class="plan-name">Pro</div>
      <div class="plan-price">$49<sub>/mo</sub></div>
      <div class="plan-fee">+ 10% performance fee on profits</div>
      <ul class="features">
        <li>All strategies + Custom presets</li>
        <li>Up to 1.0 SOL per trade</li>
        <li>Max Profit aggressive mode</li>
        <li>Priority transaction execution</li>
        <li>7-day free trial</li>
      </ul>
      <a href="/signup" class="btn btn-primary btn-full" style="margin-top:20px;padding:11px">Start Free Trial</a>
    </div>
  </div>

  <div class="panel" style="padding:24px 28px;background:rgba(37,99,235,.04);border-color:rgba(37,99,235,.18)">
    <div style="text-align:center;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:var(--t3);margin-bottom:18px">Basic vs Pro — Side by Side</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:24px;max-width:480px;margin:0 auto;font-size:12.5px;color:var(--t2)">
      <div>
        <div style="font-weight:600;color:var(--t1);margin-bottom:8px;font-size:13px">Basic</div>
        <div style="line-height:2">Max 0.1 SOL / trade<br>Steady strategy only<br>1.5x TP1 → 3x TP2<br>−25% hard stop loss<br>15% performance fee</div>
      </div>
      <div>
        <div style="font-weight:600;color:var(--blue2);margin-bottom:8px;font-size:13px">Pro</div>
        <div style="line-height:2">Max 1.0 SOL / trade<br>All strategies<br>2x TP1 → 10x TP2<br>−40% stop (aggressive)<br>10% performance fee</div>
      </div>
    </div>
  </div>
</div>

<div style="border-top:1px solid var(--b1);padding:20px 24px;text-align:center">
  <div style="font-size:11.5px;color:var(--t3)">SolTrader — Automated Solana Trading &nbsp;·&nbsp; Non-Custodial &nbsp;·&nbsp; Keys encrypted with AES-256</div>
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
/* Chart container */
.chart-wrap{position:relative;width:100%;background:#0d1117;border-radius:10px;overflow:hidden}
#market-chart{display:block;width:100%;cursor:crosshair}
.chart-legend{display:flex;gap:16px;margin-top:8px;font-size:11px;color:var(--t3)}
.leg-dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:4px}
/* Click tooltip */
.tooltip{display:none;position:fixed;z-index:9999;background:#0d1117;border:1px solid var(--grn);border-radius:14px;padding:20px;width:320px;box-shadow:0 20px 60px rgba(0,0,0,.8)}
.tooltip.show{display:block}
.tt-title{font-size:18px;font-weight:800;color:var(--t1);margin-bottom:4px}
.tt-sym{font-size:11px;color:var(--grn);text-transform:uppercase;letter-spacing:1px}
.tt-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:14px 0}
.tt-stat{background:var(--bg3);border-radius:8px;padding:10px}
.tt-stat-label{font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.tt-stat-val{font-size:15px;font-weight:700;color:var(--t1)}
.ai-score-wrap{margin:12px 0;background:var(--bg3);border-radius:10px;padding:14px}
.ai-score-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.ai-score-label{font-size:11px;color:var(--t3);text-transform:uppercase;letter-spacing:.5px}
.ai-score-num{font-size:24px;font-weight:900}
.ai-bar{height:8px;border-radius:4px;background:var(--bg2);overflow:hidden}
.ai-fill{height:100%;border-radius:4px;transition:width .6s}
.ai-verdict{font-size:11px;margin-top:6px;font-weight:600}
.tt-btns{display:flex;gap:8px;margin-top:14px}
.tt-btn{flex:1;padding:10px;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;border:none;transition:all .2s}
.tt-buy{background:var(--grn);color:#000}
.tt-buy:hover{background:#0fa86a}
.tt-chart{background:var(--bg3);color:var(--t1);border:1px solid var(--bdr)}
.tt-chart:hover{border-color:var(--grn)}
/* Ticker tape */
.ticker{background:var(--bg2);border-bottom:1px solid var(--bdr);padding:8px 0;overflow:hidden;white-space:nowrap}
.ticker-inner{display:inline-flex;gap:32px;animation:scroll 40s linear infinite}
@keyframes scroll{from{transform:translateX(0)}to{transform:translateX(-50%)}}
.tick-item{font-size:12px;color:var(--t2);display:flex;gap:8px;align-items:center}
.tick-name{font-weight:600;color:var(--t1)}
/* Layout */
.dash-grid{display:grid;grid-template-columns:340px 1fr;gap:16px;align-items:start}
@media(max-width:900px){.dash-grid{grid-template-columns:1fr}}
.live-dot{width:8px;height:8px;border-radius:50%;background:var(--grn);display:inline-block;margin-right:6px;animation:pulse 1.5s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(.8)}}
.market-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.token-count{font-size:12px;color:var(--t3)}
.filter-row{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.filter-btn{background:var(--bg3);border:1px solid var(--bdr);color:var(--t2);padding:5px 12px;border-radius:20px;font-size:11px;cursor:pointer;transition:all .2s}
.filter-btn.active{background:var(--grn);color:#000;border-color:var(--grn);font-weight:700}
.preset-card{background:var(--bg3);border:2px solid var(--bdr);border-radius:10px;padding:10px;text-align:center;cursor:pointer;transition:all .2s}
.preset-card:hover{border-color:var(--grn)}
.preset-card.active{border-color:var(--grn);background:rgba(20,199,132,.08)}
.pc-icon{font-size:20px;margin-bottom:4px}
.pc-name{font-size:12px;font-weight:700;color:var(--t1)}
.pc-desc{font-size:10px;color:var(--t3);margin-top:2px}
/* Filter pipeline */
.fp-item{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--bdr);font-size:12px}
.fp-pass{color:#14c784;font-weight:700;min-width:16px}
.fp-fail{color:#f23645;font-weight:700;min-width:16px}
.fp-name{font-weight:600;color:var(--t1);flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fp-reason{font-size:10px;color:var(--t3);text-align:right}
</style>

<nav class="nav">
  <a href="/" class="logo"><div class="logo-mark">S</div>SolTrader</a>
  <div class="nav-r">
    <span style="font-size:12px;color:var(--t3)">{{EMAIL}}</span>
    <a href="/setup">Settings</a>
    <a href="/subscribe/pro" class="nbtn">Upgrade to Pro</a>
    <a href="/logout" style="color:var(--t3)!important">Sign Out</a>
  </div>
</nav>

<!-- Ticker tape -->
<div class="ticker" id="ticker"><div class="ticker-inner" id="ticker-inner">Loading market data…</div></div>

<!-- Hover Tooltip -->
<div class="tooltip" id="tooltip">
  <div style="display:flex;justify-content:space-between;align-items:flex-start">
    <div>
      <div class="tt-title" id="tt-name">—</div>
      <div class="tt-sym" id="tt-sym">—</div>
    </div>
    <button onclick="closeTooltip()" style="background:none;border:none;color:var(--t3);font-size:18px;cursor:pointer">✕</button>
  </div>
  <div class="tt-grid">
    <div class="tt-stat"><div class="tt-stat-label">Price</div><div class="tt-stat-val" id="tt-price">—</div></div>
    <div class="tt-stat"><div class="tt-stat-label">1h Change</div><div class="tt-stat-val" id="tt-chg">—</div></div>
    <div class="tt-stat"><div class="tt-stat-label">Market Cap</div><div class="tt-stat-val" id="tt-mc">—</div></div>
    <div class="tt-stat"><div class="tt-stat-label">Liquidity</div><div class="tt-stat-val" id="tt-liq">—</div></div>
    <div class="tt-stat"><div class="tt-stat-label">Volume 24h</div><div class="tt-stat-val" id="tt-vol">—</div></div>
    <div class="tt-stat"><div class="tt-stat-label">Age</div><div class="tt-stat-val" id="tt-age">—</div></div>
  </div>
  <div class="ai-score-wrap">
    <div class="ai-score-header">
      <div class="ai-score-label">AI Signal Score</div>
      <div class="ai-score-num" id="tt-score">0</div>
    </div>
    <div class="ai-bar"><div class="ai-fill" id="tt-score-bar" style="width:0%"></div></div>
    <div class="ai-verdict" id="tt-verdict">—</div>
  </div>
  <div class="tt-btns">
    <button class="tt-btn tt-buy" id="tt-buy-btn" onclick="manualBuy()">⚡ AI Buy Now</button>
    <button class="tt-btn tt-chart" onclick="openChart()">📊 Chart</button>
  </div>
</div>
<div id="tooltip-overlay" style="display:none;position:fixed;inset:0;z-index:9998" onclick="closeTooltip()"></div>

<div class="wrap">
  <!-- Stats row -->
  <div class="stats" style="margin-bottom:16px">
    <div class="stat"><div class="slabel">SOL Balance</div><div class="sval" id="balance">—</div><div class="ssub">available</div></div>
    <div class="stat"><div class="slabel">Open Positions</div><div class="sval c-gold" id="pos-count">0</div></div>
    <div class="stat"><div class="slabel">Wins</div><div class="sval c-grn" id="wins">0</div></div>
    <div class="stat"><div class="slabel">Losses</div><div class="sval c-red" id="losses">0</div></div>
    <div class="stat"><div class="slabel">Session P&amp;L</div><div class="sval" id="pnl">+0.0000</div><div class="ssub">SOL</div></div>
  </div>

  <div class="dash-grid">
    <!-- Left panel: controls + positions + log -->
    <div>
      <div class="panel" style="margin-bottom:12px">
        <div class="sec-label">Bot Controls</div>
        <div class="row" style="margin-bottom:12px">
          <button class="btn btn-success" id="toggle-btn" onclick="toggleBot()">▶ Start Bot</button>
          <button class="btn btn-ghost" onclick="cashout()">↓ Cashout All</button>
        </div>
        <div style="display:flex;align-items:center;gap:8px">
          <div class="sdot sdot-off" id="sdot"></div>
          <span class="stxt" id="stxt" style="font-size:13px">Initializing…</span>
        </div>
      </div>

      <div class="panel" style="margin-bottom:12px">
        <div class="sec-label">Strategy</div>
        <div class="fgroup" style="margin-bottom:8px">
          <label class="flabel">Preset</label>
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:12px" id="preset-grid">
            <div class="preset-card" id="pc-safe" onclick="selectPreset('safe')">
              <div class="pc-icon">🛡️</div>
              <div class="pc-name">Safe</div>
              <div class="pc-desc">Low risk</div>
            </div>
            <div class="preset-card active" id="pc-balanced" onclick="selectPreset('balanced')">
              <div class="pc-icon">⚖️</div>
              <div class="pc-name">Balanced</div>
              <div class="pc-desc">Steady profit</div>
            </div>
            <div class="preset-card" id="pc-degen" onclick="selectPreset('degen')">
              <div class="pc-icon">🔥</div>
              <div class="pc-name">Degen</div>
              <div class="pc-desc">Max profit</div>
            </div>
          </div>
          <input type="hidden" id="s-preset" value="balanced">
        </div>
        <div class="fgroup" style="margin-bottom:8px">
          <label class="flabel">Run Mode</label>
          <select class="finput" id="s-mode" onchange="toggleStop(this.value)">
            <option value="indefinite">Run Indefinitely</option>
            <option value="duration">Run for Duration</option>
            <option value="profit">Stop at Profit Target</option>
          </select>
        </div>
        <div id="f-dur" style="display:none" class="fgroup" style="margin-bottom:8px">
          <label class="flabel">Duration (minutes)</label>
          <input class="finput" type="number" id="s-dur" placeholder="e.g. 60" min="1">
        </div>
        <div id="f-pft" style="display:none" class="fgroup" style="margin-bottom:8px">
          <label class="flabel">Profit Target (SOL)</label>
          <input class="finput" type="number" id="s-pft" placeholder="e.g. 0.5" step="0.01">
        </div>
        <button class="btn btn-primary" onclick="saveSettings()" style="width:100%;margin-top:4px">Save Settings</button>
      </div>

      <div class="panel" style="margin-bottom:12px">
        <div class="sec-label">Open Positions</div>
        <div id="pos-tbl"><div style="font-size:13px;color:var(--t3)">No open positions</div></div>
      </div>

      <div class="panel">
        <div class="sec-label">Activity Log</div>
        <div id="log" style="max-height:260px;overflow-y:auto"></div>
      </div>

      <div class="panel" style="margin-top:12px">
        <div class="sec-label">Filter Pipeline <span style="font-size:10px;color:var(--t3);font-weight:400">— real-time token screening</span></div>
        <div id="filter-pipe"><div style="font-size:12px;color:var(--t3)">Start the bot to see token filtering…</div></div>
      </div>
    </div>

    <!-- Right panel: live market board -->
    <div>
      <div class="panel">
        <div class="market-header">
          <div>
            <span class="live-dot"></span>
            <span style="font-weight:700;font-size:15px">Live Market Feed</span>
            <span class="token-count" id="token-count" style="margin-left:8px">0 tokens</span>
          </div>
          <div style="font-size:11px;color:var(--t3)">Click a dot to analyze · ⚡ buy</div>
        </div>
        <div class="filter-row">
          <button class="filter-btn active" onclick="setFilter('all',this)">All</button>
          <button class="filter-btn" onclick="setFilter('hot',this)">🔥 Hot (score 70+)</button>
          <button class="filter-btn" onclick="setFilter('new',this)">🆕 New (&lt;15min)</button>
          <button class="filter-btn" onclick="setFilter('whale',this)">🐋 Whale Picks</button>
        </div>
        <div class="chart-wrap">
          <canvas id="market-chart" height="420"></canvas>
        </div>
        <div class="chart-legend">
          <span><span class="leg-dot" style="background:#14c784"></span>AI Score 70+ (Hot)</span>
          <span><span class="leg-dot" style="background:#f5a623"></span>Score 45–69</span>
          <span><span class="leg-dot" style="background:#f23645"></span>Score &lt;45</span>
          <span style="margin-left:auto">Dot size = volume</span>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
let running = false;
let activeFilter = 'all';
let allTokens = [];
let selectedToken = null;
let hoveredIdx = -1;
let chartCanvas, chartCtx, chartDisplayTokens = [];

// ── Filters ──────────────────────────────────────────────────────────────────
function setFilter(f, btn) {
  activeFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderChart();
}

function applyFilter(tokens) {
  if (activeFilter === 'hot')   return tokens.filter(t => t.score >= 70);
  if (activeFilter === 'new')   return tokens.filter(t => t.age_min < 15);
  if (activeFilter === 'whale') return tokens.filter(t => t.whale);
  return tokens;
}

// ── Utilities ────────────────────────────────────────────────────────────────
function fmtNum(n) {
  if (!n) return '$0';
  if (n >= 1e6) return '$' + (n/1e6).toFixed(1) + 'M';
  if (n >= 1e3) return '$' + (n/1e3).toFixed(0) + 'K';
  return '$' + n.toFixed(0);
}

function scoreColor(s) {
  if (s >= 70) return '#14c784';
  if (s >= 45) return '#f5a623';
  return '#f23645';
}

function logP(p) { return p > 0 ? Math.log10(p) : -10; }

// ── Scatter Chart ─────────────────────────────────────────────────────────────
function getPad() { return { top: 30, right: 24, bottom: 52, left: 74 }; }

function renderChart() {
  if (!chartCanvas) return;
  const filtered = applyFilter(allTokens);
  chartDisplayTokens = filtered.slice(0, 100);
  document.getElementById('token-count').textContent = filtered.length + ' tokens';

  const pad = getPad();
  const W = chartCanvas.width, H = chartCanvas.height;
  const plotW = W - pad.left - pad.right;
  const plotH = H - pad.top - pad.bottom;

  chartCtx.clearRect(0, 0, W, H);
  chartCtx.fillStyle = '#0d1117';
  chartCtx.fillRect(0, 0, W, H);

  if (!chartDisplayTokens.length) {
    chartCtx.fillStyle = '#444';
    chartCtx.font = '13px monospace';
    chartCtx.textAlign = 'center';
    chartCtx.fillText('Waiting for market data… Start the bot to enable scanning.', W/2, H/2);
    return;
  }

  // Ranges
  const ages   = chartDisplayTokens.map(t => t.age_min || 0);
  const prices = chartDisplayTokens.map(t => t.price || 0).filter(p => p > 0);
  const maxAge = Math.max(...ages, 60);
  const minP   = prices.length ? Math.min(...prices) : 1e-9;
  const maxP   = prices.length ? Math.max(...prices) : 1;
  const logMin = logP(minP) - 0.3;
  const logMax = logP(maxP) + 0.3;
  const vols   = chartDisplayTokens.map(t => t.vol || 0);
  const maxVol = Math.max(...vols, 1);

  function toX(age)   { return pad.left + (age / maxAge) * plotW; }
  function toY(price) {
    if (!price || price <= 0) return pad.top + plotH;
    return pad.top + plotH - ((logP(price) - logMin) / (logMax - logMin)) * plotH;
  }

  // Grid
  chartCtx.strokeStyle = '#1a1f2e';
  chartCtx.lineWidth = 1;
  for (let a = 0; a <= maxAge; a += 10) {
    const x = toX(a);
    chartCtx.beginPath(); chartCtx.moveTo(x, pad.top); chartCtx.lineTo(x, pad.top + plotH); chartCtx.stroke();
  }
  const logSteps = [];
  for (let l = Math.floor(logMin); l <= Math.ceil(logMax); l++) logSteps.push(l);
  logSteps.forEach(l => {
    const y = pad.top + plotH - ((l - logMin) / (logMax - logMin)) * plotH;
    if (y >= pad.top && y <= pad.top + plotH) {
      chartCtx.beginPath(); chartCtx.moveTo(pad.left, y); chartCtx.lineTo(pad.left + plotW, y); chartCtx.stroke();
    }
  });

  // Axes
  chartCtx.strokeStyle = '#2a2d3e';
  chartCtx.lineWidth = 2;
  chartCtx.beginPath();
  chartCtx.moveTo(pad.left, pad.top);
  chartCtx.lineTo(pad.left, pad.top + plotH);
  chartCtx.lineTo(pad.left + plotW, pad.top + plotH);
  chartCtx.stroke();

  // X labels
  chartCtx.fillStyle = '#555';
  chartCtx.font = '11px monospace';
  chartCtx.textAlign = 'center';
  for (let a = 0; a <= maxAge; a += 10) chartCtx.fillText(a + 'm', toX(a), pad.top + plotH + 17);
  chartCtx.fillStyle = '#666';
  chartCtx.fillText('Time on Market (minutes)', pad.left + plotW / 2, H - 6);

  // Y labels
  chartCtx.textAlign = 'right';
  logSteps.forEach(l => {
    const y = pad.top + plotH - ((l - logMin) / (logMax - logMin)) * plotH;
    if (y >= pad.top && y <= pad.top + plotH) {
      const v = Math.pow(10, l);
      const lbl = v < 0.0001 ? v.toExponential(0) : v < 0.01 ? v.toFixed(4) : v < 1 ? v.toFixed(2) : v.toFixed(0);
      chartCtx.fillStyle = '#555';
      chartCtx.font = '10px monospace';
      chartCtx.fillText('$' + lbl, pad.left - 6, y + 4);
    }
  });
  chartCtx.save();
  chartCtx.translate(12, pad.top + plotH / 2);
  chartCtx.rotate(-Math.PI / 2);
  chartCtx.textAlign = 'center';
  chartCtx.fillStyle = '#666';
  chartCtx.font = '11px monospace';
  chartCtx.fillText('Price (USD, log)', 0, 0);
  chartCtx.restore();

  // Dots
  chartDisplayTokens.forEach((t, i) => {
    const x   = toX(t.age_min || 0);
    const y   = toY(t.price   || 0);
    const sc  = t.score || 0;
    const vol = t.vol   || 0;
    const r   = 5 + Math.min(10, (vol / maxVol) * 10);
    const isHov = hoveredIdx === i;
    const color = scoreColor(sc);

    if (sc >= 70) { chartCtx.shadowBlur = 14; chartCtx.shadowColor = '#14c784'; }
    chartCtx.beginPath();
    chartCtx.arc(x, y, isHov ? r + 3 : r, 0, Math.PI * 2);
    chartCtx.fillStyle = isHov ? '#fff' : color + 'cc';
    chartCtx.fill();
    chartCtx.shadowBlur = 0;
    chartCtx.strokeStyle = isHov ? color : color + '66';
    chartCtx.lineWidth = isHov ? 2 : 1;
    chartCtx.stroke();

    if (isHov) {
      const lbl = (t.symbol || t.name || '?').slice(0, 12);
      chartCtx.font = 'bold 12px monospace';
      chartCtx.fillStyle = '#fff';
      chartCtx.textAlign = 'center';
      chartCtx.fillText(lbl, x, y - r - 6);
    }
  });
}

function initChart() {
  chartCanvas = document.getElementById('market-chart');
  chartCtx    = chartCanvas.getContext('2d');

  function resize() {
    chartCanvas.width  = chartCanvas.parentElement.clientWidth;
    chartCanvas.height = 420;
    renderChart();
  }
  resize();
  window.addEventListener('resize', resize);

  chartCanvas.addEventListener('mousemove', function(e) {
    const rect = chartCanvas.getBoundingClientRect();
    const mx = (e.clientX - rect.left) * (chartCanvas.width  / rect.width);
    const my = (e.clientY - rect.top)  * (chartCanvas.height / rect.height);
    const pad = getPad();
    const plotW = chartCanvas.width  - pad.left - pad.right;
    const plotH = chartCanvas.height - pad.top  - pad.bottom;
    const ages  = chartDisplayTokens.map(t => t.age_min || 0);
    const prices= chartDisplayTokens.map(t => t.price   || 0).filter(p => p > 0);
    const maxAge= Math.max(...ages, 60);
    const minP  = prices.length ? Math.min(...prices) : 1e-9;
    const maxP  = prices.length ? Math.max(...prices) : 1;
    const logMin= logP(minP) - 0.3, logMax = logP(maxP) + 0.3;
    function toX(a) { return pad.left + (a / maxAge) * plotW; }
    function toY(p) { return !p||p<=0 ? pad.top+plotH : pad.top+plotH-((logP(p)-logMin)/(logMax-logMin))*plotH; }
    let found = -1;
    for (let i = 0; i < chartDisplayTokens.length; i++) {
      const t  = chartDisplayTokens[i];
      const dx = mx - toX(t.age_min || 0);
      const dy = my - toY(t.price   || 0);
      if (Math.hypot(dx, dy) < 14) { found = i; break; }
    }
    if (found !== hoveredIdx) {
      hoveredIdx = found;
      chartCanvas.style.cursor = found >= 0 ? 'pointer' : 'crosshair';
      renderChart();
    }
  });

  chartCanvas.addEventListener('click', function(e) {
    if (hoveredIdx >= 0) showTooltip(e, hoveredIdx);
  });

  chartCanvas.addEventListener('mouseleave', function() {
    hoveredIdx = -1;
    renderChart();
  });
}

// ── Tooltip (click-only) ──────────────────────────────────────────────────────
function showTooltip(e, idx) {
  e.stopPropagation();
  const t = chartDisplayTokens[idx];
  if (!t) return;
  selectedToken = t;
  document.getElementById('tt-name').textContent    = t.name || 'Unknown';
  document.getElementById('tt-sym').textContent     = (t.symbol || '') + ' / SOL';
  document.getElementById('tt-price').textContent   = t.price ? '$' + t.price.toFixed(8) : '—';
  const chg = t.change || 0;
  const chgEl = document.getElementById('tt-chg');
  chgEl.textContent = (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%';
  chgEl.style.color = chg >= 0 ? '#14c784' : '#f23645';
  document.getElementById('tt-mc').textContent      = fmtNum(t.mc);
  document.getElementById('tt-liq').textContent     = fmtNum(t.liq);
  document.getElementById('tt-vol').textContent     = fmtNum(t.vol);
  document.getElementById('tt-age').textContent     = (t.age_min || 0).toFixed(1) + ' min';
  const sc = t.score || 0;
  document.getElementById('tt-score').textContent   = sc;
  document.getElementById('tt-score').style.color   = scoreColor(sc);
  document.getElementById('tt-score-bar').style.width     = sc + '%';
  document.getElementById('tt-score-bar').style.background = scoreColor(sc);
  let verdict = '';
  if (sc >= 80)      verdict = '🔥 Strong Buy Signal';
  else if (sc >= 60) verdict = '✅ Moderate Buy Signal';
  else if (sc >= 40) verdict = '⚠️ Weak Signal — Caution';
  else               verdict = '❌ Low Confidence — Skip';
  document.getElementById('tt-verdict').textContent = verdict;
  document.getElementById('tt-verdict').style.color = scoreColor(sc);
  // Position tooltip near cursor
  const tt = document.getElementById('tooltip');
  tt.classList.add('show');
  document.getElementById('tooltip-overlay').style.display = 'block';
  const x = Math.min(e.clientX + 16, window.innerWidth - 340);
  const y = Math.min(e.clientY - 20, window.innerHeight - 520);
  tt.style.left = x + 'px';
  tt.style.top  = Math.max(10, y) + 'px';
}

function closeTooltip() {
  document.getElementById('tooltip').classList.remove('show');
  document.getElementById('tooltip-overlay').style.display = 'none';
  selectedToken = null;
}

function openChart() {
  if (selectedToken) window.open('https://dexscreener.com/solana/' + selectedToken.mint, '_blank');
}

async function manualBuy() {
  if (!selectedToken) return;
  const btn = document.getElementById('tt-buy-btn');
  btn.textContent = 'Buying…';
  btn.disabled = true;
  const res = await fetch('/api/manual-buy', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({mint: selectedToken.mint, name: selectedToken.name})
  }).then(r=>r.json()).catch(()=>({ok:false,msg:'Request failed'}));
  btn.textContent = res.ok ? '✅ Order Sent!' : '❌ ' + res.msg;
  setTimeout(() => { btn.textContent = '⚡ AI Buy Now'; btn.disabled = false; }, 3000);
  if (res.ok) setTimeout(refresh, 2000);
}

// ── SSE market feed ──────────────────────────────────────────────────────────
function startFeed() {
  const es = new EventSource('/api/market-feed');
  es.onmessage = function(e) {
    try {
      const newTokens = JSON.parse(e.data);
      const mints = new Set(allTokens.map(t => t.mint));
      newTokens.forEach(t => { if (!mints.has(t.mint)) allTokens.unshift(t); });
      if (allTokens.length > 100) allTokens = allTokens.slice(0, 100);
      renderChart();
      updateTicker();
    } catch(err) {}
  };
  es.onerror = function() { setTimeout(startFeed, 5000); es.close(); };
}

// ── Ticker tape ──────────────────────────────────────────────────────────────
function updateTicker() {
  const items = allTokens.slice(0, 20).map(t => {
    const chg = t.change || 0;
    const col = chg >= 0 ? '#14c784' : '#f23645';
    return `<span class="tick-item">
      <span class="tick-name">${t.symbol||t.name||'?'}</span>
      <span style="font-family:monospace;font-size:11px">$${t.price?.toFixed(6)||'—'}</span>
      <span style="color:${col};font-weight:700">${chg>=0?'+':''}${chg.toFixed(1)}%</span>
    </span>`;
  });
  // duplicate for seamless loop
  const html = [...items, ...items].join('');
  document.getElementById('ticker-inner').innerHTML = html;
}

function selectPreset(name) {
  document.querySelectorAll('.preset-card').forEach(c => c.classList.remove('active'));
  document.getElementById('pc-' + name).classList.add('active');
  document.getElementById('s-preset').value = name;
}

function updateFilterPipe(filterLog) {
  if (!filterLog || !filterLog.length) return;
  const html = filterLog.map(f => `
    <div class="fp-item">
      <span class="${f.passed ? 'fp-pass' : 'fp-fail'}">${f.passed ? '✓' : '✗'}</span>
      <span class="fp-name">${f.name || '?'}</span>
      <span class="fp-reason">${f.reason || ''}</span>
    </div>`).join('');
  document.getElementById('filter-pipe').innerHTML = html;
}

// ── Bot state refresh ────────────────────────────────────────────────────────
function toggleStop(v) {
  document.getElementById('f-dur').style.display = v==='duration' ? 'block' : 'none';
  document.getElementById('f-pft').style.display = v==='profit'   ? 'block' : 'none';
}
async function refresh() {
  const d = await fetch('/api/state').then(r=>r.json()).catch(()=>null);
  if (!d) return;
  running = d.running;
  document.getElementById('balance').textContent   = d.balance.toFixed(4);
  document.getElementById('pos-count').textContent = d.positions.length;
  document.getElementById('wins').textContent      = d.stats.wins;
  document.getElementById('losses').textContent    = d.stats.losses;
  const pnl = d.stats.total_pnl_sol;
  const pnlEl = document.getElementById('pnl');
  pnlEl.textContent = (pnl>=0?'+':'') + pnl.toFixed(4);
  pnlEl.className   = 'sval ' + (pnl>=0 ? 'c-grn' : 'c-red');
  const dot=document.getElementById('sdot'), txt=document.getElementById('stxt'), btn=document.getElementById('toggle-btn');
  if (running) {
    dot.className='sdot sdot-on'; txt.textContent='Bot Running';
    btn.textContent='⏸ Stop Bot'; btn.className='btn btn-danger';
  } else {
    dot.className='sdot sdot-off'; txt.textContent='Bot Stopped';
    btn.textContent='▶ Start Bot'; btn.className='btn btn-success';
  }
  if (d.positions.length) {
    const rows = d.positions.map(p => {
      const cls = !p.ratio ? 'c-muted' : p.ratio>=1 ? 'c-grn' : 'c-red';
      return `<tr>
        <td style="font-weight:600">${p.name}${p.tp1_hit?'<span class="badge bg-grn" style="margin-left:4px">TP1</span>':''}</td>
        <td class="${cls}" style="font-weight:600">${p.pnl}</td>
        <td><a href="https://dexscreener.com/solana/${p.address}" target="_blank" class="badge bg-blue">Chart</a></td>
      </tr>`;
    }).join('');
    document.getElementById('pos-tbl').innerHTML =
      `<table class="tbl"><thead><tr><th>Token</th><th>P&L</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
  } else {
    document.getElementById('pos-tbl').innerHTML = '<div style="font-size:13px;color:var(--t3)">No open positions</div>';
  }
  document.getElementById('log').innerHTML = d.log.map(l => {
    const c = l.includes('BUY') ? 'lbuy' : (l.includes('SELL')||l.includes('CASHOUT')) ? 'lsell' :
              l.includes('WHALE') ? 'lsig' : l.includes('MOMENTUM') ? 'lsig' : '';
    return `<div class="lline ${c}">${l}</div>`;
  }).join('');
  if (d.filter_log) updateFilterPipe(d.filter_log);
}
async function toggleBot() {
  await fetch(running ? '/api/stop' : '/api/start', {method:'POST'});
  setTimeout(refresh, 500);
}
async function cashout() {
  if (!confirm('Sell all open positions at market price?')) return;
  await fetch('/api/cashout', {method:'POST'});
  setTimeout(refresh, 1000);
}
async function saveSettings() {
  await fetch('/api/settings', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      preset: document.getElementById('s-preset').value,
      run_mode: document.getElementById('s-mode').value,
      run_duration_min: document.getElementById('s-dur')?.value || 0,
      profit_target_sol: document.getElementById('s-pft')?.value || 0,
    })
  });
  setTimeout(refresh, 300);
}
refresh();
setInterval(refresh, 5000);
initChart();
startFeed();
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
const PRESET_DEFAULTS = {
  safe:     {buy:0.02,tp1:1.3,tp2:2.0,sl:0.85,trail:0.15,age:20,tstop:20,liq:25000,minmc:10000,maxmc:80000,prio:10000,dd:0.3,maxpos:2},
  balanced: {buy:0.04,tp1:1.5,tp2:3.0,sl:0.75,trail:0.20,age:30,tstop:30,liq:10000,minmc:5000,maxmc:150000,prio:30000,dd:0.5,maxpos:3},
  degen:    {buy:0.10,tp1:2.0,tp2:10.0,sl:0.60,trail:0.30,age:10,tstop:60,liq:5000,minmc:2000,maxmc:250000,prio:100000,dd:1.0,maxpos:5},
};
let aiSuggestion = null;

function loadPreset(name) {
  const p = PRESET_DEFAULTS[name];
  if (!p) return;
  document.getElementById('s-max-buy').value = p.buy;
  document.getElementById('s-tp1').value     = p.tp1;
  document.getElementById('s-tp2').value     = p.tp2;
  document.getElementById('s-sl').value      = p.sl;
  document.getElementById('s-trail').value   = p.trail;
  document.getElementById('s-age').value     = p.age;
  document.getElementById('s-tstop').value   = p.tstop;
  document.getElementById('s-liq').value     = p.liq;
  document.getElementById('s-minmc').value   = p.minmc;
  document.getElementById('s-maxmc').value   = p.maxmc;
  document.getElementById('s-prio').value    = p.prio;
  document.getElementById('s-dd').value      = p.dd;
  document.getElementById('s-maxpos').value  = p.maxpos;
}

async function savePreset() {
  const preset = document.getElementById('edit-preset').value;
  const settings = {
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
  };
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

function applyAISuggestion() {
  if (!aiSuggestion) return;
  document.getElementById('edit-preset').value = aiSuggestion.preset;
  loadPreset(aiSuggestion.preset);
  alert('✅ AI-recommended settings loaded. Review and click Save to apply.');
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
    print(f"\n  SolTrader Platform → http://localhost:5000")
    print(f"  Admin account: {ADMIN_EMAIL}")
    print(f"  Database: {DATABASE_URL}\n")
    port = int(os.getenv("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
