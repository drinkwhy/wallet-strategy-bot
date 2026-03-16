"""
Enhanced Trading Integration — SolTrader
==========================================
Integrates all research paper improvements:
- Whale detection pipeline
- Risk engine with circuit breakers
- MEV protection
- Observability and metrics

This module provides the EnhancedBotInstance class that extends
the original BotInstance with all new capabilities.
"""

import time
import threading
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass

# Import enhanced modules
from whale_detection import (
    get_whale_detection_system,
    WhaleDetectionSystem,
    WhaleAction,
    WhaleScore,
)
from risk_engine import (
    get_risk_engine,
    RiskEngine,
    TokenRiskAssessment,
    RiskLevel,
)
from mev_protection import (
    get_mev_protection,
    MEVProtectionSystem,
    SubmissionResult,
    SubmissionStrategy,
)
from observability import (
    get_observability,
    ObservabilitySystem,
    AlertSeverity,
)


# ══════════════════════════════════════════════════════════════════════════════
# ENHANCED SIGNAL EVALUATOR
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class EnhancedSignalResult:
    """Result of enhanced signal evaluation."""
    should_buy: bool
    score: int
    risk_level: RiskLevel
    whale_score: float
    reasons: List[str]
    warnings: List[str]
    blockers: List[str]
    whale_triggers: List[str]
    
    def to_dict(self) -> dict:
        return {
            "should_buy": self.should_buy,
            "score": self.score,
            "risk_level": self.risk_level.value,
            "whale_score": round(self.whale_score, 1),
            "reasons": self.reasons,
            "warnings": self.warnings,
            "blockers": self.blockers,
            "whale_triggers": self.whale_triggers,
        }


class EnhancedSignalEvaluator:
    """
    Enhanced signal evaluation combining:
    - Original AI scoring
    - Whale detection signals
    - Risk assessment
    - Smart money tracking
    """
    
    def __init__(
        self,
        whale_system: WhaleDetectionSystem,
        risk_engine: RiskEngine,
        observability: ObservabilitySystem,
    ):
        self.whale_system = whale_system
        self.risk_engine = risk_engine
        self.observability = observability
    
    def evaluate(
        self,
        user_id: int,
        mint: str,
        name: str,
        price: float,
        mc: float,
        vol: float,
        liq: float,
        age_min: float,
        change: float,
        base_score: int,
        settings: Dict,
        position_count: int,
        max_positions: int,
        holders: Dict[str, int] = None,
        total_supply: int = 0,
        deployer_wallet: str = None,
    ) -> EnhancedSignalResult:
        """Perform enhanced signal evaluation."""
        reasons = []
        warnings = []
        blockers = []
        whale_triggers = []
        
        # Get whale activity
        whale_entry_signal, whale_reasons = self.whale_system.check_whale_entry_signal(
            mint, 
            min_score=settings.get("min_whale_score", 60)
        )
        whale_triggers.extend(whale_reasons)
        
        if whale_entry_signal:
            reasons.append("Whale accumulation detected")
        
        # Get token whale activity metrics
        whale_activity = self.whale_system.get_token_whale_activity(mint)
        whale_score = whale_activity.get("flow_metrics", {}).get("accumulation_score", 0)
        
        # Pre-trade risk check
        trade_sol = float(settings.get("max_buy_sol", 0.05))
        slippage_bps = int(settings.get("slippage_bps", 500))
        priority_fee = float(settings.get("priority_fee", 0.0001))
        
        risk_approved, risk_reasons, risk_assessment = self.risk_engine.pre_trade_check(
            user_id=user_id,
            mint=mint,
            sol_amount=trade_sol,
            slippage_bps=slippage_bps,
            priority_fee_sol=priority_fee,
            position_count=position_count,
            max_positions=max_positions,
            liquidity_usd=liq,
            holders=holders,
            total_supply=total_supply,
            deployer_wallet=deployer_wallet,
            token_age_sec=age_min * 60,
        )
        
        # Sort risk reasons into warnings and blockers
        for reason in risk_reasons:
            if reason.startswith("Warning:"):
                warnings.append(reason.replace("Warning: ", ""))
            elif "Blocked" in reason or "Cannot" in reason or "Critical" in reason:
                blockers.append(reason)
            else:
                warnings.append(reason)
        
        # Calculate enhanced score
        enhanced_score = base_score
        
        # Whale signal bonus (+10-20 points)
        if whale_entry_signal:
            enhanced_score += min(20, int(whale_score * 10))
        
        # Risk penalty
        if risk_assessment.risk_level == RiskLevel.HIGH:
            enhanced_score -= 15
        elif risk_assessment.risk_level == RiskLevel.MEDIUM:
            enhanced_score -= 5
        
        # Smart money bonus
        if "smart money" in " ".join(whale_triggers).lower():
            enhanced_score += 10
            reasons.append("Smart money buy detected")
        
        # Determine final decision
        should_buy = (
            risk_approved
            and enhanced_score >= settings.get("min_score", 30)
            and len(blockers) == 0
        )
        
        if should_buy:
            reasons.append(f"Enhanced score {enhanced_score} meets threshold")
        elif not risk_approved:
            reasons.append("Failed risk checks")
        elif blockers:
            reasons.append(f"Blocked: {blockers[0]}")
        else:
            reasons.append(f"Score {enhanced_score} below threshold")
        
        return EnhancedSignalResult(
            should_buy=should_buy,
            score=enhanced_score,
            risk_level=risk_assessment.risk_level,
            whale_score=whale_score,
            reasons=reasons,
            warnings=warnings,
            blockers=blockers,
            whale_triggers=whale_triggers,
        )


# ══════════════════════════════════════════════════════════════════════════════
# ENHANCED EXECUTION HANDLER
# ══════════════════════════════════════════════════════════════════════════════

class EnhancedExecutionHandler:
    """
    Enhanced trade execution with:
    - MEV protection
    - Dynamic slippage
    - Observability integration
    """
    
    def __init__(
        self,
        mev_system: MEVProtectionSystem,
        risk_engine: RiskEngine,
        observability: ObservabilitySystem,
    ):
        self.mev = mev_system
        self.risk = risk_engine
        self.obs = observability
    
    def execute_buy(
        self,
        user_id: int,
        mint: str,
        name: str,
        sol_amount: float,
        liquidity_usd: float,
        build_swap_tx: callable,  # Function that builds the swap transaction
        sign_tx: callable,        # Function that signs the transaction
    ) -> Tuple[bool, Optional[str], Dict]:
        """
        Execute a buy with MEV protection.
        
        Args:
            build_swap_tx: Function(slippage_bps) -> bytes or None
            sign_tx: Function(tx_bytes) -> signed_bytes
        """
        start_time = time.perf_counter()
        result_meta = {
            "strategy": None,
            "slippage_bps": 0,
            "tip_lamports": 0,
            "latency_ms": 0,
        }
        
        # Get optimal slippage
        slippage_bps = self.risk.get_safe_slippage(liquidity_usd, sol_amount)
        result_meta["slippage_bps"] = slippage_bps
        
        # Build swap transaction
        swap_tx = build_swap_tx(slippage_bps)
        if not swap_tx:
            return False, None, {"error": "Failed to build swap transaction", **result_meta}
        
        # Sign transaction
        try:
            signed_tx = sign_tx(swap_tx)
        except Exception as e:
            return False, None, {"error": f"Failed to sign: {e}", **result_meta}
        
        # Determine urgency (larger trades = higher urgency)
        urgency = "normal"
        if sol_amount >= 1.0:
            urgency = "high"
        elif sol_amount >= 0.5:
            urgency = "normal"
        else:
            urgency = "low"
        
        # Submit with MEV protection
        submission = self.mev.submit_trade(
            signed_tx=signed_tx,
            trade_size_sol=sol_amount,
            is_buy=True,
            urgency=urgency,
        )
        
        result_meta["strategy"] = submission.strategy_used.value
        result_meta["tip_lamports"] = submission.tip_lamports
        result_meta["latency_ms"] = submission.latency_ms
        
        # Record trade
        total_latency = time.perf_counter() - start_time
        self.obs.record_trade(
            user_id=user_id,
            mint=mint,
            side="buy",
            sol_amount=sol_amount,
            price=0,  # Will be updated after confirmation
            latency_sec=total_latency,
            slippage_bps=slippage_bps,
            success=submission.success,
        )
        
        # Update risk engine
        self.risk.post_trade_update(
            user_id=user_id,
            mint=mint,
            sol_amount=sol_amount,
            success=submission.success,
        )
        
        if submission.success:
            return True, submission.signature, result_meta
        else:
            return False, None, {"error": submission.error, **result_meta}
    
    def execute_sell(
        self,
        user_id: int,
        mint: str,
        name: str,
        token_amount: int,
        entry_sol: float,
        pnl_pct: float,
        liquidity_usd: float,
        build_swap_tx: callable,
        sign_tx: callable,
    ) -> Tuple[bool, Optional[str], Dict]:
        """Execute a sell with appropriate urgency."""
        start_time = time.perf_counter()
        result_meta = {
            "strategy": None,
            "slippage_bps": 0,
            "latency_ms": 0,
        }
        
        # Higher slippage tolerance for sells (need to exit)
        base_slippage = self.risk.get_safe_slippage(liquidity_usd, entry_sol)
        slippage_bps = int(base_slippage * 1.5)  # 50% more tolerance for sells
        result_meta["slippage_bps"] = slippage_bps
        
        # Build swap
        swap_tx = build_swap_tx(slippage_bps)
        if not swap_tx:
            # Record failed exit
            self.risk.post_trade_update(
                user_id=user_id,
                mint=mint,
                sol_amount=entry_sol,
                success=False,
                is_exit=True,
                exit_failed=True,
            )
            return False, None, {"error": "Failed to build sell transaction", **result_meta}
        
        # Sign
        try:
            signed_tx = sign_tx(swap_tx)
        except Exception as e:
            return False, None, {"error": f"Failed to sign: {e}", **result_meta}
        
        # Higher urgency for sells (exit is important)
        urgency = "high" if pnl_pct < 0 else "normal"
        
        # Submit (sells use standard submission - less MEV sensitive)
        submission = self.mev.submit_trade(
            signed_tx=signed_tx,
            trade_size_sol=entry_sol,
            is_buy=False,
            urgency=urgency,
        )
        
        result_meta["strategy"] = submission.strategy_used.value
        result_meta["latency_ms"] = submission.latency_ms
        
        # Calculate PnL
        pnl_sol = entry_sol * (pnl_pct / 100)
        
        # Record trade
        total_latency = time.perf_counter() - start_time
        self.obs.record_trade(
            user_id=user_id,
            mint=mint,
            side="sell",
            sol_amount=entry_sol,
            price=0,
            pnl_sol=pnl_sol,
            pnl_pct=pnl_pct,
            latency_sec=total_latency,
            slippage_bps=slippage_bps,
            success=submission.success,
        )
        
        # Update risk engine
        self.risk.post_trade_update(
            user_id=user_id,
            mint=mint,
            sol_amount=entry_sol,
            success=submission.success,
            pnl_pct=pnl_pct,
            is_exit=True,
            exit_failed=not submission.success,
        )
        
        if submission.success:
            return True, submission.signature, result_meta
        else:
            return False, None, {"error": submission.error, **result_meta}


# ══════════════════════════════════════════════════════════════════════════════
# ENHANCED POSITION MONITOR
# ══════════════════════════════════════════════════════════════════════════════

class EnhancedPositionMonitor:
    """
    Enhanced position monitoring with:
    - Whale exit warnings
    - Rug pull detection
    - Dynamic risk updates
    """
    
    def __init__(
        self,
        whale_system: WhaleDetectionSystem,
        risk_engine: RiskEngine,
        observability: ObservabilitySystem,
    ):
        self.whale = whale_system
        self.risk = risk_engine
        self.obs = observability
    
    def check_position(
        self,
        mint: str,
        entry_price: float,
        current_price: float,
        entry_sol: float,
        deployer_wallet: str = None,
    ) -> Tuple[bool, str, List[str]]:
        """
        Check position for exit signals.
        
        Returns:
            (should_exit, reason, warnings)
        """
        warnings = []
        
        # Check whale exit signals
        whale_exit, whale_reasons = self.whale.check_whale_exit_warning(mint)
        if whale_exit:
            warnings.extend(whale_reasons)
        
        # Check rug signals
        rug_signals = self.risk.monitor_position(mint, deployer_wallet)
        if rug_signals:
            warnings.extend(rug_signals)
            
            # If rug detected, immediate exit
            if any("RUG" in s for s in rug_signals):
                return True, "RUG_DETECTED", warnings
        
        # Strong whale exit signal
        if whale_exit and len(whale_reasons) >= 2:
            return True, "WHALE_EXIT", warnings
        
        return False, "", warnings
    
    def process_market_event(
        self,
        mint: str,
        event_type: str,
        wallet: str,
        sol_amount: float,
        **kwargs
    ):
        """Process market events for monitoring."""
        if event_type == "lp_remove":
            self.risk.record_lp_event(
                mint=mint,
                wallet=wallet,
                event_type="remove",
                sol_amount=sol_amount,
                lp_tokens=kwargs.get("lp_tokens", 0),
            )
        elif event_type == "deployer_sell":
            self.risk.record_deployer_sell(
                mint=mint,
                deployer_wallet=wallet,
                sol_amount=sol_amount,
                pct_of_holdings=kwargs.get("pct_holdings", 0),
            )


# ══════════════════════════════════════════════════════════════════════════════
# ENHANCED BOT MIXIN
# ══════════════════════════════════════════════════════════════════════════════

class EnhancedBotMixin:
    """
    Mixin class that adds enhanced capabilities to BotInstance.
    
    Usage:
        class EnhancedBotInstance(BotInstance, EnhancedBotMixin):
            pass
    """
    
    def init_enhanced_systems(self, helius_rpc: str):
        """Initialize enhanced systems. Call this in __init__."""
        self.whale_system = get_whale_detection_system()
        self.risk_engine = get_risk_engine(helius_rpc)
        self.mev_system = get_mev_protection(helius_rpc)
        self.observability = get_observability()
        
        self.signal_evaluator = EnhancedSignalEvaluator(
            self.whale_system,
            self.risk_engine,
            self.observability,
        )
        
        self.execution_handler = EnhancedExecutionHandler(
            self.mev_system,
            self.risk_engine,
            self.observability,
        )
        
        self.position_monitor = EnhancedPositionMonitor(
            self.whale_system,
            self.risk_engine,
            self.observability,
        )
        
        # Register alert callback
        self.observability.register_alert_callback(self._handle_alert)
    
    def _handle_alert(self, alert):
        """Handle alerts from observability system."""
        if hasattr(self, 'log_msg'):
            severity_emoji = {
                AlertSeverity.INFO: "ℹ️",
                AlertSeverity.WARNING: "⚠️",
                AlertSeverity.CRITICAL: "🚨",
            }
            emoji = severity_emoji.get(alert.severity, "📢")
            self.log_msg(f"{emoji} {alert.message}")
    
    def enhanced_evaluate_signal(
        self,
        mint: str,
        name: str,
        price: float,
        mc: float,
        vol: float,
        liq: float,
        age_min: float,
        change: float,
        base_score: int,
    ) -> EnhancedSignalResult:
        """Enhanced signal evaluation with whale + risk integration."""
        return self.signal_evaluator.evaluate(
            user_id=self.user_id,
            mint=mint,
            name=name,
            price=price,
            mc=mc,
            vol=vol,
            liq=liq,
            age_min=age_min,
            change=change,
            base_score=base_score,
            settings=self.settings,
            position_count=len(self.positions),
            max_positions=int(self.settings.get("max_correlated", 3)),
        )
    
    def enhanced_check_position(self, mint: str) -> Tuple[bool, str, List[str]]:
        """Enhanced position check with whale + rug detection."""
        pos = self.positions.get(mint)
        if not pos:
            return False, "", []
        
        current_price = self.get_token_price(mint) or pos["entry_price"]
        
        return self.position_monitor.check_position(
            mint=mint,
            entry_price=pos["entry_price"],
            current_price=current_price,
            entry_sol=pos.get("entry_sol", 0),
            deployer_wallet=pos.get("dev_wallet"),
        )
    
    def get_enhanced_dashboard_data(self) -> Dict:
        """Get enhanced dashboard data."""
        return {
            "trading_stats": self.observability.trading_metrics.get_user_stats(self.user_id),
            "system_health": self.observability.system_metrics.get_health_summary(),
            "active_alerts": [a.to_dict() for a in self.observability.alert_manager.get_active_alerts()],
            "mev_stats": self.mev_system.get_health_report(),
            "circuit_breaker": self.risk_engine.get_circuit_breaker_state(self.user_id),
        }
    
    def process_swap_event(
        self,
        wallet: str,
        mint: str,
        sol_amount: float,
        token_amount: float,
        is_buy: bool,
        liquidity_usd: float = 0,
        pool_reserves_sol: float = 0,
        total_supply: int = 0,
        price: float = 0,
    ):
        """Process swap events for whale detection."""
        return self.whale_system.process_swap_event(
            wallet=wallet,
            mint=mint,
            sol_amount=sol_amount,
            token_amount=token_amount,
            is_buy=is_buy,
            liquidity_usd=liquidity_usd,
            pool_reserves_sol=pool_reserves_sol,
            total_supply=total_supply,
            price=price,
        )


# ══════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def create_enhanced_systems(helius_rpc: str) -> Dict:
    """Create and return all enhanced system instances."""
    whale = get_whale_detection_system()
    risk = get_risk_engine(helius_rpc)
    mev = get_mev_protection(helius_rpc)
    obs = get_observability()
    
    return {
        "whale_system": whale,
        "risk_engine": risk,
        "mev_system": mev,
        "observability": obs,
    }


def get_whale_activity_for_token(mint: str) -> Dict:
    """Get whale activity metrics for a token."""
    whale = get_whale_detection_system()
    return whale.get_token_whale_activity(mint)


def check_token_risk(mint: str, helius_rpc: str, **kwargs) -> TokenRiskAssessment:
    """Quick token risk check."""
    risk = get_risk_engine(helius_rpc)
    return risk.token_analyzer.analyze_token(mint, **kwargs)


def get_submission_stats() -> Dict:
    """Get MEV submission statistics."""
    try:
        mev = get_mev_protection()
        return mev.get_health_report()
    except Exception:
        return {}


def get_observability_metrics() -> Dict:
    """Get all observability metrics."""
    obs = get_observability()
    return obs.get_metrics_export()
