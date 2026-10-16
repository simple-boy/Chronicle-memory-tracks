"""Deterministic parser fixtures, independent of private evaluation data."""

import time
import unittest

from forgetrace.parsing import parse_message, query_features, tokenize


class ParsingTests(unittest.TestCase):
    def assert_spans(self, source, parsed):
        for item in parsed["evidence"] + parsed["outcome_spans"]:
            self.assertGreaterEqual(item["start"], 0)
            self.assertGreater(item["end"], item["start"])
            self.assertEqual(source[item["start"]:item["end"]], item["text"])

    def test_full_identifier_and_components(self):
        tokens = tokenize("get_user_by_id getUserById src/auth/middleware.py TypeError pkg.parseUser")
        for item in ("get_user_by_id", "getuserbyid", "get", "user", "by", "id", "src/auth/middleware.py", "middleware", "typeerror", "type", "error", "pkg.parseuser"):
            self.assertIn(item, tokens)
        self.assertEqual(len(tokens), len(set(tokens)))

    def test_nonexpanded_retains_full_identifiers(self):
        tokens = tokenize("getUserById src/auth.py", expand=False)
        self.assertIn("getuserbyid", tokens)
        self.assertIn("src/auth.py", tokens)
        self.assertNotIn("get", tokens)

    def test_distinct_repository_paths(self):
        source = "Edit src/auth.py, not tests/auth.py. See ./pkg/auth.py:42."
        parsed = parse_message(source)
        self.assertIn("src/auth.py", parsed["paths"])
        self.assertIn("tests/auth.py", parsed["paths"])
        self.assertIn("./pkg/auth.py", parsed["paths"])
        self.assertNotEqual(parsed["paths"].index("src/auth.py"), parsed["paths"].index("tests/auth.py"))
        self.assert_spans(source, parsed)

    def test_absolute_windows_and_unix_paths(self):
        parsed = parse_message(r"File C:\repo\src\auth.py and /opt/project/main.rs")
        self.assertIn(r"C:\repo\src\auth.py", parsed["paths"])
        self.assertIn("/opt/project/main.rs", parsed["paths"])

    def test_url_not_repository_path(self):
        parsed = parse_message("Read https://example.com/docs/api first.")
        self.assertFalse(parsed["paths"])

    def test_declarations_calls_and_namespaced_symbols(self):
        source = "class AuthService:\n  def get_user_by_id(self):\n    return pkg.loadUser()\n"
        parsed = parse_message(source)
        for symbol in ("AuthService", "get_user_by_id", "pkg.loadUser"):
            self.assertIn(symbol, parsed["symbols"])

    def test_error_names_and_traceback(self):
        source = "Traceback (most recent call last):\n  TypeError: invalid token\nValueError: bad value"
        parsed = parse_message(source)
        self.assertEqual(parsed["errors"], ["TypeError", "ValueError"])
        self.assertEqual(parsed["outcome"], "unverified")
        self.assert_spans(source, parsed)

    def test_version_and_constraint_source(self):
        source = "Requires: Django==4.2.3; Python 3.11; numpy>=1.26.0; node@18.1.0"
        parsed = parse_message(source)
        for item in ({"package": "django", "version": "4.2.3"}, {"package": "python", "version": "3.11"}, {"package": "numpy", "version": "1.26.0"}, {"package": "node", "version": "18.1.0"}):
            self.assertIn(item, parsed["versions"])
        constraints = [s["text"] for s in parsed["evidence"] if s["kind"] == "constraint"]
        self.assertIn("numpy>=1.26.0", constraints)
        self.assert_spans(source, parsed)

    def test_code_and_unclosed_fence_preserved(self):
        source = "Example:\n```python\ndef getUser():\n    print('3 passed')\n"
        parsed = parse_message(source)
        self.assertEqual(parsed["outcome"], "unverified")
        self.assertEqual([s["text"] for s in parsed["evidence"] if s["kind"] == "code"], [source[source.index("```"):]])
        self.assert_spans(source, parsed)

    def test_patch_source_and_task_action_cause(self):
        source = "Task: handle None\nRoot cause: absent guard\nAction: add guard\n```diff\n--- a/src/auth.py\n+++ b/src/auth.py\n@@ -1 +1 @@\n-return x.id\n+return x.id if x else None\n```\n"
        parsed = parse_message(source)
        kinds = {item["kind"] for item in parsed["evidence"]}
        self.assertTrue({"task", "cause", "action", "diff", "path"}.issubset(kinds))
        self.assert_spans(source, parsed)

    def test_pytest_actual_report_is_only_reported(self):
        source = "$ python -m pytest tests/test_auth.py -q\n=== 12 passed in 0.10s ==="
        for role in ("user", "assistant"):
            parsed = parse_message(source, role)
            self.assertEqual(parsed["outcome"], "reported_pass")
            self.assertNotEqual(parsed["outcome"], "verified")
            self.assertIn("python -m pytest tests/test_auth.py -q", parsed["tests"])
            self.assert_spans(source, parsed)

    def test_zero_pass_with_fail_does_not_pass(self):
        parsed = parse_message("pytest -q\n0 passed, 3 failed in 1.2s")
        self.assertEqual(parsed["outcome"], "reported_fail")

    def test_mixed_single_summary_is_failure(self):
        parsed = parse_message("pytest\n8 passed, 1 failed, 2 warnings in 1s")
        self.assertEqual(parsed["outcome"], "reported_fail")

    def test_future_unexecuted_claims_not_success(self):
        for source in ("The tests will pass.", "pytest should report 12 passed.", "If tests passed, publish.", "I expect 3 passed.", 'echo "3 passed"'):
            with self.subTest(source=source):
                self.assertEqual(parse_message(source)["outcome"], "unverified")

    def test_negation_does_not_pass(self):
        for source in ("Tests have not passed.", "The tests never passed.", "Tests didn't pass.", "Not all tests passed.", "No tests passed."):
            with self.subTest(source=source):
                parsed = parse_message(source)
                self.assertNotEqual(parsed["outcome"], "reported_pass")
                self.assertEqual(parsed["outcome"], "reported_fail")

    def test_questions_and_negated_failures_not_execution_results(self):
        for source in ("Have tests passed?", "No tests failed."):
            with self.subTest(source=source):
                self.assertEqual(parse_message(source)["outcome"], "unverified")

    def test_multiple_reported_runs_preserve_conflict(self):
        source = "Initial validation: 2 failed\nAfter patch:\n3 passed in 0.1s"
        parsed = parse_message(source)
        self.assertEqual(parsed["outcome"], "conflicting")
        self.assertEqual(len(parsed["outcome_spans"]), 2)
        self.assert_spans(source, parsed)

    def test_unittest_summary(self):
        self.assertEqual(parse_message("Ran 5 tests in 0.125s\n\nOK\n")["outcome"], "reported_pass")
        self.assertEqual(parse_message("Everything is OK")["outcome"], "unverified")

    def test_unicode_offsets_and_crlf(self):
        source = "任务: 修复用户🙂\r\nTask: prevent crash\r\npytest -q\r\n2 passed in 0.1s\r\n"
        self.assert_spans(source, parse_message(source))
        self.assertIn("用户", tokenize(source))

    def test_query_is_ephemeral_same_vocabulary(self):
        query = "Fix TypeError in src/auth.py getUserById for Django==4.2"
        self.assertEqual(query_features(query), parse_message(query))
        self.assertIn("getuserbyid", query_features(query)["tokens"])

    def test_missing_information_not_invented(self):
        parsed = parse_message("Please improve the implementation.")
        for key in ("paths", "symbols", "errors", "versions", "tests", "outcome_spans"):
            self.assertEqual(parsed[key], [])
        self.assertEqual(parsed["outcome"], "unverified")

    def test_long_log_retains_tail_without_silent_truncation(self):
        source = ("INFO same repeated line\n" * 10000) + "Action: repair final_path/end.py\n1 passed in 0.1s"
        started = time.perf_counter()
        parsed = parse_message(source)
        self.assertIn("final_path/end.py", parsed["paths"])
        self.assertEqual(parsed["outcome"], "reported_pass")
        self.assert_spans(source, parsed)
        self.assertLess(time.perf_counter() - started, 5.0)

    def test_invalid_types_rejected(self):
        for value in (None, 1, [], {}):
            with self.subTest(value=value), self.assertRaises(TypeError):
                parse_message(value)

    def test_long_dotted_identifier_has_bounded_failure_cost(self):
        source = "x." * 15000 + "tail"
        started = time.perf_counter()
        parsed = parse_message(source)
        self.assertIn(source, parsed["symbols"])
        self.assertLess(time.perf_counter() - started, 5.0)

    def test_nonpackage_version_words_not_invented(self):
        parsed = parse_message("Upgrade from 3.11 to 3.12, version 3.12.0")
        self.assertEqual(parsed["versions"], [])

    def test_very_long_numeric_summary_does_not_raise_integer_limit(self):
        source = "9" * 5000 + " passed in 0.1s"
        self.assertEqual(parse_message(source)["outcome"], "reported_pass")


if __name__ == "__main__":
    unittest.main()
