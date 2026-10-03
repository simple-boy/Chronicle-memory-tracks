"""Build a source-only ZIP with per-file hashes; never includes DBs or private results."""
import argparse
import hashlib
import json
import stat
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOP_FILES = {".env.example", ".gitignore", ".dockerignore", ".gitattributes", "Dockerfile", "entrypoint.sh",
             "requirements.txt", "render.yaml", "README.md", "ATTRIBUTION.md", "LICENSE",
             "SUBMISSION_CHECKLIST.md", "submission-coding.md", "submission-multimodal.md",
             "app.py", "bootstrap.py", "coding.py", "memory_core.py", "models.py",
             "multimodal.py", "records.py", "store.py"}
FOLDERS = {"tests":".py", "scripts":".py", "evaluation":".py", "config":".json", "docs":".md"}


def source_files():
    files = [path for path in ROOT.iterdir() if path.is_file() and
             path.name in TOP_FILES]
    for folder, extension in FOLDERS.items():
        files.extend(path for path in (ROOT / folder).glob("*" + extension) if path.is_file())
    for path in files:
        if path.is_symlink() or ROOT not in path.resolve().parents:
            raise ValueError("Refuse a symlink or source outside the project")
    return sorted(files, key=lambda path: path.relative_to(ROOT).as_posix())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/chronicle-memory-tracks.zip")
    args = parser.parse_args()
    files = source_files()
    manifest = {"kind":"source_snapshot_not_git_commit", "sha256":{
        path.relative_to(ROOT).as_posix():hashlib.sha256(path.read_bytes()).hexdigest() for path in files}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(ROOT).as_posix(), date_time=(2026,1,1,0,0,0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | (0o755 if path.name == "entrypoint.sh" else 0o644)) << 16
            bundle.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
        info = zipfile.ZipInfo("SOURCE_MANIFEST.json", date_time=(2026,1,1,0,0,0))
        bundle.writestr(info, json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"source_files":len(files), "zip":str(args.output.resolve()),
                      "sha256":hashlib.sha256(args.output.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
