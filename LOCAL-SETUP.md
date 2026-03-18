# Vexa - Full Local Setup (Windows + GPU)

This fork adds a fully self-contained local deployment that runs the **entire Vexa stack** in Docker, including GPU-accelerated transcription and a local LLM for AI features via Ollama. No external services required.

## What's Included

| Service | Description | Port |
|---|---|---|
| **Dashboard** | Next.js web UI | [localhost:3001](http://localhost:3001) |
| **API Gateway** | REST API | [localhost:8056/docs](http://localhost:8056/docs) |
| **Admin API** | User/token management | [localhost:8057/docs](http://localhost:8057/docs) |
| **Bot Manager** | Spawns meeting bots via Docker socket | internal |
| **WhisperLive** | Real-time audio WebSocket relay | internal |
| **Transcription Worker** | GPU-accelerated faster-whisper (`large-v3-turbo`, float16) | internal |
| **Transcription Collector** | Stores transcription segments in Postgres | internal |
| **TTS Service** | Text-to-speech (optional, needs OpenAI key) | internal |
| **MCP** | MCP agent toolkit | [localhost:18888](http://localhost:18888) |
| **PostgreSQL** | Database | internal |
| **Redis** | Message bus / streams | internal |
| **MinIO** | S3-compatible object storage | [localhost:9001](http://localhost:9001) |
| **Ollama Proxy** | Translates Vercel AI SDK requests for Ollama | internal |

## Prerequisites

- **Docker Desktop** with WSL2 backend (Windows) or Docker Engine (Linux)
- **NVIDIA GPU** with drivers installed
- **NVIDIA Container Toolkit** ([install guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html))
- **Ollama** running on the host (for AI chat features) with a model pulled, e.g. `ollama pull qwen3.5:27b`

## Quick Start (Windows)

```powershell
cd C:\path\to\repo

# 1. Build the bot image (spawned dynamically to join meetings)
docker build --platform linux/amd64 -t vexa-bot:dev -f services/vexa-bot/Dockerfile ./services/vexa-bot

# 2. Start all services
docker compose -f docker-compose.local-full.yml up -d --build

# 3. Wait for Postgres, then initialize the database
#    (wait ~30s for all services to be healthy)
docker compose -f docker-compose.local-full.yml exec -T transcription-collector `
  python -c "import asyncio; from shared_models.database import init_db; asyncio.run(init_db())"
```

Or use the startup script which handles all of the above:

```powershell
.\start-local.ps1          # Build & start everything
.\start-local.ps1 -Down    # Stop everything
```

## Quick Start (Linux/macOS)

```bash
cd /path/to/repo
./start-local.sh           # Build & start everything
./start-local.sh --down    # Stop everything
```

## First-Time Setup

1. Start all services (see Quick Start above)
2. Open **http://localhost:3001** (Dashboard)
3. Go to Admin settings and authenticate with token: `vexa-local-admin-token`
4. Create a user and generate an API token
5. Use the dashboard to join a meeting (Google Meet, Teams, or Zoom)

## AI Chat (Ollama Integration)

The dashboard includes an "Ask AI" feature for analyzing meeting transcripts. This is powered by a local Ollama model via a compatibility proxy.

### How it works

The Vexa Dashboard uses the Vercel AI SDK, which calls OpenAI's newer `/v1/responses` API. Ollama only supports `/v1/chat/completions`. The included `ollama-proxy.py` bridges this gap by:

1. Converting `/v1/responses` requests to `/v1/chat/completions`
2. Mapping the `developer` message role to `system` (Ollama compatibility)
3. Streaming the response back in the Responses API format

### Changing the AI model

Edit `docker-compose.local-full.yml` and change the `AI_MODEL` environment variable on the `dashboard` service. The format is `ollama/<model-name>`:

```yaml
AI_MODEL: ollama/qwen3.5:27b    # or ollama/llama3.1:latest, etc.
```

Then restart: `docker compose -f docker-compose.local-full.yml up -d dashboard-internal`

## Network Access (Remote Machines)

By default the dashboard uses `localhost` URLs which only work on the host machine. To access Vexa from other machines on your network (e.g. via VPN/WireGuard):

1. Create a `docker-compose.local-full.env` file with your machine's IP:

```env
PUBLIC_HOST_URL=http://10.100.0.4:3001
PUBLIC_WS_URL=ws://10.100.0.4:8056/ws
PUBLIC_API_URL=http://10.100.0.4:8056
NEXTAUTH_URL=http://10.100.0.4:3001
```

2. Start with the env file:

```powershell
docker compose -f docker-compose.local-full.yml --env-file docker-compose.local-full.env up -d
```

The dashboard runs behind an nginx reverse proxy that strips the `Secure` flag from cookies, allowing authentication to work over plain HTTP on local networks.

## Useful Commands

```bash
# Tail all logs
docker compose -f docker-compose.local-full.yml logs -f

# Check service status
docker compose -f docker-compose.local-full.yml ps

# Tail specific service
docker compose -f docker-compose.local-full.yml logs -f whisperlive
docker compose -f docker-compose.local-full.yml logs -f transcription-worker-1
docker compose -f docker-compose.local-full.yml logs -f bot-manager

# Rebuild a single service
docker compose -f docker-compose.local-full.yml up -d --build whisperlive

# Stop everything
docker compose -f docker-compose.local-full.yml down
```

## Files Changed from Upstream

All changes are marked with `[LOCAL-FORK]` comments in the code.

| File | Change | Reason |
|---|---|---|
| `docker-compose.local-full.yml` | **New file** | Single compose file with all services, local DB, GPU transcription, Ollama proxy, dashboard with cookie fix |
| `docker-compose.local-full.env` | **New file** | Environment overrides for network access (IP/hostname) |
| `dashboard-proxy.conf` | **New file** | Nginx config that strips `Secure` flag from cookies for plain HTTP access |
| `start-local.sh` | **New file** | Linux/macOS startup script |
| `start-local.ps1` | **New file** | Windows PowerShell startup script |
| `ollama-proxy.py` | **New file** | Proxy to make Ollama compatible with Vercel AI SDK's Responses API |
| `LOCAL-SETUP.md` | **New file** | This documentation |
| `services/WhisperLive/Dockerfile.cpu` | Fix CRLF line endings | Windows clones have `\r\n` in shell scripts, which breaks `exec` in Linux containers |
| `services/vexa-bot/Dockerfile` | Fix CRLF line endings | Same CRLF issue |
| `services/bot-manager/app/orchestrator_utils.py` | `AutoRemove: False` | Keeps crashed bot containers around for log inspection instead of auto-deleting |

## Credentials

| What | Value |
|---|---|
| Admin API Token | `vexa-local-admin-token` |
| MinIO Console | `vexa-access-key` / `vexa-secret-key` |
| PostgreSQL | `postgres` / `postgres` (database: `vexa`) |

## Troubleshooting

### Bot container crashes immediately
Check `docker ps -a --filter "ancestor=vexa-bot:dev"` for exited containers, then `docker logs <id>`. Common cause: CRLF line endings in entrypoint scripts (fixed in this fork).

### Transcription not working (502 Bad Gateway)
The transcription worker takes 1-2 minutes to download the model on first start. Check `docker compose -f docker-compose.local-full.yml logs transcription-worker-1`. After the model loads, restart nginx: `docker compose -f docker-compose.local-full.yml restart transcription-nginx`.

### WhisperLive shows "TRANSCRIBER_API_KEY not set"
The `REMOTE_TRANSCRIBER_API_KEY` must be a non-empty string. It's set to `local-transcription-key` in the compose file and must match `API_TOKEN` on the transcription worker.

### AI says "no meeting context" / ignores transcript
Make sure Ollama is running on your host machine and the model specified in `AI_MODEL` is pulled. Check proxy logs: `docker compose -f docker-compose.local-full.yml logs ollama-proxy`.

### Live transcription not updating in UI
The dashboard WebSocket must connect to `ws://localhost:8056/ws` (not `ws://api-gateway:8000/ws`). This is configured via `NEXT_PUBLIC_VEXA_WS_URL` in the compose file.
