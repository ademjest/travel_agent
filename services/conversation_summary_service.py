"""On-demand summaries use complete batches and individually grounded source entries."""
import json
import re
import logging

from core.model_budget import model_call_context
from core.model_policy import current_policy, model_for_purpose
from core.execution_scope import ExecutionRevoked
from core.context_contract import response_json
from core.summary_contract import (MAX_ITEMS, MAX_TEXT_CHARS, MAX_SOURCES, MAX_QUOTE_CHARS,
                                   KINDS, PROMPT_VERSION, SummaryValidationError, load_summary_prompt)
from core.summary_semantics import check_semantics, SEMANTICS_VERSION
from infrastructure.model_gateway import MAX_INPUT_ESTIMATE, estimate_input

MIN_NEW_TURNS = 8
MIN_NEW_CHARS = 9000
MAX_SUMMARY_INPUT_CHARS = 24000
MAX_SUMMARY_CHARS = MAX_TEXT_CHARS
logger=logging.getLogger(__name__)


def excluded_turn(text):
    return bool(re.search(r'记住|忘记.{0,15}偏好|长期偏好|以后.{0,15}默认|删除.{0,15}(?:方案|资料|行程)|(?:TR-|P-|R-)[a-f0-9]|权限|密钥|API.?KEY', text, re.I))


def supported_quote(quote, originals):
    for original in originals:
        for match in re.finditer(re.escape(quote), original):
            # A continuous substring can still drop the immediately preceding
            # negation. Require that boundary to be present in the quotation.
            if not re.search(r'(?:不|未|无|不是|而非|并非|禁止|不要|不能|取消|拒绝)$', original[:match.start()]):
                return True
    return False


def select_batch(turns, previous, max_input_bytes=54000):
    old_items = json.loads(previous['items_json']) if previous else []
    old_items = [{**item, 'sources': [{'message_id': ref['message_id'], 'quote': ref['quote']}
                                    for ref in item['sources']]} for item in old_items]
    progress = json.loads(previous['progress_json']) if previous else {}
    payload = {'previous_items': old_items, 'new_turns': []}
    originals = []
    covered = previous['covered_turn_id'] if previous else 0
    next_progress = {}
    for turn in turns:
        offset = progress.get('offset', 0) if progress.get('turn_id') == turn['id'] else 0
        text = turn['user_content']
        if turn.get('summary_excluded') or excluded_turn(text):
            originals.append(turn)
            covered = turn['id']
            continue
        while offset < len(text):
            fragment = text[offset:offset + 6000]
            entry = {'message_id': turn['id'], 'offset': offset, 'user': fragment}
            trial = {**payload, 'new_turns': [*payload['new_turns'], entry]}
            serialized = json.dumps(trial, ensure_ascii=False)
            if len(serialized) > MAX_SUMMARY_INPUT_CHARS or len(serialized.encode('utf-8')) > max_input_bytes:
                next_progress={'turn_id':turn['id'],'offset':offset}
                return payload, originals, covered, next_progress
            payload = trial
            if turn not in originals:
                originals.append(turn)
            offset += len(fragment)
            if offset < len(text):
                next_progress = {'turn_id': turn['id'], 'offset': offset}
            else:
                covered = turn['id']
                next_progress = {}
        if not text:
            originals.append(turn)
            covered = turn['id']
    return payload, originals, covered, next_progress


def validate_items(value, payload):
    if not isinstance(value, dict) or set(value) != {'items'} or not isinstance(value.get('items'), list):
        raise SummaryValidationError('items_type', 'items')
    items = value['items']
    if len(items) > MAX_ITEMS:
        raise SummaryValidationError('items_count', 'items', actual=len(items), max=MAX_ITEMS)
    available = {}
    for entry in payload['new_turns']:
        # The id alias is read-only compatibility for captured v3 responses.
        available.setdefault(entry.get('message_id', entry.get('id')), []).append(entry['user'])
    for item in payload['previous_items']:
        for ref in item['sources']:
            available.setdefault(ref['message_id'], []).append(ref['quote'])
    total = 0
    for index, item in enumerate(items):
        path = f'items[{index}]'
        if (not isinstance(item, dict) or set(item) != {'kind', 'text', 'sources'}
                or not isinstance(item.get('kind'), str) or item['kind'] not in KINDS
                or not isinstance(item.get('text'), str) or not item['text'].strip()
                or not isinstance(item.get('sources'), list)):
            raise SummaryValidationError('item_type', path)
        if not 1 <= len(item['sources']) <= MAX_SOURCES:
            raise SummaryValidationError('sources_count', path + '.sources',
                                         actual=len(item['sources']), max=MAX_SOURCES)
        total += len(item['text'])
        if excluded_turn(item['text']):
            raise SummaryValidationError('excluded_content', path + '.text')
        for ref_index, ref in enumerate(item['sources']):
            if (not isinstance(ref, dict) or set(ref) != {'message_id', 'quote'} or type(ref.get('message_id')) is not int
                    or not isinstance(ref.get('quote'), str) or not 1 <= len(ref['quote']) <= MAX_QUOTE_CHARS
                    or not supported_quote(ref['quote'], available.get(ref['message_id'], []))):
                raise SummaryValidationError('source_mismatch', f'{path}.sources[{ref_index}]')
        check_semantics(item['text'], [ref['quote'] for ref in item['sources']], path + '.text')
    if total > MAX_SUMMARY_CHARS:
        raise SummaryValidationError('text_chars', 'items', actual=total, max=MAX_SUMMARY_CHARS)
    return items


class ConversationSummaryService:
    def __init__(self, repository, client=None, model='', diagnostics=None, repair_permit=None):
        self.repository, self.client, self.model = repository, client, model
        self.diagnostics=diagnostics
        # Optional caller-owned physical-call/token reservation. No implicit retry.
        self.repair_permit = repair_permit

    def _report(self,event,status,record=None,**details):
        if status in {'model_failed', 'output_truncated', 'decode_failed', 'validation_failed', 'publish_rejected', 'prompt_failed'}:
            logger.warning('Summary degraded: status=%s version=%s code=%s path=%s previous_reused=%s',
                           status, record['version'] if record else None,
                           details.get('validation', {}).get('code', ''),
                           details.get('validation', {}).get('path', ''), details.get('previous_reused', False))
        if self.diagnostics is not None:
            value={'event_key':event.event_key,'status':status,'prompt_version':PROMPT_VERSION,
                   'validator_version':SEMANTICS_VERSION,
                   'model_id':model_for_purpose(self.model,'conversation_summary'),
                   'version':record['version'] if record else None,
                   'covered_turn_id':record['covered_turn_id'] if record else None,**details}
            try:
                self.diagnostics(value)
            except Exception as error:
                logger.warning('Summary diagnostic callback failed: %s',type(error).__name__)
        return record

    def ensure(self, event, needed=True):
        if not current_policy().summary_enabled:
            return self._report(event,'disabled')
        if not needed:
            return self._report(event,'not_needed')
        previous = self.repository.latest(event)
        turns = self.repository.pending_turns(event, previous['covered_turn_id'] if previous else 0)
        in_progress = bool(previous and json.loads(previous['progress_json']))
        if self.client is None:
            return self._report(event,'client_unavailable',previous)
        if not in_progress and len(turns) < MIN_NEW_TURNS:
            return self._report(event,'threshold_not_met',previous,reason='turns',pending_turns=len(turns))
        if not in_progress and sum(len(t['user_content'])+len(t['assistant_content']) for t in turns) < MIN_NEW_CHARS:
            return self._report(event,'threshold_not_met',previous,reason='chars',pending_turns=len(turns),
                                pending_chars=sum(len(t['user_content'])+len(t['assistant_content']) for t in turns))
        try:
            prompt, prompt_hash = load_summary_prompt()
        except (OSError, ValueError) as error:
            return self._report(event, 'prompt_failed', previous, error_type=type(error).__name__)
        overhead = estimate_input([{'role': 'system', 'content': prompt}, {'role': 'user', 'content': ''}])
        payload, selected, covered, progress = select_batch(turns, previous, MAX_INPUT_ESTIMATE - overhead - 512)
        if not payload['new_turns']:
            return self._report(event,'no_eligible_source',previous)
        claim = self.repository.claim(event, previous)
        if claim is None:
            return self._report(event,'cooldown_or_lease_busy',previous)
        failed = True
        stage='model_failed'
        repair_attempt = 0
        try:
            with model_call_context('conversation_summary', prompt_version=PROMPT_VERSION,
                                    context_policy_version='summary-context-v4'):
                response = self.client.chat.completions.create(
                    model=self.model, response_format={'type': 'json_object'}, messages=[
                        {'role': 'system', 'content': prompt},
                        {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
                    ])
            stage='output_truncated' if getattr(response.choices[0],'finish_reason',None) in {'length','content_filter'} else 'decode_failed'
            try:
                value=response_json(response)
                stage='validation_failed'
                items = validate_items(value, payload)
            except (json.JSONDecodeError, SummaryValidationError) as error:
                structural = isinstance(error, json.JSONDecodeError) or error.details['code'] in {
                    'items_type', 'items_count', 'item_type', 'sources_count', 'text_chars'}
                if not structural or self.repair_permit is None:
                    raise
                request = {'model': self.model, 'response_format': {'type': 'json_object'}, 'messages': [
                    {'role': 'system', 'content': prompt},
                    {'role': 'user', 'content': json.dumps({**payload,
                        'invalid_output': response.choices[0].message.content,
                        'error': getattr(error, 'details', {'code': 'invalid_json'}),
                        'task': '只修复结构，不新增事实；无法依据原文修复时不得猜测。'}, ensure_ascii=False)}]}
                if estimate_input(request['messages']) > MAX_INPUT_ESTIMATE or not self.repair_permit(request):
                    raise
                repair_attempt = 1
                stage = 'model_failed'
                with model_call_context('conversation_summary', prompt_version=PROMPT_VERSION,
                                        context_policy_version='summary-context-v4', repair_attempt=1):
                    response = self.client.chat.completions.create(**request)
                stage = 'output_truncated' if getattr(response.choices[0], 'finish_reason', None) in {'length', 'content_filter'} else 'decode_failed'
                value = response_json(response)
                stage = 'validation_failed'
                items = validate_items(value, payload)
            stage='publish_rejected'
            record = self.repository.publish(event, claim, selected, items, covered, progress,
                                             model_for_purpose(self.model, 'conversation_summary'),
                                             prompt_version=PROMPT_VERSION, prompt_hash=prompt_hash)
            failed = record is None
            resolved=record or self.repository.latest(event)
            return self._report(event,'published' if record else 'publish_rejected',
                                resolved,item_count=len(items),prompt_hash=prompt_hash,repair_attempt=repair_attempt,
                                input_user_chars=sum(len(entry['user']) for entry in payload['new_turns']),
                                progress=progress,previous_reused=bool(record is None and previous and resolved
                                                                      and resolved['version']==previous['version']))
        except ExecutionRevoked:
            raise
        except Exception as error:
            fallback=self.repository.latest(event)
            return self._report(event,stage,fallback,error_type=type(error).__name__,
                                validation=getattr(error, 'details', {}), repair_attempt=repair_attempt,
                                previous_reused=bool(previous and fallback and fallback['version']==previous['version']))
        finally:
            self.repository.release(event, claim, failed=failed)
