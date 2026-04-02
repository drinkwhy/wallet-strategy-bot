import json


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
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, type(default)) else default
    except Exception:
        return default


def build_outcome_labels(token_rows, winner_threshold_pct=120.0, rug_threshold_pct=-45.0):
    labels = []
    for row in token_rows or []:
        first_price = _safe_float(row.get("first_price"))
        peak_price = _safe_float(row.get("peak_price"))
        trough_price = _safe_float(row.get("trough_price"))
        last_price = _safe_float(row.get("last_price"))
        if first_price <= 0 or peak_price <= 0:
            continue
        peak_return_pct = ((peak_price / first_price) - 1.0) * 100.0
        trough_return_pct = ((trough_price / first_price) - 1.0) * 100.0 if trough_price > 0 else 0.0
        last_return_pct = ((last_price / first_price) - 1.0) * 100.0 if last_price > 0 else 0.0
        label = "neutral"
        if peak_return_pct >= winner_threshold_pct and trough_return_pct <= rug_threshold_pct:
            label = "volatile_winner"
        elif peak_return_pct >= winner_threshold_pct:
            label = "winner"
        elif trough_return_pct <= rug_threshold_pct and last_return_pct <= 0:
            label = "rug"
        elif last_return_pct > 25:
            label = "survivor"
        labels.append({
            "mint": row.get("mint"),
            "name": row.get("name") or row.get("mint") or "Unknown",
            "label": label,
            "peak_return_pct": round(peak_return_pct, 2),
            "trough_return_pct": round(trough_return_pct, 2),
            "last_return_pct": round(last_return_pct, 2),
        })
    return labels


def _entry_rows(snapshot_rows):
    entries = {}
    for row in sorted(snapshot_rows or [], key=lambda item: item.get("created_at") or ""):
        mint = row.get("mint")
        if not mint or mint in entries:
            continue
        features = _parse_json(row.get("feature_json"), {})
        if not isinstance(features, dict):
            features = {}
        entries[mint] = {
            "mint": mint,
            "name": row.get("name") or row.get("mint") or "Unknown",
            "price": _safe_float(row.get("price") or features.get("price")),
            "created_at": row.get("created_at"),
            "features": features,
        }
    return list(entries.values())


def summarize_feature_edges(snapshot_rows, outcome_labels, top_n=6):
    labels_by_mint = {row["mint"]: row for row in outcome_labels if row.get("mint")}
    entries = _entry_rows(snapshot_rows)
    winners = [entry for entry in entries if labels_by_mint.get(entry["mint"], {}).get("label") in {"winner", "volatile_winner"}]
    rugs = [entry for entry in entries if labels_by_mint.get(entry["mint"], {}).get("label") == "rug"]
    if not winners or not rugs:
        return []

    feature_specs = [
        ("composite_score", False, "Composite score"),
        ("confidence", False, "Confidence"),
        ("buy_sell_ratio", False, "Buy/sell ratio"),
        ("net_flow_sol", False, "Net SOL flow"),
        ("smart_wallet_buys", False, "Smart wallet buys"),
        ("unique_buyer_count", False, "Unique buyers"),
        ("volume_spike_ratio", False, "Volume spike"),
        ("holder_growth_1h", False, "Holder growth"),
        ("green_lights", False, "Green lights"),
        ("threat_risk_score", True, "Threat risk"),
        ("liquidity_drop_pct", True, "Liquidity drop"),
    ]

    edges = []
    for key, invert, label in feature_specs:
        winner_avg = sum(_safe_float(item["features"].get(key)) for item in winners) / len(winners)
        rug_avg = sum(_safe_float(item["features"].get(key)) for item in rugs) / len(rugs)
        raw_edge = rug_avg - winner_avg if invert else winner_avg - rug_avg
        edges.append({
            "feature": key,
            "label": label,
            "winner_avg": round(winner_avg, 2),
            "rug_avg": round(rug_avg, 2),
            "edge": round(raw_edge, 2),
        })
    return sorted(edges, key=lambda item: abs(item["edge"]), reverse=True)[:top_n]


def summarize_regime_edges(snapshot_rows, outcome_labels, flow_rows, top_n=6):
    """Calculate feature edges separately for each market regime.

    Uses stored regime_label from flow snapshots (or classifies on-the-fly) to assign
    each token to a regime, then computes winner-vs-rug feature edges within that regime.

    Returns dict keyed by regime name, each containing a list of feature edges.
    """
    labels_by_mint = {row["mint"]: row for row in outcome_labels if row.get("mint")}
    entries = _entry_rows(snapshot_rows)

    # Build regime membership for each mint from flow snapshots
    # Prefer stored regime_label; fall back to on-the-fly classification
    flow_by_mint = {}
    for row in flow_rows or []:
        mint = row.get("mint")
        if not mint:
            continue
        # Use stored regime_label if available, otherwise classify from metrics
        regime = row.get("regime_label")
        if not regime or regime == "neutral":
            # Try to classify from metrics
            threat = _safe_float(row.get("threat_risk_score"))
            liq_drop = _safe_float(row.get("liquidity_drop_pct"))
            bsr = _safe_float(row.get("buy_sell_ratio"))
            net_flow = _safe_float(row.get("net_flow_sol"))
            can_exit = row.get("can_exit")
            if can_exit in (0, "0"):
                can_exit = False
            elif can_exit in (1, "1"):
                can_exit = True
            if threat >= 70 or can_exit is False or liq_drop >= 35:
                regime = "defensive"
            elif net_flow > 2.5 and bsr > 1.35 and threat < 45:
                regime = "accumulation"
            elif net_flow < -1.5 or bsr < 0.9:
                regime = "distribution"
            else:
                regime = "neutral"
        if mint not in flow_by_mint:
            flow_by_mint[mint] = regime

    # Assign each entry to its regime
    regime_entries = {}
    for entry in entries:
        regime = flow_by_mint.get(entry["mint"], "neutral")
        regime_entries.setdefault(regime, []).append(entry)

    feature_specs = [
        ("composite_score", False, "Composite score"),
        ("confidence", False, "Confidence"),
        ("buy_sell_ratio", False, "Buy/sell ratio"),
        ("net_flow_sol", False, "Net SOL flow"),
        ("smart_wallet_buys", False, "Smart wallet buys"),
        ("unique_buyer_count", False, "Unique buyers"),
        ("volume_spike_ratio", False, "Volume spike"),
        ("holder_growth_1h", False, "Holder growth"),
        ("green_lights", False, "Green lights"),
        ("threat_risk_score", True, "Threat risk"),
        ("liquidity_drop_pct", True, "Liquidity drop"),
    ]

    result = {}
    for regime_name, r_entries in regime_entries.items():
        winners = [e for e in r_entries if labels_by_mint.get(e["mint"], {}).get("label") in {"winner", "volatile_winner"}]
        rugs = [e for e in r_entries if labels_by_mint.get(e["mint"], {}).get("label") == "rug"]
        if len(winners) < 2 or len(rugs) < 2:
            result[regime_name] = {
                "edges": [],
                "token_count": len(r_entries),
                "winner_count": len(winners),
                "rug_count": len(rugs),
                "insufficient_data": True,
            }
            continue

        edges = []
        for key, invert, label in feature_specs:
            winner_avg = sum(_safe_float(item["features"].get(key)) for item in winners) / len(winners)
            rug_avg = sum(_safe_float(item["features"].get(key)) for item in rugs) / len(rugs)
            raw_edge = rug_avg - winner_avg if invert else winner_avg - rug_avg
            edges.append({
                "feature": key,
                "label": label,
                "winner_avg": round(winner_avg, 2),
                "rug_avg": round(rug_avg, 2),
                "edge": round(raw_edge, 2),
            })

        result[regime_name] = {
            "edges": sorted(edges, key=lambda item: abs(item["edge"]), reverse=True)[:top_n],
            "token_count": len(r_entries),
            "winner_count": len(winners),
            "rug_count": len(rugs),
            "insufficient_data": False,
        }

    return result


def sweep_entry_filters(snapshot_rows, outcome_labels, threshold_plan=None, direction_map=None):
    """Sweep entry filter thresholds to find values that best separate winners from rugs.

    Args:
        snapshot_rows: token feature snapshots
        outcome_labels: labelled outcomes from build_outcome_labels
        threshold_plan: dict mapping feature name -> list of threshold values to test
        direction_map: dict mapping feature name -> "min" or "max". Features with "max"
            select tokens where value <= threshold (inverted filters like max_age_min,
            max_threat_score, max_hot_change). All other features default to "min" (>=).
    """
    labels_by_mint = {row["mint"]: row for row in outcome_labels if row.get("mint")}
    entries = _entry_rows(snapshot_rows)
    if threshold_plan is None:
        threshold_plan = {
            "composite_score": [45, 55, 65, 75],
            "confidence": [0.35, 0.5, 0.65, 0.8],
            "buy_sell_ratio": [1.0, 1.5, 2.0, 3.0],
            "smart_wallet_buys": [1, 2, 3],
            "net_flow_sol": [0.0, 2.0, 5.0, 10.0],
            "green_lights": [1, 2, 3],
        }
    direction_map = direction_map or {}

    normalized_plan = {}
    for feature_name, spec in threshold_plan.items():
        if isinstance(spec, dict):
            thresholds = list(spec.get("thresholds") or [])
            direction = str(spec.get("direction") or "gte").strip().lower()
            min_selected = _safe_int(spec.get("min_selected"), 0)
        else:
            thresholds = list(spec or [])
            direction = "gte"
            min_selected = 0
        if direction not in {"gte", "lte"}:
            direction = "gte"
        normalized_plan[feature_name] = {
            "thresholds": thresholds,
            "direction": direction,
            "min_selected": min_selected,
        }

    sweeps = []
    for feature_name, plan in normalized_plan.items():
        thresholds = plan["thresholds"]
        direction = plan["direction"]
        min_selected = plan["min_selected"]
        for threshold in thresholds:
            selected = []
            for entry in entries:
                raw_value = entry["features"].get(feature_name)
                if raw_value in (None, ""):
                    continue
                value = _safe_float(raw_value)
                passed = value >= threshold if direction == "gte" else value <= threshold
                if passed:
                    outcome = labels_by_mint.get(entry["mint"])
                    if outcome:
                        selected.append((entry, outcome))
            if not selected:
                sweeps.append({
                    "feature": feature_name,
                    "threshold": threshold,
                    "direction": direction,
                    "min_selected": min_selected,
                    "selected": 0,
                    "winner_rate_pct": 0.0,
                    "rug_rate_pct": 0.0,
                    "avg_peak_return_pct": 0.0,
                    "avg_last_return_pct": 0.0,
                    "edge_score": 0.0,
                })
                continue

            winners = [item for item in selected if item[1]["label"] in {"winner", "volatile_winner"}]
            rugs = [item for item in selected if item[1]["label"] == "rug"]
            avg_peak = sum(item[1]["peak_return_pct"] for item in selected) / len(selected)
            avg_last = sum(item[1]["last_return_pct"] for item in selected) / len(selected)
            rug_rate = len(rugs) / len(selected)
            winner_rate = len(winners) / len(selected)
            edge_score = (avg_last * 0.45) + (avg_peak * 0.25) + (winner_rate * 100.0 * 0.35) - (rug_rate * 100.0 * 0.55)
            sweeps.append({
                "feature": feature_name,
                "threshold": threshold,
                "direction": direction,
                "min_selected": min_selected,
                "selected": len(selected),
                "winner_rate_pct": round(winner_rate * 100.0, 1),
                "rug_rate_pct": round(rug_rate * 100.0, 1),
                "avg_peak_return_pct": round(avg_peak, 2),
                "avg_last_return_pct": round(avg_last, 2),
                "edge_score": round(edge_score, 2),
            })

    return {
        "best_by_feature": [
            max(
                [row for row in sweeps if row["feature"] == feature_name],
                key=lambda item: (item["edge_score"], item["winner_rate_pct"], item["selected"]),
            )
            for feature_name in normalized_plan
        ],
        "all": sorted(sweeps, key=lambda item: (item["edge_score"], item["winner_rate_pct"], item["selected"]), reverse=True),
    }


# ---------------------------------------------------------------------------
# Exit-parameter sweep — runs the full position lifecycle across a grid of
# tp/stop/trail combos in a SINGLE backtest pass and returns the best combo.
# ---------------------------------------------------------------------------

_EXIT_SWEEP_GRID = {
    "tp1_mult":      [1.2, 1.4, 1.6],
    "tp2_mult":      [2.0, 3.0, 5.0, 8.0],
    "stop_loss":     [0.65, 0.72, 0.80, 0.88],
    "trail_pct":     [0.12, 0.20, 0.30],
    "time_stop_min": [15, 25, 40],
    # Position size — no longer tier-locked; optimizer picks the best SOL amount
    # given actual friction/slippage from historical event tape.
    "max_buy_sol":   [0.02, 0.04, 0.07, 0.10, 0.20, 0.50],
}


def sweep_exit_params(event_rows, base_settings, exit_grid=None, min_trades=8):
    """Sweep exit parameter combinations against the live event tape.

    All combos are evaluated in a SINGLE simulate_event_tape_backtest pass
    (each combo becomes a separate "strategy" variant) so data is read once.

    Parameters
    ----------
    event_rows   : list of event-tape rows (from _load_backtest_event_tape)
    base_settings: dict — the current strategy preset to use as the base
    exit_grid    : optional override for the sweep grid; defaults to _EXIT_SWEEP_GRID
    min_trades   : minimum closed trades required to accept a combo result

    Returns
    -------
    dict with keys:
      best_exit_settings  — {tp1_mult, tp2_mult, stop_loss, trail_pct, time_stop_min}
      best_avg_pnl        — winning combo's avg_pnl_pct
      best_win_rate       — winning combo's win_rate
      best_trades         — trade count for the winning combo
      combos_tested       — number of valid combos evaluated
      kelly_risk_pct      — quarter-Kelly position size recommendation
    Returns None if there is not enough data.
    """
    import itertools

    # Lazy import to avoid circular dependency (backtest_engine imports quant_platform
    # which imports optimizer_engine, so we must defer the import here)
    try:
        from backtest_engine import simulate_event_tape_backtest
    except ImportError:
        return None

    if not event_rows or len(event_rows) < 50:
        return None

    grid = exit_grid if isinstance(exit_grid, dict) else _EXIT_SWEEP_GRID
    keys = list(grid.keys())
    combos = []
    for vals in itertools.product(*[grid[k] for k in keys]):
        combo = dict(zip(keys, vals))
        # Skip combos where tp2 is not strictly above tp1 (would behave identically to tp1-only)
        if combo.get("tp2_mult", 2.0) <= combo.get("tp1_mult", 1.2):
            continue
        combos.append(combo)

    if not combos:
        return None

    # Build all combo variants as named strategy_settings for a single backtest pass
    strategy_settings = {}
    for idx, combo in enumerate(combos):
        settings_variant = {**base_settings, **combo}
        strategy_settings[f"_exit_combo_{idx}"] = settings_variant

    try:
        result = simulate_event_tape_backtest(
            run_id=-1,  # -1 = ephemeral sweep, never persisted
            event_rows=event_rows,
            strategy_settings=strategy_settings,
        )
    except Exception:
        return None

    strategies_summary = (result.get("summary") or {}).get("strategies") or {}

    best_score = None
    best_idx = None
    best_trades = 0
    best_avg_pnl = 0.0
    best_win_rate = 0.0

    for idx, combo in enumerate(combos):
        key = f"_exit_combo_{idx}"
        strat = strategies_summary.get(key) or {}
        closed = strat.get("closed_trades", 0) or strat.get("wins", 0) + strat.get("losses", 0)
        if closed < min_trades:
            continue
        avg_pnl = float(strat.get("avg_pnl_pct") or 0)
        win_rate = float(strat.get("win_rate") or 0)
        # Score: weight avg_pnl + bonus for win rate, penalise very low win rates
        score = avg_pnl + (win_rate * 0.3) if win_rate >= 30 else avg_pnl * (win_rate / 30.0)
        if best_score is None or score > best_score:
            best_score = score
            best_idx = idx
            best_trades = closed
            best_avg_pnl = avg_pnl
            best_win_rate = win_rate

    if best_idx is None:
        return None

    best_combo = combos[best_idx]

    # Quarter-Kelly position sizing: f* = (edge / odds) * 0.25, clamped [1%, 5%]
    # Using win_rate as P(win) and avg_pnl as the simplified edge proxy
    p_win = best_win_rate / 100.0
    p_lose = 1.0 - p_win
    if p_win > 0 and p_lose > 0 and best_avg_pnl > 0:
        # Simplified Kelly using avg_pnl as the net gain fraction
        odds = best_avg_pnl / 100.0  # expected gain per unit risked
        kelly_f = (p_win - p_lose / max(odds, 0.01)) * 0.25
        kelly_risk_pct = round(max(1.0, min(5.0, kelly_f * 100.0)), 2)
    else:
        kelly_risk_pct = 2.0  # safe default

    return {
        "best_exit_settings": best_combo,
        "best_avg_pnl": round(best_avg_pnl, 2),
        "best_win_rate": round(best_win_rate, 1),
        "best_trades": best_trades,
        "combos_tested": len(combos),
        "kelly_risk_pct": kelly_risk_pct,
    }


# ---------------------------------------------------------------------------
# Risk-threshold sweep — tests different values for RISK_THRESHOLDS entries
# that can be validated against historical winner/rug outcome data.
#
# Only thresholds whose corresponding feature is recorded in token_feature_snapshots
# (via feature_json) are included. Safety circuit-breaker thresholds are intentionally
# excluded and remain fixed.
#
# Tunable thresholds:
#   min_liquidity_usd  ← driven by "liq" in feature_json
#   max_token_age_sec  ← driven by "age_min" (minutes) in feature_json; converted to days for thresholds
# ---------------------------------------------------------------------------

_RISK_THRESHOLD_SWEEP_PLAN = {
    "liq": {
        "thresholds": [1000, 2000, 3000, 5000, 7500, 10000],
        "direction": "gte",
        "risk_key": "min_liquidity_usd",
        "cast": float,
        "min_selected": 8,
    },
    "age_min": {
        # Thresholds are in days; they are converted to seconds when applied.
        # Direction "lte" means: accept tokens where age_min <= threshold (max age cap)
        "thresholds": [30, 60, 90, 180, 365, 730],
        "direction": "lte",
        "risk_key": "max_token_age_sec",
        "cast": lambda x: int(float(x) * 86400),  # days → seconds
        "min_selected": 8,
    },
}


def sweep_risk_thresholds(snapshot_rows, outcome_labels, plan=None):
    """Sweep risk-engine threshold values to find settings that best separate
    winners from rugs using the same edge-scoring logic as sweep_entry_filters.

    Only thresholds for which feature data exists in token_feature_snapshots are
    tested.  Safety circuit-breaker thresholds (slippage, fees, circuit-breaker
    loss %, fail rate) are intentionally left out and stay fixed.

    Args:
        snapshot_rows: token feature snapshots (same format as sweep_entry_filters)
        outcome_labels: labelled outcomes from build_outcome_labels
        plan: optional override for _RISK_THRESHOLD_SWEEP_PLAN

    Returns:
        dict mapping RISK_THRESHOLDS key → optimized value, or {} if insufficient data.
    """
    sweep_plan = plan if isinstance(plan, dict) else _RISK_THRESHOLD_SWEEP_PLAN
    labels_by_mint = {row["mint"]: row for row in outcome_labels if row.get("mint")}
    entries = _entry_rows(snapshot_rows)

    if not entries or not labels_by_mint:
        return {}

    best_values = {}

    for feature_name, spec in sweep_plan.items():
        thresholds = list(spec.get("thresholds") or [])
        direction = str(spec.get("direction") or "gte").strip().lower()
        risk_key = spec.get("risk_key")
        cast_fn = spec.get("cast", float)
        min_selected = _safe_int(spec.get("min_selected"), 8)

        if not thresholds or not risk_key:
            continue

        best_score = None
        best_threshold = None

        for threshold in thresholds:
            selected = []
            for entry in entries:
                raw_value = entry["features"].get(feature_name)
                if raw_value in (None, ""):
                    continue
                value = _safe_float(raw_value)
                passed = value >= threshold if direction == "gte" else value <= threshold
                if passed:
                    outcome = labels_by_mint.get(entry["mint"])
                    if outcome:
                        selected.append((entry, outcome))

            if len(selected) < min_selected:
                continue

            winners = [item for item in selected if item[1]["label"] in {"winner", "volatile_winner"}]
            rugs = [item for item in selected if item[1]["label"] == "rug"]
            avg_peak = sum(item[1]["peak_return_pct"] for item in selected) / len(selected)
            avg_last = sum(item[1]["last_return_pct"] for item in selected) / len(selected)
            rug_rate = len(rugs) / len(selected)
            winner_rate = len(winners) / len(selected)
            edge_score = (avg_last * 0.45) + (avg_peak * 0.25) + (winner_rate * 100.0 * 0.35) - (rug_rate * 100.0 * 0.55)

            # Require a meaningful positive edge and reasonable rug suppression
            if edge_score <= 2 or rug_rate > 0.40 or winner_rate < 0.20:
                continue

            if best_score is None or edge_score > best_score:
                best_score = edge_score
                best_threshold = threshold

        if best_threshold is not None:
            try:
                best_values[risk_key] = cast_fn(best_threshold)
            except Exception:
                pass

    return best_values
