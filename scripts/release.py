"""Build a source-only local release; never publish or include runtime memory."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile

ROOT=Path(__file__).resolve().parents[1]
TOP={'README.md','METHOD.md','EXPERIMENTS.md','BENCHMARK_ANALYSIS.md','DEPLOYMENT_GUIDE.md',
     'SUBMISSION_GUIDE.md','SUBMISSION_CHECKLIST.md','REPRODUCIBILITY.md','ATTRIBUTION.md','LICENSE','Dockerfile','compose.yaml',
     '.env.example','.gitignore','.gitattributes','.dockerignore','requirements.txt','requirements.lock','requirements-dev.txt'}
DIRS={'src','scripts','tests','docs','figures','evaluation','results','config'}


def source_hash(root=ROOT):
    digest=hashlib.sha256()
    for path in sorted((root/'src/forgetrace').glob('*.py')):
        digest.update(path.name.encode()+b'\0'+hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def project_revision(root=ROOT):
    def git(*args):
        result=subprocess.run(['git','-C',str(root),*args],capture_output=True,text=True,check=False)
        return result.stdout.strip() if result.returncode==0 else None
    top=git('rev-parse','--show-toplevel')
    if top and Path(top).resolve()==root.resolve():
        return git('rev-parse','HEAD'),bool(git('status','--porcelain'))
    manifest=root/'release-manifest.json'
    if manifest.exists():
        data=json.loads(manifest.read_text(encoding='utf-8'))
        if data.get('source_sha256')!=source_hash(root): raise ValueError('Source differs from ZIP manifest')
        return data.get('commit'),False
    return None,True


def release_files(root=ROOT):
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink(): continue
        relative=path.relative_to(root)
        if relative.parts[0] not in TOP|DIRS: continue
        if any(part in {'__pycache__','.git','private','data','dist','artifacts'} for part in relative.parts): continue
        if path.suffix.lower() in {'.pyc','.log','.sqlite3','.db'} or '-private' in path.name: continue
        if path.name.startswith('.env') and path.name!='.env.example': continue
        if path.suffix.lower() not in {'.png','.jpg','.jpeg','.pdf'}:
            text=path.read_text(encoding='utf-8')
            if re.search(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}',text):
                raise ValueError('Possible credential in release input: '+relative.as_posix())
        yield path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-hash',action='store_true')
    parser.add_argument('--output',type=Path,default=ROOT/'dist/ForgeTrace-0.1.0.zip')
    parser.add_argument('--allow-dirty',action='store_true',help='Diagnostic archive, never a frozen submission release')
    args=parser.parse_args()
    if args.source_hash: print(source_hash());return
    commit,dirty=project_revision()
    if (not commit or dirty) and not args.allow_dirty:
        raise SystemExit('Commit and clean the project first; no public push is required.')
    files=list(release_files())
    manifest={'format':1,'system':'ForgeTrace','version':'0.1.0','commit':commit,'dirty':dirty,
              'source_sha256':source_hash(),'publication':'local_only',
              'files':{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(args.output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for path in files: archive.write(path,path.relative_to(ROOT).as_posix())
        archive.writestr('release-manifest.json',json.dumps(manifest,indent=2)+'\n')
    digest=hashlib.sha256(args.output.read_bytes()).hexdigest()
    args.output.with_suffix('.sha256').write_text(digest+'  '+args.output.name+'\n',encoding='utf-8')
    print(json.dumps({'archive':str(args.output),'sha256':digest,'commit':commit,'source_sha256':manifest['source_sha256'],'dirty':dirty}))


if __name__=='__main__': main()
