# Railway Kafka/Spark/TimescaleDB Deployment Setup Guide

This guide walks you through setting up all prerequisites for the Kafka/Spark/TimescaleDB rebuild on Railway.

## Overview

Your system requires these services:
- **PostgreSQL with TimescaleDB**: Time-series database for tick_prices
- **Kafka**: Message broker for market events and bot parameters
- **Redis**: Cache and session store for optimization state

## Step 1: Verify/Enable TimescaleDB Extension

### 1a. Find Your Database URL

1. Go to [Railway.app](https://railway.app)
2. Select your project
3. Click on the **PostgreSQL** service
4. Click the **"Connect"** tab
5. Copy the full connection string (looks like `postgresql://user:password@host:port/database`)

### 1b. Check if TimescaleDB is Enabled

Connect to your database and run:

```bash
psql "YOUR_DATABASE_URL" -c "SELECT * FROM pg_extension WHERE extname = 'timescaledb';"
```

If you see a result with timescaledb, skip to Step 2. If empty, continue below.

### 1c. Enable TimescaleDB Extension

**Via Railway Dashboard (Recommended):**
1. In your PostgreSQL service, click the "Query" button
2. Paste and execute:
```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;
```

**Or via psql terminal:**
```bash
psql "YOUR_DATABASE_URL" -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
```

Verify it worked:
```bash
psql "YOUR_DATABASE_URL" -c "SELECT * FROM pg_extension WHERE extname = 'timescaledb';"
```

You should see one row with timescaledb.

## Step 2: Create TimescaleDB Hypertable

Once TimescaleDB is enabled, create the tick_prices table:

```sql
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

SELECT create_hypertable('tick_prices', 'time', if_not_exists => TRUE);

CREATE INDEX ON tick_prices (mint, time DESC);
CREATE INDEX ON tick_prices (time DESC);
```

Execute this via Railway's Query tool or psql:

```bash
psql "YOUR_DATABASE_URL" << 'EOF'
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

SELECT create_hypertable('tick_prices', 'time', if_not_exists => TRUE);

CREATE INDEX ON tick_prices (mint, time DESC);
CREATE INDEX ON tick_prices (time DESC);
EOF
```

Verify the table exists:
```bash
psql "YOUR_DATABASE_URL" -c "\dt tick_prices"
```

## Step 3: Set Up Railway Kafka Service

### 3a. Create Kafka Service

1. In your Railway project, click **"+ New"** button
2. Select **"Database"** → **"Kafka"**
3. Wait for deployment (2-3 minutes)
4. Once deployed, click on the Kafka service
5. Go to the **"Connect"** tab
6. Copy the **"KAFKA_BOOTSTRAP_SERVERS"** value (looks like `host:9092`)

### 3b. Save Bootstrap Servers

Note this value - you'll need it for environment variables. Example:
```
kafka-xyz.railway.internal:9092
```

## Step 4: Set Up Railway Redis Service

### 4a. Create Redis Service

1. In your Railway project, click **"+ New"** button
2. Select **"Database"** → **"Redis"**
3. Wait for deployment (1-2 minutes)
4. Once deployed, click on the Redis service
5. Go to the **"Connect"** tab
6. Copy the **"REDIS_URL"** value (looks like `redis://...`)

### 4b. Save Redis Connection Details

Extract from the Redis URL:
- **REDIS_HOST**: The hostname part (between `://` and `:`)
- **REDIS_PORT**: Usually `6379`
- **REDIS_PASSWORD**: The password part (if present)
- **REDIS_DB**: `0` (default)

Example from `redis://:password@host.railway.internal:6379`:
```
REDIS_HOST=host.railway.internal
REDIS_PORT=6379
REDIS_PASSWORD=password
REDIS_DB=0
```

## Step 5: Create Kafka Topics

You need three topics for the system. Connect to Kafka and create them.

### Via Railway Kafka UI (Easiest)

If your Kafka service includes a UI, you can create topics there.

### Via Kafka CLI

```bash
# Export Kafka bootstrap servers
export KAFKA_BROKERS="your-bootstrap-servers:9092"

# Create market.events topic
kafka-topics --create \
  --bootstrap-server $KAFKA_BROKERS \
  --topic market.events \
  --partitions 3 \
  --replication-factor 1

# Create shadow.positions topic
kafka-topics --create \
  --bootstrap-server $KAFKA_BROKERS \
  --topic shadow.positions \
  --partitions 2 \
  --replication-factor 1

# Create bot.parameters topic
kafka-topics --create \
  --bootstrap-server $KAFKA_BROKERS \
  --topic bot.parameters \
  --partitions 1 \
  --replication-factor 1

# List topics to verify
kafka-topics --list --bootstrap-server $KAFKA_BROKERS
```

### Via Docker (If Kafka CLI not available)

```bash
docker run --rm \
  --network=host \
  confluentinc/cp-kafka:7.4.0 \
  kafka-topics --bootstrap-server localhost:9092 \
  --create --topic market.events \
  --partitions 3 --replication-factor 1
```

## Step 6: Configure Environment Variables

In your Railway project settings, add these variables:

```
# Database
DATABASE_URL=your-postgresql-url

# Kafka
KAFKA_BOOTSTRAP_SERVERS=your-kafka-bootstrap:9092

# Redis
REDIS_HOST=your-redis-host
REDIS_PORT=6379
REDIS_PASSWORD=your-redis-password
REDIS_DB=0

# Optimization Settings
AUTO_OPTIMIZE_THRESHOLD_PCT=5.0
AUTO_OPTIMIZE_INTERVAL_MIN=5
AUTO_OPTIMIZE_ENABLED=true
SHADOW_TESTING_WINDOW_MIN=60

# Spark Settings (if using Spark optimization)
SPARK_MASTER=local
SPARK_EXECUTOR_MEMORY=2g
SPARK_DRIVER_MEMORY=2g
```

### How to Set Environment Variables in Railway

1. Go to your project
2. Click on the service that will run your application
3. Click the **"Variables"** tab
4. Click **"+ New Variable"**
5. Enter key and value
6. Click **"Save"**

Repeat for all variables above.

## Step 7: Deploy the Kafka Consumer

Your application includes `kafka_to_timescale.py` which consumes market prices from Kafka and writes to TimescaleDB.

### 7a. Add Python Service to Railway

1. In your Railway project, click **"+ New"** → **"GitHub Repo"** (or upload your code)
2. Select the repository containing your code
3. Wait for deployment
4. In the service variables, add all the environment variables from Step 6

### 7b. Run the Kafka Consumer

Once deployed, you can run the consumer as a background service or one-off job:

```bash
python /path/to/connectors/kafka_to_timescale.py
```

The consumer will:
- Connect to Kafka using KAFKA_BOOTSTRAP_SERVERS
- Connect to TimescaleDB using DATABASE_URL
- Subscribe to the "market-prices" topic (or as configured)
- Batch insert price updates every 1000 messages
- Continue running indefinitely

## Step 8: Verification Checklist

Run these checks to verify everything is working:

### Check TimescaleDB

```bash
# Connect to your database
psql "YOUR_DATABASE_URL"

# Verify extension
SELECT * FROM pg_extension WHERE extname = 'timescaledb';

# Verify table exists
\dt tick_prices

# Verify indexes
\di

# Test insert
INSERT INTO tick_prices (time, mint, price, bid, ask)
VALUES (NOW(), 'TEST123', 100.0, 99.5, 100.5);

# Verify data
SELECT * FROM tick_prices LIMIT 1;
```

### Check Kafka

```bash
# Export connection details
export KAFKA_BROKERS="your-bootstrap-servers:9092"

# List topics
kafka-topics --list --bootstrap-server $KAFKA_BROKERS

# Verify topics exist
# You should see: market.events, shadow.positions, bot.parameters

# Test produce
echo "test" | kafka-console-producer \
  --broker-list $KAFKA_BROKERS \
  --topic market.events
```

### Check Redis

```bash
# Test connection
redis-cli -h REDIS_HOST -p REDIS_PORT -a REDIS_PASSWORD PING
# Should return: PONG

# Test set/get
redis-cli -h REDIS_HOST -p REDIS_PORT -a REDIS_PASSWORD SET test_key "test_value"
redis-cli -h REDIS_HOST -p REDIS_PORT -a REDIS_PASSWORD GET test_key
# Should return: "test_value"
```

## Step 9: Monitor and Troubleshoot

### Monitor TimescaleDB

```bash
# Check row count
psql "YOUR_DATABASE_URL" -c "SELECT COUNT(*) FROM tick_prices;"

# Check data by mint
psql "YOUR_DATABASE_URL" -c "SELECT DISTINCT mint FROM tick_prices LIMIT 10;"

# Check recent data
psql "YOUR_DATABASE_URL" -c "SELECT * FROM tick_prices ORDER BY time DESC LIMIT 10;"
```

### Monitor Kafka Consumer Logs

In Railway:
1. Click on your service running `kafka_to_timescale.py`
2. Go to the **"Logs"** tab
3. Look for messages like: `[Kafka→TS] Wrote X records to TimescaleDB`

### Common Issues

| Issue | Solution |
|-------|----------|
| `KAFKA_BOOTSTRAP_SERVERS not found` | Verify env var is set in Railway. Use Railway UI to set it. |
| `Database connection timeout` | Check DATABASE_URL format. Verify PostgreSQL service is running. |
| `Could not create hypertable` | Verify TimescaleDB extension is enabled. Try: `CREATE EXTENSION timescaledb;` |
| `Kafka topic not found` | Create topics using kafka-topics CLI (Step 5). |
| `Redis connection refused` | Verify REDIS_HOST, REDIS_PORT, and REDIS_PASSWORD are correct. |

## Next Steps

After completing this setup:

1. **Deploy Your Main Application** - Add your bot.py or quant_platform.py as a new Railway service
2. **Connect Spark Optimizer** - Configure spark_driver.py to read from Redis and optimize parameters
3. **Monitor Dashboard** - Use dashboard.py to visualize optimization metrics
4. **Set Up Logging** - Configure observability.py to send metrics to your monitoring system

## Reference Files

- Kafka Consumer: `connectors/kafka_to_timescale.py`
- Spark Optimizer: `optimizer/spark_driver.py`
- Dashboard: `dashboard.py`
- Configuration: Environment variables (see Step 6)
