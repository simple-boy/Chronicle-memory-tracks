"""Independent local distractor-count diagnostic, not CAMBench noise tiers.

The frozen core and six-family fixture generator are reused unchanged. Extra
records are authored task/patch messages from other source groups. They are
unrelated, not automatically labeled harmful. Existing incompatible actions
retain their original, execution-supported harmful labels.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from evaluation.fixture_consumer import consume
from scripts.build_dev_dataset import build_dataset
from scripts.evaluate import code_manifest, retrieval_metrics, summary, writer_payloads, _write_report
from forgetrace.storage import MemoryStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", nargs="+", type=int, default=[11, 23, 37])
    parser.add_argument("--count", type=int, default=6)
    parser.add_argument("--levels", nargs="+", type=int, default=[0, 12, 48, 192])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("earlier results are preserved; choose a new output filename")
    if args.count < 1 or any(level < 0 for level in args.levels):
        raise ValueError("positive case count and nonnegative source levels required")
    manifest = code_manifest()
    own_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    output = {"status": "running", "source": "independently authored six-family fixtures", "code_manifest": manifest, "noise_sweep_sha256": own_hash, "seeds": args.seeds, "cases_per_seed": args.count, "levels_additional_unrelated_sources": args.levels, "base_sources": 9, "model_mode": "offline", "paid_model_calls": 0, "official_noise_level_mapping": None, "protocol": "Shared first authored eval groups. Each level adds only other-group task/patch text, no labels/queries/test programs. Fresh durable SQLite per variant/level/seed; top_k=100. One first-applicable-diff execution. Extra unrelated sources are not harmful labels.", "runs": []}
    _write_report(args.output, output)
    try:
        with tempfile.TemporaryDirectory(prefix="forgetrace-noise-sweep-") as temporary:
            for seed in args.seeds:
                cases = build_dataset("eval", args.count, seed, validate=True)
                distractors = build_dataset("dev", max(args.levels) or 1, seed + 9000, validate=False)
                for level in args.levels:
                    for variant in ("raw", "structured", "proposed"):
                        store = MemoryStore(Path(temporary) / f"{seed}-{level}-{variant}.sqlite3", strategy=variant)
                        if store.health()["model_mode"] != "offline":
                            raise RuntimeError("offline-only diagnostic")
                        records = []
                        started = time.perf_counter()
                        try:
                            for case in cases:
                                user = "sweep-" + case["case_id"]
                                extras = [{key: item["messages"][1][key] for key in ("role", "content", "timestamp")} for item in distractors[:level]]
                                augmented = {**case, "messages": [*case["messages"], *extras]}
                                add_started = time.perf_counter()
                                for payload in writer_payloads(case, user, "noisy"):
                                    store.add(payload)
                                if extras:
                                    store.add({"request_id": "other-historical-tasks", "user_id": user, "session_id": "other-authored-groups", "messages": extras})
                                add_seconds = time.perf_counter() - add_started
                                search_started = time.perf_counter()
                                response = store.search({"query": case["query"], "user_id": user, "top_k": 100})
                                search_seconds = time.perf_counter() - search_started
                                rows = response["data"]
                                metrics = retrieval_metrics(augmented, rows)
                                ordered = metrics["ordered_source_indices"]
                                records.append({"case_id": case["case_id"], "family": case["family"], "add_seconds": add_seconds, "search_seconds": search_seconds, "retrieval": metrics, "execution": consume(case, rows), "returned_rows": len(rows), "returned_unique_sources": len(ordered), "response_json_bytes": len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()), "harmful_sources_in_top100_response": len(set(ordered) & set(case["harmful_indices"])), "harmful_exposure_in_top100_response": bool(set(ordered) & set(case["harmful_indices"]))})
                            run = {"seed": seed, "additional_sources": level, "total_available_sources": 9 + level, "variant": variant, "seconds": time.perf_counter() - started, "summary": summary(records), "records": records}
                            run["summary"].update({key: statistics.mean(record[key] for record in records) for key in ("returned_rows", "returned_unique_sources", "response_json_bytes", "harmful_sources_in_top100_response", "harmful_exposure_in_top100_response")})
                            output["runs"].append(run)
                            _write_report(args.output, output)
                            print(json.dumps({"seed": seed, "additional_sources": level, "variant": variant, "summary": run["summary"]}), flush=True)
                        finally:
                            store.close()
        output["source_unchanged"] = manifest == code_manifest() and own_hash == hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        output["status"] = "passed" if output["source_unchanged"] else "invalid_source_changed_during_run"
        _write_report(args.output, output)
    except Exception as error:
        output["status"] = "failed"
        output["error_type"] = type(error).__name__
        _write_report(args.output, output)
        raise


if __name__ == "__main__":
    main()
