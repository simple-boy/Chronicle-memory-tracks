from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
import zlib

from forgetrace.storage import MemoryStore
from forgetrace.models import ModelClient
from forgetrace.validation import ContractError


def body(text,request="r1",user="u",session="s"):
    return {"user_id":user,"request_id":request,"session_id":session,"messages":[{"role":"user","content":text}]}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=str(Path(self.temp.name)/"memory.sqlite3")
        self.store=MemoryStore(self.path)

    def tearDown(self): self.store.close();self.temp.cleanup()

    def query(self,text,user="u",k=100): return self.store.search({"query":text,"user_id":user,"top_k":k})

    def test_immediate_exact_source_and_stable_id_after_reopen(self):
        original="Task: getUserById in src/auth/middleware.py raises TypeError.\nAction: guard None."
        self.store.add(body(original));before=self.query("getUserById")
        self.assertIn(original,before["data"][0]["content"])
        self.store.close();self.store=MemoryStore(self.path)
        self.assertEqual(before,self.query("getUserById"))

    def test_idempotent_retry_and_conflict(self):
        payload=body("Action: fix src/auth.py")
        a=self.store.add(payload);self.assertEqual(self.store.add(payload),a)
        self.assertEqual(self.store._connection().execute("SELECT count(*) FROM sources").fetchone()[0],1)
        payload["messages"][0]["content"]+=" changed"
        with self.assertRaises(ContractError) as raised: self.store.add(payload)
        self.assertEqual(raised.exception.status,409)

    def test_global_other_user_does_not_change_bm25_or_full_response(self):
        self.store.add(body("src/a.py maps parser payload None.","1"))
        self.store.add(body("src/b.py maps payload and parser with longer content context.","2"))
        before=self.query("parser payload")
        for i in range(12): self.store.add(body("parser "*100,"other"+str(i),user="other"))
        self.assertEqual(before,self.query("parser payload"))
        self.assertEqual(self.query("parser",user="absent"),{"data":[]})

    def test_same_request_id_different_users_independent(self):
        self.store.add(body("alpha_parser",user="a"));self.store.add(body("beta_parser",user="b"))
        self.assertNotIn("alpha_parser",str(self.query("parser",user="b")))

    def test_unrelated_query_empty(self):
        self.store.add(body("src/auth.py guard input"))
        self.assertEqual(self.query("completely_unrelated_zzqq"),{"data":[]})

    def test_whole_batch_validation_before_write(self):
        payload=body("first")
        payload["messages"].append({"role":"tool","content":"invalid"})
        with self.assertRaises(ContractError): self.store.add(payload)
        self.assertEqual(self.store._connection().execute("SELECT count(*) FROM sources").fetchone()[0],0)

    def test_transaction_fault_rolls_back_source_fts_statistics_receipts(self):
        original=self.store._insert_source
        calls=[]
        def fault(conn,values):
            calls.append(1)
            if len(calls)==2: raise sqlite3.OperationalError("injected")
            return original(conn,values)
        payload=body("src/a.py")
        payload["messages"].append({"role":"assistant","content":"src/b.py"})
        with mock.patch.object(self.store,"_insert_source",side_effect=fault):
            with self.assertRaises(sqlite3.OperationalError): self.store.add(payload)
        conn=self.store._connection()
        for table in ("sources","chunks","term_stats","receipts","artifacts","scopes"):
            self.assertEqual(conn.execute("SELECT count(*) FROM "+table).fetchone()[0],0,table)
        self.assertEqual(conn.execute("SELECT count(*) FROM chunk_fts").fetchone()[0],0)

    def test_concurrent_duplicate_and_unique_writes(self):
        def write(index):
            try: return self.store.add(body("trace_marker_"+str(index),request="concurrent"+str(index)))
            finally: self.store.close()
        with ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(write,[1,1,2,3,4,5,6,7]))
        self.assertTrue(all(x["success"] for x in results))
        self.assertEqual(self.store._connection().execute("SELECT count(*) FROM sources").fetchone()[0],7)

    def test_cross_session_retrieval(self):
        self.store.add(body("old_session_capability get_user_by_id",session="old"))
        self.store.add(body("current content",request="r2",session="new"))
        self.assertIn("old_session_capability",str(self.query("get_user_by_id")))

    def test_long_code_tail_and_original_persistence(self):
        text="# repeated log entry data\n"*10000+"tail_unique_function()\n"
        self.store.add(body(text))
        self.assertIn("tail_unique_function",self.query("tail_unique_function",k=1)["data"][0]["content"])
        raw=self.store._connection().execute("SELECT content FROM sources").fetchone()[0]
        self.assertEqual(zlib.decompress(raw).decode(),text)

    def test_known_failure_is_bundled_with_action(self):
        action="Task: src/auth.py parse_user TypeError.\nAction: remove the guard."
        failure="src/auth.py parse_user: pytest tests/test_auth.py\n2 failed in 0.1s"
        self.store.add(body(action));self.store.add(body(failure,request="r2"))
        result=self.query("src/auth.py remove guard",k=10)
        action_packets=[row for row in result["data"] if "remove the guard" in row["content"]]
        self.assertTrue(action_packets)
        self.assertIn(failure,action_packets[0]["content"])

    def test_same_filename_other_module_does_not_link(self):
        self.store.add(body("Action: edit src/auth/config.py apply_guard()."))
        self.store.add(body("src/billing/config.py tests failed",request="r2"))
        self.assertEqual(self.store._connection().execute("SELECT count(*) FROM links").fetchone()[0],0)

    def test_new_self_contained_action_does_not_verify_previous_action(self):
        self.store.add(body("Action: edit src/api.py first_attempt()."))
        self.store.add(body("Action: edit src/api.py second_attempt().\nTests passed.",request="r2"))
        self.assertEqual(self.store._connection().execute("SELECT count(*) FROM links").fetchone()[0],0)

    def test_failed_rollback_report_stays_with_its_previous_attempt(self):
        self.store.add(body("Action: edit src/api.py remove_guard()."))
        report="File: src/api.py\nAction: rolled back the preceding change; it failed.\n1 failed in 0.1s"
        self.store.add(body(report,request="r2"))
        rows=self.query("src/api.py remove_guard",k=10)["data"]
        selected=[x for x in rows if "remove_guard" in x["content"]]
        self.assertTrue(selected)
        self.assertIn(report,selected[0]["content"])

    def test_version_conflict_prevents_false_support_link(self):
        self.store.add(body("Action: edit src/api.py client_call() SDK==1.2."))
        self.store.add(body("src/api.py SDK==2.0 tests passed",request="r2"))
        self.assertEqual(self.store._connection().execute("SELECT count(*) FROM links").fetchone()[0],0)

    def test_different_symbols_and_unrelated_reports_do_not_link(self):
        self.store.add(body("File: src/auth.py\nSymbol: get_user_by_id\nAction: add a guard."))
        self.store.add(body("File: src/auth.py\nSymbol: delete_session\n2 failed in 0.1s",request="r2"))
        self.store.add(body("File: src/auth.py\nUnrelated task: 2 failed in 0.1s",request="r3"))
        self.assertEqual(self.store._connection().execute("SELECT count(*) FROM links").fetchone()[0],0)

    def test_linux_case_distinct_paths_do_not_link(self):
        self.store.add(body("Action: edit src/Auth.py add_guard()."))
        self.store.add(body("src/auth.py tests failed",request="r2"))
        self.assertEqual(self.store._connection().execute("SELECT count(*) FROM links").fetchone()[0],0)

    def test_session_local_window_survives_other_session_traffic(self):
        self.store.add(body("Action: src/auth.py add_guard().",session="task"))
        for index in range(7): self.store.add(body("other traffic",request=str(index),session="other"))
        self.store.add(body("src/auth.py tests failed",request="report",session="task"))
        self.assertEqual(self.store._connection().execute("SELECT count(*) FROM links").fetchone()[0],1)

    def test_budget_is_part_of_frozen_configuration(self):
        with self.assertRaises(ValueError): MemoryStore(self.path,response_bytes=400)

    def test_llm_quote_label_cannot_relabel_validation_as_new_action(self):
        self.store.add(body("Action: src/auth.py add_guard()."))
        text="src/auth.py tests failed"
        anchor={"message_index":0,"start":0,"end":len(text),"kind":"action","text":text,"origin":"gpt-4o-mini_exact_quote"}
        with mock.patch.object(self.store.model,"extract",return_value=[anchor]): self.store.add(body(text,request="r2"))
        self.assertEqual(self.store._connection().execute("SELECT count(*) FROM links").fetchone()[0],1)

    def test_raw_baseline_has_no_structured_artifacts(self):
        raw=MemoryStore(str(Path(self.temp.name)/"raw.sqlite3"),strategy="raw")
        try:
            raw.add(body("Action: src/auth.py add_guard() handles TypeError."))
            self.assertEqual(raw._connection().execute("SELECT count(*) FROM artifacts").fetchone()[0],0)
            self.assertIn("add_guard",str(raw.search({"query":"add_guard","user_id":"u","top_k":1})))
        finally: raw.close()

    def test_llm_quote_cannot_turn_old_task_into_a_repair_action(self):
        text="File: src/a.py\nTask: explain input guard."
        anchor={"message_index":0,"start":15,"end":len(text),"kind":"action","text":text[15:],"origin":"gpt-4o-mini_exact_quote"}
        with mock.patch.object(self.store.model,"extract",return_value=[anchor]): self.store.add(body(text))
        self.store.add(body("src/a.py tests failed",request="r2"))
        self.assertEqual(self.store._connection().execute("SELECT count(*) FROM links").fetchone()[0],0)

    def test_dense_best_chunk_not_overwritten_by_lower_chunks(self):
        def vector(x): return [x,(1-x*x)**.5]+[0.]*1022
        client=ModelClient(embedding_mode="v4",embedding_api_key="dummy-not-a-real-key",transport=lambda *_:None)
        dense=MemoryStore(str(Path(self.temp.name)/"dense.sqlite3"),strategy="hybrid",model_client=client)
        try:
            with mock.patch.object(client,"embed",return_value=[vector(1.),vector(.2),vector(.1)]): dense.add(body("A"*9000))
            with mock.patch.object(client,"embed",return_value=[vector(.8)]): dense.add(body("B"*200,request="r2"))
            with mock.patch.object(client,"embed",return_value=[vector(1.)]): result=dense.search({"query":"unmatched dense query","user_id":"u","top_k":1})
            self.assertIn("A"*100,result["data"][0]["content"])
            with mock.patch.object(client,"embed",side_effect=AssertionError("Empty scope made a model request")):
                self.assertEqual(dense.search({"query":"query","user_id":"absent","top_k":1}),{"data":[]})
            dense._connection().execute("UPDATE scopes SET n=50000 WHERE user_id='u'")
            with mock.patch.object(client,"extract",side_effect=AssertionError("Overflow made a model request")):
                with self.assertRaises(ContractError) as raised: dense.add(body("overflow",request="r3"))
            self.assertEqual(raised.exception.status,507)
            self.assertEqual(dense._connection().execute("SELECT count(*) FROM receipts").fetchone()[0],2)
        finally: dense.close()

    def test_unknown_unsupported_claim_never_verified(self):
        self.store.add(body("Action: src/api.py change iterator. It should pass tests."))
        features=json.loads(self.store._connection().execute("SELECT features FROM sources").fetchone()[0])
        self.assertEqual(features["outcome"],"unverified")

    def test_budget_never_splits_failure_closure(self):
        action="Action: src/api.py delete the validation."
        failure="src/api.py tests failed. "+"failure_context "*100
        self.store.add(body(action));self.store.add(body(failure,request="r2"))
        self.store.response_bytes=500
        response=self.query("src/api.py delete validation")
        for row in response["data"]:
            if "delete the validation" in row["content"]: self.assertIn(failure,row["content"])

    def test_top_k_and_format(self):
        self.store.add(body("src/a.py needle1","1"));self.store.add(body("src/b.py needle1","2"))
        result=self.query("needle1",k=1)
        self.assertEqual(len(result["data"]),1)
        self.assertTrue(result["data"][0]["id"])

    def test_frozen_configuration_mismatch_preserves_database(self):
        self.store.add(body("source untouched"))
        with self.assertRaises(ValueError): MemoryStore(self.path,strategy="raw")
        self.assertEqual(self.store._connection().execute("SELECT count(*) FROM sources").fetchone()[0],1)

    def test_model_failure_is_not_successful_or_partially_persisted(self):
        def failed(messages): raise ContractError("model_http_429",503)
        with mock.patch.object(self.store.model,"extract",side_effect=failed):
            with self.assertRaises(ContractError): self.store.add(body("Action: fix this"))
        self.assertEqual(self.store._connection().execute("SELECT count(*) FROM receipts").fetchone()[0],0)

    def test_valid_model_quote_becomes_real_retrievable_index_chunk(self):
        content="Action: guard the parser input in src/auth.py."
        anchor={"message_index":0,"start":8,"end":30,"kind":"action","text":content[8:30]}
        with mock.patch.object(self.store.model,"extract",return_value=[anchor]):
            self.store.add(body(content))
        conn=self.store._connection()
        self.assertEqual(conn.execute("SELECT count(*) FROM chunks").fetchone()[0],2)
        self.assertEqual(conn.execute("SELECT start,end FROM chunks ORDER BY id DESC").fetchone()[:],(8,30))
        self.assertIn("guard the parser",str(self.query("parser input")))

    def test_disk_reserve_blocks_write(self):
        self.store.min_free_bytes=10**30
        with self.assertRaises(ContractError) as raised: self.store.add(body("x"))
        self.assertEqual(raised.exception.status,507)


if __name__=="__main__": unittest.main()
