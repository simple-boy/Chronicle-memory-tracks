"""Docker entrypoint: prepare only the data volume, then permanently drop privileges."""
import os
import sys
from pathlib import Path


def main():
    root = Path("/app/data")
    root.mkdir(exist_ok=True)
    configured = Path(os.getenv("MEMORY_DB_PATH", "/app/data/memories.sqlite3"))
    if not configured.is_absolute() or root not in configured.resolve().parents:
        raise SystemExit("Docker MEMORY_DB_PATH must be an absolute file path under /app/data")
    if os.geteuid() == 0:
        if root.is_symlink():
            raise SystemExit("The data root must not be a symlink")
        parent = configured.parent
        parent.mkdir(parents=True, exist_ok=True)
        for folder in [root, *[p for p in parent.parents if root in p.parents], parent]:
            os.chown(folder, 10001, 10001)
        for suffix in ("", "-wal", "-shm"):
            file = Path(str(configured) + suffix)
            if file.exists():
                if file.is_symlink():
                    raise SystemExit("Database files must not be symlinks")
                os.chown(file, 10001, 10001)
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    if not os.access(configured.parent, os.W_OK):
        raise SystemExit("Database parent is not writable by the service user")
    os.execv(sys.executable, [sys.executable, "-B", "/app/app.py"])


if __name__ == "__main__":
    main()
