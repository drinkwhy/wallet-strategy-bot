"""
MEV Protection — SolTrader
===========================
Implements MEV mitigation strategies from the research paper:
- Jito bundle submission for front-running protection
- Dynamic priority fees
- Multi-RPC redundancy
- Transaction timing optimization
"""

import time
import random
import threading
import requests
import base64
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any
from enum import Enum
import json

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

class SubmissionStrategy(Enum):
    STANDARD = "standard"      # Direct RPC submission
    JITO_BUNDLE = "jito"       # Jito bundle for MEV protection
    MULTI_RPC = "multi"        # Submit to multiple RPCs
    PRIORITY = "priority"      # High priority fee only


# Jito endpoints
JITO_BLOCK_ENGINE = "https://mainnet.block-engine.jito.wtf"
JITO_BUNDLE_ENDPOINT = f"{JITO_BLOCK_ENGINE}/api/v1/bundles"
JITO_TIP_ENDPOINT = "https://bundles.jito.wtf/api/v1/bundles/tip_floor"

# Jito tip accounts (mainnet)
JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
]

# Default configuration
DEFAULT_CONFIG = {
    "jito_enabled": True,
    "min_tip_lamports": 10_000,       # 0.00001 SOL minimum
    "max_tip_lamports": 500_000,      # 0.0005 SOL maximum
    "tip_percentile": 50,             # Use 50th percentile tip
    "multi_rpc_enabled": True,
    "max_retry_attempts": 3,
    "retry_delay_ms": 500,
    "submission_timeout_sec": 30,
}


# ══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class SubmissionResult:
    """Result of a transaction submission."""
    success: bool
    signature: Optional[str] = None
    strategy_used: SubmissionStrategy = SubmissionStrategy.STANDARD
    tip_lamports: int = 0
    latency_ms: int = 0
    rpc_used: str = ""
    bundle_id: Optional[str] = None
    error: Optional[str] = None
    attempts: int = 1
    
    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "signature": self.signature,
            "strategy": self.strategy_used.value,
            "tip_lamports": self.tip_lamports,
            "latency_ms": self.latency_ms,
            "rpc": self.rpc_used,
            "bundle_id": self.bundle_id,
            "error": self.error,
            "attempts": self.attempts,
        }


@dataclass
class RPCEndpoint:
    """RPC endpoint with health tracking."""
    url: str
    name: str
    priority: int = 1  # Higher = preferred
    is_healthy: bool = True
    last_check: float = 0
    success_count: int = 0
    fail_count: int = 0
    avg_latency_ms: float = 0
    
    @property
    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        return self.success_count / total if total > 0 else 1.0
    
    def record_result(self, success: bool, latency_ms: float):
        if success:
            self.success_count += 1
        else:
            self.fail_count += 1
        
        # Rolling average
        alpha = 0.3
        self.avg_latency_ms = alpha * latency_ms + (1 - alpha) * self.avg_latency_ms
        self.last_check = time.time()
        
        # Update health
        if self.fail_count > 5 and self.success_rate < 0.5:
            self.is_healthy = False


@dataclass
class TipEstimate:
    """Jito tip estimate."""
    landed_25th: float
    landed_50th: float
    landed_75th: float
    landed_95th: float
    timestamp: float = field(default_factory=time.time)
    
    def get_tip(self, percentile: int = 50) -> int:
        """Get tip in lamports for given percentile."""
        if percentile <= 25:
            sol = self.landed_25th
        elif percentile <= 50:
            sol = self.landed_50th
        elif percentile <= 75:
            sol = self.landed_75th
        else:
            sol = self.landed_95th
        
        return int(sol * 1e9)


# ══════════════════════════════════════════════════════════════════════════════
# TIP MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class JitoTipManager:
    """Manages Jito tip estimation and dynamic pricing."""
    
    def __init__(self):
        self._lock = threading.RLock()
        self._current_estimate: Optional[TipEstimate] = None
        self._estimate_ttl = 30  # seconds
        self._last_fetch = 0
        
    def get_tip_estimate(self) -> Optional[TipEstimate]:
        """Get current tip estimate, fetching if stale."""
        with self._lock:
            if self._current_estimate and time.time() - self._last_fetch < self._estimate_ttl:
                return self._current_estimate
        
        # Fetch fresh estimate
        try:
            resp = requests.get(JITO_TIP_ENDPOINT, timeout=5)
            if resp.ok:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    tip_data = data[0]
                    estimate = TipEstimate(
                        landed_25th=float(tip_data.get("landed_tips_25th_percentile", 0.00001)),
                        landed_50th=float(tip_data.get("landed_tips_50th_percentile", 0.00002)),
                        landed_75th=float(tip_data.get("landed_tips_75th_percentile", 0.00005)),
                        landed_95th=float(tip_data.get("landed_tips_95th_percentile", 0.0001)),
                    )
                    with self._lock:
                        self._current_estimate = estimate
                        self._last_fetch = time.time()
                    return estimate
        except Exception as e:
            print(f"[JitoTip] Failed to fetch tip estimate: {e}")
        
        # Return default estimate
        return TipEstimate(
            landed_25th=0.00001,
            landed_50th=0.00002,
            landed_75th=0.00005,
            landed_95th=0.0001,
        )
    
    def get_dynamic_tip(
        self,
        urgency: str = "normal",  # "low", "normal", "high", "critical"
        trade_size_sol: float = 0,
    ) -> int:
        """Get dynamic tip based on urgency and trade size."""
        estimate = self.get_tip_estimate()
        if not estimate:
            return DEFAULT_CONFIG["min_tip_lamports"]
        
        # Base percentile by urgency
        percentiles = {
            "low": 25,
            "normal": 50,
            "high": 75,
            "critical": 95,
        }
        percentile = percentiles.get(urgency, 50)
        
        base_tip = estimate.get_tip(percentile)
        
        # Scale by trade size (larger trades get higher tips)
        if trade_size_sol > 1.0:
            base_tip = int(base_tip * 1.5)
        elif trade_size_sol > 0.5:
            base_tip = int(base_tip * 1.2)
        
        # Clamp to config limits
        return max(
            DEFAULT_CONFIG["min_tip_lamports"],
            min(DEFAULT_CONFIG["max_tip_lamports"], base_tip)
        )
    
    def get_random_tip_account(self) -> str:
        """Get a random Jito tip account."""
        return random.choice(JITO_TIP_ACCOUNTS)


# ══════════════════════════════════════════════════════════════════════════════
# RPC MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class RPCManager:
    """Manages multiple RPC endpoints with health tracking and failover."""
    
    def __init__(self, primary_rpc: str):
        self._lock = threading.RLock()
        self._endpoints: List[RPCEndpoint] = []
        self._submission_history: deque = deque(maxlen=500)
        
        # Add primary endpoint
        self.add_endpoint(primary_rpc, "primary", priority=10)
    
    def add_endpoint(self, url: str, name: str, priority: int = 1):
        """Add an RPC endpoint."""
        with self._lock:
            # Check if already exists
            for ep in self._endpoints:
                if ep.url == url:
                    return
            
            self._endpoints.append(RPCEndpoint(
                url=url,
                name=name,
                priority=priority,
            ))
    
    def get_best_endpoint(self) -> Optional[RPCEndpoint]:
        """Get the best available endpoint."""
        with self._lock:
            healthy = [ep for ep in self._endpoints if ep.is_healthy]
            if not healthy:
                # Reset health and try again
                for ep in self._endpoints:
                    ep.is_healthy = True
                healthy = self._endpoints
            
            if not healthy:
                return None
            
            # Sort by priority, then success rate, then latency
            healthy.sort(key=lambda ep: (
                -ep.priority,
                -ep.success_rate,
                ep.avg_latency_ms
            ))
            
            return healthy[0]
    
    def get_all_healthy_endpoints(self) -> List[RPCEndpoint]:
        """Get all healthy endpoints for multi-RPC submission."""
        with self._lock:
            return [ep for ep in self._endpoints if ep.is_healthy]
    
    def record_result(self, url: str, success: bool, latency_ms: float):
        """Record submission result for an endpoint."""
        with self._lock:
            for ep in self._endpoints:
                if ep.url == url:
                    ep.record_result(success, latency_ms)
                    break
            
            self._submission_history.append({
                "url": url,
                "success": success,
                "latency_ms": latency_ms,
                "ts": time.time(),
            })
    
    def get_health_report(self) -> Dict:
        """Get health report for all endpoints."""
        with self._lock:
            return {
                "endpoints": [
                    {
                        "name": ep.name,
                        "url": ep.url[:50] + "...",
                        "healthy": ep.is_healthy,
                        "success_rate": round(ep.success_rate * 100, 1),
                        "avg_latency_ms": round(ep.avg_latency_ms, 0),
                    }
                    for ep in self._endpoints
                ],
                "recent_success_rate": self._get_recent_success_rate(),
            }
    
    def _get_recent_success_rate(self, window_sec: int = 300) -> float:
        """Get success rate over recent window."""
        cutoff = time.time() - window_sec
        recent = [h for h in self._submission_history if h["ts"] >= cutoff]
        if not recent:
            return 1.0
        return sum(1 for h in recent if h["success"]) / len(recent)


# ══════════════════════════════════════════════════════════════════════════════
# JITO BUNDLE SUBMITTER
# ══════════════════════════════════════════════════════════════════════════════

class JitoBundleSubmitter:
    """Submits transactions as Jito bundles for MEV protection."""
    
    def __init__(self, tip_manager: JitoTipManager):
        self.tip_manager = tip_manager
        self._lock = threading.RLock()
        self._pending_bundles: Dict[str, Dict] = {}  # bundle_id -> info
        
    def submit_bundle(
        self,
        transactions: List[bytes],  # Signed transaction bytes
        tip_lamports: int = 0,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Submit a bundle of transactions to Jito.
        Returns (success, bundle_id, error).
        """
        if tip_lamports <= 0:
            tip_lamports = self.tip_manager.get_dynamic_tip("normal")
        
        try:
            # Encode transactions
            encoded_txs = [base64.b64encode(tx).decode() for tx in transactions]
            
            # Submit bundle
            resp = requests.post(
                JITO_BUNDLE_ENDPOINT,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "sendBundle",
                    "params": [encoded_txs],
                },
                timeout=10,
            )
            
            if resp.ok:
                data = resp.json()
                if "result" in data:
                    bundle_id = data["result"]
                    with self._lock:
                        self._pending_bundles[bundle_id] = {
                            "submitted_at": time.time(),
                            "tx_count": len(transactions),
                            "tip": tip_lamports,
                        }
                    return True, bundle_id, None
                elif "error" in data:
                    return False, None, str(data["error"])
            
            return False, None, f"HTTP {resp.status_code}"
            
        except Exception as e:
            return False, None, str(e)
    
    def check_bundle_status(self, bundle_id: str) -> Optional[Dict]:
        """Check the status of a submitted bundle."""
        try:
            resp = requests.post(
                JITO_BLOCK_ENGINE + "/api/v1/bundles",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getBundleStatuses",
                    "params": [[bundle_id]],
                },
                timeout=5,
            )
            
            if resp.ok:
                data = resp.json()
                result = data.get("result", {})
                statuses = result.get("value", [])
                if statuses:
                    return statuses[0]
            
            return None
        except Exception:
            return None
    
    def wait_for_confirmation(
        self,
        bundle_id: str,
        timeout_sec: int = 30,
    ) -> Tuple[bool, Optional[str]]:
        """
        Wait for bundle confirmation.
        Returns (confirmed, signature or error).
        """
        start = time.time()
        
        while time.time() - start < timeout_sec:
            status = self.check_bundle_status(bundle_id)
            
            if status:
                confirmation_status = status.get("confirmation_status")
                
                if confirmation_status == "confirmed":
                    # Get signature from transactions
                    transactions = status.get("transactions", [])
                    if transactions:
                        return True, transactions[0]
                    return True, bundle_id
                
                elif confirmation_status == "finalized":
                    transactions = status.get("transactions", [])
                    if transactions:
                        return True, transactions[0]
                    return True, bundle_id
                
                elif confirmation_status in ("failed", "dropped"):
                    err = status.get("err")
                    return False, f"Bundle {confirmation_status}: {err}"
            
            time.sleep(0.5)
        
        return False, "Timeout waiting for bundle confirmation"


# ══════════════════════════════════════════════════════════════════════════════
# TRANSACTION SUBMITTER
# ══════════════════════════════════════════════════════════════════════════════

class TransactionSubmitter:
    """
    High-level transaction submitter with MEV protection.
    Chooses optimal submission strategy based on trade characteristics.
    """
    
    def __init__(self, primary_rpc: str, config: Dict = None):
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.tip_manager = JitoTipManager()
        self.rpc_manager = RPCManager(primary_rpc)
        self.jito_submitter = JitoBundleSubmitter(self.tip_manager)
        
        self._lock = threading.RLock()
        self._submission_stats: deque = deque(maxlen=1000)
    
    def submit_transaction(
        self,
        signed_tx: bytes,
        strategy: SubmissionStrategy = None,
        urgency: str = "normal",
        trade_size_sol: float = 0,
        is_mev_sensitive: bool = True,
    ) -> SubmissionResult:
        """
        Submit a signed transaction with optimal strategy.
        
        Args:
            signed_tx: Signed transaction bytes
            strategy: Override submission strategy
            urgency: "low", "normal", "high", "critical"
            trade_size_sol: Trade size for tip calculation
            is_mev_sensitive: Whether trade is sensitive to MEV
        """
        start_time = time.perf_counter()
        
        # Choose strategy
        if strategy is None:
            strategy = self._choose_strategy(trade_size_sol, is_mev_sensitive, urgency)
        
        result = SubmissionResult(
            success=False,
            strategy_used=strategy,
        )
        
        # Execute strategy
        if strategy == SubmissionStrategy.JITO_BUNDLE and self.config["jito_enabled"]:
            result = self._submit_jito(signed_tx, urgency, trade_size_sol)
        elif strategy == SubmissionStrategy.MULTI_RPC and self.config["multi_rpc_enabled"]:
            result = self._submit_multi_rpc(signed_tx)
        else:
            result = self._submit_standard(signed_tx)
        
        result.latency_ms = int((time.perf_counter() - start_time) * 1000)
        result.strategy_used = strategy
        
        # Record stats
        self._record_submission(result)
        
        return result
    
    def _choose_strategy(
        self,
        trade_size_sol: float,
        is_mev_sensitive: bool,
        urgency: str,
    ) -> SubmissionStrategy:
        """Choose optimal submission strategy."""
        # Large MEV-sensitive trades use Jito
        if is_mev_sensitive and trade_size_sol >= 0.1:
            if self.config["jito_enabled"]:
                return SubmissionStrategy.JITO_BUNDLE
        
        # High urgency uses multi-RPC
        if urgency in ("high", "critical"):
            if self.config["multi_rpc_enabled"]:
                return SubmissionStrategy.MULTI_RPC
        
        # Default to standard
        return SubmissionStrategy.STANDARD
    
    def _submit_standard(self, signed_tx: bytes) -> SubmissionResult:
        """Standard RPC submission."""
        endpoint = self.rpc_manager.get_best_endpoint()
        if not endpoint:
            return SubmissionResult(
                success=False,
                error="No healthy RPC endpoints",
            )
        
        start = time.perf_counter()
        attempts = 0
        
        for attempt in range(self.config["max_retry_attempts"]):
            attempts += 1
            try:
                encoded = base64.b64encode(signed_tx).decode()
                resp = requests.post(
                    endpoint.url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "sendTransaction",
                        "params": [
                            encoded,
                            {
                                "encoding": "base64",
                                "skipPreflight": False,
                                "preflightCommitment": "confirmed",
                            }
                        ],
                    },
                    timeout=10,
                )
                
                latency = (time.perf_counter() - start) * 1000
                
                if resp.ok:
                    data = resp.json()
                    if "result" in data:
                        self.rpc_manager.record_result(endpoint.url, True, latency)
                        return SubmissionResult(
                            success=True,
                            signature=data["result"],
                            rpc_used=endpoint.name,
                            attempts=attempts,
                        )
                    elif "error" in data:
                        error_msg = str(data["error"])
                        # Don't retry certain errors
                        if "insufficient" in error_msg.lower() or "already processed" in error_msg.lower():
                            self.rpc_manager.record_result(endpoint.url, False, latency)
                            return SubmissionResult(
                                success=False,
                                error=error_msg,
                                rpc_used=endpoint.name,
                                attempts=attempts,
                            )
                
                self.rpc_manager.record_result(endpoint.url, False, latency)
                
            except Exception as e:
                self.rpc_manager.record_result(endpoint.url, False, 0)
                if attempt == self.config["max_retry_attempts"] - 1:
                    return SubmissionResult(
                        success=False,
                        error=str(e),
                        rpc_used=endpoint.name,
                        attempts=attempts,
                    )
            
            time.sleep(self.config["retry_delay_ms"] / 1000)
        
        return SubmissionResult(
            success=False,
            error="Max retries exceeded",
            rpc_used=endpoint.name,
            attempts=attempts,
        )
    
    def _submit_jito(
        self,
        signed_tx: bytes,
        urgency: str,
        trade_size_sol: float,
    ) -> SubmissionResult:
        """Submit via Jito bundle."""
        tip = self.tip_manager.get_dynamic_tip(urgency, trade_size_sol)
        
        success, bundle_id, error = self.jito_submitter.submit_bundle([signed_tx], tip)
        
        if not success:
            # Fallback to standard submission
            result = self._submit_standard(signed_tx)
            result.tip_lamports = tip
            return result
        
        # Wait for confirmation
        confirmed, sig_or_error = self.jito_submitter.wait_for_confirmation(
            bundle_id,
            self.config["submission_timeout_sec"],
        )
        
        return SubmissionResult(
            success=confirmed,
            signature=sig_or_error if confirmed else None,
            bundle_id=bundle_id,
            tip_lamports=tip,
            rpc_used="jito",
            error=None if confirmed else sig_or_error,
        )
    
    def _submit_multi_rpc(self, signed_tx: bytes) -> SubmissionResult:
        """Submit to multiple RPCs simultaneously."""
        endpoints = self.rpc_manager.get_all_healthy_endpoints()
        if not endpoints:
            return self._submit_standard(signed_tx)
        
        encoded = base64.b64encode(signed_tx).decode()
        results = []
        
        def submit_to_endpoint(ep: RPCEndpoint):
            start = time.perf_counter()
            try:
                resp = requests.post(
                    ep.url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "sendTransaction",
                        "params": [encoded, {"encoding": "base64", "skipPreflight": False}],
                    },
                    timeout=10,
                )
                latency = (time.perf_counter() - start) * 1000
                
                if resp.ok:
                    data = resp.json()
                    if "result" in data:
                        self.rpc_manager.record_result(ep.url, True, latency)
                        return (True, data["result"], ep.name, latency)
                
                self.rpc_manager.record_result(ep.url, False, latency)
                return (False, None, ep.name, latency)
            except Exception as e:
                return (False, str(e), ep.name, 0)
        
        # Submit to all endpoints (could use ThreadPoolExecutor for true parallelism)
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(endpoints)) as executor:
            futures = [executor.submit(submit_to_endpoint, ep) for ep in endpoints[:3]]  # Max 3
            
            for future in concurrent.futures.as_completed(futures, timeout=15):
                try:
                    success, sig, name, latency = future.result()
                    if success:
                        return SubmissionResult(
                            success=True,
                            signature=sig,
                            rpc_used=name,
                            latency_ms=int(latency),
                        )
                    results.append((success, sig, name))
                except Exception:
                    pass
        
        # All failed
        return SubmissionResult(
            success=False,
            error="All RPCs failed",
            rpc_used="multi",
        )
    
    def _record_submission(self, result: SubmissionResult):
        """Record submission for analytics."""
        with self._lock:
            self._submission_stats.append({
                **result.to_dict(),
                "ts": time.time(),
            })
    
    def get_submission_stats(self, window_sec: int = 3600) -> Dict:
        """Get submission statistics."""
        cutoff = time.time() - window_sec
        with self._lock:
            recent = [s for s in self._submission_stats if s["ts"] >= cutoff]
        
        if not recent:
            return {
                "total": 0,
                "success_rate": 0,
                "avg_latency_ms": 0,
                "by_strategy": {},
            }
        
        total = len(recent)
        successes = sum(1 for s in recent if s["success"])
        latencies = [s["latency_ms"] for s in recent if s["success"]]
        
        by_strategy = {}
        for s in recent:
            strat = s["strategy"]
            if strat not in by_strategy:
                by_strategy[strat] = {"total": 0, "success": 0}
            by_strategy[strat]["total"] += 1
            if s["success"]:
                by_strategy[strat]["success"] += 1
        
        return {
            "total": total,
            "success_rate": round(successes / total * 100, 1),
            "avg_latency_ms": round(sum(latencies) / len(latencies), 0) if latencies else 0,
            "by_strategy": {
                k: round(v["success"] / v["total"] * 100, 1) if v["total"] > 0 else 0
                for k, v in by_strategy.items()
            },
        }
    
    def add_backup_rpc(self, url: str, name: str):
        """Add a backup RPC endpoint."""
        self.rpc_manager.add_endpoint(url, name, priority=5)


# ══════════════════════════════════════════════════════════════════════════════
# PRIORITY FEE OPTIMIZER
# ══════════════════════════════════════════════════════════════════════════════

class PriorityFeeOptimizer:
    """Optimizes priority fees based on network conditions."""
    
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self._lock = threading.RLock()
        self._fee_history: deque = deque(maxlen=100)
        self._last_fetch = 0
        self._cached_fees: Optional[Dict] = None
        
    def get_optimal_fee(
        self,
        urgency: str = "normal",
        percentile: int = 50,
    ) -> int:
        """
        Get optimal priority fee in microlamports.
        """
        fees = self._get_recent_priority_fees()
        
        if not fees:
            # Default fees by urgency
            defaults = {
                "low": 1000,
                "normal": 10000,
                "high": 50000,
                "critical": 100000,
            }
            return defaults.get(urgency, 10000)
        
        # Sort and get percentile
        sorted_fees = sorted(fees)
        idx = min(len(sorted_fees) - 1, max(0, int(len(sorted_fees) * percentile / 100)))
        base_fee = sorted_fees[idx]
        
        # Adjust by urgency
        multipliers = {
            "low": 0.8,
            "normal": 1.0,
            "high": 1.5,
            "critical": 2.0,
        }
        
        return int(base_fee * multipliers.get(urgency, 1.0))
    
    def _get_recent_priority_fees(self) -> List[int]:
        """Get recent priority fees from the network."""
        # Check cache
        if self._cached_fees and time.time() - self._last_fetch < 10:
            return self._cached_fees.get("fees", [])
        
        try:
            resp = requests.post(
                self.rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getRecentPrioritizationFees",
                    "params": [],
                },
                timeout=5,
            )
            
            if resp.ok:
                data = resp.json()
                result = data.get("result", [])
                fees = [int(r.get("prioritizationFee", 0)) for r in result if r.get("prioritizationFee")]
                
                with self._lock:
                    self._cached_fees = {"fees": fees}
                    self._last_fetch = time.time()
                
                return fees
        except Exception as e:
            print(f"[PriorityFee] Failed to fetch fees: {e}")
        
        return []
    
    def record_fee_result(self, fee: int, success: bool, latency_ms: int):
        """Record fee result for learning."""
        with self._lock:
            self._fee_history.append({
                "fee": fee,
                "success": success,
                "latency_ms": latency_ms,
                "ts": time.time(),
            })


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRATED MEV PROTECTION SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

class MEVProtectionSystem:
    """
    Integrated MEV protection system.
    Main interface for the trading bot.
    """
    
    def __init__(self, primary_rpc: str, config: Dict = None):
        self.submitter = TransactionSubmitter(primary_rpc, config)
        self.fee_optimizer = PriorityFeeOptimizer(primary_rpc)
        self.tip_manager = self.submitter.tip_manager
        
    def submit_trade(
        self,
        signed_tx: bytes,
        trade_size_sol: float,
        is_buy: bool = True,
        urgency: str = "normal",
    ) -> SubmissionResult:
        """
        Submit a trade with optimal MEV protection.
        """
        # Buys are more MEV-sensitive (front-running risk)
        is_mev_sensitive = is_buy and trade_size_sol >= 0.05
        
        return self.submitter.submit_transaction(
            signed_tx=signed_tx,
            urgency=urgency,
            trade_size_sol=trade_size_sol,
            is_mev_sensitive=is_mev_sensitive,
        )
    
    def get_optimal_priority_fee(self, urgency: str = "normal") -> int:
        """Get optimal priority fee for current network conditions."""
        return self.fee_optimizer.get_optimal_fee(urgency)
    
    def get_jito_tip(self, urgency: str = "normal", trade_size_sol: float = 0) -> int:
        """Get recommended Jito tip."""
        return self.tip_manager.get_dynamic_tip(urgency, trade_size_sol)
    
    def get_tip_account(self) -> str:
        """Get a Jito tip account address."""
        return self.tip_manager.get_random_tip_account()
    
    def add_backup_rpc(self, url: str, name: str):
        """Add backup RPC for redundancy."""
        self.submitter.add_backup_rpc(url, name)
    
    def get_health_report(self) -> Dict:
        """Get system health report."""
        return {
            "rpc_health": self.submitter.rpc_manager.get_health_report(),
            "submission_stats": self.submitter.get_submission_stats(3600),
        }


# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON
# ══════════════════════════════════════════════════════════════════════════════

_mev_system: Optional[MEVProtectionSystem] = None
_mev_lock = threading.Lock()

def get_mev_protection(primary_rpc: str = None, config: Dict = None) -> MEVProtectionSystem:
    """Get singleton MEV protection system."""
    global _mev_system
    with _mev_lock:
        if _mev_system is None:
            if not primary_rpc:
                raise ValueError("primary_rpc required for first initialization")
            _mev_system = MEVProtectionSystem(primary_rpc, config)
        return _mev_system
