"""
Kafka Producer for Real-Time Market Price Data

This module handles sending live price updates from the scanner to a Kafka topic.
Messages are formatted as JSON and include market metadata like bid/ask spreads.
"""

import json
import os
import time
import logging
from typing import Optional, Dict, Any
from kafka import KafkaProducer
from kafka.errors import KafkaError

logger = logging.getLogger(__name__)


class PriceProducer:
    """Kafka producer for streaming market price updates."""

    def __init__(self, bootstrap_servers: Optional[str] = None, topic: str = "market-prices"):
        """
        Initialize the Kafka producer.

        Args:
            bootstrap_servers: Comma-separated list of Kafka broker addresses.
                              Defaults to KAFKA_BOOTSTRAP_SERVERS env var or 'localhost:9092'
            topic: Kafka topic to publish to. Defaults to 'market-prices'
        """
        self.topic = topic

        if bootstrap_servers is None:
            bootstrap_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")

        # Handle both string and list inputs
        if isinstance(bootstrap_servers, str):
            bootstrap_servers = [s.strip() for s in bootstrap_servers.split(",")]

        logger.info(f"[Kafka Producer] Initializing with bootstrap servers: {bootstrap_servers}")

        try:
            self.producer = KafkaProducer(
                bootstrap_servers=bootstrap_servers,
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                acks="all",
                retries=3,
                api_version=(0, 10, 1),  # Compatibility with older brokers
            )
            logger.info("[Kafka Producer] Connected successfully")
        except Exception as e:
            logger.error(f"[Kafka Producer] Failed to initialize: {e}")
            raise

    def send_price_update(
        self,
        mint: str,
        price: float,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
        volume_1m: Optional[float] = None,
        volume_5m: Optional[float] = None,
        liquidity_usd: Optional[float] = None,
    ) -> bool:
        """
        Send a single price update to Kafka.

        Args:
            mint: Token mint address
            price: Current price
            bid: Bid price (optional)
            ask: Ask price (optional)
            volume_1m: 1-minute trading volume (optional)
            volume_5m: 5-minute trading volume (optional)
            liquidity_usd: Liquidity in USD (optional)

        Returns:
            True if sent successfully, False otherwise
        """
        msg = {
            "mint": mint,
            "timestamp": int(time.time() * 1000),  # milliseconds
            "price": price,
            "bid": bid,
            "ask": ask,
            "volume_1m": volume_1m,
            "volume_5m": volume_5m,
            "liquidity_usd": liquidity_usd,
            "spread_bps": self._calculate_spread_bps(bid, ask),
        }

        try:
            future = self.producer.send(self.topic, value=msg)
            # Wait briefly for the send to complete (timeout after 5 seconds)
            future.get(timeout=5)
            logger.debug(f"[Kafka Producer] Sent price update for {mint}: ${price}")
            return True
        except Exception as e:
            logger.error(f"[Kafka Producer] Failed to send price for {mint}: {e}")
            return False

    def send_batch(self, messages: list[Dict[str, Any]]) -> int:
        """
        Send multiple price updates to Kafka.

        Args:
            messages: List of message dicts with keys: mint, price, bid, ask, volume_1m, volume_5m, liquidity_usd

        Returns:
            Number of messages successfully sent
        """
        sent_count = 0
        for msg_dict in messages:
            if self.send_price_update(
                mint=msg_dict.get("mint"),
                price=msg_dict.get("price"),
                bid=msg_dict.get("bid"),
                ask=msg_dict.get("ask"),
                volume_1m=msg_dict.get("volume_1m"),
                volume_5m=msg_dict.get("volume_5m"),
                liquidity_usd=msg_dict.get("liquidity_usd"),
            ):
                sent_count += 1

        logger.info(f"[Kafka Producer] Sent {sent_count}/{len(messages)} messages")
        return sent_count

    def flush(self, timeout_ms: int = 10000) -> None:
        """
        Flush pending messages.

        Args:
            timeout_ms: Timeout in milliseconds
        """
        try:
            self.producer.flush(timeout_ms / 1000)
            logger.info("[Kafka Producer] Flushed pending messages")
        except Exception as e:
            logger.error(f"[Kafka Producer] Error flushing messages: {e}")

    def close(self) -> None:
        """Close the producer connection."""
        try:
            self.producer.close()
            logger.info("[Kafka Producer] Closed connection")
        except Exception as e:
            logger.error(f"[Kafka Producer] Error closing connection: {e}")

    @staticmethod
    def _calculate_spread_bps(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
        """Calculate bid-ask spread in basis points."""
        if bid is None or ask is None or bid <= 0:
            return None
        return ((ask - bid) / bid) * 10000


if __name__ == "__main__":
    # Simple test of the producer
    logging.basicConfig(level=logging.INFO)

    producer = PriceProducer()

    # Send a test price update
    producer.send_price_update(
        mint="TEST123",
        price=100.5,
        bid=100.4,
        ask=100.6,
        volume_1m=1000.0,
        liquidity_usd=50000.0,
    )

    producer.flush()
    producer.close()
