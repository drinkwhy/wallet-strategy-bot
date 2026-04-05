# Railway Kafka/Spark/TimescaleDB Setup - Complete Index

Welcome! This file helps you navigate all the setup resources created for your Railway deployment.

## Start Here

**First-time setup?** Read this file, then choose one of the three paths below.

## Three Ways to Get Started

### Path 1: Fast Track (30 minutes) - Recommended for experienced users
- **Read**: `QUICKSTART.md`
- **Time**: 30 minutes
- **Best for**: Users who understand Kafka, PostgreSQL, and Redis
- **Include**: Essential commands only, minimal explanations

### Path 2: Complete Guide (1-2 hours) - Recommended for thorough setup
- **Read**: `RAILWAY_SETUP_GUIDE.md`
- **Time**: 1-2 hours
- **Best for**: First-time deployments, users who want to understand each step
- **Includes**: Detailed explanations, UI screenshots, troubleshooting

### Path 3: Structured Learning (45 minutes) - Recommended for most users
- **Read in order**:
  1. `RAILWAY_DEPLOYMENT_README.md` (10 min) - Architecture overview
  2. `QUICKSTART.md` (20 min) - Fast setup steps
  3. `DEPLOYMENT_CHECKLIST.md` (10 min) - Track progress
  4. `RAILWAY_SETUP_GUIDE.md` - Reference as needed
- **Time**: 45 minutes + reference
- **Best for**: Most users - balanced learning and speed

## Document Guide

### Main Documentation

| Document | Purpose | Read Time | Use When |
|----------|---------|-----------|----------|
| `QUICKSTART.md` | 30-minute fast setup | 10-15 min | You know Kafka/PostgreSQL |
| `RAILWAY_SETUP_GUIDE.md` | Complete step-by-step guide | 30-45 min | First deployment, need details |
| `RAILWAY_DEPLOYMENT_README.md` | Architecture & reference | 10-15 min | Learning overview, troubleshooting |
| `DEPLOYMENT_CHECKLIST.md` | Progress tracking | 5 min | Track where you are |
| `DEPLOYMENT_SETUP_SUMMARY.md` | This setup package overview | 5-10 min | Understand what's available |
| `SETUP_INDEX.md` | Navigation guide | 2 min | You are here! |

### Configuration Files

| File | Purpose | How to Use |
|------|---------|-----------|
| `.env.railway.example` | Environment variables template | Copy to `.env`, fill in your values |

### SQL Scripts

| File | Purpose | When to Run |
|------|---------|------------|
| `sql/01_enable_timescaledb.sql` | Enable TimescaleDB extension | Step 1 of setup |
| `sql/02_create_tick_prices_hypertable.sql` | Create tick_prices table | Step 2 of setup |
| `sql/03_query_examples.sql` | Monitoring and query examples | During monitoring phase |

Run these with:
```bash
psql "YOUR_DATABASE_URL" < sql/01_enable_timescaledb.sql
psql "YOUR_DATABASE_URL" < sql/02_create_tick_prices_hypertable.sql
```

### Validation Scripts

| File | Purpose | How to Run |
|------|---------|-----------|
| `scripts/validate_railway_config.py` | Python validation of all services | `python3 scripts/validate_railway_config.py` |
| `test_railway_setup.sh` | Bash validation (if psql/kafka installed) | `bash test_railway_setup.sh` |

Both scripts test:
- Environment variables
- PostgreSQL/TimescaleDB connection
- Kafka connectivity
- Redis connectivity
- Required Python packages

## Step-by-Step Setup Overview

### Phase 1: Preparation (5 minutes)
1. Gather Railway connection strings
2. Choose your learning path
3. Start reading appropriate documentation

### Phase 2: TimescaleDB Setup (10 minutes)
1. Enable TimescaleDB extension
2. Create tick_prices hypertable
3. Verify table structure

Files: `sql/01_*.sql` and `sql/02_*.sql`

### Phase 3: Kafka Setup (15 minutes)
1. Create Kafka service in Railway
2. Create required topics
3. Verify topics exist

Reference: `QUICKSTART.md` Phase 2 or `RAILWAY_SETUP_GUIDE.md` Step 3

### Phase 4: Redis Setup (10 minutes)
1. Create Redis service in Railway
2. Extract connection details
3. Test connection

Reference: `QUICKSTART.md` Phase 3 or `RAILWAY_SETUP_GUIDE.md` Step 4

### Phase 5: Configuration (10 minutes)
1. Copy `.env.railway.example`
2. Fill in connection details
3. Set variables in Railway

Reference: `.env.railway.example` file

### Phase 6: Validation (5 minutes)
1. Run validation script
2. Verify all connections
3. Check logs

Files: `scripts/validate_railway_config.py` or `test_railway_setup.sh`

### Phase 7: Deployment (5 minutes)
1. Deploy Kafka consumer
2. Monitor logs
3. Verify data flow

Code: `connectors/kafka_to_timescale.py`

## Essential Commands

### Get Your Database URL
```bash
# From Railway PostgreSQL service → Connect tab
# Format: postgresql://user:password@host:port/database
export DATABASE_URL="postgresql://..."
```

### Enable TimescaleDB
```bash
psql "$DATABASE_URL" < sql/01_enable_timescaledb.sql
```

### Create Hypertable
```bash
psql "$DATABASE_URL" < sql/02_create_tick_prices_hypertable.sql
```

### Create Kafka Topics
```bash
export KAFKA_BROKERS="your-kafka-host:9092"

kafka-topics --create --bootstrap-server $KAFKA_BROKERS \
  --topic market.events --partitions 3 --replication-factor 1

kafka-topics --create --bootstrap-server $KAFKA_BROKERS \
  --topic shadow.positions --partitions 2 --replication-factor 1

kafka-topics --create --bootstrap-server $KAFKA_BROKERS \
  --topic bot.parameters --partitions 1 --replication-factor 1
```

### Validate Everything
```bash
python3 scripts/validate_railway_config.py
```

### Start Kafka Consumer
```bash
python connectors/kafka_to_timescale.py
```

## Environment Variables Checklist

Essential (required):
- [ ] DATABASE_URL
- [ ] KAFKA_BOOTSTRAP_SERVERS
- [ ] REDIS_HOST
- [ ] REDIS_PORT
- [ ] REDIS_PASSWORD (or leave empty if not required)
- [ ] REDIS_DB

Optimization (optional but recommended):
- [ ] AUTO_OPTIMIZE_ENABLED = true
- [ ] AUTO_OPTIMIZE_THRESHOLD_PCT = 5.0
- [ ] AUTO_OPTIMIZE_INTERVAL_MIN = 5
- [ ] SHADOW_TESTING_WINDOW_MIN = 60

Spark/Ray (optional):
- [ ] SPARK_MASTER = local
- [ ] SPARK_EXECUTOR_MEMORY = 2g
- [ ] SPARK_DRIVER_MEMORY = 2g

See `.env.railway.example` for complete list.

## Troubleshooting Guide

### Connection Issues

| Problem | Check | Fix |
|---------|-------|-----|
| Cannot connect to PostgreSQL | DATABASE_URL format | See RAILWAY_SETUP_GUIDE.md Step 1a |
| Cannot connect to Kafka | KAFKA_BOOTSTRAP_SERVERS | See RAILWAY_SETUP_GUIDE.md Step 3 |
| Cannot connect to Redis | REDIS_HOST, REDIS_PORT, REDIS_PASSWORD | See RAILWAY_SETUP_GUIDE.md Step 4 |

### Data Flow Issues

| Problem | Check | Fix |
|---------|-------|-----|
| No data in tick_prices | Consumer logs, Kafka topics | See RAILWAY_DEPLOYMENT_README.md Troubleshooting |
| Kafka topics don't exist | Topic list | Create with sql/02_*.sql or CLI |
| Consumer crashes | Error logs, environment vars | Check all env vars set, database accessible |

See `RAILWAY_DEPLOYMENT_README.md` Troubleshooting section for detailed help.

## Related Files in This Repository

### Application Code
- `connectors/kafka_to_timescale.py` - Kafka consumer that writes to TimescaleDB
- `optimizer/spark_driver.py` - Spark-based parameter optimizer
- `dashboard.py` - Real-time monitoring dashboard
- `bot.py` - Main trading bot

### Existing Documentation
- `phase5_integration_test.py` - Integration tests
- Recent git commits show Phase 5 Dashboard and Spark integration

## What This Setup Provides

After completing setup, you'll have:

1. **PostgreSQL with TimescaleDB**
   - Time-series optimized database
   - tick_prices hypertable
   - Proper indexes for fast queries

2. **Kafka Message Broker**
   - 3 topics for event streaming
   - Decoupled services
   - Event-driven architecture

3. **Redis Cache**
   - Fast in-memory state storage
   - Optimization parameter caching
   - Session management

4. **Kafka Consumer Service**
   - Reads market prices from Kafka
   - Batches writes for efficiency
   - Stores data in TimescaleDB

5. **Monitoring & Validation**
   - Python and Bash validation scripts
   - Example queries for monitoring
   - Troubleshooting guide

## Timeline

- **Reading Documentation**: 10-45 minutes (depending on path)
- **TimescaleDB Setup**: 10 minutes
- **Kafka Setup**: 15 minutes
- **Redis Setup**: 10 minutes
- **Configuration**: 10 minutes
- **Validation**: 5 minutes
- **Total Setup Time**: 60-95 minutes

## Next Steps

1. **Choose your learning path** (see "Three Ways to Get Started" above)
2. **Read the appropriate guide**
3. **Follow the setup steps**
4. **Run validation script**
5. **Deploy services**
6. **Monitor and verify**

## Support

If you get stuck:

1. **Check DEPLOYMENT_CHECKLIST.md** - See what step failed
2. **Read RAILWAY_SETUP_GUIDE.md Step 9** - Troubleshooting guide
3. **Run validation script** - Gets specific error messages
4. **Check logs** - Railway UI shows service logs
5. **Refer to external docs**:
   - Railway: https://docs.railway.app
   - TimescaleDB: https://docs.timescale.com
   - Kafka: https://kafka.apache.org/documentation
   - PostgreSQL: https://www.postgresql.org/docs

## Quick Links

- Start with **QUICKSTART.md** (30 min)
- Or read **RAILWAY_SETUP_GUIDE.md** (detailed)
- Or follow **Path 3** above (balanced)

All files are in your repository root directory.

---

## File Locations

```
Root Directory:
├── SETUP_INDEX.md (you are here)
├── QUICKSTART.md
├── RAILWAY_SETUP_GUIDE.md
├── RAILWAY_DEPLOYMENT_README.md
├── DEPLOYMENT_CHECKLIST.md
├── DEPLOYMENT_SETUP_SUMMARY.md
├── .env.railway.example
├── test_railway_setup.sh
│
├── sql/
│   ├── 01_enable_timescaledb.sql
│   ├── 02_create_tick_prices_hypertable.sql
│   └── 03_query_examples.sql
│
├── scripts/
│   └── validate_railway_config.py
│
└── connectors/
    └── kafka_to_timescale.py (existing code)
```

---

**Start Reading**: Choose one of the three paths above and open that file!

**Version**: 1.0
**Status**: Complete and ready to use
**Last Updated**: 2024
