import pytest
from quant_platform import shadow_position_update, _safe_float, _safe_int


@pytest.fixture
def base_settings():
    """Standard settings dict for tests (override specific fields as needed).

    Used by all tests in TestZeroMovementDetection to reduce DRY violations.
    Each test can call base_settings() and override only the fields it cares about.
    """
    return {
        "movement_band_pct": 1.0,
        "time_threshold_sec": 300,
        "tp1_mult": 2.0,
        "tp2_mult": 2.0,
        "stop_loss": 0.7,
        "trail_pct": 0.20,
        "peak_plateau_mode": False
    }


@pytest.fixture
def base_position():
    """Standard position dict for tests (override specific fields as needed).

    Used by all tests in TestZeroMovementDetection to reduce DRY violations.
    Each test can call base_position() and override only the fields it cares about.
    """
    return {
        "entry_price": 1.0,
        "peak_price": 1.0,
        "trough_price": 1.0,
        "time_in_band_sec": 0
    }


class TestZeroMovementDetection:
    """Test zero-movement detection logic in shadow_position_update()

    Band calculation: entry_price ± (movement_band_pct / 100)
    Example: if entry_price=1.0 and movement_band_pct=1.0, band is [0.99, 1.01]

    Counter behavior:
    - Increments by 1 per call when price is within band
    - Resets to 0 when price breaks band
    - Triggers close when counter >= time_threshold_sec

    Default test values:
    - Base price: 1.0
    - Band width: ±1.0% (0.99 to 1.01)
    - Time threshold: 300 seconds (5 minutes)
    """

    def test_price_in_band_increments_counter(self, base_settings, base_position):
        """When price stays in ±1% band, time_in_band_sec increments"""
        position = base_position.copy()
        position["time_in_band_sec"] = 5  # already 5 seconds in band
        settings = base_settings
        current_price = 1.005  # within ±1% band (0.99 to 1.01)
        age_min = 5

        result = shadow_position_update(position, current_price, settings, age_min)

        assert result["time_in_band_sec"] == 6, "Counter should increment by 1"
        assert result["status"] == "open", "Should stay open (not at threshold yet)"
        assert result["exit_reason"] == "", "No exit reason yet"

    def test_price_breaks_band_resets_counter(self, base_settings, base_position):
        """When price leaves band, time_in_band_sec resets to 0"""
        position = base_position.copy()
        position["peak_price"] = 1.05
        position["time_in_band_sec"] = 50  # was stuck for 50 seconds
        settings = base_settings
        current_price = 1.02  # OUTSIDE band (band is 0.99-1.01)
        age_min = 5

        result = shadow_position_update(position, current_price, settings, age_min)

        assert result["time_in_band_sec"] == 0, "Counter should reset to 0"
        assert result["status"] == "open", "Should stay open (price moved)"

    def test_closes_after_time_threshold(self, base_settings, base_position):
        """Position closes with exit_reason='zero_movement_stuck' after 5 min in band"""
        position = base_position.copy()
        position["time_in_band_sec"] = 299  # 299 seconds in band
        settings = base_settings
        current_price = 1.0  # still in band
        age_min = 5

        result = shadow_position_update(position, current_price, settings, age_min)

        assert result["status"] == "closed", "Should close at threshold"
        assert result["exit_reason"] == "zero_movement_stuck", "Exit reason should be zero_movement_stuck"
        assert result["time_in_band_sec"] == 300, "Counter should be at threshold"

    def test_configurable_band_percentage(self, base_settings, base_position):
        """Band threshold is configurable via movement_band_pct"""
        position = base_position.copy()
        position["time_in_band_sec"] = 50
        settings = base_settings.copy()
        settings["movement_band_pct"] = 2.0  # ±2% band instead of ±1%
        current_price = 1.015  # Within ±2% band (0.98 to 1.02), but outside ±1%
        age_min = 5

        result = shadow_position_update(position, current_price, settings, age_min)

        assert result["time_in_band_sec"] == 51, "Counter should increment with ±2% band"
        assert result["status"] == "open", "Should stay open"

    def test_configurable_time_threshold(self, base_settings, base_position):
        """Time threshold is configurable via time_threshold_sec"""
        position = base_position.copy()
        position["time_in_band_sec"] = 119  # 119 seconds in band
        settings = base_settings.copy()
        settings["time_threshold_sec"] = 120  # Only 2 minutes instead of 5
        current_price = 1.0  # still in band
        age_min = 5

        result = shadow_position_update(position, current_price, settings, age_min)

        assert result["status"] == "closed", "Should close at custom threshold"
        assert result["exit_reason"] == "zero_movement_stuck"

    def test_price_at_band_boundary(self, base_settings, base_position):
        """Verify counter increments at band boundaries"""
        position = base_position.copy()
        settings = base_settings

        # Test at lower boundary (0.99)
        result = shadow_position_update(position, 0.99, settings, age_min=5)
        assert result["time_in_band_sec"] == 1, "Counter should increment at lower band boundary"

        # Test at upper boundary (1.01)
        position2 = base_position.copy()
        result = shadow_position_update(position2, 1.01, settings, age_min=5)
        assert result["time_in_band_sec"] == 1, "Counter should increment at upper band boundary"

    def test_counter_increments_multiple_times(self, base_settings, base_position):
        """Verify counter accumulates correctly over multiple calls"""
        position = base_position.copy()
        settings = base_settings

        # Simulate 10 consecutive calls with price in band
        for i in range(10):
            result = shadow_position_update(position, 1.005, settings, age_min=5)
            position["time_in_band_sec"] = result["time_in_band_sec"]
            assert result["time_in_band_sec"] == i + 1, f"Counter should be {i + 1} after call {i + 1}"
            assert result["status"] == "open", "Should stay open during accumulation"

    def test_missing_settings_use_defaults(self, base_position):
        """Verify defaults are used when settings keys are missing"""
        position = base_position.copy()
        settings = {}  # No movement_band_pct or time_threshold_sec

        # With default settings, price at 1.005 should be in band (default is ±1%)
        result = shadow_position_update(position, 1.005, settings, age_min=5)
        assert result["time_in_band_sec"] == 1, "Counter should increment using default band"
        assert result["status"] == "open", "Should stay open with default settings"


class TestZeroMovementIntegration:
    """Integration test: simulate shadow position lifecycle with zero movement"""

    def test_shadow_position_stuck_then_closes(self, base_settings, base_position):
        """
        Simulate a real shadow position:
        1. Enters at $1.00
        2. Pumps to $1.10
        3. Drops to $1.005 and flatlines
        4. After 5 min, closes as stuck
        """
        settings = base_settings

        # Step 1: Position enters
        position = base_position.copy()

        # Step 2: Price pumps
        result = shadow_position_update(position, 1.10, settings, age_min=1)
        position.update(result)
        assert result["status"] == "open"
        assert result["time_in_band_sec"] == 0  # band reset

        # Step 3: Price drops to ~entry and flatlines
        position["peak_price"] = 1.10
        position["entry_price"] = 1.00

        # Simulate 5+ minutes in band (increments 1 sec per call)
        for i in range(301):  # 301 iterations = 301 seconds
            result = shadow_position_update(position, 1.005, settings, age_min=5)
            position.update(result)

            if i < 299:
                # Before threshold, should be open
                assert result["status"] == "open"
                assert result["time_in_band_sec"] == i + 1
            else:
                # After threshold (at i=299, counter=300), should close
                assert result["status"] == "closed"
                assert result["exit_reason"] == "zero_movement_stuck"
                break

        # Final state
        assert position["status"] == "closed"
        assert position["time_in_band_sec"] >= 300
