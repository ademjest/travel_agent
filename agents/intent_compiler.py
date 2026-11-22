"""LLM semantic compiler. It proposes Intent IR; it never authorizes a write."""
from infrastructure.skill_manager import skill_text, skill_request
import json
import logging

from core.intent_ir import IntentIR, parse_intent_ir
from core.intercity_contract import IntercityContractError
from core.rail_contract import SEAT_CLASSES, SEAT_REQUIREMENTS, PRICE_PREFERENCES
from core.preference_contract import preference_fields
from core.execution_scope import ExecutionRevoked
from core.model_budget import model_call_context,ModelBudgetExceeded
from core.model_policy import model_for_purpose
from core.context_contract import complete_json, response_text
from core.research_contract import ResearchRequestError, SCHEMA_VERSION, REQUEST_FIELDS, TOPICS
from infrastructure.execution_diagnostics import SEARCH_TRACE
from infrastructure.model_client import model_failure_metadata

logger = logging.getLogger(__name__)


class IntentCompilationError(RuntimeError):
    def __init__(self,code,detail='',*,budget_code='',budget_details=None):
        super().__init__(code)
        self.code=code
        self.detail=detail
        self.budget_code=budget_code
        self.budget_details=budget_details or {}

def _intent_prompt():
    prompt = skill_text('intent.compile', 'protocol')
    prompt += skill_text('intent.compile', 'protocol_2')
    prompt += '\n' + json.dumps({
        'seat_class':SEAT_CLASSES,'seat_requirement':sorted(SEAT_REQUIREMENTS),
        'price_preference':sorted(PRICE_PREFERENCES)},ensure_ascii=False)
    prompt += skill_text('intent.compile', 'protocol_3')
    prompt += skill_text('intent.compile', 'protocol_4')
    prompt += (skill_text('intent.compile', 'protocol_5') + json.dumps(preference_fields(),ensure_ascii=False))
    prompt += skill_text('intent.compile', 'protocol_6')
    prompt += skill_text('intent.compile', 'protocol_7')
    return prompt


class IntentCompiler:
    def __init__(self, client, model, diagnostics=None):
        self.client, self.model = client, model
        self.diagnostics = diagnostics

    @skill_request
    def compile(self, text: str, *, context: str = '', force_default_model: bool = False,
                research_repair: str = '', intent_repair: str = '') -> IntentIR:
        payload={'message':text,'context':context}
        if intent_repair:
            payload['contract_error']=intent_repair
        with model_call_context('intent_parse', prompt_version='intent-ir-v30-hotel-flow-state',
                                context_policy_version='intent-context-v4-hotel-state',
                                repair_attempt=int(bool(research_repair or intent_repair)),
                                fallback_from=(model_for_purpose(self.model, 'intent_parse')
                                               if force_default_model else ''),
                                force_default_model=force_default_model, stage_id='intent.compile'):
            response = self.client.chat.completions.create(model=self.model, response_format={'type': 'json_object'}, messages=[
                {'role': 'system', 'content': (_intent_prompt() + ((((((((skill_text('intent.compile', 'system') + research_repair) + skill_text('intent.compile', 'system_2')) + SCHEMA_VERSION) + skill_text('intent.compile', 'system_3')) + ','.join(sorted(REQUEST_FIELDS))) + skill_text('intent.compile', 'system_4')) + ','.join(TOPICS)) if research_repair else ''))},
                {'role': 'user', 'content': complete_json(payload, 22000,purpose='intent')},
            ])
        raw = response_text(response)
        if SEARCH_TRACE.get() is not None:
            SEARCH_TRACE.get().record('intent-results',{'raw':raw,'repair':bool(research_repair)})
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError('语义解析结果不是有效 JSON。') from exc
        try:
            return parse_intent_ir(value, text)
        except ResearchRequestError as error:
            if research_repair or intent_repair:
                raise
            if self.diagnostics:
                self.diagnostics.record('routing',{'status':'research_repair','schema_version':SCHEMA_VERSION,
                    'reason':error.code,'repair_attempt':1})
            try:
                return self.compile(text, context=context, force_default_model=force_default_model,
                                    research_repair=error.code)
            except ExecutionRevoked:
                raise
            except ModelBudgetExceeded:
                raise
            except Exception as exc:
                raise ResearchRequestError('repair_failed') from exc
        except ValueError as error:
            validation=({'validation':{'code':error.code,'path':error.path}} if isinstance(error,IntercityContractError) else {})
            if research_repair or intent_repair:
                if validation and self.diagnostics:
                    self.diagnostics.record('routing',{'status':'intent_failed','error_code':'invalid_response',
                        'repair_attempt':1,**validation})
                raise
            if self.diagnostics:
                self.diagnostics.record('routing',{'status':'intent_repair','reason':'invalid_contract','repair_attempt':1,**validation})
            return self.compile(text,context=context,force_default_model=force_default_model,
                intent_repair=str(error))

    def try_compile(self, text: str, *, context: str = '') -> IntentIR | None:
        try:
            return self.compile(text, context=context)
        except ExecutionRevoked:
            raise
        except ModelBudgetExceeded as exc:
            projection=exc.details.get('code')=='input_projection_limit'
            if projection and self.diagnostics:self.diagnostics.record('routing',{'status':'intent_failed','error_code':'context_too_large',
                'validation':exc.details,'input_parts':getattr(exc,'input_parts',{})})
            raise IntentCompilationError('context_too_large' if projection else 'budget_exceeded',str(exc),
                budget_code=exc.code,budget_details=exc.details) from exc
        except IntercityContractError as exc:
            raise IntentCompilationError('invalid_response',exc.detail+'，一次自动修复仍未通过。本次没有查询车票或修改行程，完整请求已保留。') from exc
        except ResearchRequestError as exc:
            failure=model_failure_metadata(exc)
            if failure:raise IntentCompilationError(failure['error_code']) from exc
            logger.warning('Research intent repair failed: code=%s', exc.code)
            if self.diagnostics:
                self.diagnostics.record('routing',{'status':'research_rejected','reason':exc.code,
                    'schema_version':SCHEMA_VERSION,'repair_attempt':1})
            raise IntentCompilationError('invalid_response') from exc
        except Exception as exc:
            failure=model_failure_metadata(exc)
            if failure:
                logger.warning('Intent model unavailable: code=%s error_type=%s',failure['error_code'],type(exc).__name__)
                raise IntentCompilationError(failure['error_code']) from exc
            logger.warning('Intent compiler rejected result: error_type=%s', type(exc).__name__)
            if not isinstance(exc,ValueError):raise IntentCompilationError('internal_error') from exc
            configuration=getattr(self.client,'configuration',None)
            if (configuration is None or configuration.legacy) and model_for_purpose(self.model, 'intent_parse') != self.model:
                try:
                    return self.compile(text, context=context, force_default_model=True)
                except ExecutionRevoked:
                    raise
                except ModelBudgetExceeded as fallback_exc:
                    raise IntentCompilationError('budget_exceeded',str(fallback_exc),budget_code=fallback_exc.code,
                        budget_details=fallback_exc.details) from fallback_exc
                except Exception as fallback_exc:
                    failure=model_failure_metadata(fallback_exc)
                    if failure:raise IntentCompilationError(failure['error_code']) from fallback_exc
                    logger.warning('Intent compiler fallback rejected result: error_type=%s',type(fallback_exc).__name__)
                    if not isinstance(fallback_exc,ValueError):raise IntentCompilationError('internal_error') from fallback_exc
            raise IntentCompilationError('invalid_response') from exc
