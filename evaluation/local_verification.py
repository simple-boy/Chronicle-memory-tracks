"""Exercise the shipped manual CLI, shut down, reopen SQLite, then read only."""
import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from app import create_server


def run():
    checks = []
    with tempfile.TemporaryDirectory() as tmp:
        for track in ("coding", "multimodal"):
            db = str(Path(tmp) / (track + ".sqlite3"))
            state = str(Path(tmp) / (track + ".json"))
            for phase in ("write", "read"):
                server = create_server(port=0, track=track, db_path=db, api_key="local-test-only")
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                try:
                    cmd = [sys.executable, "-B", str(PROJECT / "scripts/smoke.py"),
                           "--base-url", f"http://127.0.0.1:{server.server_port}",
                           "--track", track, "--phase", phase, "--state", state, "--check-auth"]
                    env = dict(os.environ, MEMORY_API_KEY="local-test-only")
                    result = subprocess.run(cmd, cwd=PROJECT, env=env, capture_output=True,
                                            text=True, timeout=30)
                    if result.returncode:
                        raise RuntimeError(f"{track}/{phase} manual CLI failed: {result.stderr}")
                    checks.append(json.loads(result.stdout))
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()
                    server.store.close()
    return {"kind":"manual_synthetic_cli_and_db_reopen", "official_smoke":False,
            "utc":datetime.now(timezone.utc).isoformat(), "python":sys.version,
            "platform":platform.platform(), "model_calls":0, "checks":checks,
            "limitations":"Same-host temporary SQLite; not Render restart, container build, HTTPS or official evaluation."}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    report = run()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
