-- Create tick_prices Hypertable for TimescaleDB
-- Run this after enabling TimescaleDB extension

-- Create the tick_prices table with proper schema
CREATE TABLE IF NOT EXISTS tick_prices (
  time TIMESTAMP WITH TIME ZONE NOT NULL,
  mint TEXT NOT NULL,
  price REAL NOT NULL,
  bid REAL,
  ask REAL,
  volume_1m REAL,
  volume_5m REAL,
  spread_bps REAL,
  liquidity_usd REAL
);

-- Convert to TimescaleDB hypertable (time-series optimized)
SELECT create_hypertable('tick_prices', 'time', if_not_exists => TRUE);

-- Create indexes for common queries
CREATE INDEX IF NOT EXISTS tick_prices_mint_time_idx
  ON tick_prices (mint, time DESC);

CREATE INDEX IF NOT EXISTS tick_prices_time_idx
  ON tick_prices (time DESC);

-- Create a partial index for recent data (last 24 hours)
CREATE INDEX IF NOT EXISTS tick_prices_recent_idx
  ON tick_prices (mint, time DESC)
  WHERE time > NOW() - INTERVAL '24 hours';

-- Verify table and indexes
\dt tick_prices
\di tick_prices*

-- Test insert
INSERT INTO tick_prices (time, mint, price, bid, ask, volume_1m, volume_5m, spread_bps, liquidity_usd)
VALUES (NOW(), 'TEST', 100.0, 99.5, 100.5, 1000, 5000, 5.0, 50000);

-- Verify test data
SELECT * FROM tick_prices WHERE mint = 'TEST';

-- Clean up test data
DELETE FROM tick_prices WHERE mint = 'TEST';
