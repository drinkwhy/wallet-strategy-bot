#!/bin/bash

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    echo "❌ Docker is not installed. Please install Docker Desktop first:"
    echo "   https://www.docker.com/products/docker-desktop"
    exit 1
fi

echo "╔════════════════════════════════════════════════════════════╗"
echo "║          Starting Kafka + Redis with Docker               ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""

# Start services
docker-compose up -d

echo ""
echo "✅ Services starting..."
echo ""
sleep 5

# Check if running
echo "Checking service status..."
docker-compose ps

echo ""
echo "✅ Kafka running on: localhost:9092"
echo "✅ Redis running on: localhost:6379"
echo "✅ Zookeeper running on: localhost:2181"
echo ""
echo "To stop services, run: docker-compose down"
