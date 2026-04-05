#!/bin/bash
# Railway Deployment Verification Script
# This script tests all connections to verify your Railway setup is working

set -e

echo "=========================================="
echo "Railway Setup Verification"
echo "=========================================="
echo ""

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test counter
TESTS_PASSED=0
TESTS_FAILED=0

# Function to test and report
test_result() {
    local test_name=$1
    local result=$2
    local expected=$3

    if [ "$result" == "$expected" ]; then
        echo -e "${GREEN}✓ PASS${NC}: $test_name"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}✗ FAIL${NC}: $test_name"
        echo "  Expected: $expected"
        echo "  Got: $result"
        ((TESTS_FAILED++))
    fi
}

# ============================================================================
# 1. CHECK ENVIRONMENT VARIABLES
# ============================================================================
echo ""
echo -e "${YELLOW}1. Checking Environment Variables...${NC}"

if [ -z "$DATABASE_URL" ]; then
    echo -e "${RED}✗ DATABASE_URL not set${NC}"
    ((TESTS_FAILED++))
else
    echo -e "${GREEN}✓ DATABASE_URL is set${NC}"
    ((TESTS_PASSED++))
fi

if [ -z "$KAFKA_BOOTSTRAP_SERVERS" ]; then
    echo -e "${RED}✗ KAFKA_BOOTSTRAP_SERVERS not set${NC}"
    ((TESTS_FAILED++))
else
    echo -e "${GREEN}✓ KAFKA_BOOTSTRAP_SERVERS is set${NC}"
    ((TESTS_PASSED++))
fi

if [ -z "$REDIS_HOST" ]; then
    echo -e "${RED}✗ REDIS_HOST not set${NC}"
    ((TESTS_FAILED++))
else
    echo -e "${GREEN}✓ REDIS_HOST is set${NC}"
    ((TESTS_PASSED++))
fi

# ============================================================================
# 2. TEST TIMESCALEDB CONNECTION
# ============================================================================
echo ""
echo -e "${YELLOW}2. Testing TimescaleDB Connection...${NC}"

if command -v psql &> /dev/null; then
    # Test basic connection
    if psql "$DATABASE_URL" -c "SELECT 1" &> /dev/null; then
        echo -e "${GREEN}✓ PostgreSQL connection successful${NC}"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}✗ PostgreSQL connection failed${NC}"
        ((TESTS_FAILED++))
    fi

    # Check TimescaleDB extension
    EXT_CHECK=$(psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM pg_extension WHERE extname = 'timescaledb';" -t 2>/dev/null || echo "0")
    if [ "$EXT_CHECK" -gt "0" ]; then
        echo -e "${GREEN}✓ TimescaleDB extension is enabled${NC}"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}✗ TimescaleDB extension not found${NC}"
        echo "  Run: psql \"\$DATABASE_URL\" -c \"CREATE EXTENSION IF NOT EXISTS timescaledb;\""
        ((TESTS_FAILED++))
    fi

    # Check tick_prices table
    TABLE_CHECK=$(psql "$DATABASE_URL" -c "\dt tick_prices" -t 2>/dev/null | grep -c "tick_prices" || echo "0")
    if [ "$TABLE_CHECK" -gt "0" ]; then
        echo -e "${GREEN}✓ tick_prices table exists${NC}"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}✗ tick_prices table not found${NC}"
        ((TESTS_FAILED++))
    fi
else
    echo -e "${YELLOW}⚠ psql not installed, skipping PostgreSQL tests${NC}"
fi

# ============================================================================
# 3. TEST KAFKA CONNECTION
# ============================================================================
echo ""
echo -e "${YELLOW}3. Testing Kafka Connection...${NC}"

if command -v kafka-topics &> /dev/null; then
    # Test Kafka connectivity
    if timeout 5 kafka-topics --list --bootstrap-server "$KAFKA_BOOTSTRAP_SERVERS" &> /dev/null; then
        echo -e "${GREEN}✓ Kafka connection successful${NC}"
        ((TESTS_PASSED++))

        # Check required topics
        TOPICS=$(kafka-topics --list --bootstrap-server "$KAFKA_BOOTSTRAP_SERVERS")

        if echo "$TOPICS" | grep -q "market.events"; then
            echo -e "${GREEN}✓ market.events topic exists${NC}"
            ((TESTS_PASSED++))
        else
            echo -e "${RED}✗ market.events topic not found${NC}"
            ((TESTS_FAILED++))
        fi

        if echo "$TOPICS" | grep -q "shadow.positions"; then
            echo -e "${GREEN}✓ shadow.positions topic exists${NC}"
            ((TESTS_PASSED++))
        else
            echo -e "${RED}✗ shadow.positions topic not found${NC}"
            ((TESTS_FAILED++))
        fi

        if echo "$TOPICS" | grep -q "bot.parameters"; then
            echo -e "${GREEN}✓ bot.parameters topic exists${NC}"
            ((TESTS_PASSED++))
        else
            echo -e "${RED}✗ bot.parameters topic not found${NC}"
            ((TESTS_FAILED++))
        fi
    else
        echo -e "${RED}✗ Kafka connection failed${NC}"
        echo "  Check KAFKA_BOOTSTRAP_SERVERS: $KAFKA_BOOTSTRAP_SERVERS"
        ((TESTS_FAILED++))
    fi
else
    echo -e "${YELLOW}⚠ Kafka CLI tools not installed, skipping Kafka tests${NC}"
fi

# ============================================================================
# 4. TEST REDIS CONNECTION
# ============================================================================
echo ""
echo -e "${YELLOW}4. Testing Redis Connection...${NC}"

if command -v redis-cli &> /dev/null; then
    # Build redis-cli command with authentication
    REDIS_CMD="redis-cli -h $REDIS_HOST -p ${REDIS_PORT:-6379}"

    if [ -n "$REDIS_PASSWORD" ]; then
        REDIS_CMD="$REDIS_CMD -a $REDIS_PASSWORD"
    fi

    # Test Redis PING
    PING_RESULT=$(timeout 5 $REDIS_CMD PING 2>/dev/null || echo "FAILED")

    if [ "$PING_RESULT" == "PONG" ]; then
        echo -e "${GREEN}✓ Redis connection successful${NC}"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}✗ Redis connection failed${NC}"
        echo "  Check REDIS_HOST: $REDIS_HOST"
        echo "  Check REDIS_PORT: ${REDIS_PORT:-6379}"
        ((TESTS_FAILED++))
    fi
else
    echo -e "${YELLOW}⚠ redis-cli not installed, skipping Redis tests${NC}"
fi

# ============================================================================
# 5. CHECK REQUIRED PYTHON PACKAGES
# ============================================================================
echo ""
echo -e "${YELLOW}5. Checking Required Python Packages...${NC}"

if command -v python3 &> /dev/null; then
    packages=("kafka" "psycopg2" "redis" "pyspark")

    for pkg in "${packages[@]}"; do
        if python3 -c "import ${pkg}" 2>/dev/null; then
            echo -e "${GREEN}✓ $pkg installed${NC}"
            ((TESTS_PASSED++))
        else
            echo -e "${RED}✗ $pkg not installed${NC}"
            echo "  Install with: pip install $pkg"
            ((TESTS_FAILED++))
        fi
    done
else
    echo -e "${YELLOW}⚠ python3 not found, skipping package checks${NC}"
fi

# ============================================================================
# SUMMARY
# ============================================================================
echo ""
echo "=========================================="
echo "Verification Summary"
echo "=========================================="
echo -e "${GREEN}Passed: $TESTS_PASSED${NC}"
echo -e "${RED}Failed: $TESTS_FAILED${NC}"

if [ $TESTS_FAILED -eq 0 ]; then
    echo -e "${GREEN}✓ All tests passed! Your Railway setup is ready.${NC}"
    exit 0
else
    echo -e "${RED}✗ Some tests failed. Please review the errors above.${NC}"
    exit 1
fi
