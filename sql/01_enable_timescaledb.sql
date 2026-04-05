-- Enable TimescaleDB Extension on Railway PostgreSQL
-- Run this first to enable the TimescaleDB functionality

-- Create the timescaledb extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Verify it's installed
SELECT * FROM pg_extension WHERE extname = 'timescaledb';

-- You should see one row with timescaledb extension
