import json
import unittest
from datetime import UTC, datetime, timedelta

from edge_reporting import build_edge_report, summarize_edge_report_history


class EdgeReportingTests(unittest.TestCase):
    def test_build_edge_report_computes_policy_edges(self):
        now = datetime.now(UTC)
        report = build_edge_report(
            report_kind="manual",
            window_days=7,
            model_threshold=60,
            active_regime="accumulation",
            comparison_summary={
                "comparison_mode": "rules_vs_models",
                "tokens_processed": 12,
                "snapshots_processed": 18,
                "policies": [
                    {"policy_name": "rule_balanced", "avg_pnl_pct": 12.5, "win_rate": 36.0, "closed_trades": 5},
                    {"policy_name": "model_global", "avg_pnl_pct": 18.0, "win_rate": 40.0, "closed_trades": 5},
                    {"policy_name": "model_regime_auto", "avg_pnl_pct": 22.0, "win_rate": 44.0, "closed_trades": 5},
                ],
            },
            top_trades=[
                {"policy_name": "model_regime_auto", "mint": "mint-1", "realized_pnl_pct": 110.5},
            ],
            generated_at=now,
        )
        self.assertEqual(report["summary"]["leader_policy"], "model_regime_auto")
        self.assertEqual(report["summary"]["best_model_policy"], "model_regime_auto")
        self.assertEqual(report["summary"]["global_edge_pct"], 5.5)
        self.assertEqual(report["summary"]["regime_edge_pct"], 9.5)
        self.assertEqual(report["top_trades"][0]["policy_name"], "model_regime_auto")

    def test_summarize_edge_report_history_builds_trends_and_leaderboard(self):
        now = datetime.now(UTC)
        latest = build_edge_report(
            report_kind="scheduled",
            window_days=7,
            model_threshold=60,
            active_regime="accumulation",
            comparison_summary={
                "policies": [
                    {"policy_name": "rule_balanced", "avg_pnl_pct": 10.0, "win_rate": 30.0},
                    {"policy_name": "model_global", "avg_pnl_pct": 14.0, "win_rate": 35.0},
                    {"policy_name": "model_regime_auto", "avg_pnl_pct": 18.0, "win_rate": 39.0},
                ],
            },
            generated_at=now,
        )
        previous = build_edge_report(
            report_kind="scheduled",
            window_days=7,
            model_threshold=60,
            active_regime="neutral",
            comparison_summary={
                "policies": [
                    {"policy_name": "rule_balanced", "avg_pnl_pct": 9.0, "win_rate": 28.0},
                    {"policy_name": "model_global", "avg_pnl_pct": 11.0, "win_rate": 31.0},
                    {"policy_name": "model_regime_auto", "avg_pnl_pct": 12.0, "win_rate": 32.0},
                ],
            },
            generated_at=now - timedelta(days=7),
        )
        monthly = build_edge_report(
            report_kind="scheduled",
            window_days=30,
            model_threshold=60,
            active_regime="distribution",
            comparison_summary={
                "policies": [
                    {"policy_name": "rule_balanced", "avg_pnl_pct": 6.0, "win_rate": 27.0},
                    {"policy_name": "model_global", "avg_pnl_pct": 8.0, "win_rate": 29.0},
                    {"policy_name": "model_regime_auto", "avg_pnl_pct": 9.0, "win_rate": 31.0},
                ],
            },
            generated_at=now - timedelta(hours=3),
        )
        rows = [
            {
                "id": 3,
                "report_kind": latest["report_kind"],
                "window_days": latest["window_days"],
                "model_threshold": latest["model_threshold"],
                "active_regime": latest["active_regime"],
                "summary_json": json.dumps(latest["summary"]),
                "policy_json": json.dumps({"policies": latest["policies"], "top_trades": latest["top_trades"]}),
                "generated_at": now,
            },
            {
                "id": 2,
                "report_kind": monthly["report_kind"],
                "window_days": monthly["window_days"],
                "model_threshold": monthly["model_threshold"],
                "active_regime": monthly["active_regime"],
                "summary_json": json.dumps(monthly["summary"]),
                "policy_json": json.dumps({"policies": monthly["policies"], "top_trades": monthly["top_trades"]}),
                "generated_at": now - timedelta(hours=3),
            },
            {
                "id": 1,
                "report_kind": previous["report_kind"],
                "window_days": previous["window_days"],
                "model_threshold": previous["model_threshold"],
                "active_regime": previous["active_regime"],
                "summary_json": json.dumps(previous["summary"]),
                "policy_json": json.dumps({"policies": previous["policies"], "top_trades": previous["top_trades"]}),
                "generated_at": now - timedelta(days=7),
            },
        ]
        summary = summarize_edge_report_history(rows, focus_window_days=7)
        self.assertEqual(summary["focus_report"]["window_days"], 7)
        self.assertEqual(len(summary["latest_by_window"]), 2)
        seven_day_trend = next(item for item in summary["trend_cards"] if item["window_days"] == 7)
        self.assertEqual(seven_day_trend["leader_policy"], "model_regime_auto")
        self.assertEqual(seven_day_trend["delta_leader_avg_pnl_pct"], 6.0)
        self.assertEqual(seven_day_trend["delta_regime_edge_pct"], 5.0)
        self.assertEqual(summary["leaderboard"][0]["policy_name"], "model_regime_auto")


if __name__ == "__main__":
    unittest.main()
