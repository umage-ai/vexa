#!/bin/bash
# =============================================================================
# Vexa - Full Local Startup Script (GPU Transcription)
# =============================================================================
# This script builds and starts the entire Vexa stack locally.
#
# Prerequisites:
#   - Docker with Compose v2
#   - NVIDIA GPU drivers + NVIDIA Container Toolkit (for GPU transcription)
#   - Ports 3001, 8056, 8057, 9000, 9001 available
#
# Usage:
#   ./start-local.sh          # Build & start everything
#   ./start-local.sh --down   # Stop everything
# =============================================================================

set -e
cd "$(dirname "$0")"

COMPOSE_FILE="docker-compose.local-full.yml"

# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------
if [ "$1" = "--down" ] || [ "$1" = "down" ]; then
    echo "Stopping all Vexa services..."
    docker compose -f "$COMPOSE_FILE" down
    echo "Done."
    exit 0
fi

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
echo "=== Vexa Local Full Stack ==="
echo ""

# Check Docker
if ! docker info > /dev/null 2>&1; then
    echo "ERROR: Docker is not running. Please start Docker first."
    exit 1
fi

# Check GPU availability
echo "Checking GPU availability..."
if nvidia-smi > /dev/null 2>&1; then
    echo "  NVIDIA GPU detected:"
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | sed 's/^/    /'
else
    echo "  WARNING: nvidia-smi not found. GPU transcription may fail."
    echo "  Make sure NVIDIA drivers and NVIDIA Container Toolkit are installed."
    echo ""
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi
echo ""

# ---------------------------------------------------------------------------
# Build vexa-bot image (launched dynamically by bot-manager via Docker socket)
# ---------------------------------------------------------------------------
echo "Building vexa-bot image..."
docker build --platform linux/amd64 -t vexa-bot:dev -f services/vexa-bot/Dockerfile ./services/vexa-bot
echo ""

# ---------------------------------------------------------------------------
# Build and start all services
# ---------------------------------------------------------------------------
echo "Building and starting all services..."
docker compose -f "$COMPOSE_FILE" up -d --build
echo ""

# ---------------------------------------------------------------------------
# Wait for DB and run migrations
# ---------------------------------------------------------------------------
echo "Waiting for PostgreSQL to be ready..."
for i in $(seq 1 30); do
    if docker compose -f "$COMPOSE_FILE" exec -T postgres pg_isready -U postgres -d vexa -q 2>/dev/null; then
        echo "  PostgreSQL is ready."
        break
    fi
    if [ "$i" = "30" ]; then
        echo "  ERROR: PostgreSQL did not become ready in 150s."
        echo "  Check logs: docker compose -f $COMPOSE_FILE logs postgres"
        exit 1
    fi
    sleep 5
done
echo ""

echo "Running database migrations..."
# First try to repair any stale alembic state
docker compose -f "$COMPOSE_FILE" exec -T transcription-collector \
    python /app/libs/shared-models/fix_alembic_version.py --repair-stale 2>/dev/null || true

# Check if alembic_version table exists
HAS_ALEMBIC=$(docker compose -f "$COMPOSE_FILE" exec -T postgres \
    psql -U postgres -d vexa -t -c \
    "SELECT 1 FROM information_schema.tables WHERE table_name = 'alembic_version';" 2>/dev/null | tr -d '[:space:]' || echo "")

if [ "$HAS_ALEMBIC" = "1" ]; then
    echo "  Alembic table found, running migrations..."
    docker compose -f "$COMPOSE_FILE" exec -T transcription-collector \
        alembic -c /app/alembic.ini upgrade head
else
    echo "  Fresh database, initializing schema..."
    docker compose -f "$COMPOSE_FILE" exec -T transcription-collector \
        python -c "import asyncio; from shared_models.database import init_db; asyncio.run(init_db())"
    docker compose -f "$COMPOSE_FILE" exec -T transcription-collector \
        python /app/libs/shared-models/fix_alembic_version.py --create-if-missing 2>/dev/null || true
fi
echo ""

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "============================================================================"
echo "  Vexa is running!"
echo "============================================================================"
echo ""
echo "  Dashboard:    http://localhost:3001"
echo "  API Docs:     http://localhost:8056/docs"
echo "  Admin API:    http://localhost:8057/docs"
echo "  MinIO Console: http://localhost:9001  (vexa-access-key / vexa-secret-key)"
echo "  MCP:          http://localhost:18888"
echo ""
echo "  Admin Token:  vexa-local-admin-token"
echo ""
echo "  GPU Transcription: large-v3-turbo model (float16)"
echo ""
echo "  Useful commands:"
echo "    docker compose -f $COMPOSE_FILE logs -f              # Tail all logs"
echo "    docker compose -f $COMPOSE_FILE logs -f whisperlive  # WhisperLive logs"
echo "    docker compose -f $COMPOSE_FILE ps                   # Service status"
echo "    ./start-local.sh --down                              # Stop everything"
echo "============================================================================"
