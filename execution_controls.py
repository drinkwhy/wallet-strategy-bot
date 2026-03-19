from datetime import UTC, datetime, timedelta


POLICY_LABELS = {
    "rules": "Rules",
    "model_global": "Global model",
    "model_regime_auto": "Regime model",
    "auto": "Auto",
}

REPORT_TO_POLICY = {
    "rule_balanced": "rules",
    "model_global": "model_global",
    "model_regime_auto": "model_regime_auto",
}

VALID_EXECUTION_MODES = {"live", "paper"}
VALID_POLICY_MODES = {"rules", "model_global", "model_regime_auto", "auto"}

DEFAULT_EXECUTION_CONTROL = {
    "execution_mode": "live",
    "policy_mode": "rules",
    "model_threshold": 60.0,
    "auto_promote": False,
    "auto_promote_window_days": 7,
    "auto_promote_min_reports": 3,
    "auto_promote_lock_minutes": 180,
    "active_policy": "rules",
    "active_policy_source": "manual",
    "active_policy_report_id": None,
    "active_policy_updated_at": None,
    "auto_promote_locked_until": None,
}


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def _safe_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _parse_dt(value):
    if value in (None, "", 0):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except Exception:
            return None
    return None


def _isoformat(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def normalize_execution_mode(value):
    normalized = str(value or "").strip().lower()
    return normalized if normalized in VALID_EXECUTION_MODES else DEFAULT_EXECUTION_CONTROL["execution_mode"]


def normalize_policy_mode(value):
    normalized = str(value or "").strip().lower()
    return normalized if normalized in VALID_POLICY_MODES else DEFAULT_EXECUTION_CONTROL["policy_mode"]


def normalize_execution_control(control):
    merged = dict(DEFAULT_EXECUTION_CONTROL)
    if isinstance(control, dict):
        merged.update(control)
    merged["execution_mode"] = normalize_execution_mode(merged.get("execution_mode"))
    raw_policy = merged.get("policy_mode")
    if isinstance(control, dict) and "policy_mode" not in control:
        raw_policy = control.get("decision_policy", raw_policy)
    merged["policy_mode"] = normalize_policy_mode(raw_policy)
    merged["decision_policy"] = merged["policy_mode"]
    merged["model_threshold"] = max(40.0, min(_safe_float(merged.get("model_threshold"), 60.0), 90.0))
    merged["auto_promote"] = _safe_bool(merged.get("auto_promote"))
    merged["auto_promote_window_days"] = max(3, min(_safe_int(merged.get("auto_promote_window_days"), 7), 30))
    merged["auto_promote_min_reports"] = max(2, min(_safe_int(merged.get("auto_promote_min_reports"), 3), 12))
    merged["auto_promote_lock_minutes"] = max(30, min(_safe_int(merged.get("auto_promote_lock_minutes"), 180), 7 * 24 * 60))
    merged["active_policy"] = normalize_policy_mode(merged.get("active_policy"))
    merged["active_policy_source"] = str(merged.get("active_policy_source") or "manual").strip().lower()
    merged["active_policy_report_id"] = merged.get("active_policy_report_id")
    merged["active_policy_updated_at"] = _isoformat(_parse_dt(merged.get("active_policy_updated_at")))
    merged["auto_promote_locked_until"] = _isoformat(_parse_dt(merged.get("auto_promote_locked_until")))
    return merged


def _candidate_from_history(history_summary, guard_state, control):
    report_count = _safe_int((history_summary or {}).get("report_count"))
    focus_report = (history_summary or {}).get("focus_report") or {}
    focus_summary = focus_report.get("summary") or {}
    policies = focus_report.get("policies") or []
    leader_report_policy = focus_summary.get("leader_policy") or "rule_balanced"
    leader_policy = REPORT_TO_POLICY.get(leader_report_policy, "rules")
    leader_avg_pnl_pct = _safe_float(focus_summary.get("leader_avg_pnl_pct"))
    total_closed_trades = sum(_safe_int(item.get("closed_trades")) for item in policies)
    min_reports = _safe_int(control.get("auto_promote_min_reports"), 3)

    if (guard_state or {}).get("prefer_rules_only"):
        return "rules", "edge_guard_prefers_rules", leader_policy, leader_report_policy, report_count, total_closed_trades
    if report_count < min_reports:
        return "rules", "insufficient_report_history", leader_policy, leader_report_policy, report_count, total_closed_trades
    if total_closed_trades < 6:
        return "rules", "insufficient_closed_trades", leader_policy, leader_report_policy, report_count, total_closed_trades
    if leader_avg_pnl_pct <= 0:
        return "rules", "leader_not_positive", leader_policy, leader_report_policy, report_count, total_closed_trades
    return leader_policy, "leader_from_reports", leader_policy, leader_report_policy, report_count, total_closed_trades


def determine_execution_policy(history_summary, guard_state, control, now=None):
    now = now or datetime.now(UTC)
    state = normalize_execution_control(control)
    mode = state["policy_mode"]
    active_policy = state.get("active_policy") or "rules"
    lock_until = _parse_dt(state.get("auto_promote_locked_until"))
    lock_active = bool(lock_until and lock_until > now)

    selected_policy = mode
    candidate_policy = active_policy
    candidate_reason = "manual_selection"
    leader_policy = "rules"
    leader_report_policy = "rule_balanced"
    report_count = 0
    total_closed_trades = 0
    focus_report = (history_summary or {}).get("focus_report") or {}

    if mode == "auto":
        candidate_policy, candidate_reason, leader_policy, leader_report_policy, report_count, total_closed_trades = _candidate_from_history(
            history_summary,
            guard_state,
            state,
        )
        selected_policy = candidate_policy
        if state["auto_promote"] and lock_active and active_policy in VALID_POLICY_MODES and candidate_policy != active_policy:
            selected_policy = active_policy
            candidate_reason = "promotion_locked"
    else:
        selected_policy = mode
        candidate_policy = mode

    promotion_applied = False
    db_update = {}
    if mode == "auto" and state["auto_promote"] and selected_policy != active_policy and not lock_active:
        promotion_applied = True
        locked_until = now + timedelta(minutes=state["auto_promote_lock_minutes"])
        db_update = {
            "active_policy": selected_policy,
            "active_policy_source": "auto",
            "active_policy_report_id": focus_report.get("id"),
            "active_policy_updated_at": _isoformat(now),
            "auto_promote_locked_until": _isoformat(locked_until),
        }
        active_policy = selected_policy
        lock_until = locked_until
        lock_active = True

    effective_policy = selected_policy if mode == "auto" else mode
    if mode != "auto":
        active_policy = mode

    return {
        "execution_mode": state["execution_mode"],
        "policy_mode": mode,
        "selected_policy": effective_policy,
        "selected_policy_label": POLICY_LABELS.get(effective_policy, effective_policy),
        "active_policy": active_policy,
        "active_policy_label": POLICY_LABELS.get(active_policy, active_policy),
        "candidate_policy": candidate_policy,
        "candidate_policy_label": POLICY_LABELS.get(candidate_policy, candidate_policy),
        "candidate_reason": candidate_reason,
        "selection_source": "manual" if mode != "auto" else ("locked" if candidate_reason == "promotion_locked" else "auto"),
        "auto_promote": state["auto_promote"],
        "promotion_applied": promotion_applied,
        "lock_active": lock_active,
        "lock_until": _isoformat(lock_until),
        "report_count": report_count,
        "total_closed_trades": total_closed_trades,
        "leader_policy": leader_policy,
        "leader_report_policy": leader_report_policy,
        "leader_policy_label": POLICY_LABELS.get(leader_policy, leader_policy),
        "focus_window_days": _safe_int((history_summary or {}).get("focus_window_days"), state["auto_promote_window_days"]),
        "focus_report_id": focus_report.get("id"),
        "focus_report_generated_at": focus_report.get("generated_at"),
        "model_threshold": state["model_threshold"],
        "db_update": db_update,
        "control": {
            **state,
            **db_update,
            "active_policy": active_policy,
            "auto_promote_locked_until": _isoformat(lock_until),
        },
    }
