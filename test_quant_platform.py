import unittest

from quant_platform import (
    build_flow_snapshot,
    build_feature_snapshot,
    estimate_exit_friction,
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
        self.assertEqual(update["exit_reason"], "take_profit_tp2")
        self.assertGreater(update["realized_pnl_pct"], 100)

    def test_shadow_position_closes_on_tp2_after_tp1(self):
        # TP1 was already recorded in a **prior** tick (tp1_hit=True).
        # When price now reaches TP2, the position should close via the
        # elif-tp1_hit branch with exit_reason "take_profit_tp2".
        update = shadow_position_update(
            {
                "entry_price": 1.0,
                "peak_price": 2.1,
                "trough_price": 0.98,
                "take_profit_mult": 3.0,
                "stop_loss_ratio": 0.7,
                "time_stop_min": 30,
                "tp1_hit": True,
                "tp1_pnl_pct": 50.0,
            },
            current_price=3.05,
            settings={"tp2_mult": 3.0, "stop_loss": 0.7, "time_stop_min": 30},
            age_min=10,
        )
        self.assertEqual(update["status"], "closed")
        self.assertEqual(update["exit_reason"], "take_profit_tp2")
        self.assertGreater(update["realized_pnl_pct"], 100)

    def test_shadow_position_closes_on_stop_loss(self):
        update = shadow_position_update(
            {
                "entry_price": 1.0,
                "peak_price": 1.05,
                "trough_price": 0.65,
                "take_profit_mult": 2.0,
                "stop_loss_ratio": 0.7,
                "time_stop_min": 30,
            },
            current_price=0.68,
            settings={"tp2_mult": 2.0, "stop_loss": 0.7, "time_stop_min": 30},
            age_min=5,
        )
        self.assertEqual(update["status"], "closed")
        self.assertEqual(update["exit_reason"], "stop_loss")
        self.assertLess(update["realized_pnl_pct"], 0)

    def test_shadow_position_closes_on_time_stop(self):
        # Flat price after time limit — time stop should trigger
        update = shadow_position_update(
            {
                "entry_price": 1.0,
                "peak_price": 1.02,
                "trough_price": 0.98,
                "take_profit_mult": 2.0,
                "stop_loss_ratio": 0.7,
                "time_stop_min": 30,
            },
            current_price=1.01,
            settings={"tp2_mult": 2.0, "stop_loss": 0.7, "time_stop_min": 30},
            age_min=35,
        )
        self.assertEqual(update["status"], "closed")
        self.assertEqual(update["exit_reason"], "time_stop")

    def test_shadow_position_trails_after_tp1(self):
        # Price fell below trailing stop after TP1 was hit in a prior tick
        update = shadow_position_update(
            {
                "entry_price": 1.0,
                "peak_price": 2.5,
                "trough_price": 0.95,
                "take_profit_mult": 3.0,
                "stop_loss_ratio": 0.7,
                "time_stop_min": 30,
                "tp1_hit": True,
                "tp1_pnl_pct": 50.0,
            },
            current_price=1.9,  # below peak*0.80 trail line (2.5*0.80=2.0)
            settings={"tp2_mult": 3.0, "stop_loss": 0.7, "time_stop_min": 30, "trail_pct": 0.20},
            age_min=12,
        )
        self.assertEqual(update["status"], "closed")
        self.assertEqual(update["exit_reason"], "trail_after_tp1")
        self.assertGreater(update["realized_pnl_pct"], 0)

    def test_shadow_position_stays_open_before_any_exit(self):
        # Price slightly above entry but well below take profit — position should stay open
        update = shadow_position_update(
            {
                "entry_price": 1.0,
                "peak_price": 1.3,
                "trough_price": 0.98,
                "take_profit_mult": 2.0,
                "stop_loss_ratio": 0.7,
                "time_stop_min": 30,
            },
            current_price=1.3,
            settings={"tp2_mult": 2.0, "stop_loss": 0.7, "time_stop_min": 30},
            age_min=5,
        )
        self.assertEqual(update["status"], "open")
        self.assertIsNone(update["realized_pnl_pct"])

    def test_shadow_position_peak_plateau_trails_from_peak(self):
        # In peak_plateau_mode: position hit 4x, now price dropped enough to trail out
        update = shadow_position_update(
            {
                "entry_price": 1.0,
                "peak_price": 4.0,
                "trough_price": 0.95,
                "take_profit_mult": 2.0,
                "stop_loss_ratio": 0.6,
                "time_stop_min": 30,
                "tp1_hit": True,
                "tp1_pnl_pct": 50.0,
            },
            current_price=3.1,  # below 4.0 * (1 - 0.20) = 3.2 trail line
            settings={
                "tp2_mult": 5.0,
                "stop_loss": 0.6,
                "time_stop_min": 30,
                "peak_plateau_mode": True,
                "trail_pct": 0.20,
            },
            age_min=12,
        )
        self.assertEqual(update["status"], "closed")
        self.assertIn("peak_plateau", update["exit_reason"])
        self.assertGreater(update["realized_pnl_pct"], 100)

    def test_evaluate_shadow_strategy_blocked_by_low_liquidity(self):
        snapshot = build_feature_snapshot(
            {"price": 0.001, "liq": 1000, "vol": 5000, "mc": 20000, "change": 10,
             "age_min": 3, "score": 65, "momentum": 40, "green_lights": 2,
             "narrative_score": 20, "deployer_score": 40},
            {"holder_growth_1h": 30, "volume_spike_ratio": 5, "threat_risk_score": 10,
             "whale_score": 20, "whale_action_score": 20, "unique_buyer_count": 8,
             "unique_seller_count": 2, "first_buyer_count": 5, "smart_wallet_buys": 1,
             "smart_wallet_first10": 0, "total_buy_sol": 4.0, "total_sell_sol": 0.5,
             "net_flow_sol": 3.5, "buy_sell_ratio": 3.0, "liquidity_drop_pct": 2.0,
             "can_exit": True},
        )
        settings = {"min_liq": 5000, "min_mc": 5000, "max_mc": 250000, "min_vol": 3000,
                    "min_score": 30, "max_age_min": 120, "tp2_mult": 2.0,
                    "stop_loss": 0.7, "time_stop_min": 30}
        decision = evaluate_shadow_strategy("balanced", settings, snapshot)
        self.assertFalse(decision.passed)
        self.assertIn("liquidity_below_threshold", decision.blocker_reasons)

    def test_evaluate_shadow_strategy_blocked_by_high_threat(self):
        snapshot = build_feature_snapshot(
            {"price": 0.005, "liq": 20000, "vol": 30000, "mc": 100000, "change": 20,
             "age_min": 4, "score": 75, "momentum": 60, "green_lights": 3,
             "narrative_score": 25, "deployer_score": 50},
            {"holder_growth_1h": 50, "volume_spike_ratio": 8, "threat_risk_score": 90,
             "whale_score": 40, "whale_action_score": 40, "unique_buyer_count": 15,
             "unique_seller_count": 3, "first_buyer_count": 10, "smart_wallet_buys": 2,
             "smart_wallet_first10": 1, "total_buy_sol": 10.0, "total_sell_sol": 1.5,
             "net_flow_sol": 8.5, "buy_sell_ratio": 4.0, "liquidity_drop_pct": 3.0,
             "can_exit": True},
        )
        settings = {"min_liq": 5000, "min_mc": 5000, "max_mc": 250000, "min_vol": 3000,
                    "min_score": 30, "max_age_min": 120, "max_threat_score": 70,
                    "tp2_mult": 2.0, "stop_loss": 0.7, "time_stop_min": 30}
        decision = evaluate_shadow_strategy("balanced", settings, snapshot)
        self.assertFalse(decision.passed)
        self.assertIn("threat_risk_too_high", decision.blocker_reasons)

    def test_friction_reduces_profit_on_winning_trade(self):
        result = estimate_exit_friction(
            realized_pnl_pct=100.0,
            exit_price=2.0,
            entry_price=1.0,
            entry_sol=0.1,
            liq_usd=20000,
            vol_usd=50000,
            exit_reason="take_profit",
            peak_ratio=2.0,
        )
        self.assertIsNotNone(result["friction_pnl_pct"])
        # Friction-adjusted P&L must be lower than the ideal 100%
        self.assertLess(result["friction_pnl_pct"], 100.0)
        # But still profitable (friction shouldn't wipe out a 2x trade with decent liquidity)
        self.assertGreater(result["friction_pnl_pct"], 0.0)
        self.assertGreater(result["total_friction_pct"], 0.0)

    def test_friction_makes_losing_trade_worse(self):
        result = estimate_exit_friction(
            realized_pnl_pct=-20.0,
            exit_price=0.8,
            entry_price=1.0,
            entry_sol=0.1,
            liq_usd=5000,
            vol_usd=10000,
            exit_reason="stop_loss",
            peak_ratio=0.9,
        )
        self.assertLess(result["friction_pnl_pct"], -20.0)

    def test_friction_returns_none_when_pnl_is_none(self):
        result = estimate_exit_friction(
            realized_pnl_pct=None,
            exit_price=1.0,
            entry_price=1.0,
            entry_sol=0.1,
        )
        self.assertIsNone(result["friction_pnl_pct"])
        self.assertEqual(result["total_friction_pct"], 0)

    def test_friction_illiquid_pool_has_high_slippage(self):
        result = estimate_exit_friction(
            realized_pnl_pct=50.0,
            exit_price=1.5,
            entry_price=1.0,
            entry_sol=0.05,
            liq_usd=500,  # near-illiquid pool
            vol_usd=1000,
            exit_reason="take_profit",
            peak_ratio=1.5,
        )
        # Slippage should be in the high tier (45%) for liq < $1k
        self.assertGreaterEqual(result["slippage_pct"], 28.0)
        self.assertGreater(result["total_friction_pct"], 30.0)

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
