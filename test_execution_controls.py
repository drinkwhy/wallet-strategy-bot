from datetime import UTC, datetime, timedelta
import unittest

from execution_controls import determine_execution_policy, normalize_execution_control


class ExecutionControlsTests(unittest.TestCase):
    def test_normalize_defaults(self):
        control = normalize_execution_control({})
        self.assertEqual(control["execution_mode"], "live")
        self.assertEqual(control["policy_mode"], "rules")
        self.assertEqual(control["decision_policy"], "rules")
        self.assertEqual(control["active_policy"], "rules")
        self.assertEqual(control["model_threshold"], 60.0)

    def test_normalize_accepts_decision_policy_alias(self):
        control = normalize_execution_control({"decision_policy": "model_global"})
        self.assertEqual(control["policy_mode"], "model_global")
        self.assertEqual(control["decision_policy"], "model_global")

    def test_manual_mode_stays_manual(self):
        result = determine_execution_policy(
            history_summary={},
            guard_state={},
            control={"execution_mode": "paper", "policy_mode": "model_global"},
            now=datetime(2026, 3, 19, tzinfo=UTC),
        )
        self.assertEqual(result["selected_policy"], "model_global")
        self.assertEqual(result["selection_source"], "manual")
        self.assertFalse(result["promotion_applied"])

    def test_auto_mode_promotes_leader_when_history_is_strong(self):
        now = datetime(2026, 3, 19, tzinfo=UTC)
        history = {
            "focus_window_days": 7,
            "report_count": 4,
            "focus_report": {
                "id": 22,
                "generated_at": now.isoformat(),
                "summary": {
                    "leader_policy": "model_regime_auto",
                    "leader_avg_pnl_pct": 14.2,
                },
                "policies": [
                    {"policy_name": "rule_balanced", "closed_trades": 4},
                    {"policy_name": "model_regime_auto", "closed_trades": 5},
                ],
            },
        }
        result = determine_execution_policy(
            history_summary=history,
            guard_state={"prefer_rules_only": False},
            control={
                "policy_mode": "auto",
                "auto_promote": True,
                "active_policy": "rules",
                "auto_promote_lock_minutes": 120,
            },
            now=now,
        )
        self.assertEqual(result["selected_policy"], "model_regime_auto")
        self.assertTrue(result["promotion_applied"])
        self.assertEqual(result["db_update"]["active_policy"], "model_regime_auto")
        self.assertEqual(result["db_update"]["active_policy_report_id"], 22)

    def test_auto_mode_respects_lock(self):
        now = datetime(2026, 3, 19, tzinfo=UTC)
        history = {
            "focus_window_days": 7,
            "report_count": 5,
            "focus_report": {
                "id": 30,
                "generated_at": now.isoformat(),
                "summary": {
                    "leader_policy": "model_regime_auto",
                    "leader_avg_pnl_pct": 12.0,
                },
                "policies": [
                    {"policy_name": "rule_balanced", "closed_trades": 4},
                    {"policy_name": "model_regime_auto", "closed_trades": 5},
                ],
            },
        }
        result = determine_execution_policy(
            history_summary=history,
            guard_state={},
            control={
                "policy_mode": "auto",
                "auto_promote": True,
                "active_policy": "rules",
                "auto_promote_locked_until": (now + timedelta(minutes=60)).isoformat(),
            },
            now=now,
        )
        self.assertEqual(result["selected_policy"], "rules")
        self.assertEqual(result["candidate_reason"], "promotion_locked")
        self.assertFalse(result["promotion_applied"])

    def test_edge_guard_forces_rules_candidate(self):
        now = datetime(2026, 3, 19, tzinfo=UTC)
        history = {
            "focus_window_days": 7,
            "report_count": 5,
            "focus_report": {
                "id": 40,
                "generated_at": now.isoformat(),
                "summary": {
                    "leader_policy": "model_global",
                    "leader_avg_pnl_pct": 7.0,
                },
                "policies": [
                    {"policy_name": "rule_balanced", "closed_trades": 4},
                    {"policy_name": "model_global", "closed_trades": 6},
                ],
            },
        }
        result = determine_execution_policy(
            history_summary=history,
            guard_state={"prefer_rules_only": True},
            control={"policy_mode": "auto", "auto_promote": False},
            now=now,
        )
        self.assertEqual(result["selected_policy"], "rules")
        self.assertEqual(result["candidate_reason"], "edge_guard_prefers_rules")


if __name__ == "__main__":
    unittest.main()
