#!/usr/bin/env python3
"""Build and verify a new isolated Docker service using synthetic code only.

The test service is loopback-only, 1 GiB/1.5 CPU, offline, and has its own new
volume. Existing containers, volumes, .env files and cloud services are untouched.
The stopped main test container and its named volume are retained for audit.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid

from smoke_test import request, run_checks

ROOT = Path(__file__).resolve().parents[1]


def runtime_hash():
    value = hashlib.sha256()
    for path in sorted((ROOT / "src" / "forgetrace").glob("*.py")):
        value.update(path.name.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return value.hexdigest()


def source_hashes():
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((ROOT / "src").rglob("*.py"))}


def docker(*args, timeout=300):
    completed = subprocess.run(["docker", *args], check=False, capture_output=True, text=True, timeout=timeout)
    if completed.returncode:
        raise RuntimeError("docker_command_failed_" + args[0])
    return completed.stdout.strip()


def binding_url(name):
    bindings = json.loads(docker("inspect", name, "--format", '{{json (index .NetworkSettings.Ports "8000/tcp")}}'))
    if not bindings or bindings[0]["HostIp"] != "127.0.0.1":
        raise RuntimeError("test_container_not_loopback_bound")
    return "http://127.0.0.1:" + bindings[0]["HostPort"]


def ready(base_url, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, health, _ = request(base_url, "/health", timeout=3)
            if status == 200:
                return health
        except Exception:
            pass
        time.sleep(.5)
    raise RuntimeError("isolated_container_health_timeout")


def capacity_check(base_url, key, workers=2, count=8):
    suffix = uuid.uuid4().hex
    user = "synthetic-capacity-" + suffix
    def write(index):
        marker = "capcheck_" + suffix + "_" + str(index)
        payload = {"request_id": marker, "user_id": user, "session_id": "s1", "messages": [
            {"role": "user", "content": marker + " src/auth.py getUserById guard missing user before lookup."}]}
        status, response, elapsed = request(base_url, "/add", key=key, method="POST", payload=payload)
        return {"status": status, "ack_valid": response.get("success") is True, "seconds": elapsed}
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(write, range(count)))
    errors = sum(row["status"] != 200 or not row["ack_valid"] for row in outcomes)
    # Every successful request must already be retrievable, not merely queued.
    for index, outcome in enumerate(outcomes):
        if outcome["status"] != 200:
            continue
        marker = "capcheck_" + suffix + "_" + str(index)
        status, result, _ = request(base_url, "/search", key=key, method="POST", payload={
            "query": marker, "user_id": user, "top_k": 1})
        if status != 200 or not result.get("data") or marker not in result["data"][0].get("content", ""):
            errors += 1
    return {"workers": workers, "requests": count, "errors": errors,
            "elapsed_seconds": round(time.perf_counter() - started, 6), "outcomes": outcomes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="forgetrace:local-test")
    parser.add_argument("--base-image", default="", help="Overrides Dockerfile BASE_IMAGE")
    parser.add_argument("--package-index", default="", help="Public HTTPS PyPI mirror without credentials/query parameters")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "container_check.json")
    args = parser.parse_args()
    if args.package_index:
        parsed = urllib.parse.urlsplit(args.package_index)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            parser.error("package index must be HTTPS without credentials or query parameters")
    suffix = uuid.uuid4().hex[:12]
    name, volume = "forgetrace-local-verification-" + suffix, "forgetrace-local-verification-" + suffix
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {"status": "failed", "cloud_deployment": "not_run", "official_smoke": "not_run",
              "official_full": "not_run", "real_model_calls": 0, "synthetic_only": True,
              "container_memory_bytes": 1024**3, "container_cpus": 1.5,
              "container": name, "volume": volume, "image_tag": args.image,
              "base_image_override": args.base_image or None, "package_index_override": args.package_index or None,
              "source_sha256": source_hashes()}
    created = False
    try:
        report["docker_version"] = docker("version", "--format", "{{.Server.Version}}")
        if not args.skip_build:
            log_path = ROOT / "artifacts" / "private" / (args.output.stem + ".build.log")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("w", encoding="utf-8") as log:
                build = ["docker", "build", "-t", args.image]
                if args.base_image:
                    build.extend(["--build-arg", "BASE_IMAGE=" + args.base_image])
                if args.package_index:
                    build.extend(["--build-arg", "FORGE_PIP_INDEX=" + args.package_index])
                completed = subprocess.run([*build, str(ROOT)], stdout=log, stderr=subprocess.STDOUT, timeout=600, check=False)
            if completed.returncode:
                raise RuntimeError("docker_build_failed_see_synthetic_build_log")
        report["image_id"] = docker("image", "inspect", args.image, "--format", "{{.Id}}")
        docker("volume", "create", volume)
        with tempfile.TemporaryDirectory(prefix="forgetrace-docker-check-") as temporary:
            env_path = Path(temporary) / "private-test.env"
            key = secrets.token_urlsafe(32)
            env_path.write_text("MEMORY_API_KEY=" + key + "\nPRODUCTION=1\nMIN_FREE_BYTES=0\n"
                                "MAX_ADD_CONCURRENCY=2\nMAX_SEARCH_CONCURRENCY=2\nSTRATEGY=proposed\n"
                                "MODEL_MODE=offline\nEMBEDDING_MODE=none\n", encoding="utf-8")
            os.chmod(env_path, 0o600)
            docker("run", "-d", "--name", name, "--env-file", str(env_path),
                   "-p", "127.0.0.1::8000", "--mount", "type=volume,source=" + volume + ",target=/app/data",
                   "--memory", "1024m", "--cpus", "1.5", "--pids-limit", "128", "--read-only",
                   "--tmpfs", "/tmp:size=33554432,mode=1777", "--cap-drop", "ALL", "--cap-add", "CHOWN",
                   "--cap-add", "SETUID", "--cap-add", "SETGID", "--security-opt", "no-new-privileges:true", args.image)
            created = True
            base_url = binding_url(name)
            report["health"] = ready(base_url)
            report["runtime_source_matches"] = report["health"].get("source_sha256") == runtime_hash()
            if not report["runtime_source_matches"]:
                raise RuntimeError("deployed_runtime_source_mismatch")
            uid_code = "from pathlib import Path; import json; rows=[]\nfor p in Path('/proc').iterdir():\n if p.name.isdigit():\n  try:\n   parts=(p/'cmdline').read_bytes().split(b'\\0')\n   if parts and parts[0].rsplit(b'/',1)[-1].startswith(b'python') and b'uvicorn' in parts:\n    rows.append(int(next(x for x in (p/'status').read_text().splitlines() if x.startswith('Uid:')).split()[1]))\n  except (OSError,StopIteration,ValueError): pass\nprint(json.dumps(rows))"
            report["application_uids"] = json.loads(docker("exec", name, "python", "-c", uid_code))
            if not report["application_uids"] or any(uid == 0 for uid in report["application_uids"]):
                raise RuntimeError("application_running_as_root")
            if report["health"].get("synchronous") != 2 or report["health"].get("journal_mode") != "wal":
                raise RuntimeError("sqlite_not_wal_full")
            report["write_and_immediate_read"], state = run_checks(base_url, key)
            report["capacity"] = capacity_check(base_url, key)
            if report["capacity"]["errors"]:
                raise RuntimeError("synthetic_container_capacity_failed")
            status, metrics, _ = request(base_url, "/metrics", key=key)
            if status != 200:
                raise RuntimeError("authenticated_metrics_unavailable")
            report["metrics_before_restart"] = metrics
            model_attempts = metrics.get("storage", {}).get("model_http_attempts")
            if not isinstance(model_attempts, int):
                raise RuntimeError("offline_model_attempt_metric_missing")
            if model_attempts:
                raise RuntimeError("offline_test_attempted_remote_model_calls")
            docker("restart", "-t", "30", name, timeout=60)
            base_url = binding_url(name)
            ready(base_url)
            state = {**state, "base_url": base_url}
            report["read_after_restart"], _ = run_checks(base_url, key, state=state, phase="read")
            docker("kill", "--signal", "KILL", name, timeout=30)
            docker("start", name)
            base_url = binding_url(name)
            ready(base_url)
            state = {**state, "base_url": base_url}
            report["read_after_sigkill"], _ = run_checks(base_url, key, state=state, phase="read")
            # Explicitly test the declared production default: it must fail before
            # attempting any network call when its required model key is absent.
            fail_env = Path(temporary) / "no-model-key.env"
            fail_env.write_text("MEMORY_API_KEY=" + key + "\nMODEL_MODE=gpt-4o-mini\nPRODUCTION=1\n", encoding="utf-8")
            os.chmod(fail_env, 0o600)
            fail_closed = subprocess.run(["docker", "run", "--rm", "--network", "none", "--env-file", str(fail_env),
                                          "--entrypoint", "python", args.image, "-c",
                                          "from forgetrace.api import create_app; create_app()"],
                                         capture_output=True, text=True, timeout=30, check=False)
            report["missing_model_key_fails_closed"] = (fail_closed.returncode != 0 and "requires OPENAI_API_KEY" in fail_closed.stderr)
            if not report["missing_model_key_fails_closed"]:
                raise RuntimeError("production_model_key_not_required")
            compose = subprocess.run(["docker", "compose", "--env-file", str(env_path), "-f", str(ROOT / "compose.yaml"),
                                      "config", "--no-env-resolution", "--quiet"],
                                     check=False, capture_output=True, text=True, timeout=30)
            report["compose_config"] = "passed" if compose.returncode == 0 else "failed"
            if compose.returncode:
                raise RuntimeError("compose_syntax_validation_failed")
            report["source_snapshot_unchanged"] = source_hashes() == report["source_sha256"]
            if not report["source_snapshot_unchanged"]:
                raise RuntimeError("source_changed_during_container_verification_rebuild_required")
            report["status"] = "passed"
    except Exception as exc:
        report["failure"] = {"error_type": type(exc).__name__,
                             "reason": str(exc) if isinstance(exc, RuntimeError) else "isolated_container_check_failed"}
    finally:
        if created:
            try:
                log_path = ROOT / "artifacts" / "private" / (args.output.stem + ".container.log")
                log_path.parent.mkdir(parents=True, exist_ok=True)
                logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True, timeout=30)
                log_path.write_text(logs.stdout + logs.stderr, encoding="utf-8")
                docker("stop", "-t", "30", name, timeout=60)
                report["retained_container_state"] = "stopped"
            except Exception:
                report["retained_container_state"] = "check_failed"
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
