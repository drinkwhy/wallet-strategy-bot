# Railway Deployment Setup - Complete Guide

This comprehensive guide will help you deploy the Kafka/Spark/TimescaleDB system on Railway.

## Quick Start (5 Steps)

1. **Enable TimescaleDB** - Add TimescaleDB extension to PostgreSQL
2. **Create Hypertable** - Set up tick_prices table structure
3. **Deploy Kafka & Redis** - Add message broker and cache services
4. **Create Kafka Topics** - Set up required message channels
5. **Configure & Deploy** - Set environment variables and run services

## What You'll Set Up

This deployment includes:

- **PostgreSQL with TimescaleDB** - Time-series database for market tick data
- **Kafka** - Message broker for event streaming
- **Redis** - In-memory cache for optimization state
- **Kafka Consumer** - Process that ingests market data into TimescaleDB
- **Spark Optimizer** - Parameter optimization engine (optional)

## Prerequisites

Before starting, you need:

- A Railway account (https://railway.app)
- An existing PostgreSQL service on Railway (or create one)
- Command-line tools (optional but helpful):
  - `psql` - PostgreSQL client
  - `kafka-topics` - Kafka CLI tool
  - `redis-cli` - Redis client
  - `python3` - Python interpreter

## Files in This Guide

| File | Purpose |
|------|---------|
| `RAILWAY_SETUP_GUIDE.md` | Complete step-by-step setup instructions |
| `DEPLOYMENT_CHECKLIST.md` | Checklist to track your progress |
| `.env.railway.example` | Template for environment variables |
| `RAILWAY_DEPLOYMENT_README.md` | This file - quick reference |
| `sql/01_enable_timescaledb.sql` | Enable TimescaleDB extension |
| `sql/02_create_tick_prices_hypertable.sql` | Create tick_prices table |
| `sql/03_query_examples.sql` | Example queries for monitoring |
| `test_railway_setup.sh` | Bash script to test all connections |
| `scripts/validate_railway_config.py` | Python script to validate configuration |

## Step-by-Step Setup

### Phase 1: TimescaleDB (10-15 minutes)

1. **Find your Database URL**
   - Go to Railway dashboard
   - Click PostgreSQL service → Connect tab
   - Copy the full connection string

2. **Enable TimescaleDB Extension**
   ```bash
   # Via Railway Query tool or local psql
   psql "YOUR_DATABASE_URL" -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
   ```

3. **Create tick_prices Hypertable**
   ```bash
   # Run the SQL from sql/02_create_tick_prices_hypertable.sql
   psql "YOUR_DATABASE_URL" < sql/02_create_tick_prices_hypertable.sql
   ```

### Phase 2: Kafka (10-15 minutes)

1. **Create Kafka Service in Railway**
   - Click "+ New" button
   - Select Database → Kafka
   - Wait for service to deploy

2. **Get Bootstrap Servers**
   - Click Kafka service → Connect tab
   - Copy KAFKA_BOOTSTRAP_SERVERS value

3. **Create Kafka Topics**
   ```bash
   export KAFKA_BROKERS="your-bootstrap-servers:9092"

   kafka-topics --create --bootstrap-server $KAFKA_BROKERS \
     --topic market.events --partitions 3 --replication-factor 1

   kafka-topics --create --bootstrap-server $KAFKA_BROKERS \
     --topic shadow.positions --partitions 2 --replication-factor 1

   kafka-topics --create --bootstrap-server $KAFKA_BROKERS \
     --topic bot.parameters --partitions 1 --replication-factor 1
   ```

### Phase 3: Redis (5-10 minutes)

1. **Create Redis Service in Railway**
   - Click "+ New" button
   - Select Database → Redis
   - Wait for service to deploy

2. **Get Connection Details**
   - Click Redis service → Connect tab
   - Copy REDIS_URL
   - Extract: REDIS_HOST, REDIS_PORT, REDIS_PASSWORD

### Phase 4: Environment Variables (5-10 minutes)

1. **Copy the environment template**
   ```bash
   cp .env.railway.example .env.railway
   ```

2. **Fill in your Railway connection details**
   - DATABASE_URL from PostgreSQL
   - KAFKA_BOOTSTRAP_SERVERS from Kafka
   - REDIS_HOST, REDIS_PORT, REDIS_PASSWORD from Redis

3. **Set variables in Railway**
   - For each service, go to Variables tab
   - Add each variable key and value

### Phase 5: Validation (5 minutes)

**Option A: Using Python Script (Recommended)**
```bash
python3 scripts/validate_railway_config.py
```

**Option B: Using Bash Script**
```bash
bash test_railway_setup.sh
```

## Deployment Architecture

```
┌─────────────────────────────────────────────────────┐
│  Kafka Topic: market.events                         │
│  (price updates from market feed)                   │
└──────────────────┬──────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────┐
│  Kafka Consumer (kafka_to_timescale.py)             │
│  - Consumes messages from market.events             │
│  - Batches writes for efficiency                    │
│  - Writes to TimescaleDB tick_prices table          │
└──────────────────┬──────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────┐
│  TimescaleDB (PostgreSQL)                           │
│  - Table: tick_prices (hypertable)                  │
│  - Indexes: (mint, time) and (time)                 │
│  - Storage: Time-series optimized                   │
└──────────────────┬──────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────┐
│  Spark Optimizer (optional)                         │
│  - Reads tick_prices for analysis                   │
│  - Stores parameters in Redis                       │
│  - Kafka topics: bot.parameters, shadow.positions   │
└─────────────────────────────────────────────────────┘

Redis (side-by-side)
│
├─ Optimization state
├─ Bot parameters
└─ Performance metrics
```

## Verification Steps

After deployment, verify everything is working:

```bash
# 1. Check TimescaleDB
psql "YOUR_DATABASE_URL" -c "SELECT COUNT(*) FROM tick_prices;"

# 2. Check Kafka topics
kafka-topics --list --bootstrap-server $KAFKA_BROKERS

# 3. Check Redis
redis-cli -h REDIS_HOST -p REDIS_PORT -a REDIS_PASSWORD PING
# Should return: PONG

# 4. Produce test message
echo '{"mint":"TEST","price":100.0}' | kafka-console-producer \
  --broker-list $KAFKA_BROKERS --topic market.events

# 5. Check consumer logs in Railway
# Go to service → Logs tab, look for "[Kafka→TS]" messages
```

## Troubleshooting

### Issue: "Cannot connect to Kafka"
- Verify KAFKA_BOOTSTRAP_SERVERS is correct
- Check Kafka service is running in Railway (status: Running)
- Ensure Kafka topics are created

### Issue: "TimescaleDB extension not found"
- Run: `CREATE EXTENSION IF NOT EXISTS timescaledb;`
- Verify it with: `SELECT * FROM pg_extension WHERE extname = 'timescaledb';`

### Issue: "No data appearing in tick_prices"
- Check consumer logs for errors
- Verify messages are being produced to market.events topic
- Check DATABASE_URL is correct and table exists

### Issue: "Redis connection refused"
- Verify REDIS_HOST is correct
- Check REDIS_PASSWORD is set
- Verify Redis service is running in Railway

### Issue: "Kafka topics don't exist"
- Create them manually using kafka-topics CLI
- Or use Railway Kafka UI if available
- See Phase 2, step 3 for commands

## Monitoring & Maintenance

### Monitor Data Flow

```bash
# Check if data is being written
psql "YOUR_DATABASE_URL" -c "SELECT COUNT(*) FROM tick_prices;"

# See most recent data
psql "YOUR_DATABASE_URL" -c "SELECT * FROM tick_prices ORDER BY time DESC LIMIT 5;"

# Check data by mint
psql "YOUR_DATABASE_URL" -c "SELECT DISTINCT mint FROM tick_prices LIMIT 10;"
```

### View Logs in Railway

1. Go to your service
2. Click "Logs" tab
3. Filter by keywords like:
   - `[Kafka→TS]` - Consumer messages
   - `ERROR` - Error messages
   - `Wrote` - Batch writes

### Monitor System Health

```bash
# Check Kafka consumer lag
kafka-consumer-groups --bootstrap-server $KAFKA_BROKERS \
  --group timescale-consumer --describe

# Check Redis memory usage
redis-cli -h REDIS_HOST -p REDIS_PORT -a REDIS_PASSWORD INFO memory

# Check TimescaleDB table size
psql "YOUR_DATABASE_URL" -c "SELECT pg_size_pretty(pg_total_relation_size('tick_prices'));"
```

## Next Steps After Setup

1. **Deploy Kafka Consumer**
   - Add new Python service running `python connectors/kafka_to_timescale.py`

2. **Deploy Spark Optimizer**
   - Configure `optimizer/spark_driver.py`
   - Set SPARK_MASTER, SPARK_EXECUTOR_MEMORY, SPARK_DRIVER_MEMORY

3. **Deploy Dashboard**
   - Set up `dashboard.py` for real-time monitoring

4. **Configure Monitoring**
   - Set up observability with `observability.py`
   - Send metrics to monitoring service

5. **Scale Services**
   - Increase Kafka partitions if needed
   - Add more consumer instances for parallelism
   - Tune database indexes for query performance

## Quick Reference

### Common Commands

```bash
# List all Kafka topics
kafka-topics --list --bootstrap-server $KAFKA_BROKERS

# Check Kafka consumer status
kafka-consumer-groups --bootstrap-server $KAFKA_BROKERS --list

# Test Redis connection
redis-cli -h REDIS_HOST -p REDIS_PORT -a REDIS_PASSWORD PING

# Check PostgreSQL extensions
psql "DATABASE_URL" -c "SELECT * FROM pg_extension;"

# Count records in TimescaleDB
psql "DATABASE_URL" -c "SELECT COUNT(*) FROM tick_prices;"
```

### Environment Variables Checklist

Essential variables:
- [ ] DATABASE_URL
- [ ] KAFKA_BOOTSTRAP_SERVERS
- [ ] REDIS_HOST
- [ ] REDIS_PORT
- [ ] REDIS_PASSWORD (if required)
- [ ] REDIS_DB

Optional variables:
- [ ] AUTO_OPTIMIZE_ENABLED
- [ ] AUTO_OPTIMIZE_THRESHOLD_PCT
- [ ] SPARK_MASTER
- [ ] LOG_LEVEL

## Support & Resources

- Railway Documentation: https://docs.railway.app
- TimescaleDB Docs: https://docs.timescale.com
- Kafka Documentation: https://kafka.apache.org/documentation
- PostgreSQL Docs: https://www.postgresql.org/docs

## File Locations

- Main guide: `RAILWAY_SETUP_GUIDE.md`
- Setup checklist: `DEPLOYMENT_CHECKLIST.md`
- SQL scripts: `sql/`
- Python validator: `scripts/validate_railway_config.py`
- Bash validator: `test_railway_setup.sh`
- Consumer code: `connectors/kafka_to_timescale.py`
- Optimizer code: `optimizer/spark_driver.py`

---

**Status**: Ready for deployment
**Last Updated**: 2024
**Version**: 1.0
