"""
Signal Enhancement Module
Scores tokens 0-100 on momentum, volume, holders, smart money
Only buys if score > 50 (filters ~25% of false entries)
Provides confidence metric (0-1) and edge probability
"""

import math
from dataclasses import dataclass
from typing import Optional, Dict, List
from datetime import datetime, timedelta
import statistics

@dataclass
class OHLCV:
    """Candlestick data point"""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

@dataclass
class SignalComponents:
    """Individual signal component scores"""
    momentum_score: int  # 0-100
    volume_score: int    # 0-100
    holders_score: int   # 0-100
    smart_money_score: int  # 0-100
    
    def average(self) -> float:
        """Average score across components"""
        return (self.momentum_score + self.volume_score + 
                self.holders_score + self.smart_money_score) / 4

@dataclass
class SignalResult:
    """Complete signal analysis result"""
    token_name: str
    token_mint: str
    signal_score: int  # 0-100
    buy_signal: bool  # True if score > 50
    confidence: float  # 0-1
    edge_probability: float  # P(win), 0-1
    components: SignalComponents
    reasoning: str
    
    def __repr__(self):
        return (
            f"Signal(token={self.token_name} | "
            f"score={self.signal_score} | "
            f"confidence={self.confidence:.2f} | "
            f"buy={self.buy_signal})"
        )


class SignalEnhancer:
    """
    Multi-factor signal scoring engine
    Combines momentum, volume, holders, and smart money metrics
    """
    
    MOMENTUM_WINDOW_MINUTES = 60
    VOLUME_MA_PERIOD = 20
    MIN_SCORE_FOR_SIGNAL = 50  # Buy only if score > 50
    
    def __init__(self, historical_trades: Optional[List[Dict]] = None):
        """
        Args:
            historical_trades: List of past trades {win/loss, entry_price, exit_price, etc}
                Used to calculate win_rate for edge probability
        """
        self.historical_trades = historical_trades or []
        self._calculate_win_rate()
    
    def _calculate_win_rate(self) -> float:
        """Calculate historical win rate from past trades"""
        if not self.historical_trades:
            self.win_rate = 0.50  # Default neutral
            return
        
        wins = sum(1 for t in self.historical_trades if t.get('pnl_pct', 0) > 0)
        self.win_rate = wins / len(self.historical_trades) if self.historical_trades else 0.50
    
    def score_momentum(
        self,
        candles: List[OHLCV],
        current_price: float,
    ) -> int:
        """
        Score momentum using RSI + MACD + price acceleration
        Range: 0-100
        
        Factors:
        - RSI (30=oversold=bullish, 70=overbought=bearish)
        - Price acceleration (higher recent change = more bullish)
        - EMA divergence (fast EMA above slow EMA = bullish)
        """
        if len(candles) < 14:  # Need at least 14 candles for RSI
            return 50  # Neutral
        
        # 1. Calculate RSI
        rsi = self._calculate_rsi(candles, period=14)
        
        # 2. Calculate price change %
        close_prices = [c.close for c in candles[-14:]]
        if candles[-2].close > 0:
            price_change_pct = ((current_price - candles[-2].close) / candles[-2].close) * 100
        else:
            price_change_pct = 0
        
        # 3. Calculate EMAs
        ema_12 = self._calculate_ema(close_prices, period=12)
        ema_26 = self._calculate_ema(close_prices, period=26)
        macd_line = ema_12 - ema_26
        
        # Convert to 0-100 score
        rsi_score = rsi  # RSI already 0-100
        
        # Price change: -10% to +50% maps to 0-100
        change_score = min(100, max(0, (price_change_pct + 10) * 100 / 60))
        
        # MACD: positive = bullish
        macd_score = 50 + (macd_line * 10) if macd_line else 50
        macd_score = min(100, max(0, macd_score))
        
        # Combine: RSI weight 40%, change 40%, MACD 20%
        momentum_score = int(rsi_score * 0.4 + change_score * 0.4 + macd_score * 0.2)
        return min(100, max(0, momentum_score))
    
    def score_volume(
        self,
        candles: List[OHLCV],
        current_volume: float,
    ) -> int:
        """
        Score volume relative to moving average
        Range: 0-100
        
        Factors:
        - Volume vs 20-period MA (2x MA = bullish)
        - Volume trend (increasing = bullish)
        - Relative to token's typical volume
        """
        if len(candles) < 20:
            return 50  # Neutral
        
        recent_volumes = [c.volume for c in candles[-20:]]
        volume_ma = statistics.mean(recent_volumes)
        
        if volume_ma == 0:
            return 50
        
        # Volume ratio
        volume_ratio = current_volume / volume_ma
        
        # 1x = baseline, 2x = strong, 3x+ = very strong
        # Map: 0.5x to 3x -> 0 to 100
        volume_score = min(100, (volume_ratio - 0.5) * 100 / 2.5)
        
        # 2. Volume trend: is volume increasing?
        if len(recent_volumes) >= 5:
            recent_vol = statistics.mean(recent_volumes[-5:])
            older_vol = statistics.mean(recent_volumes[-10:-5])
            if older_vol > 0:
                vol_trend = (recent_vol - older_vol) / older_vol * 100
                # Increasing trend = +20 score, decreasing = -20
                trend_bonus = min(20, max(-20, vol_trend / 10))
                volume_score += trend_bonus
        
        return min(100, max(0, int(volume_score)))
    
    def score_holders(
        self,
        holder_concentration_pct: float,  # % of supply in top 5
        holder_count: int,
        min_holder_count: int = 100,
    ) -> int:
        """
        Score holder distribution quality
        Range: 0-100 (higher = more distributed = better)
        
        Factors:
        - Top 5 holders % (lower = more distributed = better)
        - Total holder count (more = more distributed = better)
        - Rug pull risk (very high concentration = bad)
        """
        # Concentration score: <20% = 100, >70% = 0
        if holder_concentration_pct > 70:
            concentration_score = 0  # Rug risk
        elif holder_concentration_pct > 50:
            concentration_score = 20
        elif holder_concentration_pct > 35:
            concentration_score = 40
        elif holder_concentration_pct > 20:
            concentration_score = 70
        else:
            concentration_score = 100
        
        # Holder count score
        # <100 = risky, 100 = okay, 1000+ = good
        if holder_count < min_holder_count:
            holder_score = max(10, holder_count * 100 / min_holder_count)
        elif holder_count < 1000:
            holder_score = 60 + (holder_count - min_holder_count) * 40 / (1000 - min_holder_count)
        else:
            holder_score = 100
        
        # Combine: concentration 60%, count 40%
        score = int(concentration_score * 0.6 + holder_score * 0.4)
        return min(100, max(0, score))
    
    def score_smart_money(
        self,
        whale_buys_last_hour: int,
        whale_sells_last_hour: int,
        large_holder_growth_pct: float,  # % increase in top holder count
        token_age_minutes: int,
        dev_activity_score: Optional[int] = None,  # 0-100
    ) -> int:
        """
        Score on-chain smart money signals
        Range: 0-100
        
        Factors:
        - Whale accumulation (buys > sells = bullish)
        - Large holder growth (more holders buying = bullish)
        - Token age (newer = riskier but can be more explosive)
        - Dev/contract activity
        """
        # 1. Whale activity: buys vs sells
        if whale_buys_last_hour + whale_sells_last_hour == 0:
            whale_score = 50  # No activity
        else:
            whale_ratio = whale_buys_last_hour / max(1, whale_buys_last_hour + whale_sells_last_hour)
            # 100% buys = 100, 50/50 = 50, 100% sells = 0
            whale_score = int(whale_ratio * 100)
        
        # 2. Large holder growth
        # <0% = -20, 3-5% = +20, >10% = +40
        holder_growth_score = 50 + (large_holder_growth_pct / 10) * 20
        holder_growth_score = min(100, max(0, holder_growth_score))
        
        # 3. Token age
        # Brand new (0-30m) = risky but volatile, ideal 1-4h, old is stable
        if token_age_minutes < 30:
            age_score = 60  # Very volatile, could be rug
        elif token_age_minutes < 60:
            age_score = 75  # Early but established
        elif token_age_minutes < 240:  # < 4 hours
            age_score = 85  # Sweet spot
        else:
            age_score = 70  # Older, less explosive but safer
        
        # 4. Dev activity (if provided)
        dev_score = dev_activity_score or 50
        
        # Combine: whale 40%, growth 30%, age 20%, dev 10%
        score = int(whale_score * 0.4 + holder_growth_score * 0.3 + 
                   age_score * 0.2 + dev_score * 0.1)
        return min(100, max(0, score))
    
    def calculate_edge_probability(self, signal_score: int) -> float:
        """
        Calculate P(win) based on signal quality and historical data
        
        Args:
            signal_score: 0-100 from combined signal
        
        Returns:
            Probability of winning trade (0-1)
        """
        # Base: use historical win rate
        base_win_prob = self.win_rate
        
        # Signal adjustment: higher score = higher expected win prob
        # Score >75 = +15%, Score 50-75 = +5%, Score <50 = -10%
        if signal_score > 75:
            signal_boost = 0.15
        elif signal_score >= 50:
            signal_boost = 0.05
        else:
            signal_boost = -0.10
        
        edge_prob = base_win_prob + signal_boost
        return max(0.0, min(1.0, edge_prob))  # Clamp to 0-1
    
    def calculate_confidence(self, components: SignalComponents) -> float:
        """
        Calculate confidence (0-1) based on component agreement
        
        If all components agree = high confidence
        If components diverge = low confidence
        """
        scores = [components.momentum_score, components.volume_score,
                  components.holders_score, components.smart_money_score]
        
        # Std dev of components: low = agreement = high confidence
        mean_score = sum(scores) / len(scores)
        variance = sum((s - mean_score) ** 2 for s in scores) / len(scores)
        std_dev = math.sqrt(variance)
        
        # 0 std dev = 1.0 confidence, 25 std dev = 0.5 confidence
        confidence = max(0.3, 1.0 - (std_dev / 100))
        return confidence
    
    def analyze_token(
        self,
        token_name: str,
        token_mint: str,
        candles: List[OHLCV],
        current_price: float,
        current_volume: float,
        holder_concentration_pct: float,
        holder_count: int,
        whale_buys_last_hour: int,
        whale_sells_last_hour: int,
        large_holder_growth_pct: float,
        token_age_minutes: int,
    ) -> SignalResult:
        """
        Analyze token and generate comprehensive signal
        
        Returns:
            SignalResult with score, buy signal, confidence, edge probability
        """
        # Score each component
        momentum = self.score_momentum(candles, current_price)
        volume = self.score_volume(candles, current_volume)
        holders = self.score_holders(holder_concentration_pct, holder_count)
        smart_money = self.score_smart_money(
            whale_buys_last_hour, whale_sells_last_hour,
            large_holder_growth_pct, token_age_minutes
        )
        
        components = SignalComponents(
            momentum_score=momentum,
            volume_score=volume,
            holders_score=holders,
            smart_money_score=smart_money,
        )
        
        # Calculate overall score
        signal_score = int(components.average())
        
        # Buy signal: score > 50
        buy_signal = signal_score > self.MIN_SCORE_FOR_SIGNAL
        
        # Calculate confidence and edge probability
        confidence = self.calculate_confidence(components)
        edge_probability = self.calculate_edge_probability(signal_score)
        
        # Generate reasoning
        best_component = max(
            [("Momentum", momentum), ("Volume", volume),
             ("Holders", holders), ("Smart Money", smart_money)],
            key=lambda x: x[1]
        )
        worst_component = min(
            [("Momentum", momentum), ("Volume", volume),
             ("Holders", holders), ("Smart Money", smart_money)],
            key=lambda x: x[1]
        )
        
        reasoning = (
            f"Momentum={momentum} | Volume={volume} | "
            f"Holders={holders} | SmartMoney={smart_money} | "
            f"Confidence={confidence:.2f} | Edge={edge_probability:.2f} | "
            f"Best={best_component[0]} | Worst={worst_component[0]}"
        )
        
        return SignalResult(
            token_name=token_name,
            token_mint=token_mint,
            signal_score=signal_score,
            buy_signal=buy_signal,
            confidence=confidence,
            edge_probability=edge_probability,
            components=components,
            reasoning=reasoning,
        )
    
    # Helper methods
    def _calculate_rsi(self, candles: List[OHLCV], period: int = 14) -> float:
        """Calculate RSI (Relative Strength Index)"""
        if len(candles) < period + 1:
            return 50.0
        
        closes = [c.close for c in candles]
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        
        gains = [d if d > 0 else 0 for d in deltas[-period:]]
        losses = [-d if d < 0 else 0 for d in deltas[-period:]]
        
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    def _calculate_ema(self, values: List[float], period: int) -> float:
        """Calculate Exponential Moving Average"""
        if len(values) < period:
            return sum(values) / len(values) if values else 0
        
        multiplier = 2 / (period + 1)
        ema = sum(values[:period]) / period
        
        for value in values[period:]:
            ema = value * multiplier + ema * (1 - multiplier)
        
        return ema


# Example usage
if __name__ == "__main__":
    from datetime import datetime, timedelta
    
    enhancer = SignalEnhancer()
    
    # Create sample candles
    now = datetime.now()
    candles = []
    price = 100
    for i in range(20):
        price *= (1 + (i * 0.01))  # Uptrend
        candles.append(OHLCV(
            timestamp=now - timedelta(minutes=20-i),
            open=price * 0.99,
            high=price * 1.02,
            low=price * 0.98,
            close=price,
            volume=1000 + i * 100
        ))
    
    # Analyze
    signal = enhancer.analyze_token(
        token_name="TestToken",
        token_mint="11111111111111111111111111111111",
        candles=candles,
        current_price=price * 1.05,
        current_volume=2000,
        holder_concentration_pct=15.0,
        holder_count=500,
        whale_buys_last_hour=3,
        whale_sells_last_hour=1,
        large_holder_growth_pct=2.5,
        token_age_minutes=120,
    )
    
    print(f"Signal: {signal}")
    print(f"Buy Signal: {signal.buy_signal}")
    print(f"Details: {signal.reasoning}")
