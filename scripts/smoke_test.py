#!/usr/bin/env python3
"""Synthetic Coding API contract test. Does not run official AML Smoke or Full."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def request(base_url, path, *, key="", method="GET", payload=None, auth="token", timeout=120):
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("invalid_base_url")
    headers = {"Accept": "application/json"}
    if key:
        if auth == "x-api-key":
            headers["X-Api-Key"] = key
        else:
            headers["Authorization"] = ("Bearer " if auth == "bearer" else "Token ") + key
    data = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base_url.rstrip("/") + path, data=data, headers=headers, method=method)
    started = time.perf_counter()
    # Never forward a Memory System Key to a redirect target, including an
    # accidental cross-host redirect from a reverse proxy or expired domain.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as response:
            status, raw = response.status, response.read(16 * 1024**2 + 1)
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read(16 * 1024**2 + 1)
    if len(raw) > 16 * 1024**2:
        raise RuntimeError("response_exceeds_local_check_capacity")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise RuntimeError(f"non_json_response_http_{status}") from None
    return status, value, time.perf_counter() - started


def new_state(base_url):
    suffix = uuid.uuid4().hex
    marker = "forge_contract_" + suffix
    evidence = (f"Task {marker}: fix TypeError in src/auth/middleware.py get_user_by_id.\n"
                "Cause: missing user produced None before the id lookup.\n"
                "Action: guard missing users before lookup; keep valid users unchanged.\n"
                "$ pytest tests/test_auth.py -q\n2 passed in 0.12s\n")
    noise = "Unrelated TypeError in src/media/codec.py decode_frame. Attempted reinstall; tests still FAILED."
    failure = "Earlier attempt for missing auth users: cast None to str. pytest tests/test_auth.py -q: 1 failed; reverted this change."
    payload = {"request_id": "selfcheck-request-" + suffix, "user_id": "selfcheck-user-" + suffix,
               "session_id": "selfcheck-session-" + suffix,
               "messages": [{"role": "user", "timestamp": 1704067200000, "content": evidence},
                            {"role": "assistant", "timestamp": 1704067201000, "content": noise},
                            {"role": "user", "timestamp": 1704067202000, "content": failure}]}
    return {"base_url": base_url.rstrip("/"), "marker": marker, "payload": payload}


def _source_evidence(content: str, sources: list[str]) -> bool:
    if not isinstance(content, str) or not content.strip():
        return False
    if any(content in source for source in sources):
        return True
    # Bundles can contain only deterministic provenance headers and exact source
    # slices; do not accept generated prose between the quoted source sections.
    headers = list(re.finditer(r"(?m)^\[Source id=[^\n\]]+; session=[^\n\]]+; role=(?:user|assistant); chars=(\d+):(\d+)\]\n", content))
    if not headers or headers[0].start() != 0:
        return False
    for index, header in enumerate(headers):
        stop = headers[index + 1].start() if index + 1 < len(headers) else len(content)
        quote = content[header.end():stop]
        if index + 1 < len(headers):
            if not quote.endswith("\n\n"):
                return False
            quote = quote[:-2]
        start, end = map(int, header.groups())
        if start >= end or not any(end <= len(source) and source[start:end] == quote for source in sources):
            return False
    return True


def run_checks(base_url, key, *, state=None, phase="all", auth="token", timeout=120,
               allow_model_calls=False):
    state = state or new_state(base_url)
    if state.get("base_url") != base_url.rstrip("/"):
        raise RuntimeError("state_base_url_mismatch")
    payload = state["payload"]
    status, health, elapsed = request(base_url, "/health", timeout=timeout)
    if status != 200 or health.get("service") != "forgetrace" or health.get("track") != "coding":
        raise RuntimeError(f"health_failed_http_{status}")
    potentially_paid = (phase in {"all", "write"} and health.get("model_mode") != "offline") or health.get("embedding_mode", "none") != "none"
    if potentially_paid and not allow_model_calls:
        raise RuntimeError("model_calls_require_explicit_allow_after_cost_estimate")
    summary = {"health": "passed", "health_seconds": round(elapsed, 6),
               "server_version": health.get("version"), "server_commit": health.get("commit"),
               "model_mode": health.get("model_mode"), "diagnostic_only": health.get("diagnostic_only"),
               "official_smoke": "not_run", "official_full": "not_run"}
    if key:
        status, _, _ = request(base_url, "/search", method="POST", payload={
            "query": "synthetic authorization check", "user_id": payload["user_id"], "top_k": 1}, timeout=timeout)
        if status != 401:
            raise RuntimeError("unauthenticated_request_not_rejected")
        summary["authentication"] = "passed"
    else:
        summary["authentication"] = "not_checked_no_key"
    if phase in {"all", "write"}:
        status, ack, elapsed = request(base_url, "/add", key=key, auth=auth, method="POST", payload=payload, timeout=timeout)
        expected = {"success": True, **{name: payload[name] for name in ("request_id", "user_id", "session_id")}}
        if status != 200 or ack != expected:
            raise RuntimeError(f"add_contract_failed_http_{status}")
        summary["add"], summary["add_seconds"] = "passed", round(elapsed, 6)
        status, retry_ack, _ = request(base_url, "/add", key=key, auth=auth, method="POST", payload=payload, timeout=timeout)
        if status != 200 or retry_ack != expected:
            raise RuntimeError("idempotent_retry_failed")
        summary["idempotency"] = "passed"
        conflict = {**payload, "messages": [{"role": "user", "content": "Different synthetic engineering evidence."}]}
        status, _, _ = request(base_url, "/add", key=key, auth=auth, method="POST", payload=conflict, timeout=timeout)
        if status != 409:
            raise RuntimeError("changed_payload_idempotency_conflict_not_rejected")
        summary["changed_payload_conflict"] = "passed"
    if phase in {"all", "read", "write"}:
        search = {"user_id": payload["user_id"], "query": state["marker"], "top_k": 1,
                  "options": ["src/auth/middleware.py", "src/media/codec.py"]}
        status, response, elapsed = request(base_url, "/search", key=key, auth=auth, method="POST", payload=search, timeout=timeout)
        rows = response.get("data")
        if status != 200 or not isinstance(rows, list) or len(rows) != 1:
            raise RuntimeError(f"immediate_search_failed_http_{status}")
        evidence = rows[0]
        if not isinstance(evidence.get("id"), str) or not evidence["id"]:
            raise RuntimeError("stable_memory_id_missing")
        if state.get("memory_id") and evidence["id"] != state["memory_id"]:
            raise RuntimeError("memory_id_changed_since_previous_phase")
        state["memory_id"] = evidence["id"]
        sources = [message["content"] for message in payload["messages"]]
        if not _source_evidence(evidence.get("content"), sources) or state["marker"] not in evidence["content"]:
            raise RuntimeError("evidence_not_from_original_input")
        status, repeat, _ = request(base_url, "/search", key=key, auth=auth, method="POST", payload=search, timeout=timeout)
        if status != 200 or repeat.get("data", [{}])[0].get("id") != evidence["id"]:
            raise RuntimeError("memory_id_unstable")
        status, isolated, _ = request(base_url, "/search", key=key, auth=auth, method="POST", payload={
            **search, "user_id": "absent-selfcheck-" + uuid.uuid4().hex}, timeout=timeout)
        if status != 200 or isolated != {"data": []}:
            raise RuntimeError("user_isolation_failed")
        summary.update({"search": "passed", "search_seconds": round(elapsed, 6),
                        "source_faithfulness": "passed", "stable_id": "passed", "user_isolation": "passed"})
    return summary, state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--phase", choices=("all", "write", "read"), default="all")
    parser.add_argument("--state", type=Path, default=Path("state-selfcheck.json"))
    parser.add_argument("--auth", choices=("token", "bearer", "x-api-key"), default="token")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--allow-model-calls", action="store_true", help="Explicitly allow potentially paid Add/Search model requests after reviewing a cost estimate")
    args = parser.parse_args()
    try:
        saved = json.loads(args.state.read_text(encoding="utf-8")) if args.state.exists() else None
        if args.phase == "read" and saved is None:
            raise RuntimeError("read_phase_requires_existing_state_file")
        result, state = run_checks(args.base_url, os.getenv("MEMORY_API_KEY", ""), state=saved,
                                  phase=args.phase, auth=args.auth, timeout=args.timeout,
                                  allow_model_calls=args.allow_model_calls)
        if args.phase != "read":
            args.state.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if os.name != "nt":
                os.chmod(args.state, 0o600)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "reason": str(exc) if isinstance(exc, RuntimeError) else "selfcheck_transport_or_configuration_error"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
