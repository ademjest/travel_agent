"""Versioned intermediate representation between natural language and business services."""
from dataclasses import dataclass
from typing import Any


SUPPORTED_ACTIONS = {'none', 'clarify', 'read', 'create', 'update', 'delete', 'confirm', 'cancel'}
SUPPORTED_DOMAINS = {'trip', 'reminder', 'booking', 'weather', 'forecast', 'route', 'traffic', 'places', 'document', 'system'}
FORBIDDEN_MODEL_KEYS = {'trip_id', 'reminder_id', 'plan_code', 'item_code', 'owner_id', 'group_id', 'platform', 'scope_id', 'principal_id', 'account_id', 'user_id'}


def _contains_forbidden(value):
    if isinstance(value, dict):
        return any(key in FORBIDDEN_MODEL_KEYS or _contains_forbidden(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_forbidden(item) for item in value)
    return False


@dataclass(frozen=True)
class IntentOperation:
    domain: str
    operation: str
    values: dict[str, Any]


@dataclass(frozen=True)
class IntentIR:
    schema_version: int
    action: str
    operations: tuple[IntentOperation, ...]
    missing_fields: tuple[str, ...] = ()
    requires_confirmation: bool = False
    source_spans: tuple[str, ...] = ()
    confidence: float | None = None

    @property
    def actionable(self) -> bool:
        return self.action not in {'none', 'clarify'} and bool(self.operations)


def parse_intent_ir(value: Any, source_text: str) -> IntentIR:
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise ValueError('语义解析版本无效。')
    action = value.get('action') or value.get('intent')
    action = {'modify': 'update', 'edit': 'update', 'question': 'clarify', 'execute': 'confirm'}.get(action, action)
    if action not in SUPPORTED_ACTIONS:
        raise ValueError('语义解析动作无效。')
    raw_operations = value.get('operations', [])
    if not isinstance(raw_operations, list) or len(raw_operations) > 8:
        raise ValueError('语义解析操作数量无效。')
    operations = []
    for raw in raw_operations:
        if not isinstance(raw, dict) or raw.get('domain') not in SUPPORTED_DOMAINS:
            raise ValueError('语义解析领域无效。')
        operation = raw.get('operation')
        operation = {'remove_trip_activity': 'remove_activity', 'delete_activity': 'remove_activity',
                     'view_trip': 'view_current_trip', 'confirm_trip': 'confirm_pending_operation'}.get(operation, operation)
        if not isinstance(operation, str) or not operation or len(operation) > 80:
            raise ValueError('语义解析操作无效。')
        values = {key: item for key, item in raw.items() if key not in {'domain', 'operation'}}
        if _contains_forbidden(values):
            raise ValueError('语义解析不能生成资源编号或身份字段。')
        operations.append(IntentOperation(raw['domain'], operation, values))
    missing = value.get('missing_fields', [])
    spans = value.get('source_spans', [])
    if not isinstance(missing, list) or any(not isinstance(item, str) or len(item) > 80 for item in missing):
        raise ValueError('语义解析缺失字段无效。')
    if not isinstance(spans, list) or len(spans) > 20 or any(not isinstance(item, str) or not item or len(item) > 200 for item in spans):
        raise ValueError('语义解析原文依据无效。')
    if any(span not in source_text for span in spans):
        raise ValueError('语义解析原文依据无法对应用户消息。')
    confirmation = value.get('requires_confirmation', False)
    if type(confirmation) is not bool:
        raise ValueError('语义解析确认标记无效。')
    confidence = value.get('confidence')
    if confidence is not None and (type(confidence) not in {int, float} or not 0 <= confidence <= 1):
        raise ValueError('语义解析置信度无效。')
    if action == 'none' and operations:
        raise ValueError('无动作语义不能包含操作。')
    if action not in {'none', 'clarify'} and not operations and not missing:
        raise ValueError('有动作语义必须包含操作或缺失字段。')
    return IntentIR(1, action, tuple(operations), tuple(missing), confirmation, tuple(spans), confidence)


def to_jsonable(ir: IntentIR) -> dict[str, Any]:
    return {'schema_version': ir.schema_version, 'action': ir.action,
            'operations': [{'domain': operation.domain, 'operation': operation.operation, **operation.values}
                           for operation in ir.operations],
            'missing_fields': list(ir.missing_fields), 'requires_confirmation': ir.requires_confirmation,
            'source_spans': list(ir.source_spans), 'confidence': ir.confidence}
