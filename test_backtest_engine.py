import json
import unittest
from datetime import UTC, datetime, timedelta

from backtest_engine import simulate_backtest


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


if __name__ == "__main__":
    unittest.main()
