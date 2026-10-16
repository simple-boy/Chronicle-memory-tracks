"""Validate the actual source ZIP, including no-.git deployment initialization."""
import argparse
import hashlib
import json
from pathlib import Path,PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

from release import source_hash,project_revision


def verify(archive):
    report={'status':'failed','network_calls':0,'model_calls':0,'official_smoke':'not_run','official_full':'not_run'}
    with tempfile.TemporaryDirectory(prefix='forge-release-check-') as temporary:
        root=Path(temporary)
        with zipfile.ZipFile(archive) as package:
            for item in package.infolist():
                name=PurePosixPath(item.filename)
                if name.is_absolute() or '..' in name.parts or '\\' in item.filename or ':' in item.filename:
                    raise ValueError('Unsafe ZIP path')
                if item.filename=='.env' or any(p in {'data','private','.git','__pycache__'} for p in name.parts):
                    raise ValueError('Private/runtime file in release')
                if item.filename.endswith(('.db','.sqlite3','.pyc')) or (item.external_attr>>16)&0o170000==0o120000:
                    raise ValueError('Database, bytecode or symlink in release')
            package.extractall(root)
        manifest=json.loads((root/'release-manifest.json').read_text())
        if manifest.get('dirty') or not re.fullmatch('[a-f0-9]{40}',manifest.get('commit','')):
            raise ValueError('Release commit not frozen')
        for name,digest in manifest['files'].items():
            part=PurePosixPath(name)
            if part.is_absolute() or '..' in part.parts or '\\' in name or ':' in name:
                raise ValueError('Unsafe manifest path')
            if hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest: raise ValueError('File hash mismatch')
        if source_hash(root)!=manifest['source_sha256']: raise ValueError('Runtime source mismatch')
        if project_revision(root)!=(manifest['commit'],False): raise ValueError('ZIP revision discovery failed')
        for name in ('Dockerfile','requirements.lock','compose.yaml','scripts/deploy.sh','README.md','SUBMISSION_GUIDE.md','figures/method_overview.png','figures/deployment_architecture.png'):
            if not (root/name).is_file(): raise ValueError('Missing required deliverable')
        shutil.copyfile(root/'.env.example',root/'.env')
        guide=(root/'docs/deployment_guide.md').read_text(encoding='utf-8')
        snippets=re.findall(r"python3 - <<'PY'\n(.*?)\nPY",guide,re.S)
        selected=[snippet for snippet in snippets if "manifest = json.loads" in snippet or "secrets.token_urlsafe" in snippet]
        if len(selected)!=2: raise ValueError('Expected two documented ZIP initialization snippets')
        # Execute only the two project-authored, local manifest/empty-key initializers.
        for _ in range(2):
            for snippet in selected:
                result=subprocess.run([sys.executable,'-B','-c',snippet],cwd=root,capture_output=True,text=True,timeout=15)
                if result.returncode: raise ValueError('Documented initialization failed')
            values=dict(line.split('=',1) for line in (root/'.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
            if not values.get('MEMORY_API_KEY') or values.get('SOURCE_COMMIT')!=manifest['commit']: raise ValueError('Private initialization missing')
            if 'first_key' not in locals(): first_key=values['MEMORY_API_KEY']
            elif first_key!=values['MEMORY_API_KEY']: raise ValueError('Second initialization changed existing key')
        report.update(status='passed',files=len(manifest['files']),commit=manifest['commit'],source_sha256=manifest['source_sha256'],
            zip_without_git='passed',documented_init_twice='passed',existing_key_preserved=True,private_runtime_excluded=True,
            sha256=hashlib.sha256(Path(archive).read_bytes()).hexdigest())
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive',type=Path,required=True)
    p.add_argument('--report',type=Path,default=Path('dist/release-verification.json'))
    a=p.parse_args()
    try: result=verify(a.archive)
    except Exception as error: result={'status':'failed','error_type':type(error).__name__,'model_calls':0}
    a.report.parent.mkdir(parents=True,exist_ok=True)
    a.report.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))
    return 0 if result['status']=='passed' else 1


if __name__=='__main__': raise SystemExit(main())
