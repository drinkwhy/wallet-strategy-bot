import unittest

from quant_platform import (
    build_feature_snapshot,
    evaluate_shadow_strategy,
    shadow_position_update,
)


class QuantPlatformTests(unittest.TestCase):
    def test_feature_snapshot_builds_composite_score(self):
        snapshot = build_feature_snapshot(
            {
                "price": 0.0021,
                "liq": 18000,
                "vol": 24000,
                "mc": 95000,
                "change": 42,
                "age_min": 6,
                "score": 72,
                "momentum": 55,
                "green_lights": 3,
                "narrative_score": 28,
                "deployer_score": 44,
            },
            {
                "holder_growth_1h": 62,
                "volume_spike_ratio": 11,
                "threat_risk_score": 12,
                "whale_score": 38,
                "whale_action_score": 41,
                "can_exit": True,
            },
        )
        self.assertGreater(snapshot["composite_score"], 50)
        self.assertGreater(snapshot["confidence"], 0.5)

    def test_shadow_strategy_passes_good_snapshot(self):
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
            "tp2_mult": 4.0,
            "stop_loss": 0.7,
            "time_stop_min": 30,
        }
        snapshot = build_feature_snapshot(
            {
                "price": 0.001,
                "liq": 12000,
                "vol": 22000,
                "mc": 80000,
                "change": 35,
                "age_min": 4,
                "score": 68,
                "momentum": 48,
                "green_lights": 2,
                "narrative_score": 24,
                "deployer_score": 35,
            },
            {
                "holder_growth_1h": 55,
                "volume_spike_ratio": 9,
                "threat_risk_score": 8,
                "whale_score": 30,
                "whale_action_score": 36,
                "can_exit": True,
            },
        )
        decision = evaluate_shadow_strategy("balanced", settings, snapshot)
        self.assertTrue(decision.passed)
        self.assertGreaterEqual(decision.score, 45)
        self.assertIn("liquidity_ok", decision.pass_reasons)

    def test_shadow_position_closes_on_take_profit(self):
        update = shadow_position_update(
            {
                "entry_price": 1.0,
                "peak_price": 1.2,
                "trough_price": 0.95,
                "take_profit_mult": 2.0,
                "stop_loss_ratio": 0.7,
                "time_stop_min": 30,
            },
            current_price=2.05,
            settings={"tp2_mult": 2.0, "stop_loss": 0.7, "time_stop_min": 30},
            age_min=8,
        )
        self.assertEqual(update["status"], "closed")
        self.assertEqual(update["exit_reason"], "take_profit")
        self.assertGreater(update["realized_pnl_pct"], 100)


if __name__ == "__main__":
    unittest.main()
