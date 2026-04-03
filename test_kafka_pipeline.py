"""
Test Kafka Pipeline Components

This test verifies that the Kafka producer and consumer modules
can be imported and initialized successfully.
"""

import sys
import logging
from unittest.mock import patch, MagicMock

logging.basicConfig(level=logging.INFO)

# Test 1: Import modules
print("=" * 60)
print("TEST 1: Module Imports")
print("=" * 60)

try:
    from scanner.market_feed import PriceProducer
    print("[PASS] Successfully imported PriceProducer")
except Exception as e:
    print(f"[FAIL] Failed to import PriceProducer: {e}")
    sys.exit(1)

try:
    from connectors.kafka_to_timescale import KafkaToTimescale
    print("[PASS] Successfully imported KafkaToTimescale")
except Exception as e:
    print(f"[FAIL] Failed to import KafkaToTimescale: {e}")
    sys.exit(1)

# Test 2: PriceProducer initialization (with mock Kafka)
print("\n" + "=" * 60)
print("TEST 2: PriceProducer Initialization")
print("=" * 60)

with patch("scanner.market_feed.KafkaProducer") as mock_kafka:
    mock_kafka.return_value = MagicMock()
    try:
        producer = PriceProducer(bootstrap_servers="localhost:9092")
        print("[PASS] PriceProducer initialized successfully")
        print(f"  - Topic: {producer.topic}")
        print(f"  - Producer instance created: {producer.producer is not None}")
    except Exception as e:
        print(f"[FAIL] Failed to initialize PriceProducer: {e}")
        sys.exit(1)

# Test 3: send_price_update method signature
print("\n" + "=" * 60)
print("TEST 3: PriceProducer.send_price_update()")
print("=" * 60)

with patch("scanner.market_feed.KafkaProducer") as mock_kafka:
    mock_producer = MagicMock()
    mock_future = MagicMock()
    mock_producer.send.return_value = mock_future
    mock_kafka.return_value = mock_producer

    producer = PriceProducer(bootstrap_servers="localhost:9092")

    # Test send_price_update
    result = producer.send_price_update(
        mint="TEST123",
        price=100.5,
        bid=100.4,
        ask=100.6,
        volume_1m=1000.0,
        liquidity_usd=50000.0,
    )

    if mock_producer.send.called:
        print("[PASS] send_price_update called producer.send()")
        call_args = mock_producer.send.call_args
        print(f"  - Topic: {call_args[0][0]}")
        msg = call_args[1]["value"]
        print(f"  - Mint: {msg['mint']}")
        print(f"  - Price: {msg['price']}")
        print(f"  - Spread BPS: {msg['spread_bps']}")
    else:
        print("[FAIL] send_price_update did not call producer.send()")
        sys.exit(1)

# Test 4: Spread calculation
print("\n" + "=" * 60)
print("TEST 4: Spread Calculation")
print("=" * 60)

spread = PriceProducer._calculate_spread_bps(bid=100.0, ask=101.0)
expected = 100.0  # (101-100)/100 * 10000 = 100 bps
if spread == expected:
    print(f"[PASS] Spread calculation correct: {spread} bps (expected {expected} bps)")
else:
    print(f"[FAIL] Spread calculation incorrect: {spread} bps (expected {expected} bps)")
    sys.exit(1)

spread_none = PriceProducer._calculate_spread_bps(bid=None, ask=100.0)
if spread_none is None:
    print("[PASS] Spread handles None values correctly")
else:
    print(f"[FAIL] Spread should be None for None input, got {spread_none}")
    sys.exit(1)

# Test 5: KafkaToTimescale initialization (with mocks)
print("\n" + "=" * 60)
print("TEST 5: KafkaToTimescale Initialization")
print("=" * 60)

import os

with patch("connectors.kafka_to_timescale.KafkaConsumer") as mock_consumer, \
     patch("connectors.kafka_to_timescale.pool") as mock_pool:
    # Set up mocks
    mock_consumer.return_value = MagicMock()
    mock_pool_instance = MagicMock()
    mock_pool.SimpleConnectionPool.return_value = mock_pool_instance

    # Set DATABASE_URL env var
    os.environ["DATABASE_URL"] = "postgresql://localhost/test"

    try:
        consumer = KafkaToTimescale(
            kafka_servers="localhost:9092",
            database_url="postgresql://localhost/test",
        )
        print("[PASS] KafkaToTimescale initialized successfully")
        print(f"  - Topic: {consumer.topic}")
        print(f"  - Batch size: {consumer.batch_size}")
        print(f"  - Consumer instance created: {consumer.consumer is not None}")
    except Exception as e:
        print(f"[FAIL] Failed to initialize KafkaToTimescale: {e}")
        sys.exit(1)

# Test 6: Batch addition
print("\n" + "=" * 60)
print("TEST 6: Batch Management")
print("=" * 60)

with patch("connectors.kafka_to_timescale.KafkaConsumer") as mock_consumer, \
     patch("connectors.kafka_to_timescale.pool") as mock_pool:
    mock_consumer.return_value = MagicMock()
    mock_pool_instance = MagicMock()
    mock_pool.SimpleConnectionPool.return_value = mock_pool_instance

    os.environ["DATABASE_URL"] = "postgresql://localhost/test"

    consumer = KafkaToTimescale(
        kafka_servers="localhost:9092",
        database_url="postgresql://localhost/test",
        batch_size=5,
    )

    # Add a message to the batch
    test_data = {
        "mint": "TEST123",
        "price": 100.5,
        "bid": 100.4,
        "ask": 100.6,
        "timestamp": 1700000000000,
        "volume_1m": 1000.0,
        "volume_5m": 5000.0,
        "spread_bps": 10.0,
        "liquidity_usd": 50000.0,
    }

    consumer._add_to_batch(test_data)

    if len(consumer.batch) == 1:
        print("[PASS] Batch correctly added message")
        print(f"  - Batch size: {len(consumer.batch)}")
        print(f"  - Batch item: {consumer.batch[0]}")
    else:
        print(f"[FAIL] Batch size incorrect: {len(consumer.batch)} (expected 1)")
        sys.exit(1)

print("\n" + "=" * 60)
print("ALL TESTS PASSED!")
print("=" * 60)
print("\nKafka pipeline components are ready for integration.")
print("\nNext steps:")
print("1. Install kafka-python: pip install -r requirements.txt")
print("2. Set up Kafka cluster (Railway or local)")
print("3. Set KAFKA_BOOTSTRAP_SERVERS environment variable")
print("4. Integrate PriceProducer into scanner")
print("5. Run KafkaToTimescale consumer")
