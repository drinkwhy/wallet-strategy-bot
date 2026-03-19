import json
import unittest
from datetime import UTC, datetime

from optimizer_engine import (
    build_outcome_labels,
    summarize_feature_edges,
    sweep_entry_filters,
)


class OptimizerEngineTests(unittest.TestCase):
    def test_build_outcome_labels_classifies_winners_and_rugs(self):
        labels = build_outcome_labels([
            {"mint": "winner", "name": "Winner", "first_price": 1.0, "peak_price": 2.5, "trough_price": 0.92, "last_price": 1.8},
            {"mint": "rug", "name": "Rug", "first_price": 1.0, "peak_price": 1.08, "trough_price": 0.4, "last_price": 0.5},
        ])
        by_mint = {row["mint"]: row for row in labels}
        self.assertEqual(by_mint["winner"]["label"], "winner")
        self.assertEqual(by_mint["rug"]["label"], "rug")

    def test_feature_edges_favor_winners(self):
        now = datetime.now(UTC)
        snapshots = [
            {
                "mint": "winner",
                "name": "Winner",
                "price": 1.0,
                "created_at": now,
                "feature_json": json.dumps({
                    "composite_score": 74,
                    "confidence": 0.72,
                    "buy_sell_ratio": 3.2,
                    "net_flow_sol": 8.5,
                    "smart_wallet_buys": 2,
                    "unique_buyer_count": 16,
                    "volume_spike_ratio": 9.0,
                    "holder_growth_1h": 48,
                    "threat_risk_score": 10,
                    "liquidity_drop_pct": 5,
                }),
            },
            {
                "mint": "rug",
                "name": "Rug",
                "price": 1.0,
                "created_at": now,
                "feature_json": json.dumps({
                    "composite_score": 41,
                    "confidence": 0.33,
                    "buy_sell_ratio": 0.8,
                    "net_flow_sol": -2.0,
                    "smart_wallet_buys": 0,
                    "unique_buyer_count": 4,
                    "volume_spike_ratio": 2.0,
                    "holder_growth_1h": 6,
                    "threat_risk_score": 82,
                    "liquidity_drop_pct": 48,
                }),
            },
        ]
        outcomes = build_outcome_labels([
            {"mint": "winner", "first_price": 1.0, "peak_price": 2.5, "trough_price": 0.9, "last_price": 1.8},
            {"mint": "rug", "first_price": 1.0, "peak_price": 1.05, "trough_price": 0.38, "last_price": 0.44},
        ])
        edges = summarize_feature_edges(snapshots, outcomes)
        self.assertTrue(any(item["feature"] == "composite_score" and item["edge"] > 0 for item in edges))
        self.assertTrue(any(item["feature"] == "threat_risk_score" and item["edge"] > 0 for item in edges))

    def test_sweep_entry_filters_returns_ranked_thresholds(self):
        now = datetime.now(UTC)
        snapshots = [
            {
                "mint": "winner",
                "name": "Winner",
                "price": 1.0,
                "created_at": now,
                "feature_json": json.dumps({
                    "composite_score": 78,
                    "confidence": 0.74,
                    "buy_sell_ratio": 3.4,
                    "net_flow_sol": 9.0,
                    "smart_wallet_buys": 2,
                }),
            },
            {
                "mint": "rug",
                "name": "Rug",
                "price": 1.0,
                "created_at": now,
                "feature_json": json.dumps({
                    "composite_score": 38,
                    "confidence": 0.28,
                    "buy_sell_ratio": 0.7,
                    "net_flow_sol": -3.0,
                    "smart_wallet_buys": 0,
                }),
            },
        ]
        outcomes = build_outcome_labels([
            {"mint": "winner", "first_price": 1.0, "peak_price": 2.7, "trough_price": 0.95, "last_price": 2.0},
            {"mint": "rug", "first_price": 1.0, "peak_price": 1.02, "trough_price": 0.42, "last_price": 0.49},
        ])
        sweeps = sweep_entry_filters(snapshots, outcomes)
        self.assertTrue(sweeps["best_by_feature"])
        self.assertEqual(sweeps["all"][0]["feature"], "composite_score")
        self.assertGreaterEqual(sweeps["all"][0]["winner_rate_pct"], 50)


if __name__ == "__main__":
    unittest.main()
