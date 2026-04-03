"""Ray remote worker for parameter testing against live data."""

import psycopg2
from datetime import datetime, timedelta
import os
import json
import logging

logger = logging.getLogger(__name__)

# Optional imports
try:
    import ray
    RAY_AVAILABLE = True
except ImportError:
    RAY_AVAILABLE = False

try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False


# Conditionally apply ray.remote decorator
def _test_parameter_set_on_live_data(
    param_set,
    mint_list,
    time_window_minutes=60,
    dataset_id="live",
):
    """
    Test a parameter set against last N minutes of live data.

    Args:
        param_set: dict like {'tp1_mult': 1.5, 'stop_loss': 0.8, ...}
        mint_list: list of mints to test on
        time_window_minutes: how many minutes of historical data to test on
        dataset_id: label for dataset (e.g., 'live', 'historical')

    Returns:
        dict with: parameter_set, trades_closed, avg_pnl_pct, win_rate, max_drawdown_pct
    """

    # Connect to TimescaleDB
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        return {
            "parameter_set": param_set,
            "trades_closed": 0,
            "avg_pnl_pct": 0,
            "win_rate": 0,
            "max_drawdown_pct": 0,
            "error": "DATABASE_URL not set",
            "status": "failed",
        }

    conn = None
    try:
        conn = psycopg2.connect(db_url)

        # Fetch tick data for time window
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(minutes=time_window_minutes)

        query = """
            SELECT mint, time, price, bid, ask, volume_1m, liquidity_usd
            FROM tick_prices
            WHERE time >= %s AND time <= %s
            AND mint = ANY(%s)
            ORDER BY mint, time ASC
        """

        df = pd.read_sql(query, conn, params=[start_time, end_time, mint_list])

        if df.empty:
            logger.info(
                f"[Ray] No data found for mints {mint_list}, returning zero metrics"
            )
            return {
                "parameter_set": param_set,
                "trades_closed": 0,
                "avg_pnl_pct": 0,
                "win_rate": 0,
                "max_drawdown_pct": 0,
                "tested_at": datetime.utcnow().isoformat(),
                "dataset_id": dataset_id,
                "status": "tested",
            }

        # Simulate shadow trading with this parameter set
        result = simulate_shadow_trades_on_live_data(df, param_set)

        result.update(
            {
                "parameter_set": param_set,
                "tested_at": datetime.utcnow().isoformat(),
                "dataset_id": dataset_id,
                "status": "tested",
            }
        )

        return result

    except Exception as e:
        logger.error(f"[Ray] Error testing parameter set: {e}")
        return {
            "parameter_set": param_set,
            "trades_closed": 0,
            "avg_pnl_pct": 0,
            "win_rate": 0,
            "max_drawdown_pct": 0,
            "error": str(e),
            "status": "failed",
        }
    finally:
        if conn:
            conn.close()


# Apply ray.remote decorator if available
if RAY_AVAILABLE:
    test_parameter_set_on_live_data = ray.remote(_test_parameter_set_on_live_data)
else:
    test_parameter_set_on_live_data = _test_parameter_set_on_live_data


def simulate_shadow_trades_on_live_data(df_prices, param_set):
    """
    Simulate opening/updating/closing shadow positions using param_set
    against live price data.

    Args:
        df_prices: DataFrame with columns [mint, time, price, bid, ask, ...]
        param_set: dict with parameters like tp1_mult, stop_loss, trail_pct, etc.

    Returns:
        dict: {trades_closed, avg_pnl_pct, win_rate, max_drawdown_pct}
    """

    # Group by mint to process each token's price series independently
    positions = {}  # mint -> {entry_price, entry_time, peak, trough, ...}
    closed_trades = []

    for mint, group in df_prices.groupby("mint"):
        group = group.sort_values("time").reset_index(drop=True)

        for idx, row in group.iterrows():
            current_time = row["time"]
            current_price = row["price"]

            # Entry logic: if no open position and price conditions met
            if mint not in positions:
                # Simple entry: buy on any price update (no filter for now)
                # In production, this would check entry filters
                positions[mint] = {
                    "entry_price": current_price,
                    "entry_time": current_time,
                    "peak_price": current_price,
                    "trough_price": current_price,
                    "status": "open",
                }

            # Update position
            if mint in positions and positions[mint]["status"] == "open":
                pos = positions[mint]
                pos["peak_price"] = max(pos["peak_price"], current_price)
                pos["trough_price"] = min(pos["trough_price"], current_price)

                # Check exit conditions
                entry_price = pos["entry_price"]
                tp1_mult = param_set.get("tp1_mult", 1.5)
                stop_loss = param_set.get("stop_loss", 0.8)
                trail_pct = param_set.get("trail_pct", 0.20)

                pnl_pct = ((current_price - entry_price) / entry_price - 1.0) * 100

                # Exit: take profit
                if current_price >= entry_price * tp1_mult:
                    closed_trades.append(
                        {
                            "mint": mint,
                            "entry_price": entry_price,
                            "exit_price": current_price,
                            "pnl_pct": pnl_pct,
                            "peak_price": pos["peak_price"],
                            "trough_price": pos["trough_price"],
                            "duration_sec": (current_time - pos["entry_time"]).total_seconds(),
                        }
                    )
                    del positions[mint]

                # Exit: stop loss
                elif current_price <= entry_price * stop_loss:
                    closed_trades.append(
                        {
                            "mint": mint,
                            "entry_price": entry_price,
                            "exit_price": current_price,
                            "pnl_pct": pnl_pct,
                            "peak_price": pos["peak_price"],
                            "trough_price": pos["trough_price"],
                            "duration_sec": (current_time - pos["entry_time"]).total_seconds(),
                        }
                    )
                    del positions[mint]

                # Exit: trailing stop
                elif current_price < pos["peak_price"] * (1.0 - trail_pct):
                    closed_trades.append(
                        {
                            "mint": mint,
                            "entry_price": entry_price,
                            "exit_price": current_price,
                            "pnl_pct": pnl_pct,
                            "peak_price": pos["peak_price"],
                            "trough_price": pos["trough_price"],
                            "duration_sec": (current_time - pos["entry_time"]).total_seconds(),
                        }
                    )
                    del positions[mint]

    # Calculate metrics
    if closed_trades:
        avg_pnl = sum(t["pnl_pct"] for t in closed_trades) / len(closed_trades)
        win_rate = (
            sum(1 for t in closed_trades if t["pnl_pct"] > 0) / len(closed_trades)
        )
        # Calculate max drawdown across all trades
        max_dd = min(
            (
                (t["trough_price"] - t["entry_price"]) / t["entry_price"] * 100
                if t["entry_price"] > 0
                else 0
            )
            for t in closed_trades
        )
    else:
        avg_pnl = 0
        win_rate = 0
        max_dd = 0

    return {
        "trades_closed": len(closed_trades),
        "avg_pnl_pct": round(avg_pnl, 2),
        "win_rate": round(win_rate, 4),
        "max_drawdown_pct": round(max_dd, 2),
    }
