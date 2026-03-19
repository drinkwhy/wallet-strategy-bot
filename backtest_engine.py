import json
from dataclasses import dataclass

from learning_engine import classify_flow_regime_row, score_feature_snapshot_with_family
from quant_platform import build_feature_snapshot, evaluate_shadow_strategy, shadow_position_update


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
        rebuilt = build_feature_snapshot(snapshot, snapshot)
        snapshot = {**rebuilt, **snapshot}
    return snapshot


def _to_event_snapshot(row):
    payload = row.get("payload_json") or "{}"
    try:
        info = json.loads(payload)
    except Exception:
        info = {}
    if not isinstance(info, dict):
        info = {}
    intel = info.get("intel") or {}
    enriched = {
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
    snapshot = build_feature_snapshot(enriched, intel)
    snapshot["price"] = float(enriched.get("price") or snapshot.get("price") or 0)
    snapshot["mint"] = enriched.get("mint")
    snapshot["name"] = enriched.get("name") or "Unknown"
    snapshot["event_type"] = row.get("event_type") or "market_tick"
    snapshot["source"] = row.get("source") or info.get("source") or "scanner"
    return snapshot


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
                        exit_reason=update["exit_reason"] or "rule_exit",
                        feature_json=position["feature_json"],
                        decision_json=position["decision_json"],
                    ))
                    del open_positions[key]
                continue

            tracker["decisions"] += 1
            decision = evaluate_shadow_strategy(strategy_name, settings, snapshot)
            if decision.passed:
                tracker["passed"] += 1
                tracker["trades_opened"] += 1
                open_positions[key] = {
                    "opened_at": created_at,
                    "entry_price": snapshot.get("price") or 0.0,
                    "current_price": snapshot.get("price") or 0.0,
                    "peak_price": snapshot.get("price") or 0.0,
                    "trough_price": snapshot.get("price") or 0.0,
                    "take_profit_mult": decision.metrics.get("take_profit_mult"),
                    "stop_loss_ratio": decision.metrics.get("stop_loss_ratio"),
                    "time_stop_min": decision.metrics.get("time_stop_min"),
                    "score": decision.score,
                    "confidence": decision.confidence,
                    "feature_json": json.dumps(snapshot),
                    "decision_json": json.dumps(decision.as_dict()),
                }
            else:
                tracker["blocked"] += 1

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
                realized_pnl_pct=((update["current_price"] / position["entry_price"]) - 1.0) * 100.0 if position["entry_price"] else 0.0,
                exit_reason=update["exit_reason"] or "run_end",
                feature_json=position["feature_json"],
                decision_json=position["decision_json"],
            ))

    summaries = {}
    for strategy_name in strategy_settings:
        trades = [trade for trade in completed if trade.strategy_name == strategy_name]
        wins = [trade for trade in trades if trade.realized_pnl_pct > 0]
        losses = [trade for trade in trades if trade.realized_pnl_pct <= 0]
        avg_pnl = round(sum(t.realized_pnl_pct for t in trades) / len(trades), 2) if trades else 0.0
        avg_upside = round(sum(t.max_upside_pct for t in trades) / len(trades), 2) if trades else 0.0
        avg_drawdown = round(sum(t.max_drawdown_pct for t in trades) / len(trades), 2) if trades else 0.0
        summaries[strategy_name] = {
            **metrics["strategies"][strategy_name],
            "closed_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round((len(wins) / len(trades)) * 100, 1) if trades else 0.0,
            "avg_pnl_pct": avg_pnl,
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
        }

    for row in event_rows:
        mint = row.get("mint")
        created_at = row.get("created_at")
        if not mint or not created_at:
            continue
        snapshot = _to_event_snapshot(row)
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
                    ))
                    del open_positions[key]
                continue

            tracker["decisions"] += 1
            decision = evaluate_shadow_strategy(strategy_name, settings, snapshot)
            if decision.passed:
                tracker["passed"] += 1
                tracker["trades_opened"] += 1
                open_positions[key] = {
                    "opened_at": created_at,
                    "entry_price": snapshot.get("price") or 0.0,
                    "current_price": snapshot.get("price") or 0.0,
                    "peak_price": snapshot.get("price") or 0.0,
                    "trough_price": snapshot.get("price") or 0.0,
                    "take_profit_mult": decision.metrics.get("take_profit_mult"),
                    "stop_loss_ratio": decision.metrics.get("stop_loss_ratio"),
                    "time_stop_min": decision.metrics.get("time_stop_min"),
                    "score": decision.score,
                    "confidence": decision.confidence,
                    "feature_json": json.dumps(snapshot),
                    "decision_json": json.dumps(decision.as_dict()),
                }
            else:
                tracker["blocked"] += 1

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
                realized_pnl_pct=((update["current_price"] / position["entry_price"]) - 1.0) * 100.0 if position["entry_price"] else 0.0,
                exit_reason=update["exit_reason"] or "run_end",
                feature_json=position["feature_json"],
                decision_json=position["decision_json"],
            ))

    summaries = {}
    for strategy_name in strategy_settings:
        trades = [trade for trade in completed if trade.strategy_name == strategy_name]
        wins = [trade for trade in trades if trade.realized_pnl_pct > 0]
        losses = [trade for trade in trades if trade.realized_pnl_pct <= 0]
        avg_pnl = round(sum(t.realized_pnl_pct for t in trades) / len(trades), 2) if trades else 0.0
        avg_upside = round(sum(t.max_upside_pct for t in trades) / len(trades), 2) if trades else 0.0
        avg_drawdown = round(sum(t.max_drawdown_pct for t in trades) / len(trades), 2) if trades else 0.0
        summaries[strategy_name] = {
            **metrics["strategies"][strategy_name],
            "closed_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round((len(wins) / len(trades)) * 100, 1) if trades else 0.0,
            "avg_pnl_pct": avg_pnl,
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
