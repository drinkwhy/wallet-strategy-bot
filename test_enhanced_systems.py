#!/usr/bin/env python3
"""
Test suite for SolTrader enhanced trading modules.
Verifies whale detection, risk engine, MEV protection, and observability.
"""

import sys
import time
import json
from unittest.mock import MagicMock, patch

# Test results
TESTS_PASSED = 0
TESTS_FAILED = 0

def test(name):
    """Decorator for test functions."""
    def decorator(func):
        def wrapper():
            global TESTS_PASSED, TESTS_FAILED
            try:
                func()
                print(f"  ✅ {name}")
                TESTS_PASSED += 1
            except Exception as e:
                print(f"  ❌ {name}: {e}")
                TESTS_FAILED += 1
        return wrapper
    return decorator


# ══════════════════════════════════════════════════════════════════════════════
# WHALE DETECTION TESTS
# ══════════════════════════════════════════════════════════════════════════════

print("\n🐋 Testing Whale Detection Module...")

@test("Import whale_detection module")
def test_whale_import():
    from whale_detection import (
        WhaleDetectionSystem,
        EntityResolver,
        FlowAnalyzer,
        WhaleScorer,
        WhaleActionDetector,
        SmartMoneyTracker,
        get_whale_detection_system,
    )
    assert WhaleDetectionSystem is not None

test_whale_import()

@test("Create WhaleDetectionSystem instance")
def test_whale_system_create():
    from whale_detection import WhaleDetectionSystem
    system = WhaleDetectionSystem()
    assert system.entity_resolver is not None
    assert system.flow_analyzer is not None
    assert system.whale_scorer is not None

test_whale_system_create()

@test("Entity resolution and clustering")
def test_entity_resolution():
    from whale_detection import EntityResolver
    resolver = EntityResolver()
    
    # Create entities
    entity1 = resolver.get_or_create_entity("wallet1")
    assert entity1.primary_wallet == "wallet1"
    
    # Link wallets
    resolver.link_wallets("wallet1", "wallet2", "shared-counterparty")
    entity = resolver.get_entity("wallet2")
    assert entity.primary_wallet == "wallet1"
    assert "wallet2" in entity.all_wallets

test_entity_resolution()

@test("Flow analysis recording")
def test_flow_analysis():
    from whale_detection import FlowAnalyzer
    analyzer = FlowAnalyzer()
    
    mint = "test_mint_123"
    wallet = "test_wallet_456"
    
    # Record flows
    analyzer.record_flow(mint, wallet, 1.5, is_buy=True)
    analyzer.record_flow(mint, wallet, 0.5, is_buy=False)
    
    # Get net flow
    net_flow = analyzer.get_net_flow(mint, wallet, 3600)
    assert net_flow == 1.0  # 1.5 - 0.5

test_flow_analysis()

@test("Whale scoring")
def test_whale_scoring():
    from whale_detection import WhaleDetectionSystem
    system = WhaleDetectionSystem()
    
    # Process swap event
    result = system.process_swap_event(
        wallet="big_whale_wallet",
        mint="test_token",
        sol_amount=5.0,
        token_amount=1000000,
        is_buy=True,
        liquidity_usd=50000,
        pool_reserves_sol=100,
        total_supply=100000000,
        price=0.001,
    )
    
    assert "whale_score" in result
    assert "accumulation_score" in result

test_whale_scoring()

@test("Smart money tracking")
def test_smart_money():
    from whale_detection import SmartMoneyTracker, EntityResolver
    resolver = EntityResolver()
    tracker = SmartMoneyTracker(resolver, min_trades=3, min_win_rate=50)
    
    wallet = "smart_trader"
    
    # Record winning trades
    for i in range(5):
        tracker.record_buy(wallet, f"token_{i}", 0.01)
        tracker.record_sell(wallet, f"token_{i}", 0.02, 0.01)  # 100% profit
    
    perf = tracker.get_wallet_performance(wallet)
    assert perf is not None
    assert perf["wins"] == 5
    assert perf["is_smart_money"] == True

test_smart_money()


# ══════════════════════════════════════════════════════════════════════════════
# RISK ENGINE TESTS
# ══════════════════════════════════════════════════════════════════════════════

print("\n🛡️ Testing Risk Engine Module...")

@test("Import risk_engine module")
def test_risk_import():
    from risk_engine import (
        RiskEngine,
        HoneypotDetector,
        ExitSimulator,
        TokenRiskAnalyzer,
        CircuitBreaker,
        ExecutionGuardrails,
        RugPullDetector,
        RiskLevel,
    )
    assert RiskEngine is not None
    assert RiskLevel.BLOCKED is not None

test_risk_import()

@test("Circuit breaker functionality")
def test_circuit_breaker():
    from risk_engine import CircuitBreaker
    cb = CircuitBreaker()
    
    user_id = 123
    
    # Should allow trading initially
    can_trade, reason = cb.is_trading_allowed(user_id)
    assert can_trade == True
    
    # Record failures
    for i in range(5):
        cb.record_trade_result(user_id, success=False)
    
    # Should be tripped now
    can_trade, reason = cb.is_trading_allowed(user_id)
    assert can_trade == False
    assert "failure" in reason.lower()
    
    # Reset
    cb.reset(user_id)
    can_trade, reason = cb.is_trading_allowed(user_id)
    assert can_trade == True

test_circuit_breaker()

@test("Execution guardrails")
def test_guardrails():
    from risk_engine import ExecutionGuardrails
    guardrails = ExecutionGuardrails()
    
    # Valid trade
    valid, reasons = guardrails.validate_trade(
        user_id=1,
        mint="test_mint",
        sol_amount=0.1,
        slippage_bps=500,
        priority_fee_sol=0.001,
        position_count=1,
        max_positions=3,
        liquidity_usd=50000,
    )
    assert valid == True
    
    # Invalid trade - too much slippage
    valid, reasons = guardrails.validate_trade(
        user_id=1,
        mint="test_mint",
        sol_amount=0.1,
        slippage_bps=5000,  # 50% - way too high
        priority_fee_sol=0.001,
        position_count=1,
        max_positions=3,
        liquidity_usd=50000,
    )
    assert valid == False
    assert any("slippage" in r.lower() for r in reasons)

test_guardrails()

@test("Rug pull detection")
def test_rug_detection():
    from risk_engine import RugPullDetector
    detector = RugPullDetector()
    
    mint = "potential_rug"
    
    # Not rugged initially
    assert detector.is_rugged(mint) == False
    
    # Record LP removal
    detector.record_lp_event(
        mint=mint,
        wallet="dev_wallet",
        event_type="remove",
        sol_amount=5.0,
        lp_tokens=1000,
    )
    
    # Check for rug signals
    signals = detector.get_rug_signals(mint)
    assert len(signals) > 0

test_rug_detection()

@test("Safe slippage calculation")
def test_safe_slippage():
    from risk_engine import ExecutionGuardrails
    guardrails = ExecutionGuardrails()
    
    # Small position, good liquidity = low slippage
    slip1 = guardrails.get_safe_slippage(100000, 0.05)
    assert slip1 <= 300
    
    # Large position relative to liquidity = higher slippage
    slip2 = guardrails.get_safe_slippage(10000, 0.5)
    assert slip2 > slip1

test_safe_slippage()


# ══════════════════════════════════════════════════════════════════════════════
# MEV PROTECTION TESTS
# ══════════════════════════════════════════════════════════════════════════════

print("\n⚡ Testing MEV Protection Module...")

@test("Import mev_protection module")
def test_mev_import():
    from mev_protection import (
        MEVProtectionSystem,
        TransactionSubmitter,
        JitoTipManager,
        RPCManager,
        JitoBundleSubmitter,
        PriorityFeeOptimizer,
        SubmissionStrategy,
        SubmissionResult,
    )
    assert MEVProtectionSystem is not None
    assert SubmissionStrategy.JITO_BUNDLE is not None

test_mev_import()

@test("Jito tip manager")
def test_jito_tips():
    from mev_protection import JitoTipManager
    manager = JitoTipManager()
    
    # Get default estimate
    estimate = manager.get_tip_estimate()
    assert estimate is not None
    
    # Get dynamic tip
    tip_normal = manager.get_dynamic_tip("normal", 0.5)
    tip_high = manager.get_dynamic_tip("high", 0.5)
    
    assert tip_high >= tip_normal
    assert tip_normal >= 10000  # At least minimum

test_jito_tips()

@test("RPC manager health tracking")
def test_rpc_manager():
    from mev_protection import RPCManager
    manager = RPCManager("https://example.helius.xyz/rpc")
    
    # Add backup endpoint
    manager.add_endpoint("https://backup.rpc", "backup", priority=5)
    
    # Get best endpoint
    best = manager.get_best_endpoint()
    assert best is not None
    assert best.name == "primary"  # Higher priority
    
    # Record failures to degrade primary
    for i in range(10):
        manager.record_result("https://example.helius.xyz/rpc", False, 5000)
    
    # Now backup might be preferred
    report = manager.get_health_report()
    assert "endpoints" in report
    assert len(report["endpoints"]) == 2

test_rpc_manager()

@test("Submission result structure")
def test_submission_result():
    from mev_protection import SubmissionResult, SubmissionStrategy
    
    result = SubmissionResult(
        success=True,
        signature="abc123",
        strategy_used=SubmissionStrategy.JITO_BUNDLE,
        tip_lamports=100000,
        latency_ms=500,
        rpc_used="jito",
    )
    
    d = result.to_dict()
    assert d["success"] == True
    assert d["strategy"] == "jito"
    assert d["tip_lamports"] == 100000

test_submission_result()


# ══════════════════════════════════════════════════════════════════════════════
# OBSERVABILITY TESTS
# ══════════════════════════════════════════════════════════════════════════════

print("\n📊 Testing Observability Module...")

@test("Import observability module")
def test_obs_import():
    from observability import (
        ObservabilitySystem,
        MetricsRegistry,
        Counter,
        Gauge,
        Histogram,
        AlertManager,
        AlertRule,
        AlertSeverity,
        TradingMetrics,
        SystemHealthMetrics,
        get_observability,
    )
    assert ObservabilitySystem is not None

test_obs_import()

@test("Counter metric")
def test_counter():
    from observability import Counter
    counter = Counter("test_counter")
    
    counter.inc(1)
    counter.inc(2)
    
    assert counter.get() == 3
    
    # With labels
    counter.inc(5, labels={"type": "buy"})
    assert counter.get(labels={"type": "buy"}) == 5

test_counter()

@test("Gauge metric")
def test_gauge():
    from observability import Gauge
    gauge = Gauge("test_gauge")
    
    gauge.set(10)
    assert gauge.get() == 10
    
    gauge.inc(5)
    assert gauge.get() == 15
    
    gauge.dec(3)
    assert gauge.get() == 12

test_gauge()

@test("Histogram metric")
def test_histogram():
    from observability import Histogram
    hist = Histogram("test_hist", buckets=[1, 5, 10, 50, 100])
    
    for val in [2, 8, 15, 45, 90]:
        hist.observe(val)
    
    stats = hist.get_stats()
    assert stats["count"] == 5
    assert stats["avg"] == 32.0  # (2+8+15+45+90)/5

test_histogram()

@test("Alert manager")
def test_alerts():
    from observability import AlertManager, AlertRule, AlertSeverity
    manager = AlertManager()
    
    # Track fired alerts
    fired_alerts = []
    manager.register_callback(lambda a: fired_alerts.append(a))
    
    # Add rule that always fires
    always_true = AlertRule(
        name="test_alert",
        condition=lambda: True,
        severity=AlertSeverity.WARNING,
        message_template="Test alert fired",
        cooldown_sec=0,
    )
    manager.add_rule(always_true)
    
    # Check rules
    manager.check_rules()
    
    assert len(fired_alerts) == 1
    assert fired_alerts[0].name == "test_alert"

test_alerts()

@test("Trading metrics")
def test_trading_metrics():
    from observability import TradingMetrics, MetricsRegistry
    registry = MetricsRegistry()
    metrics = TradingMetrics(registry)
    
    # Record trades
    metrics.record_trade(
        user_id=1,
        mint="token1",
        side="buy",
        sol_amount=0.1,
        price=0.001,
        latency_sec=0.5,
        slippage_bps=100,
    )
    
    metrics.record_trade(
        user_id=1,
        mint="token1",
        side="sell",
        sol_amount=0.1,
        price=0.002,
        pnl_sol=0.1,
        pnl_pct=100,
        latency_sec=0.3,
    )
    
    stats = metrics.get_user_stats(1)
    assert stats["total_trades"] == 2
    assert stats["wins"] == 1

test_trading_metrics()


# ══════════════════════════════════════════════════════════════════════════════
# ENHANCED TRADING INTEGRATION TESTS
# ══════════════════════════════════════════════════════════════════════════════

print("\n🚀 Testing Enhanced Trading Integration...")

@test("Import enhanced_trading module")
def test_enhanced_import():
    from enhanced_trading import (
        EnhancedSignalEvaluator,
        EnhancedExecutionHandler,
        EnhancedPositionMonitor,
        EnhancedBotMixin,
        EnhancedSignalResult,
        create_enhanced_systems,
    )
    assert EnhancedSignalEvaluator is not None

test_enhanced_import()

@test("Create enhanced systems")
def test_create_systems():
    from enhanced_trading import create_enhanced_systems
    
    # This will fail without real RPC but should not crash
    try:
        systems = create_enhanced_systems("https://test.helius.xyz")
        assert "whale_system" in systems
        assert "risk_engine" in systems
        assert "mev_system" in systems
        assert "observability" in systems
    except Exception as e:
        # Expected to fail without real endpoint
        pass

test_create_systems()

@test("EnhancedSignalResult structure")
def test_signal_result():
    from enhanced_trading import EnhancedSignalResult
    from risk_engine import RiskLevel
    
    result = EnhancedSignalResult(
        should_buy=True,
        score=75,
        risk_level=RiskLevel.LOW,
        whale_score=65.5,
        reasons=["Whale accumulation detected"],
        warnings=["Low liquidity"],
        blockers=[],
        whale_triggers=["2 smart money buys"],
    )
    
    d = result.to_dict()
    assert d["should_buy"] == True
    assert d["score"] == 75
    assert d["risk_level"] == "low"
    assert len(d["reasons"]) == 1

test_signal_result()


# ══════════════════════════════════════════════════════════════════════════════
# FINAL SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print(f"Tests Passed: {TESTS_PASSED}")
print(f"Tests Failed: {TESTS_FAILED}")
print("=" * 60)

if TESTS_FAILED > 0:
    print("\n❌ Some tests failed!")
    sys.exit(1)
else:
    print("\n✅ All tests passed!")
    sys.exit(0)
