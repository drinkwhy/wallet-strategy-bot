"""Tests for Phase 3: Ray Shadow Testing + Spark Auto-Optimization Loop."""

import pytest
import json
import os
import pandas as pd
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

# Add parent directory to path
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shadow_testing.ray_worker import (
    test_parameter_set_on_live_data,
    simulate_shadow_trades_on_live_data,
)
from optimizer.spark_driver import AutoOptimizationLoop


class TestRayWorker:
    """Tests for Ray worker functionality."""

    def test_simulate_shadow_trades_basic(self):
        """Test basic shadow trade simulation."""
        # Create sample price data
        df = pd.DataFrame(
            {
                "mint": ["SOL"] * 10,
                "time": pd.date_range(start="2026-04-01", periods=10, freq="1min"),
                "price": [100, 105, 110, 115, 120, 125, 130, 125, 120, 115],
                "bid": [99, 104, 109, 114, 119, 124, 129, 124, 119, 114],
                "ask": [101, 106, 111, 116, 121, 126, 131, 126, 121, 116],
                "volume_1m": [1000] * 10,
                "liquidity_usd": [50000] * 10,
            }
        )

        param_set = {
            "tp1_mult": 1.5,
            "tp2_mult": 2.0,
            "stop_loss": 0.8,
            "trail_pct": 0.20,
            "time_stop_min": 30,
            "max_threat_score": 50,
        }

        result = simulate_shadow_trades_on_live_data(df, param_set)

        # Should have recorded metrics
        assert "trades_closed" in result
        assert "avg_pnl_pct" in result
        assert "win_rate" in result
        assert "max_drawdown_pct" in result

        # With prices going from 100 to 150, should hit TP
        assert result["trades_closed"] > 0
        assert result["avg_pnl_pct"] > 0

    def test_simulate_shadow_trades_no_data(self):
        """Test with empty price data."""
        df = pd.DataFrame(
            {
                "mint": [],
                "time": [],
                "price": [],
                "bid": [],
                "ask": [],
                "volume_1m": [],
                "liquidity_usd": [],
            }
        )

        param_set = {
            "tp1_mult": 1.5,
            "stop_loss": 0.8,
            "trail_pct": 0.20,
        }

        result = simulate_shadow_trades_on_live_data(df, param_set)

        assert result["trades_closed"] == 0
        assert result["avg_pnl_pct"] == 0
        assert result["win_rate"] == 0

    def test_simulate_shadow_trades_stop_loss(self):
        """Test stop loss triggering."""
        df = pd.DataFrame(
            {
                "mint": ["SOL"] * 5,
                "time": pd.date_range(start="2026-04-01", periods=5, freq="1min"),
                "price": [100, 95, 90, 85, 80],
                "bid": [99, 94, 89, 84, 79],
                "ask": [101, 96, 91, 86, 81],
                "volume_1m": [1000] * 5,
                "liquidity_usd": [50000] * 5,
            }
        )

        param_set = {
            "tp1_mult": 1.5,
            "stop_loss": 0.85,
            "trail_pct": 0.20,
        }

        result = simulate_shadow_trades_on_live_data(df, param_set)

        # Should have closed at least one trade via stop loss
        assert result["trades_closed"] >= 1
        # P&L should be negative
        assert result["avg_pnl_pct"] <= 0

    def test_simulate_shadow_trades_multiple_mints(self):
        """Test with multiple mints."""
        df = pd.DataFrame(
            {
                "mint": ["SOL", "SOL", "ETH", "ETH", "BTC", "BTC"],
                "time": [
                    datetime(2026, 4, 1, 0, 0, 0),
                    datetime(2026, 4, 1, 0, 1, 0),
                    datetime(2026, 4, 1, 0, 0, 0),
                    datetime(2026, 4, 1, 0, 1, 0),
                    datetime(2026, 4, 1, 0, 0, 0),
                    datetime(2026, 4, 1, 0, 1, 0),
                ],
                "price": [100, 150, 2000, 3000, 40000, 60000],
                "bid": [99, 149, 1999, 2999, 39999, 59999],
                "ask": [101, 151, 2001, 3001, 40001, 60001],
                "volume_1m": [1000] * 6,
                "liquidity_usd": [50000] * 6,
            }
        )

        param_set = {
            "tp1_mult": 1.5,
            "stop_loss": 0.8,
            "trail_pct": 0.20,
        }

        result = simulate_shadow_trades_on_live_data(df, param_set)

        # Should have recorded metrics
        assert result["trades_closed"] > 0


class TestSparkDriver:
    """Tests for Spark driver functionality."""

    def test_parameter_grid_generation(self):
        """Test parameter grid generation."""
        loop = AutoOptimizationLoop(optimization_interval_minutes=5)

        param_sets = loop.generate_parameter_grid(sample_size=100)

        # Should generate requested number of combinations
        assert len(param_sets) <= 100
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

    def test_parameter_grid_default_size(self):
        """Test that default parameter grid is appropriate size."""
        loop = AutoOptimizationLoop(optimization_interval_minutes=5)

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

        loop = AutoOptimizationLoop(optimization_interval_minutes=5)
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

        loop = AutoOptimizationLoop(optimization_interval_minutes=5)
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

        loop = AutoOptimizationLoop(optimization_interval_minutes=5)
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

        loop = AutoOptimizationLoop(optimization_interval_minutes=5)

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


class TestIntegration:
    """Integration tests."""

    def test_parameter_set_structure(self):
        """Verify parameter sets have correct structure."""
        loop = AutoOptimizationLoop(optimization_interval_minutes=5)
        param_sets = loop.generate_parameter_grid(sample_size=10)

        for param_set in param_sets:
            # All values should be numeric
            for key, value in param_set.items():
                assert isinstance(value, (int, float)), f"{key} has non-numeric value"

    def test_metrics_calculation_consistency(self):
        """Test that metrics calculation is consistent."""
        df = pd.DataFrame(
            {
                "mint": ["SOL"] * 5,
                "time": pd.date_range(start="2026-04-01", periods=5, freq="1min"),
                "price": [100, 110, 120, 130, 140],
                "bid": [99, 109, 119, 129, 139],
                "ask": [101, 111, 121, 131, 141],
                "volume_1m": [1000] * 5,
                "liquidity_usd": [50000] * 5,
            }
        )

        param_set = {
            "tp1_mult": 1.2,
            "stop_loss": 0.8,
            "trail_pct": 0.20,
        }

        result1 = simulate_shadow_trades_on_live_data(df, param_set)
        result2 = simulate_shadow_trades_on_live_data(df, param_set)

        # Results should be identical for same inputs
        assert result1["trades_closed"] == result2["trades_closed"]
        assert result1["avg_pnl_pct"] == result2["avg_pnl_pct"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
