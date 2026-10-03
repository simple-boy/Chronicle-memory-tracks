"""Durable Add/Search integration checks with synthetic source data and fake models."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coding import CodingProcessor
from memory_core import MemoryStore
from multimodal import MultimodalProcessor
from records import PayloadTooLarge, PreparedEvidence, ProviderError
from store import TrackStore


MIB = 1024 * 1024
SCOPED_TABLES = (
    "memories", "memory_entities", "memory_relations", "raw_messages",
    "source_records", "add_requests", "model_usage",
)


def image_part(color="red", size=(8, 8)):
    buffer = io.BytesIO()
    with Image.new("RGB", size, color) as image:
        image.save(buffer, format="PNG")
    uri = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": uri}}


def parts(text="cache_anchor original source", color="red"):
    return [{"type": "text", "text": text}, image_part(color)]


class CountingModel:
    """Records calls locally; these are counters, never paid inference."""

    enabled = True

    def __init__(self, *, fail_on=None, revision="fake-v1"):
        self.calls = []
        self.start_count = 0
        self.fail_on = fail_on
        self.revision = revision
        self.current_usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    def config_dict(self):
        return {"provider": "test-only", "model": "gpt-4o-mini", "revision": self.revision}

    def start_add(self):
        self.start_count += 1
        self.current_usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    def usage(self):
        return dict(self.current_usage)

    def _call(self, kind, source):
        self.calls.append((kind, source))
        self.current_usage["calls"] += 1
        self.current_usage["input_tokens"] += 13
        self.current_usage["output_tokens"] += 7
        if self.fail_on is not None and self.fail_on in source:
            raise ProviderError("synthetic provider failure")
        return "DERIVED_ONLY_HINT lifecycle caption"

    def index_code(self, source):
        return self._call("text", source)

    index_text = index_code

    def describe_image(self, data_uri):
        return self._call("image", data_uri)


class TrackStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chronicle-track-store-")
        self.addCleanup(self.temp.cleanup)

    def open_store(self, processor, name="memory.sqlite3", *, temporal=True):
        database = str(Path(self.temp.name) / name)
        store = TrackStore(database, processor, temporal=temporal)
        self.addCleanup(store.close)
        return store

    @staticmethod
    def add(store, content, *, request_id="request-1", user_id="user-a", session_id="session-1"):
        store.add_messages(request_id=request_id, user_id=user_id, session_id=session_id,
                           messages=[{"role": "user", "content": content}])

    @staticmethod
    def record_ids(store, user_id="user-a"):
        return [row[0] for row in store._connection().execute(
            "SELECT memory_id FROM source_records WHERE user_id=? ORDER BY memory_id", (user_id,))]

    @staticmethod
    def counts(store, user_id="user-a"):
        tables = [*SCOPED_TABLES, *(["memories_fts"] if store._fts_enabled else [])]
        return {table: store._connection().execute(
            f"SELECT COUNT(*) FROM {table} WHERE user_id=?", (user_id,)).fetchone()[0]
                for table in tables}

    def test_coding_and_multimodal_ids_survive_retry_reopen_and_independent_rebuild(self):
        for track in ("coding", "multimodal"):
            with self.subTest(track=track):
                factory = CodingProcessor if track == "coding" else MultimodalProcessor
                source = "cache_anchor exact source\n" if track == "coding" else parts()
                first = self.open_store(factory(), f"{track}-first.sqlite3")
                self.add(first, source)
                before_ids = self.record_ids(first)
                before_result = first.search(user_id="user-a", query="cache_anchor")
                self.assertTrue(before_ids)
                self.add(first, json.loads(json.dumps(source)))
                self.assertEqual(self.record_ids(first), before_ids)
                first.close()
                reopened = self.open_store(factory(), f"{track}-first.sqlite3")
                self.assertEqual(reopened.search(user_id="user-a", query="cache_anchor"), before_result)
                rebuilt = self.open_store(factory(), f"{track}-second.sqlite3")
                self.add(rebuilt, source)
                self.assertEqual(self.record_ids(rebuilt), before_ids)

    def test_both_tracks_search_all_own_sessions_without_cross_user_evidence(self):
        for track in ("coding", "multimodal"):
            with self.subTest(track=track):
                processor = CodingProcessor() if track == "coding" else MultimodalProcessor()
                store = self.open_store(processor, f"{track}-scope.sqlite3")
                for request, scope, session, text in (
                    ("one", "user-a", "s1", "cache_anchor FIRST_SESSION_SOURCE"),
                    ("two", "user-a", "s2", "cache_anchor SECOND_SESSION_SOURCE"),
                    ("one", "user-b", "s1", "cache_anchor OTHER_USER_SECRET"),
                ):
                    content = text if track == "coding" else parts(text)
                    self.add(store, content, request_id=request, user_id=scope, session_id=session)
                own = store.search(user_id="user-a", query="cache_anchor")
                other = store.search(user_id="user-b", query="cache_anchor")
                self.assertEqual({row["id"] for row in own}, set(self.record_ids(store, "user-a")))
                self.assertEqual({row["id"] for row in other}, set(self.record_ids(store, "user-b")))
                self.assertEqual(len(own), 2)
                self.assertNotIn("OTHER_USER_SECRET", json.dumps(own))
                self.assertNotIn("FIRST_SESSION_SOURCE", json.dumps(other))
                self.assertFalse(set(self.record_ids(store, "user-a")) & set(self.record_ids(store, "user-b")))

    def test_committed_retries_never_repeat_model_calls_or_usage_records(self):
        for track in ("coding", "multimodal"):
            with self.subTest(track=track):
                model = CountingModel()
                processor = CodingProcessor(model) if track == "coding" else MultimodalProcessor(model)
                store = self.open_store(processor, f"{track}-retry.sqlite3")
                content = "cache_anchor source" if track == "coding" else parts()
                self.add(store, content)
                calls_before, starts_before = list(model.calls), model.start_count
                usage_before = store._connection().execute("SELECT usage_json FROM model_usage").fetchone()[0]
                with patch.object(processor, "media_bytes", side_effect=AssertionError("retry must skip processing")):
                    self.add(store, json.loads(json.dumps(content)))
                self.assertEqual(model.calls, calls_before)
                self.assertEqual(model.start_count, starts_before)
                self.assertEqual(store._connection().execute("SELECT usage_json FROM model_usage").fetchone()[0],
                                 usage_before)
                self.assertEqual(self.counts(store)["add_requests"], 1)
                self.assertEqual(self.counts(store)["model_usage"], 1)

    def test_request_id_reuse_with_changed_body_session_or_order_is_conflict(self):
        store = self.open_store(CodingProcessor(CountingModel()))
        messages = [{"role": "user", "content": "cache_anchor first"},
                    {"role": "assistant", "content": "cache_anchor second"}]
        store.add_messages(request_id="r", user_id="user-a", session_id="s", messages=messages)
        calls_before = list(store.processor.model.calls)
        for session, changed in (
            ("another-session", messages),
            ("s", [{"role": "user", "content": "changed source"}]),
            ("s", list(reversed(messages))),
        ):
            with self.subTest(session=session, changed=changed), self.assertRaisesRegex(ValueError, "reused"):
                store.add_messages(request_id="r", user_id="user-a", session_id=session, messages=changed)
        self.assertEqual(store.processor.model.calls, calls_before)
        self.assertEqual(self.counts(store)["memories"], 2)

    def test_coding_forget_imperative_is_source_and_does_not_delete_evidence(self):
        store = self.open_store(CodingProcessor())
        fact = "cache_anchor Alice uses CacheAlpha"
        self.add(store, fact)
        first_ids = self.record_ids(store)
        with patch("memory_core.explicit_forget_target", side_effect=AssertionError("old forget path used")), \
             patch.object(MemoryStore, "_forget_exact_fact", side_effect=AssertionError("old deletion path used")):
            self.add(store, "forget " + fact, request_id="request-2")
        self.assertTrue(set(first_ids).issubset(self.record_ids(store)))
        self.assertEqual(self.counts(store)["memories"], 2)
        returned = store.search(user_id="user-a", query="cache_anchor")
        self.assertEqual(len(returned), 2)
        self.assertTrue(any(row["content"].endswith("forget " + fact) for row in returned))

    def test_both_tracks_return_only_source_not_model_generated_index_text(self):
        for track in ("coding", "multimodal"):
            with self.subTest(track=track):
                model = CountingModel()
                processor = CodingProcessor(model) if track == "coding" else MultimodalProcessor(model)
                store = self.open_store(processor, f"{track}-source.sqlite3")
                source = "def save_cache():\n    return 'CACHE_SOURCE'\n" if track == "coding" else parts()
                self.add(store, source)
                calls_before = list(model.calls)
                result = store.search(user_id="user-a", query="DERIVED_ONLY_HINT")
                self.assertTrue(result, "derived hints should be searchable as indexes")
                self.assertEqual(model.calls, calls_before, "Search must not call a model")
                self.assertNotIn("DERIVED_ONLY_HINT", json.dumps(result))
                if track == "coding":
                    self.assertTrue(result[0]["content"].endswith(source))
                else:
                    self.assertEqual(result[0]["content"], source)
                raw = json.loads(store._connection().execute("SELECT content_json FROM raw_messages").fetchone()[0])
                self.assertEqual(raw["content"], source)

    def test_bad_later_message_keeps_all_existing_tables_unchanged(self):
        for track in ("coding", "multimodal"):
            with self.subTest(track=track):
                processor = CodingProcessor() if track == "coding" else MultimodalProcessor()
                store = self.open_store(processor, f"{track}-invalid.sqlite3")
                source = "cache_anchor committed" if track == "coding" else parts()
                self.add(store, source)
                counts_before = self.counts(store)
                invalid = None if track == "coding" else [{"type": "image_url", "image_url": {"url": "https://invalid.example/x"}}]
                with self.assertRaises(ValueError):
                    store.add_messages(request_id="invalid-batch", user_id="user-a", session_id="s2", messages=[
                        {"role": "user", "content": source}, {"role": "assistant", "content": invalid}])
                self.assertEqual(self.counts(store), counts_before)
                self.assertTrue(store.search(user_id="user-a", query="cache_anchor"))

    def test_provider_failure_on_second_message_commits_no_partial_batch(self):
        for track in ("coding", "multimodal"):
            with self.subTest(track=track):
                model = CountingModel(fail_on="FAIL_SECOND")
                processor = CodingProcessor(model) if track == "coding" else MultimodalProcessor(model)
                store = self.open_store(processor, f"{track}-provider.sqlite3")
                self.add(store, "cache_anchor already committed")
                counts_before = self.counts(store)
                with self.assertRaisesRegex(ProviderError, "synthetic provider failure"):
                    store.add_messages(request_id="failed-provider", user_id="user-a", session_id="s2", messages=[
                        {"role": "user", "content": "cache_anchor FIRST_NEW_SOURCE"},
                        {"role": "assistant", "content": "FAIL_SECOND"}])
                self.assertEqual(model.usage()["calls"], 2)
                self.assertEqual(self.counts(store), counts_before)
                self.assertNotIn("FIRST_NEW_SOURCE", json.dumps(store.search(user_id="user-a", query="cache_anchor")))

    def test_sql_phase_bad_second_index_rolls_back_first_record_and_fts(self):
        processor = CodingProcessor()
        store = self.open_store(processor)
        self.add(store, "cache_anchor existing source")
        counts_before = self.counts(store)
        real_prepare = processor.prepare

        def injected_prepare(content):
            if content == "bad generated index":
                return [PreparedEvidence(content, "", {})]
            return real_prepare(content)

        with patch.object(processor, "prepare", side_effect=injected_prepare), self.assertRaisesRegex(ValueError, "empty index"):
            store.add_messages(request_id="sql-failure", user_id="user-a", session_id="s2", messages=[
                {"role": "user", "content": "cache_anchor NEW_INSERT_MUST_ROLL_BACK"},
                {"role": "assistant", "content": "bad generated index"}])
        self.assertEqual(self.counts(store), counts_before)
        self.assertNotIn("NEW_INSERT_MUST_ROLL_BACK", json.dumps(store.search(user_id="user-a", query="cache_anchor")))

    def test_database_freezes_track_processor_temporal_and_model_config(self):
        initial = self.open_store(CodingProcessor(chunk_chars=64), "frozen.sqlite3")
        self.add(initial, "cache_anchor durable source")
        initial.close()
        self.open_store(CodingProcessor(chunk_chars=64), "frozen.sqlite3").close()
        for processor, temporal in (
            (CodingProcessor(chunk_chars=65), True),
            (CodingProcessor(chunk_chars=64), False),
            (MultimodalProcessor(), True),
        ):
            with self.subTest(processor=processor.name, temporal=temporal), self.assertRaisesRegex(ValueError, "different track/config"):
                self.open_store(processor, "frozen.sqlite3", temporal=temporal)
        model_store = self.open_store(CodingProcessor(CountingModel(revision="fake-v1")), "model-frozen.sqlite3")
        model_store.close()
        with self.assertRaisesRegex(ValueError, "different track/config"):
            self.open_store(CodingProcessor(CountingModel(revision="fake-v2")), "model-frozen.sqlite3")
        mm_store = self.open_store(MultimodalProcessor(max_pixels=100), "mm-frozen.sqlite3")
        mm_store.close()
        with self.assertRaisesRegex(ValueError, "different track/config"):
            self.open_store(MultimodalProcessor(max_pixels=101), "mm-frozen.sqlite3")

    def test_visual_search_recalls_target_outside_first_hundred_lexical_candidates(self):
        processor = MultimodalProcessor()
        store = self.open_store(processor)
        blue = [image_part("blue")]
        store.add_messages(request_id="blue-batch", user_id="user-a", session_id="s1", messages=[
            {"role": "user", "content": blue} for _ in range(120)])
        target = [{"type": "text", "text": "target_source " + "padding_token " * 200}, image_part("red")]
        self.add(store, target, request_id="target-request", session_id="s2")
        target_id = store._connection().execute(
            "SELECT r.memory_id FROM source_records r JOIN raw_messages s ON s.source_id=r.source_id WHERE s.request_id=?",
            ("target-request",)).fetchone()[0]
        query = [image_part("red")]
        lexical = MemoryStore.search(store, user_id="user-a", query=processor.query(query).text, top_k=100)
        self.assertEqual(len(lexical), 100)
        self.assertNotIn(target_id, {row["id"] for row in lexical})
        result = store.search(user_id="user-a", query=query, top_k=1)
        self.assertEqual(result[0]["id"], target_id)
        self.assertEqual(result[0]["content"], target)

    def test_add_media_budget_checks_aggregate_decoded_bytes_before_model_calls(self):
        model = CountingModel()
        processor = MultimodalProcessor(model)
        store = self.open_store(processor)
        source = parts()
        with patch.object(processor, "media_bytes", return_value=16 * MIB), self.assertRaises(PayloadTooLarge):
            store.add_messages(request_id="too-large", user_id="user-a", session_id="s1", messages=[
                {"role": "user", "content": source}, {"role": "user", "content": source}])
        self.assertEqual(model.calls, [])
        self.assertTrue(all(count == 0 for count in self.counts(store).values()))
        with patch.object(processor, "media_bytes", return_value=15 * MIB):
            store.add_messages(request_id="exact-limit", user_id="user-a", session_id="s1", messages=[
                {"role": "user", "content": source}, {"role": "user", "content": source}])
        self.assertEqual(self.counts(store)["source_records"], 2)

    def test_search_media_budget_keeps_whole_ranked_evidence_prefix_and_parts(self):
        processor = MultimodalProcessor()
        store = self.open_store(processor)
        first = parts("cache_anchor first", "red") + [image_part("blue")]
        second = parts("cache_anchor second", "green")
        third = parts("cache_anchor third", "yellow")
        store.add_messages(request_id="r", user_id="user-a", session_id="s1", messages=[
            {"role": "user", "content": content} for content in (first, second, third)])
        unbounded = store.search(user_id="user-a", query="cache_anchor")
        self.assertEqual(len(unbounded), 3)
        sizes = {json.dumps(row["content"], sort_keys=True): size
                 for row, size in zip(unbounded, (20 * MIB, 11 * MIB, 1 * MIB))}
        with patch.object(processor, "media_bytes", side_effect=lambda content: sizes[json.dumps(content, sort_keys=True)]):
            bounded = store.search(user_id="user-a", query="cache_anchor")
        self.assertEqual(bounded, unbounded[:1], "budget must keep a ranked prefix, not skip the large middle record")
        self.assertEqual(bounded[0]["content"], unbounded[0]["content"])
        with patch.object(processor, "media_bytes", return_value=15 * MIB):
            exact_limit = store.search(user_id="user-a", query="cache_anchor")
        self.assertEqual(exact_limit, unbounded[:2])
        with patch.object(processor, "media_bytes", return_value=31 * MIB):
            self.assertEqual(store.search(user_id="user-a", query="cache_anchor"), [])

    def test_delete_user_clears_all_scoped_tables_preserving_other_user_and_config(self):
        for track in ("coding", "multimodal"):
            with self.subTest(track=track):
                model = CountingModel()
                processor = CodingProcessor(model) if track == "coding" else MultimodalProcessor(model)
                store = self.open_store(processor, f"{track}-delete.sqlite3")
                for user in ("user-a", "user-b"):
                    text = "Alice uses CacheAlpha. cache_anchor original source"
                    source = text if track == "coding" else parts(text)
                    self.add(store, source, user_id=user)
                before_other = self.counts(store, "user-b")
                self.assertTrue(all(count > 0 for count in self.counts(store).values()))
                config_before = store._connection().execute("SELECT value FROM memory_meta WHERE key='track_config'").fetchone()[0]
                store.delete_user("user-a")
                self.assertTrue(all(count == 0 for count in self.counts(store).values()))
                self.assertEqual(self.counts(store, "user-b"), before_other)
                self.assertEqual(store.search(user_id="user-a", query="cache_anchor"), [])
                self.assertTrue(store.search(user_id="user-b", query="cache_anchor"))
                self.assertEqual(store._connection().execute("SELECT value FROM memory_meta WHERE key='track_config'").fetchone()[0],
                                 config_before)

    def test_per_request_model_usage_is_persisted_and_search_adds_no_calls(self):
        model = CountingModel()
        store = self.open_store(CodingProcessor(model))
        messages = [{"role": "user", "content": "cache_anchor first source"},
                    {"role": "assistant", "content": "cache_anchor second source"}]
        store.add_messages(request_id="usage-1", user_id="user-a", session_id="s1", messages=messages)
        self.add(store, "cache_anchor third source", request_id="usage-2")
        usage = {row[0]: json.loads(row[1]) for row in store._connection().execute(
            "SELECT request_id,usage_json FROM model_usage WHERE user_id=?", ("user-a",))}
        self.assertEqual(usage["usage-1"], {"calls": 2, "input_tokens": 26, "output_tokens": 14})
        self.assertEqual(usage["usage-2"], {"calls": 1, "input_tokens": 13, "output_tokens": 7})
        before = list(model.calls)
        store.search(user_id="user-a", query="cache_anchor")
        self.assertEqual(model.calls, before)
        store.close()
        reopened = self.open_store(CodingProcessor(CountingModel()))
        self.assertEqual(reopened._connection().execute("SELECT COUNT(*) FROM model_usage").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
