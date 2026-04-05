# Railway Deployment Checklist

Use this checklist to track your progress through the deployment setup.

## Phase 1: TimescaleDB Setup

- [ ] **Find DATABASE_URL**
  - Go to Railway → PostgreSQL service → Connect tab
  - Copy the full connection string
  - Store it securely (will need it for multiple steps)

- [ ] **Enable TimescaleDB Extension**
  - Run: `psql "YOUR_DATABASE_URL" -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"`
  - Verify: `psql "YOUR_DATABASE_URL" -c "SELECT * FROM pg_extension WHERE extname = 'timescaledb';"`
  - Expected: One row showing timescaledb extension

- [ ] **Create tick_prices Hypertable**
  - Run the SQL in RAILWAY_SETUP_GUIDE.md Step 2
  - Verify: `psql "YOUR_DATABASE_URL" -c "\dt tick_prices"`
  - Expected: tick_prices table listed in output

- [ ] **Create Indexes**
  - Verify indexes exist: `psql "YOUR_DATABASE_URL" -c "\di"`
  - Expected: Multiple indexes on tick_prices table

## Phase 2: Kafka Setup

- [ ] **Create Kafka Service in Railway**
  - Click "+ New" button
  - Select Database → Kafka
  - Wait for deployment (2-3 minutes)
  - Status: Deploying → Running

- [ ] **Get KAFKA_BOOTSTRAP_SERVERS**
  - Click on Kafka service
  - Go to Connect tab
  - Copy KAFKA_BOOTSTRAP_SERVERS value
  - Format: `hostname:9092`

- [ ] **Create Kafka Topics**
  - Create `market.events` (3 partitions)
  - Create `shadow.positions` (2 partitions)
  - Create `bot.parameters` (1 partition)
  - Verify: `kafka-topics --list --bootstrap-server <BROKERS>`

## Phase 3: Redis Setup

- [ ] **Create Redis Service in Railway**
  - Click "+ New" button
  - Select Database → Redis
  - Wait for deployment (1-2 minutes)
  - Status: Deploying → Running

- [ ] **Get Redis Connection Details**
  - Click on Redis service
  - Go to Connect tab
  - Copy REDIS_URL
  - Extract: REDIS_HOST, REDIS_PORT, REDIS_PASSWORD

- [ ] **Test Redis Connection**
  - Run: `redis-cli -h REDIS_HOST -p REDIS_PORT -a REDIS_PASSWORD PING`
  - Expected: PONG

## Phase 4: Environment Variables Configuration

- [ ] **Set DATABASE_URL in Railway**
  - Service variables → New Variable
  - Key: DATABASE_URL
  - Value: Your PostgreSQL connection string

- [ ] **Set KAFKA_BOOTSTRAP_SERVERS**
  - Service variables → New Variable
  - Key: KAFKA_BOOTSTRAP_SERVERS
  - Value: Kafka host:port from Phase 2

- [ ] **Set REDIS_HOST**
  - Service variables → New Variable
  - Key: REDIS_HOST
  - Value: Redis hostname

- [ ] **Set REDIS_PORT**
  - Service variables → New Variable
  - Key: REDIS_PORT
  - Value: 6379

- [ ] **Set REDIS_PASSWORD**
  - Service variables → New Variable
  - Key: REDIS_PASSWORD
  - Value: Redis password from Railway

- [ ] **Set REDIS_DB**
  - Service variables → New Variable
  - Key: REDIS_DB
  - Value: 0

- [ ] **Set Optimization Settings**
  - AUTO_OPTIMIZE_THRESHOLD_PCT: 5.0
  - AUTO_OPTIMIZE_INTERVAL_MIN: 5
  - AUTO_OPTIMIZE_ENABLED: true
  - SHADOW_TESTING_WINDOW_MIN: 60

- [ ] **Set Spark Settings**
  - SPARK_MASTER: local
  - SPARK_EXECUTOR_MEMORY: 2g
  - SPARK_DRIVER_MEMORY: 2g

## Phase 5: Application Deployment

- [ ] **Deploy Kafka Consumer Service**
  - Create new GitHub/Docker service
  - Ensure all environment variables are set
  - Configure to run: `python connectors/kafka_to_timescale.py`
  - Wait for service to start

- [ ] **Monitor Consumer Logs**
  - Go to service → Logs tab
  - Look for: `[Kafka→TS] Consumer connected successfully`
  - Look for: `[Kafka→TS] Connection pool initialized`

## Phase 6: Verification & Testing

- [ ] **Test TimescaleDB Connection**
  ```bash
  psql "YOUR_DATABASE_URL" -c "SELECT COUNT(*) FROM tick_prices;"
  ```
  Expected: 0 or higher (no error)

- [ ] **Test Kafka Topics Exist**
  ```bash
  kafka-topics --list --bootstrap-server $KAFKA_BROKERS
  ```
  Expected: market.events, shadow.positions, bot.parameters listed

- [ ] **Test Redis Connection**
  ```bash
  redis-cli -h REDIS_HOST -p REDIS_PORT -a REDIS_PASSWORD PING
  ```
  Expected: PONG

- [ ] **Produce Test Message to Kafka**
  ```bash
  echo '{"mint":"TEST","price":100.0}' | kafka-console-producer \
    --broker-list $KAFKA_BROKERS --topic market.events
  ```
  Expected: No errors

- [ ] **Verify Consumer Processing**
  - Send test message (see above)
  - Check Railway logs for: `[Kafka→TS] Wrote X records`
  - Check database: `SELECT COUNT(*) FROM tick_prices;` (count should increase)

## Phase 7: Final Checks

- [ ] **All Services Running**
  - PostgreSQL: Running
  - Kafka: Running
  - Redis: Running
  - Kafka Consumer: Running

- [ ] **No Error Logs**
  - PostgreSQL: No critical errors
  - Kafka: No connection errors
  - Redis: No connection errors
  - Consumer: No database errors

- [ ] **Data Flow Working**
  - Messages can be produced to Kafka
  - Consumer reads messages
  - Data written to TimescaleDB
  - Redis accessible for state storage

## Troubleshooting Guide

If any step fails, refer to this guide:

| Problem | Solution |
|---------|----------|
| TimescaleDB extension not found | Enable with: `CREATE EXTENSION timescaledb;` |
| Cannot connect to Kafka | Check KAFKA_BOOTSTRAP_SERVERS is correct and Kafka service is running |
| Cannot connect to Redis | Verify REDIS_HOST, REDIS_PORT, REDIS_PASSWORD |
| Kafka topics not found | Create with: `kafka-topics --create --bootstrap-server ... --topic ...` |
| Consumer crashes on startup | Check all env vars set, DATABASE_URL accessible, KAFKA_BOOTSTRAP_SERVERS valid |
| No data in tick_prices | Check consumer logs. Verify market.events topic has messages. |

## Next Steps After Completion

1. **Deploy Main Bot** - Set up bot.py or quant_platform.py service
2. **Deploy Dashboard** - Set up dashboard.py for real-time monitoring
3. **Configure Spark Optimizer** - Set up spark_driver.py for parameter optimization
4. **Set Up Alerts** - Configure logging/monitoring for production

## Notes

- Services typically take 1-3 minutes to deploy on Railway
- Kafka topics are created once and persist
- TimescaleDB tables are permanent - verify structure before creation
- Redis data is volatile - used for temporary state only
- Always test connections before assuming configuration is correct

---

**Status**: [ ] All items complete and verified
**Date Completed**: ___________
**Next Phase**: _________________
