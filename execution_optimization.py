"""
Execution Optimization Module
- Token info caching (30s TTL)
- RPC call batching ready
- Order book quality scoring
- Smart slippage handling
"""

import time
import json
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Any, Callable
from datetime import datetime, timedelta
from enum import Enum
import statistics

class OrderBookQuality(Enum):
    EXCELLENT = (90, 100)  # <0.1% spread, deep liquidity
    GOOD = (70, 90)        # 0.1-0.5% spread, moderate liquidity
    FAIR = (50, 70)        # 0.5-2% spread, limited depth
    POOR = (25, 50)        # 2-5% spread, thin book
    TERRIBLE = (0, 25)     # >5% spread, dangerous

@dataclass
class CachedValue:
    """Wrapper for cached values with TTL"""
    value: Any
    timestamp: float  # Unix timestamp when cached
    ttl_seconds: int = 30
    
    def is_expired(self) -> bool:
        """Check if cache entry has expired"""
        age_seconds = time.time() - self.timestamp
        return age_seconds > self.ttl_seconds
    
    def get(self) -> Optional[Any]:
        """Get value if not expired, else None"""
        return None if self.is_expired() else self.value

@dataclass
class TokenInfo:
    """Cached token metadata"""
    mint: str
    name: str
    symbol: str
    decimals: int
    supply: float
    holders: int
    top_holder_pct: float
    liquidity_usd: float
    market_cap_usd: float
    price_usd: float
    volume_24h_usd: float
    cached_at: datetime = field(default_factory=datetime.now)

@dataclass
class OrderBookSnapshot:
    """Order book state"""
    bids: List[tuple]  # [(price, amount), ...]
    asks: List[tuple]
    timestamp: datetime = field(default_factory=datetime.now)
    
    def get_bid_ask_spread_bps(self) -> float:
        """Bid-ask spread in basis points"""
        if not self.bids or not self.asks:
            return 10000  # Very high spread (100%)
        
        best_bid = self.bids[0][0]
        best_ask = self.asks[0][0]
        
        if best_bid <= 0 or best_ask <= 0:
            return 10000
        
        spread = (best_ask - best_bid) / best_bid * 10000
        return spread
    
    def get_bid_depth(self, amount_usd: float) -> float:
        """Get price impact of buying amount_usd from bid side"""
        if not self.bids:
            return 0.0
        
        cumulative_usd = 0.0
        weighted_price_total = 0.0
        
        for price, amount in self.bids:
            add_amount = min(amount, (amount_usd - cumulative_usd) / price)
            cumulative_usd += add_amount * price
            weighted_price_total += add_amount * price
            
            if cumulative_usd >= amount_usd:
                break
        
        if cumulative_usd == 0:
            return 0.0
        
        avg_price = weighted_price_total / (cumulative_usd / min(1, weighted_price_total))
        return avg_price
    
    def get_ask_depth(self, amount_usd: float) -> float:
        """Get price impact of selling amount_usd to ask side"""
        if not self.asks:
            return 0.0
        
        cumulative_usd = 0.0
        weighted_price_total = 0.0
        
        for price, amount in self.asks:
            add_amount = min(amount, (amount_usd - cumulative_usd) / price)
            cumulative_usd += add_amount * price
            weighted_price_total += add_amount * price
            
            if cumulative_usd >= amount_usd:
                break
        
        if cumulative_usd == 0:
            return 0.0
        
        avg_price = weighted_price_total / (cumulative_usd / max(0.01, weighted_price_total))
        return avg_price

@dataclass
class ExecutionParams:
    """Optimized execution parameters"""
    slippage_tolerance_bps: int  # Basis points (100 = 1%)
    priority_fee_lamports: int
    timeout_seconds: int
    retry_count: int
    order_type: str  # "market", "limit", "twap"
    expected_price_impact_bps: int
    estimated_execution_ms: int
    reasoning: str

class TokenCache:
    """Simple in-memory cache for token metadata (30s TTL)"""
    
    def __init__(self, ttl_seconds: int = 30):
        self._cache: Dict[str, CachedValue] = {}
        self.ttl_seconds = ttl_seconds
        self.hits = 0
        self.misses = 0
    
    def get(self, key: str) -> Optional[Any]:
        """Get cached value"""
        if key in self._cache:
            value = self._cache[key].get()
            if value is not None:
                self.hits += 1
                return value
            else:
                del self._cache[key]  # Clean up expired
        self.misses += 1
        return None
    
    def set(self, key: str, value: Any, ttl_seconds: Optional[int] = None):
        """Cache a value"""
        self._cache[key] = CachedValue(
            value=value,
            timestamp=time.time(),
            ttl_seconds=ttl_seconds or self.ttl_seconds
        )
    
    def clear(self):
        """Clear entire cache"""
        self._cache.clear()
        self.hits = 0
        self.misses = 0
    
    def get_hit_rate(self) -> float:
        """Return cache hit rate (0-1)"""
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0
    
    def cleanup_expired(self):
        """Remove expired entries"""
        expired_keys = [k for k, v in self._cache.items() if v.is_expired()]
        for key in expired_keys:
            del self._cache[key]
        return len(expired_keys)

class RpcBatcher:
    """
    Batch RPC calls for efficiency
    Instead of 5 separate calls, make 1 call with 5 requests
    """
    
    def __init__(self, batch_size: int = 10, timeout_ms: int = 5000):
        self.batch_size = batch_size
        self.timeout_ms = timeout_ms
        self._pending_calls: List[Dict] = []
    
    def add_call(self, method: str, params: List[Any]) -> int:
        """
        Queue an RPC call
        Returns: call_id for later retrieval
        """
        call_id = len(self._pending_calls)
        self._pending_calls.append({
            "id": call_id,
            "method": method,
            "params": params,
            "jsonrpc": "2.0"
        })
        return call_id
    
    def get_batch(self) -> List[Dict]:
        """Get current batch and clear pending"""
        batch = self._pending_calls.copy()
        self._pending_calls.clear()
        return batch
    
    def should_flush(self) -> bool:
        """Check if batch should be sent"""
        return len(self._pending_calls) >= self.batch_size

class ExecutionOptimizer:
    """
    Optimizes order execution with caching, batching, and smart slippage
    """
    
    def __init__(self, cache_ttl_seconds: int = 30):
        self.token_cache = TokenCache(ttl_seconds=cache_ttl_seconds)
        self.orderbook_cache = TokenCache(ttl_seconds=5)  # 5s for order books
        self.rpc_batcher = RpcBatcher(batch_size=10)
        self.execution_history: List[ExecutionParams] = []
    
    def get_or_fetch_token_info(
        self,
        mint: str,
        fetch_fn: Optional[Callable[[str], TokenInfo]] = None,
    ) -> Optional[TokenInfo]:
        """
        Get cached token info or fetch if missing
        
        Args:
            mint: Token mint address
            fetch_fn: Function to fetch if cache miss (optional)
        
        Returns:
            TokenInfo or None
        """
        cache_key = f"token:{mint}"
        
        # Check cache
        cached = self.token_cache.get(cache_key)
        if cached:
            return cached
        
        # Fetch if function provided
        if fetch_fn:
            info = fetch_fn(mint)
            if info:
                self.token_cache.set(cache_key, info)
                return info
        
        return None
    
    def batch_fetch_token_info(
        self,
        mints: List[str],
        fetch_fn: Optional[Callable[[List[str]], List[TokenInfo]]] = None,
    ) -> Dict[str, Optional[TokenInfo]]:
        """
        Batch fetch multiple tokens (RPC efficient)
        Uses cache for hits, single RPC call for misses
        """
        results = {}
        cache_hits = []
        cache_misses = []
        
        # Check cache for all
        for mint in mints:
            cached = self.get_or_fetch_token_info(mint)
            if cached:
                results[mint] = cached
                cache_hits.append(mint)
            else:
                cache_misses.append(mint)
        
        # Batch fetch misses
        if cache_misses and fetch_fn:
            fetched = fetch_fn(cache_misses)
            for info in fetched:
                results[info.mint] = info
                self.token_cache.set(f"token:{info.mint}", info)
        
        # Fill in any we couldn't fetch
        for mint in mints:
            if mint not in results:
                results[mint] = None
        
        return results
    
    def score_orderbook_quality(
        self,
        orderbook: OrderBookSnapshot,
        amount_usd: float,
    ) -> int:
        """
        Score order book quality (0-100)
        
        Factors:
        - Bid-ask spread
        - Depth (can we fill without slippage)
        - Liquidity concentration
        """
        spread_bps = orderbook.get_bid_ask_spread_bps()
        
        # Spread score
        if spread_bps < 10:  # <0.1%
            spread_score = 100
        elif spread_bps < 50:  # 0.1-0.5%
            spread_score = 90
        elif spread_bps < 200:  # 0.5-2%
            spread_score = 70
        elif spread_bps < 500:  # 2-5%
            spread_score = 40
        else:  # >5%
            spread_score = 10
        
        # Depth score: can we fill the order?
        bid_depth = orderbook.get_bid_depth(amount_usd)
        if bid_depth > 0:
            depth_score = min(100, (bid_depth / 10) * 100)  # Adjust divisor as needed
        else:
            depth_score = 0
        
        # Combine: spread 60%, depth 40%
        quality_score = int(spread_score * 0.6 + depth_score * 0.4)
        return min(100, max(0, quality_score))
    
    def calculate_smart_slippage(
        self,
        order_size_sol: float,
        expected_price_impact_bps: int,
        orderbook_quality: int,
        historical_slippage_bps: Optional[List[int]] = None,
    ) -> ExecutionParams:
        """
        Calculate optimal execution parameters based on conditions
        
        Args:
            order_size_sol: Size in SOL
            expected_price_impact_bps: Model's estimate
            orderbook_quality: 0-100 quality score
            historical_slippage_bps: Past slippage values for stats
        
        Returns:
            ExecutionParams with recommended settings
        """
        # 1. Base slippage from price impact
        base_slippage = expected_price_impact_bps
        
        # 2. Adjust for order book quality
        # Excellent book: -10 bps
        # Poor book: +30 bps
        quality_adjustment = 0
        if orderbook_quality >= 90:
            quality_adjustment = -10
        elif orderbook_quality >= 70:
            quality_adjustment = 0
        elif orderbook_quality >= 50:
            quality_adjustment = 10
        else:
            quality_adjustment = 30
        
        # 3. Adjust for order size
        # Larger orders need more slippage
        if order_size_sol > 0.1:
            size_adjustment = int((order_size_sol - 0.1) * 100)
        else:
            size_adjustment = 0
        
        # 4. Use historical data if available
        if historical_slippage_bps:
            hist_median = statistics.median(historical_slippage_bps)
            hist_p75 = sorted(historical_slippage_bps)[int(len(historical_slippage_bps) * 0.75)]
            # Use p75 as safety margin
            slippage_tolerance = hist_p75
        else:
            slippage_tolerance = base_slippage + quality_adjustment + size_adjustment
        
        # 5. Final bounds
        # Min: 50 bps (0.5%)
        # Max: 2000 bps (20%)
        slippage_tolerance = max(50, min(2000, slippage_tolerance))
        
        # 6. Determine order type and timeout
        if slippage_tolerance < 100:  # Very good conditions
            order_type = "limit"
            timeout_seconds = 5
            retry_count = 2
            priority_fee = 1000  # Low priority fee
        elif slippage_tolerance < 500:  # Good conditions
            order_type = "market"
            timeout_seconds = 10
            retry_count = 3
            priority_fee = 5000
        else:  # Tough conditions
            order_type = "market"
            timeout_seconds = 15
            retry_count = 1
            priority_fee = 25000  # High priority fee
        
        # 7. Estimate execution time
        if slippage_tolerance < 100:
            est_execution_ms = 500  # Faster for limit
        elif slippage_tolerance < 500:
            est_execution_ms = 1000
        else:
            est_execution_ms = 2000
        
        reasoning = (
            f"BaseSlippage={base_slippage}bps | "
            f"QualityAdj={quality_adjustment}bps | "
            f"SizeAdj={size_adjustment}bps | "
            f"OrderBook={orderbook_quality}/100"
        )
        
        return ExecutionParams(
            slippage_tolerance_bps=slippage_tolerance,
            priority_fee_lamports=priority_fee,
            timeout_seconds=timeout_seconds,
            retry_count=retry_count,
            order_type=order_type,
            expected_price_impact_bps=expected_price_impact_bps,
            estimated_execution_ms=est_execution_ms,
            reasoning=reasoning,
        )
    
    def optimize_execution(
        self,
        token_mint: str,
        order_size_sol: float,
        orderbook: Optional[OrderBookSnapshot] = None,
        historical_slippage: Optional[List[int]] = None,
    ) -> ExecutionParams:
        """
        Full optimization: cache check, quality scoring, slippage calculation
        """
        # Get token info from cache
        token_info = self.get_or_fetch_token_info(token_mint)
        
        # Score order book
        if orderbook:
            quality_score = self.score_orderbook_quality(orderbook, order_size_sol * 1000)
        else:
            quality_score = 50  # Neutral if no data
        
        # Estimate price impact (simplified)
        # Larger orders have bigger impact
        estimated_impact_bps = int((order_size_sol / 10) * 100)  # 0.1 SOL = 100 bps
        
        # Calculate smart slippage
        params = self.calculate_smart_slippage(
            order_size_sol=order_size_sol,
            expected_price_impact_bps=estimated_impact_bps,
            orderbook_quality=quality_score,
            historical_slippage_bps=historical_slippage,
        )
        
        self.execution_history.append(params)
        return params
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """Return cache performance metrics"""
        return {
            "token_cache_hit_rate": self.token_cache.get_hit_rate(),
            "token_cache_hits": self.token_cache.hits,
            "token_cache_misses": self.token_cache.misses,
            "total_executions": len(self.execution_history),
            "cache_ttl_seconds": self.token_cache.ttl_seconds,
        }

# Example usage
if __name__ == "__main__":
    optimizer = ExecutionOptimizer(cache_ttl_seconds=30)
    
    # Simulate order book
    orderbook = OrderBookSnapshot(
        bids=[(100.0, 10), (99.9, 20), (99.8, 30)],
        asks=[(100.1, 10), (100.2, 20), (100.3, 30)],
    )
    
    # Optimize execution
    params = optimizer.optimize_execution(
        token_mint="11111111111111111111111111111111",
        order_size_sol=0.05,
        orderbook=orderbook,
        historical_slippage=[50, 75, 100, 80, 60],
    )
    
    print(f"Slippage tolerance: {params.slippage_tolerance_bps} bps")
    print(f"Priority fee: {params.priority_fee_lamports} lamports")
    print(f"Order type: {params.order_type}")
    print(f"Timeout: {params.timeout_seconds}s")
    print(f"Retry count: {params.retry_count}")
    print(f"Reasoning: {params.reasoning}")
    print(f"\nCache stats: {optimizer.get_cache_stats()}")
