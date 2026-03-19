import json
import unittest
from datetime import UTC, datetime, timedelta

from backtest_engine import simulate_backtest, simulate_event_tape_backtest, simulate_policy_comparison


class BacktestEngineTests(unittest.TestCase):
    def test_backtest_opens_and_closes_trade(self):
        now = datetime.now(UTC)
        rows = [
            {
                "mint": "mint-1",
                "name": "Runner",
                "price": 1.0,
                "created_at": now,
                "feature_json": json.dumps({
                    "price": 1.0,
                    "liq": 15000,
                    "vol": 22000,
                    "mc": 80000,
                    "score": 70,
                    "age_min": 1,
                    "green_lights": 2,
                    "narrative_score": 25,
                    "holder_growth_1h": 60,
                    "volume_spike_ratio": 10,
                    "threat_risk_score": 8,
                    "can_exit": True,
                }),
            },
            {
                "mint": "mint-1",
                "name": "Runner",
                "price": 2.2,
                "created_at": now + timedelta(minutes=4),
                "feature_json": json.dumps({
                    "price": 2.2,
                    "liq": 22000,
                    "vol": 40000,
                    "mc": 160000,
                    "score": 82,
                    "age_min": 5,
                    "green_lights": 3,
                    "narrative_score": 30,
                    "holder_growth_1h": 75,
                    "volume_spike_ratio": 12,
                    "threat_risk_score": 10,
                    "can_exit": True,
                }),
            },
        ]
        settings = {
            "balanced": {
                "min_liq": 5000,
                "min_mc": 5000,
                "max_mc": 250000,
                "min_vol": 3000,
                "min_score": 30,
                "max_age_min": 120,
                "min_green_lights": 1,
                "min_narrative_score": 12,
                "min_holder_growth_pct": 20,
                "min_volume_spike_mult": 4,
                "anti_rug": True,
                "tp2_mult": 2.0,
                "stop_loss": 0.7,
                "time_stop_min": 30,
            }
        }
        result = simulate_backtest(1, rows, settings)
        self.assertEqual(result["summary"]["trades_closed"], 1)
        trade = result["trades"][0]
        self.assertEqual(trade.strategy_name, "balanced")
        self.assertGreater(trade.realized_pnl_pct, 100)
        self.assertEqual(trade.exit_reason, "take_profit")

    def test_event_tape_backtest_replays_market_events(self):
        now = datetime.now(UTC)
        rows = [
            {
                "mint": "mint-evt",
                "name": "TapeRunner",
                "price": 1.0,
                "mc": 70000,
                "liq": 14000,
                "vol": 18000,
                "age_min": 1,
                "event_type": "token_discovered",
                "source": "scanner",
                "created_at": now,
                "payload_json": json.dumps({
                    "mint": "mint-evt",
                    "name": "TapeRunner",
                    "price": 1.0,
                    "mc": 70000,
                    "liq": 14000,
                    "vol": 18000,
                    "age_min": 1,
                    "score": 72,
                    "green_lights": 2,
                    "narrative_score": 18,
                    "intel": {
                        "holder_growth_1h": 48,
                        "volume_spike_ratio": 8,
                        "threat_risk_score": 10,
                        "can_exit": True,
                        "unique_buyer_count": 8,
                        "unique_seller_count": 2,
                        "first_buyer_count": 7,
                        "smart_wallet_buys": 1,
                        "smart_wallet_first10": 1,
                        "total_buy_sol": 6.0,
                        "total_sell_sol": 1.0,
                        "net_flow_sol": 5.0,
                        "buy_sell_ratio": 4.0,
                    },
                }),
            },
            {
                "mint": "mint-evt",
                "name": None,
                "price": None,
                "mc": None,
                "liq": None,
                "vol": None,
                "age_min": None,
                "event_type": "wallet_buy",
                "source": "helius",
                "created_at": now + timedelta(minutes=1),
                "payload_json": json.dumps({
                    "wallet": "buyer-1",
                    "sol": 2.4,
                    "token": 1200,
                    "smart_wallet": True,
                }),
            },
            {
                "mint": "mint-evt",
                "name": None,
                "price": 1.35,
                "mc": None,
                "liq": 16500,
                "vol": None,
                "age_min": None,
                "event_type": "liquidity_add",
                "source": "execution_tape",
                "created_at": now + timedelta(minutes=2),
                "payload_json": json.dumps({
                    "delta_liq": 2500,
                    "delta_pct": 17.8,
                    "current_liq": 16500,
                    "current_price": 1.35,
                }),
            },
            {
                "mint": "mint-evt",
                "name": "TapeRunner",
                "price": 2.15,
                "mc": 145000,
                "liq": 17500,
                "vol": 41000,
                "age_min": 4,
                "event_type": "price_breakout",
                "source": "scanner",
                "created_at": now + timedelta(minutes=3),
                "payload_json": json.dumps({
                    "mint": "mint-evt",
                    "name": "TapeRunner",
                    "price": 2.15,
                    "mc": 145000,
                    "liq": 17500,
                    "vol": 41000,
                    "age_min": 4,
                    "score": 84,
                    "green_lights": 3,
                    "narrative_score": 24,
                    "intel": {
                        "holder_growth_1h": 75,
                        "volume_spike_ratio": 11,
                        "threat_risk_score": 12,
                        "can_exit": True,
                        "unique_buyer_count": 14,
                        "unique_seller_count": 3,
                        "first_buyer_count": 9,
                        "smart_wallet_buys": 2,
                        "smart_wallet_first10": 1,
                        "total_buy_sol": 12.0,
                        "total_sell_sol": 1.4,
                        "net_flow_sol": 10.6,
                        "buy_sell_ratio": 4.67,
                    },
                }),
            },
        ]
        settings = {
            "balanced": {
                "min_liq": 5000,
                "min_mc": 5000,
                "max_mc": 250000,
                "min_vol": 3000,
                "min_score": 30,
                "max_age_min": 120,
                "min_green_lights": 1,
                "min_narrative_score": 12,
                "min_holder_growth_pct": 20,
                "min_volume_spike_mult": 4,
                "anti_rug": True,
                "tp2_mult": 2.0,
                "stop_loss": 0.7,
                "time_stop_min": 30,
            }
        }
        result = simulate_event_tape_backtest(2, rows, settings)
        self.assertEqual(result["summary"]["replay_mode"], "event_tape")
        self.assertEqual(result["summary"]["events_processed"], 4)
        self.assertEqual(result["summary"]["event_type_counts"]["price_breakout"], 1)
        self.assertEqual(result["summary"]["event_type_counts"]["wallet_buy"], 1)
        self.assertEqual(result["summary"]["event_type_counts"]["liquidity_add"], 1)
        self.assertEqual(result["summary"]["trades_closed"], 1)
        self.assertGreater(result["trades"][0].realized_pnl_pct, 100)

    def test_policy_comparison_compares_rules_and_models(self):
        now = datetime.now(UTC)
        snapshot_rows = [
            {
                "mint": "mint-cmp",
                "name": "Compare",
                "price": 1.0,
                "created_at": now,
                "feature_json": json.dumps({
                    "price": 1.0,
                    "liq": 15000,
                    "vol": 22000,
                    "mc": 80000,
                    "composite_score": 78,
                    "confidence": 0.74,
                    "score": 70,
                    "age_min": 1,
                    "green_lights": 2,
                    "narrative_score": 25,
                    "holder_growth_1h": 60,
                    "volume_spike_ratio": 10,
                    "threat_risk_score": 8,
                    "buy_sell_ratio": 3.2,
                    "net_flow_sol": 9.5,
                    "smart_wallet_buys": 2,
                    "unique_buyer_count": 15,
                    "liquidity_drop_pct": 4,
                    "can_exit": True,
                }),
            },
            {
                "mint": "mint-cmp",
                "name": "Compare",
                "price": 2.1,
                "created_at": now + timedelta(minutes=4),
                "feature_json": json.dumps({
                    "price": 2.1,
                    "liq": 22000,
                    "vol": 40000,
                    "mc": 150000,
                    "composite_score": 84,
                    "confidence": 0.8,
                    "score": 82,
                    "age_min": 5,
                    "green_lights": 3,
                    "narrative_score": 30,
                    "holder_growth_1h": 75,
                    "volume_spike_ratio": 12,
                    "threat_risk_score": 10,
                    "buy_sell_ratio": 4.0,
                    "net_flow_sol": 12.0,
                    "smart_wallet_buys": 3,
                    "unique_buyer_count": 18,
                    "liquidity_drop_pct": 6,
                    "can_exit": True,
                }),
            },
        ]
        flow_rows = [
            {
                "mint": "mint-cmp",
                "created_at": now,
                "buy_sell_ratio": 2.4,
                "net_flow_sol": 6.0,
                "threat_risk_score": 12,
                "liquidity_drop_pct": 5,
                "can_exit": True,
            }
        ]
        model_family = {
            "global": {
                "trained": True,
                "bias": 0.0,
                "weights": [
                    {"feature": "composite_score", "label": "Composite score", "weight": 0.08, "midpoint": 55, "scale": 10},
                    {"feature": "buy_sell_ratio", "label": "Buy/sell ratio", "weight": 0.4, "midpoint": 1.5, "scale": 1},
                    {"feature": "threat_risk_score", "label": "Threat risk", "weight": -0.08, "midpoint": 35, "scale": 10},
                ],
            },
            "regimes": {
                "accumulation": {
                    "trained": True,
                    "bias": 0.0,
                    "weights": [
                        {"feature": "net_flow_sol", "label": "Net SOL flow", "weight": 0.25, "midpoint": 3, "scale": 2},
                        {"feature": "composite_score", "label": "Composite score", "weight": 0.06, "midpoint": 55, "scale": 10},
                    ],
                }
            },
        }
        settings = {
            "min_liq": 5000,
            "min_mc": 5000,
            "max_mc": 250000,
            "min_vol": 3000,
            "min_score": 30,
            "max_age_min": 120,
            "min_green_lights": 1,
            "min_narrative_score": 12,
            "min_holder_growth_pct": 20,
            "min_volume_spike_mult": 4,
            "anti_rug": True,
            "tp2_mult": 2.0,
            "stop_loss": 0.7,
            "time_stop_min": 30,
        }
        result = simulate_policy_comparison(
            run_id=0,
            snapshot_rows=snapshot_rows,
            flow_rows=flow_rows,
            rule_settings=settings,
            model_family=model_family,
            model_threshold=60,
        )
        policy_names = {row["policy_name"] for row in result["summary"]["policies"]}
        self.assertIn("rule_balanced", policy_names)
        self.assertIn("model_global", policy_names)
        self.assertIn("model_regime_auto", policy_names)
        self.assertGreaterEqual(len(result["trades"]), 2)


if __name__ == "__main__":
    unittest.main()
