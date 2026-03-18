# =============================================================================
# Vexa - Full Local Startup Script (GPU Transcription) - Windows PowerShell
# =============================================================================
# Usage:
#   .\start-local.ps1          # Build & start everything
#   .\start-local.ps1 -Down    # Stop everything
# =============================================================================

param(
    [switch]$Down
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$ComposeFile = "docker-compose.local-full.yml"

# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------
if ($Down) {
    Write-Host "Stopping all Vexa services..."
    docker compose -f $ComposeFile down
    Write-Host "Done."
    exit 0
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
Write-Host "=== Vexa Local Full Stack ===" -ForegroundColor Cyan
Write-Host ""

# Check Docker
try {
    docker info 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw }
} catch {
    Write-Host "ERROR: Docker is not running. Please start Docker Desktop first." -ForegroundColor Red
    exit 1
}

# Check GPU
Write-Host "Checking GPU availability..."
try {
    $gpuInfo = nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  NVIDIA GPU detected:" -ForegroundColor Green
        $gpuInfo | ForEach-Object { Write-Host "    $_" }
    } else {
        throw
    }
} catch {
    Write-Host "  WARNING: nvidia-smi not found. GPU transcription may fail." -ForegroundColor Yellow
    Write-Host "  Make sure NVIDIA drivers and NVIDIA Container Toolkit are installed."
    $reply = Read-Host "Continue anyway? (y/N)"
    if ($reply -notmatch '^[Yy]$') { exit 1 }
}
Write-Host ""

# ---------------------------------------------------------------------------
# Build vexa-bot image (launched dynamically by bot-manager via Docker socket)
# ---------------------------------------------------------------------------
Write-Host "Building vexa-bot image..." -ForegroundColor Cyan
docker build --platform linux/amd64 -t vexa-bot:dev -f services/vexa-bot/Dockerfile ./services/vexa-bot
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to build vexa-bot image." -ForegroundColor Red
    exit 1
}
Write-Host ""

# ---------------------------------------------------------------------------
# Build and start all services
# ---------------------------------------------------------------------------
Write-Host "Building and starting all services..." -ForegroundColor Cyan
docker compose -f $ComposeFile up -d --build
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to start services." -ForegroundColor Red
    exit 1
}
Write-Host ""

# ---------------------------------------------------------------------------
# Wait for DB and run migrations
# ---------------------------------------------------------------------------
Write-Host "Waiting for PostgreSQL to be ready..." -ForegroundColor Cyan
$ready = $false
for ($i = 1; $i -le 30; $i++) {
    docker compose -f $ComposeFile exec -T postgres pg_isready -U postgres -d vexa 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  PostgreSQL is ready." -ForegroundColor Green
        $ready = $true
        break
    }
    Start-Sleep -Seconds 5
}
if (-not $ready) {
    Write-Host "  ERROR: PostgreSQL did not become ready in 150s." -ForegroundColor Red
    Write-Host "  Check logs: docker compose -f $ComposeFile logs postgres"
    exit 1
}
Write-Host ""

Write-Host "Running database migrations..." -ForegroundColor Cyan

# Repair stale alembic state
docker compose -f $ComposeFile exec -T transcription-collector python /app/libs/shared-models/fix_alembic_version.py --repair-stale 2>&1 | Out-Null

# Check if alembic_version table exists
$hasAlembic = docker compose -f $ComposeFile exec -T postgres psql -U postgres -d vexa -t -c "SELECT 1 FROM information_schema.tables WHERE table_name = 'alembic_version';" 2>&1
$hasAlembic = ($hasAlembic -join "").Trim()

if ($hasAlembic -eq "1") {
    Write-Host "  Alembic table found, running migrations..."
    docker compose -f $ComposeFile exec -T transcription-collector alembic -c /app/alembic.ini upgrade head
} else {
    Write-Host "  Fresh database, initializing schema..."
    docker compose -f $ComposeFile exec -T transcription-collector python -c "import asyncio; from shared_models.database import init_db; asyncio.run(init_db())"
    docker compose -f $ComposeFile exec -T transcription-collector python /app/libs/shared-models/fix_alembic_version.py --create-if-missing 2>&1 | Out-Null
}
Write-Host ""

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host "============================================================================" -ForegroundColor Green
Write-Host "  Vexa is running!" -ForegroundColor Green
Write-Host "============================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Dashboard:     http://localhost:3001"
Write-Host "  API Docs:      http://localhost:8056/docs"
Write-Host "  Admin API:     http://localhost:8057/docs"
Write-Host "  MinIO Console: http://localhost:9001  (vexa-access-key / vexa-secret-key)"
Write-Host "  MCP:           http://localhost:18888"
Write-Host ""
Write-Host "  Admin Token:   vexa-local-admin-token" -ForegroundColor Yellow
Write-Host ""
Write-Host "  GPU Transcription: large-v3-turbo model (float16)"
Write-Host ""
Write-Host "  Useful commands:"
Write-Host "    docker compose -f $ComposeFile logs -f              # Tail all logs"
Write-Host "    docker compose -f $ComposeFile logs -f whisperlive  # WhisperLive logs"
Write-Host "    docker compose -f $ComposeFile ps                   # Service status"
Write-Host "    .\start-local.ps1 -Down                             # Stop everything"
Write-Host "============================================================================" -ForegroundColor Green
