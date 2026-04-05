-- TimescaleDB Query Examples
-- These queries help you monitor and debug the tick_prices hypertable

-- Count total records
SELECT COUNT(*) as total_records FROM tick_prices;

-- Count records by mint (token)
SELECT mint, COUNT(*) as record_count
FROM tick_prices
GROUP BY mint
ORDER BY record_count DESC
LIMIT 20;

-- Get latest price for each mint
SELECT DISTINCT ON (mint) mint, time, price
FROM tick_prices
ORDER BY mint, time DESC;

-- Get price time series for a specific mint (last 100 records)
SELECT time, price, bid, ask, spread_bps, volume_1m
FROM tick_prices
WHERE mint = 'YOUR_MINT_HERE'
ORDER BY time DESC
LIMIT 100;

-- Get average price over 1-hour buckets (last 24 hours)
SELECT
  time_bucket('1 hour', time) as hour,
  mint,
  AVG(price) as avg_price,
  MIN(price) as min_price,
  MAX(price) as max_price,
  COUNT(*) as tick_count
FROM tick_prices
WHERE time > NOW() - INTERVAL '24 hours'
GROUP BY hour, mint
ORDER BY hour DESC, mint
LIMIT 100;

-- Hypertable compression statistics
SELECT
  hypertable_name,
  total_size,
  total_size_pretty,
  total_bytes_compressed
FROM hypertable_detailed_size('tick_prices');

-- Check hypertable metadata
SELECT * FROM timescaledb_information.hypertables;

-- List chunks (data partitions)
SELECT * FROM timescaledb_information.chunks
WHERE hypertable_name = 'tick_prices'
ORDER BY range_start DESC
LIMIT 10;

-- View chunk size information
SELECT
  chunk_schema,
  chunk_name,
  ranges
FROM timescaledb_information.chunks
WHERE hypertable_name = 'tick_prices'
ORDER BY range_start DESC
LIMIT 5;

-- Find mints with data written in the last hour
SELECT DISTINCT mint
FROM tick_prices
WHERE time > NOW() - INTERVAL '1 hour'
ORDER BY mint;

-- Check data freshness (most recent timestamp in table)
SELECT
  MAX(time) as latest_timestamp,
  NOW() as current_time,
  NOW() - MAX(time) as age_of_latest_data
FROM tick_prices;

-- Get write rate (records per minute)
SELECT
  DATE_TRUNC('minute', time) as minute,
  COUNT(*) as records_per_minute
FROM tick_prices
WHERE time > NOW() - INTERVAL '1 hour'
GROUP BY DATE_TRUNC('minute', time)
ORDER BY minute DESC;

-- Check for any gaps in data (more than 5 minutes without updates)
WITH time_gaps AS (
  SELECT
    mint,
    time,
    LAG(time) OVER (PARTITION BY mint ORDER BY time) as prev_time,
    EXTRACT(EPOCH FROM (time - LAG(time) OVER (PARTITION BY mint ORDER BY time))) as gap_seconds
  FROM tick_prices
  WHERE time > NOW() - INTERVAL '24 hours'
)
SELECT
  mint,
  time,
  prev_time,
  gap_seconds / 60 as gap_minutes
FROM time_gaps
WHERE gap_seconds > 300  -- 5 minutes
ORDER BY gap_seconds DESC
LIMIT 20;

-- Analyze table size and compression
SELECT
  TABLE_NAME,
  PG_SIZE_PRETTY(pg_total_relation_size(CONCAT(table_schema, '.', table_name))) as total_size,
  PG_SIZE_PRETTY(pg_relation_size(CONCAT(table_schema, '.', table_name))) as table_size,
  PG_SIZE_PRETTY(pg_total_relation_size(CONCAT(table_schema, '.', table_name)) - pg_relation_size(CONCAT(table_schema, '.', table_name))) as indexes_size
FROM information_schema.TABLES
WHERE table_name = 'tick_prices' AND table_schema = 'public';
