# Railway Kafka/Spark/TimescaleDB Deployment - Complete Setup Package

All prerequisites are now set up for your Railway deployment. This document summarizes what has been created and how to use it.

## What Has Been Created

### Documentation Files

1. **QUICKSTART.md** - Start here!
   - 30-minute fast-track setup
   - Essential steps only
   - Copy-paste ready commands

2. **RAILWAY_SETUP_GUIDE.md** - Complete guide
   - Detailed step-by-step instructions
   - Full context and explanations
   - 9 comprehensive phases

3. **RAILWAY_DEPLOYMENT_README.md** - Reference guide
   - Architecture overview
   - Quick reference sections
   - Monitoring & troubleshooting

4. **DEPLOYMENT_CHECKLIST.md** - Progress tracking
   - Phase-by-phase checklist
   - Verification steps
   - Troubleshooting table

### Configuration Files

5. **.env.railway.example** - Environment template
   - All required variables listed
   - Example values with explanations
   - Copy to .env and fill in your values

### SQL Scripts

6. **sql/01_enable_timescaledb.sql**
   - Enable TimescaleDB extension
   - Verify installation

7. **sql/02_create_tick_prices_hypertable.sql**
   - Create tick_prices table
   - Set up indexes
   - Test insert/select

8. **sql/03_query_examples.sql**
   - Monitoring queries
   - Data analysis examples
   - Performance checks

### Validation Scripts

9. **test_railway_setup.sh** - Bash validation
   - Tests all connections
   - Color-coded output
   - Cross-platform compatible

10. **scripts/validate_railway_config.py** - Python validation
    - Comprehensive checks
    - Package verification
    - Detailed error reporting

## Getting Started (Choose One Path)

### Path A: Fast Track (30 minutes)
1. Read: **QUICKSTART.md**
2. Follow: 30-minute setup steps
3. Verify: Run validation script
4. Deploy: Start kafka_to_timescale.py consumer

### Path B: Complete Setup (1-2 hours)
1. Read: **RAILWAY_SETUP_GUIDE.md**
2. Follow: All 9 phases with detailed explanations
3. Reference: Use DEPLOYMENT_CHECKLIST.md to track progress
4. Troubleshoot: Use RAILWAY_DEPLOYMENT_README.md as needed

### Path C: Guided Setup (45 minutes - Recommended)
1. Skim: **RAILWAY_DEPLOYMENT_README.md** for architecture
2. Read: **QUICKSTART.md** section by section
3. Use: **DEPLOYMENT_CHECKLIST.md** to mark progress
4. Validate: Run Python validation script
5. Reference: Keep **RAILWAY_SETUP_GUIDE.md** open for details

## Required Actions

### Step 1: Gather Information (5 minutes)
From your Railway dashboard, collect:
- [ ] PostgreSQL DATABASE_URL
- [ ] Kafka KAFKA_BOOTSTRAP_SERVERS (after creating service)
- [ ] Redis REDIS_HOST, REDIS_PORT, REDIS_PASSWORD (after creating service)

### Step 2: Enable TimescaleDB (5 minutes)
```bash
psql "YOUR_DATABASE_URL" -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
```

### Step 3: Create Database Table (5 minutes)
```bash
psql "YOUR_DATABASE_URL" < sql/02_create_tick_prices_hypertable.sql
```

### Step 4: Create Railway Services (20-30 minutes)
- Create Kafka service in Railway
- Create Redis service in Railway
- Create Kafka topics (market.events, shadow.positions, bot.parameters)

### Step 5: Configure Environment Variables (5-10 minutes)
- Set all variables in your Railway service
- Reference .env.railway.example for list

### Step 6: Validate Setup (5 minutes)
```bash
python3 scripts/validate_railway_config.py
```

### Step 7: Deploy and Monitor
```bash
python connectors/kafka_to_timescale.py
```

## System Components Explained

### PostgreSQL with TimescaleDB
- **Role**: Time-series data storage for market prices
- **Table**: tick_prices (hypertable - optimized for time-series)
- **Data**: Market ticks (price, bid, ask, volume, liquidity)
- **Indexes**: Fast lookups by (mint, time) and (time)

### Kafka Message Broker
- **Role**: Event streaming and decoupling services
- **Topics**:
  - market.events: Price updates from market feed
  - shadow.positions: Position tracking events
  - bot.parameters: Parameter update signals
- **Partitions**: Distributed for parallel processing

### Redis Cache
- **Role**: Fast in-memory storage for state
- **Usage**:
  - Optimization state caching
  - Bot parameter storage
  - Performance metrics
- **Volatility**: Data lost on restart (not persistent)

### Kafka Consumer (kafka_to_timescale.py)
- **Role**: Bridge between Kafka and TimescaleDB
- **Process**:
  1. Reads messages from market.events
  2. Batches them for efficiency
  3. Writes to tick_prices table
  4. Handles errors and reconnection

### Spark Optimizer (optional)
- **Role**: Analyzes tick data and optimizes parameters
- **Input**: tick_prices table from TimescaleDB
- **Output**: Optimized parameters to Redis and Kafka

## Key Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| DATABASE_URL | Yes | PostgreSQL connection string |
| KAFKA_BOOTSTRAP_SERVERS | Yes | Kafka broker addresses |
| REDIS_HOST | Yes | Redis server hostname |
| REDIS_PORT | Yes | Redis port (usually 6379) |
| REDIS_PASSWORD | Yes* | Redis authentication (*if required) |
| REDIS_DB | Yes | Redis database number (0-15) |
| AUTO_OPTIMIZE_ENABLED | No | Enable auto-optimization (true/false) |
| AUTO_OPTIMIZE_THRESHOLD_PCT | No | Optimization trigger threshold |
| SPARK_MASTER | No | Spark cluster address (local/host:7077) |

## Verification Checklist

After setup, verify these work:

```bash
# 1. TimescaleDB
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM tick_prices;"

# 2. Kafka
kafka-topics --list --bootstrap-server $KAFKA_BROKERS

# 3. Redis
redis-cli -h REDIS_HOST -p REDIS_PORT -a REDIS_PASSWORD PING

# 4. Consumer test
python3 -c "from connectors.kafka_to_timescale import KafkaToTimescale; print('Import OK')"

# 5. Full validation
python3 scripts/validate_railway_config.py
```

## Common Issues & Solutions

### "Cannot connect to Database"
- Check DATABASE_URL format
- Verify PostgreSQL service is running in Railway
- Test: `psql "YOUR_DATABASE_URL" -c "SELECT 1"`

### "TimescaleDB extension not found"
- Run: `CREATE EXTENSION IF NOT EXISTS timescaledb;`
- Verify: `SELECT * FROM pg_extension WHERE extname = 'timescaledb';`

### "Kafka connection timeout"
- Check KAFKA_BOOTSTRAP_SERVERS is correct
- Verify Kafka service status in Railway (should be "Running")
- Test: `kafka-topics --list --bootstrap-server $KAFKA_BROKERS`

### "Redis connection refused"
- Verify REDIS_HOST and REDIS_PORT are correct
- Check REDIS_PASSWORD is set (if required)
- Test: `redis-cli -h REDIS_HOST -p REDIS_PORT -a REDIS_PASSWORD PING`

### "Kafka topic not found"
- Create topics manually:
  ```bash
  kafka-topics --create --bootstrap-server $KAFKA_BROKERS \
    --topic market.events --partitions 3
  ```

## File Structure

```
/
├── QUICKSTART.md                              # 30-minute setup
├── RAILWAY_SETUP_GUIDE.md                     # Complete guide
├── RAILWAY_DEPLOYMENT_README.md               # Reference
├── DEPLOYMENT_CHECKLIST.md                    # Progress tracking
├── DEPLOYMENT_SETUP_SUMMARY.md                # This file
├── .env.railway.example                       # Environment template
├── test_railway_setup.sh                      # Bash validation
├── sql/
│   ├── 01_enable_timescaledb.sql             # Enable extension
│   ├── 02_create_tick_prices_hypertable.sql  # Create table
│   └── 03_query_examples.sql                  # Query examples
├── scripts/
│   └── validate_railway_config.py             # Python validation
├── connectors/
│   └── kafka_to_timescale.py                  # Consumer code
└── optimizer/
    └── spark_driver.py                        # Spark optimizer
```

## Next Steps After Setup

1. **Deploy Kafka Consumer**
   - Set up Python service running kafka_to_timescale.py
   - Monitor logs for successful data ingestion

2. **Deploy Spark Optimizer**
   - Configure spark_driver.py with your Spark settings
   - Start optimization loop

3. **Deploy Dashboard**
   - Set up dashboard.py for real-time monitoring
   - Configure charts and alerts

4. **Deploy Main Bot**
   - Deploy bot.py or quant_platform.py
   - Integrate with Kafka for event streaming

5. **Set up Monitoring**
   - Configure observability.py
   - Set up alerts and dashboards
   - Monitor metrics and performance

## Support Resources

- **Railway Docs**: https://docs.railway.app
- **TimescaleDB Docs**: https://docs.timescale.com
- **Kafka Docs**: https://kafka.apache.org/documentation
- **PostgreSQL Docs**: https://www.postgresql.org/docs
- **Redis Docs**: https://redis.io/documentation

## Estimated Timeline

| Phase | Duration | Notes |
|-------|----------|-------|
| Gather Information | 5 min | Collect connection strings |
| Enable TimescaleDB | 5 min | Create extension |
| Create Table | 5 min | Create hypertable |
| Create Kafka Service | 5-10 min | Wait for deployment |
| Create Redis Service | 5-10 min | Wait for deployment |
| Create Topics | 5 min | kafka-topics CLI |
| Set Environment Vars | 5-10 min | Update in Railway |
| Validation | 5 min | Run validation script |
| **Total Setup** | **45-60 min** | Everything ready |
| Deploy Consumer | 5 min | Start processing data |
| Deploy Spark | 10 min | Start optimization |
| Deploy Dashboard | 5 min | Start monitoring |

## Performance Tips

- **Batch Size**: kafka_to_timescale.py batches 1000 records for efficiency
- **Indexes**: tick_prices has indexes on (mint, time) for fast queries
- **Compression**: TimescaleDB auto-compresses old chunks
- **Partitions**: Use 3 partitions for market.events for parallelism
- **Redis TTL**: Set expiration on cached values to prevent memory bloat

## Security Notes

- Store DATABASE_URL, KAFKA_BOOTSTRAP_SERVERS, and REDIS_PASSWORD in Railway secrets
- Use HTTPS for all connections (Railway provides this by default)
- Limit Redis database access with strong passwords
- Consider network isolation for production deployments
- Monitor logs for suspicious activity

---

## Ready to Deploy?

1. Choose your learning path (A, B, or C above)
2. Start with the appropriate documentation file
3. Follow the setup steps
4. Use the validation scripts to verify
5. Deploy and monitor your system

**Next File to Read**: `QUICKSTART.md` (for fast setup) or `RAILWAY_SETUP_GUIDE.md` (for detailed guide)

---

**Setup Package Version**: 1.0
**Last Updated**: 2024
**Status**: Ready for deployment
