"""
Copy Engine — SolTrader Copy Bot
Polls Helius for new transactions from copy-list wallets.
Mirrors swaps proportionally. Supports SHADOW / LIVE / OFF modes.
"""

import os
import time
import threading
import requests
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Callable
from dotenv import load_dotenv

load_dotenv()

HELIUS_RPC         = os.getenv("HELIUS_RPC", "")
COPY_MAX_TRADE_PCT = float(os.getenv("COPY_MAX_TRADE_PCT", "0.10"))
POLL_INTERVAL      = 15   # seconds between polls
HEADERS            = {"User-Agent": "Mozilla/5.0"}


class CopyMode(Enum):
    SHADOW = "shadow"
    LIVE   = "live"
    OFF    = "off"


@dataclass
class SwapInfo:
    source_wallet: str
    token_mint: str
    action: str          # "BUY" or "SELL"
    estimated_sol: float
    signature: str


@dataclass
class CopyTrade:
    source_wallet: str
    token_mint: str
    token_name: str
    action: str
    size_sol: float
    entry_price: float
    shadow: bool
    timestamp: float = field(default_factory=time.time)
    current_price: float = 0.0
    pnl_sol: float = 0.0


# ── State ─────────────────────────────────────────────────────────────────────

_mode: CopyMode = CopyMode(os.getenv("COPY_MODE", "shadow"))
_mode_lock = threading.Lock()
_copy_trades: List[CopyTrade] = []
_trades_lock = threading.Lock()
_seen_sigs: set = set()
_stop_event = threading.Event()
_engine_thread: Optional[threading.Thread] = None

_get_our_balance: Optional[Callable[[], float]] = None
_execute_swap: Optional[Callable[[str, float], Optional[str]]] = None
_get_price: Optional[Callable[[str], Optional[float]]] = None


def set_mode(mode: CopyMode):
    global _mode
    with _mode_lock:
        _mode = mode


def get_mode() -> CopyMode:
    with _mode_lock:
        return _mode


def get_trades() -> List[dict]:
    with _trades_lock:
        result = []
        for t in reversed(_copy_trades[-100:]):
            result.append({
                "source_wallet": t.source_wallet[:8] + "…",
                "token_mint":    t.token_mint,
                "token_name":    t.token_name,
                "action":        t.action,
                "size_sol":      round(t.size_sol, 4),
                "entry_price":   t.entry_price,
                "shadow":        t.shadow,
                "timestamp":     t.timestamp,
                "pnl_sol":       round(t.pnl_sol, 4),
            })
        return result


def parse_swap_from_tx(tx: dict, wallet_address: str) -> Optional[SwapInfo]:
    """
    Detect a swap in a parsed Helius transaction.
    Returns SwapInfo if a token buy/sell is found for wallet_address, else None.
    """
    meta = tx.get("meta", {})
    if meta.get("err"):
        return None

    pre_balances  = meta.get("preTokenBalances",  [])
    post_balances = meta.get("postTokenBalances", [])

    pre_map  = {b["accountIndex"]: (b["mint"], float(b.get("uiTokenAmount", {}).get("uiAmount") or 0))
                for b in pre_balances}
    post_map = {b["accountIndex"]: (b["mint"], float(b.get("uiTokenAmount", {}).get("uiAmount") or 0))
                for b in post_balances}

    account_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    owned_indices = [i for i, k in enumerate(account_keys)
                     if (k.get("pubkey") if isinstance(k, dict) else k) == wallet_address]

    if not owned_indices:
        return None

    for idx in owned_indices:
        pre_entry  = pre_map.get(idx)
        post_entry = post_map.get(idx)
        if not post_entry:
            continue
        mint = post_entry[0]
        pre_amt  = pre_entry[1] if pre_entry else 0.0
        post_amt = post_entry[1]
        delta = post_amt - pre_amt
        if delta == 0:
            continue

        action = "BUY" if delta > 0 else "SELL"
        sig = (tx.get("transaction", {}).get("signatures", [""])[0] or "unknown")

        pre_sol_lamps  = meta.get("preBalances",  [0] * (idx + 2))
        post_sol_lamps = meta.get("postBalances", [0] * (idx + 2))
        sol_delta = abs(pre_sol_lamps[idx] - post_sol_lamps[idx]) / 1e9 if idx < len(pre_sol_lamps) else 0.05

        return SwapInfo(
            source_wallet=wallet_address,
            token_mint=mint,
            action=action,
            estimated_sol=max(sol_delta, 0.001),
            signature=sig,
        )
    return None


def calc_proportional_size(
    their_trade_sol: float,
    their_balance_sol: float,
    our_balance_sol: float,
    max_pct: float = COPY_MAX_TRADE_PCT,
) -> float:
    """Mirror their position size proportionally, capped at max_pct of our wallet."""
    if their_balance_sol <= 0 or our_balance_sol <= 0:
        return 0.0
    proportion = their_trade_sol / their_balance_sol
    raw_size = proportion * our_balance_sol
    cap = our_balance_sol * max_pct
    return round(min(raw_size, cap), 4)


def _get_their_sol_balance(address: str) -> float:
    try:
        resp = requests.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getBalance",
            "params": [address]
        }, timeout=8)
        return resp.json().get("result", {}).get("value", 0) / 1e9
    except Exception:
        return 1.0


def _get_latest_sigs(address: str) -> List[dict]:
    try:
        resp = requests.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": [address, {"limit": 5}]
        }, timeout=8)
        return resp.json().get("result", [])
    except Exception:
        return []


def _fetch_tx(sig: str) -> Optional[dict]:
    try:
        resp = requests.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getTransaction",
            "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
        }, timeout=10)
        return resp.json().get("result")
    except Exception:
        return None


def _process_wallet(address: str):
    mode = get_mode()
    if mode == CopyMode.OFF:
        return

    sigs = _get_latest_sigs(address)
    for sig_info in sigs:
        sig = sig_info.get("signature")
        if not sig or sig in _seen_sigs:
            continue
        _seen_sigs.add(sig)

        tx = _fetch_tx(sig)
        if not tx:
            continue

        swap = parse_swap_from_tx(tx, address)
        if not swap or swap.action != "BUY":
            continue

        our_balance   = _get_our_balance() if _get_our_balance else 0.5
        their_balance = _get_their_sol_balance(address)
        size_sol = calc_proportional_size(
            their_trade_sol=swap.estimated_sol,
            their_balance_sol=their_balance,
            our_balance_sol=our_balance,
        )
        if size_sol < 0.001:
            continue

        price = (_get_price(swap.token_mint) if _get_price else None) or 0.0
        is_shadow = (mode == CopyMode.SHADOW)

        if not is_shadow and _execute_swap:
            _execute_swap(swap.token_mint, size_sol)

        trade = CopyTrade(
            source_wallet=address,
            token_mint=swap.token_mint,
            token_name=swap.token_mint[:8],
            action=swap.action,
            size_sol=size_sol,
            entry_price=price,
            shadow=is_shadow,
        )
        with _trades_lock:
            _copy_trades.append(trade)
            if len(_copy_trades) > 500:
                _copy_trades.pop(0)


def _engine_loop(get_addresses_fn: Callable[[], List[str]]):
    while not _stop_event.is_set():
        addresses = get_addresses_fn()
        for addr in addresses:
            if _stop_event.is_set():
                break
            _process_wallet(addr)
        _stop_event.wait(POLL_INTERVAL)


def start_engine(
    get_addresses_fn: Callable[[], List[str]],
    get_balance_fn: Callable[[], float],
    execute_swap_fn: Callable[[str, float], Optional[str]],
    get_price_fn: Callable[[str], Optional[float]],
):
    global _engine_thread, _get_our_balance, _execute_swap, _get_price
    _get_our_balance = get_balance_fn
    _execute_swap    = execute_swap_fn
    _get_price       = get_price_fn
    _stop_event.clear()
    _engine_thread = threading.Thread(
        target=_engine_loop, args=(get_addresses_fn,), daemon=True, name="copy-engine"
    )
    _engine_thread.start()


def stop_engine():
    _stop_event.set()
