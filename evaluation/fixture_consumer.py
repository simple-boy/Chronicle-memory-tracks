"""Deterministic patch consumer for locally authored Python fixtures only.

This is not a coding agent, a CAMBench runner, or an untrusted-code sandbox.
It selects one applicable historical diff in returned order, never consults
relevance labels, and never searches candidate patches by test outcomes.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import time
from typing import Any

_FENCED_DIFF = re.compile(r"```(?:diff|patch)[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_HUNK = re.compile(r"^@@ -([0-9]+)(?:,([0-9]+))? \+([0-9]+)(?:,([0-9]+))? @@")
_REPORTED_FAILURE = re.compile(r"\breported_fail\b|\b[1-9][0-9]*[ \t]+failed\b|^FAILED[ \t]*(?:\(|$)|^FAIL(?:ED)?[ \t]*:", re.MULTILINE | re.IGNORECASE)


def safe_target(target_path: str) -> str:
    path = PurePosixPath(target_path)
    if path.is_absolute() or ".." in path.parts or "\\" in target_path or ":" in target_path or not path.parts:
        raise ValueError("unsafe fixture target path")
    return path.as_posix()


def extract_diffs(content: str) -> list[str]:
    matches = [match.group(1) for match in _FENCED_DIFF.finditer(content)]
    if matches:
        return matches
    start = content.find("diff --git ")
    if start < 0:
        match = re.search(r"^--- (?:a/)?[^\n]+\n\+\+\+ ", content, re.MULTILINE)
        start = match.start() if match else -1
    return [content[start:]] if start >= 0 else []


def apply_unified_diff(source: str, patch: str, target_path: str) -> str:
    """Apply exact unified hunks to one permitted file, without fuzzy matching."""
    target_path = safe_target(target_path)
    lines = patch.splitlines(keepends=True)
    header = next((index for index, line in enumerate(lines) if line.startswith("--- ")), None)
    if header is None or header + 1 >= len(lines) or not lines[header + 1].startswith("+++ "):
        raise ValueError("missing unified file headers")
    names = []
    for line in lines[header:header + 2]:
        name = line[4:].strip().split("\t", 1)[0]
        if name.startswith(("a/", "b/")):
            name = name[2:]
        names.append(safe_target(name))
    if names != [target_path, target_path]:
        raise ValueError("patch is for another target")
    original = source.splitlines(keepends=True)
    output: list[str] = []
    cursor = 0
    index = header + 2
    hunks = 0
    while index < len(lines):
        line = lines[index]
        hunk = _HUNK.match(line)
        if not hunk:
            if line.startswith(("diff --git ", "--- ", "+++ ")):
                raise ValueError("multiple files are not supported by this fixture")
            index += 1
            continue
        hunks += 1
        start = int(hunk.group(1)) - 1
        old_count = int(hunk.group(2) or "1")
        new_count = int(hunk.group(4) or "1")
        if old_count == 0:
            start = int(hunk.group(1))
        if start < cursor or start > len(original):
            raise ValueError("invalid hunk position")
        output.extend(original[cursor:start])
        cursor = start
        consumed = produced = 0
        index += 1
        while index < len(lines) and not _HUNK.match(lines[index]):
            item = lines[index]
            if item.startswith("\\ No newline at end of file"):
                raise ValueError("unterminated source lines are not used by these fixtures")
            if not item or item[0] not in " +-":
                break
            value = item[1:]
            if item[0] in " -":
                if cursor >= len(original) or original[cursor] != value:
                    raise ValueError("patch context does not match")
                cursor += 1
                consumed += 1
            if item[0] in " +":
                output.append(value)
                produced += 1
            index += 1
        if (consumed, produced) != (old_count, new_count):
            raise ValueError("incomplete hunk")
    if not hunks:
        raise ValueError("no unified hunks")
    output.extend(original[cursor:])
    return "".join(output)


def run_fixture(program_source: str, test_source: str, target_path: str, timeout: float = 5.0) -> dict[str, Any]:
    """Execute our generated fixture with a minimal environment and a deadline."""
    target_path = safe_target(target_path)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="forgetrace-fixture-") as directory:
        root = Path(directory)
        target = root / target_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(program_source, encoding="utf-8", newline="\n")
        tests = root / "tests"
        tests.mkdir()
        (tests / "test_case.py").write_text(test_source, encoding="utf-8", newline="\n")
        environment = {"PATH": os.defpath, "HOME": directory, "TMP": directory, "TEMP": directory, "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"}
        for variable in ("SYSTEMROOT", "WINDIR"):
            if variable in os.environ:
                environment[variable] = os.environ[variable]
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-m", "unittest", "discover", "-s", "tests", "-p", "test_case.py"],
                cwd=directory, env=environment, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout, check=False,
            )
            def normalized(output: str) -> str:
                output = output.replace(directory, "<fixture-root>")
                return re.sub(r"\bRan ([0-9]+ tests?) in [0-9.]+s\b", r"Ran \1", output)
            return {"success": result.returncode == 0, "exit_code": result.returncode, "timed_out": False, "seconds": time.perf_counter() - started, "stdout": normalized(result.stdout), "stderr": normalized(result.stderr), "output_normalization": "temporary_root_and_reported_runtime_removed"}
        except subprocess.TimeoutExpired:
            return {"success": False, "exit_code": None, "timed_out": True, "seconds": time.perf_counter() - started, "stdout": "", "stderr": "fixture execution timeout"}


def consume(case: dict[str, Any], rows: list[dict[str, Any]], timeout: float = 5.0) -> dict[str, Any]:
    """Choose first applicable, non-failure-marked patch, then run once."""
    selected = None
    program = case["program_source"]
    skipped = 0
    for row in rows:
        content = row.get("content", "")
        if not isinstance(content, str):
            continue
        if _REPORTED_FAILURE.search(content):
            skipped += 1
            continue
        for patch in extract_diffs(content):
            try:
                candidate = apply_unified_diff(program, patch, case["target_path"])
            except ValueError:
                continue
            program = candidate
            selected = row.get("id")
            break
        if selected is not None:
            break
    execution = run_fixture(program, case["test_source"], case["target_path"], timeout)
    return {**execution, "selected_id": selected, "patched": selected is not None, "skipped_failure_bundles": skipped, "policy": "first_applicable_diff_once_no_label_access"}
