import json
import unittest
from datetime import UTC, datetime

from learning_engine import (
    classify_flow_regime_row,
    score_recent_candidates,
    score_recent_candidates_for_regime,
    train_feature_model,
    train_regime_model_family,
)


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
                {"feature": "composite_score", "label": "Composite score", "weight": 0.08, "midpoint": 50, "scale": 10},
                {"feature": "threat_risk_score", "label": "Threat risk", "weight": -0.09, "midpoint": 40, "scale": 10},
                {"feature": "buy_sell_ratio", "label": "Buy/sell ratio", "weight": 0.5, "midpoint": 1.5, "scale": 1},
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

    def test_classify_flow_regime_row_detects_defensive_and_accumulation(self):
        self.assertEqual(
            classify_flow_regime_row({"buy_sell_ratio": 2.4, "net_flow_sol": 6.0, "threat_risk_score": 18, "liquidity_drop_pct": 5, "can_exit": True}),
            "accumulation",
        )
        self.assertEqual(
            classify_flow_regime_row({"buy_sell_ratio": 0.7, "net_flow_sol": -2.0, "threat_risk_score": 78, "liquidity_drop_pct": 40, "can_exit": False}),
            "defensive",
        )

    def test_train_regime_model_family_builds_regime_models_and_scores(self):
        now = datetime.now(UTC)
        snapshots = [
            {
                "mint": "acc-win",
                "name": "Acc Winner",
                "price": 1.0,
                "created_at": now,
                "feature_json": json.dumps({
                    "composite_score": 80, "confidence": 0.76, "buy_sell_ratio": 3.4, "net_flow_sol": 8.0,
                    "smart_wallet_buys": 2, "unique_buyer_count": 15, "volume_spike_ratio": 9,
                    "holder_growth_1h": 40, "threat_risk_score": 10, "liquidity_drop_pct": 4,
                }),
            },
            {
                "mint": "acc-rug",
                "name": "Acc Rug",
                "price": 1.0,
                "created_at": now,
                "feature_json": json.dumps({
                    "composite_score": 36, "confidence": 0.28, "buy_sell_ratio": 0.9, "net_flow_sol": -1.0,
                    "smart_wallet_buys": 0, "unique_buyer_count": 5, "volume_spike_ratio": 2,
                    "holder_growth_1h": 4, "threat_risk_score": 82, "liquidity_drop_pct": 46,
                }),
            },
            {
                "mint": "dist-win",
                "name": "Dist Winner",
                "price": 1.0,
                "created_at": now,
                "feature_json": json.dumps({
                    "composite_score": 68, "confidence": 0.6, "buy_sell_ratio": 1.1, "net_flow_sol": -2.4,
                    "smart_wallet_buys": 1, "unique_buyer_count": 10, "volume_spike_ratio": 6,
                    "holder_growth_1h": 26, "threat_risk_score": 18, "liquidity_drop_pct": 8,
                }),
            },
            {
                "mint": "dist-rug",
                "name": "Dist Rug",
                "price": 1.0,
                "created_at": now,
                "feature_json": json.dumps({
                    "composite_score": 28, "confidence": 0.2, "buy_sell_ratio": 0.6, "net_flow_sol": -4.0,
                    "smart_wallet_buys": 0, "unique_buyer_count": 3, "volume_spike_ratio": 1,
                    "holder_growth_1h": 2, "threat_risk_score": 88, "liquidity_drop_pct": 55,
                }),
            },
        ]
        outcomes = [
            {"mint": "acc-win", "label": "winner"},
            {"mint": "acc-rug", "label": "rug"},
            {"mint": "dist-win", "label": "winner"},
            {"mint": "dist-rug", "label": "rug"},
        ]
        flow_rows = [
            {"mint": "acc-win", "created_at": now, "buy_sell_ratio": 2.5, "net_flow_sol": 5.0, "threat_risk_score": 15, "liquidity_drop_pct": 6, "can_exit": True},
            {"mint": "acc-rug", "created_at": now, "buy_sell_ratio": 2.1, "net_flow_sol": 3.2, "threat_risk_score": 20, "liquidity_drop_pct": 8, "can_exit": True},
            {"mint": "dist-win", "created_at": now, "buy_sell_ratio": 0.8, "net_flow_sol": -2.5, "threat_risk_score": 20, "liquidity_drop_pct": 6, "can_exit": True},
            {"mint": "dist-rug", "created_at": now, "buy_sell_ratio": 0.7, "net_flow_sol": -3.5, "threat_risk_score": 42, "liquidity_drop_pct": 18, "can_exit": True},
        ]
        family = train_regime_model_family(snapshots, outcomes, flow_rows)
        self.assertTrue(family["global"]["trained"])
        self.assertTrue(family["regimes"]["accumulation"]["trained"])
        self.assertTrue(family["regimes"]["distribution"]["trained"] or family["regimes"]["defensive"]["trained"])

        scored = score_recent_candidates_for_regime(snapshots, family, active_regime="accumulation", mode="auto", top_n=2)
        self.assertIn(scored["selection"]["selected_key"], {"accumulation", "global"})
        self.assertTrue(scored["ranked_candidates"])


if __name__ == "__main__":
    unittest.main()
