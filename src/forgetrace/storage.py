"""Atomic durable source memory with user-local lexical statistics and trace links."""
from __future__ import annotations
import array
from collections import Counter
from contextlib import contextmanager
from datetime import datetime,timezone
import hashlib
import heapq
import json
import math
import re
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import zlib

from .models import ModelClient
from .parsing import PARSER_VERSION,parse_message,query_features
from .retrieval import (applicability,chunk_ranges,excerpt_ranges,frequencies,
                        rank_candidates,scope_keys,path_key,term_key,terms_query)
from .validation import ContractError,canonical,validate_add,validate_search

SCHEMA="""
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipts(user_id TEXT,request_id TEXT,payload_hash TEXT,response TEXT,
 PRIMARY KEY(user_id,request_id));
CREATE TABLE IF NOT EXISTS scopes(user_id TEXT PRIMARY KEY,sequence INTEGER NOT NULL,n INTEGER NOT NULL,dl INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,session_id TEXT NOT NULL,
 request_id TEXT NOT NULL,sequence INTEGER NOT NULL,message_index INTEGER NOT NULL,role TEXT NOT NULL,
 timestamp INTEGER,created_at TEXT NOT NULL,content BLOB NOT NULL,envelope TEXT NOT NULL,features TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS source_scope_sequence ON sources(user_id,session_id,sequence);
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),
 user_id TEXT NOT NULL,ordinal INTEGER NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,dl INTEGER NOT NULL,tf BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS chunk_user ON chunks(user_id);
CREATE TABLE IF NOT EXISTS term_stats(user_id TEXT NOT NULL,term TEXT NOT NULL,df INTEGER NOT NULL,
 PRIMARY KEY(user_id,term));
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(terms,content='',detail=none);
CREATE TABLE IF NOT EXISTS artifacts(user_id TEXT NOT NULL,kind TEXT NOT NULL,value TEXT NOT NULL,
 source_id TEXT NOT NULL REFERENCES sources(id),PRIMARY KEY(user_id,kind,value,source_id));
CREATE TABLE IF NOT EXISTS links(user_id TEXT NOT NULL,action_id TEXT NOT NULL REFERENCES sources(id),
 evidence_id TEXT NOT NULL REFERENCES sources(id),kind TEXT NOT NULL,basis TEXT NOT NULL,
 PRIMARY KEY(user_id,action_id,evidence_id,kind));
CREATE INDEX IF NOT EXISTS links_action ON links(user_id,action_id);
CREATE TABLE IF NOT EXISTS vectors(chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id),user_id TEXT NOT NULL,value BLOB NOT NULL);
"""


class MemoryStore:
    def __init__(self,db_path,strategy="proposed",*,response_bytes=16*1024**2,max_results=100,
                 min_free_bytes=0,model_client=None,ablation="none"):
        if strategy not in {"raw","structured","proposed","hybrid"}: raise ValueError("Invalid strategy")
        if ablation not in {"none","no_structure","no_reliability","no_applicability","no_closure"}: raise ValueError("Invalid ablation")
        self.db_path=str(db_path)
        if self.db_path==":memory:": raise ValueError("Use a durable database path")
        Path(self.db_path).parent.mkdir(parents=True,exist_ok=True)
        self.strategy,self.ablation=strategy,ablation
        self.response_bytes,self.max_results,self.min_free_bytes=response_bytes,max_results,min_free_bytes
        self.model=model_client or ModelClient()
        if strategy=="hybrid" and self.model.embedding_mode=="none":
            raise ValueError("Hybrid baseline requires real text-embedding-v4")
        self._local=threading.local();self._write_lock=threading.RLock()
        self._request_locks=[threading.RLock() for _ in range(64)]
        self._stats_lock=threading.Lock()
        self._stats={"add_requests":0,"deduplicated_requests":0,"messages":0,"chunks":0,
                     "source_bytes":0,"add_seconds":0.,"parse_seconds":0.,"commit_seconds":0.,
                     "write_lock_wait_seconds":0.,"search_requests":0,"search_seconds":0.,
                     "closure_bundles":0,"closure_budget_skips":0}
        self.configuration={"format":1,"parser":PARSER_VERSION,"strategy":strategy,"ablation":ablation,
            "chunk_chars":4096,"excerpt_chars":16000,"closure_max_sources":8,"link_window_sources":4,
            "llm_quote_index":"additional_source_span_chunks_v1",
            "raw_representation":"source_and_lexical_only_v1",
            "outcome_link_policy":"nearest_same_session4_path_symbol_report_v3",
            "response_bytes":response_bytes,"max_results":max_results,
            "dense_scope_limit_chunks":50000,
            "model":self.model.config_dict(),"ranking":"scope-lexicographic-v1"}
        self.configuration_hash=hashlib.sha256(canonical(self.configuration).encode()).hexdigest()
        # Retain the factory-thread connection. Do not close the last connection per HTTP request.
        conn=self._connection();conn.execute("PRAGMA journal_mode=WAL");conn.executescript(SCHEMA)
        previous=conn.execute("SELECT value FROM meta WHERE key='configuration'").fetchone()
        if previous and previous[0]!=canonical(self.configuration):
            self.close();raise ValueError("Database is bound to a different frozen method/configuration; keep its data and use its matching version")
        conn.execute("INSERT OR IGNORE INTO meta VALUES('configuration',?)",(canonical(self.configuration),))

    def _connection(self):
        if not hasattr(self._local,"connection"):
            connection=sqlite3.connect(self.db_path,timeout=30,isolation_level=None)
            connection.row_factory=sqlite3.Row
            connection.execute("PRAGMA synchronous=FULL");connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            self._local.connection=connection
        return self._local.connection

    def close(self):
        connection=getattr(self._local,"connection",None)
        if connection is not None: connection.close();del self._local.connection

    def health(self):
        conn=self._connection();conn.execute("SELECT 1")
        return {"status":"ok","service":"forgetrace","strategy":self.strategy,
                "synchronous":conn.execute("PRAGMA synchronous").fetchone()[0],
                "journal_mode":conn.execute("PRAGMA journal_mode").fetchone()[0],
                "configuration_sha256":self.configuration_hash,"model_mode":self.model.mode,
                "embedding_mode":self.model.embedding_mode,"diagnostic_only":self.model.mode=="offline",
                "submission_ready":False}

    def metrics(self):
        with self._stats_lock: output=self._stats.copy()
        output.update(database_bytes=sum(p.stat().st_size for p in (Path(self.db_path),Path(self.db_path+"-wal")) if p.exists()),
                      disk_free_bytes=shutil.disk_usage(Path(self.db_path).parent).free,**self.model.stats())
        return output

    def _insert_source(self,conn,values):
        return conn.execute("INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",values)

    def add(self,body):
        started=time.perf_counter();validate_add(body)
        user,request,session=(body[x] for x in ("user_id","request_id","session_id"))
        payload_hash=hashlib.sha256(canonical(body).encode()).hexdigest()
        lock=self._request_locks[int(hashlib.sha256((user+"\0"+request).encode()).hexdigest()[:8],16)%len(self._request_locks)]
        with lock:
            conn=self._connection()
            old=conn.execute("SELECT payload_hash,response FROM receipts WHERE user_id=? AND request_id=?",(user,request)).fetchone()
            if old:
                if old[0]!=payload_hash: raise ContractError("request_id_reused_with_changed_payload",409)
                with self._stats_lock: self._stats["deduplicated_requests"]+=1
                return json.loads(old[1])
            if shutil.disk_usage(Path(self.db_path).parent).free<self.min_free_bytes:
                raise ContractError("insufficient_storage_capacity",507)
            parsed_at=time.perf_counter();prepared=[]
            for index,message in enumerate(body["messages"]):
                text=message["content"]
                features=(parse_message(text,message["role"]) if self.strategy!="raw" else
                          {"paths":[],"symbols":[],"errors":[],"versions":[],"tests":[],
                           "evidence":[],"outcome":"unverified","outcome_spans":[]})
                features.pop("tokens",None)
                chunks=[]
                for begin,end in chunk_ranges(text):
                    tf=frequencies(text[begin:end])
                    chunks.append((begin,end,tf,sum(tf.values())))
                mid="forge_"+hashlib.sha256(canonical([user,request,index]).encode()).hexdigest()[:32]
                prepared.append({"id":mid,"message":message,"features":features,"chunks":chunks})
            def dense_capacity_guard():
                if self.model.embedding_mode=="none": return
                existing=conn.execute("SELECT n FROM scopes WHERE user_id=?",(user,)).fetchone()
                if (existing[0] if existing else 0)+sum(len(row["chunks"]) for row in prepared)>50000:
                    raise ContractError("dense_scope_exceeds_declared_50000_chunk_capacity",507)
            dense_capacity_guard()  # Reject known overflow before any paid call.
            anchors=self.model.extract(body["messages"])
            for anchor in anchors:
                i=anchor["message_index"]
                if 0<=i<len(prepared) and body["messages"][i]["content"][anchor["start"]:anchor["end"]]==anchor["text"]:
                    prepared[i]["features"]["evidence"].append(anchor)
                    if self.strategy!="raw" and self.ablation!="no_structure":
                        tf=frequencies(anchor["text"])
                        prepared[i]["chunks"].append((anchor["start"],anchor["end"],tf,sum(tf.values())))
            dense_capacity_guard()
            embedding_texts=[row["message"]["content"][begin:end] for row in prepared for begin,end,_,_ in row["chunks"]]
            vectors=self.model.embed(embedding_texts) if self.model.embedding_mode!="none" else []
            parse_seconds=time.perf_counter()-parsed_at
            ack={"success":True,"request_id":request,"user_id":user,"session_id":session}
            wait_at=time.perf_counter()
            with self._write_lock:
                lock_wait=time.perf_counter()-wait_at;commit_at=time.perf_counter()
                conn.execute("BEGIN IMMEDIATE")
                try:
                    # Cross-process retry safety is authoritative at the transaction boundary.
                    old=conn.execute("SELECT payload_hash,response FROM receipts WHERE user_id=? AND request_id=?",(user,request)).fetchone()
                    if old:
                        if old[0]!=payload_hash: raise ContractError("request_id_reused_with_changed_payload",409)
                        conn.execute("COMMIT");return json.loads(old[1])
                    dense_capacity_guard()  # Recheck concurrent writes atomically.
                    conn.execute("INSERT OR IGNORE INTO scopes VALUES(?,0,0,0)",(user,))
                    sequence=conn.execute("SELECT sequence FROM scopes WHERE user_id=?",(user,)).fetchone()[0]
                    now=datetime.now(timezone.utc).isoformat();df=Counter();total_dl=0;chunk_count=0;vector_index=0
                    for index,row in enumerate(prepared):
                        message=row["message"];sequence+=1
                        self._insert_source(conn,(row["id"],user,session,request,sequence,index,message["role"],message.get("timestamp"),now,
                            zlib.compress(message["content"].encode(),1),canonical({k:v for k,v in message.items() if k!="content"}),canonical(row["features"])))
                        for ordinal,(begin,end,tf,dl) in enumerate(row["chunks"]):
                            cid=conn.execute("INSERT INTO chunks(source_id,user_id,ordinal,start,end,dl,tf) VALUES(?,?,?,?,?,?,?)",
                                (row["id"],user,ordinal,begin,end,dl,zlib.compress(canonical(tf).encode(),1))).lastrowid
                            conn.execute("INSERT INTO chunk_fts(rowid,terms) VALUES(?,?)",(cid," ".join(term_key(word) for word in tf)))
                            if vectors:
                                value=array.array("f",vectors[vector_index]);conn.execute("INSERT INTO vectors VALUES(?,?,?)",(cid,user,value.tobytes()))
                            vector_index+=1;df.update(tf.keys());total_dl+=dl;chunk_count+=1
                        fields={"path":row["features"]["paths"],"symbol":row["features"]["symbols"],"error":row["features"]["errors"]}
                        conn.executemany("INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?)",
                            [(user,kind,path_key(value) if kind=="path" else value.casefold(),row["id"]) for kind,values in fields.items() for value in values])
                        self._link_evidence(conn,user,session,sequence,row)
                    conn.executemany("INSERT INTO term_stats VALUES(?,?,?) ON CONFLICT(user_id,term) DO UPDATE SET df=df+excluded.df",
                                     [(user,word,count) for word,count in df.items()])
                    conn.execute("UPDATE scopes SET sequence=?,n=n+?,dl=dl+? WHERE user_id=?",(sequence,chunk_count,total_dl,user))
                    conn.execute("INSERT INTO receipts VALUES(?,?,?,?)",(user,request,payload_hash,canonical(ack)))
                    conn.execute("COMMIT")
                except BaseException:
                    if conn.in_transaction: conn.execute("ROLLBACK")
                    raise
                commit_seconds=time.perf_counter()-commit_at
            with self._stats_lock:
                self._stats["add_requests"]+=1;self._stats["messages"]+=len(prepared);self._stats["chunks"]+=chunk_count
                self._stats["source_bytes"]+=sum(len(row["message"]["content"].encode()) for row in prepared)
                self._stats["add_seconds"]+=time.perf_counter()-started;self._stats["parse_seconds"]+=parse_seconds
                self._stats["write_lock_wait_seconds"]+=lock_wait;self._stats["commit_seconds"]+=commit_seconds
            return ack

    def _link_evidence(self,conn,user,session,sequence,row):
        features=row["features"]
        if features["outcome"]=="unverified": return
        # A self-contained new action+result must not validate a prior task merely
        # because both touched the same file. Link outcome-only follow-ups.
        # Mini-selected quote labels assist retrieval, never govern report scope.
        rule_evidence=[span for span in features["evidence"] if span.get("origin")!="gpt-4o-mini_exact_quote"]
        if re.search(r"\bunrelated\b|\b(?:different|separate)\s+(?:task|issue|incident)\b",row["message"]["content"],re.I): return
        actions=[span for span in rule_evidence if span["kind"] in {"action","diff"}]
        if actions:
            # A failed rollback-only report describes withdrawal of the previous
            # attempt, not a new repair. A new diff or any other action still
            # blocks automatic inheritance of that report by an older action.
            rollback_only=(features["outcome"] in {"reported_fail","conflicting"} and all(
                span["kind"]=="action" and re.match(r"(?:Action\s*:\s*)?(?:rolled\s+back|reverted|rollback|abandoned)\b",span["text"].strip(),re.I)
                for span in actions))
            if not rollback_only: return
        pathset={path_key(x) for x in features["paths"]}
        if not pathset: return
        prior=conn.execute("SELECT id,features FROM sources WHERE user_id=? AND session_id=? AND sequence<? ORDER BY sequence DESC LIMIT 4",
                           (user,session,sequence)).fetchall()
        for old in prior:
            old_features=json.loads(old["features"])
            shared=pathset&{path_key(x) for x in old_features["paths"]}
            if not shared or not any(span["kind"] in {"action","code","diff"} and span.get("origin")!="gpt-4o-mini_exact_quote" for span in old_features["evidence"]): continue
            if features["symbols"] and old_features["symbols"] and not set(features["symbols"])&set(old_features["symbols"]): continue
            if applicability(features,old_features)["version_conflict"]: continue
            kind="counterevidence" if features["outcome"] in {"reported_fail","conflicting"} else "reported_support"
            conn.execute("INSERT OR IGNORE INTO links VALUES(?,?,?,?,?)",(user,old["id"],row["id"],kind,canonical({"shared_paths":sorted(shared),"basis":"nearby_same_session_report_not_causal_proof"})))
            # Link the nearest compatible action only, never an entire session.
            break

    @contextmanager
    def _snapshot(self):
        conn=self._connection();conn.execute("BEGIN")
        try: yield conn
        finally:
            if conn.in_transaction: conn.execute("ROLLBACK")

    def _sparse(self,conn,user,words,limit=512):
        words=[x for x in dict.fromkeys(words) if x not in {""}][:96]
        if not words: return []
        scope=conn.execute("SELECT n,dl FROM scopes WHERE user_id=?",(user,)).fetchone()
        if not scope or scope["n"]==0: return []
        marks=",".join("?" for _ in words)
        dfs={row["term"]:row["df"] for row in conn.execute("SELECT term,df FROM term_stats WHERE user_id=? AND term IN ("+marks+")",(user,*words))}
        if not dfs: return []
        avgdl=scope["dl"]/scope["n"]
        idfs={word:math.log(1+(scope["n"]-df+.5)/(df+.5)) for word,df in dfs.items()}
        heap=[]
        cursor=conn.execute("SELECT c.id,c.source_id,c.start,c.end,c.dl,c.tf FROM chunk_fts JOIN chunks c ON c.id=chunk_fts.rowid WHERE chunk_fts MATCH ? AND c.user_id=?",(terms_query(list(dfs)),user))
        for row in cursor:
            tf=json.loads(zlib.decompress(row["tf"]))
            score=sum(idf*tf.get(word,0)*2.2/(tf.get(word,0)+1.2*(.25+.75*row["dl"]/max(1,avgdl))) for word,idf in idfs.items())
            value=(score,-row["id"],row["id"],row["source_id"],row["start"],row["end"])
            if len(heap)<limit: heapq.heappush(heap,value)
            elif value>heap[0]: heapq.heapreplace(heap,value)
        return sorted(heap,reverse=True)

    def search(self,body):
        started=time.perf_counter();validate_search(body,self.max_results)
        user=body["user_id"];query=query_features(body["query"])
        words=list(frequencies(body["query"]))
        with self._snapshot() as conn:
            lexical=self._sparse(conn,user,words)
            candidates={}
            for score,_,cid,sid,begin,end in lexical:
                if sid not in candidates: candidates[sid]={"chunk_id":cid,"start":begin,"end":end,"base_score":score}
            if self.strategy!="raw" and self.ablation!="no_structure":
                for kind,values in (("path",query["paths"]),("symbol",query["symbols"]),("error",query["errors"])):
                    for value in values[:32]:
                        for row in conn.execute("SELECT source_id FROM artifacts WHERE user_id=? AND kind=? AND value=? LIMIT 256",(user,kind,path_key(value) if kind=="path" else value.casefold())):
                            candidates.setdefault(row[0],{"start":0,"end":4096,"base_score":0.})
            if self.model.embedding_mode!="none":
                count=conn.execute("SELECT count(*) FROM vectors WHERE user_id=?",(user,)).fetchone()[0]
                if count>50000: raise ContractError("dense_scope_exceeds_declared_50000_chunk_capacity",503)
                dense={}
                if count:
                    qvector=self.model.embed([body["query"]])[0]
                    for row in conn.execute("SELECT v.chunk_id,v.value,c.source_id,c.start,c.end FROM vectors v JOIN chunks c ON c.id=v.chunk_id WHERE v.user_id=?",(user,)):
                        vector=array.array("f");vector.frombytes(row["value"])
                        similarity=sum(a*b for a,b in zip(qvector,vector))
                        value=(similarity,row["source_id"],row["start"],row["end"])
                        if row["source_id"] not in dense or value>dense[row["source_id"]]: dense[row["source_id"]]=value
                for rank,(_,sid,begin,end) in enumerate(sorted(dense.values(),reverse=True)[:512]):
                    record=candidates.setdefault(sid,{"start":begin,"end":end,"base_score":0.})
                    record["dense_rrf"]=1/(60+rank)
                sparse_order=sorted(candidates,key=lambda sid:candidates[sid]["base_score"],reverse=True)
                for rank,sid in enumerate(sparse_order):
                    candidates[sid]["base_score"]=(1/(60+rank) if candidates[sid]["base_score"] else 0)+candidates[sid].get("dense_rrf",0)
            if not candidates: return {"data":[]}
            loaded=[]
            for sid,candidate in candidates.items():
                row=conn.execute("SELECT * FROM sources WHERE user_id=? AND id=?",(user,sid)).fetchone()
                if not row: continue
                item=dict(row);item["features"]=json.loads(row["features"]);item.update(candidate)
                item["has_counterevidence"]=bool(conn.execute("SELECT 1 FROM links WHERE user_id=? AND action_id=? AND kind='counterevidence' LIMIT 1",(user,sid)).fetchone())
                loaded.append(item)
            ordered=rank_candidates(loaded,query,self.strategy,self.ablation)
            output=[];seen=set();used=32
            for candidate in ordered:
                if candidate["id"] in seen: continue
                bundle=[candidate]
                closed=self.strategy=="proposed" and self.ablation!="no_closure"
                if closed:
                    dependencies=conn.execute("SELECT s.* FROM links l JOIN sources s ON s.id=l.evidence_id WHERE l.user_id=? AND l.action_id=? ORDER BY s.sequence",(user,candidate["id"])).fetchall()
                    if len(dependencies)>7:
                        with self._stats_lock: self._stats["closure_budget_skips"]+=1
                        continue
                    for row in dependencies:
                        item=dict(row);item["features"]=json.loads(row["features"]);item.update(start=0,end=4096)
                        bundle.append(item)
                parts=[]
                for source in bundle:
                    text=zlib.decompress(source["content"]).decode()
                    ranges=excerpt_ranges(text,source["start"],min(source["end"],len(text)),source["features"])
                    if not ranges:
                        parts=[];break
                    for begin,end in ranges:
                        session_label=hashlib.sha256(source["session_id"].encode()).hexdigest()[:12]
                        parts.append(f"[Source id={source['id']}; session={session_label}; role={source['role']}; chars={begin}:{end}]\n"+text[begin:end])
                if not parts: continue
                content="\n\n".join(parts)
                bid=candidate["id"] if len(bundle)==1 else "bundle_"+hashlib.sha256(canonical([x["id"] for x in bundle]).encode()).hexdigest()[:32]
                created=(datetime.fromtimestamp(candidate["timestamp"]/1000,timezone.utc).isoformat() if candidate["timestamp"] is not None else candidate["created_at"])
                result={"id":bid,"content":content,"score":1/(len(output)+1),"created_at":created}
                cost=len(canonical(result).encode())+1
                if used+cost>self.response_bytes:
                    with self._stats_lock: self._stats["closure_budget_skips"]+=int(closed)
                    continue
                output.append(result);used+=cost;seen.update(x["id"] for x in bundle)
                if closed and len(bundle)>1:
                    with self._stats_lock: self._stats["closure_bundles"]+=1
                if len(output)>=body["top_k"]: break
        with self._stats_lock:
            self._stats["search_requests"]+=1;self._stats["search_seconds"]+=time.perf_counter()-started
        return {"data":output}
