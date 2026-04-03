import pytest
from quant_platform import shadow_position_update, _safe_float, _safe_int

class TestZeroMovementDetection:
    """Test zero-movement detection logic in shadow_position_update()"""

    def test_price_in_band_increments_counter(self):
        """When price stays in ±1% band, time_in_band_sec increments"""
        position = {
            "entry_price": 1.0,
            "peak_price": 1.0,
            "trough_price": 1.0,
            "time_in_band_sec": 5  # already 5 seconds in band
        }
        settings = {
            "movement_band_pct": 1.0,
            "time_threshold_sec": 300,
            "tp1_mult": 2.0,
            "tp2_mult": 2.0,
            "stop_loss": 0.7,
            "trail_pct": 0.20,
            "peak_plateau_mode": False
        }
        current_price = 1.005  # within ±1% band (0.99 to 1.01)
        age_min = 5

        result = shadow_position_update(position, current_price, settings, age_min)

        assert result["time_in_band_sec"] == 6, "Counter should increment by 1"
        assert result["status"] == "open", "Should stay open (not at threshold yet)"
        assert result["exit_reason"] == "", "No exit reason yet"

    def test_price_breaks_band_resets_counter(self):
        """When price leaves band, time_in_band_sec resets to 0"""
        position = {
            "entry_price": 1.0,
            "peak_price": 1.05,
            "trough_price": 1.0,
            "time_in_band_sec": 50  # was stuck for 50 seconds
        }
        settings = {
            "movement_band_pct": 1.0,
            "time_threshold_sec": 300,
            "tp1_mult": 2.0,
            "tp2_mult": 2.0,
            "stop_loss": 0.7,
            "trail_pct": 0.20,
            "peak_plateau_mode": False
        }
        current_price = 1.02  # OUTSIDE band (band is 0.99-1.01)
        age_min = 5

        result = shadow_position_update(position, current_price, settings, age_min)

        assert result["time_in_band_sec"] == 0, "Counter should reset to 0"
        assert result["status"] == "open", "Should stay open (price moved)"

    def test_closes_after_time_threshold(self):
        """Position closes with exit_reason='zero_movement_stuck' after 5 min in band"""
        position = {
            "entry_price": 1.0,
            "peak_price": 1.0,
            "trough_price": 1.0,
            "time_in_band_sec": 299  # 299 seconds in band
        }
        settings = {
            "movement_band_pct": 1.0,
            "time_threshold_sec": 300,  # threshold is 300 seconds (5 min)
            "tp1_mult": 2.0,
            "tp2_mult": 2.0,
            "stop_loss": 0.7,
            "trail_pct": 0.20,
            "peak_plateau_mode": False
        }
        current_price = 1.0  # still in band
        age_min = 5

        result = shadow_position_update(position, current_price, settings, age_min)

        assert result["status"] == "closed", "Should close at threshold"
        assert result["exit_reason"] == "zero_movement_stuck", "Exit reason should be zero_movement_stuck"
        assert result["time_in_band_sec"] == 300, "Counter should be at threshold"

    def test_configurable_band_percentage(self):
        """Band threshold is configurable via movement_band_pct"""
        position = {
            "entry_price": 1.0,
            "peak_price": 1.0,
            "trough_price": 1.0,
            "time_in_band_sec": 50
        }
        settings = {
            "movement_band_pct": 2.0,  # ±2% band instead of ±1%
            "time_threshold_sec": 300,
            "tp1_mult": 2.0,
            "tp2_mult": 2.0,
            "stop_loss": 0.7,
            "trail_pct": 0.20,
            "peak_plateau_mode": False
        }
        current_price = 1.015  # Within ±2% band (0.98 to 1.02), but outside ±1%
        age_min = 5

        result = shadow_position_update(position, current_price, settings, age_min)

        assert result["time_in_band_sec"] == 51, "Counter should increment with ±2% band"
        assert result["status"] == "open", "Should stay open"

    def test_configurable_time_threshold(self):
        """Time threshold is configurable via time_threshold_sec"""
        position = {
            "entry_price": 1.0,
            "peak_price": 1.0,
            "trough_price": 1.0,
            "time_in_band_sec": 119  # 119 seconds in band
        }
        settings = {
            "movement_band_pct": 1.0,
            "time_threshold_sec": 120,  # Only 2 minutes instead of 5
            "tp1_mult": 2.0,
            "tp2_mult": 2.0,
            "stop_loss": 0.7,
            "trail_pct": 0.20,
            "peak_plateau_mode": False
        }
        current_price = 1.0  # still in band
        age_min = 5

        result = shadow_position_update(position, current_price, settings, age_min)

        assert result["status"] == "closed", "Should close at custom threshold"
        assert result["exit_reason"] == "zero_movement_stuck"
