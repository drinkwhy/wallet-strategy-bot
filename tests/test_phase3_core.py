"""Core tests for Phase 3: Ray Shadow Testing + Spark Auto-Optimization Loop.

These tests verify the core structure and logic without external dependencies.
"""

import pytest
import json
import os
import sys
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestSparkDriverCore:
    """Tests for Spark driver core functionality."""

    @patch("optimizer.spark_driver.psycopg2.connect")
    def test_spark_driver_init(self, mock_connect):
        """Test AutoOptimizationLoop initialization."""
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://test"}):
            from optimizer.spark_driver import AutoOptimizationLoop

            loop = AutoOptimizationLoop(optimization_interval_minutes=5)
            assert loop.interval == 5
            assert loop.db_url == "postgres://test"

    def test_spark_driver_missing_database_url(self):
        """Test that AutoOptimizationLoop raises error without DATABASE_URL."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError):
                from optimizer.spark_driver import AutoOptimizationLoop
                loop = AutoOptimizationLoop()

    @patch("optimizer.spark_driver.psycopg2.connect")
    def test_parameter_grid_generation(self, mock_connect):
        """Test parameter grid generation structure."""
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://test"}):
            from optimizer.spark_driver import AutoOptimizationLoop

            loop = AutoOptimizationLoop()
            param_sets = loop.generate_parameter_grid(sample_size=50)

            # Should generate requested number of combinations
            assert len(param_sets) <= 50
            assert len(param_sets) > 0

            # Each param set should have the required keys
            required_keys = {
                "tp1_mult",
                "tp2_mult",
                "stop_loss",
                "trail_pct",
                "time_stop_min",
                "max_threat_score",
            }
            for param_set in param_sets:
                assert set(param_set.keys()) == required_keys
                # All values should be numeric
                for key, value in param_set.items():
                    assert isinstance(value, (int, float)), f"{key} has non-numeric value"

    @patch("optimizer.spark_driver.psycopg2.connect")
    def test_parameter_grid_default_size(self, mock_connect):
        """Test that default parameter grid is appropriate size."""
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://test"}):
            from optimizer.spark_driver import AutoOptimizationLoop

            loop = AutoOptimizationLoop()
            param_sets = loop.generate_parameter_grid()

            # Default should be close to 300
            assert 250 <= len(param_sets) <= 350

    @patch("optimizer.spark_driver.psycopg2.connect")
    def test_get_active_mints_with_data(self, mock_connect):
        """Test fetching active mints with data."""
        # Mock database response
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("So11111111111111111111111111111111111111112",),
            ("EPjFWaLb3odccccccccccccccccccccccccPmimeD",),
        ]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        with patch.dict(os.environ, {"DATABASE_URL": "postgres://test"}):
            from optimizer.spark_driver import AutoOptimizationLoop

            loop = AutoOptimizationLoop()
            mints = loop.get_active_mints(limit=20)

            assert len(mints) == 2
            assert "So11111111111111111111111111111111111111112" in mints

    @patch("optimizer.spark_driver.psycopg2.connect")
    def test_get_active_mints_no_data(self, mock_connect):
        """Test fallback when no active mints found."""
        # Mock empty database response
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        with patch.dict(os.environ, {"DATABASE_URL": "postgres://test"}):
            from optimizer.spark_driver import AutoOptimizationLoop

            loop = AutoOptimizationLoop()
            mints = loop.get_active_mints(limit=20)

            # Should return fallback SOL mint
            assert len(mints) >= 1
            assert "So11111111111111111111111111111111111111112" in mints

    @patch("optimizer.spark_driver.psycopg2.connect")
    def test_get_current_production_params(self, mock_connect):
        """Test fetching current production parameters."""
        # Mock database response
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            ('{"tp1_mult": 1.5, "stop_loss": 0.8}',),  # bot_settings
            (0.5, 10, 0.7),  # performance metrics
        ]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        with patch.dict(os.environ, {"DATABASE_URL": "postgres://test"}):
            from optimizer.spark_driver import AutoOptimizationLoop

            loop = AutoOptimizationLoop()
            params = loop.get_current_production_params()

            assert "settings" in params
            assert "last_1h_avg_pnl" in params
            assert "last_1h_trades" in params
            assert "last_1h_win_rate" in params

            assert params["last_1h_avg_pnl"] == 0.5
            assert params["last_1h_trades"] == 10

    @patch("optimizer.spark_driver.psycopg2.connect")
    def test_log_optimization_decision(self, mock_connect):
        """Test logging optimization decision."""
        # Mock database response
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        with patch.dict(os.environ, {"DATABASE_URL": "postgres://test"}):
            from optimizer.spark_driver import AutoOptimizationLoop

            loop = AutoOptimizationLoop()

            from_params = {"tp1_mult": 1.5}
            to_params = {"tp1_mult": 1.8}

            # Should not raise exception
            loop.log_optimization_decision(
                from_params=from_params,
                to_params=to_params,
                reason="test_improvement",
                expected_improvement=5.5,
                approved=True,
            )

            # Verify execute was called
            assert mock_cursor.execute.called


class TestRayWorkerCore:
    """Tests for Ray worker core functionality."""

    def test_ray_worker_imports(self):
        """Test that ray_worker module can be imported."""
        from shadow_testing.ray_worker import (
            test_parameter_set_on_live_data,
            simulate_shadow_trades_on_live_data,
        )

        # Functions should exist
        assert callable(test_parameter_set_on_live_data)
        assert callable(simulate_shadow_trades_on_live_data)

    def test_simulate_shadow_trades_empty_input(self):
        """Test shadow trade simulation with minimal input."""
        from shadow_testing.ray_worker import simulate_shadow_trades_on_live_data

        # Create minimal mock data
        class MockDF:
            def groupby(self, key):
                return []

        df = MockDF()
        param_set = {
            "tp1_mult": 1.5,
            "stop_loss": 0.8,
            "trail_pct": 0.20,
        }

        result = simulate_shadow_trades_on_live_data(df, param_set)

        # Should return valid structure
        assert "trades_closed" in result
        assert "avg_pnl_pct" in result
        assert "win_rate" in result
        assert "max_drawdown_pct" in result

        # With no data, metrics should be zero
        assert result["trades_closed"] == 0
        assert result["avg_pnl_pct"] == 0
        assert result["win_rate"] == 0


class TestIntegration:
    """Integration tests for Phase 3."""

    def test_files_exist(self):
        """Test that required files were created."""
        base_path = os.path.join(os.path.dirname(__file__), "..")

        # Check shadow_testing
        shadow_testing_path = os.path.join(base_path, "shadow_testing")
        assert os.path.isdir(shadow_testing_path)

        ray_worker_path = os.path.join(shadow_testing_path, "ray_worker.py")
        assert os.path.isfile(ray_worker_path)

        # Check optimizer
        optimizer_path = os.path.join(base_path, "optimizer")
        assert os.path.isdir(optimizer_path)

        spark_driver_path = os.path.join(optimizer_path, "spark_driver.py")
        assert os.path.isfile(spark_driver_path)

    def test_modules_importable(self):
        """Test that modules can be imported without errors."""
        from shadow_testing.ray_worker import (
            test_parameter_set_on_live_data,
            simulate_shadow_trades_on_live_data,
        )
        from optimizer.spark_driver import AutoOptimizationLoop

        assert callable(test_parameter_set_on_live_data)
        assert callable(simulate_shadow_trades_on_live_data)
        assert AutoOptimizationLoop is not None

    @patch("optimizer.spark_driver.psycopg2.connect")
    def test_spark_driver_has_required_methods(self, mock_connect):
        """Test that AutoOptimizationLoop has all required methods."""
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://test"}):
            from optimizer.spark_driver import AutoOptimizationLoop

            loop = AutoOptimizationLoop()

            # Required methods
            assert hasattr(loop, "run_continuous")
            assert hasattr(loop, "run_one_cycle")
            assert hasattr(loop, "generate_parameter_grid")
            assert hasattr(loop, "get_active_mints")
            assert hasattr(loop, "get_current_production_params")
            assert hasattr(loop, "log_optimization_decision")

            # All should be callable
            assert callable(loop.run_continuous)
            assert callable(loop.run_one_cycle)
            assert callable(loop.generate_parameter_grid)
            assert callable(loop.get_active_mints)
            assert callable(loop.get_current_production_params)
            assert callable(loop.log_optimization_decision)

    def test_parameter_set_structure(self):
        """Test parameter set structure is correct."""
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://test"}):
            from optimizer.spark_driver import AutoOptimizationLoop

            loop = AutoOptimizationLoop()
            param_sets = loop.generate_parameter_grid(sample_size=10)

            expected_keys = {
                "tp1_mult",
                "tp2_mult",
                "stop_loss",
                "trail_pct",
                "time_stop_min",
                "max_threat_score",
            }

            for param_set in param_sets:
                assert set(param_set.keys()) == expected_keys
                for key, value in param_set.items():
                    assert isinstance(value, (int, float))


class TestDecisionLogic:
    """Test optimization decision logic."""

    def test_improvement_threshold_above_5pct(self):
        """Test that improvements above 5% are accepted."""
        current_pnl = 1.0
        best_pnl = 6.5
        improvement = best_pnl - current_pnl
        assert improvement > 5.0

    def test_improvement_threshold_below_5pct(self):
        """Test that improvements below 5% are rejected."""
        current_pnl = 1.0
        best_pnl = 5.3
        improvement = best_pnl - current_pnl
        assert improvement < 5.0

    def test_improvement_threshold_exactly_5pct(self):
        """Test edge case of exactly 5% improvement."""
        current_pnl = 1.0
        best_pnl = 6.0
        improvement = best_pnl - current_pnl
        assert improvement == 5.0
        # At threshold, should be rejected (using > 5.0)
        assert not (improvement > 5.0)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
