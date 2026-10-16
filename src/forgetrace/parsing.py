"""Source-grounded, model-free indexes for engineering records.

These rules report what a message says, never whether code actually ran. In
particular, ``reported_pass`` is not a verified test outcome. Every evidence
span uses Python character offsets into the exact original string. No text or
vocabulary is silently truncated; service request limits belong at the API.
"""

from __future__ import annotations

import re
from typing import Any

PARSER_VERSION = "engineering-spans-v1"

# Scan maximal lexical candidates once. Requiring a slash inside a nested regex
# would rescan every suffix of a long dotted identifier on a failing match.
_PATH = re.compile(r"(?:[A-Za-z]:)?[./\\\w@+-]+")
_BASENAME = re.compile(
    r"(?<![\w./\\])[-\w]+\.(?:pyi?|js|jsx|ts|tsx|mjs|cjs|java|kt|kts|"
    r"c|h|cc|cpp|hpp|cs|go|rs|rb|php|swift|scala|sh|bash|zsh|ps1|sql|"
    r"html|css|scss|vue|svelte|json|ya?ml|toml|ini|cfg|conf|xml|md|rst|txt|"
    r"lock|gradle|properties|ipynb)(?![\w])",
    re.IGNORECASE,
)
_WORD = re.compile(r"[\w$@]+(?:[./\\:+#-][\w$@+#-]+)*", re.UNICODE)
_URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]{0,31}://[^\s<>'\"`]+")
_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z]|[0-9]|$)|[A-Z]?[a-z]+|[0-9]+")
_DECL = re.compile(
    r"\b(?:async\s+def|def|class|function|fn|func|interface|struct|trait|enum)"
    r"\s+([A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*)"
)
_ERROR = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9_]*(?:Error|Exception|Warning)"
    r"|Error|Exception|SystemExit|KeyboardInterrupt|StopIteration)\b"
)
_VERSION = re.compile(
    r"\b(?P<package>[A-Za-z][A-Za-z0-9_.-]{0,127})[ \t]*"
    r"(?:(?:==|!=|>=|<=|~=|>|<|@)[ \t]*|(?:version[ \t]+)?v?[ \t]+|[ \t]*:[ \t]*)"
    r"v?(?P<version>[0-9]+\.[0-9]+(?:\.[0-9]+){0,3}(?:[-+][A-Za-z0-9_.-]+)?)\b",
    re.IGNORECASE,
)
_TEST_COMMAND = re.compile(
    r"(?<![\w])(?:(?:python(?:3(?:\.\d+)?)?[ \t]+-m[ \t]+)?pytest\b"
    r"|python(?:3(?:\.\d+)?)?[ \t]+-m[ \t]+unittest\b"
    r"|(?:npm|yarn|pnpm)[ \t]+(?:run[ \t]+)?test\b"
    r"|(?:go|cargo|mvn|gradle|dotnet)[ \t]+test\b|ctest\b|tox\b)[^\r\n]*"
)
_COUNT = re.compile(r"\b([0-9]+)[ \t]+(passed|failed|errors?|failures?)\b", re.IGNORECASE)
_TEST_WORD = re.compile(r"\b(?:tests?|pytest|unittest|suites?|validation)\b", re.IGNORECASE)
_MODAL = re.compile(r"\b(?:will|would|should|could|may|might|hopefully|expect(?:ed)?|if)\b", re.IGNORECASE)
_NEG_PASS = re.compile(r"\b(?:not|never|haven['’]t|hasn['’]t|didn['’]t|don['’]t|doesn['’]t)[ \t]+(?:all[ \t]+)?pass(?:ed)?\b", re.IGNORECASE)
_NEG_TEST_PASS = re.compile(r"\b(?:no[ \t]+tests?|not[ \t]+all[ \t]+tests?)[ \t]+(?:have[ \t]+|has[ \t]+)?passed\b", re.IGNORECASE)
_NEG_TEST_FAIL = re.compile(r"\bno[ \t]+tests?[ \t]+(?:have[ \t]+|has[ \t]+)?failed\b", re.IGNORECASE)
_PASS = re.compile(r"\b(?:all[ \t]+)?tests?[ \t]+(?:have[ \t]+|has[ \t]+)?passed\b", re.IGNORECASE)
_FAIL = re.compile(r"\b(?:tests?[ \t]+(?:have[ \t]+|has[ \t]+)?failed|FAILED[ \t]+[^ \t]+::|FAIL[ \t]*:)", re.IGNORECASE)
_CODE_LANGUAGES = frozenset({"python", "py", "javascript", "js", "typescript", "ts", "java", "c", "cpp", "c++", "csharp", "cs", "go", "rust", "rs", "ruby", "rb", "php", "sql", "json", "yaml", "yml"})
_LABEL = re.compile(
    r"^[ \t]*(?:[-*#]+[ \t]*)?(?P<label>root[ \t]+cause|task|problem|goal|"
    r"requirement|symptom|hypothesis|cause|diagnosis|action|fix|patch|changed|"
    r"constraint|requires|environment|version|outcome|validation|test[ \t]+result)"
    r"[ \t]*:[ \t]*\S",
    re.IGNORECASE,
)
_LABEL_KIND = {
    "task": "task", "problem": "task", "goal": "task", "requirement": "task",
    "symptom": "symptom", "hypothesis": "hypothesis", "root cause": "cause",
    "cause": "cause", "diagnosis": "cause", "action": "action", "fix": "action",
    "patch": "action", "changed": "action", "constraint": "constraint",
    "requires": "constraint", "environment": "constraint", "version": "constraint",
    "outcome": "outcome", "validation": "validation", "test result": "validation",
}
_VERSION_NONPACKAGES = frozenset({"version", "versions", "for", "at", "in", "on", "with", "since", "before", "after", "under", "from", "to", "and", "or", "is", "was", "are", "be", "requires", "require", "v"})


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _path_matches(text: str) -> list[tuple[int, int, str]]:
    matches: list[tuple[int, int, str]] = []
    url_spans = [match.span() for match in _URL.finditer(text)]
    for pattern in (_PATH, _BASENAME):
        url_index = 0
        for match in pattern.finditer(text):
            start, end = match.span()
            # Paths in URLs are not evidence of a repository file path.
            while url_index < len(url_spans) and url_spans[url_index][1] <= start:
                url_index += 1
            if url_index < len(url_spans) and url_spans[url_index][0] <= start < url_spans[url_index][1]:
                continue
            if pattern is _PATH and not ("/" in match.group(0) or "\\" in match.group(0)):
                continue
            while end > start and text[end - 1] in ".,-":
                end -= 1
            if end > start:
                matches.append((start, end, text[start:end]))
    return sorted(set(matches))


def tokenize(text: str, expand: bool = True) -> list[str]:
    """Return deterministic, unique, casefolded terms, keeping full identifiers.

    Expansion adds components rather than replacing original terms. Consequently
    ``src/auth.py`` and ``tests/auth.py`` remain distinct exact-match keys.
    Tokens are only an index; raw source text must be stored separately.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    terms: dict[str, None] = {}

    def add(term: str) -> None:
        normalized = term.casefold()
        if normalized:
            terms.setdefault(normalized, None)

    values = [m.group(0) for m in _WORD.finditer(text)]
    values.extend(value for _, _, value in _path_matches(text))
    for value in values:
        add(value)
        if not expand:
            continue
        for component in re.split(r"[./\\:_@+#-]+", value):
            add(component)
            for subword in _CAMEL.findall(component):
                add(subword)
            # CJK character bigrams help mixed-language engineering queries.
            for cjk in re.findall(r"[\u3400-\u9fff]{2,}", component):
                for position in range(len(cjk) - 1):
                    add(cjk[position:position + 2])
    return list(terms)


def _span(content: str, kind: str, start: int, end: int) -> dict[str, Any]:
    return {"kind": kind, "start": start, "end": end, "text": content[start:end]}


def _line_outcome(line: str, in_program_code: bool, previous: str) -> str | None:
    """Recognize reported test results, not execution provenance or correctness."""
    stripped = line.strip()
    if not stripped or in_program_code or stripped.endswith("?"):
        return None
    # Commands which merely print hypothetical summaries are not test evidence.
    if re.match(r"^(?:\$[ \t]*)?(?:echo|printf|print)[ \t(]", stripped):
        return None
    if _MODAL.search(stripped):
        return None
    if _NEG_PASS.search(stripped) or _NEG_TEST_PASS.search(stripped):
        return "reported_fail" if _TEST_WORD.search(stripped) else None
    # String comparison avoids Python's integer digit limit on malicious logs.
    counts = [(bool(m.group(1).strip("0")), m.group(2).casefold()) for m in _COUNT.finditer(stripped)]
    if any(positive and word != "passed" for positive, word in counts):
        return "reported_fail"
    if any(positive and word == "passed" for positive, word in counts):
        return "reported_pass"
    if _FAIL.search(stripped) and not _NEG_TEST_FAIL.search(stripped):
        return "reported_fail"
    if _PASS.search(stripped):
        return "reported_pass"
    if stripped == "OK" and re.search(r"\bRan[ \t]+[0-9]+[ \t]+tests?\b", previous):
        return "reported_pass"
    if re.match(r"^PASS[ \t]+(?:\S+[/\\]|\S+\.test\.)", stripped):
        return "reported_pass"
    if re.match(r"^FAIL[ \t]+(?:\S+[/\\]|\S+\.test\.)", stripped):
        return "reported_fail"
    return None


def parse_message(content: str, role: str = "user") -> dict[str, Any]:
    """Extract only observed engineering features and exact source spans.

    ``role`` never upgrades a textual outcome to verified execution. Missing
    features remain empty; contradictions are mixed reported pass/fail evidence,
    not a claim that two runs had identical environments or assertions.
    """
    if not isinstance(content, str):
        raise TypeError("content must be a string")
    if not isinstance(role, str):
        raise TypeError("role must be a string")
    path_spans = _path_matches(content)
    paths = _unique([value for _, _, value in path_spans])
    symbols: list[str] = []
    symbols.extend(match.group(1) for match in _DECL.finditer(content))
    path_index = 0
    for match in _IDENTIFIER.finditer(content):
        word = match.group(0)
        following = match.end()
        while following < len(content) and content[following] in " \t":
            following += 1
        is_call = following < len(content) and content[following] == "("
        if is_call or "_" in word or "." in word or re.search(r"[a-z][A-Z]", word):
            while path_index < len(path_spans) and path_spans[path_index][1] <= match.start():
                path_index += 1
            in_path = path_index < len(path_spans) and path_spans[path_index][0] <= match.start() < path_spans[path_index][1]
            if not in_path:
                symbols.append(word)
    errors = _unique([m.group(0) for m in _ERROR.finditer(content)])
    versions: list[dict[str, str]] = []
    evidence: list[dict[str, Any]] = []
    for start, end, _ in path_spans:
        evidence.append(_span(content, "path", start, end))
    for match in _VERSION.finditer(content):
        item = {"package": match.group("package").casefold(), "version": match.group("version")}
        if item["package"] in _VERSION_NONPACKAGES:
            continue
        if item not in versions:
            versions.append(item)
        evidence.append(_span(content, "constraint", *match.span()))

    tests: list[str] = []
    outcome_spans: list[dict[str, Any]] = []
    outcomes: set[str] = set()
    fence_start: int | None = None
    fence_char = ""
    fence_length = 0
    fence_language = ""
    previous_nonempty = ""
    offset = 0
    diff_start: int | None = None
    lines = content.splitlines(keepends=True)
    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        end = offset + len(line)
        fence = re.match(r"^[ \t]*(`{3,}|~{3,})([^\r\n]*)$", line)
        is_fence_marker = False
        if fence:
            marker = fence.group(1)
            if fence_start is None:
                fence_start = offset
                fence_char = marker[0]
                fence_length = len(marker)
                fence_language = fence.group(2).strip().split(" ", 1)[0].casefold()
                is_fence_marker = True
            elif marker[0] == fence_char and len(marker) >= fence_length and not fence.group(2).strip():
                evidence.append(_span(content, "diff" if fence_language in {"diff", "patch"} else "code", fence_start, end))
                fence_start = None
                fence_language = ""
                is_fence_marker = True
        if line.startswith("diff --git "):
            if diff_start is not None:
                evidence.append(_span(content, "diff", diff_start, offset))
            diff_start = offset
        elif diff_start is not None and line and not line.startswith((" ", "+", "-", "@", "index ", "new file ", "deleted file ", "old mode ", "new mode ", "\\")):
            evidence.append(_span(content, "diff", diff_start, offset))
            diff_start = None

        if not is_fence_marker:
            label = _LABEL.match(line)
            if label:
                kind = _LABEL_KIND[" ".join(label.group("label").casefold().split())]
                evidence.append(_span(content, kind, offset, end))
            if _ERROR.search(line) or line.startswith("Traceback (most recent call last):"):
                evidence.append(_span(content, "error", offset, end))
            for command in _TEST_COMMAND.finditer(line):
                command_end = offset + command.end()
                while command_end > offset + command.start() and content[command_end - 1].isspace():
                    command_end -= 1
                tests.append(content[offset + command.start():command_end])
                evidence.append(_span(content, "test_command", offset + command.start(), command_end))
            line_outcome = _line_outcome(line, fence_language in _CODE_LANGUAGES, previous_nonempty)
            if line_outcome:
                outcomes.add(line_outcome)
                evidence.append(_span(content, "test_summary", offset, end))
                outcome_spans.append({"start": offset, "end": end, "text": content[offset:end]})
        if line.strip():
            previous_nonempty = line
        offset += len(raw_line)
    if fence_start is not None:
        evidence.append(_span(content, "diff" if fence_language in {"diff", "patch"} else "code", fence_start, len(content)))
    if diff_start is not None:
        evidence.append(_span(content, "diff", diff_start, len(content)))
    # Deduplicate the same rule/source pair without coalescing different kinds.
    evidence = sorted(
        {(item["kind"], item["start"], item["end"]): item for item in evidence}.values(),
        key=lambda item: (item["start"], item["end"], item["kind"]),
    )
    outcome = "conflicting" if len(outcomes) > 1 else next(iter(outcomes), "unverified")
    return {
        "tokens": tokenize(content), "paths": paths, "symbols": _unique(symbols),
        "errors": errors, "versions": versions, "tests": _unique(tests),
        "evidence": evidence, "outcome": outcome, "outcome_spans": outcome_spans,
    }


def query_features(query: str) -> dict[str, Any]:
    """Parse an ephemeral query using the same vocabulary; no memory writes."""
    return parse_message(query, role="user")
