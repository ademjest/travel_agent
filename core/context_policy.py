from __future__ import annotations

from dataclasses import dataclass

from core.model_policy import current_policy


@dataclass(frozen=True)
class ContextPolicy:
    purpose: str
    total_chars: int
    dialogue_chars: int
    group_chars: int
    quoted_chars: int
    document_chars: int
    recent_group_messages: int


BALANCED = {
    'intent_parse': ContextPolicy('intent_parse', 4200, 1200, 500, 500, 0, 6),
    'plan_reference': ContextPolicy('plan_reference', 5200, 1400, 500, 600, 0, 6),
    'reminder_continue': ContextPolicy('reminder_continue', 3600, 1000, 300, 500, 0, 4),
    'travel_lookup': ContextPolicy('travel_lookup', 4200, 1200, 600, 600, 0, 8),
    'document_qa': ContextPolicy('document_qa', 7000, 1800, 600, 700, 4000, 8),
    'tool_reasoning': ContextPolicy('tool_reasoning', 7000, 2400, 2200, 800, 3200, 16),
}


def context_policy(purpose: str) -> ContextPolicy:
    base = BALANCED.get(purpose, BALANCED['tool_reasoning'])
    tier = current_policy().tier
    factor = {'economy': 0.72, 'balanced': 1.0, 'quality': 1.2}[tier]
    if tier == 'balanced':
        return base
    return ContextPolicy(
        purpose=base.purpose,
        total_chars=max(1800, int(base.total_chars * factor)),
        dialogue_chars=max(600, int(base.dialogue_chars * factor)),
        group_chars=max(0, int(base.group_chars * factor)),
        quoted_chars=max(300, int(base.quoted_chars * factor)),
        document_chars=max(0, int(base.document_chars * factor)),
        recent_group_messages=base.recent_group_messages,
    )
