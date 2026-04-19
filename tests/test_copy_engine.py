import pytest
from copy_engine import (
    parse_swap_from_tx,
    calc_proportional_size,
    CopyMode,
    SwapInfo,
)

def test_parse_swap_returns_none_on_empty_tx():
    result = parse_swap_from_tx({}, "ADDR1")
    assert result is None

def test_parse_swap_detects_token_change():
    tx = {
        "meta": {
            "preTokenBalances":  [{"accountIndex": 0, "mint": "TOKEN1", "uiTokenAmount": {"uiAmount": "0"}}],
            "postTokenBalances": [{"accountIndex": 0, "mint": "TOKEN1", "uiTokenAmount": {"uiAmount": "100"}}],
            "preBalances":  [1000000000],
            "postBalances": [900000000],
            "err": None,
        },
        "transaction": {
            "message": {
                "accountKeys": [{"pubkey": "ADDR1"}]
            },
            "signatures": ["sig123"]
        }
    }
    result = parse_swap_from_tx(tx, "ADDR1")
    assert result is not None
    assert result.token_mint == "TOKEN1"
    assert result.action == "BUY"

def test_calc_proportional_size_basic():
    size = calc_proportional_size(
        their_trade_sol=0.5,
        their_balance_sol=10.0,
        our_balance_sol=2.0,
        max_pct=0.10,
    )
    assert abs(size - 0.1) < 0.001

def test_calc_proportional_size_caps_at_max_pct():
    size = calc_proportional_size(
        their_trade_sol=5.0,
        their_balance_sol=10.0,
        our_balance_sol=2.0,
        max_pct=0.10,
    )
    assert size == pytest.approx(0.2, abs=0.001)

def test_copy_mode_enum():
    assert CopyMode.SHADOW.value == "shadow"
    assert CopyMode.LIVE.value   == "live"
    assert CopyMode.OFF.value    == "off"
