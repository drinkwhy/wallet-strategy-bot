#!/usr/bin/env python3
"""
Railway Configuration Validator

This script validates that all Railway services are properly configured
and accessible before running the main application.

Usage:
    python scripts/validate_railway_config.py
"""

import os
import sys
import json
from typing import Dict, Tuple, List

# Color codes for terminal output
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RESET = '\033[0m'

class RailwayValidator:
    """Validates Railway service configuration."""

    def __init__(self):
        self.results: List[Tuple[str, bool, str]] = []
        self.required_vars = [
            'DATABASE_URL',
            'KAFKA_BOOTSTRAP_SERVERS',
            'REDIS_HOST',
            'REDIS_PORT',
        ]
        self.optional_vars = [
            'AUTO_OPTIMIZE_ENABLED',
            'SPARK_MASTER',
            'LOG_LEVEL',
        ]

    def check_environment_variables(self) -> bool:
        """Check that all required environment variables are set."""
        print(f"\n{Colors.BLUE}1. Checking Environment Variables...{Colors.RESET}")
        all_set = True

        for var in self.required_vars:
            if var in os.environ:
                value = os.environ[var]
                # Mask sensitive values
                display_value = value[:20] + "..." if len(value) > 20 else value
                self._log_pass(f"{var} = {display_value}")
            else:
                self._log_fail(f"{var} is not set")
                all_set = False

        for var in self.optional_vars:
            if var in os.environ:
                self._log_pass(f"{var} = {os.environ[var]}")
            else:
                self._log_info(f"{var} not set (optional)")

        return all_set

    def check_postgresql_connection(self) -> bool:
        """Test connection to PostgreSQL database."""
        print(f"\n{Colors.BLUE}2. Testing PostgreSQL Connection...{Colors.RESET}")

        try:
            import psycopg2
        except ImportError:
            self._log_warn("psycopg2 not installed, skipping PostgreSQL test")
            return True

        database_url = os.environ.get('DATABASE_URL')
        if not database_url:
            self._log_fail("DATABASE_URL not set")
            return False

        try:
            conn = psycopg2.connect(database_url)
            cursor = conn.cursor()

            # Test basic query
            cursor.execute("SELECT 1")
            self._log_pass("PostgreSQL connection successful")

            # Check TimescaleDB extension
            cursor.execute("SELECT COUNT(*) FROM pg_extension WHERE extname = 'timescaledb'")
            ext_count = cursor.fetchone()[0]

            if ext_count > 0:
                self._log_pass("TimescaleDB extension is enabled")
            else:
                self._log_warn("TimescaleDB extension not found")
                self._log_info("Enable with: CREATE EXTENSION timescaledb;")

            # Check tick_prices table
            cursor.execute("""
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_name = 'tick_prices'
            """)
            table_count = cursor.fetchone()[0]

            if table_count > 0:
                self._log_pass("tick_prices table exists")

                # Check row count
                cursor.execute("SELECT COUNT(*) FROM tick_prices")
                row_count = cursor.fetchone()[0]
                self._log_info(f"tick_prices has {row_count} rows")
            else:
                self._log_warn("tick_prices table not found")
                self._log_info("Create with SQL in sql/02_create_tick_prices_hypertable.sql")

            cursor.close()
            conn.close()
            return True

        except Exception as e:
            self._log_fail(f"PostgreSQL connection failed: {str(e)}")
            return False

    def check_kafka_connection(self) -> bool:
        """Test connection to Kafka."""
        print(f"\n{Colors.BLUE}3. Testing Kafka Connection...{Colors.RESET}")

        try:
            from kafka import KafkaAdminClient
            from kafka.errors import NoBrokersAvailable
        except ImportError:
            self._log_warn("kafka-python not installed, skipping Kafka test")
            return True

        bootstrap_servers = os.environ.get('KAFKA_BOOTSTRAP_SERVERS')
        if not bootstrap_servers:
            self._log_fail("KAFKA_BOOTSTRAP_SERVERS not set")
            return False

        try:
            admin_client = KafkaAdminClient(
                bootstrap_servers=bootstrap_servers.split(','),
                request_timeout_ms=5000
            )

            # Get metadata to test connection
            metadata = admin_client.describe_cluster(timeout_ms=5000)
            self._log_pass(f"Kafka connection successful (cluster id: {metadata.get('cluster_id', 'unknown')})")

            # Check for required topics
            topics = admin_client.list_topics()
            required_topics = ['market.events', 'shadow.positions', 'bot.parameters']

            for topic in required_topics:
                if topic in topics:
                    self._log_pass(f"Topic '{topic}' exists")
                else:
                    self._log_warn(f"Topic '{topic}' not found")
                    self._log_info(f"Create with: kafka-topics --create --bootstrap-server {bootstrap_servers} --topic {topic}")

            admin_client.close()
            return True

        except NoBrokersAvailable:
            self._log_fail(f"Cannot connect to Kafka brokers: {bootstrap_servers}")
            return False
        except Exception as e:
            self._log_fail(f"Kafka connection failed: {str(e)}")
            return False

    def check_redis_connection(self) -> bool:
        """Test connection to Redis."""
        print(f"\n{Colors.BLUE}4. Testing Redis Connection...{Colors.RESET}")

        try:
            import redis
        except ImportError:
            self._log_warn("redis-py not installed, skipping Redis test")
            return True

        redis_host = os.environ.get('REDIS_HOST')
        redis_port = int(os.environ.get('REDIS_PORT', 6379))
        redis_password = os.environ.get('REDIS_PASSWORD')
        redis_db = int(os.environ.get('REDIS_DB', 0))

        if not redis_host:
            self._log_fail("REDIS_HOST not set")
            return False

        try:
            r = redis.Redis(
                host=redis_host,
                port=redis_port,
                password=redis_password,
                db=redis_db,
                socket_connect_timeout=5
            )

            # Test PING
            result = r.ping()
            if result:
                self._log_pass(f"Redis connection successful ({redis_host}:{redis_port})")

                # Test SET/GET
                r.set('test_key', 'test_value')
                value = r.get('test_key').decode()
                if value == 'test_value':
                    self._log_pass("Redis SET/GET operations working")
                r.delete('test_key')

                return True
            else:
                self._log_fail("Redis PING failed")
                return False

        except Exception as e:
            self._log_fail(f"Redis connection failed: {str(e)}")
            return False

    def check_python_packages(self) -> bool:
        """Check that required Python packages are installed."""
        print(f"\n{Colors.BLUE}5. Checking Python Packages...{Colors.RESET}")

        required_packages = [
            ('kafka', 'kafka-python'),
            ('psycopg2', 'psycopg2'),
            ('redis', 'redis'),
        ]

        optional_packages = [
            ('pyspark', 'pyspark'),
            ('ray', 'ray'),
            ('pandas', 'pandas'),
        ]

        all_installed = True
        for import_name, package_name in required_packages:
            try:
                __import__(import_name)
                self._log_pass(f"{package_name} is installed")
            except ImportError:
                self._log_fail(f"{package_name} not installed")
                self._log_info(f"Install with: pip install {package_name}")
                all_installed = False

        for import_name, package_name in optional_packages:
            try:
                __import__(import_name)
                self._log_pass(f"{package_name} is installed (optional)")
            except ImportError:
                self._log_info(f"{package_name} not installed (optional)")

        return all_installed

    def run_all_checks(self) -> bool:
        """Run all validation checks."""
        print(f"\n{Colors.YELLOW}{'='*50}")
        print(f"Railway Configuration Validator")
        print(f"{'='*50}{Colors.RESET}")

        checks = [
            ("Environment Variables", self.check_environment_variables),
            ("PostgreSQL", self.check_postgresql_connection),
            ("Kafka", self.check_kafka_connection),
            ("Redis", self.check_redis_connection),
            ("Python Packages", self.check_python_packages),
        ]

        results = []
        for name, check in checks:
            try:
                result = check()
                results.append((name, result))
            except Exception as e:
                self._log_fail(f"Check '{name}' failed with error: {str(e)}")
                results.append((name, False))

        # Print summary
        self._print_summary(results)

        return all(result for _, result in results)

    def _log_pass(self, message: str) -> None:
        print(f"  {Colors.GREEN}✓{Colors.RESET} {message}")

    def _log_fail(self, message: str) -> None:
        print(f"  {Colors.RED}✗{Colors.RESET} {message}")

    def _log_warn(self, message: str) -> None:
        print(f"  {Colors.YELLOW}⚠{Colors.RESET} {message}")

    def _log_info(self, message: str) -> None:
        print(f"    {Colors.BLUE}→{Colors.RESET} {message}")

    def _print_summary(self, results: List[Tuple[str, bool]]) -> None:
        """Print validation summary."""
        print(f"\n{Colors.YELLOW}{'='*50}")
        print(f"Validation Summary")
        print(f"{'='*50}{Colors.RESET}")

        for name, passed in results:
            status = f"{Colors.GREEN}PASS{Colors.RESET}" if passed else f"{Colors.RED}FAIL{Colors.RESET}"
            print(f"  {name}: {status}")

        all_passed = all(result for _, result in results)
        if all_passed:
            print(f"\n{Colors.GREEN}✓ All checks passed! Your Railway setup is ready.{Colors.RESET}")
        else:
            print(f"\n{Colors.RED}✗ Some checks failed. Please review the errors above.{Colors.RESET}")

        return all_passed


if __name__ == "__main__":
    validator = RailwayValidator()
    success = validator.run_all_checks()
    sys.exit(0 if success else 1)
