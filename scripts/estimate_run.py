"""Scenario calculator, not a claim that the official Full will finish on time."""
import argparse
import json


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--add-requests',type=int,required=True)
    p.add_argument('--search-requests',type=int,required=True)
    p.add_argument('--engine-add-seconds',type=float,required=True)
    p.add_argument('--model-add-seconds',type=float,required=True)
    p.add_argument('--search-seconds',type=float,required=True)
    p.add_argument('--add-concurrency',type=int,default=2)
    p.add_argument('--search-concurrency',type=int,default=2)
    p.add_argument('--platform-hours',type=float,required=True,help='Explicit assumed Answer/execution/Eval duration; unknown is not zero')
    p.add_argument('--queue-network-hours',type=float,default=0)
    p.add_argument('--input-tokens-per-add',type=int)
    p.add_argument('--output-tokens-per-add',type=int)
    a=p.parse_args()
    if min(a.add_requests,a.search_requests,a.engine_add_seconds,a.model_add_seconds,a.search_seconds,a.platform_hours,a.queue_network_hours)<0 or min(a.add_concurrency,a.search_concurrency)<1:
        p.error('counts, durations and concurrency must be valid')
    # SQLite commit is serialized. Model concurrency overlaps remote inference,
    # not fsync; take the greater lower bound, then explicitly inflate by scenario.
    add=a.add_requests*max(a.engine_add_seconds,a.model_add_seconds/a.add_concurrency)
    search=a.search_requests*a.search_seconds/a.search_concurrency
    result={'known_workload':{'add_requests':a.add_requests,'search_requests':a.search_requests},
            'not_official_workload':True,'assumed_model_seconds_per_add':a.model_add_seconds,
            'api_add_hours_heuristic':add/3600,'api_search_hours_heuristic':search/3600,
            'assumptions':'Engine service time is treated as serialized; model and query work use declared ideal concurrency. Factors 1/1.5/3 are assumptions, not confidence intervals.',
            'scenarios_hours':{},'official_result':None,
            'limitation':'No guarantee: worker contention, same-user workload, model RPM/TPM, upstream tails and official executor queue are not inferred from the published token count.'}
    for name,factor in [('optimistic',1),('neutral',1.5),('conservative',3)]:
        result['scenarios_hours'][name]=(add+search)/3600*factor+a.platform_hours+a.queue_network_hours
    if a.input_tokens_per_add is not None and a.output_tokens_per_add is not None:
        result['official_mini_estimated_usd_excluding_retries_embedding']=a.add_requests*(a.input_tokens_per_add*.15+a.output_tokens_per_add*.60)/1e6
        result['cost_scope']='Planning estimate only; not permission to incur Full costs; third-party billing differs.'
    print(json.dumps(result,indent=2))


if __name__=='__main__': main()
