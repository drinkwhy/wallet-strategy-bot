import json
import math
from dataclasses import dataclass
from risk_engine import RISK_THRESHOLDS


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
    # Check minimum token age from risk thresholds — enforce unified approval gate
    # EXCEPTION: bypass age check for runners (huge growth >100%) to catch pump early
    _age_min = RISK_THRESHOLDS.get("min_token_age_sec", 30)
    _is_runner = _change_val > 100  # Caught 100%+ already — likely a runner
    if _age_val > 0 and _age_val < _age_min and not _is_runner:
        blocker_reasons.append("token_too_new")
    # Price change cap — reject already-pumped tokens
    _change_val = _safe_float(snapshot.get("change"))
    _max_hot = _safe_float(settings.get("max_hot_change", 9999))
    if _change_val > 0 and _max_hot < 9999 and _change_val > _max_hot:
        blocker_reasons.append("price_change_too_hot")
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
    _buy_sell_ratio_val = _safe_float(snapshot.get("buy_sell_ratio"))
    _buy_sell_ratio_min = _safe_float(settings.get("min_buy_sell_ratio", 0))
    if _buy_sell_ratio_min > 0 and _buy_sell_ratio_val > 0 and _buy_sell_ratio_val < _buy_sell_ratio_min:
        blocker_reasons.append("buy_sell_ratio_below_threshold")
    _smart_wallet_buys_val = _safe_int(snapshot.get("smart_wallet_buys"))
    _smart_wallet_buys_min = _safe_int(settings.get("min_smart_wallet_buys", 0))
    if _smart_wallet_buys_min > 0 and _smart_wallet_buys_val > 0 and _smart_wallet_buys_val < _smart_wallet_buys_min:
        blocker_reasons.append("smart_wallet_buys_below_threshold")
    _net_flow_val = _safe_float(snapshot.get("net_flow_sol"))
    _net_flow_min = _safe_float(settings.get("min_net_flow_sol", 0))
    if _net_flow_min > 0 and _net_flow_val > 0 and _net_flow_val < _net_flow_min:
        blocker_reasons.append("net_flow_below_threshold")
    _unique_buyers_val = _safe_int(snapshot.get("unique_buyer_count"))
    _unique_buyers_min = _safe_int(settings.get("min_unique_buyers", 0))
    if _unique_buyers_min > 0 and _unique_buyers_val > 0 and _unique_buyers_val < _unique_buyers_min:
        blocker_reasons.append("unique_buyers_below_threshold")
    if settings.get("anti_rug") and snapshot.get("can_exit") is False:
        blocker_reasons.append("cannot_exit")
    if settings.get("anti_rug") and snapshot.get("transfer_hook_enabled"):
        blocker_reasons.append("transfer_hook_enabled")
    _threat_max = _safe_int(settings.get("max_threat_score", 70))
    if _safe_int(snapshot.get("threat_risk_score")) >= _threat_max:
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
    if _buy_sell_ratio_val >= _buy_sell_ratio_min or _buy_sell_ratio_val == 0:
        pass_reasons.append("buy_sell_ratio_ok")
    if _smart_wallet_buys_val >= _smart_wallet_buys_min or _smart_wallet_buys_val == 0:
        pass_reasons.append("smart_wallet_buys_ok")
    if _net_flow_val >= _net_flow_min or _net_flow_val == 0:
        pass_reasons.append("net_flow_ok")
    if _unique_buyers_val >= _unique_buyers_min or _unique_buyers_val == 0:
        pass_reasons.append("unique_buyers_ok")
    if _safe_int(snapshot.get("threat_risk_score")) < 45:
        pass_reasons.append("threat_risk_ok")

    score = snapshot["composite_score"]
    score += _normalize(snapshot["liq"], 0, max(_safe_float(settings.get("min_liq", 0)) * 2, 1.0)) * 6
    score += _normalize(snapshot["vol"], 0, max(_safe_float(settings.get("min_vol", 0)) * 2, 1.0)) * 5
    score += _normalize(snapshot["green_lights"], 0, 3) * 8
    score += _normalize(snapshot["narrative_score"], 0, max(_safe_float(settings.get("min_narrative_score", 0)) + 30, 30)) * 6
    score += _normalize(snapshot.get("buy_sell_ratio", 0), 0, max(_safe_float(settings.get("min_buy_sell_ratio", 0)) * 2, 1.0)) * 4
    score += _normalize(snapshot.get("smart_wallet_buys", 0), 0, max(_safe_float(settings.get("min_smart_wallet_buys", 0)) + 6, 6.0)) * 4
    score += _normalize(snapshot.get("net_flow_sol", 0), 0, max(_safe_float(settings.get("min_net_flow_sol", 0)) + 10, 10.0)) * 3
    score += _normalize(snapshot.get("unique_buyer_count", 0), 0, max(_safe_float(settings.get("min_unique_buyers", 0)) + 30, 30.0)) * 3
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

    # NEW: Zero-movement detection parameters
    movement_band_pct = _safe_float(settings.get("movement_band_pct", 1.0), 1.0)
    time_threshold_sec = _safe_int(settings.get("time_threshold_sec", 300), 300)
    time_in_band = _safe_int(position.get("time_in_band_sec", 0), 0)

    tp1_hit = bool(position.get("tp1_hit"))
    tp1_pnl_pct = _safe_float(position.get("tp1_pnl_pct"), 0.0)

    peak_ratio = peak_price / entry_price if entry_price else 1.0
    ratio = current_price / entry_price if entry_price else 1.0

    # Progressive trailing stop calculation
    base_trail = _safe_float(settings.get("trail_pct"), 0.20)

    # Moonshot mode: if coin surges past trigger (e.g. 50%+), widen trail to let it run
    moonshot_trigger = _safe_float(settings.get("moonshot_trigger"), 0)
    moonshot_trail = _safe_float(settings.get("moonshot_trail_pct"), 0)
    moonshot_active = moonshot_trigger > 0 and ratio >= moonshot_trigger

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
        elif moonshot_active and moonshot_trail > 0:
            # Coin is surging 50%+ but hasn't hit 3x yet — give it room to moon
            effective_trail = moonshot_trail
        else:
            effective_trail = base_trail
    else:
        if moonshot_active and moonshot_trail > 0:
            effective_trail = moonshot_trail
        else:
            effective_trail = base_trail
    trail_line = peak_price * (1.0 - effective_trail)

    status = "open"
    exit_reason = ""
    new_tp1_hit = tp1_hit
    new_tp1_pnl_pct = tp1_pnl_pct
    new_time_in_band = time_in_band  # NEW: track band time

    # NEW: Zero-movement detection (check FIRST before other exits)
    band_lower = entry_price * (1.0 - movement_band_pct / 100.0)
    band_upper = entry_price * (1.0 + movement_band_pct / 100.0)

    if band_lower <= current_price <= band_upper:
        # Price is in the band
        new_time_in_band = time_in_band + 1  # increment by 1 second (called every second)
        if new_time_in_band >= time_threshold_sec:
            # Stuck for long enough — close it
            status = "closed"
            exit_reason = "zero_movement_stuck"
    else:
        # Price broke the band — reset counter
        new_time_in_band = 0

    # Existing exit conditions (only check if not already closed by zero-movement)
    if status == "open":
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
                # TP1 hit — record partial profit
                new_tp1_hit = True
                new_tp1_pnl_pct = (ratio - 1.0) * 100.0 * tp1_sell_pct
                # If price is already at or above TP2 in the same tick, close immediately
                if ratio >= tp2_mult:
                    status = "closed"
                    exit_reason = "take_profit"
                else:
                    exit_reason = ""  # not closing yet — waiting for TP2
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
        "time_in_band_sec": new_time_in_band,  # NEW
    }


def estimate_exit_friction(realized_pnl_pct, exit_price, entry_price, entry_sol,
                           liq_usd=0, vol_usd=0, mc_usd=0, exit_reason="",
                           peak_ratio=1.0):
    """
    Estimate realistic P&L by applying friction that shadow trading ignores:
      1. Slippage based on liquidity tier
      2. Price impact based on position size vs pool depth
      3. Transaction failure probability (reduces expected value)
      4. Execution delay penalty (price moves during 3-8s swap time)
      5. Priority fee / Jito tip cost
      6. Jupiter swap fee (0.5% platform fee)

    Returns dict with friction breakdown and adjusted P&L.
    """
    if realized_pnl_pct is None:
        return {"friction_pnl_pct": None, "slippage_pct": 0, "price_impact_pct": 0,
                "tx_fail_haircut_pct": 0, "delay_penalty_pct": 0, "fee_pct": 0,
                "total_friction_pct": 0, "exit_liq_usd": liq_usd, "exit_vol_usd": vol_usd}

    liq = max(_safe_float(liq_usd, 0), 0)
    vol = max(_safe_float(vol_usd, 0), 0)
    entry_sol_f = max(_safe_float(entry_sol, 0.05), 0.001)
    exit_price_f = max(_safe_float(exit_price, 0), 1e-15)
    entry_price_f = max(_safe_float(entry_price, 0), 1e-15)

    # ── 1. Slippage based on liquidity tier ──────────────────────────
    # Mirrors dynamic_slippage_bps() from real bot but applied as P&L reduction
    if liq > 100_000:
        slippage_pct = 3.0       # 3%  — healthy pool
    elif liq > 50_000:
        slippage_pct = 5.0       # 5%
    elif liq > 20_000:
        slippage_pct = 8.0       # 8%
    elif liq > 10_000:
        slippage_pct = 12.0      # 12%
    elif liq > 5_000:
        slippage_pct = 18.0      # 18% — thin pool
    elif liq > 1_000:
        slippage_pct = 28.0      # 28% — barely tradeable
    elif liq > 0:
        slippage_pct = 45.0      # 45% — effectively illiquid
    else:
        # No liquidity data — use conservative estimate based on peak ratio
        # Higher multiples usually mean micro-cap with thin pools
        if peak_ratio >= 20:
            slippage_pct = 30.0
        elif peak_ratio >= 10:
            slippage_pct = 20.0
        elif peak_ratio >= 5:
            slippage_pct = 15.0
        else:
            slippage_pct = 10.0

    # ── 2. Price impact: your sell size relative to pool depth ────────
    # Estimate position value in USD at exit
    # Rough SOL price assumption: $150 (reasonable 2025-2026 range)
    sol_price_usd = 150.0
    ratio = exit_price_f / entry_price_f
    position_value_usd = entry_sol_f * sol_price_usd * ratio
    if liq > 0:
        # AMM constant-product: selling X into pool of L causes ~(X/L * 100)% impact
        impact_raw = (position_value_usd / liq) * 100.0
        price_impact_pct = min(impact_raw * 1.2, 50.0)  # cap at 50%, 1.2x multiplier for realism
    else:
        # No liquidity data — estimate from trade size
        if position_value_usd > 500:
            price_impact_pct = 8.0
        elif position_value_usd > 100:
            price_impact_pct = 4.0
        else:
            price_impact_pct = 2.0

    # ── 3. Transaction failure probability ───────────────────────────
    # Real sells fail 10-30% of the time on meme coins.
    # Model as expected-value haircut: if 20% chance of fail,
    # and failure means position stays open (likely losing more),
    # reduce expected P&L by (fail_rate * avg_loss_on_fail)
    if liq > 50_000 and vol > 100_000:
        fail_rate = 0.08   # 8% fail — healthy token
    elif liq > 10_000:
        fail_rate = 0.15   # 15% fail — moderate
    elif liq > 2_000:
        fail_rate = 0.25   # 25% fail — thin
    else:
        fail_rate = 0.35   # 35% fail — very thin, routes often missing

    # When a sell fails, you typically lose 20-50% more before retry succeeds
    avg_loss_on_fail_pct = 25.0
    tx_fail_haircut_pct = fail_rate * avg_loss_on_fail_pct

    # ── 4. Execution delay penalty ───────────────────────────────────
    # Real execution takes 3-8 seconds. Meme coins move 2-10% per second
    # during volatile exits (which is when trailing stops trigger)
    if exit_reason in ("stop_loss", "time_stop"):
        delay_penalty_pct = 3.0   # dumps are fast, extra slippage on panic exit
    elif "trail" in str(exit_reason):
        delay_penalty_pct = 2.0   # trailing exits happen during retraces
    else:
        delay_penalty_pct = 1.5   # normal TP exits are calmer

    # Higher multiples = more volatile moments = bigger delay cost
    if peak_ratio >= 20:
        delay_penalty_pct *= 2.0
    elif peak_ratio >= 10:
        delay_penalty_pct *= 1.5
    elif peak_ratio >= 5:
        delay_penalty_pct *= 1.2

    # ── 5. Fixed fees ────────────────────────────────────────────────
    # Jupiter platform fee: ~0.5%
    # Priority fee + Jito tip: ~0.001-0.01 SOL (negligible on % basis for most trades)
    # Solana base fee: negligible
    fee_pct = 0.6  # Jupiter fee + priority fee overhead

    # ── Total friction ───────────────────────────────────────────────
    total_friction_pct = slippage_pct + price_impact_pct + tx_fail_haircut_pct + delay_penalty_pct + fee_pct

    # Apply friction to the realized P&L
    # Friction reduces the exit price, not added to loss — so:
    # If shadow says +100% (2x), and friction is 20%, realistic exit is at 1.8x = +80%
    # If shadow says -20% (0.8x), friction makes it worse: -20% - friction on remaining value
    if realized_pnl_pct >= 0:
        # Winning trade: friction reduces the gain
        exit_multiplier = 1.0 + (realized_pnl_pct / 100.0)
        friction_multiplier = 1.0 - (total_friction_pct / 100.0)
        realistic_multiplier = exit_multiplier * max(friction_multiplier, 0.01)
        friction_pnl_pct = (realistic_multiplier - 1.0) * 100.0
    else:
        # Losing trade: friction makes the loss worse
        friction_pnl_pct = realized_pnl_pct - (total_friction_pct * 0.5)
        # (Half friction on losses — you're selling less value so impact is smaller)

    return {
        "friction_pnl_pct": round(friction_pnl_pct, 2),
        "slippage_pct": round(slippage_pct, 2),
        "price_impact_pct": round(price_impact_pct, 2),
        "tx_fail_haircut_pct": round(tx_fail_haircut_pct, 2),
        "delay_penalty_pct": round(delay_penalty_pct, 2),
        "fee_pct": round(fee_pct, 2),
        "total_friction_pct": round(total_friction_pct, 2),
        "exit_liq_usd": round(liq, 2),
        "exit_vol_usd": round(vol, 2),
    }


def estimate_entry_friction(entry_price, liq_usd=0, entry_sol=0.05, priority_fee=30000):
    """Estimate the effective (inflated) entry price after buy-side friction:

    1. Entry slippage — buying into the pool raises the price you pay
    2. MEV sandwich front-run — attacker buys before you, sells after
       Probability and magnitude modelled from observed Solana mainnet data
    3. Entry price impact — your buy moves the pool price against you
    4. Jupiter platform fee (0.5%) on the buy side

    Returns:
        effective_entry_price  — use this instead of the clean market price
        mev_probability        — estimated chance a sandwich happened (0-1)
        entry_friction_pct     — total cost as % of notional
        breakdown              — dict with individual components
    """
    liq = max(_safe_float(liq_usd, 0), 0)
    sol = max(_safe_float(entry_sol, 0.05), 0.001)
    price = max(_safe_float(entry_price, 0), 1e-15)
    prio = max(_safe_float(priority_fee, 0), 0)

    # ── 1. Entry slippage (buy side) ─────────────────────────────────
    # Mirrors exit slippage tiers but slightly lower — AMM impact is
    # symmetric but MEV is directionally worse on exit (more sellers watching)
    if liq > 100_000:
        entry_slippage_pct = 2.0
    elif liq > 50_000:
        entry_slippage_pct = 4.0
    elif liq > 20_000:
        entry_slippage_pct = 6.0
    elif liq > 10_000:
        entry_slippage_pct = 9.0
    elif liq > 5_000:
        entry_slippage_pct = 14.0
    elif liq > 1_000:
        entry_slippage_pct = 22.0
    elif liq > 0:
        entry_slippage_pct = 35.0
    else:
        entry_slippage_pct = 8.0   # unknown pool — conservative estimate

    # ── 2. MEV sandwich attack ────────────────────────────────────────
    # Front-runner probability is driven by:
    #   - Pool thinness (thin pools are easier/more profitable to sandwich)
    #   - Priority fee (high fees = more visible in mempool = more MEV)
    #   - Trade size vs pool (larger relative size = bigger sandwich profit)
    sol_price_usd = 150.0
    trade_usd = sol * sol_price_usd
    pool_ratio = (trade_usd / liq) if liq > 0 else 0.5

    # Base sandwich probability
    if liq > 100_000:
        mev_prob = 0.05   # 5% — deep pool, less profitable to sandwich
    elif liq > 20_000:
        mev_prob = 0.15
    elif liq > 5_000:
        mev_prob = 0.30
    elif liq > 1_000:
        mev_prob = 0.45
    else:
        mev_prob = 0.60   # 60% — micro pool, nearly always sandwiched

    # High priority fee = you broadcast urgency = attracts MEV bots
    if prio > 200_000:     # >0.0002 SOL tip
        mev_prob = min(mev_prob + 0.10, 0.80)
    elif prio > 50_000:
        mev_prob = min(mev_prob + 0.05, 0.75)

    # Large relative trade size = bigger sandwich profit = more competition
    if pool_ratio > 0.10:
        mev_prob = min(mev_prob + 0.10, 0.85)
    elif pool_ratio > 0.02:
        mev_prob = min(mev_prob + 0.05, 0.80)

    # MEV cost: attacker front-runs by ~1-4%, then sells after your fill
    # The cost to you is the front-runner's profit = ~1-3% on your fill price
    mev_cost_if_hit_pct = min(2.0 + pool_ratio * 15.0, 8.0)
    mev_expected_cost_pct = mev_prob * mev_cost_if_hit_pct

    # ── 3. Entry price impact ─────────────────────────────────────────
    if liq > 0:
        impact_raw = (trade_usd / liq) * 100.0
        entry_impact_pct = min(impact_raw, 25.0)
    else:
        entry_impact_pct = 2.0

    # ── 4. Jupiter buy fee ────────────────────────────────────────────
    entry_fee_pct = 0.5

    # ── Total entry friction ─────────────────────────────────────────
    total_entry_friction_pct = (
        entry_slippage_pct + mev_expected_cost_pct + entry_impact_pct + entry_fee_pct
    )

    # Effective entry price: you paid this much more than the clean price
    effective_entry_price = price * (1.0 + total_entry_friction_pct / 100.0)

    return {
        "effective_entry_price": effective_entry_price,
        "mev_probability": round(mev_prob, 3),
        "entry_friction_pct": round(total_entry_friction_pct, 2),
        "entry_slippage_pct": round(entry_slippage_pct, 2),
        "mev_cost_pct": round(mev_expected_cost_pct, 2),
        "entry_impact_pct": round(entry_impact_pct, 2),
        "entry_fee_pct": entry_fee_pct,
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
