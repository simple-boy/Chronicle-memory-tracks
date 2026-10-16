"""Scope applicability and source-counterevidence closure, without answer generation."""
from collections import Counter
import hashlib
import math
import re

from .parsing import tokenize

STOP=set("a an the and or to of is are be for this that with in on at as it i you we please how what why can should could would fix task file tests test error return def class import from none true false".split())
WORD=re.compile(r"[\w$@]+(?:[./\\:+#-][\w$@+#-]+)*",re.UNICODE)


def frequencies(text):
    counts=Counter(word.casefold() for word in WORD.findall(text))
    for token in tokenize(text):
        counts.setdefault(token,1)
    return {word:count for word,count in counts.items() if word not in STOP and len(word)>1}


def term_key(word): return "t"+hashlib.blake2s(word.encode(),digest_size=16).hexdigest()


def terms_query(words): return " OR ".join('"'+term_key(word)+'"' for word in words[:96])


def path_key(value):
    normalized=value.replace("\\","/")
    # Unix paths are case-sensitive. Only explicitly Windows-shaped paths fold.
    return normalized.casefold() if "\\" in value or re.match(r"^[A-Za-z]:/",normalized) else normalized


def scope_keys(features):
    return ["path:"+path_key(x) for x in features["paths"]]+[
           "symbol:"+x.casefold() for x in features["symbols"]]


def applicability(query,record):
    """Explicit package major/minor mismatches only; unknown is not incompatible.

    Version ranges are not silently reduced to exact versions. They stay source
    evidence but cannot provide an exact-version exclusion certificate.
    """
    qp={path_key(x) for x in query["paths"]}
    rp={path_key(x) for x in record["paths"]}
    exact_path=bool(qp&rp)
    version_conflict=False
    qv={x["package"]:x["version"] for x in query["versions"]}
    rv={x["package"]:x["version"] for x in record["versions"]}
    # A version immediately attached with == or a plain package version is exact;
    # constraints such as >=,<,!=,~= are not interpreted as exact equality.
    for package in qv.keys()&rv.keys():
        qrange=any(re.search(re.escape(package)+r"\s*(?:>=|<=|!=|~=|>|<)",x["text"],re.I) for x in query["evidence"])
        rrange=any(re.search(re.escape(package)+r"\s*(?:>=|<=|!=|~=|>|<)",x["text"],re.I) for x in record["evidence"])
        if not qrange and not rrange and qv[package]!=rv[package]: version_conflict=True
    path_conflict=bool(qp and rp and not exact_path)
    return {"path_match":exact_path,"path_conflict":path_conflict,"version_conflict":version_conflict,
            "symbol_match":bool({x.casefold() for x in query["symbols"]}&{x.casefold() for x in record["symbols"]})}


def rank_candidates(candidates,query,strategy,ablation="none"):
    for row in candidates:
        scope=applicability(query,row["features"])
        row["applicability"]=scope
        structural=int(scope["path_match"])*2+int(scope["symbol_match"])
        status=row["features"]["outcome"]
        # Reports are weak ordering evidence, not trusted execution or probability.
        reliability=1 if status=="reported_pass" else (-1 if status in {"reported_fail","conflicting"} else 0)
        if row.get("has_counterevidence"): reliability=-1
        if strategy in {"raw","hybrid"}:
            key=(0,0,0,row["base_score"])
        elif strategy=="structured":
            key=(0,structural,0,row["base_score"])
        else:
            mismatch=int(scope["version_conflict"])*2+int(scope["path_conflict"])
            if ablation=="no_applicability": mismatch=0
            if ablation=="no_structure": structural=0
            if ablation=="no_reliability": reliability=0
            # Lexicographic evidence policy: no fitted weights or confidence claims.
            key=(-mismatch,structural,reliability,row["base_score"])
        row["rank_key"]=key
    return sorted(candidates,key=lambda x:(x["rank_key"],-x["sequence"],x["id"]),reverse=True)


def chunk_ranges(text,size=4096):
    start=0
    while start<len(text):
        end=min(len(text),start+size)
        if end<len(text):
            newline=text.rfind("\n",start+size//2,end)
            if newline>start: end=newline+1
        yield start,end
        start=end


def excerpt_ranges(text,anchor_start,anchor_end,features,max_chars=16000):
    if len(text)<=max_chars: return [(0,len(text))]
    start=max(0,anchor_start-1000); end=min(len(text),max(anchor_end,start+6000))
    ranges=[(start,end)]
    # Keep an observed result from the same source even if it appears at the tail.
    for span in features.get("outcome_spans",[]):
        if not (start<=span["start"] and span["end"]<=end):
            ranges.append((max(0,span["start"]-160),min(len(text),span["end"]+160)))
    result=[]
    for begin,finish in sorted(ranges):
        if result and begin<=result[-1][1]: result[-1]=(result[-1][0],max(result[-1][1],finish))
        else: result.append((begin,finish))
    # If the observed result closure exceeds the declared item budget, skip it;
    # never drop known negative evidence just to retain an action.
    return result if sum(b-a for a,b in result)<=max_chars else []
