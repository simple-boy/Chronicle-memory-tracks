#!/usr/bin/env python3
"""Offline large-request diagnostic against the retained synthetic test Docker.

Requires a passed container_check report and verifies its exact unique container,
image, volume, loopback binding and offline mode before writing synthetic logs.
Never targets a cloud endpoint, existing evaluation service, or production data.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid

from container_check import binding_url, docker, ready, runtime_hash
from smoke_test import request

ROOT = Path(__file__).resolve().parents[1]


def inspect(name):
    return json.loads(docker("inspect", name))[0]


def cgroup(name):
    code = """import json
from pathlib import Path
p=Path('/sys/fs/cgroup')
result={}
for key in ('memory.current','memory.peak','memory.max'):
 try:
  value=(p/key).read_text().strip()
  result[key]=int(value) if value.isdigit() else value
 except OSError: result[key]=None
for key in ('memory.events','memory.stat'):
 try: result[key]={a:int(b) for a,b in [line.split() for line in (p/key).read_text().splitlines()]}
 except OSError: result[key]={}
print(json.dumps(result))"""
    return json.loads(docker("exec", "--user", "10001:10001", name, "python", "-c", code))


def _memory_bytes(text):
    matched = re.fullmatch(r"\s*([0-9.]+)\s*([A-Za-z]+)\s*", text)
    if not matched:
        raise ValueError("unsupported_docker_memory_format")
    number, unit = matched.groups()
    scale = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3,
             "kB": 1000, "MB": 1000**2, "GB": 1000**3}
    return int(float(number) * scale[unit])


class Monitor:
    def __init__(self, name):
        self.rows = []
        self.parse_errors = 0
        self.process = subprocess.Popen(["docker", "stats", "--format", "{{json .}}", name],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        for line in self.process.stdout:
            try:
                clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).strip()
                if not clean:
                    continue
                row = json.loads(clean)
                self.rows.append({"monotonic": time.monotonic(),
                                  "docker_memory_usage_bytes": _memory_bytes(row["MemUsage"].split("/")[0]),
                                  "cpu_percent": float(row["CPUPerc"].rstrip("%")),
                                  "pids": int(row["PIDs"])})
            except (ValueError, KeyError, TypeError):
                self.parse_errors += 1
                continue

    def stop(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill(); self.process.wait(timeout=5)
        self.thread.join(timeout=5)
        return {"sample_count": len(self.rows),
                "sample_parse_errors": self.parse_errors,
                "sampled_peak_docker_memory_bytes": max((row["docker_memory_usage_bytes"] for row in self.rows), default=None),
                "sampled_peak_cpu_percent": max((row["cpu_percent"] for row in self.rows), default=None),
                "sampled_peak_pids": max((row["pids"] for row in self.rows), default=None),
                "samples": self.rows}


def log_text(size, marker):
    head = "Task: inspect get_user_by_id logging in src/auth/middleware.py.\n"
    line = "[debug] src/auth/middleware.py get_user_by_id user_id=None TypeError trace; candidate guard, validation pending.\n"
    tail = "\n" + marker + " exact terminal trace marker; no result asserted.\n"
    middle_size = size - len(head) - len(tail)
    if middle_size < 0:
        raise ValueError("request_too_small")
    value = head + (line * (middle_size // len(line) + 1))[:middle_size] + tail
    if len(value.encode("ascii")) != size:
        raise AssertionError("wrong_synthetic_source_size")
    return value


def one_request(base, key, size, label):
    marker = "large_probe_" + uuid.uuid4().hex
    source = log_text(size, marker)
    payload = {"request_id": marker, "user_id": marker, "session_id": "synthetic-large-log", "messages": [
        {"role": "user", "content": source}]}
    wire_size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
    started = time.perf_counter()
    status, ack, add_seconds = request(base, "/add", key=key, method="POST", payload=payload, timeout=600)
    passed = status == 200 and ack == {"success": True, **{name: payload[name] for name in ("request_id", "user_id", "session_id")}}
    result = {"label": label, "source_bytes": size, "http_request_bytes": wire_size,
              "add_http_status": status, "add_seconds": add_seconds, "ack_valid": passed}
    if passed:
        status, response, elapsed = request(base, "/search", key=key, method="POST", payload={
            "user_id": marker, "query": marker, "top_k": 1}, timeout=120)
        result.update(search_http_status=status, search_seconds=elapsed,
                      exact_tail_immediately_retrieved=status == 200 and bool(response.get("data")) and marker in response["data"][0].get("content", ""))
    result["total_seconds"] = time.perf_counter() - started
    result["passed"] = passed and result.get("exact_tail_immediately_retrieved", False)
    return result


def measured_case(name, base, key, size, count):
    before = cgroup(name)
    monitor = Monitor(name)
    started = time.perf_counter()
    try:
        if count == 1:
            rows = [one_request(base, key, size, str(size) + "-bytes")]
        else:
            with ThreadPoolExecutor(max_workers=count) as pool:
                rows = list(pool.map(lambda index: one_request(base, key, size, str(size) + "-bytes-concurrent-" + str(index)), range(count)))
    finally:
        observed = monitor.stop()
    after = cgroup(name)
    state = inspect(name)
    return {"requests": rows, "concurrency": count, "wall_seconds": time.perf_counter() - started,
            "docker_stats": observed, "cgroup_before": before, "cgroup_after": after,
            "oom_kill_delta": after.get("memory.events", {}).get("oom_kill", 0) - before.get("memory.events", {}).get("oom_kill", 0),
            "container_oom_killed": state["State"]["OOMKilled"], "restart_count": state["RestartCount"],
            "passed": all(row["passed"] for row in rows) and not state["State"]["OOMKilled"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container-report", type=Path, default=ROOT / "results/docker-final-20261016.json")
    parser.add_argument("--output", type=Path, default=ROOT / "results/large-request-20261016.json")
    args = parser.parse_args()
    report = {"status": "failed", "synthetic_only": True, "real_model_calls": 0,
              "official_smoke": "not_run", "official_full": "not_run", "cases": [],
              "limitations": ["Small local engineering-log diagnostic, not a cloud SLA or CAMBench result.",
                              "Docker stats is sampled; cgroup memory.peak is cumulative for this service lifetime and includes file cache.",
                              "No 64MiB request tested or claimed safe. No natural multi-language code corpus tested here."]}
    started = False
    name = None
    try:
        previous = json.loads(args.container_report.read_text(encoding="utf-8"))
        name = previous["container"]
        if previous.get("status") != "passed" or not name.startswith("forgetrace-local-verification-"):
            raise RuntimeError("passed_owned_container_report_required")
        state = inspect(name)
        if state["Image"] != previous["image_id"] or state["HostConfig"]["Memory"] != 1024**3 or state["HostConfig"]["NanoCpus"] != 1500000000:
            raise RuntimeError("owned_image_or_resource_configuration_changed")
        if not any(mount.get("Name") == previous["volume"] and mount["Destination"] == "/app/data" for mount in state["Mounts"]):
            raise RuntimeError("owned_volume_mismatch")
        env = dict(item.split("=", 1) for item in state["Config"]["Env"] if "=" in item)
        key = env.get("MEMORY_API_KEY", "")
        if env.get("MODEL_MODE") != "offline" or env.get("EMBEDDING_MODE") != "none" or not key:
            raise RuntimeError("offline_owned_test_configuration_required")
        if state["State"]["Running"]:
            raise RuntimeError("expected_owned_stopped_test_container")
        docker("start", name)
        started = True
        base = binding_url(name)
        health = ready(base)
        if health.get("model_mode") != "offline" or health.get("source_sha256") != runtime_hash():
            raise RuntimeError("offline_runtime_source_mismatch")
        report.update(container=name, volume=previous["volume"], image_id=previous["image_id"],
                      source_sha256=health["source_sha256"], memory_limit_bytes=1024**3, cpu_limit=1.5)
        for size in (1024**2, 8 * 1024**2):
            result = measured_case(name, base, key, size, 1)
            report["cases"].append(result)
            if not result["passed"] or result["oom_kill_delta"]:
                raise RuntimeError("sequential_large_request_failed")
        single_large = report["cases"][-1]
        peak = single_large["cgroup_after"].get("memory.peak")
        docker_peak = single_large["docker_stats"]["sampled_peak_docker_memory_bytes"]
        if isinstance(peak, int) and peak < 512 * 1024**2 and (docker_peak is None or docker_peak < 512 * 1024**2):
            report["concurrency_decision"] = "single_8MiB_cgroup_peak_below_half_memory_limit"
            result = measured_case(name, base, key, 8 * 1024**2, 2)
            report["cases"].append(result)
            if not result["passed"] or result["oom_kill_delta"]:
                raise RuntimeError("concurrent_large_request_failed")
        else:
            report["concurrency_decision"] = "skipped_2x8MiB_due_to_single_request_memory_headroom"
        status, metrics, _ = request(base, "/metrics", key=key)
        attempts = metrics.get("storage", {}).get("model_http_attempts")
        if status != 200 or attempts != 0:
            raise RuntimeError("offline_model_attempts_not_zero")
        report["model_http_attempts"] = attempts
        report["final_cgroup"] = cgroup(name)
        report["status"] = "passed"
    except Exception as exc:
        report["failure"] = {"error_type": type(exc).__name__, "reason": str(exc) if isinstance(exc, RuntimeError) else "large_request_probe_failed"}
    finally:
        if started:
            try:
                docker("stop", "-t", "30", name, timeout=60)
                report["retained_container_state"] = "stopped"
            except Exception:
                report["retained_container_state"] = "check_failed"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": report["status"], "output": str(args.output),
                          "completed_cases": len(report["cases"]), "failure": report.get("failure")}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
