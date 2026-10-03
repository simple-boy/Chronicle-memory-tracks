"""Verify the shipped administrative CLI's dry run, scope and actual cleanup."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from coding import CodingProcessor
from store import TrackStore

ROOT = Path(__file__).resolve().parents[1]


class PurgeTests(unittest.TestCase):
    def test_dry_run_then_delete_only_exact_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "memories.sqlite3")
            store = TrackStore(db, CodingProcessor())
            try:
                for user in ("purge-user", "keep-user"):
                    store.add_messages(request_id="same-r",user_id=user,session_id="s",
                                       messages=[{"role":"user","content":"src/config.py parseConfig fixed ConfigError"}])
            finally:
                store.close()
            command = [sys.executable, "-B", str(ROOT / "scripts/purge.py"),
                       "--db", db, "--user-id", "purge-user"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            dry = json.loads(result.stdout)
            self.assertFalse(dry["applied"])
            self.assertEqual(dry["counts"]["source_records"], 1)
            store = TrackStore(db, CodingProcessor())
            try:
                self.assertTrue(store.search(user_id="purge-user",query="parseConfig"))
            finally:
                store.close()
            result = subprocess.run(command + ["--apply"],capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["applied"])
            store = TrackStore(db, CodingProcessor())
            try:
                self.assertEqual(store.search(user_id="purge-user",query="parseConfig"), [])
                self.assertTrue(store.search(user_id="keep-user",query="parseConfig"))
                for table in ("memories","memories_fts","memory_entities","memory_relations",
                              "source_records","raw_messages","add_requests","model_usage"):
                    self.assertEqual(store._connection().execute(
                        f"SELECT COUNT(*) FROM {table} WHERE user_id=?",("purge-user",)).fetchone()[0],0)
            finally:
                store.close()
