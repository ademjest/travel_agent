"""Reviewed brand-network names; never infer a branch's ownership or room price."""
from functools import lru_cache
from itertools import zip_longest
from pathlib import Path
import json
import re
import unicodedata


def _name(value):
    return re.sub(r'[\W_]+','',unicodedata.normalize('NFKC',str(value))).casefold()


@lru_cache(maxsize=1)
def _groups():
    return json.loads(Path(__file__).with_name('hotel_brands.json').read_text(encoding='utf-8'))['groups']


def hotel_brand_targets(preferences):
    """Resolve alternative selectors, interleaving groups for bounded searches."""
    groups=_groups();blocks=[]
    for field in ('preferred_groups','preferred_brands'):
        for value in preferences.get(field,[]):
            group=next((g for g in groups if _name(value) in {_name(a) for a in g['aliases' if field=='preferred_groups' else 'legacy_aliases']}),None)
            if group:
                blocks.append([{**b,'group':group['name']} for b in group['brands']]);continue
            if field=='preferred_groups':continue  # An unknown group is not a hotel-name keyword.
            known=[b for g in groups for b in g['brands'] if _name(value) in {_name(b['name']),_name(b['name'].removesuffix('酒店'))}]
            blocks.append(known or [{'name':value,'source':None}])
    result=[];seen=set()
    for batch in zip_longest(*blocks):
        for row in batch:
            if row and _name(row['name']) not in seen:
                seen.add(_name(row['name']));result.append(row)
    return result


def match_hotel_brand(name,targets):
    name=_name(name)
    return next((row for row in sorted(targets,key=lambda r:len(_name(r['name'])),reverse=True) if _name(row['name']) in name),None)
