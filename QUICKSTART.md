# Railway Deployment - Quick Start (30 Minutes)

This is the fastest way to get your Kafka/Spark/TimescaleDB system running on Railway.

## Before You Start

You need:
- Your PostgreSQL **DATABASE_URL** from Railway (from PostgreSQL service → Connect tab)
- A Railway account with access to create new services

## 30-Minute Quick Start

### Minute 0-2: Enable TimescaleDB

```bash
# Copy your DATABASE_URL from Railway PostgreSQL service
export DATABASE_URL="postgresql://user:password@host:port/db"

# Enable TimescaleDB
psql "$DATABASE_URL" << 'EOF'
CREATE EXTENSION IF NOT EXISTS timescaledb;
SELECT * FROM pg_extension WHERE extname = 'timescaledb';
EOF
```

Should see one row with "timescaledb" in the output.

### Minute 2-5: Create Hypertable

```bash
psql "$DATABASE_URL" << 'EOF'
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

SELECT * FROM tick_prices LIMIT 1;
EOF
```

Should see table structure with no errors.

### Minute 5-15: Create Kafka Service

1. Go to Railway dashboard
2. Click **"+ New"** button
3. Select **Database** → **Kafka**
4. Wait for deployment (shows "Running")
5. Click on Kafka service
6. Click **"Connect"** tab
7. **Copy the KAFKA_BOOTSTRAP_SERVERS value** (save this)

### Minute 15-20: Create Redis Service

1. Click **"+ New"** button
2. Select **Database** → **Redis**
3. Wait for deployment
4. Click on Redis service
5. Click **"Connect"** tab
6. **Copy the REDIS_URL** (save this)

From REDIS_URL like `redis://:password@host:6379`, extract:
- REDIS_HOST = the hostname
- REDIS_PORT = 6379
- REDIS_PASSWORD = the password

### Minute 20-25: Create Kafka Topics

```bash
# Get bootstrap servers from Railway Kafka service (from step above)
export KAFKA_BROKERS="your-kafka-host:9092"

# Create topics (copy-paste each)
kafka-topics --create --bootstrap-server $KAFKA_BROKERS \
  --topic market.events --partitions 3 --replication-factor 1

kafka-topics --create --bootstrap-server $KAFKA_BROKERS \
  --topic shadow.positions --partitions 2 --replication-factor 1

kafka-topics --create --bootstrap-server $KAFKA_BROKERS \
  --topic bot.parameters --partitions 1 --replication-factor 1

# Verify
kafka-topics --list --bootstrap-server $KAFKA_BROKERS
```

Should see all three topics listed.

### Minute 25-30: Set Environment Variables in Railway

For your Kafka Consumer service in Railway:

1. Click on your service (or create a new one)
2. Click **"Variables"** tab
3. Add these variables (click "+ New Variable" for each):

```
DATABASE_URL = [your PostgreSQL connection string]
KAFKA_BOOTSTRAP_SERVERS = [from Kafka service]
REDIS_HOST = [from Redis REDIS_URL]
REDIS_PORT = 6379
REDIS_PASSWORD = [from Redis REDIS_URL]
REDIS_DB = 0
AUTO_OPTIMIZE_ENABLED = true
AUTO_OPTIMIZE_THRESHOLD_PCT = 5.0
SPARK_MASTER = local
```

## Done! Verification

Run this to verify everything works:

```bash
python3 scripts/validate_railway_config.py
```

Or manually test each service:

```bash
# Test TimescaleDB
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM tick_prices;"

# Test Kafka
kafka-topics --list --bootstrap-server $KAFKA_BROKERS

# Test Redis
redis-cli -h REDIS_HOST -p REDIS_PORT -a REDIS_PASSWORD PING
# Should return: PONG
```

## Next: Run the Kafka Consumer

Once verified, deploy and run:

```bash
python connectors/kafka_to_timescale.py
```

The consumer will:
- Connect to Kafka
- Read from market.events topic
- Write to TimescaleDB tick_prices table
- Continue running indefinitely

Check logs in Railway for messages like:
```
[Kafka→TS] Consumer connected successfully
[Kafka→TS] Connection pool initialized
[Kafka→TS] Wrote 1000 records to TimescaleDB
```

## Troubleshooting

| Error | Fix |
|-------|-----|
| "psql: command not found" | Install PostgreSQL client: `brew install postgresql` (Mac) or `apt install postgresql-client` (Linux) |
| "kafka-topics not found" | Install Kafka CLI tools or use Docker |
| "Cannot connect to Kafka" | Check KAFKA_BROKERS value, make sure Kafka service is Running in Railway |
| "TimescaleDB extension not found" | Run: `CREATE EXTENSION IF NOT EXISTS timescaledb;` |
| "Redis connection refused" | Check REDIS_HOST, REDIS_PORT, REDIS_PASSWORD values |

## What's Next?

After verification:

1. **Deploy Spark Optimizer** - Analyze and optimize trading parameters
2. **Deploy Dashboard** - Real-time monitoring interface
3. **Deploy Main Bot** - Your trading bot application
4. **Set up Monitoring** - Error tracking and alerts

See `RAILWAY_SETUP_GUIDE.md` for detailed steps.

## Files You Need

- `RAILWAY_SETUP_GUIDE.md` - Full detailed guide
- `DEPLOYMENT_CHECKLIST.md` - Track progress
- `.env.railway.example` - Environment template
- `sql/02_create_tick_prices_hypertable.sql` - Hypertable creation
- `scripts/validate_railway_config.py` - Validation script
- `connectors/kafka_to_timescale.py` - Consumer code

## Time Estimate

- Setup: 30 minutes
- Deployment: 5-10 minutes per service
- Verification: 5 minutes
- Total: ~45 minutes to fully running system

---

**Next**: See `RAILWAY_SETUP_GUIDE.md` for complete deployment details
