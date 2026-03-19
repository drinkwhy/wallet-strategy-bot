import unittest

from quant_platform import (
    build_flow_snapshot,
    build_feature_snapshot,
    evaluate_shadow_strategy,
    shadow_position_update,
    summarize_flow_regime,
    summarize_opportunity_matrix,
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
                "unique_buyer_count": 14,
                "unique_seller_count": 3,
                "first_buyer_count": 10,
                "smart_wallet_buys": 3,
                "smart_wallet_first10": 2,
                "total_buy_sol": 18.5,
                "total_sell_sol": 2.1,
                "net_flow_sol": 16.4,
                "buy_sell_ratio": 4.67,
                "liquidity_drop_pct": 4.0,
                "can_exit": True,
            },
        )
        self.assertGreater(snapshot["composite_score"], 50)
        self.assertGreater(snapshot["confidence"], 0.5)
        self.assertGreater(snapshot["flow_quality"], 0.5)
        self.assertGreater(snapshot["smart_money_quality"], 0)

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
                "unique_buyer_count": 10,
                "unique_seller_count": 2,
                "first_buyer_count": 8,
                "smart_wallet_buys": 2,
                "smart_wallet_first10": 1,
                "total_buy_sol": 10.8,
                "total_sell_sol": 1.7,
                "net_flow_sol": 9.1,
                "buy_sell_ratio": 5.0,
                "liquidity_drop_pct": 3.5,
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

    def test_flow_snapshot_summarizes_wallet_pressure(self):
        flow = build_flow_snapshot(
            {"price": 0.0012, "liq": 9000, "mc": 50000, "vol": 12000, "age_min": 5},
            {
                "holder_count": 46,
                "holder_growth_1h": 21,
                "unique_buyer_count": 12,
                "unique_seller_count": 3,
                "first_buyer_count": 9,
                "smart_wallet_buys": 2,
                "smart_wallet_first10": 1,
                "total_buy_sol": 8.4,
                "total_sell_sol": 1.2,
                "net_flow_sol": 7.2,
                "buy_sell_ratio": 4.0,
                "liquidity_drop_pct": 6.5,
                "threat_risk_score": 15,
                "can_exit": True,
            },
        )
        self.assertEqual(flow["unique_buyer_count"], 12)
        self.assertAlmostEqual(flow["buy_pressure_pct"], 80.0)
        self.assertGreater(flow["net_flow_sol"], 7)

    def test_flow_regime_flags_accumulation(self):
        summary = summarize_flow_regime([
            {"buy_sell_ratio": 2.8, "net_flow_sol": 5.2, "smart_wallet_buys": 2, "unique_buyer_count": 12, "threat_risk_score": 20, "can_exit": True, "liquidity_drop_pct": 4},
            {"buy_sell_ratio": 1.9, "net_flow_sol": 3.1, "smart_wallet_buys": 1, "unique_buyer_count": 9, "threat_risk_score": 18, "can_exit": True, "liquidity_drop_pct": 8},
        ])
        self.assertEqual(summary["regime"], "accumulation")
        self.assertGreater(summary["avg_net_flow_sol"], 3)

    def test_opportunity_matrix_tracks_missed_winners(self):
        summary = summarize_opportunity_matrix([
            {
                "strategy_name": "balanced",
                "mint": "mint-1",
                "name": "Missed Moon",
                "passed": 0,
                "price": 1.0,
                "first_price": 1.0,
                "peak_price": 2.6,
                "trough_price": 0.9,
                "last_price": 1.8,
                "blocker_reasons": '["liquidity_below_threshold","ai_score_below_threshold"]',
            },
            {
                "strategy_name": "balanced",
                "mint": "mint-2",
                "name": "Avoided Rug",
                "passed": 0,
                "price": 1.0,
                "first_price": 1.0,
                "peak_price": 1.08,
                "trough_price": 0.42,
                "last_price": 0.5,
                "blocker_reasons": '["threat_risk_too_high"]',
            },
            {
                "strategy_name": "balanced",
                "mint": "mint-3",
                "name": "Bad Entry",
                "passed": 1,
                "price": 1.0,
                "first_price": 1.0,
                "peak_price": 1.15,
                "trough_price": 0.48,
                "last_price": 0.55,
                "blocker_reasons": "[]",
            },
        ], winner_threshold_pct=120, loser_threshold_pct=-35)
        self.assertEqual(summary["totals"]["missed_winners"], 1)
        self.assertEqual(summary["totals"]["avoided_losers"], 1)
        self.assertEqual(summary["totals"]["false_positives"], 1)
        self.assertEqual(summary["top_blockers"][0]["reason"], "ai_score_below_threshold")


if __name__ == "__main__":
    unittest.main()
