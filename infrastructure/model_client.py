"""Shared SDK connection policy and metadata-only failure classification."""
import os
import re

import httpx
from openai import OpenAI,DefaultHttpxClient,APIConnectionError,APITimeoutError,APIStatusError

from core.model_budget import ModelBudgetExceeded
from core.model_config import ModelConfigError


def create_model_client(*, api_key, base_url, timeout=90, before_request=None, **kwargs):
    http=DefaultHttpxClient(timeout=timeout,trust_env=True,
        event_hooks={'request':[before_request]} if before_request else None)
    try:
        client=OpenAI(api_key=api_key,base_url=base_url,timeout=timeout,http_client=http,
                      **{**kwargs,'max_retries':0})
    except Exception:
        http.close()
        raise
    client.network_metadata={'network_policy':'sdk_environment','trust_env':True,
        'proxy_env_set':any(os.getenv(key) for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy')),
        'no_proxy_env_set':bool(os.getenv('NO_PROXY') or os.getenv('no_proxy')),
        'target_host':client.base_url.host}
    return client


def model_failure_metadata(error):
    chain=[]
    while error is not None and id(error) not in {id(item) for item in chain}:
        chain.append(error)
        error=error.__cause__ or (error.__context__ if not error.__suppress_context__ else None)
    code=''
    if any(isinstance(item,ModelConfigError) for item in chain):code='model_configuration'
    elif any(isinstance(item,ModelBudgetExceeded) for item in chain):code='budget_exceeded'
    elif any(isinstance(item,(APITimeoutError,httpx.TimeoutException,TimeoutError)) for item in chain):code='timeout'
    elif any(isinstance(item,(APIConnectionError,httpx.RequestError)) for item in chain):code='connection'
    status=next((item.status_code for item in chain if isinstance(item,APIStatusError)),None)
    if status is not None:
        code='authentication' if status in {401,403} else 'rate_limit' if status==429 else 'upstream_error' if status>=500 else 'invalid_request'
    if not code:return {}
    data={'error_code':code,'cause_type':type(chain[1] if len(chain)>1 else chain[0]).__name__,
          'root_cause_type':type(chain[-1]).__name__}
    for kind,phase in ((httpx.ConnectTimeout,'connect'),(httpx.ReadTimeout,'read'),(httpx.WriteTimeout,'write'),(httpx.PoolTimeout,'pool')):
        if any(isinstance(item,kind) for item in chain):data['timeout_phase']=phase;break
    if code=='timeout' and 'timeout_phase' not in data:data['timeout_phase']='unknown'
    if status is not None:data['http_status']=status
    request_id=next((getattr(item,'request_id','') for item in chain if isinstance(item,APIStatusError)),'')
    if isinstance(request_id,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',request_id):data['upstream_request_id']=request_id
    return data
