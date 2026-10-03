"""Synthetic manual API check. Not official AML Smoke. Never prints credentials."""
import argparse
import base64
import io
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from PIL import Image


def call(base, path, data=None, auth=True):
    headers = {"Content-Type": "application/json"}
    key = os.getenv("MEMORY_API_KEY", "")
    if auth and key:
        headers["Authorization"] = "Token " + key
    req = urllib.request.Request(base.rstrip("/") + path,
          data=None if data is None else json.dumps(data).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as result:
            return result.status, json.load(result)
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, json.load(exc)


def image(color):
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buffer, "PNG")
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--track", required=True, choices=["coding", "multimodal"])
    parser.add_argument("--phase", choices=["write", "read"], default="write")
    parser.add_argument("--state", default=None)
    parser.add_argument("--check-auth", action="store_true")
    args = parser.parse_args()
    state_path = Path(args.state or f"state-{args.track}.json")
    status, health = call(args.base_url, "/health", auth=False)
    assert status == 200 and health.get("track") == args.track, "Health/track mismatch"
    if args.phase == "write":
        scope = "synthetic-check-" + uuid.uuid4().hex
        if args.track == "coding":
            sources = ["src/config.py: parseConfig raises ConfigError on an empty path. "
                       "Validate the path before parseConfig; regression test: 4 passed.",
                       "Heartbeat: src/network.py fetchSettings progress 10%; connection poll."]
            query = "Find the historical ConfigError fix in src/config.py parseConfig"
        else:
            sources = [[{"type": "text", "text": "A red sample swatch stored in session one."}, image("red")],
                       [{"type": "text", "text": "A blue sample swatch stored in session two."}, image("blue")]]
            query = [{"type": "text", "text": "Find the memory matching this sample."}, image("red")]
        state = {"track": args.track, "user_id": scope, "query": query, "ids": [],
                 "expected_source": sources[0]}
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        for number, content in enumerate(sources):
            payload = {"request_id": scope + f"-{number}", "user_id": scope,
                       "session_id": f"s{number}", "messages": [{"role": "user", "content": content}]}
            status, response = call(args.base_url, "/add", payload)
            assert status == 200 and response == {"success": True, **{k: payload[k] for k in ("request_id", "user_id", "session_id")}}, f"Add failed, HTTP {status}"
            status, _ = call(args.base_url, "/add", payload)
            assert status == 200, "Identical Add retry must succeed"
        changed = dict(payload, messages=[{"role":"user","content":sources[0]}])
        status, _ = call(args.base_url, "/add", changed)
        assert status == 409, "Changed Add retry must conflict"
    else:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["track"] == args.track, "State track mismatch"
    request = {"query": state["query"], "user_id": state["user_id"], "top_k": 100}
    status, response = call(args.base_url, "/search", request)
    assert status == 200 and isinstance(response.get("data"), list) and response["data"], f"Search failed/empty, HTTP {status}"
    ids = [row["id"] for row in response["data"]]
    assert len(ids) <= 100 and len(ids) == len(set(ids)), "Invalid candidate IDs/count"
    content = response["data"][0]["content"]
    expected = state["expected_source"]
    assert (content.endswith(expected) if isinstance(expected, str) else content == expected), "Top-1 source mismatch"
    foreign = dict(request, user_id=state["user_id"] + "-unwritten")
    status, isolated = call(args.base_url, "/search", foreign)
    assert status == 200 and isolated == {"data":[]}, "Cross-user evidence leaked"
    if args.phase == "read":
        assert ids == state["ids"], "Restart changed the source IDs or ranking"
    else:
        state["ids"] = ids
        state_path.write_text(json.dumps(state), encoding="utf-8")
    if args.check_auth:
        assert os.getenv("MEMORY_API_KEY"), "Set MEMORY_API_KEY before checking authentication"
        status, _ = call(args.base_url, "/search", request, auth=False)
        assert status == 401, "Unauthenticated Search should return 401"
    print(json.dumps({"manual_synthetic_check": "pass", "phase": args.phase,
                      "track": args.track, "candidate_count": len(ids)}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, ValueError) as exc:
        print(f"Check failed: {exc}", file=sys.stderr)
        sys.exit(1)
