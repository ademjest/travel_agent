"""Provider/model-scoped price snapshots with decimal currency arithmetic."""
from decimal import Decimal, InvalidOperation
import json
import os
import re


def decimal_price(value):
    if value is None or value == '':
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('费率必须为有限非负数字。') from exc
    if not number.is_finite() or not 0 <= number <= Decimal('1000000000'):
        raise ValueError('费率必须为有限非负数字。')
    return number


def parse_profiles(raw):
    value = json.loads(raw or '[]')
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError('模型费率必须是最多20项的JSON数组。')
    seen = set()
    for item in value:
        if not isinstance(item, dict) or set(item) - {'provider','model','input','cached','output','currency','version'}:
            raise ValueError('模型费率字段无效。')
        if not isinstance(item.get('model'), str) or not item['model'] or len(item['model']) > 200:
            raise ValueError('模型费率缺少模型名称。')
        if not isinstance(item.get('provider',''), str) or not isinstance(item.get('version',''), str):
            raise ValueError('供应商与版本必须是文字。')
        if not isinstance(item.get('currency'), str) or not re.fullmatch('[A-Z]{3}',item['currency']):
            raise ValueError('费率币种必须为三位大写代码。')
        key = item.get('provider',''),item['model']
        if key in seen:
            raise ValueError('同供应商模型费率不能重复。')
        seen.add(key)
        for name in ('input','cached','output'):
            decimal_price(item.get(name))
    return value


def pricing_config(model=None, default_model=None):
    provider = os.getenv('LLM_PROVIDER_ALIAS','').strip()
    for profile in parse_profiles(os.getenv('LLM_MODEL_PRICES','')):
        if profile['model'] == model and profile.get('provider','') == provider:
            return {'input':profile.get('input'), 'cached':profile.get('cached',profile.get('input')),
                    'output':profile.get('output'), 'currency':profile['currency'],
                    'version':profile.get('version',''), 'provider':provider}
    known_default = default_model or os.getenv('LLM_MODEL_ID','').strip()
    allowed = not model or not known_default or model == known_default
    def rate(key):
        return float(decimal_price(os.getenv(key,''))) if allowed and os.getenv(key,'').strip() else None
    normal = rate('LLM_INPUT_PRICE_PER_M')
    cached = rate('LLM_CACHED_INPUT_PRICE_PER_M')
    return {'input':normal, 'cached':normal if cached is None else cached,
            'output':rate('LLM_OUTPUT_PRICE_PER_M'),
            'currency':os.getenv('LLM_PRICE_CURRENCY','').strip().upper() or 'USD',
            'version':os.getenv('LLM_CONFIG_VERSION','').strip(), 'provider':provider}


def estimate_cost(prompt, cached, completion, pricing):
    if (type(prompt) is not int or type(completion) is not int or prompt < 0 or completion < 0
            or (cached is not None and (type(cached) is not int or cached < 0 or cached > prompt))):
        return None
    input_price, output_price = decimal_price(pricing.get('input')), decimal_price(pricing.get('output'))
    if input_price is None or output_price is None:
        return None
    cached_price = decimal_price(pricing.get('cached'))
    if cached_price is None:
        cached_price = input_price
    count = cached if cached is not None else 0
    return float(((prompt-count)*input_price+count*cached_price+completion*output_price)/Decimal(1000000))
