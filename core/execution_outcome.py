"""Service-owned context and result contracts; model output cannot authorize a write."""
from dataclasses import dataclass, field

from core.tasks import TaskUpdate
from core.tool_result import ToolResult
from core.intent_ir import IntentIR, IntentOperation, READ_OPERATION_FIELDS, RAIL_ENUMS, DERIVED_READ_FIELDS


@dataclass(frozen=True)
class TaskContext:
    task: object | None
    message: str
    reference_time: str
    slots: dict = field(default_factory=dict)
    input_text: str = ''


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
    source: str = 'semantic'
    explain: bool = False
    missing_fields: tuple[tuple[str, ...], ...] = ()

    @classmethod
    def from_ir(cls, ir, context):
        if (not isinstance(ir,IntentIR) or ir.action not in {'read','clarify'}
                or ir.requires_confirmation or not ir.source_spans or not 1<=len(ir.operations)<=8
                or any(span not in context.message for span in ir.source_spans)):
            return None
        queries=[]
        missing=[]
        for name in ir.missing_fields:
            # Hotel missing fields are derived by the typed tool, not model labels.
            if len(ir.operations)==1 and ir.operations[0].domain=='hotel':continue
            owners=[op for op in ir.operations if name in READ_OPERATION_FIELDS.get(op.domain,())]
            if not owners and any(op.domain=='hotel' for op in ir.operations):
                from core.hotel_contract import LABELS
                if name in {*LABELS,'city'} or name.startswith(('stay.','discovery.')):
                    owners=[op for op in ir.operations if op.domain=='hotel']
            if not owners or len(owners)>1 and any(name not in DERIVED_READ_FIELDS.get(op.domain,()) for op in owners):
                return None
        for index,operation in enumerate(ir.operations):
            fields=READ_OPERATION_FIELDS.get(operation.domain)
            if (fields is None or operation.operation != ('search' if operation.domain=='places' else 'query')
                    or operation.values.keys()-fields):
                return None
            if operation.domain=='hotel':
                from core.hotel_contract import validate_request
                # Validate evidence once, after the executor binds owned task/message context.
                queries.append(IntentOperation('hotel','query',validate_request(operation.values)));missing.append(())
                continue
            inherited=context.slots
            source_index=index
            if operation.domain=='rail' and context.slots.get('queries'):
                baselines=context.slots['queries']
                scores=[sum(bool(operation.values.get(k)) and operation.values[k]==base.get(k) for k in ('origin','destination')) for base in baselines]
                best=max(scores)
                if not best or scores.count(best)!=1:
                    return None
                source_index=scores.index(best)
                inherited={**context.slots,**baselines[source_index]}
            pending_rows=context.slots.get('missing_fields',())
            pending=context.task.missing_slots if context.task else pending_rows[source_index] if source_index<len(pending_rows) else ()
            derived=DERIVED_READ_FIELDS.get(operation.domain,())
            needed=tuple(dict.fromkeys([name for name in ir.missing_fields if name in fields and name not in derived]+
                [name for name in pending if name in fields and name not in derived and not operation.values.get(name)]))
            values={}
            for field in fields:
                if field in needed:
                    continue
                if (operation.domain=='rail' and field.endswith('_scope') and field not in operation.values
                        and operation.values.get(field[:-6]) and operation.values[field[:-6]]!=inherited.get(field[:-6])):
                    continue
                if (field in derived and not (isinstance(operation.values.get(field),str) and operation.values[field]
                        and operation.values[field] in context.input_text) and any(
                        operation.values.get(key) and inherited.get(key) and operation.values[key].strip()!=inherited[key].strip()
                        for key in ('origin','destination'))):
                    continue
                value=context.read_value(field,operation.values.get(field,inherited.get(field,'')))
                if field in derived and isinstance(value,str) and value not in context.message and value!=inherited.get(field):
                    continue
                if (not isinstance(value,str) or len(value)>150
                        or (value and value not in context.message and value!=inherited.get(field)
                            and not (operation.domain=='rail' and field in RAIL_ENUMS and value in RAIL_ENUMS[field]))):
                    return None
                if value:
                    values[field]=value
            queries.append(IntentOperation(operation.domain,operation.operation,values))
            missing.append(needed)
        selected=[(query,needed) for query,needed in zip(queries,missing) if not (query.domain=='route'
            and query.values.get('origin') and query.values.get('destination')
            and any(other.domain=='traffic' and other.values==query.values for other in queries))]
        return cls(tuple(query for query,_ in selected),context,
                   missing_fields=tuple(needed for _,needed in selected))

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


@dataclass(frozen=True)
class TravelDecision:
    intent: str
    intents: tuple[str, ...]
    require_live_data: bool
    allowed_tools: tuple[str, ...]
    required_tool_groups: tuple[tuple[str, ...], ...]
    needs_clarification: bool
    response_detail: str
    action_resources: tuple[tuple[str, str], ...] = ()
    clarification_reply: str = ""


CONVERSATION_POLICY = TravelDecision("general", ("general",), False, ('query_hotel_prices',), (), False, "normal")
