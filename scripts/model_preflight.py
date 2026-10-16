"""Explicitly authorized, capped real-model connectivity/source-grounding test.

Reads only this project's private .env. Never reads another project's credentials
or formal evaluation records. Missing credentials means NOT RUN, not success.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from forgetrace.models import ModelClient
from forgetrace.storage import MemoryStore
from forgetrace.validation import ContractError


def private_env(path):
    values={}
    if not path.exists(): return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#"): continue
        key,sep,value=line.partition("=")
        if sep:
            key=key.strip()
            if key in values: raise ValueError("Duplicate private environment setting")
            values[key]=value.strip().strip("\"'")
    return values


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file",type=Path,default=ROOT/".env")
    parser.add_argument("--allow-model-calls",action="store_true")
    parser.add_argument("--budget-usd",type=float,default=.1)
    parser.add_argument("--max-attempts",type=int,default=6)
    parser.add_argument("--output",type=Path,default=ROOT/"results/model-preflight.json")
    args=parser.parse_args()
    report={"status":"not_run","real_model_calls":0,"official_smoke":"not_run","official_full":"not_run",
            "kind":"synthetic_real_model_preflight_not_cambench_result","authorized_usd_limit":args.budget_usd}
    client=None
    try:
        if not args.allow_model_calls: raise ValueError("Explicit --allow-model-calls is required")
        if not 0<args.budget_usd<=.1 or not 1<=args.max_attempts<=12:
            raise ValueError("This preflight cannot exceed the authorized USD 0.10 / 12 attempt cap")
        env=private_env(args.env_file)
        if not env.get("OPENAI_API_KEY"): raise ValueError("Configure OPENAI_API_KEY in the new private .env; do not paste it into chat")
        base=env.get("OPENAI_BASE_URL","https://api.openai.com/v1")
        if urlsplit(base).hostname!="api.openai.com":
            raise ValueError("Third-party price/model identity not verified; this OpenAI-price budget test will not call that endpoint")
        embedding=bool(env.get("EMBEDDING_API_KEY") and env.get("EMBEDDING_BASE_URL"))
        if embedding:
            host=urlsplit(env["EMBEDDING_BASE_URL"]).hostname or ""
            if not (host.endswith(".aliyuncs.com") or host.endswith(".alibabacloud.com")):
                raise ValueError("Embedding provider price is not verified for this capped preflight")
        client=ModelClient("gpt-4o-mini",env["OPENAI_API_KEY"],base,timeout=60,retries=0,
            embedding_mode="v4" if embedding else "none",embedding_api_key=env.get("EMBEDDING_API_KEY",""),
            embedding_base_url=env.get("EMBEDDING_BASE_URL",""),max_total_attempts=args.max_attempts,cost_limit_usd=args.budget_usd)
        messages=[{"role":"user","content":"Task: src/auth.py parse_user raises TypeError for None.\nAction: guard None before accessing user.id.\nConstraint: Python==3.11.\nValidation: pytest tests/test_auth.py -q\n2 passed in 0.1s"}]
        with tempfile.TemporaryDirectory(prefix="forge-model-preflight-") as folder:
            store=MemoryStore(Path(folder)/"preflight.sqlite3",model_client=client)
            try:
                payload={"request_id":"synthetic-model-preflight","user_id":"self-created-model-check","session_id":"s1","messages":messages}
                store.add(payload)
                attempts=client.stats()["model_http_attempts"]
                store.add(payload)
                if client.stats()["model_http_attempts"]!=attempts: raise ValueError("Idempotent retry made an extra model call")
                result=store.search({"query":"src/auth.py parse_user None","user_id":payload["user_id"],"top_k":1})
                if not result["data"] or "guard None" not in result["data"][0]["content"]:
                    raise ValueError("Real-model Add was not immediately source-retrievable")
                report.update(status="passed",idempotent_model_retry="passed",immediate_source_evidence="passed",
                    embedding_preflight="passed" if embedding else "not_run_missing_separate_v4_credentials",
                    remote_identity="provider_response_only_not_independent_weight_verification")
            finally: store.close()
    except (ValueError,ContractError) as error:
        report["reason"]=str(error)
        if client is not None and client.stats()["model_http_attempts"]: report["status"]="failed"
    except Exception as error:
        report.update(status="failed",reason="preflight_transport_or_storage_error",error_type=type(error).__name__)
    if client is not None:
        report["usage"]=client.stats()
        report["real_model_calls"]=client.stats()["model_http_attempts"]
        report["reserved_upper_bound_usd"]=client._reserved_cost
        report["billing_note"]="Conservative bytes-as-token bound at cited official rates, not an invoice; includes failed attempts."
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report,indent=2))
    return 0 if report["status"]=="passed" else 1


if __name__=="__main__": raise SystemExit(main())
