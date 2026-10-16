"""Actual legacy/new call-path count with mock transports, never paid inference.

Requires an explicitly supplied legacy checkout. Measures calls, not model quality
or end-to-end speed; it does not read legacy .env, memory databases or logs.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import zlib

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from forgetrace.models import ModelClient
from forgetrace.storage import MemoryStore


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--legacy-dir',type=Path,required=True)
    p.add_argument('--output',type=Path,default=ROOT/'results/model-call-counts.json')
    args=p.parse_args()
    legacy=args.legacy_dir.resolve()
    if not (legacy/'coding.py').is_file(): p.error('legacy-dir must contain the actual coding.py')
    sys.path.insert(0,str(legacy))
    from coding import CodingProcessor
    report={'kind':'mock_transport_call_path_diagnostic_not_latency_or_model_quality','real_model_calls':0,
            'legacy_coding_sha256':hashlib.sha256((legacy/'coding.py').read_bytes()).hexdigest(),'profiles':[]}
    for size,count in [(1024,1),(25000,8),(8192,100)]:
        text=('File: src/auth.py\nAction: add a None guard before get_user_by_id().\n'*(size//65+2))[:size]
        class LegacyCounter:
            calls=0
            def index_code(self,chunk): self.calls+=1;return 'source index hints'
        old=LegacyCounter();processor=CodingProcessor(old)
        before=sum(processor.planned_calls(text) for _ in range(count))
        for _ in range(count): processor.prepare(text)
        seen=[]
        def transport(url,payload):
            seen.append(payload)
            return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'anchors':[{'snippet_id':0,'kind':'action','quote':'Action: add a None guard'}]})}}]}
        client=ModelClient('gpt-4o-mini','dummy-not-a-real-key',transport=transport)
        with tempfile.TemporaryDirectory(prefix='forge-count-') as directory:
            store=MemoryStore(Path(directory)/'count.sqlite3',model_client=client)
            body={'user_id':'self-created','session_id':'s','request_id':'r','messages':[{'role':'user','content':text} for _ in range(count)]}
            store.add(body);store.add(body)
            stored=[zlib.decompress(row[0]).decode() for row in store._connection().execute('SELECT content FROM sources')]
            if stored!=[text]*count: raise RuntimeError('New representation lost raw evidence')
            store.close()
        if before!=old.calls or len(seen)!=1: raise RuntimeError('Unexpected call path count')
        report['profiles'].append({'source_characters_each':len(text),'messages':count,'legacy_planned_and_observed_mock_calls':old.calls,
            'new_observed_mock_calls_including_idempotent_retry':len(seen),'new_model_view_bytes':client.stats()['sampled_model_input_bytes'],
            'raw_sources_losslessly_preserved':True,'new_anchor_exact_source_check':'passed'})
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__': main()
