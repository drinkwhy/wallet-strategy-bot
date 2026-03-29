import json
from datetime import UTC, datetime


KNOWN_POLICY_ORDER = {
    "rule_balanced": 0,
    "model_global": 1,
    "model_regime_auto": 2,
}


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _parse_json(value, default):
    if value in (None, "", b""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value)
        return parsed if parsed is not None else default
    except Exception:
        return default


def _isoformat(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _normalize_policies(policy_rows):
    normalized = []
    for row in policy_rows or []:
        if not isinstance(row, dict):
            continue
        policy_name = row.get("policy_name") or row.get("strategy_name") or "unknown"
        normalized.append({
            "policy_name": policy_name,
            "decisions": _safe_int(row.get("decisions")),
            "passed": _safe_int(row.get("passed")),
            "blocked": _safe_int(row.get("blocked")),
            "trades_opened": _safe_int(row.get("trades_opened")),
            "trades_closed": _safe_int(row.get("trades_closed") or row.get("closed_trades")),
            "closed_trades": _safe_int(row.get("closed_trades") or row.get("trades_closed")),
            "wins": _safe_int(row.get("wins")),
            "win_rate": round(_safe_float(row.get("win_rate")), 1),
            "avg_pnl_pct": round(_safe_float(row.get("avg_pnl_pct")), 2),
            "avg_upside_pct": round(_safe_float(row.get("avg_upside_pct")), 2),
            "avg_drawdown_pct": round(_safe_float(row.get("avg_drawdown_pct")), 2),
            "best_trade_pct": round(_safe_float(row.get("best_trade_pct")), 2),
            "worst_trade_pct": round(_safe_float(row.get("worst_trade_pct")), 2),
        })
    return sorted(
        normalized,
        key=lambda item: (
            KNOWN_POLICY_ORDER.get(item["policy_name"], 99),
            -item["avg_pnl_pct"],
            -item["win_rate"],
        ),
    )


def _normalize_trades(trade_rows, limit=8):
    normalized = []
    for row in trade_rows or []:
        if not isinstance(row, dict):
            continue
        normalized.append({
            "policy_name": row.get("policy_name") or row.get("strategy_name") or "unknown",
            "mint": row.get("mint"),
            "name": row.get("name"),
            "entry_price": round(_safe_float(row.get("entry_price")), 8),
            "exit_price": round(_safe_float(row.get("exit_price")), 8),
            "realized_pnl_pct": round(_safe_float(row.get("realized_pnl_pct")), 2),
            "max_upside_pct": round(_safe_float(row.get("max_upside_pct")), 2),
            "max_drawdown_pct": round(_safe_float(row.get("max_drawdown_pct")), 2),
            "exit_reason": row.get("exit_reason"),
        })
    return normalized[: max(1, int(limit))]


def _policy_map(policies):
    return {item["policy_name"]: item for item in (policies or []) if item.get("policy_name")}


def _best_policy(policies):
    ranked = sorted(
        policies or [],
        key=lambda item: (item.get("avg_pnl_pct", 0), item.get("win_rate", 0), -item.get("avg_drawdown_pct", 0)),
        reverse=True,
    )
    return ranked[0] if ranked else {}


def _best_model_policy(policy_map):
    candidates = [policy_map.get("model_global"), policy_map.get("model_regime_auto")]
    candidates = [item for item in candidates if item]
    return _best_policy(candidates)


def build_edge_report(report_kind, window_days, model_threshold, active_regime, comparison_summary, top_trades=None, generated_at=None):
    normalized_policies = _normalize_policies((comparison_summary or {}).get("policies") or [])
    policy_map = _policy_map(normalized_policies)
    leader = _best_policy(normalized_policies)
    baseline = policy_map.get("rule_balanced") or {}
    best_model = _best_model_policy(policy_map)
    global_model = policy_map.get("model_global") or {}
    regime_model = policy_map.get("model_regime_auto") or {}
    generated = generated_at or datetime.now(UTC)

    summary = {
        "comparison_mode": (comparison_summary or {}).get("comparison_mode") or "rules_vs_models",
        "tokens_processed": _safe_int((comparison_summary or {}).get("tokens_processed")),
        "snapshots_processed": _safe_int((comparison_summary or {}).get("snapshots_processed")),
        "policy_count": len(normalized_policies),
        "leader_policy": leader.get("policy_name"),
        "leader_avg_pnl_pct": round(_safe_float(leader.get("avg_pnl_pct")), 2),
        "leader_win_rate": round(_safe_float(leader.get("win_rate")), 1),
        "rule_avg_pnl_pct": round(_safe_float(baseline.get("avg_pnl_pct")), 2),
        "rule_win_rate": round(_safe_float(baseline.get("win_rate")), 1),
        "best_model_policy": best_model.get("policy_name"),
        "best_model_avg_pnl_pct": round(_safe_float(best_model.get("avg_pnl_pct")), 2),
        "model_edge_pct": round(_safe_float(best_model.get("avg_pnl_pct")) - _safe_float(baseline.get("avg_pnl_pct")), 2),
        "global_edge_pct": round(_safe_float(global_model.get("avg_pnl_pct")) - _safe_float(baseline.get("avg_pnl_pct")), 2),
        "regime_edge_pct": round(_safe_float(regime_model.get("avg_pnl_pct")) - _safe_float(baseline.get("avg_pnl_pct")), 2),
        "generated_at": _isoformat(generated),
    }

    return {
        "report_kind": report_kind or "scheduled",
        "window_days": max(1, _safe_int(window_days, 7)),
        "model_threshold": round(_safe_float(model_threshold, 60.0), 2),
        "active_regime": active_regime or "neutral",
        "generated_at": _isoformat(generated),
        "summary": summary,
        "policies": normalized_policies,
        "top_trades": _normalize_trades(top_trades or []),
    }


def parse_edge_report_row(row):
    summary = _parse_json((row or {}).get("summary_json"), {})
    policy_payload = _parse_json((row or {}).get("policy_json"), {})
    policies = []
    top_trades = []
    if isinstance(policy_payload, dict):
        policies = _normalize_policies(policy_payload.get("policies") or [])
        top_trades = _normalize_trades(policy_payload.get("top_trades") or [])
    elif isinstance(policy_payload, list):
        policies = _normalize_policies(policy_payload)
    report = {
        "id": (row or {}).get("id"),
        "report_kind": (row or {}).get("report_kind") or "scheduled",
        "window_days": max(1, _safe_int((row or {}).get("window_days"), 7)),
        "model_threshold": round(_safe_float((row or {}).get("model_threshold"), 60.0), 2),
        "active_regime": (row or {}).get("active_regime") or "neutral",
        "generated_at": _isoformat((row or {}).get("generated_at")),
        "summary": summary if isinstance(summary, dict) else {},
        "policies": policies,
        "top_trades": top_trades,
    }
    if "generated_at" not in report["summary"]:
        report["summary"]["generated_at"] = report["generated_at"]
    return report


def summarize_edge_report_history(report_rows, focus_window_days=7):
    reports = [parse_edge_report_row(row) for row in (report_rows or [])]
    groups = {}
    for report in reports:
        groups.setdefault(report["window_days"], []).append(report)

    latest_by_window = []
    trend_cards = []
    for window_days in sorted(groups):
        bucket = groups[window_days]
        latest = bucket[0]
        previous = bucket[1] if len(bucket) > 1 else None
        latest_summary = latest.get("summary") or {}
        previous_summary = (previous or {}).get("summary") or {}
        latest_by_window.append({
            "id": latest.get("id"),
            "window_days": window_days,
            "report_kind": latest.get("report_kind"),
            "generated_at": latest.get("generated_at"),
            "active_regime": latest.get("active_regime"),
            "leader_policy": latest_summary.get("leader_policy"),
            "leader_avg_pnl_pct": round(_safe_float(latest_summary.get("leader_avg_pnl_pct")), 2),
            "rule_avg_pnl_pct": round(_safe_float(latest_summary.get("rule_avg_pnl_pct")), 2),
            "model_edge_pct": round(_safe_float(latest_summary.get("model_edge_pct")), 2),
            "regime_edge_pct": round(_safe_float(latest_summary.get("regime_edge_pct")), 2),
            "global_edge_pct": round(_safe_float(latest_summary.get("global_edge_pct")), 2),
        })
        trend_cards.append({
            "window_days": window_days,
            "generated_at": latest.get("generated_at"),
            "leader_policy": latest_summary.get("leader_policy"),
            "leader_avg_pnl_pct": round(_safe_float(latest_summary.get("leader_avg_pnl_pct")), 2),
            "rule_avg_pnl_pct": round(_safe_float(latest_summary.get("rule_avg_pnl_pct")), 2),
            "model_edge_pct": round(_safe_float(latest_summary.get("model_edge_pct")), 2),
            "regime_edge_pct": round(_safe_float(latest_summary.get("regime_edge_pct")), 2),
            "global_edge_pct": round(_safe_float(latest_summary.get("global_edge_pct")), 2),
            "delta_leader_avg_pnl_pct": None if previous is None else round(
                _safe_float(latest_summary.get("leader_avg_pnl_pct")) - _safe_float(previous_summary.get("leader_avg_pnl_pct")),
                2,
            ),
            "delta_model_edge_pct": None if previous is None else round(
                _safe_float(latest_summary.get("model_edge_pct")) - _safe_float(previous_summary.get("model_edge_pct")),
                2,
            ),
            "delta_regime_edge_pct": None if previous is None else round(
                _safe_float(latest_summary.get("regime_edge_pct")) - _safe_float(previous_summary.get("regime_edge_pct")),
                2,
            ),
            "leader_changed": bool(previous and latest_summary.get("leader_policy") != previous_summary.get("leader_policy")),
        })

    leaderboard_map = {}
    for report in [groups[window][0] for window in groups]:
        for policy in report.get("policies") or []:
            bucket = leaderboard_map.setdefault(policy["policy_name"], {
                "policy_name": policy["policy_name"],
                "windows": 0,
                "avg_pnl_total": 0.0,
                "win_rate_total": 0.0,
            })
            bucket["windows"] += 1
            bucket["avg_pnl_total"] += _safe_float(policy.get("avg_pnl_pct"))
            bucket["win_rate_total"] += _safe_float(policy.get("win_rate"))

    leaderboard = []
    for bucket in leaderboard_map.values():
        windows = max(1, bucket["windows"])
        leaderboard.append({
            "policy_name": bucket["policy_name"],
            "windows": bucket["windows"],
            "avg_pnl_pct": round(bucket["avg_pnl_total"] / windows, 2),
            "avg_win_rate": round(bucket["win_rate_total"] / windows, 1),
        })
    leaderboard.sort(key=lambda item: (item["avg_pnl_pct"], item["avg_win_rate"]), reverse=True)

    focus_window = max(1, _safe_int(focus_window_days, 7))
    focus_report = (groups.get(focus_window) or [None])[0]
    return {
        "focus_window_days": focus_window,
        "focus_report": focus_report,
        "latest_by_window": latest_by_window,
        "trend_cards": trend_cards,
        "leaderboard": leaderboard,
        "recent_reports": reports[:12],
        "report_count": len(reports),
    }


def derive_edge_guard_state(history_summary):
    focus_report = (history_summary or {}).get("focus_report") or {}
    focus_summary = focus_report.get("summary") or {}
    focus_policies = focus_report.get("policies") or []
    latest_by_window = {
        int(item.get("window_days") or 0): item
        for item in ((history_summary or {}).get("latest_by_window") or [])
    }
    trend_cards = {
        int(item.get("window_days") or 0): item
        for item in ((history_summary or {}).get("trend_cards") or [])
    }
    total_closed_trades = sum(_safe_int(item.get("closed_trades")) for item in focus_policies)
    active_regime = focus_report.get("active_regime") or "neutral"
    leader_policy = focus_summary.get("leader_policy") or "unknown"
    model_edge_pct = round(_safe_float(focus_summary.get("model_edge_pct")), 2)
    regime_edge_pct = round(_safe_float(focus_summary.get("regime_edge_pct")), 2)
    leader_avg_pnl_pct = round(_safe_float(focus_summary.get("leader_avg_pnl_pct")), 2)
    latest_30d = latest_by_window.get(30) or {}
    has_long_term_report = bool(latest_30d)
    long_term_edge_pct = round(_safe_float(latest_30d.get("model_edge_pct")), 2)
    seven_day_trend = trend_cards.get((history_summary or {}).get("focus_window_days") or 7) or {}
    delta_model_edge_pct = seven_day_trend.get("delta_model_edge_pct")
    delta_regime_edge_pct = seven_day_trend.get("delta_regime_edge_pct")

    state = {
        "status": "warming_up",
        "action_label": "Warm-up — preset filters active",
        "reason": "Not enough report history yet; using preset optimization filters for entry quality.",
        "allow_new_entries": True,
        "size_multiplier": 0.9,
        "risk_multiplier": 0.9,
        "max_positions_cap": 3,
        "drawdown_multiplier": 0.9,
        "prefer_rules_only": False,
        "report_count": _safe_int((history_summary or {}).get("report_count")),
        "focus_window_days": _safe_int((history_summary or {}).get("focus_window_days"), 7),
        "active_regime": active_regime,
        "leader_policy": leader_policy,
        "leader_avg_pnl_pct": leader_avg_pnl_pct,
        "model_edge_pct": model_edge_pct,
        "regime_edge_pct": regime_edge_pct,
        "long_term_edge_pct": long_term_edge_pct,
        "total_closed_trades": total_closed_trades,
        "delta_model_edge_pct": delta_model_edge_pct,
        "delta_regime_edge_pct": delta_regime_edge_pct,
        "source_report_id": focus_report.get("id"),
        "source_generated_at": focus_report.get("generated_at") or focus_summary.get("generated_at"),
    }

    if not focus_report or not focus_policies:
        return state

    if state["report_count"] < 2 or total_closed_trades < 4:
        return state

    if (
        model_edge_pct <= -4.0
        or regime_edge_pct <= -4.0
        or (active_regime == "defensive" and leader_avg_pnl_pct <= 0)
        or (has_long_term_report and long_term_edge_pct <= -3.0 and active_regime in {"distribution", "defensive"})
    ):
        state.update({
            "status": "halted",
            "action_label": "Safety halt",
            "reason": "Recent report edge is materially negative; block new entries until the reports recover.",
            "allow_new_entries": False,
            "size_multiplier": 0.0,
            "risk_multiplier": 0.0,
            "max_positions_cap": 0,
            "drawdown_multiplier": 0.5,
            "prefer_rules_only": True,
        })
        return state

    if (
        active_regime in {"distribution", "defensive"}
        or regime_edge_pct < 0
        or leader_policy == "rule_balanced"
        or (has_long_term_report and long_term_edge_pct < 1.0)
    ):
        state.update({
            "status": "defensive",
            "action_label": "Defensive — relaxed",
            "reason": "Tape regime or report leadership is weak; using preset filters instead of hard throttle.",
            "allow_new_entries": True,
            "size_multiplier": 0.85,
            "risk_multiplier": 0.85,
            "max_positions_cap": 3,
            "drawdown_multiplier": 0.85,
            "prefer_rules_only": True,
        })
        return state

    if (
        model_edge_pct < 2.0
        or regime_edge_pct < 1.5
        or (delta_model_edge_pct is not None and _safe_float(delta_model_edge_pct) < -2.0)
        or (delta_regime_edge_pct is not None and _safe_float(delta_regime_edge_pct) < -2.0)
    ):
        state.update({
            "status": "throttled",
            "action_label": "Lightly throttled",
            "reason": "Edge is positive but trend softening; slight size reduction, preset filters handle quality.",
            "allow_new_entries": True,
            "size_multiplier": 0.85,
            "risk_multiplier": 0.85,
            "max_positions_cap": 3,
            "drawdown_multiplier": 0.9,
            "prefer_rules_only": False,
        })
        return state

    state.update({
        "status": "normal",
        "action_label": "Normal risk",
        "reason": "Reports are healthy enough to allow normal position sizing and exposure.",
        "allow_new_entries": True,
        "size_multiplier": 1.0,
        "risk_multiplier": 1.0,
        "max_positions_cap": None,
        "drawdown_multiplier": 1.0,
        "prefer_rules_only": False,
    })
    return state
