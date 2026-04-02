import requests
import time
import os
import base64
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
import base58


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
HELIUS_RPC = require_env("HELIUS_RPC")

MAX_BUY_SOL = 0.05   # ~$5-10 per trade
TAKE_PROFIT = 2.0    # sell at 2x
STOP_LOSS   = 0.70   # sell at -30%

keypair = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY))
wallet = str(keypair.pubkey())
print(f"Wallet: {wallet}")
print(f"TP: {TAKE_PROFIT}x | SL: {int((1-STOP_LOSS)*100)}% loss | Max buy: {MAX_BUY_SOL} SOL")

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

seen_tokens = set()
positions = {}  # {address: {name, entry_price, timestamp}}
token_last_checked = {}

MIN_MC = 5000
MAX_MC = 250000
MIN_VOLUME_24H = 10000
MIN_LIQUIDITY = 3000
RETRY_SECONDS = 300


def to_float(value, default=0.0):
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return float(default)


def get_sol_balance():
    resp = requests.post(HELIUS_RPC, json={
        "jsonrpc": "2.0", "id": 1,
        "method": "getBalance",
        "params": [wallet]
    }, timeout=8)
    return resp.json().get("result", {}).get("value", 0) / 1e9


def sign_and_send(swap_transaction):
    raw = base64.b64decode(swap_transaction)
    tx = VersionedTransaction.from_bytes(raw)
    signed = VersionedTransaction(tx.message, [keypair])
    encoded = base64.b64encode(bytes(signed)).decode()

    resp = requests.post(HELIUS_RPC, json={
        "jsonrpc": "2.0", "id": 1,
        "method": "sendTransaction",
        "params": [encoded, {
            "encoding": "base64",
            "skipPreflight": False,
            "preflightCommitment": "confirmed",
            "maxRetries": 3
        }]
    }, timeout=15)
    result = resp.json()
    if "result" in result:
        return result["result"]  # tx signature
    print(f"   RPC error: {result.get('error')}")
    return None


def get_token_price(address):
    try:
        resp = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{address}",
            headers=headers, timeout=5
        )
        pairs = resp.json().get("pairs")
        if pairs:
            return float(pairs[0].get("priceUsd") or 0)
    except:
        pass
    return None


def get_token_info(address):
    try:
        resp = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{address}",
            headers=headers, timeout=5
        )
        payload = resp.json()
        pairs = payload.get("pairs") if isinstance(payload, dict) else []
        if pairs:
            # Prefer Solana pairs and then pick the deepest liquidity pool.
            sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
            ranked_pairs = sol_pairs or pairs
            p = max(ranked_pairs, key=lambda pair: to_float((pair.get("liquidity") or {}).get("usd"), 0))
            return {
                "name":        p.get("baseToken", {}).get("name", "Unknown"),
                "symbol":      p.get("baseToken", {}).get("symbol", "?"),
                "price":       to_float(p.get("priceUsd"), 0),
                "marketcap":   to_float(p.get("marketCap"), 0),
                "volume":      to_float((p.get("volume") or {}).get("h24"), 0),
                "liquidity":   to_float((p.get("liquidity") or {}).get("usd"), 0),
                "priceChange": to_float((p.get("priceChange") or {}).get("h24"), 0),
            }
    except Exception:
        pass
    return None


def fetch_latest_profile_tokens():
    try:
        response = requests.get(
            "https://api.dexscreener.com/token-profiles/latest/v1",
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            print(f"   ⚠️  Unexpected scanner payload type: {type(payload).__name__}")
            return []
        return payload
    except Exception as e:
        print(f"   ⚠️  Scanner request failed: {e}")
        return []


def get_token_balance(address):
    """Return actual token amount (raw integer) held in wallet."""
    resp = requests.post(HELIUS_RPC, json={
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [wallet, {"mint": address}, {"encoding": "jsonParsed"}]
    }, timeout=8)
    accounts = resp.json().get("result", {}).get("value", [])
    if not accounts:
        return 0
    return int(
        accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]
    )


def buy_token(address, name, entry_price):
    balance = get_sol_balance()
    if balance < MAX_BUY_SOL + 0.01:
        print(f"   ⚠️  Low balance: {balance:.4f} SOL — skipping")
        return False

    try:
        quote = requests.get(
            f"https://lite-api.jup.ag/swap/v1/quote"
            f"?inputMint=So11111111111111111111111111111111111111112"
            f"&outputMint={address}"
            f"&amount={int(MAX_BUY_SOL * 1e9)}"
            f"&slippageBps=1000"
        , timeout=10).json()

        if "error" in quote:
            print(f"   ❌ No route: {quote['error']}")
            return False

        swap_resp = requests.post("https://lite-api.jup.ag/swap/v1/swap", json={
            "quoteResponse": quote,
            "userPublicKey": wallet,
            "wrapAndUnwrapSol": True,
        }, timeout=10).json()

        swap_tx = swap_resp.get("swapTransaction")
        if not swap_tx:
            print(f"   ❌ No swap transaction returned")
            return False

        sig = sign_and_send(swap_tx)
        if sig:
            positions[address] = {
                "name":        name,
                "entry_price": entry_price,
                "timestamp":   time.time(),
            }
            print(f"   ✅ Bought {name} @ ${entry_price}")
            print(f"   🔗 https://solscan.io/tx/{sig}")
            return True
        return False

    except Exception as e:
        print(f"   ❌ Buy error: {e}")
        return False


def sell_token(address, reason):
    pos = positions.get(address)
    if not pos:
        return

    try:
        token_amount = get_token_balance(address)
        if token_amount == 0:
            print(f"   ⚠️  No balance found for {pos['name']} — removing position")
            del positions[address]
            return

        quote = requests.get(
            f"https://lite-api.jup.ag/swap/v1/quote"
            f"?inputMint={address}"
            f"&outputMint=So11111111111111111111111111111111111111112"
            f"&amount={token_amount}"
            f"&slippageBps=1000"
        , timeout=10).json()

        if "error" in quote:
            print(f"   ❌ No sell route for {pos['name']}: {quote['error']}")
            return

        swap_resp = requests.post("https://lite-api.jup.ag/swap/v1/swap", json={
            "quoteResponse": quote,
            "userPublicKey": wallet,
            "wrapAndUnwrapSol": True,
        }, timeout=10).json()

        swap_tx = swap_resp.get("swapTransaction")
        if not swap_tx:
            print(f"   ❌ No sell transaction for {pos['name']}")
            return

        sig = sign_and_send(swap_tx)
        if sig:
            current_price = get_token_price(address)
            if current_price and pos["entry_price"]:
                pnl = (current_price / pos["entry_price"] - 1) * 100
                print(f"   ✅ Sold {pos['name']} — {reason} | PnL: {pnl:+.1f}%")
            else:
                print(f"   ✅ Sold {pos['name']} — {reason}")
            print(f"   🔗 https://solscan.io/tx/{sig}")
            del positions[address]

    except Exception as e:
        print(f"   ❌ Sell error for {pos['name']}: {e}")


def check_positions():
    for address in list(positions.keys()):
        pos = positions[address]
        current_price = get_token_price(address)
        if not current_price or not pos["entry_price"]:
            continue

        ratio = current_price / pos["entry_price"]
        age_min = (time.time() - pos["timestamp"]) / 60

        if ratio >= TAKE_PROFIT:
            print(f"🎯 TAKE PROFIT: {pos['name']} ({ratio:.2f}x) after {age_min:.0f}m")
            sell_token(address, f"TP {ratio:.2f}x")
        elif ratio <= STOP_LOSS:
            print(f"🛑 STOP LOSS: {pos['name']} ({ratio:.2f}x) after {age_min:.0f}m")
            sell_token(address, f"SL {ratio:.2f}x")
        else:
            pnl = (ratio - 1) * 100
            print(f"   📊 {pos['name']}: {ratio:.2f}x ({pnl:+.1f}%) | held {age_min:.0f}m")


# ── Main loop ──────────────────────────────────────────────────────────────────
print("Bot running...\n")

while True:
    try:
        if positions:
            check_positions()

        tokens = fetch_latest_profile_tokens()
        now = time.time()
        scanned_total = 0
        scanned_solana = 0
        checked_candidates = 0
        throttled = 0
        info_ready = 0
        filtered_out = 0
        pass_filters = 0
        trade_failed = 0

        for token in (tokens if isinstance(tokens, list) else []):
            if not isinstance(token, dict):
                continue
            scanned_total += 1
            address = token.get("tokenAddress")
            chain   = token.get("chainId")
            if not address:
                continue

            address_key = str(address).lower()

            if chain == "solana":
                scanned_solana += 1

            if chain == "solana" and address_key not in seen_tokens:
                if address_key in token_last_checked and (now - token_last_checked[address_key]) < RETRY_SECONDS:
                    throttled += 1
                    continue

                token_last_checked[address_key] = now
                checked_candidates += 1
                info = get_token_info(address)

                if info:
                    info_ready += 1
                    mc     = info["marketcap"]
                    vol    = info["volume"]
                    liq    = info["liquidity"]
                    change = info["priceChange"]

                    if MIN_MC <= mc <= MAX_MC and vol >= MIN_VOLUME_24H and liq >= MIN_LIQUIDITY:
                        pass_filters += 1
                        print(f"🚨 {info['name']} ({info['symbol']})")
                        print(f"   MC: ${mc:,.0f} | Vol: ${vol:,.0f} | Liq: ${liq:,.0f} | {change:+.1f}%")
                        print(f"   https://dexscreener.com/solana/{address}")
                        bought = buy_token(address, info["name"], info["price"])
                        if bought:
                            # Mark as seen only after a successful buy so filtered tokens can be retried later.
                            seen_tokens.add(address_key)
                        else:
                            trade_failed += 1
                        print("---")
                    else:
                        filtered_out += 1

        print(
            "🔎 Scan "
            f"total={scanned_total} "
            f"sol={scanned_solana} "
            f"checked={checked_candidates} "
            f"throttled={throttled} "
            f"with_info={info_ready} "
            f"filtered={filtered_out} "
            f"buy_candidates={pass_filters} "
            f"trade_failed={trade_failed} "
            f"positions={len(positions)}"
        )

        time.sleep(10)

    except Exception as e:
        print(f"Error: {e}")
        time.sleep(10)
