#!/usr/bin/env python3
"""
Basic Database Setup (No TimescaleDB needed)
Creates tick_prices table as regular PostgreSQL table
"""

import psycopg2
import sys

DATABASE_URL = "postgresql://postgres:LUQlXQokLesAZMZhnWjVnowWEQjVUToE@gondola.proxy.rlwy.net:16787/railway"

def main():
    print("\n╔══════════════════════════════════════════════════════════════╗")
    print("║           PostgreSQL Database Setup (No TimescaleDB)        ║")
    print("╚══════════════════════════════════════════════════════════════╝\n")

    try:
        print("✓ Connecting to PostgreSQL...")
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        print("✓ Connected!\n")

        # Create tick_prices table
        print("✓ Creating tick_prices table...")
        sql = """
        CREATE TABLE IF NOT EXISTS tick_prices (
            id SERIAL PRIMARY KEY,
            time TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
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
        print("  ✓ Table created\n")

        # Create indexes
        print("✓ Creating indexes...")
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_tick_prices_mint_time ON tick_prices (mint, time DESC);",
            "CREATE INDEX IF NOT EXISTS idx_tick_prices_time ON tick_prices (time DESC);",
            "CREATE INDEX IF NOT EXISTS idx_tick_prices_mint ON tick_prices (mint);"
        ]

        for sql in indexes:
            cur.execute(sql)
            conn.commit()
            print(f"  ✓ {sql.split('ON')[0].strip()}")

        # Verify
        print("\n✓ Verification:")
        cur.execute("SELECT COUNT(*) FROM tick_prices;")
        print(f"  ✓ tick_prices table exists (rows: {cur.fetchone()[0]})")

        cur.execute("""
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'tick_prices' AND indexname LIKE 'idx_%';
        """)
        indexes = cur.fetchall()
        print(f"  ✓ Indexes created: {len(indexes)}")

        print("\n╔══════════════════════════════════════════════════════════════╗")
        print("║                   ✅ SETUP COMPLETE!                        ║")
        print("╠══════════════════════════════════════════════════════════════╣")
        print("║                                                              ║")
        print("║  ✓ tick_prices table ready                                  ║")
        print("║  ✓ Indexes created for performance                          ║")
        print("║  ✓ Database ready for Kafka consumer                        ║")
        print("║                                                              ║")
        print("║  Next: Create Kafka and Redis services in Railway           ║")
        print("║                                                              ║")
        print("╚══════════════════════════════════════════════════════════════╝\n")

        cur.close()
        conn.close()
        return 0

    except Exception as e:
        print(f"\n❌ Error: {str(e)}\n")
        return 1

if __name__ == "__main__":
    exit(main())
