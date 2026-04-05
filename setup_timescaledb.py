#!/usr/bin/env python3
"""
Automated TimescaleDB Setup Script
Run this to enable TimescaleDB and create the tick_prices hypertable
"""

import psycopg2
import sys
from datetime import datetime

# Your DATABASE_URL
DATABASE_URL = "postgresql://postgres:LUQlXQokLesAZMZhnWjVnowWEQjVUToE@gondola.proxy.rlwy.net:16787/railway"

def print_step(step_num, message):
    """Print a step message"""
    print(f"\n{'='*70}")
    print(f"STEP {step_num}: {message}")
    print(f"{'='*70}")

def print_success(message):
    """Print success message"""
    print(f"✅ {message}")

def print_error(message):
    """Print error message"""
    print(f"❌ {message}")
    sys.exit(1)

def main():
    print("\n")
    print("╔" + "="*68 + "╗")
    print("║" + " "*68 + "║")
    print("║" + "  TimescaleDB Setup - Automated Installation".center(68) + "║")
    print("║" + " "*68 + "║")
    print("╚" + "="*68 + "╝")

    try:
        # Connect to database
        print_step(1, "Connecting to PostgreSQL Database")
        print(f"Connecting to: postgresql://postgres:***@gondola.proxy.rlwy.net:16787/railway")

        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        print_success("Connected to PostgreSQL")

        # Step 2: Enable TimescaleDB
        print_step(2, "Enable TimescaleDB Extension")
        print("Executing: CREATE EXTENSION IF NOT EXISTS timescaledb;")
        cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")
        conn.commit()
        print_success("TimescaleDB extension enabled")

        # Verify extension
        cur.execute("SELECT * FROM pg_extension WHERE extname = 'timescaledb';")
        result = cur.fetchone()
        if result:
            print_success(f"Verified: TimescaleDB is installed (extname: {result[0]})")
        else:
            print_error("TimescaleDB extension not found after installation")

        # Step 3: Create tick_prices table
        print_step(3, "Create tick_prices Table")
        print("Creating table structure...")

        sql = """
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
        """
        cur.execute(sql)
        conn.commit()
        print_success("tick_prices table created")

        # Step 4: Create hypertable
        print_step(4, "Convert to TimescaleDB Hypertable")
        print("Executing: SELECT create_hypertable('tick_prices', 'time', if_not_exists => TRUE);")

        cur.execute("SELECT create_hypertable('tick_prices', 'time', if_not_exists => TRUE);")
        conn.commit()
        print_success("Hypertable created with 'time' as time column")

        # Step 5: Create indexes
        print_step(5, "Create Indexes for Performance")

        index_commands = [
            ("CREATE INDEX IF NOT EXISTS idx_tick_prices_mint_time ON tick_prices (mint, time DESC);",
             "Index: (mint, time DESC)"),
            ("CREATE INDEX IF NOT EXISTS idx_tick_prices_time ON tick_prices (time DESC);",
             "Index: (time DESC)")
        ]

        for sql, desc in index_commands:
            print(f"Creating: {desc}")
            cur.execute(sql)
            conn.commit()
            print_success(f"Created {desc}")

        # Step 6: Verify everything
        print_step(6, "Verification & Summary")

        # Check table exists
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'tick_prices'
            );
        """)
        table_exists = cur.fetchone()[0]
        print_success(f"tick_prices table exists: {table_exists}")

        # Check hypertable
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM timescaledb_information.hypertables
                WHERE hypertable_name = 'tick_prices'
            );
        """)
        is_hypertable = cur.fetchone()[0]
        print_success(f"Is TimescaleDB hypertable: {is_hypertable}")

        # Get table info
        cur.execute("SELECT COUNT(*) FROM tick_prices;")
        row_count = cur.fetchone()[0]
        print_success(f"Current rows in tick_prices: {row_count}")

        # Get indexes
        cur.execute("""
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'tick_prices' AND indexname LIKE 'idx_%';
        """)
        indexes = cur.fetchall()
        print_success(f"Indexes created: {len(indexes)}")
        for idx in indexes:
            print(f"  - {idx[0]}")

        # Step 7: Final summary
        print_step(7, "Setup Complete!")
        print("""
╔══════════════════════════════════════════════════════════════╗
║                   ✅ ALL SETUP COMPLETE                      ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  ✓ TimescaleDB extension enabled                            ║
║  ✓ tick_prices hypertable created                           ║
║  ✓ Indexes created for performance                          ║
║  ✓ Database ready for market data ingestion                 ║
║                                                              ║
║  Next Steps:                                                 ║
║  1. Create Kafka service in Railway                          ║
║  2. Create Redis service in Railway                          ║
║  3. Configure environment variables                          ║
║  4. Run: python connectors/kafka_to_timescale.py            ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
        """)

        cur.close()
        conn.close()

        return 0

    except Exception as e:
        print_error(f"Error: {str(e)}")
        return 1

if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
