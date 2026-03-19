import json
import unittest
from datetime import UTC, datetime

from learning_engine import score_recent_candidates, train_feature_model


class LearningEngineTests(unittest.TestCase):
    def test_train_feature_model_learns_from_winners_and_rugs(self):
        now = datetime.now(UTC)
        snapshots = [
            {
                "mint": "winner-a",
                "name": "Winner A",
                "price": 1.0,
                "created_at": now,
                "feature_json": json.dumps({
                    "composite_score": 78,
                    "confidence": 0.74,
                    "buy_sell_ratio": 3.0,
                    "net_flow_sol": 8.0,
                    "smart_wallet_buys": 2,
                    "unique_buyer_count": 14,
                    "volume_spike_ratio": 9,
                    "holder_growth_1h": 44,
                    "threat_risk_score": 10,
                    "liquidity_drop_pct": 4,
                }),
            },
            {
                "mint": "rug-a",
                "name": "Rug A",
                "price": 1.0,
                "created_at": now,
                "feature_json": json.dumps({
                    "composite_score": 34,
                    "confidence": 0.25,
                    "buy_sell_ratio": 0.7,
                    "net_flow_sol": -2.8,
                    "smart_wallet_buys": 0,
                    "unique_buyer_count": 4,
                    "volume_spike_ratio": 2,
                    "holder_growth_1h": 5,
                    "threat_risk_score": 84,
                    "liquidity_drop_pct": 52,
                }),
            },
        ]
        outcomes = [
            {"mint": "winner-a", "label": "winner"},
            {"mint": "rug-a", "label": "rug"},
        ]
        model = train_feature_model(snapshots, outcomes)
        self.assertTrue(model["trained"])
        self.assertGreater(model["accuracy_pct"], 50)
        self.assertTrue(any(item["feature"] == "threat_risk_score" for item in model["weights"]))

    def test_score_recent_candidates_ranks_best_candidate_first(self):
        now = datetime.now(UTC)
        model = {
            "trained": True,
            "bias": 0.0,
            "weights": [
                {"feature": "composite_score", "label": "Composite score", "weight": 0.08},
                {"feature": "threat_risk_score", "label": "Threat risk", "weight": -0.09},
                {"feature": "buy_sell_ratio", "label": "Buy/sell ratio", "weight": 0.5},
            ],
        }
        snapshots = [
            {
                "mint": "best",
                "name": "Best",
                "price": 1.0,
                "created_at": now,
                "feature_json": json.dumps({
                    "composite_score": 80,
                    "threat_risk_score": 10,
                    "buy_sell_ratio": 3.2,
                }),
            },
            {
                "mint": "worst",
                "name": "Worst",
                "price": 1.0,
                "created_at": now,
                "feature_json": json.dumps({
                    "composite_score": 30,
                    "threat_risk_score": 82,
                    "buy_sell_ratio": 0.8,
                }),
            },
        ]
        ranked = score_recent_candidates(snapshots, model, top_n=2)
        self.assertEqual(ranked[0]["mint"], "best")
        self.assertGreater(ranked[0]["model_score"], ranked[1]["model_score"])


if __name__ == "__main__":
    unittest.main()
