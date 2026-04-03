"""Spark driver for auto-optimization loop using Ray workers."""

import psycopg2
import json
import os
from datetime import datetime
from itertools import product
import random
import logging
import time

logger = logging.getLogger(__name__)

# Optional Ray import - can work without it for testing
try:
    import ray
    RAY_AVAILABLE = True
except ImportError:
    RAY_AVAILABLE = False
    logger.warning("[Spark] Ray not installed - some features unavailable")

# Import Ray worker
try:
    from shadow_testing.ray_worker import test_parameter_set_on_live_data
except ImportError:
    logger.warning("[Spark] Ray worker not available")


class AutoOptimizationLoop:
    """Orchestrates parameter optimization via Ray workers."""

    def __init__(self, optimization_interval_minutes=5):
        """Initialize the auto-optimization loop."""
        self.interval = optimization_interval_minutes
        self.db_url = os.getenv("DATABASE_URL")

        if not self.db_url:
            raise ValueError("DATABASE_URL environment variable not set")

        # Initialize Ray if available and not already initialized
        if RAY_AVAILABLE:
            if not ray.is_initialized():
                try:
                    ray.init(
                        num_cpus=8,
                        object_store_memory=4_000_000_000,
                        ignore_reinit_error=True,
                    )
                    logger.info("[Spark] Ray cluster initialized")
                except Exception as e:
                    logger.warning(
                        f"[Spark] Ray already initialized or init error: {e}"
                    )

        logger.info(
            f"[Spark] Auto-optimizer initialized. Interval: {self.interval} min"
        )

    def run_continuous(self):
        """Run optimization loop indefinitely."""
        logger.info("[Spark] Starting continuous optimization loop...")
        while True:
            try:
                self.run_one_cycle()
            except Exception as e:
                logger.error(f"[Spark] Error in cycle: {e}")

            # Wait for next cycle
            time.sleep(self.interval * 60)

    def run_one_cycle(self):
        """Execute one optimization cycle."""
        logger.info(f"\n[Spark] Starting optimization cycle at {datetime.utcnow()}")

        # 1. Generate parameter grid
        param_sets = self.generate_parameter_grid()
        logger.info(f"[Spark] Generated {len(param_sets)} parameter combinations to test")

        # 2. Get list of active mints
        active_mints = self.get_active_mints()
        if not active_mints:
            logger.warning("[Spark] No active mints, skipping cycle")
            return

        logger.info(f"[Spark] Testing on {len(active_mints)} active mints")

        # 3. Deploy Ray tasks to test each parameter set
        futures = []
        for param_set in param_sets:
            future = test_parameter_set_on_live_data.remote(
                param_set, active_mints, time_window_minutes=60, dataset_id="live"
            )
            futures.append((param_set, future))

        logger.info(f"[Spark] Deployed {len(futures)} Ray tasks")

        # 4. Collect results as they complete
        results = []
        failed_count = 0
        for param_set, future in futures:
            try:
                result = ray.get(future, timeout=120)  # 2 min timeout per task
                if result.get("status") == "tested":
                    results.append(result)
                else:
                    failed_count += 1
            except Exception as e:
                logger.warning(f"[Spark] Task timeout/error: {e}")
                failed_count += 1

        logger.info(
            f"[Spark] Collected results from {len(results)} tasks "
            f"({failed_count} failed)"
        )

        if not results:
            logger.error("[Spark] No valid results, skipping deployment decision")
            return

        # 5. Find best result
        best_result = max(results, key=lambda r: r.get("avg_pnl_pct", -999))

        # 6. Get current production parameters
        current_params = self.get_current_production_params()
        current_pnl = current_params.get("last_1h_avg_pnl", 0)

        # 7. Decision logic
        best_pnl = best_result.get("avg_pnl_pct", 0)
        improvement = best_pnl - current_pnl

        logger.info(
            f"[Spark] Best result: {best_pnl:.2f}% P&L (current: {current_pnl:.2f}%)"
        )
        logger.info(f"[Spark] Improvement: {improvement:.2f}%")

        # 8. Log decision (don't deploy yet - Phase 4 integrates deployment)
        if improvement > 5.0:
            logger.info(
                f"[Spark] APPROVED: {improvement:.2f}% improvement (> 5% threshold)"
            )
            self.log_optimization_decision(
                from_params=current_params.get("settings", {}),
                to_params=best_result["parameter_set"],
                reason=f"shadow_test_showed_{improvement:.1f}pct_improvement",
                expected_improvement=improvement,
                approved=True,
            )
        else:
            logger.info(
                f"[Spark] REJECTED: {improvement:.2f}% improvement (< 5% threshold)"
            )
            self.log_optimization_decision(
                from_params=current_params.get("settings", {}),
                to_params=best_result["parameter_set"],
                reason=f"insufficient_improvement_{improvement:.1f}pct",
                expected_improvement=improvement,
                approved=False,
            )

        logger.info(f"[Spark] Cycle complete at {datetime.utcnow()}")

    def generate_parameter_grid(self, sample_size=300):
        """Generate random parameter combinations."""
        grid = {
            "tp1_mult": [1.15, 1.3, 1.5, 1.8, 2.0],
            "tp2_mult": [2.0, 3.0, 4.0, 5.0, 8.0],
            "stop_loss": [0.65, 0.70, 0.75, 0.80, 0.85],
            "trail_pct": [0.10, 0.15, 0.20, 0.25, 0.30],
            "time_stop_min": [15, 20, 30, 45],
            "max_threat_score": [45, 50, 60],
        }

        # Generate all combinations
        all_combos = list(product(*grid.values()))

        # Sample random subset
        sample = random.sample(all_combos, min(sample_size, len(all_combos)))

        # Convert to dicts
        keys = grid.keys()
        param_sets = [dict(zip(keys, combo)) for combo in sample]

        return param_sets

    def get_active_mints(self, limit=20):
        """Get list of mints with recent trading activity."""
        try:
            conn = psycopg2.connect(self.db_url)
            cur = conn.cursor()

            cur.execute(
                """
                SELECT DISTINCT mint FROM tick_prices
                WHERE time >= NOW() - INTERVAL '1 hour'
                ORDER BY mint
                LIMIT %s
            """,
                (limit,),
            )

            mints = [row[0] for row in cur.fetchall()]
            cur.close()
            conn.close()

            if mints:
                return mints
            else:
                # Fallback to SOL if no data
                logger.warning(
                    "[Spark] No active mints found, using fallback list"
                )
                return ["So11111111111111111111111111111111111111112"]
        except Exception as e:
            logger.error(f"[Spark] Error fetching active mints: {e}")
            return ["So11111111111111111111111111111111111111112"]

    def get_current_production_params(self):
        """Get current live parameters and their recent performance."""
        try:
            conn = psycopg2.connect(self.db_url)
            cur = conn.cursor()

            # Get current params from bot_settings
            cur.execute(
                "SELECT custom_settings FROM bot_settings WHERE user_id = 1 LIMIT 1"
            )
            row = cur.fetchone()
            current_settings = json.loads(row[0]) if row and row[0] else {}

            # Get 1h performance from shadow_positions_live
            cur.execute(
                """
                SELECT
                    AVG(realized_pnl_pct) as avg_pnl,
                    COUNT(*) as trades,
                    SUM(CASE WHEN realized_pnl_pct > 0 THEN 1 ELSE 0 END)::FLOAT
                    / NULLIF(COUNT(*), 0) as win_rate
                FROM shadow_positions
                WHERE status = 'closed'
                AND strategy_name = 'shadow_live'
                AND closed_at >= NOW() - INTERVAL '1 hour'
            """
            )

            perf = cur.fetchone()

            cur.close()
            conn.close()

            return {
                "settings": current_settings,
                "last_1h_avg_pnl": float(perf[0]) if perf and perf[0] else 0,
                "last_1h_trades": int(perf[1]) if perf and perf[1] else 0,
                "last_1h_win_rate": float(perf[2]) if perf and perf[2] else 0,
            }
        except Exception as e:
            logger.error(f"[Spark] Error fetching current params: {e}")
            return {"settings": {}, "last_1h_avg_pnl": 0}

    def log_optimization_decision(
        self, from_params, to_params, reason, expected_improvement, approved
    ):
        """Log the optimization decision to database."""
        try:
            conn = psycopg2.connect(self.db_url)
            cur = conn.cursor()

            # First, ensure optimization_decisions table exists
            self._ensure_optimization_decisions_table(cur)

            cur.execute(
                """
                INSERT INTO optimization_decisions
                (decision_time, from_parameter_set, to_parameter_set, reason, expected_improvement, status)
                VALUES (%s, %s, %s, %s, %s, %s)
            """,
                (
                    datetime.utcnow(),
                    json.dumps(from_params),
                    json.dumps(to_params),
                    reason,
                    expected_improvement,
                    "approved" if approved else "rejected",
                ),
            )

            conn.commit()
            cur.close()
            conn.close()

            logger.info(
                f"[Spark] Logged optimization decision: "
                f"{reason} (approved={approved})"
            )
        except Exception as e:
            logger.error(f"[Spark] Error logging decision: {e}")

    def _ensure_optimization_decisions_table(self, cur):
        """Ensure the optimization_decisions table exists."""
        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS optimization_decisions (
                    id SERIAL PRIMARY KEY,
                    decision_time TIMESTAMP DEFAULT NOW(),
                    from_parameter_set TEXT,
                    to_parameter_set TEXT,
                    reason TEXT,
                    expected_improvement REAL,
                    status TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """
            )
        except Exception as e:
            logger.warning(f"[Spark] Could not create table: {e}")


def main():
    """Main entry point for the optimization loop."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    loop = AutoOptimizationLoop(optimization_interval_minutes=5)
    loop.run_continuous()


if __name__ == "__main__":
    main()
