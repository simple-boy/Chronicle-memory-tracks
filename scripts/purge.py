"""Delete one explicit evaluation user/run scope and all derived records. Dry run by default."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True)
    p.add_argument("--user-id", required=True)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    path = Path(args.db).resolve()
    if not path.is_file():
        p.error("Existing database file required")
    # Open existing file only. This command never creates an empty replacement database.
    conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=30)
    conn.execute("PRAGMA secure_delete=ON")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    target_tables = [t for t in ("memories_fts", "memory_entities", "memory_relations", "source_records",
                     "raw_messages", "add_requests", "model_usage", "memories") if t in tables]
    conn.execute("BEGIN IMMEDIATE")
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE user_id=?", (args.user_id,)).fetchone()[0] for t in target_tables}
    if args.apply:
        for table in target_tables:
            conn.execute(f"DELETE FROM {table} WHERE user_id=?", (args.user_id,))
        conn.commit()
        conn.execute("VACUUM")
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint and checkpoint[0]:
            raise RuntimeError("WAL checkpoint busy: retry when active connections are idle")
    else:
        conn.rollback()
    conn.close()
    print(json.dumps({"applied": args.apply, "counts": counts,
                      "remaining_duty": "Delete provider snapshots, backups, logs and exports separately"}))


if __name__ == "__main__":
    main()
