"""Check a running API; readiness requires operator evidence, never implies AML success."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys

from smoke_test import request,run_checks,new_state


def verify(args,key):
    report={'system':'ForgeTrace','ready_for_submission':False,'official_smoke':'not_run','official_full':'not_run','checks':{},'blockers':[]}
    def block(reason): report['blockers'].append(reason)
    try:
        status,health,_=request(args.base_url,'/health')
        if status!=200 or health.get('service')!='forgetrace' or health.get('track')!='coding':
            raise RuntimeError('health_failed')
        report['health']={k:health.get(k) for k in ('version','commit','source_sha256','model_mode','embedding_mode','diagnostic_only')}
        report['checks']['health']='passed'
        if not args.commit or args.commit=='unfixed' or health.get('commit')!=args.commit: block('fixed_commit_not_matched')
        if not args.source_sha256 or health.get('source_sha256')!=args.source_sha256: block('runtime_source_hash_not_matched')
        if health.get('diagnostic_only') or health.get('model_mode')!='gpt-4o-mini': block('offline_model_not_cleared_for_open_source_submission')
        if not key: block('memory_key_missing');return report
        status,_,_=request(args.base_url,'/search',method='POST',payload={'query':'private auth check','user_id':'selfcheck-none','top_k':1})
        report['checks']['unauthenticated_rejected']='passed' if status==401 else 'failed'
        if status!=401: block('unauthenticated_search_allowed')
        status,metrics,_=request(args.base_url,'/metrics',key=key)
        if status!=200: block('authenticated_metrics_unavailable')
        else:
            free=metrics.get('storage',{}).get('disk_free_bytes')
            report['disk_free_bytes']=free
            if free is None or free<1024**3: block('disk_reserve_below_1gib')
        if args.write:
            if not (1<=args.concurrency<=2 and 1<=args.requests<=4): raise RuntimeError('verification_is_capped_at_2_concurrency_4_requests')
            states=[new_state(args.base_url) for _ in range(args.requests)]
            def one(state):
                return run_checks(args.base_url,key,state=state,allow_model_calls=args.allow_model_calls)
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool: outcomes=list(pool.map(one,states))
            report['checks']['synthetic_contract_and_isolation']=outcomes
            report['checks']['requested_concurrency']=args.concurrency
        else: block('end_to_end_write_check_not_run')
        if args.division=='open-source' and not args.eligibility_evidence: block('model_eligibility_evidence_not_recorded')
        # These are attestations. They cannot be established by public health or a tiny smoke test.
        for flag in ('confirm_disk','confirm_version','confirm_no_private_data'):
            report['checks'][flag]=bool(getattr(args,flag))
            if not getattr(args,flag): block(flag+'_missing')
        report['eligibility_evidence']=args.eligibility_evidence or None
        report['ready_for_submission']=not report['blockers']
    except Exception as error:
        block('verification_failed:'+type(error).__name__)
        report['checks']['execution']='failed'
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',required=True)
    parser.add_argument('--write',action='store_true')
    parser.add_argument('--concurrency',type=int,default=2)
    parser.add_argument('--requests',type=int,default=2)
    parser.add_argument('--commit',default='')
    parser.add_argument('--source-sha256',default='')
    parser.add_argument('--division',choices=('open-source','commercial'),default='open-source')
    parser.add_argument('--eligibility-evidence',default='')
    for name in ('confirm-disk','confirm-version','confirm-no-private-data','allow-model-calls'): parser.add_argument('--'+name,action='store_true')
    parser.add_argument('--report',type=Path,default=Path('results/submission-verification.json'))
    args=parser.parse_args()
    result=verify(args,os.getenv('MEMORY_API_KEY',''))
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))
    return 0 if result['ready_for_submission'] else 1


if __name__=='__main__': raise SystemExit(main())
