"""Offline Add-model client tests: all URL opens and sleeps are mocked."""

from __future__ import annotations

import io
import json
import math
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import models
from models import ModelClient
from records import ProviderError


TEST_KEY = "fake-model-test-only-key"
MODEL_URL = "https://api.openai.com/v1/chat/completions"


def completion(text="  observed retrieval index  ", *, finish_reason="stop", input_tokens=7, output_tokens=2):
    return {
        "choices": [{"message": {"content": text}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens},
    }


def response(value):
    return io.BytesIO(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def http_error(code):
    return HTTPError(MODEL_URL, code, "test HTTP failure", hdrs=None, fp=io.BytesIO(b"test upstream body"))


class ModelClientTests(unittest.TestCase):
    def setUp(self):
        opener = patch.object(
            models.urllib.request, "urlopen", side_effect=AssertionError("Real network is forbidden in model tests")
        )
        self.urlopen = opener.start()
        self.addCleanup(opener.stop)
        sleeper = patch.object(models.time, "sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        # Protect against an accidental new path that bypasses the patched urlopen.
        network_guard = patch(
            "socket.create_connection", side_effect=AssertionError("Real socket connections are forbidden")
        )
        network_guard.start()
        self.addCleanup(network_guard.stop)

    def client(self, **kwargs):
        return ModelClient(mode="openai", api_key=TEST_KEY, **kwargs)

    def successful_opener(self, result=None):
        value = completion() if result is None else result
        self.urlopen.side_effect = lambda *args, **kwargs: response(value)

    def test_none_is_disabled_and_never_opens_network(self):
        client = ModelClient(mode="none")
        self.assertFalse(client.enabled)
        self.assertEqual(client.index_code("def source(): pass"), "")
        self.assertEqual(client.index_text("source text"), "")
        self.assertEqual(client.describe_image("data:image/png;base64,AA=="), "")
        self.assertEqual(client.usage(), {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        self.urlopen.assert_not_called()
        self.sleep.assert_not_called()

    def test_only_declared_modes_models_and_image_details_are_accepted(self):
        for model in ("gpt-4o-mini", "gpt-4o-mini-2024-07-18"):
            for detail in ("high", "low"):
                client = self.client(model=model, image_detail=detail)
                self.assertTrue(client.enabled)
                self.assertEqual(client.config_dict()["model"], model)
                self.assertEqual(client.config_dict()["vision_detail"], detail)
        invalid = [
            {"mode": "mock"}, {"mode": "other"}, {"mode": "openai", "api_key": ""},
            {"model": "gpt-4o"}, {"model": "unapproved-model"},
            {"image_detail": "auto"}, {"image_detail": "High"},
        ]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ModelClient(**kwargs)
        self.urlopen.assert_not_called()

    def test_from_env_reads_explicit_configuration_and_freezes_budgets_without_key(self):
        environment = {
            "MODEL_PROVIDER": "openai", "OPENAI_API_KEY": TEST_KEY,
            "ADD_MODEL": "gpt-4o-mini-2024-07-18", "VISION_DETAIL": "low",
            "MODEL_TIMEOUT_SECONDS": "2.5", "MODEL_MAX_CALLS_PER_ADD": "3",
            "MODEL_ADD_BUDGET_SECONDS": "7.5",
        }
        with patch.dict(os.environ, environment, clear=True):
            client = ModelClient.from_env()
        config = client.config_dict()
        self.assertEqual(config["provider"], "openai")
        self.assertEqual(config["model"], environment["ADD_MODEL"])
        self.assertEqual(config["vision_detail"], "low")
        self.assertEqual(config["timeout"], 2.5)
        self.assertEqual(config["max_calls_per_add"], 3)
        self.assertEqual(config["add_budget_seconds"], 7.5)
        self.assertEqual(config["max_output_tokens"], 384)
        self.assertTrue(config["prompt_version"])
        self.assertNotIn(TEST_KEY, json.dumps(config))
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(ModelClient.from_env().enabled)
        self.urlopen.assert_not_called()

    def test_timeouts_and_add_deadline_budget_must_be_positive_and_finite(self):
        for field in ("timeout", "add_budget_seconds"):
            for value in (0, -1, math.nan, math.inf, -math.inf):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.client(**{field: value})
        self.urlopen.assert_not_called()

    def test_call_and_output_budgets_must_be_positive_finite_integers(self):
        for field in ("max_calls_per_add", "max_output_tokens"):
            for value in (0, -1, math.nan, math.inf, -math.inf, 1.5, True):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.client(**{field: value})
        self.urlopen.assert_not_called()

    def test_code_request_preserves_source_and_tracks_usage(self):
        self.successful_opener()
        client = self.client(max_output_tokens=211)
        source = "def load_config():\n    return '中文'\n"
        self.assertEqual(client.index_code(source), "observed retrieval index")
        request = self.urlopen.call_args.args[0]
        self.assertEqual(request.full_url, MODEL_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TEST_KEY)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "gpt-4o-mini")
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual(payload["max_tokens"], 211)
        self.assertEqual([message["role"] for message in payload["messages"]], ["system", "user"])
        self.assertEqual(payload["messages"][1]["content"], "SOURCE EVIDENCE:\n" + source)
        self.assertNotIn(TEST_KEY, request.data.decode("utf-8"))
        self.assertEqual(client.usage(), {"calls": 1, "input_tokens": 7, "output_tokens": 2})
        self.assertEqual(client.index_text("second source"), "observed retrieval index")
        self.assertEqual(client.usage(), {"calls": 2, "input_tokens": 14, "output_tokens": 4})
        usage_copy = client.usage()
        usage_copy["calls"] = -1
        self.assertEqual(client.usage()["calls"], 2)
        self.sleep.assert_not_called()

    def test_image_request_preserves_data_uri_and_configured_detail(self):
        self.successful_opener()
        client = self.client(image_detail="low")
        uri = "data:image/png;base64,AA=="
        self.assertEqual(client.describe_image(uri), "observed retrieval index")
        payload = json.loads(self.urlopen.call_args.args[0].data)
        parts = payload["messages"][1]["content"]
        self.assertEqual([part["type"] for part in parts], ["text", "image_url"])
        self.assertEqual(parts[1]["image_url"], {"url": uri, "detail": "low"})
        self.assertEqual(client.usage()["calls"], 1)

    def test_401_is_permanent_and_not_retried(self):
        failure = http_error(401)
        body = failure.fp
        self.addCleanup(failure.close)
        self.urlopen.side_effect = failure
        client = self.client()
        with self.assertRaisesRegex(ProviderError, "HTTP 401"):
            client.index_code("source")
        self.assertEqual(self.urlopen.call_count, 1)
        self.assertEqual(client.usage(), {"calls": 1, "input_tokens": 0, "output_tokens": 0})
        self.assertTrue(body.closed)
        self.sleep.assert_not_called()

    def test_429_and_5xx_retry_once_then_use_successful_usage(self):
        for code in (429, 500, 503):
            with self.subTest(code=code):
                self.urlopen.reset_mock()
                self.sleep.reset_mock()
                self.urlopen.side_effect = [http_error(code), response(completion())]
                client = self.client()
                self.assertEqual(client.index_code("source"), "observed retrieval index")
                self.assertEqual(self.urlopen.call_count, 2)
                self.sleep.assert_called_once_with(1)
                self.assertEqual(client.usage(), {"calls": 2, "input_tokens": 7, "output_tokens": 2})

    def test_retry_limit_returns_provider_error_without_a_third_attempt(self):
        for code in (429, 500):
            with self.subTest(code=code):
                self.urlopen.reset_mock()
                self.sleep.reset_mock()
                # Mock may raise the same exception twice; closing it must stay safe.
                failure = http_error(code)
                body = failure.fp
                self.addCleanup(failure.close)
                self.urlopen.side_effect = failure
                client = self.client()
                with self.assertRaisesRegex(ProviderError, f"HTTP {code}"):
                    client.index_code("source")
                self.assertEqual(self.urlopen.call_count, 2)
                self.assertEqual(client.usage()["calls"], 2)
                self.assertTrue(body.closed)
                self.sleep.assert_called_once_with(1)

    def test_connection_failures_retry_boundedly_without_real_sleep(self):
        for failure in (URLError("offline test failure"), TimeoutError("offline test timeout")):
            with self.subTest(failure=type(failure).__name__):
                self.urlopen.reset_mock()
                self.sleep.reset_mock()
                self.urlopen.side_effect = failure
                client = self.client()
                with self.assertRaisesRegex(ProviderError, "connection unavailable"):
                    client.index_code("source")
                self.assertEqual(self.urlopen.call_count, 2)
                self.sleep.assert_called_once_with(1)

    def test_malformed_json_and_damaged_response_shapes_are_provider_errors(self):
        damaged = {
            "invalid JSON": b"not JSON", "null top level": b"null", "array top level": b"[]",
            "missing choices": json.dumps({"usage": {}}).encode(),
            "empty choices": json.dumps({"choices": []}).encode(),
            "nonobject choice": json.dumps({"choices": [7]}).encode(),
            "missing message": json.dumps({"choices": [{"finish_reason": "stop"}]}).encode(),
            "null message": json.dumps({"choices": [{"message": None, "finish_reason": "stop"}]}).encode(),
            "array message": json.dumps({"choices": [{"message": [], "finish_reason": "stop"}]}).encode(),
            "null usage": json.dumps({**completion(), "usage": None}).encode(),
            "bad usage count": json.dumps(completion(input_tokens="not a number")).encode(),
        }
        for label, raw in damaged.items():
            with self.subTest(shape=label):
                self.urlopen.reset_mock()
                self.sleep.reset_mock()
                self.urlopen.side_effect = lambda *args, raw=raw, **kwargs: io.BytesIO(raw)
                client = self.client()
                with self.assertRaises(ProviderError):
                    client.index_code("source")
                self.assertEqual(self.urlopen.call_count, 1)
                self.sleep.assert_not_called()

    def test_incomplete_or_empty_model_text_fails_without_retry(self):
        invalid = [
            completion(finish_reason="length"), completion(finish_reason="content_filter"),
            completion(finish_reason="tool_calls"), completion(finish_reason=None),
            completion(text=""), completion(text=" \t\n"), completion(text=None), completion(text=[]),
        ]
        for index, value in enumerate(invalid):
            with self.subTest(index=index):
                self.urlopen.reset_mock()
                self.sleep.reset_mock()
                self.successful_opener(value)
                client = self.client()
                with self.assertRaisesRegex(ProviderError, "incomplete"):
                    client.index_code("source")
                self.assertEqual(self.urlopen.call_count, 1)
                self.sleep.assert_not_called()

    def test_default_64_call_budget_counts_attempts_and_resets_for_next_add(self):
        self.successful_opener()
        client = self.client()
        for _ in range(64):
            self.assertEqual(client.index_code("source"), "observed retrieval index")
        with self.assertRaisesRegex(ProviderError, "model-call budget exhausted"):
            client.index_code("source")
        self.assertEqual(self.urlopen.call_count, 64)
        self.assertEqual(client.usage(), {"calls": 64, "input_tokens": 64 * 7, "output_tokens": 64 * 2})
        client.start_add()
        self.assertEqual(client.usage(), {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        client.index_code("new Add source")
        self.assertEqual(client.usage()["calls"], 1)
        self.assertEqual(self.urlopen.call_count, 65)

    def test_retry_cannot_bypass_a_single_call_budget(self):
        self.urlopen.side_effect = http_error(429)
        client = self.client(max_calls_per_add=1)
        with self.assertRaisesRegex(ProviderError, "model-call budget exhausted"):
            client.index_code("source")
        self.assertEqual(self.urlopen.call_count, 1)
        self.assertEqual(client.usage()["calls"], 1)

    def test_deadline_expiry_prevents_the_first_request_and_call_charge(self):
        client = self.client(add_budget_seconds=10)
        with patch.object(models.time, "monotonic", side_effect=[100.0, 110.0]):
            client.start_add()
            with self.assertRaisesRegex(ProviderError, "time budget exhausted"):
                client.index_code("source")
        self.urlopen.assert_not_called()
        self.assertEqual(client.usage()["calls"], 0)
        self.sleep.assert_not_called()

    def test_request_timeout_is_capped_by_remaining_add_budget(self):
        self.successful_opener()
        client = self.client(timeout=90, add_budget_seconds=10)
        with patch.object(models.time, "monotonic", side_effect=[100.0, 106.0]):
            client.start_add()
            client.index_code("source")
        self.assertEqual(self.urlopen.call_args.kwargs["timeout"], 4.0)
        self.assertEqual(client.usage()["calls"], 1)

    def test_deadline_is_shared_across_calls_until_start_add_resets_it(self):
        self.successful_opener()
        client = self.client(add_budget_seconds=10)
        with patch.object(models.time, "monotonic", side_effect=[100.0, 102.0, 111.0]):
            client.start_add()
            client.index_code("first source")
            with self.assertRaisesRegex(ProviderError, "time budget exhausted"):
                client.index_code("second source")
        self.assertEqual(self.urlopen.call_count, 1)
        self.assertEqual(client.usage()["calls"], 1)
        with patch.object(models.time, "monotonic", side_effect=[200.0, 201.0]):
            client.start_add()
            client.index_code("new Add source")
        self.assertEqual(self.urlopen.call_count, 2)
        self.assertEqual(client.usage()["calls"], 1)

    def test_deadline_exhaustion_after_retryable_error_blocks_retry(self):
        self.urlopen.side_effect = http_error(429)
        client = self.client(add_budget_seconds=10)
        with patch.object(models.time, "monotonic", side_effect=[100.0, 101.0, 111.0]):
            client.start_add()
            with self.assertRaisesRegex(ProviderError, "time budget exhausted"):
                client.index_code("source")
        self.assertEqual(self.urlopen.call_count, 1)
        self.assertEqual(client.usage()["calls"], 1)
        self.sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
