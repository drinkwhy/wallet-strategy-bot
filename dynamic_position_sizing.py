"""
Dynamic Position Sizing Module
Scales positions 0.02-0.1 SOL based on signal confidence + token risk
Risk-adjusted by account limits (max 2% per trade)
Kelly Criterion-inspired scaling
"""

import math
from dataclasses import dataclass
from typing import Optional, Tuple
from enum import Enum

class RiskLevel(Enum):
    VERY_LOW = (0.5, 1.5)      # Whales, high liquidity
    LOW = (1.5, 3.0)           # Balanced, moderate whales
    MEDIUM = (3.0, 7.0)        # Some whale concentration
    HIGH = (7.0, 15.0)         # High concentration risk
    VERY_HIGH = (15.0, 100.0)  # Extreme whale dominance

@dataclass
class TokenRisk:
    """Token-level risk metrics"""
    holder_concentration_pct: float  # Top 5 holders % of supply
    liquidity_usd: float
    market_cap_usd: float
    age_minutes: int
    volume_24h_usd: float
    price_change_pct: float
    volatility_score: float  # 0-100, higher = more volatile
    
    def get_risk_level(self) -> RiskLevel:
        """Categorize token risk"""
        if self.holder_concentration_pct > 70:
            return RiskLevel.VERY_HIGH
        elif self.holder_concentration_pct > 50:
            return RiskLevel.HIGH
        elif self.holder_concentration_pct > 35:
            return RiskLevel.MEDIUM
        elif self.holder_concentration_pct > 15:
            return RiskLevel.LOW
        else:
            return RiskLevel.VERY_LOW
    
    def get_risk_multiplier(self) -> float:
        """Return position size multiplier based on risk"""
        risk_level = self.get_risk_level()
        # Use midpoint of range
        lower, upper = risk_level.value
        midpoint = (lower + upper) / 2
        # Invert: higher concentration = lower multiplier
        return max(0.3, 1.0 - (midpoint / 100))


@dataclass
class PositionSizingResult:
    position_size_sol: float
    risk_adjusted_size_sol: float
    kelly_fraction: float
    kelly_optimal_size_sol: float
    confidence_multiplier: float
    max_position_sol: float
    account_risk_pct: float
    reasoning: str


class DynamicPositionSizer:
    """
    Sizes trading positions using Kelly Criterion + account risk limits
    """
    
    MIN_POSITION_SOL = 0.02
    MAX_POSITION_SOL = 0.1
    MAX_RISK_PER_TRADE_PCT = 2.0  # Account risk limit
    
    def __init__(
        self,
        account_balance_sol: float,
        win_rate: Optional[float] = None,
        avg_win_loss_ratio: Optional[float] = None,
        use_half_kelly: bool = True,  # Safer than full Kelly
    ):
        """
        Args:
            account_balance_sol: Current SOL balance
            win_rate: Historical win % (0-1). If None, defaults to conservative 0.45
            avg_win_loss_ratio: Avg winning trade / Avg losing trade. If None, defaults to 1.0
            use_half_kelly: Use f* / 2 (safer than full Kelly)
        """
        self.account_balance_sol = account_balance_sol
        self.win_rate = win_rate or 0.45
        self.avg_win_loss_ratio = avg_win_loss_ratio or 1.0
        self.use_half_kelly = use_half_kelly
        
    def calculate_kelly_fraction(self) -> float:
        """
        Kelly Criterion: f* = (edge * odds - (1 - edge)) / odds
        
        Where:
        - edge = win_rate (probability of winning)
        - odds = avg_win / avg_loss (reward / risk ratio)
        
        Returns: Optimal fraction of bankroll to risk (0-1)
        """
        if self.win_rate <= 0 or self.win_rate >= 1:
            return 0.0  # Can't calculate Kelly with edge cases
        
        edge = self.win_rate
        odds = self.avg_win_loss_ratio
        
        if odds <= 0:
            return 0.0
        
        # Kelly formula
        kelly_fraction = (edge * odds - (1 - edge)) / odds
        kelly_fraction = max(0, min(kelly_fraction, 0.25))  # Clamp to 0-25%
        
        # Use half-Kelly for safety
        if self.use_half_kelly:
            kelly_fraction = kelly_fraction / 2
        
        return kelly_fraction
    
    def size_position(
        self,
        signal_confidence: float,  # 0-1, from signal_enhancement.py
        token_risk: TokenRisk,
        estimated_stop_loss_pct: float = 2.0,  # Default stop loss
    ) -> PositionSizingResult:
        """
        Calculate optimal position size combining Kelly + confidence + risk
        
        Args:
            signal_confidence: Signal quality (0-1)
            token_risk: Token risk metrics
            estimated_stop_loss_pct: Expected loss if stop triggered
        
        Returns:
            PositionSizingResult with sizing details
        """
        # Validate inputs
        signal_confidence = max(0, min(signal_confidence, 1.0))
        
        if self.account_balance_sol <= 0:
            return PositionSizingResult(
                position_size_sol=0.0,
                risk_adjusted_size_sol=0.0,
                kelly_fraction=0.0,
                kelly_optimal_size_sol=0.0,
                confidence_multiplier=signal_confidence,
                max_position_sol=0.0,
                account_risk_pct=0.0,
                reasoning="Invalid account balance"
            )
        
        # 1. Calculate Kelly Criterion
        kelly_frac = self.calculate_kelly_fraction()
        kelly_optimal_sol = kelly_frac * self.account_balance_sol
        
        # 2. Apply signal confidence multiplier
        # Higher confidence = bigger position
        # Confidence range: 0.3 (low) to 1.0 (high)
        confidence_mult = 0.3 + (signal_confidence * 0.7)
        
        # 3. Apply risk-based multiplier
        risk_mult = token_risk.get_risk_multiplier()
        
        # 4. Combine: Kelly * Confidence * Risk
        combined_size_sol = kelly_optimal_sol * confidence_mult * risk_mult
        
        # 5. Clamp to portfolio limits
        max_position_by_kelly = min(self.MAX_POSITION_SOL, kelly_optimal_sol * 2)
        clamped_size_sol = max(
            self.MIN_POSITION_SOL,
            min(combined_size_sol, max_position_by_kelly)
        )
        
        # 6. Apply account risk limit (2% max)
        max_risk_sol = self.account_balance_sol * (self.MAX_RISK_PER_TRADE_PCT / 100)
        risk_adjusted_size = clamped_size_sol
        
        # Calculate actual account risk %
        account_risk_pct = (clamped_size_sol * estimated_stop_loss_pct / 100) / self.account_balance_sol * 100
        
        if account_risk_pct > self.MAX_RISK_PER_TRADE_PCT:
            # Scale down to meet account risk limit
            scale_factor = self.MAX_RISK_PER_TRADE_PCT / account_risk_pct
            risk_adjusted_size = clamped_size_sol * scale_factor
            account_risk_pct = self.MAX_RISK_PER_TRADE_PCT
        
        # Final bounds check
        final_size_sol = max(
            self.MIN_POSITION_SOL,
            min(risk_adjusted_size, self.MAX_POSITION_SOL)
        )
        
        # Generate reasoning string
        reasoning = (
            f"Kelly={kelly_frac:.2%} | "
            f"Confidence={confidence_mult:.2f}x | "
            f"Risk={risk_mult:.2f}x | "
            f"RiskLevel={token_risk.get_risk_level().name} | "
            f"AccountRisk={account_risk_pct:.2f}%"
        )
        
        return PositionSizingResult(
            position_size_sol=clamped_size_sol,
            risk_adjusted_size_sol=final_size_sol,
            kelly_fraction=kelly_frac,
            kelly_optimal_size_sol=kelly_optimal_sol,
            confidence_multiplier=confidence_mult,
            max_position_sol=max_position_by_kelly,
            account_risk_pct=account_risk_pct,
            reasoning=reasoning
        )
    
    def batch_size_positions(
        self,
        positions: list[Tuple[float, TokenRisk]],  # (confidence, risk)
    ) -> list[PositionSizingResult]:
        """Size multiple positions, respecting max correlated exposure"""
        results = []
        total_correlated_sol = 0.0
        max_correlated_sol = self.account_balance_sol * 0.3  # Max 30% correlated exposure
        
        for confidence, token_risk in positions:
            result = self.size_position(confidence, token_risk)
            
            # Check if correlated (same sector, similar pattern)
            # For now, simple cap: ensure total doesn't exceed limit
            if total_correlated_sol + result.risk_adjusted_size_sol > max_correlated_sol:
                # Scale down
                available_sol = max_correlated_sol - total_correlated_sol
                if available_sol < self.MIN_POSITION_SOL:
                    result.risk_adjusted_size_sol = 0.0
                    result.reasoning += " | SKIPPED: Correlated exposure limit"
                else:
                    result.risk_adjusted_size_sol = available_sol
                    result.reasoning += f" | SCALED: Correlated limit (${available_sol:.4f})"
            
            total_correlated_sol += result.risk_adjusted_size_sol
            results.append(result)
        
        return results


# Example usage
if __name__ == "__main__":
    # Test with example account
    sizer = DynamicPositionSizer(
        account_balance_sol=1.5,
        win_rate=0.52,
        avg_win_loss_ratio=1.2,
    )
    
    # Low-risk token
    safe_token = TokenRisk(
        holder_concentration_pct=8.0,
        liquidity_usd=150000,
        market_cap_usd=2000000,
        age_minutes=720,
        volume_24h_usd=500000,
        price_change_pct=+15,
        volatility_score=35,
    )
    
    result = sizer.size_position(signal_confidence=0.75, token_risk=safe_token)
    print(f"Safe token position: {result.risk_adjusted_size_sol:.4f} SOL")
    print(f"Reasoning: {result.reasoning}")
    print()
    
    # High-risk token
    risky_token = TokenRisk(
        holder_concentration_pct=65.0,
        liquidity_usd=25000,
        market_cap_usd=150000,
        age_minutes=45,
        volume_24h_usd=50000,
        price_change_pct=+85,
        volatility_score=92,
    )
    
    result = sizer.size_position(signal_confidence=0.65, token_risk=risky_token)
    print(f"Risky token position: {result.risk_adjusted_size_sol:.4f} SOL")
    print(f"Reasoning: {result.reasoning}")
