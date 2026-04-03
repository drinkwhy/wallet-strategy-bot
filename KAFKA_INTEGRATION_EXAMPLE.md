# Kafka Integration Guide

This document explains how to integrate the Kafka producer and consumer into the existing scanner and application.

## Architecture Overview

```
Scanner (Price Updates) --> Kafka Topic (market-prices) --> TimescaleDB (tick_prices)
                               |
                               +---> Future integrations (analytics, alerting, etc.)
```

## Components

### 1. PriceProducer (scanner/market_feed.py)

Sends market price updates to Kafka.

**Usage Example:**

```python
from scanner.market_feed import PriceProducer

# Initialize
producer = PriceProducer(bootstrap_servers="localhost:9092")

# Send a single update
producer.send_price_update(
    mint="EPjFWaLb3odcccccccccccccccccccccccccccccccc",
    price=1.0025,
    bid=1.0020,
    ask=1.0030,
    volume_1m=1000000.0,
    volume_5m=5000000.0,
    liquidity_usd=50000000.0
)

# Or send multiple updates
messages = [
    {
        "mint": "mint1",
        "price": 100.0,
        "bid": 99.5,
        "ask": 100.5,
        "volume_1m": 1000.0,
        "volume_5m": 5000.0,
        "liquidity_usd": 50000.0
    },
    {
        "mint": "mint2",
        "price": 50.0,
        "bid": 49.8,
        "ask": 50.2,
        "volume_1m": 500.0,
        "volume_5m": 2500.0,
        "liquidity_usd": 25000.0
    }
]
producer.send_batch(messages)

# Flush pending messages
producer.flush()

# Close when done
producer.close()
```

### 2. KafkaToTimescale (connectors/kafka_to_timescale.py)

Consumes price updates from Kafka and writes them to TimescaleDB.

**Usage Example:**

```python
from connectors.kafka_to_timescale import KafkaToTimescale

# Initialize
consumer = KafkaToTimescale(
    kafka_servers="localhost:9092",
    database_url="postgresql://user:pass@localhost/mydb",
    batch_size=1000
)

# Start consuming (blocks until interrupted)
consumer.run()
```

**Or as a background service:**

```python
import threading

consumer = KafkaToTimescale()
consumer_thread = threading.Thread(target=consumer.run, daemon=True)
consumer_thread.start()

# Your main application continues...
```

## Environment Variables

```bash
# Kafka Bootstrap Servers (comma-separated)
KAFKA_BOOTSTRAP_SERVERS=localhost:9092

# TimescaleDB Connection
DATABASE_URL=postgresql://user:password@localhost:5432/mydb

# Optional: Kafka Consumer Group
KAFKA_CONSUMER_GROUP=timescale-consumer
```

## Integration Steps

### Step 1: Update Requirements

```bash
pip install -r requirements.txt
```

This installs `kafka-python>=2.0.2`

### Step 2: Initialize tick_prices Table

The `tick_prices` table is created automatically by `app.py`'s `init_db()` function.

Schema:
```sql
CREATE TABLE tick_prices (
    time TIMESTAMP NOT NULL,
    mint TEXT NOT NULL,
    price REAL NOT NULL,
    bid REAL,
    ask REAL,
    volume_1m REAL DEFAULT 0,
    volume_5m REAL DEFAULT 0,
    spread_bps REAL,
    liquidity_usd REAL,
    PRIMARY KEY (time, mint)
)
```

### Step 3: Integrate with Scanner

If you have a scanner that emits price updates (e.g., a `PriceUpdater` class), integrate like this:

```python
from scanner.market_feed import PriceProducer

class PriceScanner:
    def __init__(self):
        self.producer = PriceProducer()

    def on_price_update(self, mint, price, bid=None, ask=None, volume=0, liquidity=0):
        """Called when a new price is detected"""
        # Send to Kafka
        self.producer.send_price_update(
            mint=mint,
            price=price,
            bid=bid,
            ask=ask,
            volume_1m=volume,
            liquidity_usd=liquidity
        )

        # Continue with existing logic...
        # (write to PostgreSQL, update cache, etc.)

    def shutdown(self):
        self.producer.close()
```

### Step 4: Start Consumer

Run the consumer as a separate process or service:

```bash
# As a standalone script
python3 -c "from connectors.kafka_to_timescale import KafkaToTimescale; KafkaToTimescale().run()"

# Or with explicit configuration
KAFKA_BOOTSTRAP_SERVERS=kafka.railway.app:9092 \
DATABASE_URL=postgresql://... \
python3 connectors/kafka_to_timescale.py
```

### Step 5: Verify Data Flow

```python
import psycopg2
from datetime import datetime, timedelta

conn = psycopg2.connect("postgresql://...")
cur = conn.cursor()

# Check recent tick_prices
cur.execute("""
    SELECT
        time, mint, price, volume_1m, liquidity_usd,
        COUNT(*) as tick_count
    FROM tick_prices
    WHERE time > NOW() - INTERVAL '1 hour'
    GROUP BY mint, DATE_TRUNC('minute', time)
    ORDER BY time DESC
    LIMIT 10
""")

for row in cur.fetchall():
    print(f"{row[0]} {row[1]} ${row[2]:.6f} Vol:{row[3]:.0f} Liq:${row[4]:.0f}")

cur.close()
conn.close()
```

## Configuration

### Kafka Bootstrap Servers

- **Local Kafka**: `localhost:9092`
- **Railway Kafka**: See railway.app dashboard (format: `kafka.railway.app:9092`)
- **Multiple brokers**: `broker1:9092,broker2:9092,broker3:9092`

### Consumer Groups

- Default: `timescale-consumer`
- Can be changed via `KAFKA_CONSUMER_GROUP` env var
- Allows multiple consumer instances to share load

### Batch Size

- Default: 1000 messages
- Adjust via `batch_size` parameter in KafkaToTimescale
- Smaller batches = more frequent database writes (higher latency, lower throughput)
- Larger batches = fewer writes (lower latency, higher throughput)

## Monitoring

### Check Consumer Lag

```python
from kafka.admin import KafkaAdminClient, ConfigResource, ConfigResourceType

admin = KafkaAdminClient(bootstrap_servers="localhost:9092")

# Get consumer group offsets
# (Implementation depends on kafka-python version)
```

### Monitor Database

```sql
-- Size of tick_prices table
SELECT
    pg_size_pretty(pg_total_relation_size('tick_prices')) as size,
    COUNT(*) as record_count
FROM tick_prices;

-- Inserts per minute
SELECT
    DATE_TRUNC('minute', time) as minute,
    COUNT(*) as inserts
FROM tick_prices
WHERE time > NOW() - INTERVAL '1 day'
GROUP BY DATE_TRUNC('minute', time)
ORDER BY minute DESC;

-- Mints being tracked
SELECT DISTINCT mint
FROM tick_prices
ORDER BY mint;
```

## Troubleshooting

### Issue: "Connection refused" on localhost:9092

**Solution**: Start a local Kafka cluster or configure KAFKA_BOOTSTRAP_SERVERS to point to your Kafka cluster.

### Issue: Consumer not writing to database

**Cause**: DATABASE_URL not set or invalid

**Solution**: Verify the connection string and ensure the database is accessible.

### Issue: Messages not appearing in tick_prices

**Debug steps:**

1. Check consumer logs: `KafkaToTimescale` logs to `connectors.kafka_to_timescale`
2. Verify Kafka has messages: Use Kafka tools like `kafka-console-consumer.sh`
3. Check database: Run the monitoring SQL above
4. Verify table exists: `\d tick_prices` in psql

### Issue: High latency or lag

**Solutions**:
- Increase `batch_size` in consumer
- Add more consumer instances (same consumer group)
- Increase `acks='1'` in producer (trade-off: less durability)
- Use connection pooling in consumer (already implemented)

## Performance Targets

- **Message throughput**: 1,000+ messages/second
- **Latency**: 1-2 seconds from producer to database
- **Batch efficiency**: 90%+ of messages batched within 5 seconds
- **Error recovery**: Automatic retry with backoff

## Phase 4 Integration (Future)

Phase 4 will:
1. Remove direct PostgreSQL writes from scanner
2. Rely entirely on Kafka→TimescaleDB pipeline
3. Add consumer for real-time alerting and risk checks
4. Integrate with app.py's existing endpoints
