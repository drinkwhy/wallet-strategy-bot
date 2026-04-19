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
TOP_PCT            = float(os.getenv("COPY_TOP_PCT", "0.10"))
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
    fetch each swap tx, compute win/loss/roi from token balance changes.
    Returns WalletStats. Falls back to zero-trade stats on any error.
    """
    zero = WalletStats(address=address, wins=0, losses=0,
                       avg_roi=0.0, roi_std=0.0, recency_weight=0.0)
    try:
        resp = requests.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": [address, {"limit": HISTORY_TX_LIMIT}]
        }, timeout=10)
        sigs = resp.json().get("result", [])
        if not sigs:
            return zero

        now = time.time()
        recent = sum(1 for s in sigs if s.get("blockTime", 0) > now - 7 * 86400)
        recency_weight = recent / len(sigs)

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

    pre_map  = {b["accountIndex"]: float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
                for b in pre_balances}
    post_map = {b["accountIndex"]: float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
                for b in post_balances}

    account_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    owned_indices = [i for i, k in enumerate(account_keys)
                     if (k.get("pubkey") if isinstance(k, dict) else k) == address]

    if not owned_indices:
        return None

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
