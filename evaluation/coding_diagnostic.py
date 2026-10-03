"""100 synthetic scoped coding-retrieval cases; this is NOT CAMBench.

This deterministic diagnostic stresses five engineered features. Its labels stay
inside the evaluator and are never sent to Add. It measures direct store calls,
not HTTP, software-task resolution, an official score, or real-world performance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from pathlib import Path
import sys
import tempfile
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from coding import CodingProcessor
from memory_core import MemoryStore
from store import TrackStore


class IndexOnlyCodingProcessor(CodingProcessor):
    """Controlled ablation: the same indexes/store, without feature reranking."""

    def score(self, query_features: dict, record_features: dict) -> float:
        return 0.0

    def config_dict(self) -> dict:
        return {**super().config_dict(), "diagnostic_ablation": "no_feature_score"}


def make_cases() -> list[dict]:
    cases = []
    for topic in ("symbols", "paths", "errors", "noise", "languages"):
        for variant in range(20):
            if topic == "symbols":
                query = "Which request fingerprint helper handles reconnect?"
                target = f"request_fingerprint_{variant}() computes the stable key for reconnect."
                distractors = [
                    "The request fingerprint helper dashboard shows sample events.",
                    "The request helper serializes the HTTP body before sending.",
                    "The reconnect helper clears cached authentication credentials.",
                ]
            elif topic == "paths":
                path = f"src/storage/cache_{variant}.py"
                query = f"Where was eviction repaired in {path}?"
                target = f"In {path}, eviction was repaired by preserving the pinned cache entry."
                distractors = [
                    f"In tests/storage/cache_{variant}.py, eviction has a synthetic fixture.",
                    f"In src/network/cache_{variant}.py, eviction clears socket state.",
                    "The storage eviction documentation describes the expected behavior.",
                ]
            elif topic == "errors":
                query = "Which socket timeout error trace identified connection disposal?"
                target = (f'Traceback (most recent call last):\n  File "src/connect_{variant}.py", line 17\n'
                          "SocketTimeoutError: connection disposal interrupted the pending operation.")
                distractors = [
                    "Socket timeout error logs were added to the monitoring dashboard.",
                    "Connection disposal completed after normal shutdown. Tests passed.",
                    "A socket connection trace shows an unrelated InvalidStateError.",
                ]
            elif topic == "noise":
                query = "Which bounded retry backoff handled a request timeout?"
                target = "A bounded retry backoff handled a request timeout by preserving the retry budget."
                distractors = [
                    "INFO heartbeat retry backoff request timeout\n" * (10 + variant),
                    "DEBUG polling retry backoff request timeout\n" * (10 + variant),
                    "The request timeout counter was exported to the dashboard.",
                ]
            else:
                language = ("C", "C++", "C#")[variant % 3]
                query = f"Which {language} compiler bounds repair was used?"
                target = f"The {language} compiler bounds repair was checked against the regression fixture."
                distractors = [
                    f"The {other} compiler bounds repair was checked against the regression fixture."
                    for other in ("C", "C++", "C#") if other != language
                ] + ["Compiler bounds diagnostics were documented for the test harness."]
            # Vary source order without random sampling or semantic labels in IDs.
            history = list(distractors)
            gold_index = variant % 4
            history.insert(gold_index, target)
            number = len(cases)
            cases.append({
                "case_id": f"synthetic_{number:03d}", "topic": topic,
                "user_id": f"synthetic_scope_{number:03d}", "query": query,
                "history": history, "gold_index": gold_index,
            })
    assert len(cases) == 100
    return cases


def _code_hashes() -> dict:
    return {name: hashlib.sha256((PROJECT / name).read_bytes()).hexdigest()
            for name in ("coding.py", "store.py", "memory_core.py", "evaluation/coding_diagnostic.py")}


def _metrics(rows: list[dict], top_k: int) -> dict:
    size = len(rows)
    return {"queries": size,
            "recall_at_1": sum(row["rank"] == 1 for row in rows) / size,
            f"recall_at_{top_k}": sum(row["rank"] is not None for row in rows) / size,
            f"mrr_at_{top_k}": sum(1 / row["rank"] if row["rank"] else 0 for row in rows) / size,
            "returned_records": sum(row["returned_count"] for row in rows),
            "scope_violations": sum(row["scope_violations"] for row in rows)}


def run_diagnostic(top_k: int = 5) -> dict:
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 100:
        raise ValueError("top_k must be an integer from 1 to 100")
    cases = make_cases()
    before_hashes = _code_hashes()
    results = {}
    factories = {
        # Match the inherited engine's feature scope to TrackStore(temporal=False).
        "raw_v2_aligned": lambda path: MemoryStore(path, features={"entity", "options"}),
        "coding_index_only": lambda path: TrackStore(path, IndexOnlyCodingProcessor(), temporal=False),
        "coding_with_features": lambda path: TrackStore(path, CodingProcessor(), temporal=False),
    }
    with tempfile.TemporaryDirectory(prefix="synthetic_coding_diagnostic_") as temporary:
        for method, factory in factories.items():
            path = str(Path(temporary) / f"{method}.sqlite3")
            started = time.perf_counter()
            store = factory(path)
            cold_start_seconds = time.perf_counter() - started
            add_seconds, search_seconds = 0.0, 0.0
            gold_ids = {}
            try:
                for number, case in enumerate(cases):
                    request_id = f"synthetic_add_{number:03d}"
                    payload = {"request_id": request_id, "user_id": case["user_id"],
                               "session_id": f"synthetic_session_{number:03d}",
                               "messages": [{"role": "assistant", "content": content}
                                            for content in case["history"]]}
                    started = time.perf_counter()
                    store.add_messages(**payload)
                    add_seconds += time.perf_counter() - started
                    gold_id = MemoryStore._message_id(case["user_id"], request_id, case["gold_index"])
                    gold_ids[case["case_id"]] = gold_id + ("_0" if isinstance(store, TrackStore) else "")
                # Foreign-scope histories intentionally have highly matching text.
                # Their presence tests real storage isolation rather than mocked metadata.
                started = time.perf_counter()
                store.add_messages(request_id="synthetic_foreign_add", user_id="synthetic_foreign_scope",
                                   session_id="synthetic_foreign_session",
                                   messages=[{"role": "assistant", "content": case["query"]}
                                             for case in cases])
                add_seconds += time.perf_counter() - started
                id_scopes = {row["memory_id"]: row["user_id"] for row in store._connection().execute(
                    "SELECT memory_id,user_id FROM memories")}
                rows = []
                for case in cases:
                    started = time.perf_counter()
                    returned = store.search(user_id=case["user_id"], query=case["query"], top_k=top_k)
                    elapsed = time.perf_counter() - started
                    search_seconds += elapsed
                    ids = [record["id"] for record in returned]
                    gold_id = gold_ids[case["case_id"]]
                    rank = ids.index(gold_id) + 1 if gold_id in ids else None
                    violations = sum(id_scopes.get(memory_id) != case["user_id"] for memory_id in ids)
                    rows.append({"case_id": case["case_id"], "topic": case["topic"],
                                 "rank": rank, "returned_count": len(returned),
                                 "search_seconds": elapsed, "scope_violations": violations})
                memory_count = store._connection().execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                results[method] = {
                    "overall": _metrics(rows, top_k),
                    "topics": {topic: _metrics([row for row in rows if row["topic"] == topic], top_k)
                               for topic in sorted({case["topic"] for case in cases})},
                    "timing_seconds": {"cold_start": cold_start_seconds, "add_total": add_seconds,
                                       "search_total": search_seconds, "search_mean": search_seconds / 100},
                    "counts": {"add_requests": 101, "input_messages": 500, "stored_records": memory_count,
                               "model_calls": 0, "api_requests": 0, "query_count": 100},
                    "per_query": rows,
                }
            finally:
                store.close()
            results[method]["database_bytes_after_close"] = Path(path).stat().st_size
    after_hashes = _code_hashes()
    return {
        "name": "synthetic_coding_diagnostic_100", "official_benchmark": False,
        "dataset": "synthetic; NOT CAMBench", "top_k": top_k,
        "design": "Five engineered topics, 20 deterministic cases each; four in-scope histories per query; 100 foreign-scope histories.",
        "comparison": "raw V2 with aligned feature scope; coding index only; same coding indexes with processor feature scoring",
        "limitations": ["Handwritten synthetic labels, not natural task or verifier evidence.",
                        "Feature-specific diagnostic; no software-task resolution or official score.",
                        "Direct store calls exclude HTTP, model/API cost and deployment overhead.",
                        "One deterministic run; no random-seed variance or performance generalization."],
        "manifest_sha256": hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "code_sha256": before_hashes, "code_unchanged_during_run": before_hashes == after_hashes,
        "environment": {"python": sys.version, "platform": platform.platform()},
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--output", type=Path, help="Optional full JSON report path")
    args = parser.parse_args()
    report = run_diagnostic(args.top_k)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # stdout stays compact; the optional artifact includes per-query records.
    summary = {key: value for key, value in report.items() if key != "results"}
    summary["results"] = {method: {key: value for key, value in result.items() if key != "per_query"}
                          for method, result in report["results"].items()}
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
