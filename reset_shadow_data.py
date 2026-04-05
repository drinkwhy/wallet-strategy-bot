#!/usr/bin/env python3
"""
Reset shadow trades and backtest data
Clears old data to start fresh with new Kafka/Spark system
"""

import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv('DATABASE_URL')

def confirm():
    """Ask for confirmation before deleting"""
    print("\n⚠️  WARNING: This will DELETE all shadow trading and backtest data!")
    print("   This action CANNOT be undone.\n")
    response = input("Type 'YES' to confirm: ").strip().upper()
    return response == "YES"

def main():
    if not confirm():
        print("❌ Cancelled.")
        return 1

    print("\n🗑️  Resetting shadow data...\n")

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        # Tables to clear
        tables = [
            'shadow_positions',
            'shadow_decisions',
            'shadow_zero_movement_closes',
            'backtest_trades',
            'backtest_runs',
            'optimization_decisions',
            'parameter_sweep_results',  # AI approval gate metrics
            'ai_approval_decisions'  # Auto-tune approval log
        ]

        for table in tables:
            print(f"Clearing {table}...", end=" ")
            try:
                cur.execute(f"DELETE FROM {table};")
                count = cur.rowcount
                conn.commit()
                print(f"✅ ({count} rows deleted)")
            except psycopg2.errors.UndefinedTable:
                print("⚠️  (table doesn't exist)")
                conn.commit()  # Clear transaction state
            except Exception as e:
                conn.rollback()  # Clear failed transaction
                print(f"⚠️  (skipped - {type(e).__name__})")
                conn = psycopg2.connect(DATABASE_URL)  # Reconnect
                cur = conn.cursor()

        print("\n✅ Reset complete!\n")
        print("System ready for:")
        print("  • New shadow trading with fresh baseline")
        print("  • New Spark optimization starting fresh")
        print("  • Clean comparison metrics")

        cur.close()
        conn.close()
        return 0

    except Exception as e:
        print(f"\n❌ Error: {e}")
        return 1

if __name__ == "__main__":
    exit(main())
