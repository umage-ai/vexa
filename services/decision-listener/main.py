# [LOCAL-FORK] Decision Listener Service
# Real-time meeting analysis using local Ollama LLM.
# Watches transcript segments from Redis, analyzes them in rolling windows,
# and categorizes items (decisions, action items, etc.)
#
# API endpoints (expected by the Vexa Dashboard):
#   GET  /config              - Get tracker configuration
#   PUT  /config              - Update tracker configuration
#   POST /config/reset        - Reset to defaults
#   GET  /decisions/{id}      - SSE stream of real-time decisions for a meeting
#   GET  /decisions/{id}/all  - Get all decisions for a meeting

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3.5:27b")
WINDOW_SIZE = int(os.environ.get("WINDOW_SIZE", "20"))  # transcript segments per analysis window
ANALYSIS_INTERVAL = float(os.environ.get("ANALYSIS_INTERVAL", "15"))  # seconds between analyses
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
# [LOCAL-FORK] API gateway URL — single entry point that proxies to bot-manager & transcription-collector
API_GATEWAY_URL = os.environ.get("API_GATEWAY_URL", "http://api-gateway:8000")
# Vexa API key for authenticated requests through the gateway
VEXA_API_KEY = os.environ.get("VEXA_API_KEY", "")

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class TrackerCategory(BaseModel):
    key: str
    label: str
    description: str
    enabled: bool = True

class TrackerConfig(BaseModel):
    name: str = "Meeting Intelligence"
    description: str = "Detects decisions, action items, and key statements in real-time"
    categories: list[TrackerCategory] = Field(default_factory=lambda: [
        TrackerCategory(key="decision", label="Decision", description="A concrete decision or agreement made by the participants", enabled=True),
        TrackerCategory(key="action_item", label="Action Item", description="A task or action assigned to a specific person with a clear deliverable", enabled=True),
        TrackerCategory(key="key_insight", label="Key Insight", description="An important observation, insight, or piece of information shared", enabled=True),
    ])
    extra_instructions: str = "Be conservative. Only capture explicit statements, not tentative suggestions."

class DecisionEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str  # matches a category key
    summary: str
    speaker: Optional[str] = None
    confidence: Optional[float] = None
    meeting_id: Optional[str] = None
    timestamp: Optional[float] = None

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

config = TrackerConfig()
DEFAULT_CONFIG = TrackerConfig()

# meeting_id -> list of DecisionEvent
decisions: dict[str, list[DecisionEvent]] = {}

# meeting_id -> list of asyncio.Queue for SSE subscribers
subscribers: dict[str, list[asyncio.Queue]] = {}

# meeting_id -> asyncio.Task for the analysis loop
analysis_tasks: dict[str, asyncio.Task] = {}

# Redis connection
redis_client: Optional[aioredis.Redis] = None

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    print(f"[decision-listener] Starting. Redis={REDIS_URL}, Ollama={OLLAMA_URL}, Model={LLM_MODEL}")
    try:
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        print("[decision-listener] Redis connected")
    except Exception as e:
        print(f"[decision-listener] Redis connection failed: {e}")
        redis_client = None
    yield
    # Cleanup
    for task in analysis_tasks.values():
        task.cancel()
    if redis_client:
        await redis_client.aclose()
    print("[decision-listener] Shutdown complete")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Decision Listener", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

def build_system_prompt() -> str:
    enabled = [c for c in config.categories if c.enabled]
    lines = [
        "You are a precise meeting analyst.",
        "You are given a rolling window of recent transcript segments from a live meeting.",
        "",
        "Your job: detect exactly ONE of the following, if present:",
    ]
    for c in enabled:
        lines.append(f"- **{c.key}**: {c.description}")
    lines.append("- **no_match**: nothing significant to capture right now")
    lines.append("")
    lines.append("Rules:")
    for sentence in config.extra_instructions.split(". "):
        s = sentence.strip()
        if s:
            lines.append(f"- {s}.")
    lines.append("- Always respond with EXACTLY one JSON object: {\"type\": \"<category_key or no_match>\", \"summary\": \"<one sentence>\", \"speaker\": \"<name or null>\", \"confidence\": <0.0-1.0>}")
    lines.append("- Do NOT wrap the JSON in markdown code blocks.")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Ollama LLM call
# ---------------------------------------------------------------------------

async def analyze_transcript_window(segments: list[dict], meeting_id: str) -> Optional[DecisionEvent]:
    """Send a window of transcript segments to Ollama for analysis."""
    if not segments:
        return None

    # Build the transcript text
    transcript_lines = []
    for seg in segments:
        speaker = seg.get("speaker", "Unknown")
        text = seg.get("text", "")
        transcript_lines.append(f"[{speaker}]: {text}")
    transcript_text = "\n".join(transcript_lines)

    system_prompt = build_system_prompt()
    user_message = f"Analyze these recent transcript segments:\n\n{transcript_text}"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    "stream": False,
                    "format": "json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("message", {}).get("content", "")

            # Parse the JSON response
            result = json.loads(content)
            item_type = result.get("type", "no_match")

            if item_type == "no_match":
                return None

            # Validate type is a known category
            known_keys = {c.key for c in config.categories if c.enabled}
            if item_type not in known_keys:
                return None

            return DecisionEvent(
                type=item_type,
                summary=result.get("summary", ""),
                speaker=result.get("speaker"),
                confidence=result.get("confidence"),
                meeting_id=meeting_id,
                timestamp=time.time(),
            )
    except Exception as e:
        print(f"[decision-listener] LLM analysis error: {e}")
        return None

# ---------------------------------------------------------------------------
# Meeting analysis loop
# ---------------------------------------------------------------------------

# Cache: meeting_id -> (platform, native_id)
_meeting_lookup_cache: dict[str, tuple[str, str]] = {}

async def fetch_segments_from_api(meeting_id: str) -> list[dict]:
    """[LOCAL-FORK] Fetch transcript segments via the API gateway.
    Used when Redis segments have been flushed to Postgres (completed meetings).

    Strategy: Call the API gateway (which proxies to bot-manager and
    transcription-collector) using VEXA_API_KEY for auth.
    """
    auth_headers = {"X-API-Key": VEXA_API_KEY} if VEXA_API_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Step 1: Get platform and native_id for this meeting_id
            platform, native_id = _meeting_lookup_cache.get(meeting_id, (None, None))

            if not platform or not native_id:
                resp = await client.get(
                    f"{API_GATEWAY_URL}/meetings",
                    headers=auth_headers,
                )
                if resp.status_code == 200:
                    meetings = resp.json()
                    meeting_list = meetings if isinstance(meetings, list) else meetings.get("meetings", [])
                    for m in meeting_list:
                        mid = str(m.get("id", ""))
                        if mid == meeting_id:
                            platform = m.get("platform", "")
                            native_id = m.get("platform_specific_id", "") or m.get("native_meeting_id", "")
                            if platform and native_id:
                                _meeting_lookup_cache[meeting_id] = (platform, native_id)
                            break
                else:
                    print(f"[decision-listener] Meetings lookup failed: {resp.status_code} {resp.text[:200]}")

            if not platform or not native_id:
                print(f"[decision-listener] Could not resolve meeting {meeting_id} to platform/native_id")
                return []

            # Step 2: Fetch transcript segments via the gateway
            resp = await client.get(
                f"{API_GATEWAY_URL}/transcripts/{platform}/{native_id}",
                headers=auth_headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                segments = data.get("segments", [])
                print(f"[decision-listener] Fetched {len(segments)} segments for meeting {meeting_id} ({platform}/{native_id})")
                return segments
            else:
                print(f"[decision-listener] Transcript fetch failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[decision-listener] API segment fetch error: {e}")
    return []


async def meeting_analysis_loop(meeting_id: str):
    """Continuously analyze transcript segments for a meeting."""
    print(f"[decision-listener] Starting analysis loop for meeting {meeting_id}")
    segment_buffer: list[dict] = []
    last_analysis_time = 0.0
    api_fetched = False  # Only fetch from API once for completed meetings
    analysis_count = 0

    while True:
        try:
            # Try Redis first (live meetings)
            redis_segments_found = False
            if redis_client:
                redis_key = f"meeting:{meeting_id}:segments"
                raw_segments = await redis_client.hgetall(redis_key)
                if raw_segments:
                    redis_segments_found = True
                    all_segments = []
                    for start_key, val_json in raw_segments.items():
                        try:
                            seg = json.loads(val_json)
                            seg["start_time"] = float(start_key)
                            all_segments.append(seg)
                        except (json.JSONDecodeError, ValueError):
                            continue
                    # Sort by start time and take latest WINDOW_SIZE
                    all_segments.sort(key=lambda s: s.get("start_time", 0))
                    segment_buffer = all_segments[-WINDOW_SIZE:]

            # [LOCAL-FORK] If Redis is empty and we haven't fetched from API yet, try the API.
            # Always set api_fetched=True afterwards to avoid spamming repeated lookups.
            if not redis_segments_found and not api_fetched:
                print(f"[decision-listener] No Redis segments for meeting {meeting_id}, fetching from API...")
                api_segments = await fetch_segments_from_api(meeting_id)
                api_fetched = True  # prevent repeated fetch attempts regardless of result
                if api_segments:
                    print(f"[decision-listener] Got {len(api_segments)} segments from API for meeting {meeting_id}")
                    segment_buffer = api_segments[-WINDOW_SIZE:]
                else:
                    print(f"[decision-listener] No segments found for meeting {meeting_id} (Redis or API)")

            now = time.time()
            if now - last_analysis_time >= ANALYSIS_INTERVAL and len(segment_buffer) > 0:
                last_analysis_time = now

                # For completed meetings (API-sourced), analyze in sliding windows
                if api_fetched and analysis_count == 0:
                    # Analyze the full transcript in chunks for completed meetings
                    all_api_segments = await fetch_segments_from_api(meeting_id)
                    chunk_size = WINDOW_SIZE
                    for i in range(0, len(all_api_segments), chunk_size):
                        chunk = all_api_segments[i:i + chunk_size]
                        event = await analyze_transcript_window(chunk, meeting_id)
                        if event:
                            if meeting_id not in decisions:
                                decisions[meeting_id] = []
                            decisions[meeting_id].append(event)
                            print(f"[decision-listener] [{meeting_id}] {event.type}: {event.summary}")
                            event_data = event.model_dump()
                            for queue in subscribers.get(meeting_id, []):
                                try:
                                    queue.put_nowait(event_data)
                                except asyncio.QueueFull:
                                    pass
                    analysis_count += 1
                    # For completed meetings, stop after full analysis
                    print(f"[decision-listener] Completed analysis for meeting {meeting_id}: {len(decisions.get(meeting_id, []))} items found")
                    return
                else:
                    # Live meeting: analyze latest window
                    event = await analyze_transcript_window(segment_buffer, meeting_id)
                    if event:
                        if meeting_id not in decisions:
                            decisions[meeting_id] = []
                        decisions[meeting_id].append(event)
                        print(f"[decision-listener] [{meeting_id}] {event.type}: {event.summary}")
                        event_data = event.model_dump()
                        for queue in subscribers.get(meeting_id, []):
                            try:
                                queue.put_nowait(event_data)
                            except asyncio.QueueFull:
                                pass
                    analysis_count += 1

            await asyncio.sleep(3)  # Poll every 3 seconds
        except asyncio.CancelledError:
            print(f"[decision-listener] Analysis loop cancelled for meeting {meeting_id}")
            break
        except Exception as e:
            print(f"[decision-listener] Analysis loop error for meeting {meeting_id}: {e}")
            await asyncio.sleep(5)

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/config")
async def get_config():
    return config.model_dump()

@app.put("/config")
async def update_config(new_config: TrackerConfig):
    global config
    config = new_config
    return config.model_dump()

@app.post("/config/reset")
async def reset_config():
    global config
    config = TrackerConfig()
    return config.model_dump()

@app.get("/decisions/{meeting_id}")
async def stream_decisions(meeting_id: str, request: Request):
    """SSE endpoint — streams decisions in real-time for a meeting."""
    # Start analysis loop if not running
    if meeting_id not in analysis_tasks or analysis_tasks[meeting_id].done():
        analysis_tasks[meeting_id] = asyncio.create_task(meeting_analysis_loop(meeting_id))

    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    if meeting_id not in subscribers:
        subscribers[meeting_id] = []
    subscribers[meeting_id].append(queue)

    async def event_stream():
        try:
            # First, send any existing decisions
            for d in decisions.get(meeting_id, []):
                yield f"data: {json.dumps(d.model_dump() if hasattr(d, 'model_dump') else d)}\n\n"

            # Then stream new ones
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event_data = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {json.dumps(event_data)}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive
                    yield ": keepalive\n\n"
        finally:
            # Cleanup subscriber
            if meeting_id in subscribers and queue in subscribers[meeting_id]:
                subscribers[meeting_id].remove(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

@app.get("/decisions/{meeting_id}/all")
async def get_all_decisions(meeting_id: str):
    """Return all captured decisions for a meeting."""
    # Start analysis loop if not running
    if meeting_id not in analysis_tasks or analysis_tasks[meeting_id].done():
        analysis_tasks[meeting_id] = asyncio.create_task(meeting_analysis_loop(meeting_id))

    items = decisions.get(meeting_id, [])
    return [d.model_dump() if hasattr(d, "model_dump") else d for d in items]

@app.get("/health")
async def health():
    return {"status": "ok", "model": LLM_MODEL, "redis": redis_client is not None}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8766"))
    uvicorn.run(app, host="0.0.0.0", port=port)
