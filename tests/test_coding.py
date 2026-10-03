"""Meaningful checks for coding evidence fidelity and retrieval features."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coding import CodingProcessor
from records import ProviderError


class CodingProcessorTests(unittest.TestCase):
    def test_all_source_spans_restore_whitespace_repetition_and_unicode(self):
        source = "\r\n  def load_config():\r\n\treturn '中文'  \r\n\r\n" * 4 + " \t\n"
        processor = CodingProcessor(chunk_chars=31)
        records = processor.prepare(source)
        self.assertEqual("".join(record.content for record in records), source)
        position = 0
        for number, record in enumerate(records):
            self.assertEqual(record.features["source_start"], position)
            self.assertEqual(record.features["chunk_index"], number)
            self.assertEqual(record.content, source[position:record.features["source_end"]])
            self.assertLessEqual(len(record.content), 31)
            position = record.features["source_end"]
        self.assertEqual(position, len(source))
        # Whitespace inside a valid message remains source evidence even if an
        # entire bounded chunk contains only whitespace.
        padded = "head" + " \t\n" * 50 + "tail"
        padding_records = processor.prepare(padded)
        self.assertEqual("".join(record.content for record in padding_records), padded)
        whitespace_records = [record for record in padding_records if not record.content.strip()]
        self.assertTrue(whitespace_records)
        self.assertTrue(all(record.index_text.strip() for record in whitespace_records))
        self.assertTrue(all("_source_whitespace_" in record.index_text for record in whitespace_records))
        self.assertTrue(all(not record.features["terms"] for record in whitespace_records))

    def test_identifier_and_path_expansion_preserve_exact_source(self):
        source = 'File "src/network/http_server.py", line 42\nclass HTTPServer:\n    def load_config(self): pass'
        record = CodingProcessor().prepare(source)[0]
        self.assertEqual(record.content, source)
        self.assertIn("src/network/http_server.py", record.features["paths"])
        self.assertIn("httpserver", record.features["symbols"])
        self.assertIn("load_config", record.features["symbols"])
        for token in ("http", "server", "load", "config"):
            self.assertIn(token, record.index_text.split())

    def test_exact_path_and_symbol_outrank_nearby_names(self):
        processor = CodingProcessor()
        query = processor.query("Repair load_config in src/network/http_server.py")
        exact = processor.prepare("In src/network/http_server.py, load_config() validates the headers.")[0]
        wrong_path = processor.prepare("In tests/fixtures/http_server.py, load_config() validates the headers.")[0]
        wrong_symbol = processor.prepare("In src/network/http_server.py, save_config() validates the headers.")[0]
        self.assertGreater(processor.score(query.features, exact.features), processor.score(query.features, wrong_path.features))
        self.assertGreater(processor.score(query.features, exact.features), processor.score(query.features, wrong_symbol.features))

    def test_windows_path_and_error_codes_are_source_features(self):
        processor = CodingProcessor()
        source = 'C:\\repo\\src\\parser.ts:81:7: error TS2345\nENOENT: cannot open file'
        record = processor.prepare(source)[0]
        self.assertIn("c:/repo/src/parser.ts", record.features["paths"])
        self.assertIn("ts2345", record.features["errors"])
        self.assertIn("enoent", record.features["errors"])
        query = processor.query("TS2345 in C:/repo/src/parser.ts")
        unrelated = processor.prepare("src/other.ts compiled successfully.")[0]
        self.assertGreater(processor.score(query.features, record.features), processor.score(query.features, unrelated.features))

    def test_c_family_language_literals_do_not_collapse_to_one_token(self):
        processor = CodingProcessor()
        languages = {"C++": "cplusplus", "C#": "csharp", "C": "clanguage"}
        records = {language: processor.prepare(f"The {language} compiler fixes array bounds.")[0]
                   for language in languages}
        for language, alias in languages.items():
            query = processor.query(f"Find the {language} compiler array bounds fix")
            self.assertEqual(query.features["languages"], [alias])
            self.assertIn(alias, query.text.split())
            self.assertEqual(records[language].features["languages"], [alias])
            self.assertIn(alias, records[language].index_text.split())
            for other, record in records.items():
                if other != language:
                    self.assertGreater(processor.score(query.features, records[language].features),
                                       processor.score(query.features, record.features))
        alias_query = processor.query("cplusplus compiler array bounds")
        self.assertGreater(processor.score(alias_query.features, records["C++"].features),
                           processor.score(alias_query.features, records["C#"].features))
        self.assertEqual(processor.prepare("C:/repo/main.py config")[0].features["languages"], [])

    def test_language_weight_preserves_specific_path_and_symbol_priority(self):
        processor = CodingProcessor()
        query = processor.query("C++ load_config in src/config.cfg")
        exact_code = processor.prepare("C# load_config() in src/config.cfg validates input.")[0]
        generic_language = processor.prepare("C++ compiler configuration tips.")[0]
        self.assertGreater(processor.score(query.features, exact_code.features),
                           processor.score(query.features, generic_language.features))

    def test_failed_trace_is_kept_and_outranks_unrelated_success(self):
        processor = CodingProcessor()
        trace = 'Traceback (most recent call last):\n  File "src/cache.py", line 11, in update_cache\nValueError: cache key invalid\n3 failed'
        record = processor.prepare(trace)[0]
        query = processor.query("Debug ValueError in update_cache in src/cache.py")
        passed = processor.prepare("The formatting tests passed. Exit code 0.")[0]
        self.assertEqual(record.content, trace)
        self.assertTrue(record.features["stacktrace"])
        self.assertIn("valueerror", record.features["errors"])
        self.assertIn("3 failed", record.features["failure_cues"])
        self.assertGreater(processor.score(query.features, record.features), processor.score(query.features, passed.features))

    def test_noise_discount_is_query_conditioned(self):
        processor = CodingProcessor()
        noisy = processor.prepare("INFO heartbeat upload status\n" * 20 + "retry")[0]
        useful = processor.prepare("A retry uses the bounded backoff policy after a request timeout.")[0]
        query = processor.query("How does retry backoff handle a timeout?")
        self.assertGreater(noisy.features["noise"], useful.features["noise"])
        self.assertGreater(processor.score(query.features, useful.features), processor.score(query.features, noisy.features))
        # Equal query-term overlap isolates the discount from ordinary keyword ranking.
        terse = processor.prepare("retry")[0]
        self.assertEqual(set(query.features["terms"]) & set(noisy.features["terms"]),
                         set(query.features["terms"]) & set(terse.features["terms"]))
        self.assertGreater(processor.score(query.features, terse.features),
                           processor.score(query.features, noisy.features))
        # A diagnostic symbol in a noisy log still provides a strong exact anchor.
        trace = processor.prepare("INFO heartbeat upload status\n" * 20 + "ValueError in update_cache()")[0]
        diagnostic = processor.query("ValueError in update_cache()")
        self.assertGreater(processor.score(diagnostic.features, trace.features), processor.score(diagnostic.features, useful.features))

    def test_model_hints_are_index_only_and_query_does_not_call_model(self):
        class MockModel:
            def __init__(self):
                self.calls = []

            def index_code(self, chunk):
                self.calls.append(chunk)
                return "Connection lifecycle cleanup and socket disposal"

        model = MockModel()
        processor = CodingProcessor(model=model)
        source = "def close_socket(conn):\n    conn.close()\n"
        record = processor.prepare(source)[0]
        self.assertEqual(model.calls, [source])
        self.assertEqual(record.content, source)
        self.assertIn("lifecycle", record.index_text)
        self.assertNotIn("lifecycle", record.content)
        query = processor.query("connection lifecycle", ["cleanup", "retry"])
        self.assertEqual(model.calls, [source])
        self.assertIn("cleanup", query.features["option_terms"])
        self.assertGreater(processor.score(query.features, record.features), 0)

    def test_ids_and_condition_labels_are_not_ranking_oracles(self):
        processor = CodingProcessor()
        record = processor.prepare("load_config() handles configuration files.")[0]
        query = processor.query("load_config()")
        score = processor.score(query.features, record.features)
        self.assertEqual(score, processor.score({**query.features, "user_id": "relevant"}, {**record.features, "user_id": "noisy"}))
        self.assertEqual(score, processor.score({**query.features, "user_id": "noisy"}, {**record.features, "user_id": "relevant"}))

    def test_success_cues_do_not_hide_failures_or_negation(self):
        processor = CodingProcessor()
        record = processor.prepare("Not all tests passed. 12 passed, 3 failed. Tests did not pass.")[0]
        self.assertNotIn("all tests passed", [cue.casefold() for cue in record.features["success_cues"]])
        self.assertIn("12 passed", record.features["success_cues"])
        self.assertIn("3 failed", record.features["failure_cues"])
        self.assertIn("Tests did not pass", record.features["failure_cues"])

    def test_invalid_content_and_configuration_are_rejected(self):
        processor = CodingProcessor()
        for value in (None, "", " \t\r\n", [], {}, b"code", 5):
            with self.subTest(content=value), self.assertRaises(ValueError):
                processor.prepare(value)
        for size in (0, -1, True, 1.5, "10"):
            with self.subTest(chunk_chars=size), self.assertRaises(ValueError):
                CodingProcessor(chunk_chars=size)
        for value in (None, "", " \t", []):
            with self.subTest(query=value), self.assertRaises(ValueError):
                processor.query(value)
        for options in ("answer", [None], [""], ["  "]):
            with self.subTest(options=options), self.assertRaises(ValueError):
                processor.query("load_config", options)
        self.assertEqual(processor.media_bytes("source"), 0)

    def test_invalid_or_failed_model_hints_fail_preparation(self):
        class BadModel:
            def index_code(self, chunk):
                return None

        with self.assertRaises(ProviderError):
            CodingProcessor(model=BadModel()).prepare("def handler(): pass")

        class FailedModel:
            def index_code(self, chunk):
                raise ProviderError("provider unavailable")

        with self.assertRaisesRegex(ProviderError, "provider unavailable"):
            CodingProcessor(model=FailedModel()).prepare("def handler(): pass")


if __name__ == "__main__":
    unittest.main()
