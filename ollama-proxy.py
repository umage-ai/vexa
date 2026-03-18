"""Proxy that converts Vercel AI SDK's /v1/responses calls to /v1/chat/completions for Ollama.

The Vercel AI SDK (v5+) uses the OpenAI Responses API (/v1/responses) which Ollama doesn't support.
This proxy converts those requests to the chat completions format that Ollama understands,
and also ensures system prompts are in the messages array.
"""
import json
import os
import sys
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")


def convert_responses_to_chat(data):
    """Convert /v1/responses format to /v1/chat/completions format."""
    model = data.get("model", "")
    input_data = data.get("input", [])
    instructions = data.get("instructions", "")
    stream = data.get("stream", False)
    temperature = data.get("temperature")
    max_tokens = data.get("max_output_tokens") or data.get("max_tokens")

    messages = []

    # Add instructions (system prompt) as first message
    if instructions:
        messages.append({"role": "system", "content": instructions})

    # Convert input to messages
    if isinstance(input_data, str):
        messages.append({"role": "user", "content": input_data})
    elif isinstance(input_data, list):
        for item in input_data:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                role = item.get("role", "user")
                content = item.get("content", "")
                if isinstance(content, list):
                    # Handle multi-part content (text parts)
                    text_parts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                        elif isinstance(part, dict) and part.get("type") == "input_text":
                            text_parts.append(part.get("text", ""))
                        elif isinstance(part, str):
                            text_parts.append(part)
                    content = "\n".join(text_parts) if text_parts else ""
                # Map "developer" role to "system" for Ollama compatibility
                if role == "developer":
                    role = "system"
                if content:
                    messages.append({"role": role, "content": content})

    result = {"model": model, "messages": messages, "stream": stream}
    if temperature is not None:
        result["temperature"] = temperature
    if max_tokens is not None:
        result["max_tokens"] = max_tokens

    return result


class ResponsesStreamConverter:
    """Converts chat completions SSE stream to OpenAI Responses API SSE stream."""

    def __init__(self):
        self.started = False
        self.finished = False
        self.full_text = ""
        self.response_id = "resp_" + os.urandom(8).hex()
        self.item_id = "msg_" + os.urandom(8).hex()

    def get_preamble_events(self):
        """Events needed before any content deltas."""
        return [
            json.dumps({
                "type": "response.created",
                "response": {
                    "id": self.response_id,
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                }
            }),
            json.dumps({
                "type": "response.in_progress",
                "response": {"id": self.response_id, "status": "in_progress"},
            }),
            json.dumps({
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": self.item_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                },
            }),
            json.dumps({
                "type": "response.content_part.added",
                "item_id": self.item_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": ""},
            }),
        ]

    def convert_chunk(self, chunk_str):
        """Convert a single SSE event line."""
        if not chunk_str.startswith("data: "):
            return []

        data_str = chunk_str[6:].strip()
        if data_str == "[DONE]":
            return self._finish_events()

        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            return []

        choices = chunk.get("choices", [])
        if not choices:
            return []

        delta = choices[0].get("delta", {})
        content = delta.get("content", "")
        finish_reason = choices[0].get("finish_reason")

        events = []

        if not self.started and (content or finish_reason):
            self.started = True
            events.extend(self.get_preamble_events())

        if content:
            self.full_text += content
            events.append(json.dumps({
                "type": "response.output_text.delta",
                "item_id": self.item_id,
                "output_index": 0,
                "content_index": 0,
                "delta": content,
            }))

        if finish_reason == "stop":
            events.extend(self._finish_events())

        return events

    def _finish_events(self):
        if self.finished:
            return []
        self.finished = True
        return [
            json.dumps({
                "type": "response.output_text.done",
                "item_id": self.item_id,
                "output_index": 0,
                "content_index": 0,
                "text": self.full_text,
            }),
            json.dumps({
                "type": "response.content_part.done",
                "item_id": self.item_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": self.full_text},
            }),
            json.dumps({
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": self.item_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": self.full_text}],
                },
            }),
            json.dumps({
                "type": "response.completed",
                "response": {
                    "id": self.response_id,
                    "object": "response",
                    "status": "completed",
                    "output": [{
                        "id": self.item_id,
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": self.full_text}],
                    }],
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                },
            }),
        ]


class ProxyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = None

        is_responses_api = "/v1/responses" in self.path

        if is_responses_api and data:
            print(f"[proxy] /v1/responses -> converting to /v1/chat/completions", flush=True)
            print(f"[proxy] instructions length: {len(data.get('instructions', '') or '')}", flush=True)
            print(f"[proxy] input items: {len(data.get('input', []))}", flush=True)

            chat_data = convert_responses_to_chat(data)
            print(f"[proxy] Converted to {len(chat_data['messages'])} messages, roles: {[m['role'] for m in chat_data['messages']]}", flush=True)
            if chat_data['messages'] and chat_data['messages'][0]['role'] == 'system':
                sys_msg = chat_data['messages'][0]['content']
                print(f"[proxy] System message ({len(sys_msg)} chars): {sys_msg[:200]}...", flush=True)

            body = json.dumps(chat_data).encode()
            target_url = f"{OLLAMA_URL}/v1/chat/completions"
        else:
            # Regular chat completions - just fix system field
            if data and "system" in data and data["system"]:
                system_text = data.pop("system")
                messages = data.get("messages", [])
                if not messages or messages[0].get("role") != "system":
                    messages.insert(0, {"role": "system", "content": system_text})
                data["messages"] = messages
                body = json.dumps(data).encode()
                print(f"[proxy] Moved system field into messages[0]", flush=True)

            target_url = f"{OLLAMA_URL}{self.path}"

        req = urllib.request.Request(target_url, data=body, method="POST")
        for header in ("Content-Type", "Authorization"):
            val = self.headers.get(header)
            if val:
                req.add_header(header, val)
        req.add_header("Content-Length", str(len(body)))

        try:
            resp = urllib.request.urlopen(req, timeout=300)
            self.send_response(resp.status)

            if is_responses_api:
                # Stream response, converting chat completions to Responses API format
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()

                converter = ResponsesStreamConverter()
                raw_buffer = b""
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    raw_buffer += chunk

                    # Decode only complete UTF-8 sequences
                    try:
                        text = raw_buffer.decode("utf-8")
                        raw_buffer = b""
                    except UnicodeDecodeError:
                        # Find last valid UTF-8 boundary
                        for i in range(1, 4):
                            try:
                                text = raw_buffer[:-i].decode("utf-8")
                                raw_buffer = raw_buffer[-i:]
                                break
                            except UnicodeDecodeError:
                                continue
                        else:
                            continue

                    lines = text.split("\n")
                    for line in lines:
                        line = line.strip()
                        if line.startswith("data: "):
                            events = converter.convert_chunk(line)
                            for event in events:
                                self.wfile.write(f"data: {event}\n\n".encode())
                                self.wfile.flush()

                # Handle remaining buffer
                if raw_buffer:
                    text = raw_buffer.decode("utf-8", errors="replace")
                    for line in text.split("\n"):
                        line = line.strip()
                        if line.startswith("data: "):
                            events = converter.convert_chunk(line)
                            for event in events:
                                self.wfile.write(f"data: {event}\n\n".encode())

                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            else:
                for key, val in resp.getheaders():
                    if key.lower() not in ("transfer-encoding", "content-length", "connection"):
                        self.send_header(key, val)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(f"{len(chunk):x}\r\n".encode())
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(e.read())
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
            print(f"[proxy] ERROR: {e}", flush=True)

    def do_GET(self):
        target_url = f"{OLLAMA_URL}{self.path}"
        req = urllib.request.Request(target_url, method="GET")
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            self.send_response(resp.status)
            for key, val in resp.getheaders():
                if key.lower() not in ("transfer-encoding", "connection"):
                    self.send_header(key, val)
            self.end_headers()
            self.wfile.write(resp.read())
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        pass  # Suppress default access logs


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    server = HTTPServer(("0.0.0.0", port), ProxyHandler)
    print(f"Ollama proxy listening on port {port}, forwarding to {OLLAMA_URL}", flush=True)
    server.serve_forever()
