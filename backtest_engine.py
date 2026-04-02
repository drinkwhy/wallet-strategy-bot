import json
from dataclasses import dataclass, field

from learning_engine import classify_flow_regime_row, score_feature_snapshot_with_family
from quant_platform import (
    build_feature_snapshot, evaluate_shadow_strategy, shadow_position_update,
    estimate_exit_friction, estimate_entry_friction,
)


@dataclass
class BacktestTrade:
    run_id: int
    strategy_name: str
    mint: str
    name: str
    opened_at: object
    closed_at: object
    entry_price: float
    exit_price: float
    status: str
    score: float
    confidence: float
    max_upside_pct: float
    max_drawdown_pct: float
    realized_pnl_pct: float
    exit_reason: str
    feature_json: str
    decision_json: str
    # Friction-adjusted fields — realistic P&L after slippage/MEV/fees
    friction_pnl_pct: float = 0.0
    entry_friction_pct: float = 0.0
    exit_friction_pct: float = 0.0
    mev_probability: float = 0.0

    def as_insert_tuple(self):
        return (
            self.run_id,
            self.strategy_name,
            self.mint,
            self.name,
            self.opened_at,
            self.closed_at,
            self.entry_price,
            self.exit_price,
            self.status,
            self.score,
            self.confidence,
            self.max_upside_pct,
            self.max_drawdown_pct,
            self.realized_pnl_pct,
            self.exit_reason,
            self.feature_json,
            self.decision_json,
            self.friction_pnl_pct,
            self.entry_friction_pct,
            self.exit_friction_pct,
            self.mev_probability,
        )


def _to_snapshot(row):
    payload = row.get("feature_json") or "{}"
    try:
        snapshot = json.loads(payload)
    except Exception:
        snapshot = {}
    snapshot["price"] = float(row.get("price") or snapshot.get("price") or 0)
    snapshot["mint"] = row.get("mint")
    snapshot["name"] = row.get("name")
    if "composite_score" not in snapshot or "confidence" not in snapshot:
        # Rebuild features from whatever data exists in the snapshot.
        # Use snapshot as both info and intel so all stored fields are considered.
        rebuilt = build_feature_snapshot(snapshot, snapshot)
        # IMPORTANT: rebuilt goes SECOND so its computed values (composite_score,
        # confidence, quality scores) are NOT overwritten by zero-valued raw fields.
        # Only keep non-zero raw values over rebuilt defaults.
        merged = dict(rebuilt)
        for key, val in snapshot.items():
            if val not in (None, "", 0, 0.0, False) or key in ("mint", "name", "price"):
                merged[key] = val
        snapshot = merged
    return snapshot


def _event_payload_info(row):
    payload = row.get("payload_json") or "{}"
    try:
        info = json.loads(payload)
    except Exception:
        info = {}
    if not isinstance(info, dict):
        info = {}
    return info


def _to_event_snapshot(row, previous_snapshot=None):
    info = _event_payload_info(row)
    event_type = row.get("event_type") or "market_tick"
    previous_snapshot = dict(previous_snapshot or {})
    intel = info.get("intel") or {}
    if previous_snapshot:
        intel = {**previous_snapshot, **intel}
    if event_type == "wallet_buy":
        intel["unique_buyer_count"] = int(intel.get("unique_buyer_count") or 0) + 1
        intel["total_buy_sol"] = float(intel.get("total_buy_sol") or 0) + float(info.get("sol") or 0)
        intel["smart_wallet_buys"] = int(intel.get("smart_wallet_buys") or 0) + (1 if info.get("smart_wallet") else 0)
    elif event_type == "wallet_sell":
        intel["unique_seller_count"] = int(intel.get("unique_seller_count") or 0) + 1
        intel["total_sell_sol"] = float(intel.get("total_sell_sol") or 0) + float(info.get("sol") or 0)
    elif event_type in {"liquidity_add", "liquidity_drop"}:
        intel["liquidity_drop_pct"] = float(row.get("change_pct") or info.get("delta_pct") or intel.get("liquidity_drop_pct") or 0)

    total_buy_sol = float(intel.get("total_buy_sol") or 0)
    total_sell_sol = float(intel.get("total_sell_sol") or 0)
    intel["net_flow_sol"] = round(total_buy_sol - total_sell_sol, 4)
    unique_buyers = int(intel.get("unique_buyer_count") or 0)
    unique_sellers = int(intel.get("unique_seller_count") or 0)
    intel["buy_sell_ratio"] = round(unique_buyers / max(unique_sellers, 1), 2) if unique_buyers else float(intel.get("buy_sell_ratio") or 0)

    enriched = {
        **previous_snapshot,
        **info,
        "price": row.get("price") or info.get("price") or 0,
        "mc": row.get("mc") or info.get("mc") or 0,
        "liq": row.get("liq") or info.get("liq") or 0,
        "vol": row.get("vol") or info.get("vol") or 0,
        "age_min": row.get("age_min") or info.get("age_min") or 0,
        "change": row.get("change_pct") or info.get("change") or 0,
        "mint": row.get("mint") or info.get("mint"),
        "name": row.get("name") or info.get("name"),
    }
    for field in ("price", "mc", "liq", "vol", "age_min", "change", "score", "green_lights", "narrative_score"):
        if enriched.get(field) in (None, "", 0):
            enriched[field] = previous_snapshot.get(field, enriched.get(field))
    snapshot = build_feature_snapshot(enriched, intel)
    snapshot["price"] = float(enriched.get("price") or snapshot.get("price") or 0)
    snapshot["mint"] = enriched.get("mint")
    snapshot["name"] = enriched.get("name") or "Unknown"
    snapshot["event_type"] = event_type
    snapshot["source"] = row.get("source") or info.get("source") or "scanner"
    return snapshot


def _apply_trade_friction(ideal_pnl_pct, entry_price, exit_price, entry_friction, snapshot, settings, exit_reason):
    """Compute friction-adjusted P&L for a completed backtest trade.

    Combines:
    - Entry friction already baked into the effective entry price (stored separately)
    - Exit friction: slippage, price impact, tx failure, delay, fees
    Returns dict with friction_pnl_pct and breakdown components.
    """
    liq_usd = float(snapshot.get("liq") or 0) * 1000 if float(snapshot.get("liq") or 0) < 1000 else float(snapshot.get("liq") or 0)
    vol_usd = float(snapshot.get("vol") or 0)
    entry_sol = float(settings.get("max_buy_sol") or 0.04)
    exit_f = estimate_exit_friction(
        realized_pnl_pct=ideal_pnl_pct,
        exit_price=exit_price,
        entry_price=entry_price,
        entry_sol=entry_sol,
        liq_usd=liq_usd,
        vol_usd=vol_usd,
        exit_reason=exit_reason,
        peak_ratio=max((exit_price / entry_price) if entry_price else 1.0, 1.0),
    )
    # The entry friction already inflated the entry price, so the ideal_pnl_pct
    # is already lower than clean price would imply. Exit friction compounds on top.
    friction_pnl_pct = exit_f.get("friction_pnl_pct")
    if friction_pnl_pct is None:
        friction_pnl_pct = ideal_pnl_pct
    return {
        "friction_pnl_pct": round(friction_pnl_pct, 2),
        "exit_friction_pct": round(exit_f.get("total_friction_pct", 0), 2),
        "entry_friction_pct": round(entry_friction, 2),
    }


def simulate_backtest(run_id, snapshot_rows, strategy_settings):
    open_positions = {}
    completed = []
    metrics = {
        "snapshots_processed": 0,
        "tokens_processed": len({row.get("mint") for row in snapshot_rows if row.get("mint")}),
        "strategies": {},
    }

    for strategy_name in strategy_settings:
        metrics["strategies"][strategy_name] = {
            "decisions": 0,
            "passed": 0,
            "blocked": 0,
            "trades_opened": 0,
            "trades_closed": 0,
            "blocker_counts": {},  # track WHY entries are blocked
        }

    for row in snapshot_rows:
        mint = row.get("mint")
        created_at = row.get("created_at")
        name = row.get("name") or "Unknown"
        if not mint or not created_at:
            continue
        snapshot = _to_snapshot(row)
        metrics["snapshots_processed"] += 1

        for strategy_name, settings in strategy_settings.items():
            key = (strategy_name, mint)
            tracker = metrics["strategies"][strategy_name]

            if key in open_positions:
                position = open_positions[key]
                age_min = max(0.0, (created_at - position["opened_at"]).total_seconds() / 60.0)
                update = shadow_position_update(position, snapshot.get("price"), settings, age_min)
                position.update({
                    "current_price": update["current_price"],
                    "peak_price": update["peak_price"],
                    "trough_price": update["trough_price"],
                    "max_upside_pct": update["max_upside_pct"],
                    "max_drawdown_pct": update["max_drawdown_pct"],
                })
                if update["status"] == "closed":
                    tracker["trades_closed"] += 1
                    ideal_pnl = update["realized_pnl_pct"] or 0.0
                    _frict = _apply_trade_friction(
                        ideal_pnl, position["entry_price"], update["current_price"],
                        position.get("entry_friction_pct", 0.0), snapshot, settings,
                        update["exit_reason"] or "rule_exit",
                    )
                    completed.append(BacktestTrade(
                        run_id=run_id,
                        strategy_name=strategy_name,
                        mint=mint,
                        name=name,
                        opened_at=position["opened_at"],
                        closed_at=created_at,
                        entry_price=position["entry_price"],
                        exit_price=update["current_price"],
                        status="closed",
                        score=position["score"],
                        confidence=position["confidence"],
                        max_upside_pct=update["max_upside_pct"],
                        max_drawdown_pct=update["max_drawdown_pct"],
                        realized_pnl_pct=ideal_pnl,
                        exit_reason=update["exit_reason"] or "rule_exit",
                        feature_json=position["feature_json"],
                        decision_json=position["decision_json"],
                        friction_pnl_pct=_frict["friction_pnl_pct"],
                        entry_friction_pct=_frict["entry_friction_pct"],
                        exit_friction_pct=_frict["exit_friction_pct"],
                        mev_probability=position.get("mev_probability", 0.0),
                    ))
                    del open_positions[key]
                continue

            tracker["decisions"] += 1
            decision = evaluate_shadow_strategy(strategy_name, settings, snapshot)
            if decision.passed:
                tracker["passed"] += 1
                tracker["trades_opened"] += 1
                # Apply entry friction (MEV + slippage) — inflates the effective entry price
                _entry_f = estimate_entry_friction(
                    snapshot.get("price") or 0.0,
                    liq_usd=float(snapshot.get("liq") or 0),
                    entry_sol=float(settings.get("max_buy_sol") or 0.04),
                    priority_fee=float(settings.get("priority_fee") or 30000),
                )
                _eff_entry = _entry_f["effective_entry_price"]
                open_positions[key] = {
                    "opened_at": created_at,
                    "entry_price": _eff_entry,
                    "current_price": _eff_entry,
                    "peak_price": _eff_entry,
                    "trough_price": _eff_entry,
                    "take_profit_mult": decision.metrics.get("take_profit_mult"),
                    "stop_loss_ratio": decision.metrics.get("stop_loss_ratio"),
                    "time_stop_min": decision.metrics.get("time_stop_min"),
                    "score": decision.score,
                    "confidence": decision.confidence,
                    "feature_json": json.dumps(snapshot),
                    "decision_json": json.dumps(decision.as_dict()),
                    "entry_friction_pct": _entry_f["entry_friction_pct"],
                    "mev_probability": _entry_f["mev_probability"],
                }
            else:
                tracker["blocked"] += 1
                for reason in (decision.blocker_reasons or []):
                    tracker["blocker_counts"][reason] = tracker["blocker_counts"].get(reason, 0) + 1
                if not decision.blocker_reasons:
                    if decision.score < 40:
                        tracker["blocker_counts"]["score_below_threshold"] = tracker["blocker_counts"].get("score_below_threshold", 0) + 1
                    elif decision.confidence < 0.35:
                        tracker["blocker_counts"]["confidence_below_threshold"] = tracker["blocker_counts"].get("confidence_below_threshold", 0) + 1

    if snapshot_rows:
        final_ts = snapshot_rows[-1].get("created_at")
        final_prices = {}
        for row in snapshot_rows:
            final_prices[row.get("mint")] = float(row.get("price") or 0.0)
        for (strategy_name, mint), position in list(open_positions.items()):
            settings = strategy_settings[strategy_name]
            last_price = final_prices.get(mint) or position["current_price"]
            age_min = max(0.0, (final_ts - position["opened_at"]).total_seconds() / 60.0) if final_ts else 0.0
            update = shadow_position_update(position, last_price, settings, age_min)
            metrics["strategies"][strategy_name]["trades_closed"] += 1
            _last_snap = {"liq": 0, "vol": 0}  # no snapshot available at run end
            ideal_pnl = ((update["current_price"] / position["entry_price"]) - 1.0) * 100.0 if position["entry_price"] else 0.0
            _frict = _apply_trade_friction(
                ideal_pnl, position["entry_price"], update["current_price"],
                position.get("entry_friction_pct", 0.0), _last_snap, settings,
                update["exit_reason"] or "run_end",
            )
            completed.append(BacktestTrade(
                run_id=run_id,
                strategy_name=strategy_name,
                mint=mint,
                name=next((row.get("name") for row in reversed(snapshot_rows) if row.get("mint") == mint), "Unknown"),
                opened_at=position["opened_at"],
                closed_at=final_ts,
                entry_price=position["entry_price"],
                exit_price=update["current_price"],
                status="closed",
                score=position["score"],
                confidence=position["confidence"],
                max_upside_pct=update["max_upside_pct"],
                max_drawdown_pct=update["max_drawdown_pct"],
                realized_pnl_pct=ideal_pnl,
                exit_reason=update["exit_reason"] or "run_end",
                feature_json=position["feature_json"],
                decision_json=position["decision_json"],
                friction_pnl_pct=_frict["friction_pnl_pct"],
                entry_friction_pct=_frict["entry_friction_pct"],
                exit_friction_pct=_frict["exit_friction_pct"],
                mev_probability=position.get("mev_probability", 0.0),
            ))

    summaries = {}
    for strategy_name in strategy_settings:
        trades = [trade for trade in completed if trade.strategy_name == strategy_name]
        wins = [trade for trade in trades if trade.realized_pnl_pct > 0]
        losses = [trade for trade in trades if trade.realized_pnl_pct <= 0]
        friction_wins = [trade for trade in trades if trade.friction_pnl_pct > 0]
        avg_pnl = round(sum(t.realized_pnl_pct for t in trades) / len(trades), 2) if trades else 0.0
        avg_friction_pnl = round(sum(t.friction_pnl_pct for t in trades) / len(trades), 2) if trades else 0.0
        avg_mev_prob = round(sum(t.mev_probability for t in trades) / len(trades), 3) if trades else 0.0
        avg_upside = round(sum(t.max_upside_pct for t in trades) / len(trades), 2) if trades else 0.0
        avg_drawdown = round(sum(t.max_drawdown_pct for t in trades) / len(trades), 2) if trades else 0.0
        summaries[strategy_name] = {
            **metrics["strategies"][strategy_name],
            "closed_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round((len(wins) / len(trades)) * 100, 1) if trades else 0.0,
            "avg_pnl_pct": avg_pnl,
            "avg_friction_pnl_pct": avg_friction_pnl,
            "friction_win_rate": round((len(friction_wins) / len(trades)) * 100, 1) if trades else 0.0,
            "avg_mev_probability": avg_mev_prob,
            "avg_upside_pct": avg_upside,
            "avg_drawdown_pct": avg_drawdown,
            "best_trade_pct": max((trade.realized_pnl_pct for trade in trades), default=0.0),
            "worst_trade_pct": min((trade.realized_pnl_pct for trade in trades), default=0.0),
        }

    return {
        "trades": completed,
        "summary": {
            "replay_mode": "snapshot",
            "tokens_processed": metrics["tokens_processed"],
            "snapshots_processed": metrics["snapshots_processed"],
            "trades_closed": len(completed),
            "strategies": summaries,
        },
    }


def simulate_event_tape_backtest(run_id, event_rows, strategy_settings):
    open_positions = {}
    completed = []
    last_snapshots = {}
    metrics = {
        "events_processed": 0,
        "tokens_processed": len({row.get("mint") for row in event_rows if row.get("mint")}),
        "event_type_counts": {},
        "source_counts": {},
        "strategies": {},
    }

    for strategy_name in strategy_settings:
        metrics["strategies"][strategy_name] = {
            "decisions": 0,
            "passed": 0,
            "blocked": 0,
            "trades_opened": 0,
            "trades_closed": 0,
            "blocker_counts": {},
        }

    for row in event_rows:
        mint = row.get("mint")
        created_at = row.get("created_at")
        if not mint or not created_at:
            continue
        snapshot = _to_event_snapshot(row, previous_snapshot=last_snapshots.get(mint))
        last_snapshots[mint] = dict(snapshot)
        name = snapshot.get("name") or row.get("name") or "Unknown"
        metrics["events_processed"] += 1
        event_type = snapshot.get("event_type") or "market_tick"
        source = snapshot.get("source") or "scanner"
        metrics["event_type_counts"][event_type] = metrics["event_type_counts"].get(event_type, 0) + 1
        metrics["source_counts"][source] = metrics["source_counts"].get(source, 0) + 1

        for strategy_name, settings in strategy_settings.items():
            key = (strategy_name, mint)
            tracker = metrics["strategies"][strategy_name]

            if key in open_positions:
                position = open_positions[key]
                age_min = max(0.0, (created_at - position["opened_at"]).total_seconds() / 60.0)
                update = shadow_position_update(position, snapshot.get("price"), settings, age_min)
                position.update({
                    "current_price": update["current_price"],
                    "peak_price": update["peak_price"],
                    "trough_price": update["trough_price"],
                    "max_upside_pct": update["max_upside_pct"],
                    "max_drawdown_pct": update["max_drawdown_pct"],
                })
                if update["status"] == "closed":
                    tracker["trades_closed"] += 1
                    ideal_pnl_et_mid = update["realized_pnl_pct"] or 0.0
                    _frict_et = _apply_trade_friction(
                        ideal_pnl_et_mid, position["entry_price"], update["current_price"],
                        position.get("entry_friction_pct", 0.0), snapshot, settings,
                        update["exit_reason"] or event_type,
                    )
                    completed.append(BacktestTrade(
                        run_id=run_id,
                        strategy_name=strategy_name,
                        mint=mint,
                        name=name,
                        opened_at=position["opened_at"],
                        closed_at=created_at,
                        entry_price=position["entry_price"],
                        exit_price=update["current_price"],
                        status="closed",
                        score=position["score"],
                        confidence=position["confidence"],
                        max_upside_pct=update["max_upside_pct"],
                        max_drawdown_pct=update["max_drawdown_pct"],
                        realized_pnl_pct=update["realized_pnl_pct"] or 0.0,
                        exit_reason=update["exit_reason"] or event_type,
                        feature_json=position["feature_json"],
                        decision_json=position["decision_json"],
                        friction_pnl_pct=_frict_et["friction_pnl_pct"],
                        entry_friction_pct=_frict_et["entry_friction_pct"],
                        exit_friction_pct=_frict_et["exit_friction_pct"],
                        mev_probability=position.get("mev_probability", 0.0),
                    ))
                    del open_positions[key]
                continue

            tracker["decisions"] += 1
            decision = evaluate_shadow_strategy(strategy_name, settings, snapshot)
            if decision.passed:
                tracker["passed"] += 1
                tracker["trades_opened"] += 1
                _entry_f_et = estimate_entry_friction(
                    snapshot.get("price") or 0.0,
                    liq_usd=float(snapshot.get("liq") or 0),
                    entry_sol=float(settings.get("max_buy_sol") or 0.04),
                    priority_fee=float(settings.get("priority_fee") or 30000),
                )
                _eff_entry_et = _entry_f_et["effective_entry_price"]
                open_positions[key] = {
                    "opened_at": created_at,
                    "entry_price": _eff_entry_et,
                    "current_price": _eff_entry_et,
                    "peak_price": _eff_entry_et,
                    "trough_price": _eff_entry_et,
                    "take_profit_mult": decision.metrics.get("take_profit_mult"),
                    "stop_loss_ratio": decision.metrics.get("stop_loss_ratio"),
                    "time_stop_min": decision.metrics.get("time_stop_min"),
                    "score": decision.score,
                    "confidence": decision.confidence,
                    "feature_json": json.dumps(snapshot),
                    "decision_json": json.dumps(decision.as_dict()),
                    "entry_friction_pct": _entry_f_et["entry_friction_pct"],
                    "mev_probability": _entry_f_et["mev_probability"],
                }
            else:
                tracker["blocked"] += 1
                for reason in (decision.blocker_reasons or []):
                    tracker["blocker_counts"][reason] = tracker["blocker_counts"].get(reason, 0) + 1
                if not decision.blocker_reasons:
                    if decision.score < 40:
                        tracker["blocker_counts"]["score_below_threshold"] = tracker["blocker_counts"].get("score_below_threshold", 0) + 1
                    elif decision.confidence < 0.35:
                        tracker["blocker_counts"]["confidence_below_threshold"] = tracker["blocker_counts"].get("confidence_below_threshold", 0) + 1

    if event_rows:
        final_ts = event_rows[-1].get("created_at")
        final_prices = {}
        final_names = {}
        for row in event_rows:
            mint = row.get("mint")
            if not mint:
                continue
            final_prices[mint] = float(row.get("price") or 0.0)
            final_names[mint] = row.get("name") or final_names.get(mint) or "Unknown"
        for (strategy_name, mint), position in list(open_positions.items()):
            settings = strategy_settings[strategy_name]
            last_price = final_prices.get(mint) or position["current_price"]
            age_min = max(0.0, (final_ts - position["opened_at"]).total_seconds() / 60.0) if final_ts else 0.0
            update = shadow_position_update(position, last_price, settings, age_min)
            metrics["strategies"][strategy_name]["trades_closed"] += 1
            ideal_pnl_et = ((update["current_price"] / position["entry_price"]) - 1.0) * 100.0 if position["entry_price"] else 0.0
            _frict_end = _apply_trade_friction(
                ideal_pnl_et, position["entry_price"], update["current_price"],
                position.get("entry_friction_pct", 0.0), {}, settings,
                update["exit_reason"] or "run_end",
            )
            completed.append(BacktestTrade(
                run_id=run_id,
                strategy_name=strategy_name,
                mint=mint,
                name=final_names.get(mint, "Unknown"),
                opened_at=position["opened_at"],
                closed_at=final_ts,
                entry_price=position["entry_price"],
                exit_price=update["current_price"],
                status="closed",
                score=position["score"],
                confidence=position["confidence"],
                max_upside_pct=update["max_upside_pct"],
                max_drawdown_pct=update["max_drawdown_pct"],
                realized_pnl_pct=ideal_pnl_et,
                exit_reason=update["exit_reason"] or "run_end",
                feature_json=position["feature_json"],
                decision_json=position["decision_json"],
                friction_pnl_pct=_frict_end["friction_pnl_pct"],
                entry_friction_pct=_frict_end["entry_friction_pct"],
                exit_friction_pct=_frict_end["exit_friction_pct"],
                mev_probability=position.get("mev_probability", 0.0),
            ))

    summaries = {}
    for strategy_name in strategy_settings:
        trades = [trade for trade in completed if trade.strategy_name == strategy_name]
        wins = [trade for trade in trades if trade.realized_pnl_pct > 0]
        losses = [trade for trade in trades if trade.realized_pnl_pct <= 0]
        friction_wins = [trade for trade in trades if trade.friction_pnl_pct > 0]
        avg_pnl = round(sum(t.realized_pnl_pct for t in trades) / len(trades), 2) if trades else 0.0
        avg_friction_pnl = round(sum(t.friction_pnl_pct for t in trades) / len(trades), 2) if trades else 0.0
        avg_mev_prob = round(sum(t.mev_probability for t in trades) / len(trades), 3) if trades else 0.0
        avg_upside = round(sum(t.max_upside_pct for t in trades) / len(trades), 2) if trades else 0.0
        avg_drawdown = round(sum(t.max_drawdown_pct for t in trades) / len(trades), 2) if trades else 0.0
        summaries[strategy_name] = {
            **metrics["strategies"][strategy_name],
            "closed_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round((len(wins) / len(trades)) * 100, 1) if trades else 0.0,
            "avg_pnl_pct": avg_pnl,
            "avg_friction_pnl_pct": avg_friction_pnl,
            "friction_win_rate": round((len(friction_wins) / len(trades)) * 100, 1) if trades else 0.0,
            "avg_mev_probability": avg_mev_prob,
            "avg_upside_pct": avg_upside,
            "avg_drawdown_pct": avg_drawdown,
            "best_trade_pct": max((trade.realized_pnl_pct for trade in trades), default=0.0),
            "worst_trade_pct": min((trade.realized_pnl_pct for trade in trades), default=0.0),
        }

    return {
        "trades": completed,
        "summary": {
            "replay_mode": "event_tape",
            "tokens_processed": metrics["tokens_processed"],
            "events_processed": metrics["events_processed"],
            "trades_closed": len(completed),
            "event_type_counts": metrics["event_type_counts"],
            "source_counts": metrics["source_counts"],
            "strategies": summaries,
        },
    }


def _flow_rows_by_mint(flow_rows):
    indexed = {}
    for row in sorted(flow_rows or [], key=lambda item: (item.get("mint") or "", item.get("created_at") or "")):
        mint = row.get("mint")
        if not mint:
            continue
        normalized = dict(row)
        normalized["regime"] = normalized.get("regime") or classify_flow_regime_row(normalized)
        indexed.setdefault(mint, []).append(normalized)
    return indexed


def _active_regime_for_snapshot(snapshot_row, flow_index):
    mint = snapshot_row.get("mint")
    created_at = snapshot_row.get("created_at")
    rows = flow_index.get(mint) or []
    if not rows:
        return "neutral"
    if created_at is None:
        return rows[-1].get("regime") or "neutral"
    nearest = min(
        rows,
        key=lambda row: abs(((row.get("created_at") or created_at) - created_at).total_seconds()) if row.get("created_at") else 10**12,
    )
    return nearest.get("regime") or "neutral"


def simulate_policy_comparison(run_id, snapshot_rows, flow_rows, rule_settings, model_family, model_threshold=60.0):
    policies = {
        "rule_balanced": {"type": "rule"},
        "model_global": {"type": "model", "mode": "global"},
        "model_regime_auto": {"type": "model", "mode": "auto"},
    }
    metrics = {
        "tokens_processed": len({row.get("mint") for row in snapshot_rows if row.get("mint")}),
        "snapshots_processed": 0,
        "policies": {
            key: {"decisions": 0, "passed": 0, "blocked": 0, "trades_opened": 0, "trades_closed": 0}
            for key in policies
        },
    }
    open_positions = {}
    completed = []
    flow_index = _flow_rows_by_mint(flow_rows)

    for row in snapshot_rows:
        mint = row.get("mint")
        created_at = row.get("created_at")
        name = row.get("name") or "Unknown"
        if not mint or not created_at:
            continue
        snapshot = _to_snapshot(row)
        active_regime = _active_regime_for_snapshot(row, flow_index)
        metrics["snapshots_processed"] += 1

        for policy_name, policy in policies.items():
            key = (policy_name, mint)
            tracker = metrics["policies"][policy_name]
            if key in open_positions:
                position = open_positions[key]
                age_min = max(0.0, (created_at - position["opened_at"]).total_seconds() / 60.0)
                update = shadow_position_update(position, snapshot.get("price"), rule_settings, age_min)
                position.update({
                    "current_price": update["current_price"],
                    "peak_price": update["peak_price"],
                    "trough_price": update["trough_price"],
                    "max_upside_pct": update["max_upside_pct"],
                    "max_drawdown_pct": update["max_drawdown_pct"],
                })
                if update["status"] == "closed":
                    tracker["trades_closed"] += 1
                    completed.append(BacktestTrade(
                        run_id=run_id,
                        strategy_name=policy_name,
                        mint=mint,
                        name=name,
                        opened_at=position["opened_at"],
                        closed_at=created_at,
                        entry_price=position["entry_price"],
                        exit_price=update["current_price"],
                        status="closed",
                        score=position["score"],
                        confidence=position["confidence"],
                        max_upside_pct=update["max_upside_pct"],
                        max_drawdown_pct=update["max_drawdown_pct"],
                        realized_pnl_pct=update["realized_pnl_pct"] or 0.0,
                        exit_reason=update["exit_reason"] or "rule_exit",
                        feature_json=position["feature_json"],
                        decision_json=position["decision_json"],
                    ))
                    del open_positions[key]
                continue

            tracker["decisions"] += 1
            if policy["type"] == "rule":
                decision = evaluate_shadow_strategy("balanced", rule_settings, snapshot)
                passed = bool(decision.passed)
                score = decision.score
                confidence = decision.confidence
                decision_json = json.dumps(decision.as_dict())
            else:
                scored = score_feature_snapshot_with_family(snapshot, model_family, active_regime, mode=policy["mode"])
                passed = bool(scored.get("trained")) and float(scored.get("model_score") or 0) >= float(model_threshold)
                score = float(scored.get("model_score") or 0)
                confidence = min(1.0, max(0.0, score / 100.0))
                decision_json = json.dumps({
                    "policy": policy_name,
                    "threshold": model_threshold,
                    "passed": passed,
                    "active_regime": active_regime,
                    "model_key": scored.get("model_key"),
                    "score": score,
                    "top_drivers": scored.get("top_drivers") or [],
                    "selection": scored.get("selection") or {},
                })

            if passed:
                tracker["passed"] += 1
                tracker["trades_opened"] += 1
                open_positions[key] = {
                    "opened_at": created_at,
                    "entry_price": snapshot.get("price") or 0.0,
                    "current_price": snapshot.get("price") or 0.0,
                    "peak_price": snapshot.get("price") or 0.0,
                    "trough_price": snapshot.get("price") or 0.0,
                    "take_profit_mult": rule_settings.get("tp2_mult"),
                    "stop_loss_ratio": rule_settings.get("stop_loss"),
                    "time_stop_min": rule_settings.get("time_stop_min"),
                    "score": score,
                    "confidence": confidence,
                    "feature_json": json.dumps(snapshot),
                    "decision_json": decision_json,
                }
            else:
                tracker["blocked"] += 1

    if snapshot_rows:
        final_ts = snapshot_rows[-1].get("created_at")
        final_prices = {}
        for row in snapshot_rows:
            final_prices[row.get("mint")] = float(row.get("price") or 0.0)
        for (policy_name, mint), position in list(open_positions.items()):
            last_price = final_prices.get(mint) or position["current_price"]
            age_min = max(0.0, (final_ts - position["opened_at"]).total_seconds() / 60.0) if final_ts else 0.0
            update = shadow_position_update(position, last_price, rule_settings, age_min)
            metrics["policies"][policy_name]["trades_closed"] += 1
            completed.append(BacktestTrade(
                run_id=run_id,
                strategy_name=policy_name,
                mint=mint,
                name=next((row.get("name") for row in reversed(snapshot_rows) if row.get("mint") == mint), "Unknown"),
                opened_at=position["opened_at"],
                closed_at=final_ts,
                entry_price=position["entry_price"],
                exit_price=update["current_price"],
                status="closed",
                score=position["score"],
                confidence=position["confidence"],
                max_upside_pct=update["max_upside_pct"],
                max_drawdown_pct=update["max_drawdown_pct"],
                realized_pnl_pct=((update["current_price"] / position["entry_price"]) - 1.0) * 100.0 if position["entry_price"] else 0.0,
                exit_reason=update["exit_reason"] or "run_end",
                feature_json=position["feature_json"],
                decision_json=position["decision_json"],
            ))

    summaries = []
    for policy_name in policies:
        trades = [trade for trade in completed if trade.strategy_name == policy_name]
        wins = [trade for trade in trades if trade.realized_pnl_pct > 0]
        avg_pnl = round(sum(t.realized_pnl_pct for t in trades) / len(trades), 2) if trades else 0.0
        avg_upside = round(sum(t.max_upside_pct for t in trades) / len(trades), 2) if trades else 0.0
        avg_drawdown = round(sum(t.max_drawdown_pct for t in trades) / len(trades), 2) if trades else 0.0
        summaries.append({
            "policy_name": policy_name,
            **metrics["policies"][policy_name],
            "closed_trades": len(trades),
            "wins": len(wins),
            "win_rate": round((len(wins) / len(trades)) * 100.0, 1) if trades else 0.0,
            "avg_pnl_pct": avg_pnl,
            "avg_upside_pct": avg_upside,
            "avg_drawdown_pct": avg_drawdown,
            "best_trade_pct": max((trade.realized_pnl_pct for trade in trades), default=0.0),
            "worst_trade_pct": min((trade.realized_pnl_pct for trade in trades), default=0.0),
        })

    return {
        "summary": {
            "comparison_mode": "rules_vs_models",
            "tokens_processed": metrics["tokens_processed"],
            "snapshots_processed": metrics["snapshots_processed"],
            "policies": summaries,
        },
        "trades": sorted(completed, key=lambda trade: trade.realized_pnl_pct, reverse=True),
    }
