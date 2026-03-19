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


def sweep_entry_filters(snapshot_rows, outcome_labels, threshold_plan=None):
    labels_by_mint = {row["mint"]: row for row in outcome_labels if row.get("mint")}
    entries = _entry_rows(snapshot_rows)
    if threshold_plan is None:
        threshold_plan = {
            "composite_score": [45, 55, 65, 75],
            "confidence": [0.35, 0.5, 0.65, 0.8],
            "buy_sell_ratio": [1.0, 1.5, 2.0, 3.0],
            "smart_wallet_buys": [1, 2, 3],
            "net_flow_sol": [0.0, 2.0, 5.0, 10.0],
        }

    sweeps = []
    for feature_name, thresholds in threshold_plan.items():
        for threshold in thresholds:
            selected = []
            for entry in entries:
                value = _safe_float(entry["features"].get(feature_name))
                if value >= threshold:
                    outcome = labels_by_mint.get(entry["mint"])
                    if outcome:
                        selected.append((entry, outcome))
            if not selected:
                sweeps.append({
                    "feature": feature_name,
                    "threshold": threshold,
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
            for feature_name in threshold_plan
        ],
        "all": sorted(sweeps, key=lambda item: (item["edge_score"], item["winner_rate_pct"], item["selected"]), reverse=True),
    }

