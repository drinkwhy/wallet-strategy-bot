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
        "age_freshness": round(age_freshness, 4),
        "threat_penalty": round(threat_penalty, 4),
        "composite_score": round(composite_score, 2),
        "confidence": round(confidence, 4),
    }


def evaluate_shadow_strategy(strategy_name, settings, snapshot):
    pass_reasons = []
    blocker_reasons = []

    if snapshot["price"] <= 0:
        blocker_reasons.append("missing_price")
    if settings.get("min_liq", 0) > 0 and snapshot["liq"] < _safe_float(settings.get("min_liq")):
        blocker_reasons.append("liquidity_below_threshold")
    if snapshot["mc"] < _safe_float(settings.get("min_mc", 0)):
        blocker_reasons.append("market_cap_below_floor")
    if _safe_float(settings.get("max_mc", 0)) > 0 and snapshot["mc"] > _safe_float(settings.get("max_mc", 0)):
        blocker_reasons.append("market_cap_above_ceiling")
    if snapshot["vol"] < _safe_float(settings.get("min_vol", 0)):
        blocker_reasons.append("volume_below_threshold")
    if snapshot["score"] < _safe_float(settings.get("min_score", 0)):
        blocker_reasons.append("ai_score_below_threshold")
    if snapshot["age_min"] > _safe_float(settings.get("max_age_min", 9999)):
        blocker_reasons.append("token_too_old")
    if snapshot["green_lights"] < _safe_int(settings.get("min_green_lights", 0)):
        blocker_reasons.append("green_lights_below_threshold")
    if snapshot["narrative_score"] < _safe_int(settings.get("min_narrative_score", 0)):
        blocker_reasons.append("narrative_below_threshold")
    if _safe_float(settings.get("min_holder_growth_pct", 0)) > 0 and snapshot["holder_growth_1h"] < _safe_float(settings.get("min_holder_growth_pct", 0)):
        blocker_reasons.append("holder_growth_below_threshold")
    if _safe_float(settings.get("min_volume_spike_mult", 0)) > 0 and snapshot["volume_spike_ratio"] < _safe_float(settings.get("min_volume_spike_mult", 0)):
        blocker_reasons.append("volume_spike_below_threshold")
    if settings.get("anti_rug") and snapshot["can_exit"] is False:
        blocker_reasons.append("cannot_exit")
    if settings.get("anti_rug") and snapshot["transfer_hook_enabled"]:
        blocker_reasons.append("transfer_hook_enabled")
    if snapshot["threat_risk_score"] >= 70:
        blocker_reasons.append("threat_risk_too_high")

    if snapshot["liq"] >= _safe_float(settings.get("min_liq", 0)):
        pass_reasons.append("liquidity_ok")
    if snapshot["vol"] >= _safe_float(settings.get("min_vol", 0)):
        pass_reasons.append("volume_ok")
    if snapshot["score"] >= _safe_float(settings.get("min_score", 0)):
        pass_reasons.append("ai_score_ok")
    if snapshot["green_lights"] >= _safe_int(settings.get("min_green_lights", 0)):
        pass_reasons.append("green_lights_ok")
    if snapshot["narrative_score"] >= _safe_int(settings.get("min_narrative_score", 0)):
        pass_reasons.append("narrative_ok")
    if snapshot["holder_growth_1h"] >= _safe_float(settings.get("min_holder_growth_pct", 0)):
        pass_reasons.append("holder_growth_ok")
    if snapshot["volume_spike_ratio"] >= _safe_float(settings.get("min_volume_spike_mult", 0)):
        pass_reasons.append("volume_spike_ok")
    if snapshot["threat_risk_score"] < 45:
        pass_reasons.append("threat_risk_ok")

    score = snapshot["composite_score"]
    score += _normalize(snapshot["liq"], 0, max(_safe_float(settings.get("min_liq", 0)) * 2, 1.0)) * 6
    score += _normalize(snapshot["vol"], 0, max(_safe_float(settings.get("min_vol", 0)) * 2, 1.0)) * 5
    score += _normalize(snapshot["green_lights"], 0, 3) * 8
    score += _normalize(snapshot["narrative_score"], 0, max(_safe_float(settings.get("min_narrative_score", 0)) + 30, 30)) * 6
    score -= len(blocker_reasons) * 8
    score = max(0.0, min(100.0, score))

    confidence = _clamp(snapshot["confidence"] + (len(pass_reasons) * 0.035) - (len(blocker_reasons) * 0.08))
    passed = not blocker_reasons and score >= 45 and confidence >= 0.4

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

    tp_mult = max(_safe_float(position.get("take_profit_mult"), settings.get("tp2_mult", 2.0)), 1.01)
    stop_ratio = _safe_float(position.get("stop_loss_ratio"), settings.get("stop_loss", 0.7))
    time_stop_min = _safe_int(position.get("time_stop_min"), settings.get("time_stop_min", 30))

    status = "open"
    exit_reason = ""
    if current_price >= entry_price * tp_mult:
        status = "closed"
        exit_reason = "take_profit"
    elif stop_ratio > 0 and current_price <= entry_price * stop_ratio:
        status = "closed"
        exit_reason = "stop_loss"
    elif age_min >= time_stop_min:
        status = "closed"
        exit_reason = "time_stop"

    realized_pnl_pct = ((current_price / entry_price) - 1.0) * 100.0 if entry_price and status == "closed" else None
    return {
        "status": status,
        "exit_reason": exit_reason,
        "current_price": current_price,
        "peak_price": peak_price,
        "trough_price": trough_price,
        "max_upside_pct": round(max_upside_pct, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "realized_pnl_pct": round(realized_pnl_pct, 2) if realized_pnl_pct is not None else None,
    }
