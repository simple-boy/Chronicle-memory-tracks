"""Tests for authored data and its transparent non-oracle patch consumer."""

import copy
import difflib
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation.fixture_consumer import apply_unified_diff, consume, extract_diffs, run_fixture, safe_target
from scripts.build_dev_dataset import FAMILIES, build_dataset, unified
from scripts.evaluate import retrieval_metrics, source_units, writer_payloads


class FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = build_dataset("dev", len(FAMILIES), 11, validate=True)

    def test_each_family_assumptions_executed(self):
        self.assertEqual({case["family"] for case in self.cases}, set(FAMILIES))
        for case in self.cases:
            with self.subTest(family=case["family"]):
                self.assertEqual(case["construction_validation"], {"executed": True, "initial_success": False, "intended_patch_success": True, "alternate_patch_success": False})

    def test_intended_patch_roundtrip_all_families(self):
        for case in self.cases:
            with self.subTest(family=case["family"]):
                patch = extract_diffs(case["messages"][1]["content"])[0]
                self.assertEqual(apply_unified_diff(case["program_source"], patch, case["target_path"]), case["authored_repaired_source"])

    def test_no_memory_really_executes_and_fails(self):
        result = consume(self.cases[0], [])
        self.assertFalse(result["success"])
        self.assertFalse(result["patched"])
        self.assertIsNotNone(result["exit_code"])
        self.assertIn("Ran 3 tests", result["stderr"])

    def test_first_patch_selected_without_oracle_retries(self):
        case = self.cases[0]
        rows = [{"id": "first", "content": case["messages"][7]["content"]}, {"id": "second", "content": case["messages"][1]["content"]}]
        result = consume(case, rows)
        self.assertEqual(result["selected_id"], "first")
        self.assertFalse(result["success"])

    def test_failure_marked_bundle_skipped_then_run_once(self):
        case = self.cases[0]
        failed_bundle = case["messages"][5]["content"] + "\n\n" + case["messages"][6]["content"]
        rows = [{"id": "attempt", "content": failed_bundle}, {"id": "implementation", "content": case["messages"][1]["content"]}]
        result = consume(case, rows)
        self.assertEqual(result["selected_id"], "implementation")
        self.assertEqual(result["skipped_failure_bundles"], 1)
        self.assertTrue(result["success"])

    def test_consumer_does_not_read_labels(self):
        original = self.cases[0]
        changed = copy.deepcopy(original)
        changed["relevant_indices"], changed["harmful_indices"] = [7], [1]
        changed["authored_repaired_source"] = "raise RuntimeError('unused label field')\n"
        rows = [{"id": "same", "content": original["messages"][1]["content"]}]
        self.assertTrue(consume(changed, rows)["success"])

    def test_unrelated_module_not_harmful_label(self):
        for case in self.cases:
            self.assertNotIn(3, case["harmful_indices"])
            self.assertNotIn(3, case["relevant_indices"])
            self.assertTrue(set(case["harmful_indices"]).isdisjoint(case["caution_indices"]))

    def test_no_memory_labels_or_future_query_in_source(self):
        for case in self.cases:
            for message in case["messages"]:
                self.assertNotIn(case["query"], message["content"])
                self.assertFalse({"relevant_indices", "harmful_indices", "program_source", "test_source"}.intersection(message))
                for label in ("gold", "relevant", "noise"):
                    self.assertNotIn(label, message["content"].casefold())

    def test_splits_source_groups_disjoint_and_seeded(self):
        dev = build_dataset("dev", 4, 23, validate=False)
        evaluation = build_dataset("eval", 4, 23, validate=False)
        self.assertTrue({case["group"] for case in dev}.isdisjoint({case["group"] for case in evaluation}))
        self.assertTrue({case["target_path"] for case in dev}.isdisjoint({case["target_path"] for case in evaluation}))
        self.assertEqual(dev, build_dataset("dev", 4, 23, validate=False))

    def test_unsafe_paths_rejected(self):
        for path in ("../x.py", "/tmp/x.py", "C:/x.py", r"src\x.py"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                safe_target(path)

    def test_patch_target_and_context_mismatch_rejected(self):
        case = self.cases[0]
        patch = extract_diffs(case["messages"][1]["content"])[0]
        with self.assertRaises(ValueError):
            apply_unified_diff("def other():\n    pass\n", patch, case["target_path"])
        with self.assertRaises(ValueError):
            apply_unified_diff(case["program_source"], patch, "other.py")

    def test_multiple_hunks_roundtrip(self):
        source = "".join(f"value_{index} = {index}\n" for index in range(30))
        updated = source.replace("value_1 = 1\n", "value_1 = 99\n").replace("value_28 = 28\n", "value_28 = 77\n")
        patch = unified(source, updated, "src/item.py")
        self.assertGreaterEqual(patch.count("@@ -"), 2)
        self.assertEqual(apply_unified_diff(source, patch, "src/item.py"), updated)

    def test_incomplete_hunk_rejected(self):
        patch = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n-old\n+new\n"
        with self.assertRaises(ValueError):
            apply_unified_diff("old\n", patch, "x.py")

    def test_minimal_environment_not_inherited(self):
        tests = "import os\nimport unittest\nclass EnvTests(unittest.TestCase):\n    def test_environment(self):\n        self.assertNotIn('OPENAI_API_KEY', os.environ)\n        self.assertNotIn('MEMORY_API_KEY', os.environ)\n"
        result = run_fixture("pass\n", tests, "src/safe.py")
        self.assertTrue(result["success"], result["stderr"])

    def test_timeout_is_explicit(self):
        result = run_fixture("pass\n", "while True:\n    pass\n", "src/safe.py", timeout=0.2)
        self.assertFalse(result["success"])
        self.assertTrue(result["timed_out"])

    def test_writer_excludes_query_labels_and_test_program(self):
        case = self.cases[0]
        payloads = writer_payloads(case, "a-user", "noisy")
        self.assertEqual(sum(len(item["messages"]) for item in payloads), len(case["messages"]))
        for payload in payloads:
            self.assertEqual(set(payload), {"request_id", "user_id", "session_id", "messages"})
            for message in payload["messages"]:
                self.assertEqual(set(message), {"role", "content", "timestamp"})

    def test_source_matching_checks_exact_slice_and_bounds(self):
        messages = [{"content": "abcdefgh"}]
        rows = [{"content": "[Source id=x; session=s; role=user; chars=2:5]\ncde"}]
        self.assertEqual(source_units(rows, messages), ([0], [[0]], 1, 1))
        rows[0]["content"] = "[Source id=x; session=s; role=user; chars=2:5]\nXYZ"
        self.assertEqual(source_units(rows, messages), ([], [[]], 0, 1))

    def test_metrics_deduplicate_bundle_sources_and_measure_closure(self):
        case = self.cases[0]
        def block(index):
            text = case["messages"][index]["content"]
            return f"[Source id={index}; session=s; role=user; chars=0:{len(text)}]\n{text}"
        rows = [{"id": "a", "content": block(1) + "\n\n" + block(2)}, {"id": "b", "content": block(1)}, {"id": "c", "content": block(5) + "\n\n" + block(6)}]
        metrics = retrieval_metrics(case, rows)
        self.assertEqual(metrics["ordered_source_indices"], [1, 2, 5, 6])
        self.assertEqual(metrics["recall@1"], 0.5)
        self.assertEqual(metrics["recall@5"], 1.0)
        self.assertEqual(metrics["ndcg@5"], 1.0)
        self.assertEqual(metrics["failure_closure_rate"], 1.0)

    def test_executed_transcripts_are_reproducible_after_disclosed_normalization(self):
        second = build_dataset("dev", 1, 11, validate=True)[0]
        self.assertEqual(self.cases[0]["messages"], second["messages"])


if __name__ == "__main__":
    unittest.main()
