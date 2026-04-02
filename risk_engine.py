"""
Risk Engine — SolTrader
========================
Implements comprehensive risk management from the research paper:
- Pre-trade validation (exit simulation, honeypot detection)
- Token risk scoring (rug pull, transfer hooks, liquidity traps)
- Execution guardrails (slippage, fees, circuit breakers)
- Post-trade monitoring
"""

import math
import time
import threading
import requests
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Any
from enum import Enum
import json
import re

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

class RiskLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
    BLOCKED = "blocked"


# Risk thresholds
RISK_THRESHOLDS = {
    "max_slippage_bps": 1500,        # 15% max slippage
    "max_priority_fee_sol": 0.01,    # Max priority fee
    "min_liquidity_usd": 5000,       # Minimum liquidity
    "max_holder_concentration_pct": 80,  # Max top-10 holder %
    "max_deployer_holding_pct": 20,  # Max deployer can hold
    "min_holders": 50,               # Minimum holder count
    "max_lp_single_holder_pct": 90,  # Max % of LP by single wallet
    "min_token_age_sec": 60,         # Minimum token age (1 min)
    "max_failed_exits_consecutive": 3,
    "circuit_breaker_loss_pct": 30,  # Trip after 30% drawdown
    "circuit_breaker_fail_rate": 0.5,  # Trip after 50% tx failures
    "honeypot_sell_tax_threshold": 20,  # >20% sell tax = honeypot
    "honeypot_buy_tax_threshold": 15,   # >15% buy tax suspicious
}

# Keys that the optimizer is allowed to tune — safety/circuit-breaker thresholds are excluded.
_TUNABLE_RISK_KEYS = frozenset({"min_liquidity_usd", "min_token_age_sec"})


def apply_optimized_risk_thresholds(values):
    """Apply optimizer-derived values to the global RISK_THRESHOLDS dict.

    Only keys present in _TUNABLE_RISK_KEYS are updated; safety and
    circuit-breaker thresholds are intentionally excluded so they cannot
    be relaxed by the optimizer.

    Args:
        values: dict mapping RISK_THRESHOLDS key → new value (from sweep_risk_thresholds)
    """
    if not isinstance(values, dict):
        return
    for key, value in values.items():
        if key in _TUNABLE_RISK_KEYS and key in RISK_THRESHOLDS:
            RISK_THRESHOLDS[key] = value

# Known rug pull patterns
RUG_PATTERNS = {
    "instant_lp_removal": {
        "description": "LP removed within minutes of creation",
        "severity": RiskLevel.CRITICAL,
    },
    "deployer_dump": {
        "description": "Deployer selling large amounts",
        "severity": RiskLevel.CRITICAL,
    },
    "hidden_mint": {
        "description": "Token has hidden mint authority",
        "severity": RiskLevel.BLOCKED,
    },
    "freeze_authority": {
        "description": "Token has active freeze authority",
        "severity": RiskLevel.HIGH,
    },
    "transfer_hook_malicious": {
        "description": "Transfer hook with suspicious behavior",
        "severity": RiskLevel.BLOCKED,
    },
    "honeypot_detected": {
        "description": "Cannot sell or high sell tax",
        "severity": RiskLevel.BLOCKED,
    },
    "liquidity_trap": {
        "description": "Liquidity locked but controlled by team",
        "severity": RiskLevel.HIGH,
    },
}

# Token-2022 transfer hook risk indicators
TRANSFER_HOOK_RISKS = [
    "blacklist",
    "whitelist", 
    "transfer_fee",
    "pausable",
    "freezable",
]


# ══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class TokenRiskAssessment:
    """Comprehensive risk assessment for a token."""
    mint: str
    timestamp: float = field(default_factory=time.time)
    risk_level: RiskLevel = RiskLevel.MEDIUM
    risk_score: int = 50  # 0-100, higher = riskier
    can_exit: bool = True
    exit_confidence: float = 1.0  # 0-1
    
    # Component scores
    liquidity_risk: int = 0
    holder_risk: int = 0
    contract_risk: int = 0
    deployer_risk: int = 0
    market_risk: int = 0
    
    # Flags
    flags: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    
    # Token details
    has_mint_authority: bool = False
    has_freeze_authority: bool = False
    has_transfer_hook: bool = False
    transfer_hook_program: Optional[str] = None
    is_token_2022: bool = False
    
    # Tax info
    buy_tax_pct: float = 0.0
    sell_tax_pct: float = 0.0
    
    # Liquidity info
    liquidity_usd: float = 0.0
    lp_locked_pct: float = 0.0
    lp_holder_count: int = 0
    
    def to_dict(self) -> dict:
        return {
            "mint": self.mint,
            "risk_level": self.risk_level.value,
            "risk_score": self.risk_score,
            "can_exit": self.can_exit,
            "exit_confidence": round(self.exit_confidence, 2),
            "flags": self.flags,
            "warnings": self.warnings,
            "blockers": self.blockers,
            "components": {
                "liquidity": self.liquidity_risk,
                "holder": self.holder_risk,
                "contract": self.contract_risk,
                "deployer": self.deployer_risk,
                "market": self.market_risk,
            },
            "buy_tax_pct": round(self.buy_tax_pct, 1),
            "sell_tax_pct": round(self.sell_tax_pct, 1),
        }


@dataclass
class ExitSimulationResult:
    """Result of simulating a token exit."""
    mint: str
    can_exit: bool
    expected_slippage_bps: int
    expected_output_sol: float
    route_available: bool
    route_source: str = ""
    error: Optional[str] = None
    simulation_time_ms: int = 0
    
    def to_dict(self) -> dict:
        return {
            "mint": self.mint,
            "can_exit": self.can_exit,
            "slippage_bps": self.expected_slippage_bps,
            "expected_sol": round(self.expected_output_sol, 4),
            "route": self.route_source,
            "error": self.error,
        }


@dataclass
class CircuitBreakerState:
    """State of circuit breaker for a bot instance."""
    user_id: int
    is_tripped: bool = False
    trip_reason: Optional[str] = None
    trip_time: Optional[float] = None
    consecutive_failures: int = 0
    consecutive_losses: int = 0
    total_loss_pct: float = 0.0
    failed_exits: int = 0
    last_health_check: float = field(default_factory=time.time)
    
    def trip(self, reason: str):
        self.is_tripped = True
        self.trip_reason = reason
        self.trip_time = time.time()
    
    def reset(self):
        self.is_tripped = False
        self.trip_reason = None
        self.trip_time = None
        self.consecutive_failures = 0
        self.consecutive_losses = 0
        self.failed_exits = 0
    
    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "is_tripped": self.is_tripped,
            "trip_reason": self.trip_reason,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_losses": self.consecutive_losses,
            "failed_exits": self.failed_exits,
        }


# ══════════════════════════════════════════════════════════════════════════════
# HONEYPOT DETECTOR
# ══════════════════════════════════════════════════════════════════════════════

class HoneypotDetector:
    """
    Detects honeypot tokens using multiple signals:
    - Sell simulation failures
    - High sell taxes
    - Transfer restrictions
    - Historical sell patterns
    """
    
    def __init__(self, helius_rpc: str):
        self.helius_rpc = helius_rpc
        self._lock = threading.RLock()
        self._cache: Dict[str, Tuple[bool, float]] = {}  # mint -> (is_honeypot, timestamp)
        self._cache_ttl = 300  # 5 minute cache
        
    def is_honeypot(self, mint: str) -> Tuple[bool, List[str]]:
        """
        Check if a token is a honeypot.
        Returns (is_honeypot, reasons).
        """
        # Check cache
        with self._lock:
            cached = self._cache.get(mint)
            if cached and time.time() - cached[1] < self._cache_ttl:
                return cached[0], ["cached result"]
        
        reasons = []
        is_honeypot = False
        
        # Check 1: Token account info for authorities
        token_info = self._get_token_info(mint)
        if token_info:
            if token_info.get("freeze_authority"):
                reasons.append("Has freeze authority")
                is_honeypot = True
            
            if token_info.get("mint_authority"):
                reasons.append("Has mint authority (can dilute)")
        
        # Check 2: Check for Token-2022 transfer hooks
        if token_info and token_info.get("is_token_2022"):
            hook_info = self._check_transfer_hook(mint)
            if hook_info.get("has_hook"):
                reasons.append(f"Has transfer hook: {hook_info.get('hook_type', 'unknown')}")
                if hook_info.get("is_malicious"):
                    is_honeypot = True
                    reasons.append("Transfer hook appears malicious")
        
        # Check 3: Historical sell success rate
        sell_stats = self._analyze_sell_history(mint)
        if sell_stats:
            if sell_stats.get("sell_failure_rate", 0) > 0.3:
                reasons.append(f"High sell failure rate: {sell_stats['sell_failure_rate']*100:.0f}%")
                is_honeypot = True
            
            if sell_stats.get("avg_sell_tax", 0) > RISK_THRESHOLDS["honeypot_sell_tax_threshold"]:
                reasons.append(f"High sell tax: {sell_stats['avg_sell_tax']:.0f}%")
                is_honeypot = True
        
        # Update cache
        with self._lock:
            self._cache[mint] = (is_honeypot, time.time())
        
        return is_honeypot, reasons
    
    def _get_token_info(self, mint: str) -> Optional[Dict]:
        """Get token account info from RPC."""
        try:
            resp = requests.post(self.helius_rpc, json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAccountInfo",
                "params": [mint, {"encoding": "jsonParsed"}],
            }, timeout=5)
            
            data = resp.json()
            if "result" not in data or not data["result"]:
                return None
            
            account = data["result"]["value"]
            if not account:
                return None
            
            parsed = account.get("data", {}).get("parsed", {})
            info = parsed.get("info", {})
            
            return {
                "mint_authority": info.get("mintAuthority"),
                "freeze_authority": info.get("freezeAuthority"),
                "decimals": info.get("decimals", 9),
                "supply": int(info.get("supply", 0)),
                "is_token_2022": account.get("owner") == "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
            }
        except Exception as e:
            print(f"[HoneypotDetector] Token info error: {e}")
            return None
    
    def _check_transfer_hook(self, mint: str) -> Dict:
        """Check for Token-2022 transfer hooks."""
        # This would require parsing the token's extension data
        # For now, return a basic check
        return {
            "has_hook": False,
            "hook_type": None,
            "is_malicious": False,
        }
    
    def _analyze_sell_history(self, mint: str) -> Optional[Dict]:
        """Analyze historical sell transactions for the token."""
        # This would analyze recent transactions to detect sell patterns
        # Returns sell success rate and average tax
        return {
            "sell_failure_rate": 0.0,
            "avg_sell_tax": 0.0,
            "total_sells_analyzed": 0,
        }
    
    def estimate_sell_tax(self, mint: str, input_amount: int, output_amount: int, 
                          expected_output: int) -> float:
        """Estimate sell tax from actual vs expected output."""
        if expected_output <= 0:
            return 0.0
        
        actual_ratio = output_amount / expected_output
        tax_pct = (1 - actual_ratio) * 100
        return max(0, tax_pct)


# ══════════════════════════════════════════════════════════════════════════════
# EXIT SIMULATOR
# ══════════════════════════════════════════════════════════════════════════════

class ExitSimulator:
    """
    Simulates token exits to verify sellability before entry.
    Uses Jupiter quote API to check route availability.
    """
    
    SOL_MINT = "So11111111111111111111111111111111111111112"
    
    def __init__(self, helius_rpc: str):
        self.helius_rpc = helius_rpc
        self._lock = threading.RLock()
        self._cache: Dict[str, Tuple[ExitSimulationResult, float]] = {}
        self._cache_ttl = 120  # 2 minute cache
        
    def simulate_exit(
        self, 
        mint: str, 
        token_amount: int,
        max_slippage_bps: int = 1000,
    ) -> ExitSimulationResult:
        """
        Simulate selling token_amount of a token.
        Returns ExitSimulationResult with route info.
        """
        start_time = time.perf_counter()
        
        # Check cache for recent simulation
        cache_key = f"{mint}:{token_amount}"
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and time.time() - cached[1] < self._cache_ttl:
                return cached[0]
        
        try:
            # Get Jupiter quote for sell
            quote = self._get_jupiter_quote(
                input_mint=mint,
                output_mint=self.SOL_MINT,
                amount=token_amount,
                slippage_bps=max_slippage_bps,
            )
            
            sim_time = int((time.perf_counter() - start_time) * 1000)
            
            if not quote:
                result = ExitSimulationResult(
                    mint=mint,
                    can_exit=False,
                    expected_slippage_bps=0,
                    expected_output_sol=0,
                    route_available=False,
                    error="No route available",
                    simulation_time_ms=sim_time,
                )
            else:
                out_amount = int(quote.get("outAmount", 0))
                in_amount = int(quote.get("inAmount", 0))
                price_impact = float(quote.get("priceImpactPct", 0))
                
                # Extract route info
                route_plan = quote.get("routePlan", [])
                route_source = "unknown"
                if route_plan:
                    swap_info = route_plan[0].get("swapInfo", {})
                    route_source = swap_info.get("label", swap_info.get("ammKey", "unknown"))
                
                # Calculate expected slippage
                slippage_bps = int(abs(price_impact) * 100)
                
                result = ExitSimulationResult(
                    mint=mint,
                    can_exit=True,
                    expected_slippage_bps=slippage_bps,
                    expected_output_sol=out_amount / 1e9,
                    route_available=True,
                    route_source=route_source,
                    simulation_time_ms=sim_time,
                )
                
                # Check if slippage is too high
                if slippage_bps > max_slippage_bps:
                    result.can_exit = False
                    result.error = f"Slippage too high: {slippage_bps}bps"
            
        except Exception as e:
            sim_time = int((time.perf_counter() - start_time) * 1000)
            result = ExitSimulationResult(
                mint=mint,
                can_exit=False,
                expected_slippage_bps=0,
                expected_output_sol=0,
                route_available=False,
                error=str(e),
                simulation_time_ms=sim_time,
            )
        
        # Cache result
        with self._lock:
            self._cache[cache_key] = (result, time.time())
        
        return result
    
    def _get_jupiter_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int = 500,
    ) -> Optional[Dict]:
        """Get quote from Jupiter API."""
        try:
            url = (
                f"https://lite-api.jup.ag/swap/v1/quote"
                f"?inputMint={input_mint}"
                f"&outputMint={output_mint}"
                f"&amount={amount}"
                f"&slippageBps={slippage_bps}"
            )
            
            resp = requests.get(url, timeout=8)
            if resp.ok:
                data = resp.json()
                if "error" not in data:
                    return data
            return None
        except Exception:
            return None
    
    def check_exit_feasibility(self, mint: str, position_sol: float) -> Dict:
        """
        Check exit feasibility for a position size.
        Returns feasibility assessment.
        """
        # Estimate token amount from position
        # This is a rough estimate - would need price info for accuracy
        token_amount = int(position_sol * 1e9 * 1000)  # Rough estimate
        
        result = self.simulate_exit(mint, token_amount)
        
        return {
            "can_exit": result.can_exit,
            "slippage_bps": result.expected_slippage_bps,
            "route": result.route_source,
            "error": result.error,
            "confidence": 0.9 if result.route_available else 0.3,
        }


# ══════════════════════════════════════════════════════════════════════════════
# TOKEN RISK ANALYZER
# ══════════════════════════════════════════════════════════════════════════════

class TokenRiskAnalyzer:
    """
    Comprehensive token risk analysis combining:
    - Contract analysis
    - Holder distribution
    - Liquidity analysis
    - Deployer behavior
    """
    
    def __init__(self, helius_rpc: str, honeypot_detector: HoneypotDetector, exit_simulator: ExitSimulator):
        self.helius_rpc = helius_rpc
        self.honeypot_detector = honeypot_detector
        self.exit_simulator = exit_simulator
        self._lock = threading.RLock()
        self._cache: Dict[str, Tuple[TokenRiskAssessment, float]] = {}
        self._cache_ttl = 180  # 3 minute cache
        
    def analyze_token(
        self,
        mint: str,
        holders: Optional[Dict[str, int]] = None,
        total_supply: int = 0,
        liquidity_usd: float = 0,
        deployer_wallet: Optional[str] = None,
        token_age_sec: float = 0,
    ) -> TokenRiskAssessment:
        """
        Perform comprehensive risk analysis on a token.
        """
        # Check cache
        with self._lock:
            cached = self._cache.get(mint)
            if cached and time.time() - cached[1] < self._cache_ttl:
                return cached[0]
        
        assessment = TokenRiskAssessment(mint=mint)
        
        # 1. Honeypot check
        is_honeypot, hp_reasons = self.honeypot_detector.is_honeypot(mint)
        if is_honeypot:
            assessment.can_exit = False
            assessment.blockers.extend(hp_reasons)
            assessment.contract_risk = 100
        
        # 2. Exit simulation
        if not is_honeypot:
            exit_result = self.exit_simulator.simulate_exit(mint, int(0.01 * 1e9 * 1000))
            assessment.can_exit = exit_result.can_exit
            assessment.exit_confidence = 0.9 if exit_result.route_available else 0.3
            
            if not exit_result.can_exit:
                assessment.warnings.append(f"Exit simulation failed: {exit_result.error}")
                assessment.market_risk += 30
        
        # 3. Holder concentration risk
        if holders and total_supply > 0:
            top_10 = sorted(holders.values(), reverse=True)[:10]
            top_10_pct = sum(top_10) / total_supply * 100
            
            if top_10_pct > RISK_THRESHOLDS["max_holder_concentration_pct"]:
                assessment.warnings.append(f"High concentration: top 10 hold {top_10_pct:.0f}%")
                assessment.holder_risk = min(100, int(top_10_pct))
            else:
                assessment.holder_risk = int(top_10_pct / 2)
            
            # Check deployer holding
            if deployer_wallet and deployer_wallet in holders:
                deployer_pct = holders[deployer_wallet] / total_supply * 100
                if deployer_pct > RISK_THRESHOLDS["max_deployer_holding_pct"]:
                    assessment.warnings.append(f"Deployer holds {deployer_pct:.0f}%")
                    assessment.deployer_risk = min(100, int(deployer_pct * 2))
            
            # Check holder count
            if len(holders) < RISK_THRESHOLDS["min_holders"]:
                assessment.warnings.append(f"Low holder count: {len(holders)}")
                assessment.holder_risk += 20
        
        # 4. Liquidity risk
        assessment.liquidity_usd = liquidity_usd
        if liquidity_usd < RISK_THRESHOLDS["min_liquidity_usd"]:
            assessment.warnings.append(f"Low liquidity: ${liquidity_usd:,.0f}")
            assessment.liquidity_risk = min(100, int(100 - (liquidity_usd / 100)))
        else:
            assessment.liquidity_risk = max(0, int(50 - (liquidity_usd / 500)))
        
        # 5. Token age risk
        if token_age_sec < RISK_THRESHOLDS["min_token_age_sec"]:
            assessment.warnings.append(f"Very new token: {token_age_sec:.0f}s old")
            assessment.market_risk += 30
        
        # 6. Calculate overall risk score
        assessment.risk_score = self._calculate_risk_score(assessment)
        assessment.risk_level = self._determine_risk_level(assessment)
        
        # Update cache
        with self._lock:
            self._cache[mint] = (assessment, time.time())
        
        return assessment
    
    def _calculate_risk_score(self, assessment: TokenRiskAssessment) -> int:
        """Calculate weighted risk score 0-100."""
        weights = {
            "liquidity_risk": 0.25,
            "holder_risk": 0.20,
            "contract_risk": 0.25,
            "deployer_risk": 0.15,
            "market_risk": 0.15,
        }
        
        score = (
            assessment.liquidity_risk * weights["liquidity_risk"]
            + assessment.holder_risk * weights["holder_risk"]
            + assessment.contract_risk * weights["contract_risk"]
            + assessment.deployer_risk * weights["deployer_risk"]
            + assessment.market_risk * weights["market_risk"]
        )
        
        # Add penalty for blockers
        if assessment.blockers:
            score = max(score, 90)
        
        return min(100, int(score))
    
    def _determine_risk_level(self, assessment: TokenRiskAssessment) -> RiskLevel:
        """Determine risk level from assessment."""
        if assessment.blockers:
            return RiskLevel.BLOCKED
        if assessment.risk_score >= 80:
            return RiskLevel.CRITICAL
        if assessment.risk_score >= 60:
            return RiskLevel.HIGH
        if assessment.risk_score >= 40:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW
    
    def quick_check(self, mint: str) -> Tuple[bool, str]:
        """
        Quick pass/fail check for a token.
        Returns (is_safe, reason).
        """
        assessment = self.analyze_token(mint)
        
        if assessment.blockers:
            return False, f"Blocked: {assessment.blockers[0]}"
        if not assessment.can_exit:
            return False, "Cannot exit position"
        if assessment.risk_level == RiskLevel.CRITICAL:
            return False, f"Critical risk: {assessment.warnings[0] if assessment.warnings else 'high score'}"
        
        return True, "Passed risk checks"


# ══════════════════════════════════════════════════════════════════════════════
# CIRCUIT BREAKER
# ══════════════════════════════════════════════════════════════════════════════

class CircuitBreaker:
    """
    Circuit breaker to halt trading when conditions are unsafe:
    - Consecutive losses
    - High failure rate
    - Drawdown limits
    - RPC health issues
    """
    
    def __init__(self):
        self._lock = threading.RLock()
        self._states: Dict[int, CircuitBreakerState] = {}  # user_id -> state
        self._global_halt = False
        self._global_halt_reason: Optional[str] = None
        
    def get_state(self, user_id: int) -> CircuitBreakerState:
        """Get or create circuit breaker state for a user."""
        with self._lock:
            if user_id not in self._states:
                self._states[user_id] = CircuitBreakerState(user_id=user_id)
            return self._states[user_id]
    
    def is_trading_allowed(self, user_id: int) -> Tuple[bool, Optional[str]]:
        """Check if trading is allowed for a user."""
        if self._global_halt:
            return False, f"Global halt: {self._global_halt_reason}"
        
        state = self.get_state(user_id)
        if state.is_tripped:
            return False, f"Circuit breaker tripped: {state.trip_reason}"
        
        return True, None
    
    def record_trade_result(
        self,
        user_id: int,
        success: bool,
        pnl_pct: float = 0,
        is_exit_failure: bool = False,
    ):
        """Record a trade result and check circuit breaker conditions."""
        state = self.get_state(user_id)
        
        with self._lock:
            if success:
                state.consecutive_failures = 0
                if pnl_pct >= 0:
                    state.consecutive_losses = 0
                else:
                    state.consecutive_losses += 1
                    state.total_loss_pct += abs(pnl_pct)
            else:
                state.consecutive_failures += 1
                if is_exit_failure:
                    state.failed_exits += 1
            
            # Check trip conditions
            self._check_trip_conditions(state)
    
    def _check_trip_conditions(self, state: CircuitBreakerState):
        """Check if circuit breaker should trip."""
        # Consecutive failures
        if state.consecutive_failures >= 5:
            state.trip(f"5 consecutive transaction failures")
            return
        
        # Consecutive losses
        if state.consecutive_losses >= 4:
            state.trip(f"4 consecutive losses")
            return
        
        # Total drawdown
        if state.total_loss_pct >= RISK_THRESHOLDS["circuit_breaker_loss_pct"]:
            state.trip(f"Drawdown limit hit: {state.total_loss_pct:.0f}%")
            return
        
        # Failed exits
        if state.failed_exits >= RISK_THRESHOLDS["max_failed_exits_consecutive"]:
            state.trip(f"{state.failed_exits} consecutive failed exits")
            return
    
    def reset(self, user_id: int):
        """Reset circuit breaker for a user."""
        state = self.get_state(user_id)
        with self._lock:
            state.reset()
    
    def global_halt(self, reason: str):
        """Trigger global trading halt."""
        with self._lock:
            self._global_halt = True
            self._global_halt_reason = reason
    
    def global_resume(self):
        """Resume global trading."""
        with self._lock:
            self._global_halt = False
            self._global_halt_reason = None
    
    def health_check(self, user_id: int, rpc_fail_rate: float, rpc_latency_p95: float):
        """Perform health check and update state."""
        state = self.get_state(user_id)
        state.last_health_check = time.time()
        
        # Check RPC health
        if rpc_fail_rate > 0.5:
            state.trip(f"RPC failure rate too high: {rpc_fail_rate*100:.0f}%")
        elif rpc_latency_p95 > 5000:
            state.trip(f"RPC latency too high: {rpc_latency_p95:.0f}ms p95")


# ══════════════════════════════════════════════════════════════════════════════
# EXECUTION GUARDRAILS
# ══════════════════════════════════════════════════════════════════════════════

class ExecutionGuardrails:
    """
    Enforces execution safety limits:
    - Slippage limits
    - Fee limits
    - Position size limits
    - Frequency limits
    """
    
    def __init__(self):
        self._lock = threading.RLock()
        self._recent_trades: Dict[int, deque] = defaultdict(lambda: deque(maxlen=100))
        
    def validate_trade(
        self,
        user_id: int,
        mint: str,
        sol_amount: float,
        slippage_bps: int,
        priority_fee_sol: float,
        position_count: int,
        max_positions: int,
        liquidity_usd: float,
    ) -> Tuple[bool, List[str]]:
        """
        Validate a trade against guardrails.
        Returns (is_valid, reasons).
        """
        violations = []
        
        # Slippage check
        if slippage_bps > RISK_THRESHOLDS["max_slippage_bps"]:
            violations.append(f"Slippage {slippage_bps}bps exceeds max {RISK_THRESHOLDS['max_slippage_bps']}bps")
        
        # Fee check
        if priority_fee_sol > RISK_THRESHOLDS["max_priority_fee_sol"]:
            violations.append(f"Priority fee {priority_fee_sol:.4f} SOL exceeds max")
        
        # Position count check
        if position_count >= max_positions:
            violations.append(f"Max positions ({max_positions}) reached")
        
        # Liquidity check
        if liquidity_usd < RISK_THRESHOLDS["min_liquidity_usd"]:
            violations.append(f"Liquidity ${liquidity_usd:,.0f} below minimum ${RISK_THRESHOLDS['min_liquidity_usd']:,}")
        
        # Position size vs liquidity check
        position_pct_of_liquidity = (sol_amount * 150) / max(liquidity_usd, 1) * 100  # Rough SOL->USD
        if position_pct_of_liquidity > 5:
            violations.append(f"Position size {position_pct_of_liquidity:.1f}% of liquidity (max 5%)")
        
        # Rate limit check
        recent_count = self._count_recent_trades(user_id, window_sec=60)
        if recent_count >= 10:
            violations.append(f"Rate limit: {recent_count} trades in last minute")
        
        return len(violations) == 0, violations
    
    def record_trade(self, user_id: int, mint: str, sol_amount: float):
        """Record a trade for rate limiting."""
        with self._lock:
            self._recent_trades[user_id].append({
                "mint": mint,
                "sol": sol_amount,
                "ts": time.time(),
            })
    
    def _count_recent_trades(self, user_id: int, window_sec: int) -> int:
        """Count recent trades within window."""
        cutoff = time.time() - window_sec
        with self._lock:
            trades = self._recent_trades.get(user_id, deque())
            return sum(1 for t in trades if t["ts"] >= cutoff)
    
    def get_safe_slippage(self, liquidity_usd: float, sol_amount: float) -> int:
        """Calculate safe slippage based on liquidity and position size."""
        if liquidity_usd <= 0:
            return 500  # Default 5%
        
        # Position as % of liquidity
        position_pct = (sol_amount * 150) / liquidity_usd * 100
        
        # Scale slippage: small positions get tight slippage, large get more
        if position_pct < 0.5:
            return 200  # 2%
        elif position_pct < 1:
            return 300  # 3%
        elif position_pct < 2:
            return 500  # 5%
        elif position_pct < 5:
            return 800  # 8%
        else:
            return 1000  # 10%


# ══════════════════════════════════════════════════════════════════════════════
# RUG PULL DETECTOR
# ══════════════════════════════════════════════════════════════════════════════

class RugPullDetector:
    """
    Detects rug pull patterns:
    - LP removal events
    - Deployer selling
    - Coordinated exits
    - Liquidity drops
    """
    
    def __init__(self):
        self._lock = threading.RLock()
        self._lp_events: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        self._deployer_sells: Dict[str, deque] = defaultdict(lambda: deque(maxlen=50))
        self._detected_rugs: Set[str] = set()
        
    def record_lp_event(
        self,
        mint: str,
        wallet: str,
        event_type: str,  # "add" or "remove"
        sol_amount: float,
        lp_tokens: float,
        timestamp: float = None,
    ):
        """Record an LP add/remove event."""
        ts = timestamp or time.time()
        with self._lock:
            self._lp_events[mint].append({
                "wallet": wallet,
                "type": event_type,
                "sol": sol_amount,
                "lp_tokens": lp_tokens,
                "ts": ts,
            })
            
            # Check for rug pattern
            self._check_rug_pattern(mint)
    
    def record_deployer_sell(
        self,
        mint: str,
        deployer_wallet: str,
        sol_amount: float,
        pct_of_holdings: float,
        timestamp: float = None,
    ):
        """Record a deployer sell event."""
        ts = timestamp or time.time()
        with self._lock:
            self._deployer_sells[mint].append({
                "wallet": deployer_wallet,
                "sol": sol_amount,
                "pct": pct_of_holdings,
                "ts": ts,
            })
            
            # Check for rug pattern
            self._check_rug_pattern(mint)
    
    def _check_rug_pattern(self, mint: str):
        """Check if current events indicate a rug pull."""
        now = time.time()
        
        # Check LP removal pattern
        lp_events = list(self._lp_events.get(mint, deque()))
        recent_removes = [
            e for e in lp_events 
            if e["type"] == "remove" and now - e["ts"] < 300  # Last 5 minutes
        ]
        
        if recent_removes:
            total_removed = sum(e["sol"] for e in recent_removes)
            if total_removed >= 1.0:  # 1+ SOL removed
                self._detected_rugs.add(mint)
                return
        
        # Check deployer dump pattern
        sells = list(self._deployer_sells.get(mint, deque()))
        recent_sells = [s for s in sells if now - s["ts"] < 300]
        
        if recent_sells:
            total_pct = sum(s["pct"] for s in recent_sells)
            if total_pct >= 20:  # 20%+ dumped
                self._detected_rugs.add(mint)
                return
    
    def is_rugged(self, mint: str) -> bool:
        """Check if a token has been flagged as rugged."""
        with self._lock:
            return mint in self._detected_rugs
    
    def get_rug_signals(self, mint: str) -> List[str]:
        """Get rug pull warning signals for a token."""
        signals = []
        now = time.time()
        
        with self._lock:
            # LP removal signals
            lp_events = list(self._lp_events.get(mint, deque()))
            recent_removes = [e for e in lp_events if e["type"] == "remove" and now - e["ts"] < 600]
            if recent_removes:
                total = sum(e["sol"] for e in recent_removes)
                signals.append(f"LP removed: {total:.2f} SOL in last 10m")
            
            # Deployer sell signals
            sells = list(self._deployer_sells.get(mint, deque()))
            recent_sells = [s for s in sells if now - s["ts"] < 600]
            if recent_sells:
                total_pct = sum(s["pct"] for s in recent_sells)
                signals.append(f"Deployer sold: {total_pct:.0f}% in last 10m")
        
        return signals


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRATED RISK ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class RiskEngine:
    """
    Integrated risk management engine combining all components.
    Main interface for the trading bot.
    """
    
    def __init__(self, helius_rpc: str):
        self.helius_rpc = helius_rpc
        self.honeypot_detector = HoneypotDetector(helius_rpc)
        self.exit_simulator = ExitSimulator(helius_rpc)
        self.token_analyzer = TokenRiskAnalyzer(helius_rpc, self.honeypot_detector, self.exit_simulator)
        self.circuit_breaker = CircuitBreaker()
        self.guardrails = ExecutionGuardrails()
        self.rug_detector = RugPullDetector()
        
        self._lock = threading.RLock()
        self._risk_cache: Dict[str, TokenRiskAssessment] = {}
        
    def pre_trade_check(
        self,
        user_id: int,
        mint: str,
        sol_amount: float,
        slippage_bps: int,
        priority_fee_sol: float,
        position_count: int,
        max_positions: int,
        liquidity_usd: float,
        holders: Optional[Dict[str, int]] = None,
        total_supply: int = 0,
        deployer_wallet: Optional[str] = None,
        token_age_sec: float = 0,
    ) -> Tuple[bool, List[str], TokenRiskAssessment]:
        """
        Comprehensive pre-trade risk check.
        Returns (is_approved, reasons, risk_assessment).
        """
        reasons = []
        
        # 1. Check circuit breaker
        can_trade, cb_reason = self.circuit_breaker.is_trading_allowed(user_id)
        if not can_trade:
            reasons.append(cb_reason)
            return False, reasons, TokenRiskAssessment(mint=mint, risk_level=RiskLevel.BLOCKED)
        
        # 2. Check if rugged
        if self.rug_detector.is_rugged(mint):
            reasons.append("Token flagged as rugged")
            return False, reasons, TokenRiskAssessment(mint=mint, risk_level=RiskLevel.BLOCKED)
        
        # 3. Token risk analysis
        assessment = self.token_analyzer.analyze_token(
            mint=mint,
            holders=holders,
            total_supply=total_supply,
            liquidity_usd=liquidity_usd,
            deployer_wallet=deployer_wallet,
            token_age_sec=token_age_sec,
        )
        
        if assessment.blockers:
            reasons.extend(assessment.blockers)
            return False, reasons, assessment
        
        if not assessment.can_exit:
            assessment.warnings.append("Exit simulation failed - cannot sell")
        
        if assessment.risk_level == RiskLevel.CRITICAL:
            reasons.append(f"Critical risk level (score: {assessment.risk_score})")
            return False, reasons, assessment
        
        # 4. Execution guardrails
        guardrail_ok, guardrail_reasons = self.guardrails.validate_trade(
            user_id=user_id,
            mint=mint,
            sol_amount=sol_amount,
            slippage_bps=slippage_bps,
            priority_fee_sol=priority_fee_sol,
            position_count=position_count,
            max_positions=max_positions,
            liquidity_usd=liquidity_usd,
        )
        
        if not guardrail_ok:
            reasons.extend(guardrail_reasons)
            return False, reasons, assessment
        
        # All checks passed
        if assessment.warnings:
            reasons.extend([f"Warning: {w}" for w in assessment.warnings[:3]])
        
        return True, reasons, assessment
    
    def post_trade_update(
        self,
        user_id: int,
        mint: str,
        sol_amount: float,
        success: bool,
        pnl_pct: float = 0,
        is_exit: bool = False,
        exit_failed: bool = False,
    ):
        """Update risk state after a trade."""
        # Record for guardrails
        if success:
            self.guardrails.record_trade(user_id, mint, sol_amount)
        
        # Update circuit breaker
        self.circuit_breaker.record_trade_result(
            user_id=user_id,
            success=success,
            pnl_pct=pnl_pct,
            is_exit_failure=exit_failed,
        )
    
    def monitor_position(
        self,
        mint: str,
        deployer_wallet: Optional[str] = None,
    ) -> List[str]:
        """
        Monitor an open position for risk signals.
        Returns list of warning signals.
        """
        signals = []
        
        # Check rug signals
        rug_signals = self.rug_detector.get_rug_signals(mint)
        signals.extend(rug_signals)
        
        # Check if rugged
        if self.rug_detector.is_rugged(mint):
            signals.insert(0, "⚠️ RUG DETECTED - IMMEDIATE EXIT RECOMMENDED")
        
        return signals
    
    def record_lp_event(
        self,
        mint: str,
        wallet: str,
        event_type: str,
        sol_amount: float,
        lp_tokens: float,
    ):
        """Record LP event for rug detection."""
        self.rug_detector.record_lp_event(mint, wallet, event_type, sol_amount, lp_tokens)
    
    def record_deployer_sell(
        self,
        mint: str,
        deployer_wallet: str,
        sol_amount: float,
        pct_of_holdings: float,
    ):
        """Record deployer sell for rug detection."""
        self.rug_detector.record_deployer_sell(mint, deployer_wallet, sol_amount, pct_of_holdings)
    
    def get_safe_slippage(self, liquidity_usd: float, sol_amount: float) -> int:
        """Get recommended safe slippage for a trade."""
        return self.guardrails.get_safe_slippage(liquidity_usd, sol_amount)
    
    def health_check(self, user_id: int, rpc_fail_rate: float, rpc_latency_p95: float):
        """Perform system health check."""
        self.circuit_breaker.health_check(user_id, rpc_fail_rate, rpc_latency_p95)
    
    def reset_circuit_breaker(self, user_id: int):
        """Reset circuit breaker for a user."""
        self.circuit_breaker.reset(user_id)
    
    def get_circuit_breaker_state(self, user_id: int) -> Dict:
        """Get circuit breaker state for a user."""
        return self.circuit_breaker.get_state(user_id).to_dict()


# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON INSTANCE
# ══════════════════════════════════════════════════════════════════════════════

_risk_engine: Optional[RiskEngine] = None
_risk_engine_lock = threading.Lock()

def get_risk_engine(helius_rpc: str = None) -> RiskEngine:
    """Get the singleton risk engine instance."""
    global _risk_engine
    with _risk_engine_lock:
        if _risk_engine is None:
            if not helius_rpc:
                raise ValueError("helius_rpc required for first initialization")
            _risk_engine = RiskEngine(helius_rpc)
        return _risk_engine
