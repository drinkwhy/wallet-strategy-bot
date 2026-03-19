import json
import math


FEATURE_SPECS = [
    ("composite_score", "Composite score"),
    ("confidence", "Confidence"),
    ("buy_sell_ratio", "Buy/sell ratio"),
    ("net_flow_sol", "Net SOL flow"),
    ("smart_wallet_buys", "Smart wallet buys"),
    ("unique_buyer_count", "Unique buyers"),
    ("volume_spike_ratio", "Volume spike"),
    ("holder_growth_1h", "Holder growth"),
    ("threat_risk_score", "Threat risk"),
    ("liquidity_drop_pct", "Liquidity drop"),
]


def _safe_float(value, default=0.0):
    try:
        return float(value)
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
            "name": row.get("name") or mint or "Unknown",
            "price": _safe_float(row.get("price") or features.get("price")),
            "created_at": row.get("created_at"),
            "features": features,
        }
    return list(entries.values())


def _flow_value(row, key):
    if key in row and row.get(key) is not None:
        return row.get(key)
    flow_json = _parse_json(row.get("flow_json"), {})
    if isinstance(flow_json, dict):
        return flow_json.get(key)
    return None


def classify_flow_regime_row(row):
    buy_sell_ratio = _safe_float(_flow_value(row, "buy_sell_ratio"))
    net_flow_sol = _safe_float(_flow_value(row, "net_flow_sol"))
    threat_risk_score = _safe_float(_flow_value(row, "threat_risk_score"))
    liquidity_drop_pct = _safe_float(_flow_value(row, "liquidity_drop_pct"))
    can_exit = _flow_value(row, "can_exit")
    if can_exit in (0, "0"):
        can_exit = False
    elif can_exit in (1, "1"):
        can_exit = True
    if threat_risk_score >= 70 or can_exit is False or liquidity_drop_pct >= 35:
        return "defensive"
    if net_flow_sol > 2.5 and buy_sell_ratio > 1.35 and threat_risk_score < 45:
        return "accumulation"
    if net_flow_sol < -1.5 or buy_sell_ratio < 0.9:
        return "distribution"
    return "neutral"


def _flow_index(flow_rows):
    by_mint = {}
    for row in sorted(flow_rows or [], key=lambda item: (item.get("mint") or "", item.get("created_at") or "")):
        mint = row.get("mint")
        if not mint:
            continue
        by_mint.setdefault(mint, []).append(row)
    return by_mint


def _nearest_flow_regime(mint, created_at, flow_index):
    candidates = flow_index.get(mint) or []
    if not candidates:
        return "neutral"
    if created_at is None:
        return classify_flow_regime_row(candidates[-1])
    nearest = min(
        candidates,
        key=lambda row: abs(((row.get("created_at") or created_at) - created_at).total_seconds()) if row.get("created_at") else 10**12,
    )
    return classify_flow_regime_row(nearest)


def _mean(values):
    return sum(values) / len(values) if values else 0.0


def _variance(values, mean):
    if not values:
        return 0.0
    return sum((value - mean) ** 2 for value in values) / len(values)


def _sigmoid(value):
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def train_feature_model(snapshot_rows, outcome_labels):
    labels_by_mint = {row["mint"]: row for row in (outcome_labels or []) if row.get("mint")}
    entries = _entry_rows(snapshot_rows)
    winners = [entry for entry in entries if labels_by_mint.get(entry["mint"], {}).get("label") in {"winner", "volatile_winner"}]
    rugs = [entry for entry in entries if labels_by_mint.get(entry["mint"], {}).get("label") == "rug"]
    if not winners or not rugs:
        return {
            "trained": False,
            "winner_count": len(winners),
            "rug_count": len(rugs),
            "weights": [],
            "bias": 0.0,
            "accuracy_pct": 0.0,
        }

    weights = []
    bias = 0.0
    for feature_name, label in FEATURE_SPECS:
        winner_values = [_safe_float(item["features"].get(feature_name)) for item in winners]
        rug_values = [_safe_float(item["features"].get(feature_name)) for item in rugs]
        winner_mean = _mean(winner_values)
        rug_mean = _mean(rug_values)
        pooled_var = (_variance(winner_values, winner_mean) + _variance(rug_values, rug_mean)) / 2.0
        pooled_std = math.sqrt(max(pooled_var, 1e-9))
        direction = winner_mean - rug_mean
        raw_weight = direction / pooled_std if pooled_std else 0.0
        weight = max(-3.0, min(3.0, raw_weight))
        midpoint = (winner_mean + rug_mean) / 2.0
        weights.append({
            "feature": feature_name,
            "label": label,
            "weight": round(weight, 4),
            "winner_mean": round(winner_mean, 4),
            "rug_mean": round(rug_mean, 4),
            "midpoint": round(midpoint, 4),
            "scale": round(max(pooled_std, abs(direction) / 2.0, 1e-6), 6),
        })

    def _score_entry(entry):
        raw_score = bias
        contributions = []
        for spec in weights:
            value = _safe_float(entry["features"].get(spec["feature"]))
            scaled_value = (value - _safe_float(spec.get("midpoint"))) / max(_safe_float(spec.get("scale"), 1.0), 1e-6)
            contribution = spec["weight"] * scaled_value
            raw_score += contribution
            contributions.append({
                "feature": spec["feature"],
                "label": spec["label"],
                "value": round(value, 4),
                "contribution": round(contribution, 4),
            })
        probability = _sigmoid(raw_score / max(len(weights), 1))
        return raw_score, probability, contributions

    correct = 0
    classified = 0
    for entry in winners + rugs:
        _, probability, _ = _score_entry(entry)
        predicted_winner = probability >= 0.5
        actual_winner = entry in winners
        classified += 1
        if predicted_winner == actual_winner:
            correct += 1

    ordered_weights = sorted(weights, key=lambda item: abs(item["weight"]), reverse=True)
    return {
        "trained": True,
        "winner_count": len(winners),
        "rug_count": len(rugs),
        "weights": ordered_weights,
        "bias": round(bias, 4),
        "accuracy_pct": round((correct / classified) * 100.0, 1) if classified else 0.0,
    }


def score_recent_candidates(snapshot_rows, model, top_n=8):
    if not model or not model.get("trained"):
        return []
    weight_rows = model.get("weights") or []
    bias = _safe_float(model.get("bias"))
    entries = _entry_rows(snapshot_rows)
    ranked = []
    for entry in entries:
        raw_score = bias
        contributions = []
        for spec in weight_rows:
            value = _safe_float(entry["features"].get(spec["feature"]))
            scaled_value = (value - _safe_float(spec.get("midpoint"))) / max(_safe_float(spec.get("scale"), 1.0), 1e-6)
            contribution = _safe_float(spec["weight"]) * scaled_value
            raw_score += contribution
            contributions.append({
                "feature": spec["feature"],
                "label": spec["label"],
                "value": round(value, 4),
                "contribution": round(contribution, 4),
            })
        probability = _sigmoid(raw_score / max(len(weight_rows), 1))
        top_drivers = sorted(contributions, key=lambda item: abs(item["contribution"]), reverse=True)[:4]
        ranked.append({
            "mint": entry["mint"],
            "name": entry["name"],
            "price": round(entry["price"], 8),
            "model_score": round(probability * 100.0, 1),
            "raw_score": round(raw_score, 4),
            "top_drivers": top_drivers,
            "features": {
                "composite_score": round(_safe_float(entry["features"].get("composite_score")), 2),
                "confidence": round(_safe_float(entry["features"].get("confidence")), 4),
                "buy_sell_ratio": round(_safe_float(entry["features"].get("buy_sell_ratio")), 2),
                "net_flow_sol": round(_safe_float(entry["features"].get("net_flow_sol")), 2),
                "smart_wallet_buys": round(_safe_float(entry["features"].get("smart_wallet_buys")), 2),
                "threat_risk_score": round(_safe_float(entry["features"].get("threat_risk_score")), 2),
            },
            "created_at": entry["created_at"].isoformat() if hasattr(entry["created_at"], "isoformat") else entry["created_at"],
        })
    return sorted(ranked, key=lambda item: item["model_score"], reverse=True)[:top_n]


def train_regime_model_family(snapshot_rows, outcome_labels, flow_rows):
    global_model = train_feature_model(snapshot_rows, outcome_labels)
    flow_index = _flow_index(flow_rows)
    entries = _entry_rows(snapshot_rows)
    regime_membership = {}
    for entry in entries:
        regime = _nearest_flow_regime(entry["mint"], entry.get("created_at"), flow_index)
        regime_membership.setdefault(regime, []).append(entry["mint"])

    labels_by_mint = {row["mint"]: row for row in (outcome_labels or []) if row.get("mint")}
    regimes = {}
    for regime_name in ("accumulation", "distribution", "defensive", "neutral"):
        mints = set(regime_membership.get(regime_name) or [])
        regime_snapshots = [row for row in snapshot_rows or [] if row.get("mint") in mints]
        regime_outcomes = [labels_by_mint[mint] for mint in mints if mint in labels_by_mint]
        model = train_feature_model(regime_snapshots, regime_outcomes)
        model["regime"] = regime_name
        model["token_count"] = len(mints)
        model["trained"] = bool(model.get("trained"))
        regimes[regime_name] = model

    return {
        "global": {**global_model, "regime": "global", "token_count": len(entries)},
        "regimes": regimes,
    }


def select_model_for_regime(model_family, active_regime, mode="auto"):
    family = model_family or {}
    global_model = family.get("global") or {}
    regime_models = family.get("regimes") or {}
    normalized_mode = (mode or "auto").strip().lower()
    chosen_key = "global"
    fallback_reason = ""

    if normalized_mode in regime_models and regime_models[normalized_mode].get("trained"):
        chosen_key = normalized_mode
    elif normalized_mode == "auto":
        if regime_models.get(active_regime, {}).get("trained"):
            chosen_key = active_regime
        else:
            fallback_reason = f"{active_regime}_model_untrained"
    elif normalized_mode in regime_models and not regime_models[normalized_mode].get("trained"):
        fallback_reason = f"{normalized_mode}_model_untrained"
    elif normalized_mode not in {"auto", "global"}:
        fallback_reason = "unknown_mode"

    selected_model = global_model if chosen_key == "global" else regime_models.get(chosen_key) or global_model
    return {
        "mode": normalized_mode,
        "active_regime": active_regime,
        "selected_key": chosen_key,
        "selected_model": selected_model,
        "fallback_reason": fallback_reason,
        "using_global_fallback": chosen_key == "global" and normalized_mode not in {"global", ""},
    }


def score_recent_candidates_for_regime(snapshot_rows, model_family, active_regime, mode="auto", top_n=8):
    selection = select_model_for_regime(model_family, active_regime, mode=mode)
    ranked = score_recent_candidates(snapshot_rows, selection["selected_model"], top_n=top_n)
    for item in ranked:
        item["model_key"] = selection["selected_key"]
        item["active_regime"] = active_regime
        item["selection_mode"] = selection["mode"]
    return {
        "selection": selection,
        "ranked_candidates": ranked,
    }
