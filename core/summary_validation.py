"""Summary schema, finite evidence checks and rendering; not general entailment validation."""
import hashlib
import re
import unicodedata
from pathlib import Path

MAX_ITEMS = 12
MAX_TEXT_CHARS = 1800
MAX_SOURCES = 4
MAX_QUOTE_CHARS = 500
KINDS = frozenset({'decision', 'correction', 'unresolved', 'reference'})
PROMPT_VERSION = 'conversation-summary-v4'
PROMPT_PATH = Path(__file__).resolve().parents[1] / 'prompts/conversation_summary.md'
OUTPUT_SCHEMA = {
    'type': 'object', 'required': ['items'], 'additionalProperties': False,
    'properties': {'items': {'type': 'array', 'maxItems': MAX_ITEMS, 'items': {
        'type': 'object', 'required': ['kind', 'text', 'sources'], 'additionalProperties': False,
        'properties': {'kind': {'type': 'string', 'enum': sorted(KINDS)},
                       'text': {'type': 'string', 'minLength': 1, 'maxLength': MAX_TEXT_CHARS},
                       'sources': {'type': 'array', 'minItems': 1, 'maxItems': MAX_SOURCES, 'items': {
                           'type': 'object', 'required': ['message_id', 'quote'], 'additionalProperties': False,
                           'properties': {'message_id': {'type': 'integer'},
                                          'quote': {'type': 'string', 'minLength': 1, 'maxLength': MAX_QUOTE_CHARS}}}}}}}},
}


class SummaryValidationError(ValueError):
    def __init__(self, code, path, **limits):
        self.details = {'code': code, 'path': path, **limits}
        super().__init__(f'{code}: {path}')


def load_summary_prompt():
    # Fail closed on accidental contract drift, without modifying the reviewed text.
    from infrastructure.skill_manager import skill_text
    prompt = skill_text('memory.summary', 'system')
    required = (f'最多{MAX_ITEMS}项', f'最多{MAX_TEXT_CHARS}字符',
                f'1—{MAX_SOURCES}条来源', f'1—{MAX_QUOTE_CHARS}字符', *KINDS)
    if not all(fragment in prompt for fragment in required):
        raise ValueError('摘要提示词与共享契约不一致。')
    return prompt, hashlib.sha256(prompt.encode()).hexdigest()


SEMANTICS_VERSION = 'summary-semantics-v4.4'
NEGATION = r'不是|不安排|不需要|不借用|不去|不允许|不同意|不参加|不购买|不预订|不使用|不进入|不参观|禁止|不要|不能|取消|否定|避免|拒绝'
UNITS = r'每人|每间|每天|每晚|全程|总额|CNY|USD|EUR'
NUMBER = r'\d+(?:\.\d+)?'


def normalize(text):
    text = unicodedata.normalize('NFKC', text)
    for source, target in [('人民币', 'CNY'), ('美元', 'USD'), ('欧元', 'EUR'),
                           ('/人/天', '每人每天'), ('/间/晚', '每间每晚'),
                           ('每个房间', '每间'), ('每个晚上', '每晚'),
                           ('而非', '不是'), ('并非', '不是'),
                           ('不超过', '最多'), ('不低于', '至少')]:
        text = text.replace(source, target)
    digits = dict(zip('零一二三四五六七八九两', '01234567892'))

    def chinese_number(match):
        value = match[0]
        if '十' in value:
            left, right = value.split('十')
            return str(int(digits.get(left, '1')) * 10 + int(digits.get(right, '0')))
        return digits[value]

    text = re.sub(r'[一二三四五六七八九两]?十[一二三四五六七八九]?|[零一二三四五六七八九两](?=个?月|人|间|天|晚|分钟|小时|元)', chinese_number, text)
    return re.sub(r'\s+', '', text)


def clauses(text):
    return [part for part in re.split(r'[，,。；;\n]', normalize(text)) if part]


def negative_targets(text):
    return [(match[1], match[2]) for match in re.finditer(
        rf'({NEGATION})([^，,。；;：:\n]+)', normalize(text))]


def compatible_target(left, right):
    left = re.sub(r'需要|的|按|计算|计价', '', left)
    right = re.sub(r'需要|的|按|计算|计价', '', right)
    return left in right or right in left


def check_semantics(text, quotes, path):
    normalized = normalize(text)
    source = normalize('；'.join(quotes))
    source_clauses = [clause for quote in quotes for clause in clauses(quote)]

    def reject(code):
        raise SummaryValidationError(code, path)

    # Pair negation with its object rather than comparing a global boolean.
    target_negatives = negative_targets(text)
    source_negatives = negative_targets('；'.join(quotes))
    proposal = r'(?:名称|名字|决定|选择)'
    disclaimed_proposal = any(re.fullmatch(
        rf'(?:你|助手)(?:刚才)?(?:提到|提出|给出)的{proposal}(?:和|或|及){proposal}'
        r'(?:都)?不是(?:我|用户)的(?:决定|选择|确认)', clause) for clause in source_clauses)
    if disclaimed_proposal:
        def ownership(value):
            value = re.sub(r'我的(决定|选择|确认)', r'用户的\1', value)
            return re.sub(r'用户本人的(决定|选择|确认)', r'用户的\1', value)
        normalized = ownership(normalized)
        target_negatives = [(marker, ownership(target)) for marker, target in target_negatives]
        source_negatives = [(marker, ownership(target)) for marker, target in source_negatives]
    for predicate, target in target_negatives:
        # Only generic proposal ownership is equivalent here, not domain facts,
        # permissions or execution status. Other clauses still pass every guard.
        if (disclaimed_proposal and predicate in {'不要', '不能'} and re.fullmatch(
                rf'(?:将|把)未经(?:用户|我)(?:明确)?(?:表达|确认)的{proposal}(?:或|和|及){proposal}'
                r'(?:归于(?:用户|我)|当作(?:用户|我)的(?:决定|选择))', target)):
            continue
        if not any(compatible_target(target, origin) and
                   (predicate == marker or predicate not in {'不需要', '不借用', '不是'}
                    and marker not in {'不需要', '不借用', '不是'})
                   for marker, origin in source_negatives):
            reject('negation_target')
    for _, target in source_negatives:
        if target in normalized and not any(compatible_target(target, value) for _, value in target_negatives):
            reject('negation_removed')

    for token in re.findall(rf'{NUMBER}|{UNITS}', normalized):
        pattern = rf'(?<![\d.]){re.escape(token)}(?![\d.])' if token[0].isdigit() else re.escape(token)
        if not re.search(pattern, source):
            reject('number_currency_unit')

    # A value and its denomination/comparison must occur together in a source
    # clause, not merely somewhere among the quotations for a different value.
    for clause in clauses(text):
        for number in re.findall(NUMBER, clause):
            candidates = [part for part in source_clauses
                          if re.search(rf'(?<![\d.]){re.escape(number)}(?![\d.])', part)]
            qualifiers = re.findall(rf'{UNITS}|最多|至少', clause)
            qualifiers += [scope for scope in re.findall(r'住宿|餐饮|交通|门票', clause) if scope in source]
            candidates = [part for part in candidates if all(value in part for value in qualifiers)]
            if qualifiers and not candidates:
                reject('value_scope')
            # Explicit person+walking-limit pairs are guarded without a name list.
            subject = re.match(r'([^:：]{1,8}?)(?:的)?(?:连续(?:行走|步行)|预算)', clause)
            contextual = subject and re.search(r'^(?:本次|此次|当前)|(?:要求|限制|规则|安排)$', subject[1])
            if subject and not contextual and subject[1] in source and not any(subject[1] in part for part in candidates):
                reject('subject_value')

    # Do not turn uncertainty, conditions or intentions into completed decisions.
    if re.search(r'尚未决定|还没决定|未确定|待决定', source):
        decision_text = normalized
        # An explicit current negative decision can supersede earlier uncertainty.
        # Match the same subject and action; this never establishes completion.
        for origin in source_clauses:
            match = re.fullmatch(r'([^:：]{1,16}?)(?:现在|现已|已经|已)决定不(借用|购买|预订|参加)', origin)
            if match and not re.search(r'如果|可能|希望|尚未|还没|不曾|并未|没有', match[1]):
                decision_text = re.sub(re.escape(match[1]) + r'(?:现)?已(?:经)?决定不' + match[2],
                                       match[1] + '决定不' + match[2], decision_text)
        if re.search(r'已(?:经)?(?:决定|借用|购买|预订|完成)|(?<!未)决定(?:借用|购买|预订|参加)', decision_text):
            if not re.search(r'已(?:经)?(?:决定|借用|购买|预订|完成)|(?<!未)决定(?:借用|购买|预订|参加)', source):
                reject('unresolved_to_decided')
    if '如果' in source and '才' in source and '如果' not in normalized:
        reject('condition_removed')
    completed = r'已(?:经)?(?:完成|购买|借用|预订|参加)'
    if re.search(completed, normalized) and not re.search(completed, source):
        reject('unsupported_completion')


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
        if contains_credentials(item['text']):
            raise SummaryValidationError('excluded_content', path + '.text')
        for ref_index, ref in enumerate(item['sources']):
            if (not isinstance(ref, dict) or set(ref) != {'message_id', 'quote'} or type(ref.get('message_id')) is not int
                    or not isinstance(ref.get('quote'), str) or not 1 <= len(ref['quote']) <= MAX_QUOTE_CHARS
                    or not supported_quote(ref['quote'], available.get(ref['message_id'], []))):
                raise SummaryValidationError('source_mismatch', f'{path}.sources[{ref_index}]')
        check_semantics(item['text'], [ref['quote'] for ref in item['sources']], path + '.text')
    if total > MAX_TEXT_CHARS:
        raise SummaryValidationError('text_chars', 'items', actual=total, max=MAX_TEXT_CHARS)
    return items


def render_summary(items,max_chars=None):
    lines=[]
    used=0
    for item in items:
        line=item['text']+' '+''.join(f"[{ref['message_id']}]" for ref in item['sources'])
        size=len(line)+(1 if lines else 0)
        if max_chars is not None and used+size>max_chars:
            break
        lines.append(line)
        used+=size
    return '\n'.join(lines)



def contains_credentials(text):
    return bool(re.search(r'(?:API.?KEY|密钥|password|token)\s*[:=： ]+\S+|sk-[A-Za-z0-9]{12,}', text, re.I))


def excluded_source(turn):
    if 'summary_eligible' in turn:
        return not turn['summary_eligible']
    if turn.get('summary_excluded') or turn.get('source_kind') == 'excluded':
        return True
    if turn.get('source_kind') == 'conversation':
        return contains_credentials(turn['user_content'])
    return excluded_turn(turn['user_content'])
