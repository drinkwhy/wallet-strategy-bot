# Wallet Copy Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a wallet copy bot that scans Solana for top-performing wallets by composite score (win rate + ROI + consistency + recency) and mirrors their trades proportionally with a shadow/live/off toggle.

**Architecture:** `wallet_scanner.py` scores wallets every 4h and maintains a ranked copy list. `copy_engine.py` polls Helius for new transactions from copy-list wallets and mirrors them proportionally. `dashboard.py` gets new routes and an HTML tab for the leaderboard, toggle, and trade log.

**Tech Stack:** Python, Flask, Helius RPC (getSignaturesForAddress + getTransaction), DexScreener API, existing `sign_and_send()` in `dashboard.py`, `SMART_MONEY_WALLETS` from `whale_detection.py`.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `wallet_scanner.py` | **CREATE** | Score wallets, maintain ranked copy list, refresh every 4h |
| `copy_engine.py` | **CREATE** | Poll Helius for top-wallet txs, execute or shadow-copy trades |
| `tests/test_wallet_scanner.py` | **CREATE** | Unit tests for scoring logic |
| `tests/test_copy_engine.py` | **CREATE** | Unit tests for copy engine parse/sizing logic |
| `dashboard.py` | **MODIFY** | Import copy engine, add 4 new routes, add Copy Bot HTML tab |
| `.env.example` | **MODIFY** | Add COPY_MODE, COPY_MAX_TRADE_PCT, COPY_TOP_N |

---

## Task 1: wallet_scanner.py — data structures and scoring

**Files:**
- Create: `wallet_scanner.py`
- Test: `tests/test_wallet_scanner.py`

- [ ] **Step 1: Write failing tests for score_wallet()**

```python
# tests/test_wallet_scanner.py
import pytest
from wallet_scanner import score_wallet, rank_wallets, WalletStats

def make_stats(wins, losses, avg_roi, roi_std, recency_weight):
    return WalletStats(
        address="ADDR1",
        wins=wins,
        losses=losses,
        avg_roi=avg_roi,
        roi_std=roi_std,
        recency_weight=recency_weight,
    )

def test_score_wallet_perfect():
    stats = make_stats(wins=90, losses=10, avg_roi=0.50, roi_std=0.05, recency_weight=1.0)
    score = score_wallet(stats)
    assert 0.0 <= score <= 1.0
    assert score > 0.7

def test_score_wallet_terrible():
    stats = make_stats(wins=10, losses=90, avg_roi=-0.20, roi_std=0.80, recency_weight=0.2)
    score = score_wallet(stats)
    assert score < 0.3

def test_score_wallet_zero_trades():
    stats = make_stats(wins=0, losses=0, avg_roi=0.0, roi_std=0.0, recency_weight=0.0)
    score = score_wallet(stats)
    assert score == 0.0

def test_rank_wallets_returns_top_pct():
    stats_list = [
        make_stats(90, 10, 0.5, 0.05, 1.0),   # high score
        make_stats(50, 50, 0.1, 0.3, 0.5),    # mid score
        make_stats(10, 90, -0.2, 0.8, 0.2),   # low score
        make_stats(80, 20, 0.4, 0.1, 0.9),    # high score
        make_stats(20, 80, -0.1, 0.6, 0.3),   # low score
    ]
    top = rank_wallets(stats_list, top_pct=0.4)
    assert len(top) == 2
    assert top[0].wins >= top[1].wins  # sorted descending
```

- [ ] **Step 2: Run tests to confirm they fail**

```
cd C:/Users/dyllan/wallet-strategy-bot
python -m pytest tests/test_wallet_scanner.py -v
```
Expected: ImportError or ModuleNotFoundError — `wallet_scanner` does not exist yet.

- [ ] **Step 3: Create wallet_scanner.py with WalletStats, score_wallet(), rank_wallets()**

```python
# wallet_scanner.py
"""
Wallet Scanner — SolTrader Copy Bot
Scores wallets by composite metric and maintains a ranked copy list.
Refreshes every 4 hours via background thread.
"""

import math
import time
import threading
import requests
from dataclasses import dataclass, field
from typing import List, Optional
import os
from dotenv import load_dotenv

load_dotenv()

HELIUS_RPC = os.getenv("HELIUS_RPC", "")
HEADERS = {"User-Agent": "Mozilla/5.0"}

# Score weights — must sum to 1.0
WEIGHT_WIN_RATE    = 0.35
WEIGHT_AVG_ROI     = 0.35
WEIGHT_CONSISTENCY = 0.20
WEIGHT_RECENCY     = 0.10

RESCORE_INTERVAL   = 4 * 60 * 60   # 4 hours in seconds
HISTORY_TX_LIMIT   = 100            # transactions to analyse per wallet
TOP_PCT            = float(os.getenv("COPY_TOP_PCT", "0.10"))  # top 10% by default
COPY_TOP_N         = int(os.getenv("COPY_TOP_N", "5"))


@dataclass
class WalletStats:
    address: str
    wins: int
    losses: int
    avg_roi: float        # e.g. 0.35 = 35% avg return
    roi_std: float        # standard deviation of per-trade ROI
    recency_weight: float # 0.0–1.0: how active recently
    score: float = 0.0


@dataclass
class CopyListEntry:
    address: str
    score: float
    win_rate: float
    avg_roi: float
    last_scored: float = field(default_factory=time.time)


# Thread-safe copy list
_copy_list: List[CopyListEntry] = []
_copy_list_lock = threading.Lock()
_scanner_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def score_wallet(stats: WalletStats) -> float:
    """Return composite score in [0, 1]."""
    total = stats.wins + stats.losses
    if total == 0:
        return 0.0

    win_rate = stats.wins / total  # 0–1

    # Normalise avg_roi: cap at 200% return, floor at -100%
    roi_norm = max(0.0, min(1.0, (stats.avg_roi + 1.0) / 3.0))

    # Consistency: lower std dev = better. Cap std at 1.0.
    consistency = max(0.0, 1.0 - min(stats.roi_std, 1.0))

    recency = max(0.0, min(1.0, stats.recency_weight))

    composite = (
        WEIGHT_WIN_RATE    * win_rate    +
        WEIGHT_AVG_ROI     * roi_norm    +
        WEIGHT_CONSISTENCY * consistency +
        WEIGHT_RECENCY     * recency
    )
    return round(composite, 4)


def rank_wallets(stats_list: List[WalletStats], top_pct: float = TOP_PCT) -> List[WalletStats]:
    """Score all wallets and return the top_pct fraction, sorted descending."""
    for s in stats_list:
        s.score = score_wallet(s)
    ranked = sorted(stats_list, key=lambda s: s.score, reverse=True)
    cutoff = max(1, math.ceil(len(ranked) * top_pct))
    return ranked[:cutoff]


def _fetch_wallet_trades(address: str) -> WalletStats:
    """
    Pull last HISTORY_TX_LIMIT signatures for address via Helius,
    fetch each swap tx, compute win/loss/roi from DexScreener price snapshots.
    Returns WalletStats. Falls back to zero-trade stats on any error.
    """
    zero = WalletStats(address=address, wins=0, losses=0,
                       avg_roi=0.0, roi_std=0.0, recency_weight=0.0)
    try:
        # Step 1: get signatures
        resp = requests.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": [address, {"limit": HISTORY_TX_LIMIT}]
        }, timeout=10)
        sigs = resp.json().get("result", [])
        if not sigs:
            return zero

        # Step 2: compute recency weight — fraction of trades in last 7 days
        now = time.time()
        recent = sum(1 for s in sigs if s.get("blockTime", 0) > now - 7 * 86400)
        recency_weight = recent / len(sigs)

        # Step 3: fetch each tx and detect swap (look for token balance changes)
        wins, losses, rois = 0, 0, []
        for sig_info in sigs[:HISTORY_TX_LIMIT]:
            sig = sig_info.get("signature")
            if not sig:
                continue
            try:
                tx_resp = requests.post(HELIUS_RPC, json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTransaction",
                    "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
                }, timeout=8)
                tx = tx_resp.json().get("result")
                if not tx:
                    continue
                roi = _extract_roi_from_tx(tx, address)
                if roi is None:
                    continue
                if roi > 0:
                    wins += 1
                else:
                    losses += 1
                rois.append(roi)
            except Exception:
                continue

        if not rois:
            return zero

        avg_roi = sum(rois) / len(rois)
        mean = avg_roi
        variance = sum((r - mean) ** 2 for r in rois) / len(rois)
        roi_std = math.sqrt(variance)

        return WalletStats(
            address=address,
            wins=wins,
            losses=losses,
            avg_roi=avg_roi,
            roi_std=roi_std,
            recency_weight=recency_weight,
        )
    except Exception:
        return zero


def _extract_roi_from_tx(tx: dict, address: str) -> Optional[float]:
    """
    Extract approximate ROI from a parsed transaction.
    Looks at pre/post token balances for the given address.
    Returns positive float for profit, negative for loss, None if not a swap.
    """
    meta = tx.get("meta", {})
    pre_balances  = meta.get("preTokenBalances", [])
    post_balances = meta.get("postTokenBalances", [])

    # Map: account_index -> {mint, amount}
    pre_map  = {b["accountIndex"]: float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
                for b in pre_balances}
    post_map = {b["accountIndex"]: float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
                for b in post_balances}

    # Find accounts owned by address
    account_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    owned_indices = [i for i, k in enumerate(account_keys)
                     if (k.get("pubkey") if isinstance(k, dict) else k) == address]

    if not owned_indices:
        return None

    # Sum token changes
    total_pre  = sum(pre_map.get(i, 0) for i in owned_indices)
    total_post = sum(post_map.get(i, 0) for i in owned_indices)

    if total_pre == 0:
        return None

    return (total_post - total_pre) / total_pre


def _rescore_loop(seed_wallets: List[str]):
    """Background thread: score seed wallets, update copy list every 4h."""
    while not _stop_event.is_set():
        stats_list = []
        for addr in seed_wallets:
            if _stop_event.is_set():
                break
            stats = _fetch_wallet_trades(addr)
            stats_list.append(stats)

        top = rank_wallets(stats_list)
        entries = [
            CopyListEntry(
                address=s.address,
                score=s.score,
                win_rate=s.wins / max(1, s.wins + s.losses),
                avg_roi=s.avg_roi,
            )
            for s in top
        ]
        with _copy_list_lock:
            _copy_list.clear()
            _copy_list.extend(entries[:COPY_TOP_N])

        _stop_event.wait(RESCORE_INTERVAL)


def start_scanner(seed_wallets: List[str]):
    """Start the background scoring thread."""
    global _scanner_thread
    _stop_event.clear()
    _scanner_thread = threading.Thread(
        target=_rescore_loop, args=(seed_wallets,), daemon=True, name="wallet-scanner"
    )
    _scanner_thread.start()


def stop_scanner():
    _stop_event.set()


def get_copy_list() -> List[CopyListEntry]:
    with _copy_list_lock:
        return list(_copy_list)


def get_copy_addresses() -> List[str]:
    with _copy_list_lock:
        return [e.address for e in _copy_list]
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/test_wallet_scanner.py -v
```
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd C:/Users/dyllan/wallet-strategy-bot
git add wallet_scanner.py tests/test_wallet_scanner.py
git commit -m "feat: add wallet_scanner with composite scoring and copy list"
```

---

## Task 2: copy_engine.py — trade copying with shadow/live/off

**Files:**
- Create: `copy_engine.py`
- Test: `tests/test_copy_engine.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_copy_engine.py
import pytest
from copy_engine import (
    parse_swap_from_tx,
    calc_proportional_size,
    CopyMode,
    SwapInfo,
)

def test_parse_swap_returns_none_on_empty_tx():
    result = parse_swap_from_tx({}, "ADDR1")
    assert result is None

def test_parse_swap_detects_token_change():
    tx = {
        "meta": {
            "preTokenBalances":  [{"accountIndex": 0, "mint": "TOKEN1", "uiTokenAmount": {"uiAmount": "0"}}],
            "postTokenBalances": [{"accountIndex": 0, "mint": "TOKEN1", "uiTokenAmount": {"uiAmount": "100"}}],
        },
        "transaction": {
            "message": {
                "accountKeys": [{"pubkey": "ADDR1"}]
            }
        }
    }
    result = parse_swap_from_tx(tx, "ADDR1")
    assert result is not None
    assert result.token_mint == "TOKEN1"
    assert result.action == "BUY"

def test_calc_proportional_size_basic():
    # They used 5% of their wallet, we have 2 SOL → expect 0.1 SOL
    size = calc_proportional_size(
        their_trade_sol=0.5,
        their_balance_sol=10.0,
        our_balance_sol=2.0,
        max_pct=0.10,
    )
    assert abs(size - 0.1) < 0.001

def test_calc_proportional_size_caps_at_max_pct():
    # They used 50% — we cap at 10%
    size = calc_proportional_size(
        their_trade_sol=5.0,
        their_balance_sol=10.0,
        our_balance_sol=2.0,
        max_pct=0.10,
    )
    assert size == pytest.approx(0.2, abs=0.001)

def test_copy_mode_enum():
    assert CopyMode.SHADOW.value == "shadow"
    assert CopyMode.LIVE.value   == "live"
    assert CopyMode.OFF.value    == "off"
```

- [ ] **Step 2: Run tests to confirm they fail**

```
python -m pytest tests/test_copy_engine.py -v
```
Expected: ImportError — `copy_engine` does not exist yet.

- [ ] **Step 3: Create copy_engine.py**

```python
# copy_engine.py
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

HELIUS_RPC        = os.getenv("HELIUS_RPC", "")
COPY_MAX_TRADE_PCT = float(os.getenv("COPY_MAX_TRADE_PCT", "0.10"))  # max 10% of wallet per trade
POLL_INTERVAL     = 15   # seconds between polls
HEADERS           = {"User-Agent": "Mozilla/5.0"}


class CopyMode(Enum):
    SHADOW = "shadow"
    LIVE   = "live"
    OFF    = "off"


@dataclass
class SwapInfo:
    source_wallet: str
    token_mint: str
    action: str          # "BUY" or "SELL"
    estimated_sol: float # approx SOL value
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

# Injected at startup by dashboard.py
_get_our_balance: Optional[Callable[[], float]] = None
_execute_swap: Optional[Callable[[str, float], Optional[str]]] = None
_get_price: Optional[Callable[[str], Optional[float]]] = None
_get_copy_addresses: Optional[Callable[[], List[str]]] = None


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
        sig = (tx.get("transaction", {}).get("signatures", [""])[0] or
               tx.get("transaction", {}).get("message", {}).get("recentBlockhash", "unknown"))

        # Rough SOL estimate from account balance change
        pre_sol_lamps  = meta.get("preBalances", [0] * (idx + 1))
        post_sol_lamps = meta.get("postBalances", [0] * (idx + 1))
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
        return 1.0   # fallback: assume 1 SOL so proportion is 1:1


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
    """Check a single wallet for new swaps and copy if warranted."""
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
            continue  # only copy buys for now

        our_balance  = _get_our_balance() if _get_our_balance else 0.5
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
    """Start the copy engine background thread."""
    global _engine_thread, _get_our_balance, _execute_swap, _get_price, _get_copy_addresses
    _get_our_balance  = get_balance_fn
    _execute_swap     = execute_swap_fn
    _get_price        = get_price_fn
    _get_copy_addresses = get_addresses_fn
    _stop_event.clear()
    _engine_thread = threading.Thread(
        target=_engine_loop, args=(get_addresses_fn,), daemon=True, name="copy-engine"
    )
    _engine_thread.start()


def stop_engine():
    _stop_event.set()
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/test_copy_engine.py -v
```
Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add copy_engine.py tests/test_copy_engine.py
git commit -m "feat: add copy_engine with shadow/live/off modes and proportional sizing"
```

---

## Task 3: dashboard.py — wire in copy bot routes

**Files:**
- Modify: `dashboard.py`

- [ ] **Step 1: Add imports at top of dashboard.py**

Find the block of imports near line 1 (after `from dotenv import load_dotenv`) and add:

```python
from wallet_scanner import start_scanner, get_copy_list, get_copy_addresses, stop_scanner
from copy_engine import (
    start_engine, stop_engine, get_mode, set_mode, get_trades, CopyMode,
)
```

- [ ] **Step 2: Wire up scanner + engine startup after bot thread starts**

Find the section near the bottom of `dashboard.py` where `bot_loop` thread is started (around line 1140). Add after it:

```python
# ── Copy Bot startup ──────────────────────────────────────────────────────────
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
```

- [ ] **Step 3: Add 4 new Flask routes**

Add these routes after the existing `/trade_detail` route:

```python
@app.route("/copy/state")
def copy_state():
    """Return copy bot mode, copy list leaderboard, and recent copy trades."""
    copy_list = get_copy_list()
    return jsonify({
        "mode": get_mode().value,
        "leaderboard": [
            {
                "address":  e.address[:8] + "…" + e.address[-4:],
                "full_address": e.address,
                "score":    round(e.score, 3),
                "win_rate": round(e.win_rate * 100, 1),
                "avg_roi":  round(e.avg_roi * 100, 1),
            }
            for e in copy_list
        ],
        "trades": get_trades(),
    })


@app.route("/copy/mode", methods=["POST"])
def copy_set_mode():
    """Set copy engine mode: shadow | live | off"""
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
    """Manually add a wallet address to the seed list for scanning."""
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
    """Remove a wallet address from the seed list."""
    data = request.json or {}
    addr = (data.get("address") or "").strip()
    from whale_detection import SMART_MONEY_WALLETS
    SMART_MONEY_WALLETS.discard(addr)
    log(f"Copy bot: removed wallet {addr[:8]}… from seed list")
    return jsonify({"ok": True})
```

- [ ] **Step 4: Add Copy Bot HTML tab to the dashboard**

Find the line in `dashboard.py` that contains `<!-- ── Log ── -->` and insert the copy bot panel directly before it:

```html
<!-- ── Copy Bot ── -->
<div class="card" style="margin-bottom:12px" id="copy-bot-card">
  <h2>Copy Bot</h2>

  <!-- Mode toggle -->
  <div style="display:flex;gap:8px;margin-bottom:14px;align-items:center">
    <span style="font-size:11px;color:#888;margin-right:4px">MODE:</span>
    <button id="copy-btn-shadow" class="btn" onclick="setCopyMode('shadow')" style="background:#7c6f00;color:#ffe;border:none;padding:6px 14px;border-radius:4px;cursor:pointer">SHADOW</button>
    <button id="copy-btn-live"   class="btn" onclick="setCopyMode('live')"   style="background:#1a4d1a;color:#afe;border:none;padding:6px 14px;border-radius:4px;cursor:pointer">LIVE</button>
    <button id="copy-btn-off"    class="btn" onclick="setCopyMode('off')"    style="background:#4d1a1a;color:#faa;border:none;padding:6px 14px;border-radius:4px;cursor:pointer">OFF</button>
    <span id="copy-mode-label" style="margin-left:10px;font-weight:bold;font-size:13px"></span>
  </div>

  <!-- Add wallet -->
  <div style="display:flex;gap:6px;margin-bottom:14px">
    <input id="copy-wallet-input" type="text" placeholder="Add wallet address…" style="flex:1;padding:5px 8px;background:#111;color:#eee;border:1px solid #333;border-radius:4px;font-size:12px">
    <button onclick="addCopyWallet()" style="background:#222;color:#eee;border:1px solid #555;padding:5px 12px;border-radius:4px;cursor:pointer;font-size:12px">Add</button>
  </div>

  <!-- Leaderboard -->
  <h3 style="font-size:11px;color:#888;margin-bottom:6px;text-transform:uppercase;letter-spacing:1px">Top Wallets</h3>
  <div id="copy-leaderboard" style="margin-bottom:14px">
    <p class="empty">Scoring wallets…</p>
  </div>

  <!-- Trade log -->
  <h3 style="font-size:11px;color:#888;margin-bottom:6px;text-transform:uppercase;letter-spacing:1px">Copy Trade Log</h3>
  <div id="copy-trade-log"><p class="empty">No copied trades yet</p></div>
</div>
```

- [ ] **Step 5: Add Copy Bot JS to the dashboard script block**

Find the closing `</script>` tag in the HTML and insert before it:

```javascript
// ── Copy Bot ──────────────────────────────────────────────────────────────────
async function refreshCopyBot() {
  const d = await fetch('/copy/state').then(r => r.json()).catch(() => null);
  if (!d) return;

  // Mode label + button highlight
  const modeLabel = document.getElementById('copy-mode-label');
  const modeColors = { shadow: '#ffe566', live: '#66ffaa', off: '#ff6666' };
  modeLabel.textContent = d.mode.toUpperCase();
  modeLabel.style.color = modeColors[d.mode] || '#eee';
  ['shadow','live','off'].forEach(m => {
    document.getElementById('copy-btn-' + m).style.opacity = d.mode === m ? '1' : '0.45';
  });

  // Leaderboard
  const lb = document.getElementById('copy-leaderboard');
  if (d.leaderboard.length === 0) {
    lb.innerHTML = '<p class="empty">No wallets scored yet — add seed wallets above</p>';
  } else {
    lb.innerHTML = '<table><thead><tr><th>Wallet</th><th>Score</th><th>Win%</th><th>Avg ROI</th><th></th></tr></thead><tbody>' +
      d.leaderboard.map(w => `<tr>
        <td style="font-family:monospace;font-size:11px">${w.address}</td>
        <td>${w.score}</td>
        <td class="${w.win_rate >= 50 ? 'green' : 'red'}">${w.win_rate}%</td>
        <td class="${w.avg_roi >= 0 ? 'green' : 'red'}">${w.avg_roi >= 0 ? '+' : ''}${w.avg_roi}%</td>
        <td><button onclick="removeCopyWallet('${w.full_address}')" style="background:none;border:none;color:#f66;cursor:pointer;font-size:11px">✕</button></td>
      </tr>`).join('') +
    '</tbody></table>';
  }

  // Trade log
  const tl = document.getElementById('copy-trade-log');
  if (d.trades.length === 0) {
    tl.innerHTML = '<p class="empty">No copied trades yet</p>';
  } else {
    tl.innerHTML = '<table><thead><tr><th>Time</th><th>Source</th><th>Token</th><th>Action</th><th>Size</th><th>P&L</th><th>Mode</th></tr></thead><tbody>' +
      d.trades.slice(0, 30).map(t => {
        const ts = new Date(t.timestamp * 1000).toLocaleTimeString();
        const pnlCl = t.pnl_sol >= 0 ? 'green' : 'red';
        const modeCl = t.shadow ? 'color:#aaa' : (t.pnl_sol >= 0 ? 'color:#6f6' : 'color:#f66');
        return `<tr style="opacity:${t.shadow ? '0.7' : '1'}">
          <td style="font-size:10px">${ts}</td>
          <td style="font-family:monospace;font-size:10px">${t.source_wallet}</td>
          <td style="font-family:monospace;font-size:10px">${t.token_name}</td>
          <td class="${t.action === 'BUY' ? 'green' : 'red'}">${t.action}</td>
          <td>${t.size_sol} SOL</td>
          <td class="${pnlCl}">${t.pnl_sol >= 0 ? '+' : ''}${t.pnl_sol}</td>
          <td style="${modeCl};font-size:10px">${t.shadow ? 'SHADOW' : 'LIVE'}</td>
        </tr>`;
      }).join('') +
    '</tbody></table>';
  }
}

async function setCopyMode(mode) {
  await fetch('/copy/mode', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({mode}) });
  refreshCopyBot();
}

async function addCopyWallet() {
  const input = document.getElementById('copy-wallet-input');
  const addr = input.value.trim();
  if (!addr) return;
  await fetch('/copy/add_wallet', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({address: addr}) });
  input.value = '';
  refreshCopyBot();
}

async function removeCopyWallet(addr) {
  await fetch('/copy/remove_wallet', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({address: addr}) });
  refreshCopyBot();
}

// Poll copy bot every 10 seconds
setInterval(refreshCopyBot, 10000);
refreshCopyBot();
```

- [ ] **Step 6: Commit**

```bash
git add dashboard.py
git commit -m "feat: wire copy bot into dashboard — leaderboard, shadow/live/off toggle, trade log"
```

---

## Task 4: .env.example — add copy bot env vars

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Append copy bot vars to .env.example**

Add at the end of `.env.example`:

```
# Copy Bot
COPY_MODE=shadow              # shadow | live | off
COPY_MAX_TRADE_PCT=0.10       # max 10% of wallet per copied trade
COPY_TOP_N=5                  # number of top wallets to copy
COPY_TOP_PCT=0.10             # top 10% by composite score enter copy list
```

- [ ] **Step 2: Commit**

```bash
git add .env.example
git commit -m "chore: add copy bot env vars to .env.example"
```

---

## Task 5: Deploy to Railway

- [ ] **Step 1: Verify Procfile has the right start command**

```
cat Procfile
```
Expected output should reference `app.py` or `dashboard.py`. If it references `dashboard.py`, no change needed.

- [ ] **Step 2: Push to GitHub**

```bash
git push origin main
```

Railway auto-deploys on push. Monitor the Railway dashboard at `https://railway.app/project/<your-project>` for build logs.

- [ ] **Step 3: Verify deployment**

```
curl https://soltrader-production.up.railway.app/copy/state
```
Expected: JSON with `mode`, `leaderboard: []`, `trades: []`.
