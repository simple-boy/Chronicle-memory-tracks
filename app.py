"""Participant-hosted Coding/Multimodal Add/Search. One frozen track per service."""
from __future__ import annotations

import hmac
import json
import os
import signal
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from coding import CodingProcessor
from models import ModelClient
from multimodal import MultimodalProcessor
from records import PayloadTooLarge, ProviderError
from store import TrackStore


def create_server(host="127.0.0.1", port=8000, *, track="coding", db_path="data/coding.sqlite3",
                  api_key="", model=None, max_request_bytes=64 * 1024 * 1024,
                  add_concurrency=1, search_concurrency=4, temporal=True):
    if track not in {"coding", "multimodal"}:
        raise ValueError("MEMORY_TRACK must be coding or multimodal")
    if min(max_request_bytes, add_concurrency, search_concurrency) < 1:
        raise ValueError("Request byte limit and concurrency must be positive")
    processor = CodingProcessor(model=model) if track == "coding" else MultimodalProcessor(model=model)
    store = TrackStore(db_path, processor, temporal=temporal)
    add_slots = threading.BoundedSemaphore(add_concurrency)
    search_slots = threading.BoundedSemaphore(search_concurrency)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ChronicleTracks/1.0"

        def log_message(self, fmt, *args):
            # Request bodies, queries, identities and credentials are never logged.
            return

        def setup(self):
            super().setup()
            self.connection.settimeout(120)

        def send_json(self, status, body, retry_after=None):
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if retry_after is not None:
                self.send_header("Retry-After", str(retry_after))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.split("?", 1)[0] == "/health":
                self.send_json(200, {"status": "ok", "service": "chronicle-memory-tracks", "track": track})
            else:
                self.send_json(404, {"detail": {"reason": "not_found"}})

        def authorized(self):
            if not api_key:
                return True
            values = [self.headers.get("X-Api-Key", "")]
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith(("token ", "bearer ")):
                values.append(auth.split(" ", 1)[1])
            return any(hmac.compare_digest(value.encode("utf-8"), api_key.encode("utf-8")) for value in values)

        def do_POST(self):
            if not self.authorized():
                self.send_json(401, {"detail": {"reason": "unauthorized"}})
                return
            path = self.path.split("?", 1)[0]
            if path not in {"/add", "/search"}:
                self.send_json(404, {"detail": {"reason": "not_found"}})
                return
            slots = add_slots if path == "/add" else search_slots
            if not slots.acquire(blocking=False):
                self.send_json(429, {"detail": {"reason": "concurrency_limit"}}, retry_after=1)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > max_request_bytes:
                    raise PayloadTooLarge("Request body exceeds declared byte limit")
                if length <= 0:
                    raise ValueError("A JSON request body is required")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("Incomplete request body")
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError("JSON object required")
                if path == "/add":
                    for field in ("request_id", "user_id", "session_id"):
                        if not isinstance(body.get(field), str) or not body[field].strip():
                            raise ValueError(f"{field} is required")
                    store.add_messages(request_id=body["request_id"], user_id=body["user_id"],
                                       session_id=body["session_id"], messages=body.get("messages"))
                    result = {"success": True, **{k: body[k] for k in ("request_id", "user_id", "session_id")}}
                else:
                    result = {"data": store.search(user_id=body.get("user_id"), query=body.get("query"),
                              top_k=body.get("top_k"), options=body.get("options"))}
                self.send_json(200, result)
            except PayloadTooLarge as exc:
                self.send_json(413, {"detail": {"reason": str(exc)}})
            except (ValueError, UnicodeError) as exc:
                self.send_json(409 if "request_id was reused" in str(exc) else 422, {"detail": {"reason": str(exc)}})
            except ProviderError as exc:
                self.send_json(503, {"detail": {"reason": str(exc)}})
            except (TimeoutError, socket.timeout):
                self.send_json(408, {"detail": {"reason": "request_read_timeout"}})
            except Exception:
                self.send_json(500, {"detail": {"reason": "internal_error"}})
            finally:
                store.close()  # Close this request-thread connection, not another thread's handle.
                slots.release()

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = False  # server_close waits for active requests within the host's grace period.
    server.store = store
    return server


def main():
    track = os.getenv("MEMORY_TRACK", "coding")
    api_key = os.getenv("MEMORY_API_KEY", "").strip()
    model = ModelClient.from_env()
    if os.getenv("PRODUCTION", "0") == "1" and (not api_key or not model.enabled):
        raise ValueError("PRODUCTION=1 requires MEMORY_API_KEY and MODEL_PROVIDER=openai")
    server = create_server(host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")),
            track=track, db_path=os.getenv("MEMORY_DB_PATH", f"data/{track}.sqlite3"), api_key=api_key,
            model=model if model.enabled else None,
            max_request_bytes=int(os.getenv("MAX_REQUEST_BYTES", str(64 * 1024 * 1024))),
            add_concurrency=int(os.getenv("MAX_ADD_CONCURRENCY", "1")),
            search_concurrency=int(os.getenv("MAX_SEARCH_CONCURRENCY", "4")),
            temporal=os.getenv("TEMPORAL_RETRIEVAL", "1") == "1")
    print(f"Chronicle {track} listening on port {server.server_port}; model={model.mode}", flush=True)
    def stop_service(signum, frame):
        # shutdown must run outside serve_forever's thread. The host controls
        # its grace period; unfinished Add is never acknowledged as durable.
        threading.Thread(target=server.shutdown, daemon=True).start()
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_service)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        server.store.close()


if __name__ == "__main__":
    main()
