"""
Kafka Consumer to TimescaleDB

This module consumes price updates from Kafka and writes them to TimescaleDB
in batches for efficiency and reduced database load.
"""

import json
import os
import logging
from datetime import datetime
from typing import Optional, List, Tuple
from kafka import KafkaConsumer
from kafka.errors import KafkaError
import psycopg2
from psycopg2.extras import execute_batch
from psycopg2 import pool

logger = logging.getLogger(__name__)


class KafkaToTimescale:
    """Consumes Kafka messages and writes to TimescaleDB with batching."""

    def __init__(
        self,
        kafka_servers: Optional[str] = None,
        database_url: Optional[str] = None,
        topic: str = "market-prices",
        batch_size: int = 1000,
        group_id: str = "timescale-consumer",
    ):
        """
        Initialize the Kafka consumer and TimescaleDB connection.

        Args:
            kafka_servers: Comma-separated list of Kafka brokers.
                          Defaults to KAFKA_BOOTSTRAP_SERVERS env var or 'localhost:9092'
            database_url: PostgreSQL/TimescaleDB connection string.
                         Defaults to DATABASE_URL env var
            topic: Kafka topic to consume from. Defaults to 'market-prices'
            batch_size: Number of messages to accumulate before writing. Defaults to 1000
            group_id: Kafka consumer group ID. Defaults to 'timescale-consumer'
        """
        self.topic = topic
        self.batch_size = batch_size
        self.batch: List[Tuple] = []

        # Initialize Kafka consumer
        if kafka_servers is None:
            kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")

        if isinstance(kafka_servers, str):
            kafka_servers = [s.strip() for s in kafka_servers.split(",")]

        logger.info(f"[Kafka→TS] Initializing consumer with brokers: {kafka_servers}")

        try:
            self.consumer = KafkaConsumer(
                topic,
                bootstrap_servers=kafka_servers,
                group_id=group_id,
                auto_offset_reset="latest",
                enable_auto_commit=True,
                auto_commit_interval_ms=5000,
                value_deserializer=lambda m: json.loads(m.decode("utf-8")),
                api_version=(0, 10, 1),  # Compatibility with older brokers
            )
            logger.info("[Kafka→TS] Consumer connected successfully")
        except Exception as e:
            logger.error(f"[Kafka→TS] Failed to initialize consumer: {e}")
            raise

        # Initialize TimescaleDB connection
        if database_url is None:
            database_url = os.getenv("DATABASE_URL")

        if not database_url:
            raise ValueError("DATABASE_URL environment variable not set")

        self.database_url = database_url
        self.conn_pool = None
        self._init_connection_pool()

    def _init_connection_pool(self) -> None:
        """Initialize a connection pool for efficient database access."""
        try:
            self.conn_pool = pool.SimpleConnectionPool(
                1, 5, self.database_url, connect_timeout=5
            )
            logger.info("[Kafka→TS] Connection pool initialized")
        except Exception as e:
            logger.error(f"[Kafka→TS] Failed to initialize connection pool: {e}")
            raise

    def run(self) -> None:
        """Start consuming messages and writing to TimescaleDB."""
        logger.info("[Kafka→TS] Starting consumer...")

        try:
            for message in self.consumer:
                data = message.value

                # Validate message has required fields
                if not data or "mint" not in data or "price" not in data:
                    logger.warning(f"[Kafka→TS] Invalid message, skipping: {data}")
                    continue

                # Add to batch
                self._add_to_batch(data)

                # Write batch when full
                if len(self.batch) >= self.batch_size:
                    self.write_batch()

        except KeyboardInterrupt:
            logger.info("[Kafka→TS] Consumer interrupted by user")
            self.write_batch()  # Flush remaining records
        except Exception as e:
            logger.error(f"[Kafka→TS] Consumer error: {e}")
            raise
        finally:
            self.close()

    def _add_to_batch(self, data: dict) -> None:
        """Add a message to the batch for writing."""
        try:
            timestamp_ms = data.get("timestamp", int(time.time() * 1000))
            timestamp = datetime.fromtimestamp(timestamp_ms / 1000)

            self.batch.append(
                (
                    timestamp,
                    data.get("mint"),
                    data.get("price"),
                    data.get("bid"),
                    data.get("ask"),
                    data.get("volume_1m", 0),
                    data.get("volume_5m", 0),
                    data.get("spread_bps"),
                    data.get("liquidity_usd"),
                )
            )
        except Exception as e:
            logger.error(f"[Kafka→TS] Error adding message to batch: {e}")

    def write_batch(self) -> None:
        """Write accumulated batch to TimescaleDB."""
        if not self.batch:
            return

        conn = None
        try:
            conn = self.conn_pool.getconn()
            cur = conn.cursor()

            execute_batch(
                cur,
                """
                INSERT INTO tick_prices
                (time, mint, price, bid, ask, volume_1m, volume_5m, spread_bps, liquidity_usd)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """,
                self.batch,
            )

            conn.commit()
            logger.info(f"[Kafka→TS] Wrote {len(self.batch)} records to TimescaleDB")
            self.batch = []

        except psycopg2.Error as e:
            logger.error(f"[Kafka→TS] Database error writing batch: {e}")
            if conn:
                conn.rollback()
        except Exception as e:
            logger.error(f"[Kafka→TS] Error writing batch: {e}")
        finally:
            if cur:
                cur.close()
            if conn:
                self.conn_pool.putconn(conn)

    def close(self) -> None:
        """Close consumer and connection pool."""
        # Write any remaining batch
        if self.batch:
            self.write_batch()

        try:
            self.consumer.close()
            logger.info("[Kafka→TS] Consumer closed")
        except Exception as e:
            logger.error(f"[Kafka→TS] Error closing consumer: {e}")

        try:
            if self.conn_pool:
                self.conn_pool.closeall()
            logger.info("[Kafka→TS] Connection pool closed")
        except Exception as e:
            logger.error(f"[Kafka→TS] Error closing connection pool: {e}")


import time


if __name__ == "__main__":
    # Simple test of the consumer
    logging.basicConfig(level=logging.INFO)

    consumer = KafkaToTimescale()
    consumer.run()
