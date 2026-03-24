import json
import math
from dataclasses import dataclass


CANONICAL_STRATEGIES = ("safe", "balanced", "aggressive", "degen")


def _clamp(value, lo=0.0, hi=1.0):
    return max(lo, min(hi, value))


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


def _normalize(value, floor, ceiling):
    if ceiling <= floor:
        return 0.0
    return _clamp((_safe_float(value) - floor) / (ceiling - floor))


@dataclass
class ShadowDecision:
    strategy_name: str
    passed: bool
    score: float
    confidence: float
    pass_reasons: list
    blocker_reasons: list
    metrics: dict

    def as_dict(self):
        return {
            "strategy_name": self.strategy_name,
            "passed": self.passed,
            "score": round(self.score, 2),
            "confidence": round(self.confidence, 4),
            "pass_reasons": list(self.pass_reasons),
            "blocker_reasons": list(self.blocker_reasons),
            "metrics": dict(self.metrics),
        }


def build_feature_snapshot(info, intel=None):
    intel = intel or {}
    liq = _safe_float(info.get("liq"))
    vol = _safe_float(info.get("vol"))
    mc = _safe_float(info.get("mc"))
    change = _safe_float(info.get("change"))
    age_min = _safe_float(info.get("age_min"), 9999.0)
    ai_score = _safe_int(info.get("score"))
    momentum = _safe_float(info.get("momentum"))
    green_lights = _safe_int(info.get("green_lights") or intel.get("green_lights"))
    narrative_score = _safe_int(info.get("narrative_score") or intel.get("narrative_score"))
    deployer_score = _safe_int(info.get("deployer_score") or intel.get("deployer_score"))
    whale_score = _safe_int(intel.get("whale_score"))
    whale_action_score = _safe_int(intel.get("whale_action_score"))
    holder_growth_1h = _safe_float(intel.get("holder_growth_1h"))
    volume_spike_ratio = _safe_float(intel.get("volume_spike_ratio"))
    threat_risk_score = _safe_int(intel.get("threat_risk_score"))
    cluster_confidence = _safe_int(intel.get("cluster_confidence"))
    unique_buyer_count = _safe_int(intel.get("unique_buyer_count"))
    unique_seller_count = _safe_int(intel.get("unique_seller_count"))
    first_buyer_count = _safe_int(intel.get("first_buyer_count"))
    smart_wallet_buys = _safe_int(intel.get("smart_wallet_buys"))
    smart_wallet_first10 = _safe_int(intel.get("smart_wallet_first10"))
    total_buy_sol = _safe_float(intel.get("total_buy_sol"))
    total_sell_sol = _safe_float(intel.get("total_sell_sol"))
    net_flow_sol = _safe_float(intel.get("net_flow_sol"))
    buy_sell_ratio = _safe_float(intel.get("buy_sell_ratio"))
    liquidity_drop_pct = _safe_float(intel.get("liquidity_drop_pct"))
    transfer_hook_enabled = bool(intel.get("transfer_hook_enabled"))
    can_exit = intel.get("can_exit")

    liquidity_quality = _normalize(liq, 3_000, 50_000)
    volume_quality = _normalize(vol, 800, 40_000)
    momentum_quality = _normalize(change, -10, 120)
    ai_quality = _normalize(ai_score, 0, 100)
    narrative_quality = _normalize(narrative_score, 0, 60)
    deployer_quality = _normalize(deployer_score, 0, 100)
    whale_quality = _normalize((whale_score * 0.6) + (whale_action_score * 0.4), 0, 100)
    holder_quality = _normalize(holder_growth_1h, 0, 100)
    spike_quality = _normalize(volume_spike_ratio, 0, 20)
    age_freshness = 1.0 - _normalize(age_min, 0, 240)
    threat_penalty = _normalize(threat_risk_score, 0, 100)
    wallet_participation_quality = _normalize(unique_buyer_count + first_buyer_count, 0, 40)
    smart_money_quality = _normalize((smart_wallet_buys * 0.7) + (smart_wallet_first10 * 1.2), 0, 12)
    flow_quality = (
        _normalize(buy_sell_ratio, 0.5, 4.5) * 0.45
        + _normalize(net_flow_sol, -2, 35) * 0.55
    )
    liquidity_retention_quality = 1.0 - _normalize(liquidity_drop_pct, 0, 70)
    exit_penalty = 0.4 if can_exit is False else 0.0
    hook_penalty = 0.25 if transfer_hook_enabled else 0.0

    composite_score = (
        liquidity_quality * 18
        + volume_quality * 16
        + momentum_quality * 12
        + ai_quality * 18
        + narrative_quality * 10
        + deployer_quality * 8
        + whale_quality * 10
        + holder_quality * 4
        + spike_quality * 4
        + wallet_participation_quality * 5
        + smart_money_quality * 7
        + flow_quality * 8
        + liquidity_retention_quality * 4
        + age_freshness * 8
        - threat_penalty * 15
        - exit_penalty * 12
        - hook_penalty * 10
    )

    confidence = _clamp(
        (liquidity_quality * 0.18)
        + (volume_quality * 0.16)
        + (ai_quality * 0.18)
        + (narrative_quality * 0.10)
        + (deployer_quality * 0.08)
        + (whale_quality * 0.12)
        + (holder_quality * 0.06)
        + (wallet_participation_quality * 0.06)
        + (smart_money_quality * 0.07)
        + (flow_quality * 0.08)
        + (liquidity_retention_quality * 0.05)
        + (age_freshness * 0.08)
        - (threat_penalty * 0.18)
        - exit_penalty
        - hook_penalty,
        0.0,
        1.0,
    )

    return {
        "price": _safe_float(info.get("price")),
        "liq": liq,
        "vol": vol,
        "mc": mc,
        "change": change,
        "age_min": age_min,
        "score": ai_score,
        "momentum": momentum,
        "green_lights": green_lights,
        "narrative_score": narrative_score,
        "deployer_score": deployer_score,
        "whale_score": whale_score,
        "whale_action_score": whale_action_score,
        "holder_growth_1h": holder_growth_1h,
        "volume_spike_ratio": volume_spike_ratio,
        "threat_risk_score": threat_risk_score,
        "cluster_confidence": cluster_confidence,
        "unique_buyer_count": unique_buyer_count,
        "unique_seller_count": unique_seller_count,
        "first_buyer_count": first_buyer_count,
        "smart_wallet_buys": smart_wallet_buys,
        "smart_wallet_first10": smart_wallet_first10,
        "total_buy_sol": round(total_buy_sol, 4),
        "total_sell_sol": round(total_sell_sol, 4),
        "net_flow_sol": round(net_flow_sol, 4),
        "buy_sell_ratio": round(buy_sell_ratio, 4),
        "liquidity_drop_pct": round(liquidity_drop_pct, 2),
        "transfer_hook_enabled": transfer_hook_enabled,
        "can_exit": can_exit,
        "liquidity_quality": round(liquidity_quality, 4),
        "volume_quality": round(volume_quality, 4),
        "momentum_quality": round(momentum_quality, 4),
        "ai_quality": round(ai_quality, 4),
        "narrative_quality": round(narrative_quality, 4),
        "deployer_quality": round(deployer_quality, 4),
        "whale_quality": round(whale_quality, 4),
        "holder_quality": round(holder_quality, 4),
        "spike_quality": round(spike_quality, 4),
        "wallet_participation_quality": round(wallet_participation_quality, 4),
        "smart_money_quality": round(smart_money_quality, 4),
        "flow_quality": round(flow_quality, 4),
        "liquidity_retention_quality": round(liquidity_retention_quality, 4),
        "age_freshness": round(age_freshness, 4),
        "threat_penalty": round(threat_penalty, 4),
        "composite_score": round(composite_score, 2),
        "confidence": round(confidence, 4),
    }


def build_flow_snapshot(info, intel=None):
    intel = intel or {}
    unique_buyers = _safe_int(intel.get("unique_buyer_count"))
    unique_sellers = _safe_int(intel.get("unique_seller_count"))
    total_buy_sol = _safe_float(intel.get("total_buy_sol"))
    total_sell_sol = _safe_float(intel.get("total_sell_sol"))
    net_flow_sol = _safe_float(intel.get("net_flow_sol"), total_buy_sol - total_sell_sol)
    participants = max(unique_buyers + unique_sellers, 1)
    buy_pressure_pct = (unique_buyers / participants) * 100.0 if participants else 0.0
    return {
        "price": _safe_float(info.get("price")),
        "liq": _safe_float(info.get("liq")),
        "mc": _safe_float(info.get("mc")),
        "vol": _safe_float(info.get("vol")),
        "age_min": _safe_float(info.get("age_min"), 9999.0),
        "holder_count": _safe_int(intel.get("holder_count")),
        "holder_growth_1h": _safe_float(intel.get("holder_growth_1h")),
        "unique_buyer_count": unique_buyers,
        "unique_seller_count": unique_sellers,
        "first_buyer_count": _safe_int(intel.get("first_buyer_count")),
        "smart_wallet_buys": _safe_int(intel.get("smart_wallet_buys")),
        "smart_wallet_first10": _safe_int(intel.get("smart_wallet_first10")),
        "total_buy_sol": round(total_buy_sol, 4),
        "total_sell_sol": round(total_sell_sol, 4),
        "net_flow_sol": round(net_flow_sol, 4),
        "buy_sell_ratio": round(_safe_float(intel.get("buy_sell_ratio")), 4),
        "buy_pressure_pct": round(buy_pressure_pct, 2),
        "liquidity_drop_pct": round(_safe_float(intel.get("liquidity_drop_pct")), 2),
        "threat_risk_score": _safe_int(intel.get("threat_risk_score")),
        "transfer_hook_enabled": bool(intel.get("transfer_hook_enabled")),
        "can_exit": intel.get("can_exit"),
        "narrative_tags": list(intel.get("narrative_tags") or []),
        "infra_labels": list(intel.get("infra_labels") or []),
    }


def evaluate_shadow_strategy(strategy_name, settings, snapshot):
    pass_reasons = []
    blocker_reasons = []

    if snapshot["price"] <= 0:
        blocker_reasons.append("missing_price")
    _liq_val = _safe_float(snapshot.get("liq"))
    _liq_min = _safe_float(settings.get("min_liq", 0))
    if _liq_min > 0 and _liq_val > 0 and _liq_val < _liq_min:
        blocker_reasons.append("liquidity_below_threshold")
    _mc_val = _safe_float(snapshot.get("mc"))
    _mc_min = _safe_float(settings.get("min_mc", 0))
    if _mc_min > 0 and _mc_val > 0 and _mc_val < _mc_min:
        blocker_reasons.append("market_cap_below_floor")
    if _safe_float(settings.get("max_mc", 0)) > 0 and _mc_val > 0 and _mc_val > _safe_float(settings.get("max_mc", 0)):
        blocker_reasons.append("market_cap_above_ceiling")
    _vol_val = _safe_float(snapshot.get("vol"))
    _vol_min = _safe_float(settings.get("min_vol", 0))
    if _vol_min > 0 and _vol_val > 0 and _vol_val < _vol_min:
        blocker_reasons.append("volume_below_threshold")
    _score_val = _safe_float(snapshot.get("score"))
    _score_min = _safe_float(settings.get("min_score", 0))
    if _score_min > 0 and _score_val > 0 and _score_val < _score_min:
        blocker_reasons.append("ai_score_below_threshold")
    _age_val = _safe_float(snapshot.get("age_min"))
    _age_max = _safe_float(settings.get("max_age_min", 9999))
    if _age_val > 0 and _age_val < 9999 and _age_val > _age_max:
        blocker_reasons.append("token_too_old")
    # Green lights, narrative, holder growth, and volume spike are enrichment
    # fields that may not exist in historical snapshots.  Treat them as blockers
    # ONLY when data is actually present (non-zero) but below the threshold —
    # if data is missing (0), skip the blocker so backtests don't produce zero trades.
    _gl_val = _safe_int(snapshot.get("green_lights"))
    _gl_min = _safe_int(settings.get("min_green_lights", 0))
    if _gl_min > 0 and _gl_val > 0 and _gl_val < _gl_min:
        blocker_reasons.append("green_lights_below_threshold")
    _narr_val = _safe_int(snapshot.get("narrative_score"))
    _narr_min = _safe_int(settings.get("min_narrative_score", 0))
    if _narr_min > 0 and _narr_val > 0 and _narr_val < _narr_min:
        blocker_reasons.append("narrative_below_threshold")
    _holder_growth_val = _safe_float(snapshot.get("holder_growth_1h"))
    _holder_growth_min = _safe_float(settings.get("min_holder_growth_pct", 0))
    if _holder_growth_min > 0 and _holder_growth_val > 0 and _holder_growth_val < _holder_growth_min:
        blocker_reasons.append("holder_growth_below_threshold")
    _vol_spike_val = _safe_float(snapshot.get("volume_spike_ratio"))
    _vol_spike_min = _safe_float(settings.get("min_volume_spike_mult", 0))
    if _vol_spike_min > 0 and _vol_spike_val > 0 and _vol_spike_val < _vol_spike_min:
        blocker_reasons.append("volume_spike_below_threshold")
    if settings.get("anti_rug") and snapshot.get("can_exit") is False:
        blocker_reasons.append("cannot_exit")
    if settings.get("anti_rug") and snapshot.get("transfer_hook_enabled"):
        blocker_reasons.append("transfer_hook_enabled")
    if _safe_int(snapshot.get("threat_risk_score")) >= 70:
        blocker_reasons.append("threat_risk_too_high")

    if _liq_val >= _liq_min or _liq_val == 0:
        pass_reasons.append("liquidity_ok")
    if _vol_val >= _vol_min or _vol_val == 0:
        pass_reasons.append("volume_ok")
    if _score_val >= _score_min or _score_val == 0:
        pass_reasons.append("ai_score_ok")
    if _gl_val >= _gl_min or _gl_val == 0:
        pass_reasons.append("green_lights_ok")
    if _narr_val >= _narr_min or _narr_val == 0:
        pass_reasons.append("narrative_ok")
    if _holder_growth_val >= _holder_growth_min or _holder_growth_val == 0:
        pass_reasons.append("holder_growth_ok")
    if _vol_spike_val >= _vol_spike_min or _vol_spike_val == 0:
        pass_reasons.append("volume_spike_ok")
    if _safe_int(snapshot.get("threat_risk_score")) < 45:
        pass_reasons.append("threat_risk_ok")

    score = snapshot["composite_score"]
    score += _normalize(snapshot["liq"], 0, max(_safe_float(settings.get("min_liq", 0)) * 2, 1.0)) * 6
    score += _normalize(snapshot["vol"], 0, max(_safe_float(settings.get("min_vol", 0)) * 2, 1.0)) * 5
    score += _normalize(snapshot["green_lights"], 0, 3) * 8
    score += _normalize(snapshot["narrative_score"], 0, max(_safe_float(settings.get("min_narrative_score", 0)) + 30, 30)) * 6
    score -= len(blocker_reasons) * 5
    score = max(0.0, min(100.0, score))

    confidence = _clamp(snapshot["confidence"] + (len(pass_reasons) * 0.04) - (len(blocker_reasons) * 0.06))
    min_composite = _safe_float(settings.get("min_composite_score", 40))
    min_conf = _safe_float(settings.get("min_confidence", 0.35))
    passed = not blocker_reasons and score >= min_composite and confidence >= min_conf

    metrics = {
        "entry_price": snapshot["price"],
        "take_profit_mult": _safe_float(settings.get("tp2_mult", settings.get("tp1_mult", 2.0) or 2.0)),
        "stop_loss_ratio": _safe_float(settings.get("stop_loss", 0.7)),
        "time_stop_min": _safe_int(settings.get("time_stop_min", 30)),
        "max_buy_sol": _safe_float(settings.get("max_buy_sol", 0)),
    }

    return ShadowDecision(
        strategy_name=strategy_name,
        passed=passed,
        score=score,
        confidence=confidence,
        pass_reasons=pass_reasons,
        blocker_reasons=blocker_reasons,
        metrics=metrics,
    )


def shadow_position_update(position, current_price, settings, age_min):
    entry_price = max(_safe_float(position.get("entry_price"), 0.0), 1e-12)
    current_price = max(_safe_float(current_price, 0.0), 0.0)
    peak_price = max(_safe_float(position.get("peak_price"), entry_price), current_price)
    trough_price = min(_safe_float(position.get("trough_price"), entry_price), current_price)
    max_upside_pct = ((peak_price / entry_price) - 1.0) * 100.0 if entry_price else 0.0
    max_drawdown_pct = ((trough_price / entry_price) - 1.0) * 100.0 if entry_price else 0.0

    tp1_mult = max(_safe_float(settings.get("tp1_mult"), 2.0), 1.01)
    tp2_mult = max(_safe_float(position.get("take_profit_mult"), settings.get("tp2_mult", 2.0)), 1.01)
    stop_ratio = _safe_float(position.get("stop_loss_ratio"), settings.get("stop_loss", 0.7))
    time_stop_min = _safe_int(position.get("time_stop_min"), settings.get("time_stop_min", 30))
    peak_plateau_mode = bool(settings.get("peak_plateau_mode"))
    tp1_sell_pct = _safe_float(settings.get("tp1_sell_pct"), 0.50)

    tp1_hit = bool(position.get("tp1_hit"))
    tp1_pnl_pct = _safe_float(position.get("tp1_pnl_pct"), 0.0)

    peak_ratio = peak_price / entry_price if entry_price else 1.0
    ratio = current_price / entry_price if entry_price else 1.0

    # Progressive trailing stop calculation
    base_trail = _safe_float(settings.get("trail_pct"), 0.20)
    if peak_plateau_mode:
        if peak_ratio >= 50:
            effective_trail = min(base_trail, 0.10)
        elif peak_ratio >= 20:
            effective_trail = min(base_trail, 0.12)
        elif peak_ratio >= 10:
            effective_trail = min(base_trail, 0.15)
        elif peak_ratio >= 5:
            effective_trail = min(base_trail, 0.20)
        elif peak_ratio >= 3:
            effective_trail = min(base_trail, 0.25)
        else:
            effective_trail = base_trail
    else:
        effective_trail = base_trail
    trail_line = peak_price * (1.0 - effective_trail)

    status = "open"
    exit_reason = ""
    new_tp1_hit = tp1_hit
    new_tp1_pnl_pct = tp1_pnl_pct

    if stop_ratio > 0 and current_price <= entry_price * stop_ratio:
        status = "closed"
        exit_reason = "stop_loss"
    elif peak_plateau_mode:
        # Peak Plateau Mode: TP1 skim, then trail to peak
        if not tp1_hit and ratio >= tp1_mult:
            # TP1 hit — record partial profit, keep position open
            new_tp1_hit = True
            new_tp1_pnl_pct = (ratio - 1.0) * 100.0 * tp1_sell_pct
            exit_reason = ""  # not closing yet
        elif (peak_ratio >= 1.5) and current_price < trail_line:
            status = "closed"
            exit_reason = f"peak_plateau_{effective_trail*100:.0f}pct_trail"
        elif age_min >= time_stop_min and ratio < 1.05:
            # Time stop: close if flat or losing after time limit
            status = "closed"
            exit_reason = "time_stop"
        elif age_min >= time_stop_min and ratio >= 1.05 and current_price < trail_line:
            # In profit past time limit but dropping from peak — trail out
            status = "closed"
            exit_reason = "time_trail"
    else:
        # Standard mode with TP1/TP2
        if not tp1_hit and ratio >= tp1_mult:
            # TP1 hit — record partial profit, keep position open for TP2
            new_tp1_hit = True
            new_tp1_pnl_pct = (ratio - 1.0) * 100.0 * tp1_sell_pct
            exit_reason = ""
        elif tp1_hit and ratio >= tp2_mult:
            status = "closed"
            exit_reason = "take_profit_tp2"
        elif tp1_hit and current_price < trail_line:
            # After TP1, use trailing stop instead of waiting forever for TP2
            status = "closed"
            exit_reason = "trail_after_tp1"
        elif not tp1_hit and age_min >= time_stop_min and ratio < 1.05:
            # Time stop: close if flat or losing after time limit
            status = "closed"
            exit_reason = "time_stop"
        elif not tp1_hit and age_min >= time_stop_min and ratio >= 1.05 and current_price < trail_line:
            # In profit past time limit but dropping from peak — trail out
            status = "closed"
            exit_reason = "time_trail"

    # Calculate realized P&L on close
    # Combine: TP1 partial profit (on the sold fraction) + remaining position P&L (on the unsold fraction)
    realized_pnl_pct = None
    if entry_price and status == "closed":
        remaining_pct = (ratio - 1.0) * 100.0
        if new_tp1_hit or tp1_hit:
            # Weighted: tp1_sell_pct was sold at TP1, remainder sold now
            remaining_fraction = 1.0 - tp1_sell_pct
            realized_pnl_pct = new_tp1_pnl_pct + (remaining_pct * remaining_fraction)
        else:
            realized_pnl_pct = remaining_pct

    return {
        "status": status,
        "exit_reason": exit_reason,
        "current_price": current_price,
        "peak_price": peak_price,
        "trough_price": trough_price,
        "max_upside_pct": round(max_upside_pct, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "realized_pnl_pct": round(realized_pnl_pct, 2) if realized_pnl_pct is not None else None,
        "tp1_hit": new_tp1_hit,
        "tp1_pnl_pct": round(new_tp1_pnl_pct, 2),
    }


def _json_list(value):
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def summarize_flow_regime(rows):
    sample = [row for row in (rows or []) if isinstance(row, dict)]
    count = len(sample)
    if not count:
        return {
            "sample_size": 0,
            "regime": "uninitialized",
            "avg_buy_sell_ratio": 0.0,
            "avg_net_flow_sol": 0.0,
            "avg_smart_wallet_buys": 0.0,
            "avg_unique_buyers": 0.0,
            "high_risk_share_pct": 0.0,
            "exit_blocked_share_pct": 0.0,
            "liquidity_drain_share_pct": 0.0,
        }

    avg_buy_sell_ratio = sum(_safe_float(row.get("buy_sell_ratio")) for row in sample) / count
    avg_net_flow_sol = sum(_safe_float(row.get("net_flow_sol")) for row in sample) / count
    avg_smart_wallet_buys = sum(_safe_float(row.get("smart_wallet_buys")) for row in sample) / count
    avg_unique_buyers = sum(_safe_float(row.get("unique_buyer_count")) for row in sample) / count
    high_risk_share = sum(1 for row in sample if _safe_int(row.get("threat_risk_score")) >= 70) / count
    exit_blocked_share = sum(1 for row in sample if row.get("can_exit") is False) / count
    liquidity_drain_share = sum(1 for row in sample if _safe_float(row.get("liquidity_drop_pct")) >= 35) / count

    regime = "neutral"
    if high_risk_share >= 0.45 or exit_blocked_share >= 0.18 or liquidity_drain_share >= 0.35:
        regime = "defensive"
    elif avg_net_flow_sol > 2.5 and avg_buy_sell_ratio > 1.35 and high_risk_share < 0.3:
        regime = "accumulation"
    elif avg_net_flow_sol < -1.5 or avg_buy_sell_ratio < 0.9:
        regime = "distribution"

    return {
        "sample_size": count,
        "regime": regime,
        "avg_buy_sell_ratio": round(avg_buy_sell_ratio, 2),
        "avg_net_flow_sol": round(avg_net_flow_sol, 2),
        "avg_smart_wallet_buys": round(avg_smart_wallet_buys, 2),
        "avg_unique_buyers": round(avg_unique_buyers, 2),
        "high_risk_share_pct": round(high_risk_share * 100.0, 1),
        "exit_blocked_share_pct": round(exit_blocked_share * 100.0, 1),
        "liquidity_drain_share_pct": round(liquidity_drain_share * 100.0, 1),
    }


def summarize_opportunity_matrix(rows, winner_threshold_pct=120.0, loser_threshold_pct=-35.0, sample_limit=5):
    categories = {
        "missed_winners": [],
        "avoided_losers": [],
        "captured_winners": [],
        "false_positives": [],
    }
    blocker_counts = {}
    by_strategy = {}

    for row in rows or []:
        if not isinstance(row, dict):
            continue
        strategy_name = (row.get("strategy_name") or "unknown").lower()
        passed = bool(row.get("passed"))
        basis_price = _safe_float(row.get("price")) or _safe_float(row.get("first_price"))
        peak_price = _safe_float(row.get("peak_price"))
        trough_price = _safe_float(row.get("trough_price"))
        last_price = _safe_float(row.get("last_price"))
        if basis_price <= 0 or peak_price <= 0:
            continue

        peak_return_pct = ((peak_price / basis_price) - 1.0) * 100.0
        trough_return_pct = ((trough_price / basis_price) - 1.0) * 100.0 if trough_price > 0 else 0.0
        last_return_pct = ((last_price / basis_price) - 1.0) * 100.0 if last_price > 0 else 0.0
        item = {
            "strategy_name": strategy_name,
            "mint": row.get("mint"),
            "name": row.get("name") or row.get("mint") or "Unknown",
            "passed": passed,
            "peak_return_pct": round(peak_return_pct, 2),
            "trough_return_pct": round(trough_return_pct, 2),
            "last_return_pct": round(last_return_pct, 2),
            "blocker_reasons": _json_list(row.get("blocker_reasons")),
        }
        by_strategy.setdefault(strategy_name, {
            "missed_winners": 0,
            "avoided_losers": 0,
            "captured_winners": 0,
            "false_positives": 0,
        })

        if not passed and peak_return_pct >= winner_threshold_pct:
            categories["missed_winners"].append(item)
            by_strategy[strategy_name]["missed_winners"] += 1
            for reason in item["blocker_reasons"]:
                blocker_counts[reason] = blocker_counts.get(reason, 0) + 1
        elif not passed and trough_return_pct <= loser_threshold_pct and peak_return_pct < max(40.0, winner_threshold_pct * 0.5):
            categories["avoided_losers"].append(item)
            by_strategy[strategy_name]["avoided_losers"] += 1
        elif passed and peak_return_pct >= winner_threshold_pct:
            categories["captured_winners"].append(item)
            by_strategy[strategy_name]["captured_winners"] += 1
        elif passed and last_return_pct <= loser_threshold_pct and peak_return_pct < max(35.0, winner_threshold_pct * 0.45):
            categories["false_positives"].append(item)
            by_strategy[strategy_name]["false_positives"] += 1

    def _sort_key(entry):
        return (entry.get("peak_return_pct") or 0.0, entry.get("last_return_pct") or 0.0)

    return {
        "winner_threshold_pct": winner_threshold_pct,
        "loser_threshold_pct": loser_threshold_pct,
        "totals": {
            key: len(value)
            for key, value in categories.items()
        },
        "top_blockers": [
            {"reason": reason, "count": count}
            for reason, count in sorted(blocker_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
        ],
        "by_strategy": [
            {"strategy_name": strategy_name, **stats}
            for strategy_name, stats in sorted(by_strategy.items(), key=lambda item: item[0])
        ],
        "samples": {
            key: sorted(value, key=_sort_key, reverse=True)[:sample_limit]
            for key, value in categories.items()
        },
    }
