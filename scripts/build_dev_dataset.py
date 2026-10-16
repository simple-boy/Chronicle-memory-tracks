"""Generate our own small software fixtures, never CAMBench tasks or data.

Splits have disjoint authored source groups and parameters, but share generator
families. Results are developer diagnostics, not held-out natural SWE accuracy.
Ground-truth labels and current queries are only evaluation records; the writer
must receive ``messages[].role/content/timestamp`` and session IDs alone.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
from pathlib import Path
import random
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from evaluation.fixture_consumer import run_fixture

DATASET_VERSION = "authored-coding-six-families-v1"
FAMILIES = ("none_guard", "upper_bound", "default_value", "casefold", "ordered_unique", "numeric_sort")


def unified(source: str, replacement: str, target: str) -> str:
    return "".join(difflib.unified_diff(source.splitlines(keepends=True), replacement.splitlines(keepends=True), fromfile="a/" + target, tofile="b/" + target))


def _program(family: str, suffix: str, default: int) -> tuple[str, str, str, str, str, list[str]]:
    name = {
        "none_guard": "resolve_amount", "upper_bound": "clamp_value", "default_value": "read_timeout",
        "casefold": "normalize_key", "ordered_unique": "unique_items", "numeric_sort": "order_rows",
    }[family] + "_" + suffix
    if family == "none_guard":
        broken = f'def {name}(record):\n    return record.get("amount", {default})\n'
        repaired = f'def {name}(record):\n    if record is None:\n        return {default}\n    return record.get("amount", {default})\n'
        older = f'def {name}(record):\n    if record is None:\n        return {default + 1}\n    return record.get("amount", {default})\n'
        request = f"Accept None records by returning the configured amount {default}, and preserve present values."
        checks = [f"self.assertEqual(m.{name}(None), {default})", f"self.assertEqual(m.{name}({{}}), {default})", f"self.assertEqual(m.{name}({{'amount': 0}}), 0)"]
        error = "AttributeError"
    elif family == "upper_bound":
        broken = f'def {name}(value, upper):\n    if value < 0:\n        return 0\n    return value\n'
        repaired = f'def {name}(value, upper):\n    return min(max(value, 0), upper)\n'
        older = f'def {name}(value, upper):\n    return min(max(value, 0), upper - 1)\n'
        request = "Clamp values to the inclusive interval [0, upper], keeping the upper endpoint."
        checks = [f"self.assertEqual(m.{name}(-1, {default}), 0)", f"self.assertEqual(m.{name}({default}, {default}), {default})", f"self.assertEqual(m.{name}({default + 2}, {default}), {default})"]
        error = "AssertionError"
    elif family == "default_value":
        broken = f'def {name}(config):\n    return config.get("timeout")\n'
        repaired = f'def {name}(config):\n    return config.get("timeout", {default})\n'
        older = f'def {name}(config):\n    return config.get("timeout", {default + 1})\n'
        request = f"Use timeout {default} only when the key is missing; keep explicit None and zero unchanged."
        checks = [f"self.assertEqual(m.{name}({{}}), {default})", f"self.assertIsNone(m.{name}({{'timeout': None}}))", f"self.assertEqual(m.{name}({{'timeout': 0}}), 0)"]
        error = "AssertionError"
    elif family == "casefold":
        broken = f'def {name}(value):\n    return value.strip()\n'
        repaired = f'def {name}(value):\n    return value.strip().casefold()\n'
        older = f'def {name}(value):\n    return value.strip().lower()\n'
        request = "Normalize surrounding whitespace and Unicode caseless keys, including German sharp s."
        checks = [f"self.assertEqual(m.{name}(' Straße '), 'strasse')", f"self.assertEqual(m.{name}(' ALPHA '), 'alpha')", f"self.assertEqual(m.{name}(''), '')"]
        error = "AssertionError"
    elif family == "ordered_unique":
        broken = f'def {name}(items):\n    return list(items)\n'
        repaired = f'def {name}(items):\n    return list(dict.fromkeys(items))\n'
        older = f'def {name}(items):\n    return sorted(set(items))\n'
        request = "Remove repeated hashable items while preserving their first appearance order."
        checks = [f"self.assertEqual(m.{name}([3, 1, 3, 2]), [3, 1, 2])", f"self.assertEqual(m.{name}(['z', 'a', 'z']), ['z', 'a'])", f"self.assertEqual(m.{name}([]), [])"]
        error = "AssertionError"
    else:
        broken = f'def {name}(rows):\n    return sorted(rows, key=lambda row: row["rank"])\n'
        repaired = f'def {name}(rows):\n    return sorted(rows, key=lambda row: int(row["rank"]))\n'
        older = f'def {name}(rows):\n    return sorted(rows, key=lambda row: int(row["rank"]), reverse=True)\n'
        request = "Order numeric rank strings ascending and preserve input order for equal ranks."
        checks = [f"self.assertEqual([r['rank'] for r in m.{name}([{{'rank': '10'}}, {{'rank': '2'}}])], ['2', '10'])", f"self.assertEqual([r['id'] for r in m.{name}([{{'rank': '2', 'id': 'b'}}, {{'rank': '2', 'id': 'a'}}])], ['b', 'a'])", f"self.assertEqual(m.{name}([]), [])"]
        error = "AssertionError"
    return name, broken, repaired, older, request, checks + [error]


def _tests(target: str, checks: list[str]) -> str:
    body = "\n".join(f"    def test_behavior_{index}(self):\n        {check}\n" for index, check in enumerate(checks))
    return (
        "import importlib.util\nfrom pathlib import Path\nimport unittest\n\n"
        f"spec = importlib.util.spec_from_file_location('fixture_module', Path({target!r}))\n"
        "m = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(m)\n\n"
        "class BehaviorTests(unittest.TestCase):\n" + body
    )


def build_dataset(split: str = "dev", count: int | None = None, seed: int = 11, validate: bool = True) -> list[dict[str, Any]]:
    if split not in {"dev", "eval"}:
        raise ValueError("split must be dev or eval")
    count = (40 if split == "dev" else 120) if count is None else count
    if count < 1:
        raise ValueError("count must be positive")
    rng = random.Random(seed + (0 if split == "dev" else 100003))
    cases = []
    for index in range(count):
        family = FAMILIES[index % len(FAMILIES)]
        suffix = f"{split}_{seed}_{index:04d}"
        target = f"src/{family}/{suffix}.py"
        value = rng.randint(3, 95)
        name, broken, repaired, older, requirement, checks = _program(family, suffix, value)
        error = checks.pop()
        test_source = _tests(target, checks)
        passed = run_fixture(repaired, test_source, target) if validate else None
        failed = run_fixture(older, test_source, target) if validate else None
        initial = run_fixture(broken, test_source, target) if validate else None
        if validate and (not passed["success"] or failed["success"] or initial["success"]):
            raise RuntimeError(f"authored fixture assumptions failed: {family}/{index}")
        success_trace = (passed["stdout"] + passed["stderr"]).strip() if passed else "Validation was not executed in this dataset export."
        failure_trace = (failed["stdout"] + failed["stderr"]).strip() if failed else "Validation was not executed in this dataset export."
        good_patch = unified(broken, repaired, target)
        other_patch = unified(broken, older, target)
        base = f"Package: LedgerKit==2.4.0\nFile: {target}\nSymbol: {name}\n"
        unrelated = f"src/transport/{suffix}.py"
        entries = [
            ("context", "user", base + f"Task: {requirement}\nSymptom: {error} in behavior tests."),
            ("implementation", "assistant", base + f"Task: {requirement}\nAction: update {name}.\n```diff\n{good_patch}```"),
            ("implementation", "user", base + f"Validation: python -m unittest discover -s tests\n{success_trace}"),
            ("transport", "assistant", f"Package: LedgerKit==2.4.0\nFile: {unrelated}\nSymptom: {error} during transport handling.\nAction: update transport handling.\n```diff\n{unified(broken, older, unrelated)}```"),
            ("legacy", "assistant", f"Package: LedgerKit==1.3.0\nFile: {target}\nSymbol: {name}\nConstraint: applies to LedgerKit==1.3.0\nAction: adapt the earlier behavior contract.\n```diff\n{other_patch}```"),
            ("attempt", "assistant", base + f"Action: try another change to {name}.\n```diff\n{other_patch}```"),
            ("attempt", "user", base + f"Action: rolled back the preceding change; it failed the behavior tests.\nValidation: python -m unittest discover -s tests\n{failure_trace}"),
            ("proposal", "assistant", base + f"Action: propose this change; the tests should pass.\n```diff\n{other_patch}```"),
            ("logging", "user", f"File: src/audit/{suffix}.py\n" + (f"DEBUG transport request entered; {error} retained in transport trace.\n" * 35)),
        ]
        # Source order is observable; condition labels remain outside messages.
        messages = [{"role": role, "content": content, "session_id": f"{suffix}-{session}", "timestamp": 1760000000000 + index * 10000 + message_index} for message_index, (session, role, content) in enumerate(entries)]
        query = f"In LedgerKit==2.4.0 update {target}, function {name}. {requirement} The current test reports {error}. Retrieve prior implementation and validation evidence."
        cases.append({
            "case_id": suffix, "group": f"{split}/authored-source-{seed}-{index:04d}", "family": family,
            "split": split, "query": query, "messages": messages, "relevant_indices": [1, 2],
            "harmful_indices": [4, 7], "caution_indices": [5, 6],
            "program_source": broken, "test_source": test_source, "target_path": target,
            "authored_repaired_source": repaired,
            "label_evidence": {"harmful": "same source context but incompatible package contract or unverified action that fails authored behavior tests", "caution": "explicitly rolled-back attempt and its failing test report", "unrelated": "other-module transport history is not labeled harmful"},
            "construction_validation": {"executed": validate, "initial_success": initial["success"] if initial else None, "intended_patch_success": passed["success"] if passed else None, "alternate_patch_success": failed["success"] if failed else None},
        })
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "eval"), default="dev")
    parser.add_argument("--count", type=int)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = build_dataset(args.split, args.count, args.seed)
    serialized = json.dumps(cases, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload = {"dataset_version": DATASET_VERSION, "source": "independently authored six-family Python fixtures; not CAMBench", "seed": args.seed, "split": args.split, "cases_sha256": hashlib.sha256(serialized.encode()).hexdigest(), "cases": cases}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "cases": len(cases), "sha256": payload["cases_sha256"]}))


if __name__ == "__main__":
    main()
