from __future__ import annotations

from dataclasses import dataclass
import os


PURPOSE_MODEL_ENV = {
    'intent_parse': 'LLM_INTENT_MODEL_ID',
    'conversation_summary': 'LLM_SUMMARY_MODEL_ID',
    'research_plan': 'LLM_RESEARCH_MODEL_ID',
    'research_targets': 'LLM_RESEARCH_MODEL_ID',
    'research_fact_synthesis': 'LLM_RESEARCH_MODEL_ID',
    'research_synthesis': 'LLM_RESEARCH_MODEL_ID',
    'travel_answer_write': 'LLM_RESEARCH_MODEL_ID',
    'image_extract': 'LLM_VISION_MODEL_ID',
}

PURPOSE_OUTPUTS = {
    'intent_parse': (768, 1024, 1536),
    'reminder_extract': (768, 1024, 1536),
    'tool_reasoning': (1024, 2048, 4096),
    'tool_answer': (768, 1536, 3072),
    'trip_requirement_extract': (1024, 1536, 2048),
    'trip_plan': (4096, 6144, 8192),
    'trip_plan_repair': (4096, 6144, 8192),
    'travel_answer_write': (4096, 6144, 8192),
    'research_plan': (1024, 1536, 2048),
    'research_targets': (2048, 4096, 6144),
    'research_fact_synthesis': (2048, 3072, 4096),
    'research_synthesis': (1536, 2048, 4096),
    'document_summary': (1024, 2048, 3072),
    'image_extract': (2048, 4096, 6144),
    'conversation_summary': (768, 1024, 1536),
    'retrieval_embedding': (1, 1, 1),
    # Legacy and third-party callers keep the previous output ceiling until
    # they opt into an explicit, evaluated purpose.
    'unspecified': (8192, 8192, 8192),
}

TIERS = {'economy': 0, 'balanced': 1, 'quality': 2}


@dataclass(frozen=True)
class ModelPolicy:
    tier: str = 'balanced'
    daily_token_limit: int = 0
    monthly_token_limit: int = 0
    summary_enabled: bool = False
    vector_enabled: bool = False
    daily_cost_limit: float = 0
    monthly_cost_limit: float = 0
    cost_currency: str = 'USD'


def _positive_int(name: str) -> int:
    try:
        return max(0, int(os.getenv(name, '0').strip() or '0'))
    except ValueError:
        return 0


def _flag(name: str) -> bool:
    return os.getenv(name, '').strip().lower() in {'1', 'true', 'yes', 'on'}


def current_policy() -> ModelPolicy:
    tier = os.getenv('AI_BUDGET_TIER', 'balanced').strip().lower()
    if tier not in TIERS:
        tier = 'balanced'
    return ModelPolicy(
        tier=tier,
        daily_token_limit=_positive_int('AI_DAILY_TOKEN_LIMIT'),
        monthly_token_limit=_positive_int('AI_MONTHLY_TOKEN_LIMIT'),
        summary_enabled=_flag('AI_SUMMARY_ENABLED'),
        vector_enabled=_flag('AI_VECTOR_ENABLED'),
        daily_cost_limit=_cost_limit('AI_DAILY_COST_LIMIT'),
        monthly_cost_limit=_cost_limit('AI_MONTHLY_COST_LIMIT'),
        cost_currency=os.getenv('AI_COST_CURRENCY','USD').strip().upper() or 'USD',
    )


def _cost_limit(name):
    from core.model_pricing import decimal_price
    value=decimal_price(os.getenv(name,'0'))
    return float(value or 0)


def model_for_purpose(default_model: str, purpose: str) -> str:
    key = PURPOSE_MODEL_ENV.get(purpose)
    return (os.getenv(key, '').strip() if key else '') or default_model


def output_limit_for_purpose(purpose: str, requested: int) -> int:
    policy = current_policy()
    values = PURPOSE_OUTPUTS.get(purpose, PURPOSE_OUTPUTS['unspecified'])
    limit = values[TIERS[policy.tier]]
    return max(1, min(requested, limit))
