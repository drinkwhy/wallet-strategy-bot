# Shadow Trading Phase 1: Zero-Movement Detection & Separate Tracking
**Date:** 2026-04-02  
**Status:** Design (awaiting implementation)  
**Phase:** 1 of 4 (Realistic Auto-Optimization)

## Executive Summary

Implement zero-movement detection in shadow trading to identify and close "stuck" positions that waste trading capital. These positions will be tracked separately so they don't pollute divergence metrics used for validation, but remain visible for analysis.

**Goal:** Make shadow trading realistic by preventing positions from sitting indefinitely while offering clear visibility into wasted capital.

**Success Criteria:**
- Positions stuck for 5+ minutes close automatically
- Zero-movement closes excluded from divergence calculations
- Separate analytics dashboard shows stuck position patterns
- No impact to Phase 2 validation workflow

---

## Problem Statement

Currently, shadow positions can sit indefinitely if price stays flat. This is unrealistic because:
1. Real traders close positions that aren't moving (capital opportunity cost)
2. Zero-movement positions artificially inflate backtest vs shadow divergence
3. Optimizer can't learn to avoid low-momentum tokens if they're not flagged

Example: Token enters at $0.001, immediately pumps to $0.0011 (+10%), then flatlines for hours. Shadow trade sits open. Real trader closes it.

---

## Solution: Phase 1 Architecture

### 1. Zero-Movement Detection Logic

**Mechanism:** Track if position stays within micro-movement band for time threshold.

```
if (current_price >= entry_price * 0.99) AND (current_price <= entry_price * 1.01):
    time_in_band_sec += elapsed_seconds
    if time_in_band_sec >= 300:  // 5 minutes
        close_position(reason="zero_movement_stuck")
else:
    time_in_band_sec = 0  // reset if price breaks band
```

**Parameters:**
- `movement_band_pct`: ±1% of entry price (configurable, default 1%)
- `time_threshold_sec`: 300 seconds = 5 minutes (configurable)
- Applied in `shadow_position_update()` function in quant_platform.py

**Behavior:**
- Band is relative to entry price (e.g., if entry=$0.001, band is $0.00099-$0.00101)
- Timer resets if price breaks band
- Once time_in_band_sec >= threshold, position closes immediately
- Exit reason logged as "zero_movement_stuck"

### 2. Database Schema Updates

**Add to `shadow_positions` table:**
```sql
ALTER TABLE shadow_positions ADD COLUMN IF NOT EXISTS time_in_band_sec INTEGER DEFAULT 0;
```

**Create new table `shadow_zero_movement_closes`:**
```sql
CREATE TABLE shadow_zero_movement_closes (
    id SERIAL PRIMARY KEY,
    mint TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    opened_at TIMESTAMP NOT NULL,
    closed_at TIMESTAMP NOT NULL,
    entry_price REAL NOT NULL,
    peak_price REAL NOT NULL,
    close_price REAL NOT NULL,
    time_stuck_sec INTEGER NOT NULL,
    realized_pnl_pct REAL,
    entry_vol_usd REAL,
    peak_drawdown_pct REAL,
    reason TEXT DEFAULT 'zero_movement_stuck'
);
CREATE INDEX idx_zero_movement_closes_mint ON shadow_zero_movement_closes(mint);
CREATE INDEX idx_zero_movement_closes_strategy ON shadow_zero_movement_closes(strategy_name);
CREATE INDEX idx_zero_movement_closes_closed_at ON shadow_zero_movement_closes(closed_at DESC);
```

### 3. Divergence Metrics Integration

**Current behavior (unchanged):**
- Query: `SELECT realized_pnl_pct FROM shadow_positions WHERE status='closed' AND closed_at >= NOW() - INTERVAL '7 days'`
- Calculates: shadow_win_rate, shadow_avg_pnl_pct, divergence vs backtest

**New behavior:**
- Modify query to exclude zero-movement closes:
```sql
SELECT realized_pnl_pct FROM shadow_positions 
WHERE status='closed' 
  AND closed_at >= NOW() - INTERVAL '7 days'
  AND exit_reason != 'zero_movement_stuck'
```
- Kept separate: zero-movement closes don't affect divergence calculations
- This keeps validation metrics "pure" (focused on real trading performance)

### 4. Admin Dashboard Updates

**New widget: "Stuck Position Analysis" (Phase 1 tab)**

```
┌─────────────────────────────────────────┐
│ 🧪 Phase 1: Shadow Validation (FREE)    │
├─────────────────────────────────────────┤
│                                         │
│ Stuck Position Analysis (Last 7 days)   │
│ ────────────────────────────────────────│
│ Total stuck: 12                         │
│ % of all trades: 8.5%                   │
│ Avg time stuck: 4.2 minutes             │
│ Avg entry→close PnL: +0.3%              │
│ Most common coin: BONK, RAY, JTO        │
│                                         │
│ [Export] [Clear] [Auto-close disabled]  │
│                                         │
└─────────────────────────────────────────┘
```

**Metrics displayed:**
- `total_stuck`: Count of zero-movement closes
- `pct_of_trades`: Percentage of all shadow trades that got stuck
- `avg_time_stuck_sec`: Average time in band before closing
- `avg_entry_to_close_pnl`: Average realized PnL for stuck positions
- `top_stuck_tokens`: 5 most frequently stuck tokens (mint, count, avg_time_stuck)

**Data refreshes:** Every 15 seconds (aligned with existing divergence refresh)

### 5. Code Changes

**File: `quant_platform.py`**
- Modify `shadow_position_update()` to check time_in_band_sec
- If position is zero-movement, set `status="closed"` and `exit_reason="zero_movement_stuck"`

**File: `app.py`**
- Modify divergence query to exclude zero-movement closes
- Add new admin endpoint: `/api/admin/stuck-analysis` (returns metrics above)
- Update JavaScript on admin panel to fetch and display stuck analysis
- Add migration: create shadow_zero_movement_closes table

**File: `nixpacks.toml`**
- No changes (migrations run at startup via `run_startup_migrations()`)

### 6. Data Flow

```
Shadow Position Update (every 30s)
  ├─ Check: is current_price in band? (±1% of entry)
  │   ├─ YES: increment time_in_band_sec
  │   │        if time_in_band_sec >= 300: close position
  │   └─ NO: reset time_in_band_sec = 0
  │
  ├─ On close: write to shadow_positions
  │            if exit_reason='zero_movement_stuck': 
  │              also write to shadow_zero_movement_closes
  │
  └─ Admin queries
      ├─ divergence: exclude zero_movement_stuck from PnL calculations
      └─ stuck analysis: query shadow_zero_movement_closes for metrics
```

### 7. Testing Strategy

**Unit Tests:**
- Test `time_in_band_sec` increments when price stays flat
- Test `time_in_band_sec` resets when price breaks band
- Test close triggers after time_threshold
- Test both tables are written on zero-movement close

**Integration Tests:**
- Run shadow trading for 30 minutes with mock prices
- Verify stuck positions close correctly
- Verify divergence calculation excludes stuck positions
- Verify admin dashboard shows accurate metrics

**Manual Testing:**
- Create shadow trade manually
- Hold price flat for 5 minutes
- Confirm position closes with exit_reason="zero_movement_stuck"
- Confirm appears in stuck analysis dashboard

---

## Implementation Notes

### Configurable Parameters

These should be environment variables or admin-editable settings:
- `SHADOW_MOVEMENT_BAND_PCT`: ±% for stuck detection (default: 1.0)
- `SHADOW_TIME_THRESHOLD_SEC`: Minutes to wait before closing (default: 300)

### Performance Considerations

- `time_in_band_sec` is a lightweight integer counter (no DB queries)
- Check happens in existing `shadow_position_update()` loop (no extra overhead)
- Stuck analysis query is indexed on (strategy_name, closed_at DESC)
- Minimal impact on Railway resources

### Backward Compatibility

- Existing shadow positions continue normally
- Only NEW positions after deployment track time_in_band_sec
- Divergence calculation automatically excludes zero-movement closes
- No breaking changes to API or data structures

---

## Success Metrics

**Post-deployment (after 7 days):**
- [ ] Stuck analysis shows X% of trades getting stuck (baseline established)
- [ ] Divergence metrics are 2-5% different (excluding stuck trades cleans up noise)
- [ ] No performance degradation on Railway
- [ ] Admin dashboard loads stuck analysis in <500ms

**Validation insight:**
- If X% of shadow trades get stuck but backtest predicts 0% stuck: optimizer is unrealistic
- Phase 2 (slippage friction) will reveal if stuck trades are due to low liquidity or low momentum

---

## Deployment Plan

1. Create schema migration (shadow_zero_movement_closes table)
2. Update quant_platform.py with zero-movement detection
3. Update app.py divergence query + admin endpoint
4. Update admin dashboard UI with stuck analysis widget
5. Deploy to Railway
6. Monitor admin panel for 24 hours
7. Collect baseline metrics (% stuck, avg time, top tokens)

**Rollback:** Revert quant_platform.py changes; stuck analysis will show 0 data but won't break anything.

---

## Questions for Implementation

- Should `movement_band_pct` be hardcoded or admin-editable?
- Should zero-movement closes trigger a webhook alert (for debugging)?
- Should we log why specific tokens get stuck (low vol, low liquidity, whale dump)?

