from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


MODEL_EVENT = ContextVar('travel_model_event', default='')
MODEL_CALL = ContextVar('travel_model_call', default=None)
MODEL_RUN = ContextVar('travel_model_run', default='')


@dataclass(frozen=True)
class ModelCallContext:
    purpose: str = 'unspecified'
    prompt_version: str = ''
    context_policy_version: str = ''
    repair_attempt: int = 0
    fallback_from: str = ''
    force_default_model: bool = False
    output_multiplier: int = 1


@contextmanager
def model_call_context(
        purpose: str,
        *,
        prompt_version: str = '',
        context_policy_version: str = '',
        repair_attempt: int = 0,
        fallback_from: str = '',
        force_default_model: bool = False,
        output_multiplier: int = 1):
    if output_multiplier not in (1,2) or (output_multiplier != 1 and purpose not in {'trip_plan','trip_plan_repair'}):
        raise ValueError('输出扩展仅适用于经过校验的长行程，最大两倍。')
    value = ModelCallContext(
        purpose=purpose,
        prompt_version=prompt_version,
        context_policy_version=context_policy_version,
        repair_attempt=repair_attempt,
        fallback_from=fallback_from,
        force_default_model=force_default_model,
        output_multiplier=output_multiplier,
    )
    token = MODEL_CALL.set(value)
    try:
        yield value
    finally:
        MODEL_CALL.reset(token)


class ModelBudgetExceeded(ValueError):
    pass
