"""Shared budget bounds and comparison units."""
import re

BUDGET_UNITS = {
    'currency': r'人民币|CNY|RMB|美元|美金|USD|欧元|EUR|英镑|GBP|日元|JPY|港币|港元|HKD|澳元|AUD|加元|CAD|新加坡元|SGD|韩元|KRW|泰铢|THB|[￥¥$€£]|元',
    'basis': r'(?:每|[/／])(?:间房?|房间|房|人)|人均|总额|总价|总预算',
    'period': r'(?:每|[/／])(?:小时|晚|天|日|周|月|年)|全程',
}
HOTEL_BUDGET_DEFAULTS = {'currency':'人民币', 'basis':'每间', 'period':'每晚'}
CURRENCY_NAMES = {'CNY':'人民币','RMB':'人民币','元':'人民币','￥':'人民币','¥':'人民币',
                  'USD':'美元','美金':'美元','$':'美元','EUR':'欧元','€':'欧元','GBP':'英镑','£':'英镑'}


def budget_bounds(value,source=''):
    """Normalize evidence-backed inequalities; consumers only compare the canonical bounds."""
    text=str(value);amounts=[float(v) for v in re.findall(r'\d+(?:\.\d+)?',text)]
    if not 1<=len(amounts)<=2:raise ValueError('预算需要一个金额或明确上下区间。')
    clauses=[part for part in re.split(r'[，,。；;\n]',source) if text in part]
    if len(clauses)==1:text+=' '+clauses[0]
    approximate=bool(re.search(r'左右|大约|约|大概|差不多|上下',text))
    bounds={'lower':min(amounts) if len(amounts)==2 else None,'upper':max(amounts) if len(amounts)==2 else None,
        'lower_inclusive':True,'upper_inclusive':True,'kind':'approximate' if approximate else 'bounded'}
    currency='(?:'+BUDGET_UNITS['currency']+'|块)?'
    prefix=r'(?P<op>不(?:能|得)?(?:低于|少于|小于|高于|超过|多于|大于)|至少|最低|至多|最多|上限|下限|高于|大于|超过|多于|低于|小于|少于|不足|>=|<=|≥|≤|>|<)\s*'+currency+r'\s*(?P<n>\d+(?:\.\d+)?)'
    suffix=r'(?P<n>\d+(?:\.\d+)?)\s*'+currency+r'\s*(?P<op>以上|以下|以内|起)'
    adopted=set()
    for match in [*re.finditer(prefix,text,re.I),*re.finditer(suffix,text,re.I)]:
        amount=float(match['n'])
        if amount not in amounts:continue
        op=match['op'];negative=op.startswith('不') and op!='不足'
        lower=(bool(re.search(r'低于|少于|小于',op)) if negative else op in {'至少','最低','下限','>=','≥','以上','起'} or bool(re.fullmatch(r'高于|大于|超过|多于|>',op)))
        inclusive=negative or op in {'至少','最低','下限','至多','最多','上限','>=','<=','≥','≤','以上','以下','以内','起'}
        field='lower' if lower else 'upper';previous=bounds[field]
        if previous is None or (amount>previous if lower else amount<previous):
            bounds[field]=amount;bounds[field+'_inclusive']=inclusive
        elif amount==previous:bounds[field+'_inclusive'] &= inclusive
        adopted.add(field)
    if len(amounts)==1 and not adopted and not approximate:bounds['upper']=amounts[0]
    if bounds['lower'] is not None and bounds['upper'] is not None and (bounds['lower']>bounds['upper'] or
            bounds['lower']==bounds['upper'] and not (bounds['lower_inclusive'] and bounds['upper_inclusive'])):
        raise ValueError('预算上下限存在冲突，请明确有效金额区间。')
    if approximate:bounds['target']=amounts[0]
    return bounds


def hotel_comparison_unit(unit):
    currencies=[CURRENCY_NAMES.get(v.upper(),v) for v in re.findall(BUDGET_UNITS['currency'],unit,re.I)]
    if not currencies or any(v!='人民币' for v in currencies):return 'incomparable'
    if not re.search(r'每(?:间房?|房间)|[/／](?:间房?|房间)',unit) or not re.search(r'每晚|[/／]晚',unit):return 'incomparable'
    rest=re.sub(BUDGET_UNITS['currency'],'',unit,flags=re.I)
    rest=re.sub(r'每(?:间房?|房间)|[/／](?:间房?|房间)|每晚|[/／]晚|房价|参考|预算|[\s/／（）()]','',rest)
    return 'incomparable' if rest else 'per_room_night_cny'

