"""Offline source-retrieval and executable-fixture diagnostics, not CAMBench.

All strategies receive identical historical messages without queries, labels,
test programs or target patches outside those recorded historical messages.
The fixture consumer executes once after the first applicable returned patch.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from evaluation.fixture_consumer import consume
from scripts.build_dev_dataset import DATASET_VERSION, build_dataset
from forgetrace.storage import MemoryStore

import re
_SOURCE = re.compile(r"\[Source id=([^;\n]+); session=[^;\n]+; role=[^;\n]+; chars=([0-9]+):([0-9]+)\]\n")
VARIANTS = ("raw", "structured", "proposed", "no_structure", "no_reliability", "no_applicability", "no_closure")


def code_manifest() -> dict[str, str]:
    paths = sorted((ROOT / "src").rglob("*.py")) + [Path(__file__), ROOT / "scripts/build_dev_dataset.py", ROOT / "evaluation/fixture_consumer.py"]
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def source_units(rows: list[dict[str, Any]], messages: list[dict[str, Any]]) -> tuple[list[int], list[list[int]], int, int]:
    """Recover only exact original excerpts; labels play no part in matching."""
    flattened: list[int] = []
    row_units: list[list[int]] = []
    exact = total = 0
    for row in rows:
        content = row.get("content", "")
        indices = []
        headers = list(_SOURCE.finditer(content))
        if headers:
            for header in headers:
                total += 1
                begin, end = int(header.group(2)), int(header.group(3))
                excerpt = content[header.end():header.end() + end - begin]
                matches = [index for index, message in enumerate(messages) if 0 <= begin < end <= len(message["content"]) and message["content"][begin:end] == excerpt]
                if matches:
                    exact += 1
                    indices.extend(matches)
        elif content:
            total += 1
            matches = [index for index, message in enumerate(messages) if message["content"] == content]
            exact += bool(matches)
            indices.extend(matches)
        indices = list(dict.fromkeys(indices))
        row_units.append(indices)
        flattened.extend(index for index in indices if index not in flattened)
    return flattened, row_units, exact, total


def retrieval_metrics(case: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered, bundles, exact, total = source_units(rows, case["messages"])
    relevant, harmful = set(case["relevant_indices"]), set(case["harmful_indices"])
    result: dict[str, Any] = {"ordered_source_indices": ordered, "source_exact": exact / total if total else 1.0, "source_parts": total, "source_exact_parts": exact}
    first = next((rank for rank, item in enumerate(ordered, 1) if item in relevant), None)
    result["mrr"] = 1 / first if first else 0.0
    for k in (1, 5, 10):
        prefix = ordered[:k]
        hits = sum(index in relevant for index in prefix)
        result[f"recall@{k}"] = hits / len(relevant) if relevant else 0.0
        result[f"precision@{k}"] = hits / k
        result[f"mrr@{k}"] = 1 / first if first and first <= k else 0.0
        dcg = sum((1 if item in relevant else 0) / math.log2(rank + 2) for rank, item in enumerate(prefix))
        ideal = sum(1 / math.log2(rank + 2) for rank in range(min(k, len(relevant))))
        result[f"ndcg@{k}"] = dcg / ideal if ideal else 0.0
        result[f"harmful@{k}"] = sum(index in harmful for index in prefix) / k
        result[f"harmful_exposure@{k}"] = float(bool(set(prefix) & harmful))
    # Fixture indices 5/6 are the attempted action and its explicit rollback.
    attempted = [bundle for bundle in bundles if 5 in bundle]
    result["failure_closure_observations"] = len(attempted)
    result["failure_closed_observations"] = sum(6 in bundle for bundle in attempted)
    result["failure_closure_rate"] = sum(6 in bundle for bundle in attempted) / len(attempted) if attempted else None
    return result


def writer_payloads(case: dict[str, Any], user: str, condition: str) -> list[dict[str, Any]]:
    selected = set(range(len(case["messages"]))) if condition == "noisy" else {0, 1, 2}
    sessions: dict[str, list[dict[str, Any]]] = {}
    for index, message in enumerate(case["messages"]):
        if index in selected:
            sessions.setdefault(message["session_id"], []).append({key: message[key] for key in ("role", "content", "timestamp") if key in message})
    return [{"request_id": f"authored-{sequence}", "user_id": user, "session_id": session, "messages": messages} for sequence, (session, messages) in enumerate(sessions.items())]


def summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {"cases": len(records)}
    numeric = ("mrr", "source_exact", *(f"{metric}@{k}" for metric in ("recall", "precision", "mrr", "ndcg", "harmful", "harmful_exposure") for k in (1, 5, 10)))
    for key in numeric:
        values = [record["retrieval"][key] for record in records if record.get("retrieval") is not None]
        output[key] = statistics.mean(values) if values else None
    closed = sum(record.get("retrieval", {}).get("failure_closed_observations", 0) for record in records if record.get("retrieval") is not None)
    observed = sum(record.get("retrieval", {}).get("failure_closure_observations", 0) for record in records if record.get("retrieval") is not None)
    output["failure_closure_rate"] = closed / observed if observed else None
    output["failure_closure_observations"] = observed
    successes = [record["execution"]["success"] for record in records if record.get("execution") is not None]
    output["fixture_execution_success"] = statistics.mean(successes) if successes else None
    for operation in ("add", "search"):
        values = sorted(record[f"{operation}_seconds"] for record in records if f"{operation}_seconds" in record)
        for quantile in (50, 95, 99):
            output[f"{operation}_p{quantile}_seconds"] = values[min(len(values) - 1, math.ceil(quantile / 100 * len(values)) - 1)] if values else None
    return output


def _write_report(path: Path, output: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    if args.output.exists():
        raise FileExistsError("Use a new output filename; earlier runs are preserved")
    manifest = code_manifest()
    cases = build_dataset(args.split, args.count, args.seed, validate=True)
    case_json = json.dumps(cases, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    output: dict[str, Any] = {
        "status": "running", "dataset": DATASET_VERSION, "split": args.split, "seed": args.seed,
        "cases": len(cases), "cases_sha256": hashlib.sha256(case_json.encode()).hexdigest(),
        "source": "independently authored Python fixtures; no CAMBench/private evaluation data",
        "model_mode": "offline", "paid_model_calls": 0,
        "hybrid_real_embedding_baseline": {"result": None, "reason": "Real text-embedding-v4 calls require credentials and approved cost; not simulated"},
        "code_manifest": manifest, "environment": {"python": sys.version, "executable": sys.executable, "platform": platform.platform(), "cpu_count": os.cpu_count(), "database": "fresh temporary durable filesystem SQLite per strategy/condition; WAL FULL"},
        "protocol": {"metric_unit": "ranked unique original source appearances, flattening bundle source order", "k": [1, 5, 10], "search_top_k": 100, "precision_denominator": "k, even when fewer records returned", "execution_policy": "one first applicable returned diff, one real test execution; never label or test-guided candidate search", "split_limit": "disjoint source paths/groups, shared authored family templates; not natural held-out SWE generalization", "test_transcript_normalization": "temporary roots and reported runtime removed from actual executed unittest reports"},
        "runs": [],
    }
    _write_report(args.output, output)
    try:
        with tempfile.TemporaryDirectory(prefix="forgetrace-evaluation-", dir=str(args.database_root) if args.database_root else None) as temporary:
            variants = ["no_memory", *args.strategies]
            for condition in args.conditions:
                for variant in variants:
                    if variant not in (*VARIANTS, "no_memory"):
                        raise ValueError("unknown evaluation variant")
                    started = time.perf_counter()
                    store = None if variant == "no_memory" else MemoryStore(Path(temporary) / f"{condition}-{variant}.sqlite3", strategy=variant if variant in {"raw", "structured", "proposed"} else "proposed", ablation=variant if variant.startswith("no_") else "none")
                    records = []
                    try:
                        for case in cases:
                            user = "fixture-" + case["case_id"]
                            add_started = time.perf_counter()
                            if store is not None:
                                for payload in writer_payloads(case, user, condition):
                                    store.add(payload)
                            add_seconds = time.perf_counter() - add_started
                            search_started = time.perf_counter()
                            rows = store.search({"query": case["query"], "user_id": user, "top_k": 100})["data"] if store is not None else []
                            search_seconds = time.perf_counter() - search_started
                            execution = consume(case, rows) if args.execute else None
                            records.append({"case_id": case["case_id"], "group": case["group"], "family": case["family"], "add_seconds": add_seconds, "search_seconds": search_seconds, "retrieval": retrieval_metrics(case, rows) if store is not None else None, "execution": execution, "returned_rows": len(rows)})
                        run = {"condition": condition, "variant": variant, "seconds": time.perf_counter() - started, "summary": summary(records), "records": records, "store_metrics": store.metrics() if store is not None else None}
                        output["runs"].append(run)
                        _write_report(args.output, output)
                        print(json.dumps({"condition": condition, "variant": variant, "cases": len(records), "summary": run["summary"]}), flush=True)
                    finally:
                        if store is not None:
                            store.close()
        after = code_manifest()
        output["source_unchanged"] = manifest == after
        output["status"] = "passed" if output["source_unchanged"] else "invalid_source_changed_during_run"
        _write_report(args.output, output)
    except Exception as error:
        output["status"] = "failed"
        output["error_type"] = type(error).__name__
        _write_report(args.output, output)
        raise
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "eval"), default="dev")
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--count", type=int)
    parser.add_argument("--strategies", nargs="+", choices=VARIANTS, default=["raw", "structured", "proposed"])
    parser.add_argument("--conditions", nargs="+", choices=("relevant", "noisy"), default=["relevant", "noisy"])
    parser.add_argument("--execute", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--database-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.database_root:
        args.database_root.mkdir(parents=True, exist_ok=True)
    evaluate(args)


if __name__ == "__main__":
    main()
