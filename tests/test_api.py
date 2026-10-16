"""API boundary/admission tests with injected stores; integration tests use real SQLite."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from starlette.requests import ClientDisconnect

from forgetrace.api import create_app
from forgetrace.config import Settings


def payload(request_id="r1", content="Fix get_user_by_id in src/auth.py; TypeError on missing user."):
    return {"request_id": request_id, "user_id": "u1", "session_id": "s1", "messages": [
        {"role": "user", "content": content, "timestamp": 1704067200000}]}


def settings(**kwargs):
    return Settings(api_key="test-private-key", model_mode="offline", min_free_bytes=0, **kwargs)


class StubStore:
    def __init__(self):
        self.closed_threads, self.worker_threads, self.received = [], [], []
        self.exception = None
        self.block = False
        self.started, self.release = threading.Event(), threading.Event()

    def add(self, value):
        self.worker_threads.append(threading.get_ident())
        if self.block:
            self.started.set()
            if not self.release.wait(5):
                raise RuntimeError("test wait timed out")
        if self.exception:
            raise self.exception
        self.received.append(value)
        return {"success": True, **{key: value[key] for key in ("request_id", "user_id", "session_id")}}

    def search(self, value):
        self.worker_threads.append(threading.get_ident())
        return {"data": []}

    def metrics(self):
        return {"model_calls": 0}

    def health(self):
        return {"status": "ok", "journal_mode": "wal", "synchronous": "FULL"}

    def close(self):
        self.closed_threads.append(threading.get_ident())


class ApiBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = StubStore()
        self.settings = settings()
        self.app = create_app(self.settings, self.store)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.headers = {"Authorization": "Token test-private-key"}

    async def asyncTearDown(self):
        await self.client.aclose()

    async def post(self, path, value):
        return await self.client.post(path, json=value, headers=self.headers)

    async def test_public_health_private_metrics_and_no_credentials(self):
        health = await self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["service"], "forgetrace")
        self.assertEqual(health.json()["track"], "coding")
        self.assertTrue(health.json()["diagnostic_only"])
        self.assertFalse(health.json()["submission_ready"])
        self.assertEqual(len(health.json()["source_sha256"]), 64)
        self.assertNotIn("test-private-key", health.text)
        self.assertEqual((await self.client.get("/metrics")).status_code, 401)
        self.assertEqual((await self.client.get("/metrics", headers=self.headers)).status_code, 200)

    async def test_auth_before_body_and_three_auth_schemes(self):
        self.assertEqual((await self.client.post("/add", content=b"bad")).status_code, 401)
        for headers in ({"Authorization": "Token test-private-key"},
                        {"Authorization": "Bearer test-private-key"}, {"X-API-Key": "test-private-key"}):
            response = await self.client.post("/search", json={"user_id": "u", "query": "TypeError", "top_k": 1}, headers=headers)
            self.assertEqual(response.status_code, 200)
        self.assertEqual((await self.client.post("/add", json=payload(), headers={"Authorization": "Token wrong"})).status_code, 401)

    async def test_ack_preserves_all_message_and_extra_fields(self):
        value = payload()
        value["metadata"] = {"opaque": "retained"}
        value["messages"].append({"role": "assistant", "content": "```python\ndef getUserById(user_id):\n    return None\n```"})
        response = await self.post("/add", value)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": True, "request_id": "r1", "user_id": "u1", "session_id": "s1"})
        self.assertEqual(self.store.received, [value])
        self.assertEqual(self.store.worker_threads, self.store.closed_threads)

    async def test_invalid_role_timestamp_and_missing_fields(self):
        invalid = []
        for role in ("system", "tool", None, [], {}):
            value = payload(); value["messages"][0]["role"] = role; invalid.append(value)
        for stamp in (None, True, 1.5, "1704067200000", 10**30):
            value = payload(); value["messages"][0]["timestamp"] = stamp; invalid.append(value)
        for key in ("user_id", "request_id", "session_id", "messages"):
            value = payload(); del value[key]; invalid.append(value)
        for value in invalid:
            self.assertEqual((await self.post("/add", value)).status_code, 422)
        self.assertEqual(self.store.received, [])

    async def test_coding_content_is_string_only(self):
        for content in (None, "", "  ", 12, {}, [], [{"type": "text", "text": "code"}]):
            self.assertEqual((await self.post("/add", payload(content=content))).status_code, 422)
        self.assertEqual((await self.post("/add", payload(content="x" * 200000))).status_code, 200)

    async def test_search_top_k_query_and_options(self):
        base = {"user_id": "u", "query": "TypeError getUserById", "top_k": 100}
        for top_k in (None, 0, -1, 101, True, 1.5, "1"):
            self.assertEqual((await self.post("/search", {**base, "top_k": top_k})).status_code, 422)
        for query in (None, "", " ", [], {}, 123):
            self.assertEqual((await self.post("/search", {**base, "query": query})).status_code, 422)
        for options in (None, "A", {}, [""], [1]):
            self.assertEqual((await self.post("/search", {**base, "options": options})).status_code, 422)
        self.assertEqual((await self.post("/search", {**base, "options": []})).status_code, 200)
        self.assertEqual((await self.post("/search", {**base, "options": ["src/auth.py", "tests/test_auth.py"]})).status_code, 200)

    async def test_json_duplicates_nan_unicode_and_content_type(self):
        headers = {**self.headers, "Content-Type": "application/json"}
        for raw in (b"{", b'{"x":NaN}', b'{"user_id":"a","user_id":"b"}', b"\xff"):
            self.assertEqual((await self.client.post("/add", content=raw, headers=headers)).status_code, 400)
        raw = b'{"request_id":"r","user_id":"u","session_id":"s","messages":[{"role":"user","content":"\\ud800"}]}'
        self.assertEqual((await self.client.post("/add", content=raw, headers=headers)).status_code, 422)
        self.assertEqual((await self.post("/add", [])).status_code, 422)
        self.assertEqual((await self.client.post("/add", content=b"{}", headers=self.headers)).status_code, 415)
        self.assertEqual((await self.client.post("/add", content=b"{}", headers={**headers, "Content-Encoding": "gzip"})).status_code, 415)

    async def test_declared_streamed_body_limits_and_capacity_recovery(self):
        app = create_app(replace(self.settings, max_request_bytes=256), self.store)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            self.assertEqual((await client.post("/add", json=payload(content="x" * 500), headers=self.headers)).status_code, 413)
            async def chunks():
                yield b" " * 200
                yield b" " * 200
            self.assertEqual((await client.post("/add", content=chunks(), headers={**self.headers, "Content-Type": "application/json"})).status_code, 413)
            self.assertEqual((await client.post("/search", json={"user_id": "u", "query": "x", "top_k": 1}, headers=self.headers)).status_code, 200)

    async def test_stalled_body_timeout_releases_slot(self):
        app = create_app(replace(self.settings, read_timeout_seconds=1, max_add_concurrency=1), self.store)
        async def stalled():
            yield b'{"x":'
            await asyncio.sleep(2)
            yield b"1}"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            result = await client.post("/add", content=stalled(), headers={**self.headers, "Content-Type": "application/json"})
            self.assertEqual(result.status_code, 408)
            self.assertEqual((await client.post("/add", json=payload(), headers=self.headers)).status_code, 200)

    async def test_body_disconnect_is_not_a_second_internal_error(self):
        app = create_app(replace(self.settings, max_add_concurrency=1), self.store)
        async def disconnected():
            yield b'{"request_id":'
            raise ClientDisconnect()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/add", content=disconnected(), headers={**self.headers, "Content-Type": "application/json"})
            self.assertEqual(response.status_code, 408)
            self.assertEqual(response.json(), {"detail": {"reason": "client_disconnected"}})
            self.assertEqual((await client.post("/add", json=payload(), headers=self.headers)).status_code, 200)

    async def test_concurrency_429_search_independent_and_worker_cleanup(self):
        self.store.block = True
        app = create_app(replace(self.settings, max_add_concurrency=1), self.store)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first = asyncio.create_task(client.post("/add", json=payload(), headers=self.headers))
            await asyncio.wait_for(asyncio.to_thread(self.store.started.wait, 3), timeout=4)
            try:
                rejected = await client.post("/add", json=payload("busy"), headers=self.headers)
                self.assertEqual(rejected.status_code, 429)
                self.assertEqual(rejected.headers["Retry-After"], "1")
                self.assertEqual((await client.post("/search", json={"user_id": "u", "query": "x", "top_k": 1}, headers=self.headers)).status_code, 200)
            finally:
                self.store.release.set()
            self.assertEqual((await first).status_code, 200)
            self.assertEqual((await client.post("/add", json=payload("after"), headers=self.headers)).status_code, 200)
        self.assertEqual(sorted(self.store.worker_threads), sorted(self.store.closed_threads))

    async def test_exception_redaction_and_subsequent_requests(self):
        self.store.exception = RuntimeError("PRIVATE-BODY KEY=secret")
        response = await self.post("/add", payload())
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": {"reason": "internal_error"}})
        self.store.exception = None
        self.assertEqual((await self.post("/add", payload())).status_code, 200)

    async def test_storage_failure_retry_after(self):
        self.store.exception = OSError("SECRET /database/path")
        response = await self.post("/add", payload())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Retry-After"], "1")
        self.assertNotIn("SECRET", response.text)

    async def test_response_limit_explicit_no_truncation(self):
        self.store.search = lambda value: {"data": [{"id": "source", "content": "x" * 2000}]}
        app = create_app(replace(self.settings, max_response_bytes=1024), self.store)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/search", json={"user_id": "u", "query": "x", "top_k": 1}, headers=self.headers)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {"detail": {"reason": "response_body_limit"}})

    async def test_log_never_contains_body_identifiers_credentials(self):
        with self.assertLogs(logging.getLogger("forgetrace"), level="INFO") as captured:
            await self.post("/add", payload(content="secret-code-never-log"))
        text = "\n".join(captured.output)
        for secret in ("secret-code-never-log", "test-private-key", '"u1"', '"r1"', '"s1"'):
            self.assertNotIn(secret, text)
        self.assertIn("request_completed", text)


class SettingsTests(unittest.TestCase):
    def test_production_credentials_models_and_offline_diagnostic(self):
        with self.assertRaises(ValueError):
            Settings()
        with self.assertRaises(ValueError):
            Settings(api_key="private")
        config = Settings(api_key="private", openai_api_key="upstream-private")
        self.assertNotIn("private", repr(config))
        self.assertEqual(config.model_mode, "gpt-4o-mini")
        self.assertEqual(settings().model_mode, "offline")

    def test_invalid_limits_and_unapproved_models(self):
        for changes in ({"max_add_concurrency": 0}, {"max_results": 101}, {"model_mode": "other-model"},
                        {"embedding_mode": "other-embedding"}, {"strategy": "hybrid"},
                        {"openai_base_url": "https://user:secret@example.com/v1"}, {"api_key": " spaced "}):
            with self.assertRaises(ValueError):
                replace(settings(), **changes)
        with mock.patch.dict("os.environ", {"MEMORY_API_KEY": "private", "MODEL_MODE": "offline", "MAX_ADD_CONCURRENCY": "2"}, clear=True):
            self.assertEqual(Settings.from_env().max_add_concurrency, 2)


class RealStorageApiTests(unittest.IsolatedAsyncioTestCase):
    """These tests construct the production MemoryStore, not StubStore."""
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = settings(db_path=str(Path(self.directory.name) / "memory.sqlite3"))
        self.app = create_app(self.config)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.headers = {"Authorization": "Token test-private-key"}

    async def asyncTearDown(self):
        await self.client.aclose()
        self.app.state.store.close()
        self.directory.cleanup()

    async def post(self, path, value):
        return await self.client.post(path, json=value, headers=self.headers)

    async def test_immediate_retrieval_and_cross_session_history(self):
        value = payload(content="unique_auth_evidence src/auth.py get_user_by_id raises TypeError. Guard None before lookup.")
        self.assertEqual((await self.post("/add", value)).status_code, 200)
        value2 = payload("r2", "Unrelated src/media.py decode_frame raises ValueError.")
        value2["session_id"] = "another-session"
        self.assertEqual((await self.post("/add", value2)).status_code, 200)
        response = await self.post("/search", {"query": "unique_auth_evidence get_user_by_id", "user_id": "u1", "top_k": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["data"]), 1)
        self.assertIn("unique_auth_evidence", response.json()["data"][0]["content"])

    async def test_real_idempotency_and_request_collision(self):
        first = await self.post("/add", payload())
        retry = await self.post("/add", payload())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), retry.json())
        conflict = await self.post("/add", payload(content="Different original code evidence."))
        self.assertEqual(conflict.status_code, 409)
        rows = (await self.post("/search", {"query": "get_user_by_id", "user_id": "u1", "top_k": 100})).json()["data"]
        self.assertEqual(len(rows), 1)

    async def test_real_user_isolation_and_empty_irrelevant(self):
        self.assertEqual((await self.post("/add", payload())).status_code, 200)
        other = {**payload("other", "private_other_user_secret TypeError get_user_by_id"), "user_id": "u2"}
        self.assertEqual((await self.post("/add", other)).status_code, 200)
        response = await self.post("/search", {"query": "TypeError get_user_by_id", "user_id": "u1", "top_k": 100})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("private_other_user_secret", response.text)
        absent = await self.post("/search", {"query": "TypeError", "user_id": "missing-user", "top_k": 100})
        self.assertEqual(absent.json(), {"data": []})
        irrelevant = await self.post("/search", {"query": "xyzzzz_utterly_unrelated", "user_id": "u1", "top_k": 100})
        self.assertEqual(irrelevant.json(), {"data": []})

    async def test_reopen_same_sqlite_preserves_stable_source_id(self):
        self.assertEqual((await self.post("/add", payload())).status_code, 200)
        query = {"query": "get_user_by_id", "user_id": "u1", "top_k": 1}
        before = (await self.post("/search", query)).json()
        reopened = create_app(self.config)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=reopened), base_url="http://test") as client:
                after = await client.post("/search", json=query, headers=self.headers)
                self.assertEqual(after.status_code, 200)
                self.assertEqual(after.json(), before)
        finally:
            reopened.state.store.close()

    async def test_long_code_input_exact_identifier_and_top_k(self):
        content = "\n".join(f"# log line {i}: get_user_by_id handles user_id in src/auth/middleware.py" for i in range(1800))
        content += "\nunique_long_code_end_marker: pytest tests/test_auth.py -q\n2 passed in 0.12s\n"
        self.assertEqual((await self.post("/add", payload(content=content))).status_code, 200)
        result = await self.post("/search", {"query": "unique_long_code_end_marker", "user_id": "u1", "top_k": 1})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json()["data"]), 1)
        self.assertIn("unique_long_code_end_marker", result.json()["data"][0]["content"])

    async def test_two_concurrent_real_writes_then_read(self):
        for batch in range(4):
            outcomes = await asyncio.gather(*[self.post("/add", payload(f"concurrent-{batch}-{i}", f"concurrent_api_{batch}_{i} src/auth.py handles None user.")) for i in range(2)])
            self.assertEqual([result.status_code for result in outcomes], [200, 200])
        result = await self.post("/search", {"query": "concurrent_api_3_1", "user_id": "u1", "top_k": 1})
        self.assertEqual(result.status_code, 200)
        self.assertIn("concurrent_api_3_1", result.json()["data"][0]["content"])


class SelfcheckSecurityTests(unittest.TestCase):
    def test_memory_key_is_not_forwarded_to_redirect_origin(self):
        spec = importlib.util.spec_from_file_location("forge_selfcheck", Path(__file__).parents[1] / "scripts" / "smoke_test.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        calls = []
        class Destination(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.headers.get("Authorization"))
                self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
            do_POST = do_GET
            def log_message(self, *args): pass
        destination = ThreadingHTTPServer(("127.0.0.1", 0), Destination)
        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:" + str(destination.server_port) + "/sink")
                self.send_header("Content-Type", "application/json")
                self.end_headers(); self.wfile.write(b'{"redirect":"blocked"}')
            def log_message(self, *args): pass
        redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        workers = [threading.Thread(target=server.serve_forever, daemon=True) for server in (destination, redirect)]
        for worker in workers: worker.start()
        try:
            status, value, _ = module.request("http://127.0.0.1:" + str(redirect.server_port), "/add", key="synthetic-never-forward", method="POST", payload={})
            self.assertEqual(status, 302)
            self.assertEqual(value, {"redirect": "blocked"})
            self.assertEqual(calls, [])
        finally:
            for server in (redirect, destination):
                server.shutdown(); server.server_close()
            for worker in workers: worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
