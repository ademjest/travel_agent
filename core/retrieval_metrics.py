import math


def retrieval_metrics(returned, relevant, k=5):
    found=list(dict.fromkeys(returned))[:k]
    gold=set(relevant)
    hits=[index+1 for index,item in enumerate(found) if item in gold]
    recall=len(hits)/len(gold) if gold else None
    dcg=sum(1/math.log2(rank+1) for rank in hits)
    ideal=sum(1/math.log2(rank+1) for rank in range(1,min(len(gold),k)+1))
    return {'recall':recall,'hit':bool(hits) if gold else None,
            'mrr':1/hits[0] if hits else 0,'ndcg':dcg/ideal if ideal else None,
            'false_positive':bool(found) if not gold else None}


def reciprocal_rank_fusion(rankings, constant=60):
    scores={}
    for ranked in rankings:
        for rank,key in enumerate(dict.fromkeys(ranked),1):
            scores[key]=scores.get(key,0)+1/(constant+rank)
    return sorted(scores,key=lambda key:(-scores[key],key))
