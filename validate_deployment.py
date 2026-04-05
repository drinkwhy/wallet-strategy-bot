#!/usr/bin/env python3
"""
Validate that all services are connected and ready
"""

import os
import psycopg2
import redis
import sys

def load_env():
    """Load environment variables from .env file"""
    from dotenv import load_dotenv
    load_dotenv()

def test_postgres():
    """Test PostgreSQL connection"""
    print("\n📊 Testing PostgreSQL Connection...")
    try:
        db_url = os.getenv('DATABASE_URL')
        if not db_url:
            print("  ❌ DATABASE_URL not set")
            return False

        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute("SELECT version();")
        version = cur.fetchone()[0]
        print(f"  ✅ Connected to PostgreSQL")

        # Check if tick_prices table exists
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'tick_prices'
            );
        """)
        table_exists = cur.fetchone()[0]

        if table_exists:
            cur.execute("SELECT COUNT(*) FROM tick_prices;")
            row_count = cur.fetchone()[0]
            print(f"  ✅ tick_prices table exists ({row_count} rows)")
        else:
            print("  ⚠️  tick_prices table not found (run setup_database_basic.py)")

        cur.close()
        conn.close()
        return True

    except Exception as e:
        print(f"  ❌ PostgreSQL Error: {str(e)}")
        return False

def test_redis():
    """Test Redis connection"""
    print("\n🔴 Testing Redis Connection...")
    try:
        redis_url = os.getenv('REDIS_URL')
        if not redis_url:
            print("  ❌ REDIS_URL not set")
            return False

        r = redis.from_url(redis_url)
        r.ping()
        print(f"  ✅ Connected to Redis")

        # Check circuit breaker flag
        breaker = r.get('auto_optimize_disabled:test')
        print(f"  ✅ Redis working (circuit breaker ready)")

        return True

    except Exception as e:
        print(f"  ❌ Redis Error: {str(e)}")
        return False

def main():
    print("\n╔═══════════════════════════════════════════════════════════════╗")
    print("║           Deployment Validation - Service Check              ║")
    print("╚═══════════════════════════════════════════════════════════════╝")

    load_env()

    postgres_ok = test_postgres()
    redis_ok = test_redis()

    print("\n" + "="*65)
    print("SUMMARY")
    print("="*65)
    print(f"PostgreSQL:  {'✅ READY' if postgres_ok else '❌ FAILED'}")
    print(f"Redis:       {'✅ READY' if redis_ok else '❌ FAILED'}")

    if postgres_ok and redis_ok:
        print("\n✅ ALL SERVICES READY FOR DEPLOYMENT!\n")
        print("Next Steps:")
        print("  1. Run: python app.py")
        print("  2. Open: http://localhost:5000")
        print("  3. Watch dashboard for optimization metrics")
        return 0
    else:
        print("\n❌ Some services not ready. Fix errors above.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
