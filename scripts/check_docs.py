#!/usr/bin/env python3
"""Check portable Markdown links, fences, figures and Python syntax locally.

Does not execute documentation commands, import the service, contact any network,
read .env, or run models. Bytecode is written only in a temporary directory.
"""
from __future__ import annotations

import argparse
import compileall
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import re
import struct
import sys
import tempfile
from urllib.parse import unquote, urlsplit
import zlib

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {".git", "artifacts", "data", "dist", "models", "__pycache__"}
REQUIRED_DOCS = ("README.md", "METHOD.md", "BENCHMARK_ANALYSIS.md", "EXPERIMENTS.md",
                 "DEPLOYMENT_GUIDE.md", "SUBMISSION_GUIDE.md", "REPRODUCIBILITY.md", "ATTRIBUTION.md")
REQUIRED_FIGURES = ("figures/method_overview.png", "figures/deployment_architecture.png")


def document_paths(root):
    return [path for path in sorted(root.rglob("*.md"))
            if not any(part in EXCLUDED or part.startswith(".venv") for part in path.relative_to(root).parts)]


def fenced_text(text):
    """Return prose without code bodies plus an unclosed-fence line, if any."""
    active = None
    prose = []
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})([^\n]*)", line)
        if active is None and marker:
            active = (marker[1][0], len(marker[1]), number)
            prose.append("\n")
        elif active is not None:
            if marker and marker[1][0] == active[0] and len(marker[1]) >= active[1] and not marker[2].strip():
                active = None
            prose.append("\n")
        else:
            prose.append(line)
    return "".join(prose), active[2] if active else None


def link_targets(prose):
    # Inline code is not a hyperlink. References defined with [name]: are also
    # checked, even when the corresponding label is used more than once.
    prose = re.sub(r"`[^`\n]*`", "", prose)
    inline = re.finditer(r"!?\[[^\]\n]*\]\((<[^>\n]+>|[^\n]*?)\)", prose)
    reference = re.finditer(r"(?m)^ {0,3}\[[^\]\n]+\]:\s*(<[^>\n]+>|\S+)", prose)
    for match in [*inline, *reference]:
        raw = match[1].strip()
        if raw.startswith("<"):
            target = raw[1:raw.find(">")]
        else:
            # Optional Markdown title is separated from a bare path by space.
            target = re.split(r"\s+[\"']", raw, maxsplit=1)[0].strip()
        yield 1 + prose[:match.start()].count("\n"), target


def check_links(path, root):
    text = path.read_text(encoding="utf-8")
    prose, open_line = fenced_text(text)
    errors = []
    if open_line:
        errors.append({"file": path.relative_to(root).as_posix(), "line": open_line, "reason": "unclosed_code_fence"})
    checked = 0
    for line, target in link_targets(prose):
        parsed = urlsplit(target)
        if not target or target.startswith("#") or target.startswith("//"):
            continue
        if parsed.scheme:
            if len(parsed.scheme) == 1 or parsed.scheme == "file":
                errors.append({"file": path.relative_to(root).as_posix(), "line": line, "reason": "nonportable_absolute_file_link", "target": target})
            continue
        location = unquote(parsed.path).replace("\\ ", " ")
        if not location:
            continue
        checked += 1
        resolved = (path.parent / location).resolve()
        if location.startswith("/") or not resolved.is_relative_to(root):
            errors.append({"file": path.relative_to(root).as_posix(), "line": line, "reason": "file_link_outside_project", "target": target})
        elif not resolved.exists():
            errors.append({"file": path.relative_to(root).as_posix(), "line": line, "reason": "missing_relative_link_target", "target": target})
    return checked, errors


def png_info(path):
    raw = path.read_bytes()
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("invalid_png_signature")
    offset, dimensions, ended = 8, None, False
    while offset < len(raw):
        if offset + 12 > len(raw):
            raise ValueError("truncated_png_chunk")
        size = struct.unpack(">I", raw[offset:offset + 4])[0]
        kind = raw[offset + 4:offset + 8]
        stop = offset + 12 + size
        if stop > len(raw):
            raise ValueError("truncated_png_data")
        data = raw[offset + 8:offset + 8 + size]
        expected = struct.unpack(">I", raw[offset + 8 + size:stop])[0]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != expected:
            raise ValueError("png_crc_mismatch")
        if kind == b"IHDR":
            if offset != 8 or size != 13:
                raise ValueError("invalid_png_header")
            dimensions = struct.unpack(">II", data[:8])
        if kind == b"IEND":
            if size or stop != len(raw):
                raise ValueError("invalid_png_end")
            ended = True
            break
        offset = stop
    if not ended or not dimensions or min(dimensions) < 1:
        raise ValueError("incomplete_png")
    return {"width": dimensions[0], "height": dimensions[1], "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()}


def check(root=ROOT):
    root = root.resolve()
    errors = []
    documents = document_paths(root)
    links = 0
    for path in documents:
        count, problems = check_links(path, root)
        links += count
        errors.extend(problems)
    for relative in REQUIRED_DOCS:
        if not (root / relative).is_file():
            errors.append({"file": relative, "reason": "required_document_missing"})
    figures = {}
    for relative in REQUIRED_FIGURES:
        try:
            figures[relative] = png_info(root / relative)
        except (OSError, ValueError, struct.error) as exc:
            errors.append({"file": relative, "reason": str(exc) if isinstance(exc, ValueError) else "figure_missing_or_unreadable"})
    output = io.StringIO()
    previous = sys.pycache_prefix
    python_files = [*sorted((root / "src").rglob("*.py")), *sorted((root / "scripts").rglob("*.py"))]
    with tempfile.TemporaryDirectory(prefix="forgetrace-syntax-") as temporary:
        try:
            sys.pycache_prefix = temporary
            with redirect_stdout(output), redirect_stderr(output):
                syntax_ok = all(compileall.compile_file(str(path), quiet=2, force=True) for path in python_files)
        finally:
            sys.pycache_prefix = previous
    if not syntax_ok:
        errors.append({"reason": "python_syntax_failed", "diagnostics": output.getvalue()})
    return {"status": "passed" if not errors else "failed", "markdown_files": len(documents),
            "relative_links_checked": links, "python_files_compiled": len(python_files),
            "python_syntax": "passed" if syntax_ok else "failed", "figures": figures,
            "errors": errors, "network_calls": 0, "model_calls": 0,
            "limitations": "Checks local file targets and balanced fences; does not verify external URLs or render Markdown."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=ROOT / "results" / "docs-quality.json")
    args = parser.parse_args()
    result = check(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
