"""Real HTTP contract checks on temporary SQLite files with a fake Add provider."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_server
from records import ProviderError


TEST_KEY = "http-tests-only-not-a-real-secret"


def png_uri(color="red"):
    buffer = io.BytesIO()
    with Image.new("RGB", (12, 10), color) as image:
        image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


class FakeAddProvider:
    """Injected provider: no credentials, networking, or environmental model mode."""

    enabled = True

    def __init__(self, *, delay=0.0, fail_on_call=None):
        self.calls = 0
        self.delay = delay
        self.fail_on_call = fail_on_call
        self.lock = threading.Lock()
        self.local = threading.local()

    def config_dict(self):
        return {"provider": "test-fake", "prompt_version": "http-test-v1"}

    def start_add(self):
        self.local.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    def usage(self):
        return dict(getattr(self.local, "usage", {"calls": 0}))

    def _record(self):
        if not hasattr(self.local, "usage"):
            self.start_add()
        with self.lock:
            self.calls += 1
            call_number = self.calls
        self.local.usage["calls"] += 1
        if self.delay:
            time.sleep(self.delay)
        if call_number == self.fail_on_call:
            raise ProviderError("fake provider unavailable")
        return "contractneedle source indexing hints"

    def index_code(self, text):
        return self._record()

    def index_text(self, text):
        return self._record()

    def describe_image(self, data_uri):
        return self._record()


@contextmanager
def running_server(track, *, model=None, max_request_bytes=64 * 1024):
    with tempfile.TemporaryDirectory(prefix="chronicle-http-test-") as temporary:
        db_path = Path(temporary) / "memory.sqlite3"
        server = create_server(
            "127.0.0.1", 0, track=track, db_path=str(db_path), api_key=TEST_KEY,
            model=model, max_request_bytes=max_request_bytes,
            add_concurrency=1, search_concurrency=4,
        )
        # Join request workers on close, so Windows can remove the SQLite files.
        server.daemon_threads = False
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
            name=f"http-test-{track}",
        )
        thread.start()
        try:
            yield server, db_path, f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            server.store.close()
            if thread.is_alive():
                raise RuntimeError("HTTP test server did not shut down")


class HTTPContractChecks:
    """Shared contracts are run independently for each frozen track."""

    track = None

    def setUp(self):
        self.model = FakeAddProvider()
        self.runtime = running_server(self.track, model=self.model)
        self.server, self.db_path, self.base_url = self.runtime.__enter__()
        self.addCleanup(self.runtime.__exit__, None, None, None)
        self.image_uri = png_uri()

    def source_content(self, text="contractneedle source evidence"):
        if self.track == "coding":
            return text + "\n def load_config(raw): return raw\n"
        return [
            {"type": "text", "text": text + " first"},
            {"type": "image_url", "image_url": {"url": self.image_uri}},
            {"type": "text", "text": " last source part\n"},
        ]

    def query_content(self):
        if self.track == "coding":
            return "contractneedle load_config"
        return [
            {"type": "text", "text": "contractneedle source evidence"},
            {"type": "image_url", "image_url": {"url": self.image_uri}},
        ]

    def add_body(self, *, request_id="http-write", user_id="http-user", session_id="http-session"):
        return {
            "request_id": request_id, "user_id": user_id, "session_id": session_id,
            "messages": [{"role": "user", "timestamp": 1704067200000,
                          "content": self.source_content()}],
        }

    def search_body(self, *, user_id="http-user", top_k=100):
        return {"query": self.query_content(), "user_id": user_id, "top_k": top_k}

    def request(self, method, path, payload=None, *, auth="x-api-key", raw=None, base_url=None):
        headers = {"Content-Type": "application/json", "Connection": "close"}
        if auth == "x-api-key":
            headers["X-Api-Key"] = TEST_KEY
        elif auth in {"bearer", "token"}:
            headers["Authorization"] = auth.title() + " " + TEST_KEY
        elif auth == "wrong":
            headers["X-Api-Key"] = "wrong-test-value"
        elif auth is not None:
            raise ValueError(f"Unknown test auth mode: {auth}")
        body = raw if raw is not None else (
            None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        request = Request((base_url or self.base_url) + path, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=8) as response:
                return response.status, json.loads(response.read()), dict(response.headers)
        except HTTPError as error:
            try:
                return error.code, json.loads(error.read()), dict(error.headers)
            finally:
                error.close()

    def count(self, table):
        allowed = {"raw_messages", "source_records", "memories", "add_requests", "model_usage", "memories_fts"}
        if table not in allowed:
            raise ValueError("Unknown test table")
        connection = sqlite3.connect(self.db_path)
        try:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        finally:
            connection.close()

    def assert_error(self, status, body, expected):
        self.assertEqual(status, expected)
        self.assertIsInstance(body.get("detail"), dict)
        self.assertIsInstance(body["detail"].get("reason"), str)
        self.assertTrue(body["detail"]["reason"])
        self.assertIsNot(body.get("success"), True)

    def test_health_is_public_and_reports_bound_track(self):
        status, body, headers = self.request("GET", "/health", auth=None)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["track"], self.track)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(self.model.calls, 0)

    def test_all_declared_auth_schemes_allow_add_and_search(self):
        for auth in ("token", "bearer", "x-api-key"):
            with self.subTest(auth=auth):
                status, body, _ = self.request("POST", "/add", self.add_body(request_id=auth), auth=auth)
                self.assertEqual(status, 200)
                self.assertIs(body["success"], True)
                status, body, _ = self.request("POST", "/search", self.search_body(), auth=auth)
                self.assertEqual(status, 200)
                self.assertTrue(body["data"])

    def test_missing_and_wrong_credentials_cannot_write_or_read(self):
        for auth in (None, "wrong"):
            for path, payload in (("/add", self.add_body()), ("/search", self.search_body())):
                with self.subTest(auth=auth, path=path):
                    status, body, _ = self.request("POST", path, payload, auth=auth)
                    self.assert_error(status, body, 401)
        self.assertEqual(self.count("memories"), 0)
        self.assertEqual(self.model.calls, 0)

    def test_ids_are_echoed_exactly_and_whitespace_is_an_isolation_boundary(self):
        add = self.add_body(request_id=" request-edge \t", user_id=" user-edge \n", session_id=" session-edge ")
        status, body, _ = self.request("POST", "/add", add)
        self.assertEqual(status, 200)
        self.assertIs(body["success"], True)
        for field in ("request_id", "user_id", "session_id"):
            self.assertEqual(body[field], add[field])
        status, result, _ = self.request("POST", "/search", self.search_body(user_id=add["user_id"]))
        self.assertEqual(status, 200)
        self.assertTrue(result["data"])
        for other_user in (add["user_id"].strip(), "different-user"):
            status, result, _ = self.request("POST", "/search", self.search_body(user_id=other_user))
            self.assertEqual(status, 200)
            self.assertEqual(result, {"data": []})

    def test_retry_is_a_noop_and_changed_body_or_session_is_conflict(self):
        add = self.add_body()
        status, body, _ = self.request("POST", "/add", add)
        self.assertEqual(status, 200)
        model_calls = self.model.calls
        status, retried, _ = self.request("POST", "/add", copy.deepcopy(add))
        self.assertEqual(status, 200)
        self.assertEqual(retried, body)
        self.assertEqual(self.model.calls, model_calls)
        changed = copy.deepcopy(add)
        changed["messages"][0]["content"] = self.source_content("changed source")
        changed_session = {**add, "session_id": "changed-session"}
        changed_payloads = [changed, changed_session]
        if self.track == "multimodal":
            changed_image = copy.deepcopy(add)
            changed_image["messages"][0]["content"][1]["image_url"]["url"] = png_uri("blue")
            changed_payloads.append(changed_image)
        for payload in changed_payloads:
            status, error, _ = self.request("POST", "/add", payload)
            self.assert_error(status, error, 409)
        self.assertEqual(self.model.calls, model_calls)
        for table in ("raw_messages", "source_records", "memories", "add_requests", "model_usage"):
            self.assertEqual(self.count(table), 1, table)

    def test_track_contract_returns_at_most_100_source_records_with_stable_ids(self):
        add = self.add_body()
        add["messages"] = [
            {"role": "user" if index % 2 == 0 else "assistant",
             "content": self.source_content(f"contractneedle source record {index}")}
            for index in range(110)
        ]
        self.assertEqual(self.request("POST", "/add", add)[0], 200)
        status, result, _ = self.request("POST", "/search", self.search_body(top_k=100))
        self.assertEqual(status, 200)
        self.assertEqual(len(result["data"]), 100)
        self.assertEqual(len({row["id"] for row in result["data"]}), 100)
        for row in result["data"]:
            self.assertIsInstance(row["id"], str)
            self.assertTrue(row["id"])
            self.assertNotIn("answer", row)
            if self.track == "coding":
                self.assertIsInstance(row["content"], str)
                self.assertIn("contractneedle source record", row["content"])
                self.assertIn("def load_config", row["content"])
            else:
                self.assertEqual([part["type"] for part in row["content"]], ["text", "image_url", "text"])
                self.assertEqual(row["content"][1]["image_url"]["url"], self.image_uri)
                self.assertEqual(row["content"][2]["text"], " last source part\n")
        status, repeated, _ = self.request("POST", "/search", self.search_body(top_k=100))
        self.assertEqual(status, 200)
        self.assertEqual([row["id"] for row in repeated["data"]], [row["id"] for row in result["data"]])
        for top_k in (1, 7):
            status, limited, _ = self.request("POST", "/search", self.search_body(top_k=top_k))
            self.assertEqual(status, 200)
            self.assertEqual(len(limited["data"]), top_k)

    def test_invalid_add_content_role_timestamp_and_envelope_are_422(self):
        invalid = []
        for messages in (None, [], "not messages", [{"role": "system", "content": self.source_content()}]):
            invalid.append({**self.add_body(), "messages": messages})
        bad_content = [None, "", " \t\n", {}, 7]
        if self.track == "coding":
            bad_content.append([{"type": "text", "text": "code"}])
        else:
            bad_content.extend([
                [], [{"type": "text", "text": " "}],
                [{"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}],
                [{"type": "image_url", "image_url": {"url": "data:image/png;base64,%%%"}}],
                [{"type": "unknown", "text": "unsupported"}],
            ])
        for content in bad_content:
            invalid.append({**self.add_body(), "messages": [{"role": "user", "content": content}]})
        for timestamp in (True, 1.5, "1704067200000", 10 ** 50):
            invalid.append({**self.add_body(), "messages": [{"role": "user", "content": self.source_content(),
                                                           "timestamp": timestamp}]})
        for field in ("request_id", "user_id", "session_id"):
            invalid.append({**self.add_body(), field: " \t"})
        for index, payload in enumerate(invalid):
            with self.subTest(index=index):
                status, body, _ = self.request("POST", "/add", payload)
                self.assert_error(status, body, 422)
        self.assertEqual(self.count("raw_messages"), 0)
        self.assertEqual(self.count("memories"), 0)
        self.assertEqual(self.count("add_requests"), 0)
        self.assertEqual(self.model.calls, 0)

    def test_invalid_search_query_options_and_top_k_are_422(self):
        invalid = []
        queries = (None, "", " \t", [], {}, [{"type": "text", "text": ""}])
        for query in queries:
            invalid.append({**self.search_body(), "query": query})
        for options in ("A", {"A": "choice"}, [1], [""], [" "]):
            invalid.append({**self.search_body(), "options": options})
        for top_k in (None, True, 0, -1, 101, 1.5, "100"):
            invalid.append(self.search_body(top_k=top_k))
        for user_id in (None, "", " \t"):
            invalid.append(self.search_body(user_id=user_id))
        for index, payload in enumerate(invalid):
            with self.subTest(index=index):
                status, body, _ = self.request("POST", "/search", payload)
                self.assert_error(status, body, 422)
        self.assertEqual(self.model.calls, 0)

    def test_malformed_json_nonobject_and_invalid_utf8_are_422(self):
        for raw in (b"{not-json", b"[]", b"null", b'{"query":"\xff"}'):
            for path in ("/add", "/search"):
                with self.subTest(raw=raw, path=path):
                    status, body, _ = self.request("POST", path, raw=raw)
                    self.assert_error(status, body, 422)
        self.assertEqual(self.count("memories"), 0)
        self.assertEqual(self.model.calls, 0)

    def test_oversize_body_returns_413_without_processing(self):
        with running_server(self.track, model=self.model, max_request_bytes=128) as (_, _, base_url):
            for path in ("/add", "/search"):
                status, body, _ = self.request("POST", path, {"oversized": "x" * 512}, base_url=base_url)
                self.assert_error(status, body, 413)
        self.assertEqual(self.model.calls, 0)

    def test_provider_error_is_503_without_acknowledgement_or_partial_write(self):
        self.model.fail_on_call = 2
        add = self.add_body()
        add["messages"].append({"role": "assistant", "content": self.source_content("second evidence")})
        status, body, _ = self.request("POST", "/add", add)
        self.assert_error(status, body, 503)
        self.assertEqual(self.model.calls, 2)
        for table in ("raw_messages", "source_records", "memories", "add_requests", "model_usage"):
            self.assertEqual(self.count(table), 0, table)
        if self.server.store._fts_enabled:
            self.assertEqual(self.count("memories_fts"), 0)
        status, result, _ = self.request("POST", "/search", self.search_body())
        self.assertEqual(status, 200)
        self.assertEqual(result, {"data": []})
        self.assertEqual(self.model.calls, 2)

    def test_unknown_get_and_post_paths_are_404(self):
        for method, payload in (("GET", None), ("POST", {})):
            status, body, _ = self.request(method, "/unknown", payload)
            self.assert_error(status, body, 404)
        self.assertEqual(self.model.calls, 0)

    def test_parallel_duplicate_adds_retry_rate_limits_and_commit_one_record_set(self):
        self.model.delay = 0.10
        barrier = threading.Barrier(6)
        add = self.add_body(request_id="parallel-write")

        def submit():
            barrier.wait(timeout=5)
            return self.request("POST", "/add", add)

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: submit(), range(6)))
        for status, body, headers in results:
            self.assertIn(status, (200, 429))
            if status == 429:
                self.assertEqual(headers.get("Retry-After"), "1")
                status, body, _ = self.request("POST", "/add", add)
                self.assertEqual(status, 200)
            self.assertIs(body["success"], True)
        self.assertEqual(self.model.calls, 1)
        for table in ("raw_messages", "source_records", "memories", "add_requests", "model_usage"):
            self.assertEqual(self.count(table), 1, table)
        status, result, _ = self.request("POST", "/search", self.search_body())
        self.assertEqual(status, 200)
        self.assertEqual(len(result["data"]), 1)


class CodingHTTPTests(HTTPContractChecks, unittest.TestCase):
    track = "coding"


class MultimodalHTTPTests(HTTPContractChecks, unittest.TestCase):
    track = "multimodal"


if __name__ == "__main__":
    unittest.main()
