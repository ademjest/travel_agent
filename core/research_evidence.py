"""Bounded checks for cited prices, times and reservation rules; not general entailment."""
import re

from core.research_contract import MAX_ITEMS, MAX_TEXT_CHARS

EXCERPT_CHARS = 4500
MAX_QUOTES = 4
MAX_QUOTE_CHARS = 500
VERSION = 'research-evidence-v1.1'
SOURCE_PREFIX = '来源原文：'
SCOPES = r'周[一二三四五六日天]|工作日|节假日|儿童|成人|学生|老人'


class ResearchEvidenceError(ValueError):
    def __init__(self, code, index):
        super().__init__(code)
        self.code, self.index = code, index


def normalized(text):
    return re.sub(r'\s+', '', text.replace('人民币', 'CNY').replace('美元', 'USD').replace('欧元', 'EUR')
                  .replace('RMB', 'CNY').replace('￥', 'CNY').replace('¥', 'CNY').replace('$', 'USD').replace('€', 'EUR')
                  .replace('：', ':').replace('票价', '门票').replace('房间费用', '住宿费用').replace('房费', '住宿费用'))


def reservation_modes(text):
    text = text.replace('不需要提前预约', '无需提前预约').replace('不需要预约', '无需预约').replace('免预约', '无需预约')
    modes = set()
    if '无需预约' in text:
        modes.add('none')
    if '无需提前预约' in text:
        modes.add('no_advance')
    if re.search(r'(?<!无)(?:必须|需要|需)(?:提前(?:\d+天)?)?预约', text):
        modes.add('required')
    return modes


def clock_facts(text):
    facts = []
    for clause in re.split(r'[，,。；;\n]', text):
        times = tuple(f'{int(hour):02}:{minute}' for hour, minute in re.findall(r'(\d{1,2})[:：](\d{2})', clause))
        for label, pattern in (('open', r'开放|开馆|开门'), ('close', r'闭馆|闭园|关门')):
            if times and re.search(pattern, clause):
                facts.append((label, times))
    return facts


def concrete_claim(text):
    policy_assertion = any(re.search(r'开放|闭馆|开馆|闭园|预约|免费|免票|收费|票价|门票|价格|费用', clause)
        and not re.search(r'未知|待核实|未核实|未提供|无法核实|尚待核对|需核对|需再次核对', clause)
        for clause in re.split(r'[，,。；;\n]', text))
    return bool(policy_assertion or reservation_modes(text) or re.search(
        r'\d+(?:\.\d+)?\s*(?:元|CNY|USD|EUR|天|分钟|小时)|\d{1,2}[:：]\d{2}'
        r'|[¥￥$€]\s*\d+'
        r'|\d{4}[-年]\d{1,2}[-月]\d{1,2}日?'
        r'|保证有票|一定开放|(?:已|已经).{0,6}(?:预约|订票|购票)', text))


def complete_quote(quote, text):
    for match in re.finditer(re.escape(quote), text[:EXCERPT_CHARS]):
        prefix, suffix = text[:match.start()], text[match.end():]
        left = not prefix.strip() or re.search(r'[。；;\n]\s*$', prefix)
        right = (not suffix.strip() or re.search(r'[。；;\n]$', quote)
                 or re.match(r'\s*[。；;\n]', suffix))
        if left and right:
            return True
    return False


def validate_research_items(items, sources):
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise ResearchEvidenceError('items_type', -1)
    available = {source['id']: source for source in sources}
    checked = []
    for index, item in enumerate(items):
        def reject(code):
            raise ResearchEvidenceError(code, index)

        if (not isinstance(item, dict) or item.keys() - {'text', 'sources', 'evidence'}
                or not isinstance(item.get('text'), str) or not 1 <= len(item['text'].strip()) <= MAX_TEXT_CHARS
                or not isinstance(item.get('sources'), list) or not item['sources']
                or any(type(key) is not int or key not in available for key in item['sources'])):
            reject('item_contract')
        evidence = item.get('evidence', [])
        if not isinstance(evidence, list) or len(evidence) > MAX_QUOTES:
            reject('evidence_contract')
        for ref in evidence:
            if (not isinstance(ref, dict) or set(ref) != {'source_id', 'quote'}
                    or type(ref['source_id']) is not int or ref['source_id'] not in item['sources']
                    or not isinstance(ref['quote'], str) or not 1 <= len(ref['quote']) <= MAX_QUOTE_CHARS
                    or not complete_quote(ref['quote'], available[ref['source_id']]['text'])):
                reject('quote_mismatch')
        if not concrete_claim(item['text']):
            checked.append(item)
            continue
        if re.search(r'(?:已|已经).{0,6}(?:预约|订票|购票)|保证有票|一定开放', item['text']):
            reject('unsupported_execution_or_guarantee')
        if not evidence or any(available[ref['source_id']]['status'] != 'read' for ref in evidence):
            reject('read_evidence_required')
        quotes = [ref['quote'] for ref in evidence]
        source = normalized('；'.join(quotes))
        target = normalized(item['text'])
        for phrase in re.findall(r'(?:上午|下午|晚上|早上|中午)?[零一二两三四五六七八九十]{1,3}点(?:半|[一二三四五六七八九十]+分)?', target):
            if phrase not in source:
                reject('time_wording_mismatch')
        fee_assertions = [clause for clause in re.split(r'[，,。；;\n]', target)
            if re.search(r'免费|免票',clause) and not re.fullmatch(
                r'(?:资料|来源)?未(?:说明|提供|明确)(?:优惠或)?(?:免费|免票)(?:政策|规则|信息)',clause)]
        if fee_assertions:
            if not any(re.search(r'免费|免票', part) and all(scope in target for scope in re.findall(SCOPES, part))
                       for quote in quotes for part in re.split(r'[，,。；;\n]', quote)):
                reject('fee_scope_mismatch')
        for quote in quotes:
            for condition in re.findall(r'(?:如果|只有|仅在|若)[^，,。；;\n]+', quote):
                if normalized(condition) not in target:
                    reject('condition_not_preserved')
        all_times = lambda value: {f'{int(hour):02}:{minute}' for hour, minute in re.findall(r'(\d{1,2}):(\d{2})', value)}
        if all_times(target) - all_times(source):
            reject('time_mismatch')
        for number in re.findall(r'\d+(?:\.\d+)?', re.sub(r'\d{1,2}:\d{2}', '', target)):
            if not re.search(rf'(?<![\d.]){re.escape(number)}(?![\d.])', source):
                reject('number_mismatch')
        for clause in re.split(r'[，,。；;\n]', target):
            qualifiers = re.findall(r'每人|每间|每天|每晚|CNY|USD|EUR|最多|至少|住宿|餐饮|门票|元|小时|分钟|天', clause)
            for number in re.findall(r'\d+(?:\.\d+)?', re.sub(r'\d{1,2}:\d{2}', '', clause)):
                if not any(all(value in part for value in qualifiers)
                        and all(scope in clause for scope in re.findall(SCOPES, part))
                        and re.search(rf'(?<![\d.]){re.escape(number)}(?![\d.])', part)
                        for quote in quotes for part in re.split(r'[，,。；;\n]', normalized(quote))):
                    reject('value_scope')
        modes = set().union(*(reservation_modes(quote) for quote in quotes))
        if reservation_modes(item['text']) - modes:
            reject('reservation_mismatch')
        clocks = [fact for quote in quotes for fact in clock_facts(quote)]
        if any(fact not in clocks for fact in clock_facts(item['text'])):
            reject('time_role_mismatch')
        # Finite checks do not prove a natural-language paraphrase. Publish the
        # actual policy quotation; carry every quotation into planning intact.
        checked.append({**item, 'text': SOURCE_PREFIX + evidence[0]['quote']})
    return checked
