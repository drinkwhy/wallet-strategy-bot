"""
Observability — SolTrader
==========================
Comprehensive monitoring and metrics from the research paper:
- Trading metrics and dashboards
- System health monitoring
- Alerting and notifications
- Performance analytics
"""

import time
import math
import threading
import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Any
from enum import Enum
import statistics

# ══════════════════════════════════════════════════════════════════════════════
# METRICS TYPES
# ══════════════════════════════════════════════════════════════════════════════

class MetricType(Enum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    TIMER = "timer"


class AlertSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class MetricPoint:
    """A single metric data point."""
    name: str
    value: float
    timestamp: float
    labels: Dict[str, str] = field(default_factory=dict)
    metric_type: MetricType = MetricType.GAUGE


@dataclass
class Alert:
    """An alert event."""
    name: str
    severity: AlertSeverity
    message: str
    timestamp: float = field(default_factory=time.time)
    labels: Dict[str, str] = field(default_factory=dict)
    resolved: bool = False
    resolved_at: Optional[float] = None
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "severity": self.severity.value,
            "message": self.message,
            "timestamp": self.timestamp,
            "labels": self.labels,
            "resolved": self.resolved,
        }


# ══════════════════════════════════════════════════════════════════════════════
# METRICS COLLECTORS
# ══════════════════════════════════════════════════════════════════════════════

class Counter:
    """Thread-safe counter metric."""
    
    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self._lock = threading.Lock()
        self._values: Dict[str, float] = defaultdict(float)  # labels_key -> value
    
    def inc(self, value: float = 1, labels: Dict[str, str] = None):
        key = self._labels_key(labels)
        with self._lock:
            self._values[key] += value
    
    def get(self, labels: Dict[str, str] = None) -> float:
        key = self._labels_key(labels)
        with self._lock:
            return self._values.get(key, 0)
    
    def _labels_key(self, labels: Dict[str, str] = None) -> str:
        if not labels:
            return ""
        return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


class Gauge:
    """Thread-safe gauge metric."""
    
    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self._lock = threading.Lock()
        self._values: Dict[str, float] = {}
    
    def set(self, value: float, labels: Dict[str, str] = None):
        key = self._labels_key(labels)
        with self._lock:
            self._values[key] = value
    
    def inc(self, value: float = 1, labels: Dict[str, str] = None):
        key = self._labels_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0) + value
    
    def dec(self, value: float = 1, labels: Dict[str, str] = None):
        self.inc(-value, labels)
    
    def get(self, labels: Dict[str, str] = None) -> float:
        key = self._labels_key(labels)
        with self._lock:
            return self._values.get(key, 0)
    
    def _labels_key(self, labels: Dict[str, str] = None) -> str:
        if not labels:
            return ""
        return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


class Histogram:
    """Thread-safe histogram for distributions."""
    
    DEFAULT_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10]
    
    def __init__(self, name: str, description: str = "", buckets: List[float] = None):
        self.name = name
        self.description = description
        self.buckets = sorted(buckets or self.DEFAULT_BUCKETS)
        self._lock = threading.Lock()
        self._observations: Dict[str, List[float]] = defaultdict(list)
        self._bucket_counts: Dict[str, Dict[float, int]] = defaultdict(lambda: {b: 0 for b in self.buckets})
    
    def observe(self, value: float, labels: Dict[str, str] = None):
        key = self._labels_key(labels)
        with self._lock:
            self._observations[key].append(value)
            for bucket in self.buckets:
                if value <= bucket:
                    self._bucket_counts[key][bucket] += 1
    
    def get_percentile(self, percentile: float, labels: Dict[str, str] = None) -> float:
        key = self._labels_key(labels)
        with self._lock:
            obs = self._observations.get(key, [])
            if not obs:
                return 0
            sorted_obs = sorted(obs)
            idx = min(len(sorted_obs) - 1, max(0, int(len(sorted_obs) * percentile / 100)))
            return sorted_obs[idx]
    
    def get_stats(self, labels: Dict[str, str] = None) -> Dict:
        key = self._labels_key(labels)
        with self._lock:
            obs = self._observations.get(key, [])
            if not obs:
                return {"count": 0, "sum": 0, "avg": 0, "p50": 0, "p95": 0, "p99": 0}
            
            sorted_obs = sorted(obs)
            return {
                "count": len(obs),
                "sum": sum(obs),
                "avg": statistics.mean(obs),
                "p50": sorted_obs[int(len(sorted_obs) * 0.5)],
                "p95": sorted_obs[int(len(sorted_obs) * 0.95)] if len(sorted_obs) > 1 else sorted_obs[0],
                "p99": sorted_obs[int(len(sorted_obs) * 0.99)] if len(sorted_obs) > 1 else sorted_obs[0],
            }
    
    def _labels_key(self, labels: Dict[str, str] = None) -> str:
        if not labels:
            return ""
        return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


class Timer:
    """Context manager for timing operations."""
    
    def __init__(self, histogram: Histogram, labels: Dict[str, str] = None):
        self.histogram = histogram
        self.labels = labels
        self.start_time = None
    
    def __enter__(self):
        self.start_time = time.perf_counter()
        return self
    
    def __exit__(self, *args):
        elapsed = time.perf_counter() - self.start_time
        self.histogram.observe(elapsed, self.labels)


# ══════════════════════════════════════════════════════════════════════════════
# METRICS REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

class MetricsRegistry:
    """Central registry for all metrics."""
    
    def __init__(self):
        self._lock = threading.RLock()
        self._counters: Dict[str, Counter] = {}
        self._gauges: Dict[str, Gauge] = {}
        self._histograms: Dict[str, Histogram] = {}
        self._time_series: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        
    def counter(self, name: str, description: str = "") -> Counter:
        with self._lock:
            if name not in self._counters:
                self._counters[name] = Counter(name, description)
            return self._counters[name]
    
    def gauge(self, name: str, description: str = "") -> Gauge:
        with self._lock:
            if name not in self._gauges:
                self._gauges[name] = Gauge(name, description)
            return self._gauges[name]
    
    def histogram(self, name: str, description: str = "", buckets: List[float] = None) -> Histogram:
        with self._lock:
            if name not in self._histograms:
                self._histograms[name] = Histogram(name, description, buckets)
            return self._histograms[name]
    
    def record_time_series(self, name: str, value: float, labels: Dict[str, str] = None):
        """Record a time series data point."""
        key = f"{name}:{json.dumps(labels or {}, sort_keys=True)}"
        with self._lock:
            self._time_series[key].append({
                "value": value,
                "ts": time.time(),
                "labels": labels or {},
            })
    
    def get_time_series(self, name: str, labels: Dict[str, str] = None, window_sec: int = 3600) -> List[Dict]:
        """Get time series data."""
        key = f"{name}:{json.dumps(labels or {}, sort_keys=True)}"
        cutoff = time.time() - window_sec
        with self._lock:
            series = self._time_series.get(key, deque())
            return [p for p in series if p["ts"] >= cutoff]
    
    def get_all_metrics(self) -> Dict:
        """Export all current metric values."""
        with self._lock:
            return {
                "counters": {name: c._values for name, c in self._counters.items()},
                "gauges": {name: g._values for name, g in self._gauges.items()},
                "histograms": {
                    name: h.get_stats() for name, h in self._histograms.items()
                },
            }


# Global registry
_registry = MetricsRegistry()

def get_registry() -> MetricsRegistry:
    return _registry


# ══════════════════════════════════════════════════════════════════════════════
# TRADING METRICS
# ══════════════════════════════════════════════════════════════════════════════

class TradingMetrics:
    """Trading-specific metrics collector."""
    
    def __init__(self, registry: MetricsRegistry = None):
        self.registry = registry or get_registry()
        self._lock = threading.RLock()
        
        # Initialize metrics
        self.trades_total = self.registry.counter("trades_total", "Total number of trades")
        self.trades_won = self.registry.counter("trades_won", "Number of winning trades")
        self.trades_lost = self.registry.counter("trades_lost", "Number of losing trades")
        
        self.pnl_total = self.registry.gauge("pnl_total_sol", "Total PnL in SOL")
        self.pnl_realized = self.registry.gauge("pnl_realized_sol", "Realized PnL in SOL")
        
        self.positions_open = self.registry.gauge("positions_open", "Number of open positions")
        self.position_value = self.registry.gauge("position_value_sol", "Total position value in SOL")
        
        self.trade_latency = self.registry.histogram(
            "trade_latency_seconds",
            "Trade execution latency",
            buckets=[0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30]
        )
        
        self.slippage = self.registry.histogram(
            "slippage_bps",
            "Trade slippage in basis points",
            buckets=[10, 25, 50, 100, 200, 500, 1000, 2000]
        )
        
        # Trade history
        self._trade_history: deque = deque(maxlen=1000)
        self._position_history: deque = deque(maxlen=500)
    
    def record_trade(
        self,
        user_id: int,
        mint: str,
        side: str,  # "buy" or "sell"
        sol_amount: float,
        price: float,
        pnl_sol: float = 0,
        pnl_pct: float = 0,
        latency_sec: float = 0,
        slippage_bps: int = 0,
        success: bool = True,
    ):
        """Record a trade."""
        labels = {"user_id": str(user_id), "side": side}
        
        self.trades_total.inc(labels=labels)
        
        if side == "sell" and success:
            if pnl_sol >= 0:
                self.trades_won.inc(labels=labels)
            else:
                self.trades_lost.inc(labels=labels)
            
            self.pnl_realized.inc(pnl_sol, labels={"user_id": str(user_id)})
        
        if latency_sec > 0:
            self.trade_latency.observe(latency_sec, labels=labels)
        
        if slippage_bps > 0:
            self.slippage.observe(slippage_bps, labels=labels)
        
        # Record to history
        with self._lock:
            self._trade_history.append({
                "user_id": user_id,
                "mint": mint,
                "side": side,
                "sol": sol_amount,
                "price": price,
                "pnl_sol": pnl_sol,
                "pnl_pct": pnl_pct,
                "latency_sec": latency_sec,
                "slippage_bps": slippage_bps,
                "success": success,
                "ts": time.time(),
            })
    
    def update_positions(self, user_id: int, positions: List[Dict]):
        """Update position metrics."""
        labels = {"user_id": str(user_id)}
        
        self.positions_open.set(len(positions), labels=labels)
        
        total_value = sum(p.get("entry_sol", 0) for p in positions)
        self.position_value.set(total_value, labels=labels)
    
    def get_user_stats(self, user_id: int, window_sec: int = 86400) -> Dict:
        """Get trading stats for a user."""
        cutoff = time.time() - window_sec
        
        with self._lock:
            trades = [
                t for t in self._trade_history
                if t["user_id"] == user_id and t["ts"] >= cutoff
            ]
        
        if not trades:
            return {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0,
                "total_pnl_sol": 0,
                "avg_pnl_pct": 0,
                "best_trade_pct": 0,
                "worst_trade_pct": 0,
                "avg_latency_sec": 0,
                "avg_slippage_bps": 0,
            }
        
        sells = [t for t in trades if t["side"] == "sell" and t["success"]]
        wins = [t for t in sells if t["pnl_sol"] >= 0]
        losses = [t for t in sells if t["pnl_sol"] < 0]
        
        pnl_values = [t["pnl_pct"] for t in sells]
        latencies = [t["latency_sec"] for t in trades if t["latency_sec"] > 0]
        slippages = [t["slippage_bps"] for t in trades if t["slippage_bps"] > 0]
        
        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(sells) * 100, 1) if sells else 0,
            "total_pnl_sol": round(sum(t["pnl_sol"] for t in sells), 4),
            "avg_pnl_pct": round(statistics.mean(pnl_values), 1) if pnl_values else 0,
            "best_trade_pct": round(max(pnl_values), 1) if pnl_values else 0,
            "worst_trade_pct": round(min(pnl_values), 1) if pnl_values else 0,
            "avg_latency_sec": round(statistics.mean(latencies), 2) if latencies else 0,
            "avg_slippage_bps": round(statistics.mean(slippages), 0) if slippages else 0,
        }


# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM HEALTH METRICS
# ══════════════════════════════════════════════════════════════════════════════

class SystemHealthMetrics:
    """System health monitoring."""
    
    def __init__(self, registry: MetricsRegistry = None):
        self.registry = registry or get_registry()
        
        # RPC metrics
        self.rpc_requests = self.registry.counter("rpc_requests_total", "Total RPC requests")
        self.rpc_errors = self.registry.counter("rpc_errors_total", "RPC errors")
        self.rpc_latency = self.registry.histogram(
            "rpc_latency_ms",
            "RPC request latency in milliseconds",
            buckets=[10, 25, 50, 100, 250, 500, 1000, 2500, 5000]
        )
        
        # API metrics
        self.api_requests = self.registry.counter("api_requests_total", "API requests")
        self.api_latency = self.registry.histogram(
            "api_latency_ms",
            "API latency",
            buckets=[5, 10, 25, 50, 100, 250, 500, 1000]
        )
        
        # Bot metrics
        self.bots_active = self.registry.gauge("bots_active", "Number of active bots")
        self.evaluations_total = self.registry.counter("token_evaluations_total", "Token evaluations")
        self.signals_triggered = self.registry.counter("signals_triggered_total", "Signals triggered")
        
        # Memory/resource metrics
        self.memory_usage_mb = self.registry.gauge("memory_usage_mb", "Memory usage in MB")
        self.db_connections = self.registry.gauge("db_connections_active", "Active DB connections")
    
    def record_rpc_call(self, source: str, method: str, success: bool, latency_ms: float):
        """Record an RPC call."""
        labels = {"source": source, "method": method}
        self.rpc_requests.inc(labels=labels)
        if not success:
            self.rpc_errors.inc(labels=labels)
        self.rpc_latency.observe(latency_ms, labels=labels)
    
    def record_api_call(self, endpoint: str, method: str, latency_ms: float):
        """Record an API call."""
        labels = {"endpoint": endpoint, "method": method}
        self.api_requests.inc(labels=labels)
        self.api_latency.observe(latency_ms, labels=labels)
    
    def get_health_summary(self) -> Dict:
        """Get system health summary."""
        rpc_stats = self.rpc_latency.get_stats()
        
        return {
            "rpc": {
                "total_requests": self.rpc_requests.get(),
                "error_count": self.rpc_errors.get(),
                "error_rate": round(
                    self.rpc_errors.get() / max(self.rpc_requests.get(), 1) * 100, 1
                ),
                "latency_p50_ms": round(rpc_stats.get("p50", 0), 0),
                "latency_p95_ms": round(rpc_stats.get("p95", 0), 0),
            },
            "bots_active": self.bots_active.get(),
            "evaluations": self.evaluations_total.get(),
            "signals": self.signals_triggered.get(),
        }


# ══════════════════════════════════════════════════════════════════════════════
# ALERTING
# ══════════════════════════════════════════════════════════════════════════════

class AlertRule:
    """Alert rule definition."""
    
    def __init__(
        self,
        name: str,
        condition: Callable[[], bool],
        severity: AlertSeverity,
        message_template: str,
        cooldown_sec: int = 300,
    ):
        self.name = name
        self.condition = condition
        self.severity = severity
        self.message_template = message_template
        self.cooldown_sec = cooldown_sec
        self.last_fired: Optional[float] = None
        self.is_firing = False
    
    def check(self) -> Optional[Alert]:
        """Check if alert should fire."""
        try:
            should_fire = self.condition()
        except Exception:
            return None
        
        now = time.time()
        
        if should_fire:
            if not self.is_firing:
                # New alert
                if self.last_fired and now - self.last_fired < self.cooldown_sec:
                    return None  # Still in cooldown
                
                self.is_firing = True
                self.last_fired = now
                return Alert(
                    name=self.name,
                    severity=self.severity,
                    message=self.message_template,
                )
        else:
            if self.is_firing:
                # Alert resolved
                self.is_firing = False
                return Alert(
                    name=self.name,
                    severity=AlertSeverity.INFO,
                    message=f"[RESOLVED] {self.message_template}",
                    resolved=True,
                    resolved_at=now,
                )
        
        return None


class AlertManager:
    """Manages alerts and notifications."""
    
    def __init__(self):
        self._lock = threading.RLock()
        self._rules: List[AlertRule] = []
        self._active_alerts: Dict[str, Alert] = {}
        self._alert_history: deque = deque(maxlen=1000)
        self._callbacks: List[Callable[[Alert], None]] = []
    
    def add_rule(self, rule: AlertRule):
        """Add an alert rule."""
        with self._lock:
            self._rules.append(rule)
    
    def register_callback(self, callback: Callable[[Alert], None]):
        """Register a callback for alerts."""
        self._callbacks.append(callback)
    
    def check_rules(self):
        """Check all rules and fire alerts."""
        for rule in self._rules:
            alert = rule.check()
            if alert:
                self._handle_alert(alert)
    
    def _handle_alert(self, alert: Alert):
        """Handle a new alert."""
        with self._lock:
            if alert.resolved:
                if alert.name in self._active_alerts:
                    del self._active_alerts[alert.name]
            else:
                self._active_alerts[alert.name] = alert
            
            self._alert_history.append(alert)
        
        # Notify callbacks
        for cb in self._callbacks:
            try:
                cb(alert)
            except Exception as e:
                print(f"[AlertManager] Callback error: {e}")
    
    def fire_alert(self, name: str, severity: AlertSeverity, message: str, labels: Dict = None):
        """Manually fire an alert."""
        alert = Alert(
            name=name,
            severity=severity,
            message=message,
            labels=labels or {},
        )
        self._handle_alert(alert)
    
    def get_active_alerts(self) -> List[Alert]:
        """Get all active alerts."""
        with self._lock:
            return list(self._active_alerts.values())
    
    def get_alert_history(self, window_sec: int = 86400) -> List[Dict]:
        """Get alert history."""
        cutoff = time.time() - window_sec
        with self._lock:
            return [a.to_dict() for a in self._alert_history if a.timestamp >= cutoff]


# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD DATA
# ══════════════════════════════════════════════════════════════════════════════

class DashboardDataProvider:
    """Provides data for dashboard rendering."""
    
    def __init__(
        self,
        trading_metrics: TradingMetrics,
        system_metrics: SystemHealthMetrics,
        alert_manager: AlertManager,
    ):
        self.trading = trading_metrics
        self.system = system_metrics
        self.alerts = alert_manager
        self.registry = get_registry()
    
    def get_overview(self, user_id: int = None) -> Dict:
        """Get dashboard overview data."""
        data = {
            "system_health": self.system.get_health_summary(),
            "active_alerts": [a.to_dict() for a in self.alerts.get_active_alerts()],
        }
        
        if user_id:
            data["user_stats"] = self.trading.get_user_stats(user_id)
        
        return data
    
    def get_trading_dashboard(self, user_id: int, window_sec: int = 86400) -> Dict:
        """Get trading dashboard data."""
        stats = self.trading.get_user_stats(user_id, window_sec)
        
        # Get time series for charts
        pnl_series = self.registry.get_time_series(
            "pnl_realized_sol",
            labels={"user_id": str(user_id)},
            window_sec=window_sec,
        )
        
        return {
            "stats": stats,
            "pnl_history": pnl_series,
            "latency_stats": self.trading.trade_latency.get_stats({"user_id": str(user_id)}),
            "slippage_stats": self.trading.slippage.get_stats({"user_id": str(user_id)}),
        }
    
    def get_whale_radar(self, whale_system) -> Dict:
        """Get whale activity radar data."""
        # This integrates with the whale detection system
        return {
            "recent_whale_actions": [],
            "top_accumulators": [],
            "smart_money_activity": [],
        }
    
    def get_risk_dashboard(self, risk_engine, user_id: int) -> Dict:
        """Get risk dashboard data."""
        cb_state = risk_engine.get_circuit_breaker_state(user_id)
        
        return {
            "circuit_breaker": cb_state,
            "recent_risk_events": [],
            "token_risk_cache_size": 0,
        }
    
    def get_execution_dashboard(self, mev_system) -> Dict:
        """Get execution/MEV dashboard data."""
        return {
            **mev_system.get_health_report(),
        }


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRATED OBSERVABILITY SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

class ObservabilitySystem:
    """
    Integrated observability system.
    Main interface for monitoring.
    """
    
    def __init__(self):
        self.registry = get_registry()
        self.trading_metrics = TradingMetrics(self.registry)
        self.system_metrics = SystemHealthMetrics(self.registry)
        self.alert_manager = AlertManager()
        self.dashboard = DashboardDataProvider(
            self.trading_metrics,
            self.system_metrics,
            self.alert_manager,
        )
        
        self._setup_default_alerts()
    
    def _setup_default_alerts(self):
        """Setup default alert rules."""
        # High RPC error rate
        self.alert_manager.add_rule(AlertRule(
            name="high_rpc_error_rate",
            condition=lambda: (
                self.system_metrics.rpc_errors.get() / max(self.system_metrics.rpc_requests.get(), 1) > 0.3
            ),
            severity=AlertSeverity.WARNING,
            message_template="RPC error rate above 30%",
        ))
        
        # High RPC latency
        self.alert_manager.add_rule(AlertRule(
            name="high_rpc_latency",
            condition=lambda: self.system_metrics.rpc_latency.get_percentile(95) > 3000,
            severity=AlertSeverity.WARNING,
            message_template="RPC p95 latency above 3 seconds",
        ))
    
    def record_trade(self, **kwargs):
        """Record a trade."""
        self.trading_metrics.record_trade(**kwargs)
    
    def record_rpc_call(self, **kwargs):
        """Record an RPC call."""
        self.system_metrics.record_rpc_call(**kwargs)
    
    def fire_alert(self, name: str, severity: AlertSeverity, message: str, labels: Dict = None):
        """Fire an alert."""
        self.alert_manager.fire_alert(name, severity, message, labels)
    
    def register_alert_callback(self, callback: Callable[[Alert], None]):
        """Register alert callback."""
        self.alert_manager.register_callback(callback)
    
    def check_alerts(self):
        """Check all alert rules."""
        self.alert_manager.check_rules()
    
    def get_dashboard_overview(self, user_id: int = None) -> Dict:
        """Get dashboard overview."""
        return self.dashboard.get_overview(user_id)
    
    def get_metrics_export(self) -> Dict:
        """Export all metrics."""
        return self.registry.get_all_metrics()


# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON
# ══════════════════════════════════════════════════════════════════════════════

_observability: Optional[ObservabilitySystem] = None
_obs_lock = threading.Lock()

def get_observability() -> ObservabilitySystem:
    """Get singleton observability system."""
    global _observability
    with _obs_lock:
        if _observability is None:
            _observability = ObservabilitySystem()
        return _observability
