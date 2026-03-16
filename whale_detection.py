"""
Whale Detection Pipeline — SolTrader
=====================================
Implements entity intelligence from the research paper:
- Supply concentration tracking
- Flow analysis (accumulation vs distribution)
- Entity clustering and labeling
- Whale scoring with interpretable features
- Infrastructure filtering (routers, bridges, CEX)
"""

import math
import time
import threading
import requests
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from functools import lru_cache
import json

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

# Known infrastructure addresses (routers, bridges, CEX hot wallets)
KNOWN_INFRASTRUCTURE = {
    # Jupiter Aggregator
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "jupiter-aggregator",
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB": "jupiter-v4",
    "JUP2jxvXaqu7NQY1GmNF4m1vodw12LVXYxbFL2uJvfo": "jupiter-v2",
    # Raydium
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium-amm",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "raydium-clmm",
    "routeUGWgWzqBWFcrCfv8tritsqukccJPu3q5GPP3xS": "raydium-router",
    # Pump.fun
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump-fun",
    "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg": "pump-fun-v2",
    # Orca
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "orca-whirlpool",
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP": "orca-v2",
    # Meteora
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "meteora-dlmm",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "meteora-pools",
    # Wormhole Bridge
    "wormDTUJ6AWPNvk59vGQbDvGJmqbDTdgWgAqcLBCgUb": "wormhole",
    # CEX Hot Wallets (known public addresses)
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": "coinbase-custody",
    "2AQdpHJ2JpcEgPiATUXjQxA8QmafFegfQwSLWSprPicm": "binance-hot",
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": "kraken-hot",
}

# CEX deposit pattern detection
CEX_DEPOSIT_PATTERNS = {
    "binance": ["2AQdpHJ2JpcEgPiATUXjQxA8QmafFegfQwSLWSprPicm"],
    "coinbase": ["H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS"],
    "kraken": ["5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"],
}

# Smart money wallets (known successful traders)
SMART_MONEY_WALLETS: Set[str] = set()

# Whale score weights (from research paper formula)
WHALE_SCORE_WEIGHTS = {
    "percent_supply_held": 0.25,
    "net_buy_flow_ratio": 0.20,
    "trade_size_impact": 0.20,
    "counterparty_entropy": 0.10,
    "infra_penalty": -0.30,
    "cluster_confidence": 0.15,
}


# ══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class WalletEntity:
    """Represents a clustered wallet entity (may span multiple addresses)."""
    primary_wallet: str
    linked_wallets: Set[str] = field(default_factory=set)
    label: Optional[str] = None  # "whale", "smart-money", "router", "cex", etc.
    is_infrastructure: bool = False
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    total_volume_sol: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    tokens_traded: Set[str] = field(default_factory=set)
    
    @property
    def all_wallets(self) -> Set[str]:
        return {self.primary_wallet} | self.linked_wallets
    
    @property
    def win_rate(self) -> float:
        total = self.win_count + self.loss_count
        return (self.win_count / total * 100) if total > 0 else 0.0
    
    def to_dict(self) -> dict:
        return {
            "primary_wallet": self.primary_wallet,
            "linked_wallets": list(self.linked_wallets),
            "label": self.label,
            "is_infrastructure": self.is_infrastructure,
            "total_volume_sol": round(self.total_volume_sol, 4),
            "win_rate": round(self.win_rate, 1),
            "tokens_traded": len(self.tokens_traded),
        }


@dataclass
class WhaleAction:
    """Represents a whale action event."""
    wallet: str
    entity_id: Optional[str]
    mint: str
    action_type: str  # "accumulate", "distribute", "lp_add", "lp_remove"
    sol_amount: float
    token_amount: float
    price_impact_bps: int
    timestamp: float
    supply_share_pct: float
    is_first_buyer: bool = False
    counterparties: List[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return {
            "wallet": self.wallet[:8] + "...",
            "entity_id": self.entity_id,
            "mint": self.mint,
            "action_type": self.action_type,
            "sol_amount": round(self.sol_amount, 4),
            "price_impact_bps": self.price_impact_bps,
            "supply_share_pct": round(self.supply_share_pct, 2),
            "is_first_buyer": self.is_first_buyer,
            "timestamp": self.timestamp,
        }


@dataclass 
class TokenHolderSnapshot:
    """Snapshot of token holder distribution."""
    mint: str
    timestamp: float
    total_supply: int
    holders: Dict[str, int]  # wallet -> balance
    top_10_pct: float
    gini_coefficient: float
    hhi_index: float  # Herfindahl-Hirschman Index
    
    @classmethod
    def compute(cls, mint: str, holders: Dict[str, int], total_supply: int) -> "TokenHolderSnapshot":
        if not holders or total_supply <= 0:
            return cls(
                mint=mint,
                timestamp=time.time(),
                total_supply=total_supply,
                holders=holders,
                top_10_pct=0.0,
                gini_coefficient=0.0,
                hhi_index=0.0,
            )
        
        balances = sorted(holders.values(), reverse=True)
        n = len(balances)
        
        # Top 10 concentration
        top_10 = balances[:10]
        top_10_pct = sum(top_10) / total_supply * 100 if total_supply > 0 else 0
        
        # Gini coefficient
        if n > 1:
            sorted_balances = sorted(balances)
            cumulative = sum((i + 1) * b for i, b in enumerate(sorted_balances))
            gini = (2 * cumulative) / (n * sum(sorted_balances)) - (n + 1) / n
        else:
            gini = 0.0
        
        # HHI (market concentration index)
        hhi = sum((b / total_supply * 100) ** 2 for b in balances) if total_supply > 0 else 0
        
        return cls(
            mint=mint,
            timestamp=time.time(),
            total_supply=total_supply,
            holders=holders,
            top_10_pct=top_10_pct,
            gini_coefficient=gini,
            hhi_index=hhi,
        )


@dataclass
class WhaleScore:
    """Computed whale score for an entity-token pair."""
    entity_wallet: str
    mint: str
    score: float  # 0-100
    confidence: float  # 0-1
    components: Dict[str, float]
    triggers: List[str]
    timestamp: float = field(default_factory=time.time)
    
    def to_dict(self) -> dict:
        return {
            "wallet": self.entity_wallet[:8] + "...",
            "mint": self.mint,
            "score": round(self.score, 1),
            "confidence": round(self.confidence, 2),
            "components": {k: round(v, 2) for k, v in self.components.items()},
            "triggers": self.triggers,
        }


# ══════════════════════════════════════════════════════════════════════════════
# ENTITY RESOLUTION & CLUSTERING
# ══════════════════════════════════════════════════════════════════════════════

class EntityResolver:
    """
    Clusters addresses into likely entities using heuristics from research:
    - Deposit address reuse
    - Multi-airdrop participation
    - Token authorization patterns
    - Temporal transaction correlation
    """
    
    def __init__(self):
        self._lock = threading.RLock()
        self._entities: Dict[str, WalletEntity] = {}  # primary_wallet -> entity
        self._wallet_to_entity: Dict[str, str] = {}  # any_wallet -> primary_wallet
        self._transaction_graph: Dict[str, Set[str]] = defaultdict(set)  # wallet -> counterparties
        
    def get_entity(self, wallet: str) -> Optional[WalletEntity]:
        """Get the entity for a wallet address."""
        with self._lock:
            primary = self._wallet_to_entity.get(wallet, wallet)
            return self._entities.get(primary)
    
    def get_or_create_entity(self, wallet: str) -> WalletEntity:
        """Get existing entity or create a new one."""
        with self._lock:
            primary = self._wallet_to_entity.get(wallet, wallet)
            if primary in self._entities:
                return self._entities[primary]
            
            # Check if this is known infrastructure
            is_infra = wallet in KNOWN_INFRASTRUCTURE
            label = KNOWN_INFRASTRUCTURE.get(wallet)
            
            entity = WalletEntity(
                primary_wallet=wallet,
                is_infrastructure=is_infra,
                label=label,
            )
            self._entities[wallet] = entity
            self._wallet_to_entity[wallet] = wallet
            return entity
    
    def link_wallets(self, wallet1: str, wallet2: str, reason: str = ""):
        """Link two wallets as belonging to the same entity."""
        with self._lock:
            entity1 = self.get_or_create_entity(wallet1)
            entity2 = self.get_or_create_entity(wallet2)
            
            if entity1.primary_wallet == entity2.primary_wallet:
                return  # Already linked
            
            # Merge smaller into larger
            if len(entity2.all_wallets) > len(entity1.all_wallets):
                entity1, entity2 = entity2, entity1
            
            # Transfer linked wallets
            for w in entity2.all_wallets:
                entity1.linked_wallets.add(w)
                self._wallet_to_entity[w] = entity1.primary_wallet
            
            # Merge stats
            entity1.total_volume_sol += entity2.total_volume_sol
            entity1.win_count += entity2.win_count
            entity1.loss_count += entity2.loss_count
            entity1.tokens_traded |= entity2.tokens_traded
            
            # Remove old entity
            if entity2.primary_wallet in self._entities:
                del self._entities[entity2.primary_wallet]
    
    def record_transaction(self, from_wallet: str, to_wallet: str, mint: str, sol_amount: float):
        """Record a transaction for clustering analysis."""
        with self._lock:
            self._transaction_graph[from_wallet].add(to_wallet)
            self._transaction_graph[to_wallet].add(from_wallet)
            
            # Update entity stats
            entity = self.get_or_create_entity(from_wallet)
            entity.tokens_traded.add(mint)
            entity.total_volume_sol += sol_amount
            entity.last_seen = time.time()
    
    def detect_clustering_patterns(self, wallets: List[str]) -> List[Tuple[str, str, str]]:
        """
        Detect clustering patterns among a set of wallets.
        Returns list of (wallet1, wallet2, reason) tuples.
        """
        links = []
        
        # Pattern 1: Shared counterparties (many transactions with same addresses)
        for i, w1 in enumerate(wallets):
            counterparties1 = self._transaction_graph.get(w1, set())
            for w2 in wallets[i+1:]:
                counterparties2 = self._transaction_graph.get(w2, set())
                overlap = counterparties1 & counterparties2
                if len(overlap) >= 5:  # Threshold for shared counterparties
                    links.append((w1, w2, f"shared-counterparties:{len(overlap)}"))
        
        return links
    
    def is_infrastructure(self, wallet: str) -> bool:
        """Check if wallet is known infrastructure."""
        entity = self.get_entity(wallet)
        if entity and entity.is_infrastructure:
            return True
        return wallet in KNOWN_INFRASTRUCTURE
    
    def label_entity(self, wallet: str, label: str):
        """Apply a label to an entity."""
        with self._lock:
            entity = self.get_or_create_entity(wallet)
            entity.label = label
    
    def get_all_entities(self) -> List[WalletEntity]:
        """Get all tracked entities."""
        with self._lock:
            return list(self._entities.values())
    
    def export_state(self) -> dict:
        """Export resolver state for persistence."""
        with self._lock:
            return {
                "entities": {k: v.to_dict() for k, v in self._entities.items()},
                "wallet_map": dict(self._wallet_to_entity),
            }


# ══════════════════════════════════════════════════════════════════════════════
# FLOW ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

class FlowAnalyzer:
    """
    Analyzes token flows for whale detection:
    - Net flow (accumulation vs distribution)
    - Flow velocity and acceleration
    - Counterparty diversity
    """
    
    def __init__(self, window_sizes: List[int] = None):
        self._lock = threading.RLock()
        self.window_sizes = window_sizes or [60, 300, 3600, 86400]  # 1m, 5m, 1h, 24h
        # {mint -> {wallet -> deque of (timestamp, amount, is_buy)}}
        self._flows: Dict[str, Dict[str, deque]] = defaultdict(lambda: defaultdict(lambda: deque(maxlen=1000)))
        self._token_flows: Dict[str, deque] = defaultdict(lambda: deque(maxlen=5000))
        
    def record_flow(self, mint: str, wallet: str, amount_sol: float, is_buy: bool, timestamp: float = None):
        """Record a flow event."""
        ts = timestamp or time.time()
        with self._lock:
            self._flows[mint][wallet].append((ts, amount_sol, is_buy))
            self._token_flows[mint].append((ts, wallet, amount_sol, is_buy))
    
    def get_net_flow(self, mint: str, wallet: str, window_sec: int) -> float:
        """Get net flow for a wallet in a token over a time window."""
        cutoff = time.time() - window_sec
        with self._lock:
            flows = self._flows.get(mint, {}).get(wallet, deque())
            net = 0.0
            for ts, amount, is_buy in flows:
                if ts >= cutoff:
                    net += amount if is_buy else -amount
            return net
    
    def get_flow_velocity(self, mint: str, wallet: str, window_sec: int = 300) -> float:
        """Get flow velocity (change rate) over window."""
        cutoff = time.time() - window_sec
        with self._lock:
            flows = self._flows.get(mint, {}).get(wallet, deque())
            recent = [(ts, amt, buy) for ts, amt, buy in flows if ts >= cutoff]
            if len(recent) < 2:
                return 0.0
            
            # Calculate rate of change
            time_span = recent[-1][0] - recent[0][0]
            if time_span <= 0:
                return 0.0
            
            total_flow = sum(amt if buy else -amt for ts, amt, buy in recent)
            return total_flow / time_span * 60  # Per minute
    
    def get_flow_acceleration(self, mint: str, wallet: str) -> float:
        """Get flow acceleration (second derivative)."""
        v1 = self.get_flow_velocity(mint, wallet, 60)   # Last minute
        v2 = self.get_flow_velocity(mint, wallet, 300)  # Last 5 minutes
        return v1 - v2
    
    def get_token_net_flow(self, mint: str, window_sec: int) -> float:
        """Get total net flow for a token."""
        cutoff = time.time() - window_sec
        with self._lock:
            flows = self._token_flows.get(mint, deque())
            net = 0.0
            for ts, wallet, amount, is_buy in flows:
                if ts >= cutoff:
                    net += amount if is_buy else -amount
            return net
    
    def get_top_accumulators(self, mint: str, window_sec: int = 3600, limit: int = 10) -> List[Tuple[str, float]]:
        """Get top accumulating wallets for a token."""
        cutoff = time.time() - window_sec
        wallet_flows: Dict[str, float] = defaultdict(float)
        
        with self._lock:
            for wallet, flows in self._flows.get(mint, {}).items():
                for ts, amount, is_buy in flows:
                    if ts >= cutoff:
                        wallet_flows[wallet] += amount if is_buy else -amount
        
        sorted_wallets = sorted(wallet_flows.items(), key=lambda x: x[1], reverse=True)
        return sorted_wallets[:limit]
    
    def get_counterparty_entropy(self, mint: str, wallet: str, window_sec: int = 3600) -> float:
        """
        Calculate counterparty diversity using entropy.
        High entropy = accumulating from many sources (more organic).
        Low entropy = accumulating from few sources (potentially coordinated).
        """
        cutoff = time.time() - window_sec
        with self._lock:
            flows = self._token_flows.get(mint, deque())
            counterparties: Dict[str, int] = defaultdict(int)
            total = 0
            
            for ts, flow_wallet, amount, is_buy in flows:
                if ts >= cutoff and flow_wallet != wallet:
                    counterparties[flow_wallet] += 1
                    total += 1
            
            if total == 0:
                return 0.0
            
            # Shannon entropy
            entropy = 0.0
            for count in counterparties.values():
                p = count / total
                if p > 0:
                    entropy -= p * math.log2(p)
            
            # Normalize to 0-1 (max entropy is log2(n))
            max_entropy = math.log2(len(counterparties)) if len(counterparties) > 1 else 1
            return entropy / max_entropy if max_entropy > 0 else 0.0
    
    def get_burstiness(self, mint: str, wallet: str, window_sec: int = 300) -> float:
        """
        Calculate burstiness of trading activity.
        High burstiness = clustered activity (whale behavior).
        Low burstiness = even distribution (organic).
        """
        cutoff = time.time() - window_sec
        with self._lock:
            flows = self._flows.get(mint, {}).get(wallet, deque())
            timestamps = [ts for ts, amt, buy in flows if ts >= cutoff]
            
            if len(timestamps) < 2:
                return 0.0
            
            # Calculate inter-arrival times
            timestamps = sorted(timestamps)
            intervals = [timestamps[i+1] - timestamps[i] for i in range(len(timestamps)-1)]
            
            if not intervals:
                return 0.0
            
            mean_interval = sum(intervals) / len(intervals)
            std_interval = math.sqrt(sum((x - mean_interval)**2 for x in intervals) / len(intervals))
            
            # Burstiness coefficient
            return (std_interval - mean_interval) / (std_interval + mean_interval) if (std_interval + mean_interval) > 0 else 0.0


# ══════════════════════════════════════════════════════════════════════════════
# WHALE SCORING ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class WhaleScorer:
    """
    Computes whale scores using the research paper formula:
    
    whale_score = sigmoid(
        w1 * z(percent_supply_held)
        + w2 * z(net_buy_flow_5m / liquidity_USD)
        + w3 * z(trade_size / pool_reserves)
        + w4 * z(counterparty_entropy)
        + w5 * infra_penalty
        + w6 * cluster_confidence
    )
    """
    
    def __init__(self, entity_resolver: EntityResolver, flow_analyzer: FlowAnalyzer):
        self.entity_resolver = entity_resolver
        self.flow_analyzer = flow_analyzer
        self._lock = threading.RLock()
        self._holder_snapshots: Dict[str, TokenHolderSnapshot] = {}
        self._recent_scores: deque = deque(maxlen=1000)
        
    def update_holder_snapshot(self, mint: str, holders: Dict[str, int], total_supply: int):
        """Update holder snapshot for a token."""
        with self._lock:
            self._holder_snapshots[mint] = TokenHolderSnapshot.compute(mint, holders, total_supply)
    
    def get_holder_snapshot(self, mint: str) -> Optional[TokenHolderSnapshot]:
        """Get latest holder snapshot for a token."""
        with self._lock:
            return self._holder_snapshots.get(mint)
    
    def compute_whale_score(
        self,
        wallet: str,
        mint: str,
        trade_size_sol: float = 0,
        liquidity_usd: float = 0,
        pool_reserves_sol: float = 0,
    ) -> WhaleScore:
        """Compute whale score for a wallet-token pair."""
        components = {}
        triggers = []
        
        # 1. Supply concentration
        snapshot = self.get_holder_snapshot(mint)
        percent_supply = 0.0
        if snapshot and snapshot.total_supply > 0:
            entity = self.entity_resolver.get_entity(wallet)
            if entity:
                total_held = sum(snapshot.holders.get(w, 0) for w in entity.all_wallets)
                percent_supply = total_held / snapshot.total_supply * 100
            else:
                percent_supply = snapshot.holders.get(wallet, 0) / snapshot.total_supply * 100
        
        components["percent_supply_held"] = self._z_score(percent_supply, 1.0, 5.0)
        if percent_supply >= 2.0:
            triggers.append(f"holds {percent_supply:.1f}% supply")
        
        # 2. Net flow ratio
        net_flow_5m = self.flow_analyzer.get_net_flow(mint, wallet, 300)
        flow_ratio = net_flow_5m / max(liquidity_usd / 150, 1)  # Normalize by liquidity in SOL equiv
        components["net_buy_flow_ratio"] = self._z_score(flow_ratio, 0.1, 1.0)
        if flow_ratio >= 0.3:
            triggers.append(f"net flow {net_flow_5m:.2f} SOL (5m)")
        
        # 3. Trade size impact
        trade_impact = trade_size_sol / max(pool_reserves_sol, 1) if pool_reserves_sol > 0 else 0
        components["trade_size_impact"] = self._z_score(trade_impact, 0.01, 0.1)
        if trade_impact >= 0.05:
            triggers.append(f"trade {trade_impact*100:.1f}% of reserves")
        
        # 4. Counterparty entropy
        entropy = self.flow_analyzer.get_counterparty_entropy(mint, wallet, 3600)
        components["counterparty_entropy"] = entropy
        if entropy < 0.3:
            triggers.append(f"low counterparty diversity ({entropy:.2f})")
        
        # 5. Infrastructure penalty
        is_infra = self.entity_resolver.is_infrastructure(wallet)
        components["infra_penalty"] = 1.0 if is_infra else 0.0
        if is_infra:
            triggers.append("infrastructure wallet")
        
        # 6. Cluster confidence
        entity = self.entity_resolver.get_entity(wallet)
        cluster_size = len(entity.all_wallets) if entity else 1
        cluster_confidence = min(1.0, (cluster_size - 1) / 10)  # 0-1 scale
        components["cluster_confidence"] = cluster_confidence
        if cluster_size > 3:
            triggers.append(f"clustered with {cluster_size} wallets")
        
        # Compute weighted score
        raw_score = sum(
            WHALE_SCORE_WEIGHTS.get(k, 0) * v 
            for k, v in components.items()
        )
        
        # Sigmoid normalization to 0-100
        score = 100 / (1 + math.exp(-raw_score * 2))
        
        # Confidence based on data quality
        confidence = 0.5
        if snapshot:
            confidence += 0.2
        if net_flow_5m != 0:
            confidence += 0.15
        if entity and len(entity.all_wallets) > 1:
            confidence += 0.15
        confidence = min(1.0, confidence)
        
        result = WhaleScore(
            entity_wallet=wallet,
            mint=mint,
            score=score,
            confidence=confidence,
            components=components,
            triggers=triggers,
        )
        
        with self._lock:
            self._recent_scores.append(result)
        
        return result
    
    def get_whale_triggers(self, mint: str, min_score: float = 60) -> List[WhaleScore]:
        """Get recent whale triggers for a token above score threshold."""
        with self._lock:
            return [
                s for s in self._recent_scores
                if s.mint == mint and s.score >= min_score and time.time() - s.timestamp < 300
            ]
    
    @staticmethod
    def _z_score(value: float, mean: float, std: float) -> float:
        """Compute z-score normalized to 0-1."""
        z = (value - mean) / std if std > 0 else 0
        return max(0, min(1, (z + 2) / 4))  # Map [-2, 2] to [0, 1]


# ══════════════════════════════════════════════════════════════════════════════
# WHALE ACTION DETECTOR
# ══════════════════════════════════════════════════════════════════════════════

class WhaleActionDetector:
    """
    Detects and classifies whale actions:
    - Large accumulation
    - Distribution (potential dump)
    - LP manipulation
    - Coordinated buying
    """
    
    def __init__(
        self,
        entity_resolver: EntityResolver,
        flow_analyzer: FlowAnalyzer,
        whale_scorer: WhaleScorer,
    ):
        self.entity_resolver = entity_resolver
        self.flow_analyzer = flow_analyzer
        self.whale_scorer = whale_scorer
        self._lock = threading.RLock()
        self._actions: deque = deque(maxlen=2000)
        self._callbacks: List[callable] = []
        
    def register_callback(self, callback: callable):
        """Register a callback for whale action events."""
        self._callbacks.append(callback)
    
    def process_swap(
        self,
        wallet: str,
        mint: str,
        sol_amount: float,
        token_amount: float,
        is_buy: bool,
        liquidity_usd: float,
        pool_reserves_sol: float,
        total_supply: int,
        timestamp: float = None,
    ) -> Optional[WhaleAction]:
        """Process a swap event and detect whale action."""
        ts = timestamp or time.time()
        
        # Record flow
        self.flow_analyzer.record_flow(mint, wallet, sol_amount, is_buy, ts)
        self.entity_resolver.record_transaction(
            from_wallet=wallet if not is_buy else "pool",
            to_wallet=wallet if is_buy else "pool",
            mint=mint,
            sol_amount=sol_amount,
        )
        
        # Check if this is infrastructure (skip)
        if self.entity_resolver.is_infrastructure(wallet):
            return None
        
        # Compute whale score
        score = self.whale_scorer.compute_whale_score(
            wallet=wallet,
            mint=mint,
            trade_size_sol=sol_amount,
            liquidity_usd=liquidity_usd,
            pool_reserves_sol=pool_reserves_sol,
        )
        
        # Only trigger on significant scores
        if score.score < 50:
            return None
        
        # Calculate price impact
        price_impact_bps = int(sol_amount / max(pool_reserves_sol, 1) * 10000) if pool_reserves_sol > 0 else 0
        
        # Calculate supply share
        supply_share = (token_amount / max(total_supply, 1) * 100) if total_supply > 0 else 0
        
        # Detect action type
        if is_buy:
            if supply_share >= 1.0:
                action_type = "whale_accumulate"
            else:
                action_type = "accumulate"
        else:
            if supply_share >= 1.0:
                action_type = "whale_distribute"
            else:
                action_type = "distribute"
        
        # Check if first buyer
        top_accumulators = self.flow_analyzer.get_top_accumulators(mint, 86400, 20)
        is_first_buyer = wallet in [w for w, _ in top_accumulators[:5]]
        
        action = WhaleAction(
            wallet=wallet,
            entity_id=self.entity_resolver.get_entity(wallet).primary_wallet if self.entity_resolver.get_entity(wallet) else None,
            mint=mint,
            action_type=action_type,
            sol_amount=sol_amount,
            token_amount=token_amount,
            price_impact_bps=price_impact_bps,
            timestamp=ts,
            supply_share_pct=supply_share,
            is_first_buyer=is_first_buyer,
            counterparties=[],
        )
        
        with self._lock:
            self._actions.append(action)
        
        # Notify callbacks
        for cb in self._callbacks:
            try:
                cb(action, score)
            except Exception as e:
                print(f"[WhaleDetector] Callback error: {e}")
        
        return action
    
    def get_recent_actions(self, mint: str = None, window_sec: int = 300) -> List[WhaleAction]:
        """Get recent whale actions, optionally filtered by token."""
        cutoff = time.time() - window_sec
        with self._lock:
            actions = [a for a in self._actions if a.timestamp >= cutoff]
            if mint:
                actions = [a for a in actions if a.mint == mint]
            return actions
    
    def get_accumulation_score(self, mint: str, window_sec: int = 300) -> float:
        """
        Get net accumulation score for a token.
        Positive = whales accumulating
        Negative = whales distributing
        """
        actions = self.get_recent_actions(mint, window_sec)
        score = 0.0
        for a in actions:
            weight = a.sol_amount * (1 + a.supply_share_pct / 10)
            if a.action_type in ("accumulate", "whale_accumulate"):
                score += weight
            else:
                score -= weight
        return score


# ══════════════════════════════════════════════════════════════════════════════
# SMART MONEY TRACKER
# ══════════════════════════════════════════════════════════════════════════════

class SmartMoneyTracker:
    """
    Tracks and identifies smart money wallets based on trading performance.
    Updates SMART_MONEY_WALLETS set for use in scoring.
    """
    
    def __init__(self, entity_resolver: EntityResolver, min_trades: int = 10, min_win_rate: float = 55):
        self.entity_resolver = entity_resolver
        self.min_trades = min_trades
        self.min_win_rate = min_win_rate
        self._lock = threading.RLock()
        self._wallet_performance: Dict[str, Dict] = defaultdict(lambda: {
            "buys": [],  # [(mint, entry_price, timestamp)]
            "sells": [],  # [(mint, exit_price, entry_price, pnl_pct, timestamp)]
            "wins": 0,
            "losses": 0,
            "total_pnl_pct": 0.0,
        })
    
    def record_buy(self, wallet: str, mint: str, price: float, timestamp: float = None):
        """Record a buy for a wallet."""
        ts = timestamp or time.time()
        with self._lock:
            self._wallet_performance[wallet]["buys"].append((mint, price, ts))
    
    def record_sell(self, wallet: str, mint: str, exit_price: float, entry_price: float, timestamp: float = None):
        """Record a sell and compute PnL."""
        ts = timestamp or time.time()
        pnl_pct = (exit_price / entry_price - 1) * 100 if entry_price > 0 else 0
        
        with self._lock:
            perf = self._wallet_performance[wallet]
            perf["sells"].append((mint, exit_price, entry_price, pnl_pct, ts))
            perf["total_pnl_pct"] += pnl_pct
            
            if pnl_pct >= 0:
                perf["wins"] += 1
            else:
                perf["losses"] += 1
            
            # Update smart money status
            self._update_smart_money_status(wallet)
    
    def _update_smart_money_status(self, wallet: str):
        """Update smart money status based on performance."""
        perf = self._wallet_performance.get(wallet)
        if not perf:
            return
        
        total_trades = perf["wins"] + perf["losses"]
        if total_trades < self.min_trades:
            return
        
        win_rate = perf["wins"] / total_trades * 100
        
        if win_rate >= self.min_win_rate and perf["total_pnl_pct"] > 0:
            SMART_MONEY_WALLETS.add(wallet)
            self.entity_resolver.label_entity(wallet, "smart-money")
        elif wallet in SMART_MONEY_WALLETS and win_rate < self.min_win_rate - 5:
            SMART_MONEY_WALLETS.discard(wallet)
    
    def get_wallet_performance(self, wallet: str) -> Optional[Dict]:
        """Get performance stats for a wallet."""
        with self._lock:
            perf = self._wallet_performance.get(wallet)
            if not perf:
                return None
            
            total = perf["wins"] + perf["losses"]
            return {
                "total_trades": total,
                "wins": perf["wins"],
                "losses": perf["losses"],
                "win_rate": round(perf["wins"] / total * 100, 1) if total > 0 else 0,
                "total_pnl_pct": round(perf["total_pnl_pct"], 1),
                "is_smart_money": wallet in SMART_MONEY_WALLETS,
            }
    
    def get_smart_money_wallets(self) -> Set[str]:
        """Get current set of smart money wallets."""
        return SMART_MONEY_WALLETS.copy()
    
    def is_smart_money(self, wallet: str) -> bool:
        """Check if wallet is classified as smart money."""
        return wallet in SMART_MONEY_WALLETS


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRATED WHALE DETECTION SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

class WhaleDetectionSystem:
    """
    Integrated whale detection system combining all components.
    This is the main interface for the trading bot.
    """
    
    def __init__(self):
        self.entity_resolver = EntityResolver()
        self.flow_analyzer = FlowAnalyzer()
        self.whale_scorer = WhaleScorer(self.entity_resolver, self.flow_analyzer)
        self.action_detector = WhaleActionDetector(
            self.entity_resolver, self.flow_analyzer, self.whale_scorer
        )
        self.smart_money_tracker = SmartMoneyTracker(self.entity_resolver)
        
        self._lock = threading.RLock()
        self._token_metrics: Dict[str, Dict] = {}
    
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
    ) -> Dict:
        """
        Process a swap event and return whale intelligence.
        
        Returns:
            {
                "whale_action": WhaleAction or None,
                "whale_score": WhaleScore,
                "accumulation_score": float,
                "is_smart_money": bool,
                "triggers": list,
            }
        """
        # Process the swap
        action = self.action_detector.process_swap(
            wallet=wallet,
            mint=mint,
            sol_amount=sol_amount,
            token_amount=token_amount,
            is_buy=is_buy,
            liquidity_usd=liquidity_usd,
            pool_reserves_sol=pool_reserves_sol,
            total_supply=total_supply,
        )
        
        # Track for smart money
        if is_buy:
            self.smart_money_tracker.record_buy(wallet, mint, price)
        
        # Get current whale score
        score = self.whale_scorer.compute_whale_score(
            wallet=wallet,
            mint=mint,
            trade_size_sol=sol_amount,
            liquidity_usd=liquidity_usd,
            pool_reserves_sol=pool_reserves_sol,
        )
        
        # Get accumulation trend
        accumulation_score = self.action_detector.get_accumulation_score(mint, 300)
        
        return {
            "whale_action": action.to_dict() if action else None,
            "whale_score": score.to_dict(),
            "accumulation_score": round(accumulation_score, 2),
            "is_smart_money": self.smart_money_tracker.is_smart_money(wallet),
            "triggers": score.triggers,
        }
    
    def update_token_holders(self, mint: str, holders: Dict[str, int], total_supply: int):
        """Update holder distribution for a token."""
        self.whale_scorer.update_holder_snapshot(mint, holders, total_supply)
    
    def get_token_whale_activity(self, mint: str) -> Dict:
        """Get comprehensive whale activity metrics for a token."""
        snapshot = self.whale_scorer.get_holder_snapshot(mint)
        recent_actions = self.action_detector.get_recent_actions(mint, 3600)
        accumulation = self.action_detector.get_accumulation_score(mint, 300)
        top_accumulators = self.flow_analyzer.get_top_accumulators(mint, 3600, 5)
        
        return {
            "holder_metrics": {
                "top_10_pct": round(snapshot.top_10_pct, 1) if snapshot else 0,
                "gini": round(snapshot.gini_coefficient, 3) if snapshot else 0,
                "hhi": round(snapshot.hhi_index, 1) if snapshot else 0,
            },
            "flow_metrics": {
                "net_flow_1h": round(self.flow_analyzer.get_token_net_flow(mint, 3600), 2),
                "net_flow_5m": round(self.flow_analyzer.get_token_net_flow(mint, 300), 2),
                "accumulation_score": round(accumulation, 2),
            },
            "whale_actions": {
                "count_1h": len(recent_actions),
                "buy_actions": len([a for a in recent_actions if "accumulate" in a.action_type]),
                "sell_actions": len([a for a in recent_actions if "distribute" in a.action_type]),
            },
            "top_accumulators": [
                {"wallet": w[:8] + "...", "net_sol": round(sol, 2)}
                for w, sol in top_accumulators
            ],
        }
    
    def check_whale_entry_signal(self, mint: str, min_score: float = 65) -> Tuple[bool, List[str]]:
        """
        Check if there's a whale entry signal for a token.
        Returns (should_consider, reasons).
        """
        reasons = []
        
        # Check accumulation trend
        acc_score = self.action_detector.get_accumulation_score(mint, 300)
        if acc_score > 0.5:
            reasons.append(f"Whale accumulation +{acc_score:.2f} SOL (5m)")
        
        # Check for smart money buys
        recent_actions = self.action_detector.get_recent_actions(mint, 300)
        smart_money_buys = [
            a for a in recent_actions 
            if "accumulate" in a.action_type and self.smart_money_tracker.is_smart_money(a.wallet)
        ]
        if smart_money_buys:
            reasons.append(f"{len(smart_money_buys)} smart money buys")
        
        # Check for high-score whale triggers
        whale_triggers = self.whale_scorer.get_whale_triggers(mint, min_score)
        if whale_triggers:
            reasons.append(f"{len(whale_triggers)} whale triggers (score>{min_score})")
        
        # Check holder concentration improvement
        snapshot = self.whale_scorer.get_holder_snapshot(mint)
        if snapshot and snapshot.top_10_pct < 40:
            reasons.append(f"Healthy distribution (top10: {snapshot.top_10_pct:.0f}%)")
        
        return len(reasons) >= 2, reasons
    
    def check_whale_exit_warning(self, mint: str) -> Tuple[bool, List[str]]:
        """
        Check if there's a whale exit warning for a token.
        Returns (should_exit, reasons).
        """
        warnings = []
        
        # Check distribution trend
        acc_score = self.action_detector.get_accumulation_score(mint, 300)
        if acc_score < -0.5:
            warnings.append(f"Whale distribution {acc_score:.2f} SOL (5m)")
        
        # Check for large sell actions
        recent_actions = self.action_detector.get_recent_actions(mint, 300)
        large_sells = [
            a for a in recent_actions 
            if "distribute" in a.action_type and a.sol_amount >= 0.5
        ]
        if len(large_sells) >= 2:
            warnings.append(f"{len(large_sells)} large sells detected")
        
        # Check holder concentration spike
        snapshot = self.whale_scorer.get_holder_snapshot(mint)
        if snapshot and snapshot.top_10_pct > 70:
            warnings.append(f"High concentration risk (top10: {snapshot.top_10_pct:.0f}%)")
        
        return len(warnings) >= 2, warnings
    
    def export_state(self) -> Dict:
        """Export system state for persistence."""
        return {
            "entities": self.entity_resolver.export_state(),
            "smart_money_wallets": list(SMART_MONEY_WALLETS),
        }


# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON INSTANCE
# ══════════════════════════════════════════════════════════════════════════════

_whale_system: Optional[WhaleDetectionSystem] = None
_whale_system_lock = threading.Lock()

def get_whale_detection_system() -> WhaleDetectionSystem:
    """Get the singleton whale detection system instance."""
    global _whale_system
    with _whale_system_lock:
        if _whale_system is None:
            _whale_system = WhaleDetectionSystem()
        return _whale_system
