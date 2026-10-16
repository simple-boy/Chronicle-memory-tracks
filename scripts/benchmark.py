"""Measured offline engine throughput. Never calls models or official endpoints."""
from concurrent.futures import ThreadPoolExecutor
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import secrets
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import Request,build_opener,ProxyHandler

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from forgetrace.storage import MemoryStore
import psutil

RUNS=Path.home()/".cache/forgetrace-bench"
RUNS.mkdir(parents=True,exist_ok=True)
HTTP=build_opener(ProxyHandler({})).open


def source_hashes():
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/"src").rglob("*.py")}


def percentile(values,p):
    values=sorted(values)
    if not values: return None
    index=(len(values)-1)*p;left=int(index)
    return values[left]+(values[min(left+1,len(values)-1)]-values[left])*(index-left)


def send(base,path,key,payload=None):
    data=json.dumps(payload).encode() if payload is not None else None
    request=Request(base+path,data=data,headers={"Authorization":"Token "+key,"Content-Type":"application/json"},method="POST" if data else "GET")
    with HTTP(request,timeout=180) as response: return json.loads(response.read())


class Monitor:
    def __init__(self,pid): self.pid=pid;self.stop_event=threading.Event();self.samples=[]
    def __enter__(self):
        self.process=psutil.Process(self.pid);self.begin=self.process.cpu_times()
        self.before_io=self.process.io_counters() if hasattr(self.process,"io_counters") else None
        def sample():
            while not self.stop_event.wait(.02):
                try: self.samples.append(self.process.memory_info().rss)
                except psutil.Error: return
        self.thread=threading.Thread(target=sample,daemon=True);self.thread.start();return self
    def __exit__(self,*_):
        self.stop_event.set();self.thread.join();end=self.process.cpu_times()
        after=self.process.io_counters() if self.before_io else None
        self.result={"rss_peak_sampled_bytes":max(self.samples,default=self.process.memory_info().rss),
                     "cpu_seconds":end.user+end.system-self.begin.user-self.begin.system,
                     "os_write_bytes":after.write_bytes-self.before_io.write_bytes if after else None}


def source_text(size,index):
    line=f"src/runtime/worker.py process_payload_{index%17} handles incoming arguments with None values.\n"
    return (line*(size//len(line)+1))[:size]+f"\nsource_benchmark_marker_{index}\n"


def performance(profile,strategy,requests,sustained):
    definitions={"small_text":(256,1,1),"code_8k":(8192,1,1),"log_256k":(262144,1,1),
                 "batch_100":(2048,100,1),"concurrent_code":(8192,1,2),"sustained":(8192,1,2)}
    size,batch,workers=definitions[profile]
    with tempfile.TemporaryDirectory(prefix="http-",dir=RUNS) as directory:
        with socket.socket() as sock: sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
        base=f"http://127.0.0.1:{port}";key=secrets.token_urlsafe(24)
        env=os.environ.copy();env.update(PYTHONPATH=str(ROOT/"src"),HOST="127.0.0.1",PORT=str(port),
            MEMORY_API_KEY=key,MEMORY_DB_PATH=directory+"/memory.sqlite3",MODEL_MODE="offline",EMBEDDING_MODE="none",
            OPENAI_API_KEY="",EMBEDDING_API_KEY="",PRODUCTION="1",MIN_FREE_BYTES="0",STRATEGY=strategy)
        logs=ROOT/"artifacts/private";logs.mkdir(parents=True,exist_ok=True)
        with (logs/f"{strategy}-{profile}.log").open("w") as log:
            process=subprocess.Popen([sys.executable,"-B","-m","forgetrace.api"],env=env,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
            try:
                for _ in range(100):
                    try: send(base,"/health",key);break
                    except Exception:
                        if process.poll() is not None: raise RuntimeError("Benchmark service failed startup")
                        time.sleep(.1)
                else: raise RuntimeError("Benchmark health timeout")
                payloads=[{"request_id":"perf-"+str(i),"user_id":"perf-user","session_id":"session-"+str(i),
                           "messages":[{"role":"user","content":source_text(size,i*batch+j)} for j in range(batch)]} for i in range(max(2,requests))]
                latencies=[];errors=[];counter=0;lock=threading.Lock();wire=0
                def once(index):
                    nonlocal wire
                    payload=dict(payloads[index%len(payloads)]);payload["request_id"]="perf-"+str(index)
                    started=time.perf_counter()
                    try:
                        result=send(base,"/add",key,payload)
                        if result.get("success") is not True: raise RuntimeError("False acknowledgement")
                        with lock: latencies.append((time.perf_counter()-started)*1000);wire+=len(json.dumps(payload).encode())
                    except Exception as error:
                        with lock: errors.append(type(error).__name__)
                with Monitor(process.pid) as monitor:
                    started=time.perf_counter()
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        if profile!="sustained": list(pool.map(once,range(requests)))
                        else:
                            while time.perf_counter()-started<sustained:
                                list(pool.map(once,range(counter,counter+workers)));counter+=workers
                    elapsed=time.perf_counter()-started
                query_times=[]
                for _ in range(10):
                    begin=time.perf_counter();send(base,"/search",key,{"query":"process_payload_3 src/runtime/worker.py","user_id":"perf-user","top_k":100})
                    query_times.append((time.perf_counter()-begin)*1000)
                metrics=send(base,"/metrics",key)
                return {"profile":profile,"strategy":strategy,"model_mode":"offline","real_model_calls":0,
                        "requests":len(latencies)+len(errors),"successes":len(latencies),"errors":len(errors),
                        "error_rate":len(errors)/max(1,len(latencies)+len(errors)),"request_payload_text_bytes":size*batch,
                        "batch_messages":batch,"concurrency":workers,"seconds":elapsed,"requests_per_second":len(latencies)/elapsed,
                        "messages_per_second":len(latencies)*batch/elapsed,"wire_bytes_per_second":wire/elapsed,
                        "actual_source_bytes":metrics["storage"]["source_bytes"],
                        "source_bytes_per_second":metrics["storage"]["source_bytes"]/elapsed,
                        "add_p50_ms":percentile(latencies,.5),"add_p95_ms":percentile(latencies,.95),"add_p99_ms":percentile(latencies,.99),
                        "search_p50_ms":percentile(query_times,.5),"search_p95_ms":percentile(query_times,.95),"search_p99_ms":percentile(query_times,.99),
                        "resources":monitor.result,"storage_metrics":metrics}
            finally:
                process.terminate()
                try: process.wait(timeout=20)
                except subprocess.TimeoutExpired: process.kill();process.wait()


def scale(count,batch_size):
    with tempfile.TemporaryDirectory(prefix="scale-",dir=RUNS) as directory:
        store=MemoryStore(directory+"/memory.sqlite3")
        latencies=[];source_bytes=0
        with Monitor(os.getpid()) as monitor:
            started=time.perf_counter()
            for begin in range(0,count,batch_size):
                messages=[{"role":"user","content":source_text(1024,i)} for i in range(begin,min(count,begin+batch_size))]
                source_bytes+=sum(len(x["content"].encode()) for x in messages)
                tick=time.perf_counter();store.add({"user_id":"scale-user","session_id":"history","request_id":"batch-"+str(begin),"messages":messages})
                latencies.append((time.perf_counter()-tick)*1000)
                if begin%10000==0: print("Persisted",min(count,begin+batch_size),"of",count,flush=True)
            elapsed=time.perf_counter()-started
        write_metrics=store.metrics()
        store.close();store=MemoryStore(directory+"/memory.sqlite3")
        searches=[]
        for i in range(20):
            tick=time.perf_counter();result=store.search({"query":"source_benchmark_marker_"+str((i*1237)%count),"user_id":"scale-user","top_k":5})
            marker="source_benchmark_marker_"+str((i*1237)%count)
            if not result["data"] or not any(marker in x["content"] for x in result["data"]):
                raise RuntimeError("Missing requested source marker after restart")
            searches.append((time.perf_counter()-tick)*1000)
        actual=store._connection().execute("SELECT count(*) FROM sources").fetchone()[0]
        if actual!=count: raise RuntimeError("Wrong committed source count")
        metrics=store.metrics();store.close()
        return {"sources":count,"persisted_sources":actual,"batch_size":batch_size,"source_bytes":source_bytes,
                "add_seconds":elapsed,"messages_per_second":count/elapsed,"batch_add_p95_ms":percentile(latencies,.95),
                "search_p50_ms":percentile(searches,.5),"search_p95_ms":percentile(searches,.95),"search_p99_ms":percentile(searches,.99),
                "resources":monitor.result,"write_metrics":write_metrics,"storage_metrics":metrics,"model_mode":"offline","real_model_calls":0,
                "limitation":"Direct Store, not HTTP; repeated coding logs, not the official 1.8B-token corpus."}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode",choices=("performance","scale","all"),default="performance")
    parser.add_argument("--requests",type=int,default=12)
    parser.add_argument("--sustained-seconds",type=float,default=20)
    parser.add_argument("--profiles",nargs="+",default=["small_text","code_8k","log_256k","batch_100","concurrent_code","sustained"])
    parser.add_argument("--strategies",nargs="+",default=["raw","structured","proposed"])
    parser.add_argument("--sizes",type=int,nargs="+",default=[1000,10000,100000])
    parser.add_argument("--batch-size",type=int,default=100)
    parser.add_argument("--cores",type=int,default=2)
    parser.add_argument("--output",type=Path,default=ROOT/"results/performance.json")
    args=parser.parse_args()
    if hasattr(os,"sched_getaffinity"):
        allowed=sorted(os.sched_getaffinity(0));os.sched_setaffinity(0,allowed[:args.cores])
    report={"kind":"offline_engine_diagnostic_not_production_model_or_cambench_score","date":"2026-10-16",
            "platform":platform.platform(),"python":sys.version,"real_model_calls":0,"ram_limit_applied":False,
            "cpu_affinity":sorted(os.sched_getaffinity(0)) if hasattr(os,"sched_getaffinity") else None,
            "database_directory":str(RUNS),"performance":[],"scale":[],"source_sha256_at_start":source_hashes()}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    def save(): args.output.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    if args.mode in {"performance","all"}:
        for strategy in args.strategies:
            for profile in args.profiles:
                print("Measuring",strategy,profile,flush=True)
                report["performance"].append(performance(profile,strategy,args.requests,args.sustained_seconds));save()
    if args.mode in {"scale","all"}:
        for size in args.sizes:
            print("Scale",size,flush=True);report["scale"].append(scale(size,args.batch_size));save()
    report["source_sha256_at_end"]=source_hashes()
    report["source_unchanged"]=report["source_sha256_at_start"]==report["source_sha256_at_end"]
    save();print(json.dumps({"report":str(args.output),"profiles":len(report["performance"]),"scales":len(report["scale"])}))


if __name__=="__main__": main()
