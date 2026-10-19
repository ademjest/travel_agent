"""Service-owned context and result contracts; model output cannot authorize a write."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re

from core.tasks import TaskUpdate, read_task_continuation
from core.tool_result import ToolResult
from core.intent_ir import IntentIR, IntentOperation, READ_OPERATION_FIELDS


@dataclass(frozen=True)
class TaskContext:
    task: object | None
    message: str
    reference_time: str
    slots: dict = field(default_factory=dict)
    input_text: str = ''

    @classmethod
    def for_read(cls, event, tasks):
        task, message = read_task_continuation(event.content, tasks)
        timestamp = (event.occurred_at or datetime.now(timezone.utc)).isoformat()
        reference = task.slots.get('reference_time', timestamp) if task else timestamp
        if task and re.search(r'今天|明天|后天', event.content):
            reference = timestamp
        return cls(task, message, reference, dict(task.slots) if task else {}, event.content)

    def read_value(self, field, proposed):
        current=self.slots.get(field,'')
        if (self.task and field in {'origin','destination'} and current
                and isinstance(proposed,str) and proposed!=current and proposed not in self.input_text):
            return current
        return proposed


@dataclass(frozen=True)
class TravelReadRequest:
    queries: tuple[IntentOperation, ...]
    context: TaskContext

    @classmethod
    def from_ir(cls, ir, context):
        if (not isinstance(ir,IntentIR) or ir.action not in {'read','clarify'}
                or ir.requires_confirmation or not ir.source_spans or not 1<=len(ir.operations)<=8
                or any(span not in context.message for span in ir.source_spans)):
            return None
        queries=[]
        for operation in ir.operations:
            fields=READ_OPERATION_FIELDS.get(operation.domain)
            if (fields is None or operation.operation != ('search' if operation.domain=='places' else 'query')
                    or operation.values.keys()-fields):
                return None
            values={}
            for field in fields:
                value=context.read_value(field,operation.values.get(field,context.slots.get(field,'')))
                if (not isinstance(value,str) or len(value)>150
                        or (value and value not in context.message and value!=context.slots.get(field))):
                    return None
                if value:
                    values[field]=value
            queries.append(IntentOperation(operation.domain,operation.operation,values))
        queries=[query for query in queries if not (query.domain=='route'
            and query.values.get('origin') and query.values.get('destination')
            and any(other.domain=='traffic' and other.values==query.values for other in queries))]
        # The existing loop tracks required tools by domain, not by location.
        # Same-domain multi-location comparisons keep the full original-text path.
        if len({query.domain for query in queries})!=len(queries):
            return None
        return cls(tuple(queries),context)

    @property
    def known_fields(self):
        fields=set().union(*(READ_OPERATION_FIELDS[query.domain] for query in self.queries))
        return {field for field in fields if all(query.values.get(field)
                for query in self.queries if field in READ_OPERATION_FIELDS[query.domain])}


@dataclass(frozen=True)
class ExecutionOutcome:
    status: str
    reply: str
    task_update: TaskUpdate | None = None
    receipts: tuple[ToolResult, ...] = ()

    def persist(self, store, event, claim):
        if self.task_update is not None and claim is not None and hasattr(store, 'tasks'):
            store.tasks.prepare_result(event, claim, self.task_update, self.reply)
        return self.reply
