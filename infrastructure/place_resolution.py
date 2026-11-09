"""Resolve named route endpoints without silently downgrading their entity or precision."""
import math
import re
import logging
from difflib import SequenceMatcher
from contextvars import ContextVar


PLACE_RESOLUTION_VERSION='route-poi-v5-city'
PLACE_CONTEXT=ContextVar('route_place_context',default=None)
POINT_PATTERN=r'大学|学院|学校|医院|小区|公司|中心|大厦|楼|路\d|街\d|\d+号|门|入口|园|机场|站|景区'
logger=logging.getLogger(__name__)


def normalized(value):
    text=re.sub(r'[\s（）()·，,。;；\-—–－]','',str(value or '')).lower()
    text=re.sub(r'(大学|学院|学校|医院|园区|小区|区|县|市)的',r'\1',text)
    return re.sub(r'(?<=门)(?:校门|出入口|入口)$','',text)


def city_name(value):
    return normalized(value).removesuffix('市')


def bind_visit_entities(names,places,city=''):
    """Bind visit names to POIs using identity evidence, never arbitrary substring aliases."""
    import json
    from core.data_paths import PROJECT_ROOT
    catalog=json.loads((PROJECT_ROOT/'data/attraction_aliases.json').read_text(encoding='utf-8'))
    result={}
    for requested in dict.fromkeys(names):
        matches=[]
        for pid,poi in places.items():
            local=poi.get('cityname') or poi.get('city') or ''
            if city and local and city_name(city)!=city_name(local):continue
            query,name=normalized(requested),normalized(poi.get('name',''))
            kind=str(poi.get('type',''))
            if re.search(r'交通设施|公交|地铁站|停车|自然地名|行政地名',kind):continue
            if re.search(r'入口|出口|售票|[东南西北正]门',name) and not re.search(r'入口|出口|售票|[东南西北正]门',query):continue
            basis='exact_name' if query==name and query else ''
            if not basis and city and local and city_name(city)==city_name(local):
                address=normalized(poi.get('address',''))
                if address and query in {name+address,address+name}:basis='city_address_name'
                for entry in catalog:
                    labels=[normalized(v) for v in [entry['name'],*entry.get('aliases',[])]]
                    if (city_name(entry['city'])==city_name(city) and query in labels and name in labels
                            and any(normalized(a) in normalized(poi.get('address','')) for a in entry.get('address_markers',[]))):
                        basis='reviewed_city_address_alias';break
                if not basis and re.search(r'风景|公园|博物馆|美术馆|景点|文物|寺庙',kind):
                    def core(text):
                        for prefix in sorted({normalized(local),city_name(local)},key=len,reverse=True):
                            if prefix and text.startswith(prefix):text=text[len(prefix):];break
                        return text.removesuffix('景区')
                    if len(core(query))>=3 and core(query)==core(name):basis='city_category_name'
            if basis:matches.append(({'exact_name':0,'city_address_name':1,'reviewed_city_address_alias':1,'city_category_name':2}[basis],pid,basis))
        best=[row for row in matches if row[0]==min(v[0] for v in matches)] if matches else []
        result[requested]={'requested_name':requested,'status':'bound' if len(best)==1 else 'ambiguous' if best else 'not_found',
            'candidate_ids':[row[1] for row in best]}
        if len(best)==1:result[requested].update(poi_id=best[0][1],basis=best[0][2])
    return result


def entity_text(query,poi):
    """Remove only the candidate's explicitly reported administrative prefix."""
    text=normalized(query)
    if normalized(poi.get('name','')).startswith(text):
        return text
    for field,suffix in (('pname','省'),('cityname','市'),('adname','')):
        name=normalized(poi.get(field,''))
        for variant in dict.fromkeys((name,name.removesuffix(suffix) if suffix else name)):
            if variant!=name and not text[len(variant):].startswith((normalized(poi.get('adname','')) or '\0','的')):
                continue
            if variant and text.startswith(variant):
                text=text[len(variant):]
                text=text.removeprefix('的')
                break
    return text


def matches(query,poi):
    text=entity_text(query,poi)
    name=normalized(poi.get('name',''))
    address=normalized(poi.get('address',''))
    if re.search(r'门|入口',text):
        parent=re.fullmatch(r'(.+(?:大学|学院|学校|医院|小区|园区))(.{1,20}(?:门|入口))',text)
        if parent:
            return bool(re.fullmatch(re.escape(parent[1])+r'(?:(?:东|西|南|北)?校区|本部)?'+re.escape(parent[2]),name))
        return bool(text and text==name)
    return bool(text and (text in name or text in name+address or text in address+name))


def resolve_route_location(client,query,city='',*,purpose='',previous=None):
    from infrastructure.amap_client import Location, PlaceResolutionError, AmapError
    requested_city=city
    parts=re.split(r'[，,]\s*(?:具体(?:地址)?(?:是|为|在)?|地址(?:是|为|在)?|位于)\s*[:：]?\s*',query,maxsplit=1)
    requested=parts[0]
    address_hint=parts[1] if len(parts)==2 else ''
    text=normalized(requested)
    is_point=bool(re.search(POINT_PATTERN,text)) and not re.fullmatch(r'.+(?:省|市|区|县|旗)',text)
    if not is_point:
        try:
            location=client.geocode(query,**({'city':city} if city else {}))
        except AmapError as error:
            if error.code not in {'location_ambiguous','location_not_found'}:
                raise
        else:
            administrative=bool(re.fullmatch(r'.+(?:省|市|区|县|旗)',text))
            if (text in normalized(location.address) and (not city or city_name(city) in city_name(location.city or location.address))
                    and (administrative or location.level not in {'未知','省','市','区县','区','县','乡镇'})):
                return location

    expected_city=city
    expected_adcode=''
    keyword=text
    parent_match=re.fullmatch(r'(.+(?:大学|学院|学校|医院|小区|园区))(.{1,20}(?:门|入口))',text)
    if parent_match:
        parent=parent_match[1]
        anchors=[]
        for poi in client.search_places(city,parent,offset=20,extensions='all'):
            name=normalized(poi.get('name',''))
            parent_requested=entity_text(parent,poi)
            if (parent_requested and (name==parent_requested or (name.startswith(parent_requested) and name.endswith(('校区','院区'))))
                    and poi.get('cityname')
                    and not any(word in poi.get('type','') for word in ('公交','停车','商场','餐饮'))):
                anchors.append(poi)
        if city and anchors:
            scoped=[poi for poi in anchors if city_name(poi['cityname'])==city_name(city)]
            if not scoped:
                raise PlaceResolutionError(f'“{parent}”的匹配候选与指定城市“{city}”不一致，请核对城市或校区。',
                                           code='location_mismatch',field='city')
            anchors=scoped
        cities={city_name(poi['cityname']) for poi in anchors}
        if len(cities)!=1:
            raise PlaceResolutionError(f'无法唯一确认“{parent}”所在校区/城市，请提供该入口的具体地址或地图定位。',code='location_ambiguous')
        anchor_city=anchors[0]['cityname']
        if expected_city and city_name(expected_city)!=city_name(anchor_city):
            raise PlaceResolutionError(f'“{parent}”与指定城市“{expected_city}”不一致，请核对校区。',code='location_mismatch')
        expected_city=anchor_city
    else:
        region=re.match(r'^(.+?(?:区|县|旗|镇))(.+)$',text)
        if not region:
            region=re.match(r'^(.+?市)(.+)$',text) or re.match(r'^(.{2,12})的(.+)$',text)
        if region:
            administrative=client.geocode(region[1])
            # This coarse location is only a search constraint, never the endpoint.
            if normalized(region[1]).replace('市','') not in normalized(administrative.address).replace('市',''):
                raise PlaceResolutionError(f'无法确认“{region[1]}”的行政范围，请提供“{query}”的详细地址。',code='location_mismatch')
            expected_adcode=administrative.adcode if re.search(r'(?:区|县|旗|镇)$',region[1]) else ''
            expected_city=expected_city or administrative.city
            city=administrative.adcode or expected_city
            keyword=region[2].removeprefix('的')

    candidates={}
    accepted={}
    conflicting_cities=set()
    related={}
    diagnostics=[]
    reusable=bool(previous and previous.get('city')==requested_city and previous.get('candidates') and
        (normalized(previous.get('query','')) in text or text in normalized(previous.get('query',''))))
    stages=(['saved'] if reusable else [])+['text']+(['inputtips'] if parent_match and expected_city else [])
    for stage in stages:
        results=(previous['candidates'] if stage=='saved' else client.search_places(city or expected_city,keyword,offset=20,extensions='all') if stage=='text'
                 else client.search_place_tips(expected_city,keyword))
        trace={'stage':stage,'city':city or expected_city,'keywords':keyword,'returned':len(results),'rejected':[]}
        diagnostics.append(trace)
        for poi in results:
            reason=''
            if expected_city and city_name(poi.get('cityname',''))!=city_name(expected_city):
                reason='city_mismatch'
            elif expected_adcode and poi.get('adcode')!=expected_adcode:
                reason='district_mismatch'
            elif re.search(r'门|入口',text) and any(word in poi.get('type','') for word in ('公交','地铁','停车')):
                reason='not_an_entrance'
            elif any(word in poi.get('type','') for word in ('行政地名','行政区划')):
                reason='administrative_center'
            elif not matches(requested,poi):
                reason='name_mismatch'
            elif address_hint and entity_text(address_hint,poi) not in normalized(poi.get('address','')):
                reason='address_hint_mismatch'
            if reason:
                if reason=='city_mismatch' and matches(requested,poi):
                    conflicting_cities.add(poi.get('cityname',''))
                trace['rejected'].append({'id':poi.get('id',''),'name':poi.get('name',''),'reason':reason})
                name=normalized(poi.get('name',''))
                if reason=='name_mismatch' and poi.get('id'):
                    similar=(bool(re.fullmatch(re.escape(parent_match[1])+r'(?:(?:东|西|南|北)?校区|本部)?.{1,20}(?:门|入口)',name))
                             if parent_match else SequenceMatcher(None,entity_text(requested,poi),name).ratio()>=0.75)
                    if similar:
                        related[poi['id']]=poi
                continue
            try:
                longitude,latitude=(float(value) for value in poi.get('location','').split(','))
                valid=math.isfinite(longitude) and math.isfinite(latitude) and -180<=longitude<=180 and -90<=latitude<=90
            except (TypeError,ValueError):
                valid=False
            if not valid or not poi.get('id') or not poi.get('cityname'):
                trace['rejected'].append({'id':poi.get('id',''),'name':poi.get('name',''),'reason':'invalid_poi'})
                continue
            address=''.join(dict.fromkeys(str(poi.get(field) or '') for field in ('pname','cityname','adname','address')))
            label=poi['name']+'（'+address+'）'
            candidates[poi['id']]=Location(query,label,longitude,latitude,str(poi.get('adcode') or ''),
                poi_id=poi['id'],name=poi['name'],city=poi['cityname'],level='POI',source=PLACE_RESOLUTION_VERSION)
            accepted[poi['id']]={key:poi.get(key,'') for key in ('id','name','location','pname','cityname','adname','adcode','address','parent','type','typecode')}
        if candidates:
            break
    if candidates:
        scores={}
        for identifier,poi in accepted.items():
            target=entity_text(requested,poi); name=normalized(poi['name'])
            rank=3 if target==name else 2 if name.startswith(target) else 1
            station=str(poi.get('typecode','')) in {'150200','150500'}
            if station and re.sub(r'(?:地铁站|火车站|站)$','',target)==re.sub(r'(?:地铁站|火车站)$','',name).removesuffix('站'):
                rank=3
            mode_match=int(station and ((purpose=='transit' and poi.get('typecode')=='150500') or (purpose=='driving' and poi.get('typecode')=='150200')))
            scores[identifier]=(rank,mode_match)
        best=max(scores.values())
        named={key:value for key,value in candidates.items() if scores[key][0]==best[0]}
        candidates=(named if not expected_city and len({city_name(value.city) for value in named.values()})>1 else
                    {key:value for key,value in candidates.items() if scores[key]==best})
        if not re.search(r'门|入口|\d+号口',text):
            candidates={key:value for key,value in candidates.items() if not isinstance(accepted[key].get('parent'),str) or accepted[key]['parent'] not in candidates}
    logger.info('Route place resolution: %s',{'query':query,'stages':diagnostics,'accepted':list(candidates)})
    if len(candidates)!=1:
        if not candidates and conflicting_cities:
            raise PlaceResolutionError(f'“{query}”的匹配候选位于{"、".join(sorted(conflicting_cities))}，与指定城市“{expected_city}”不一致，请核对城市或地点。',
                                       code='location_mismatch',field='city')
        if not candidates and not re.search(r'门|入口',text) and re.search(r'[路街巷].*?\d+号',text):
            location=client.geocode(query,**({'city':expected_city} if expected_city else {}))
            if (location.level in {'门牌号','门牌'}
                    and normalized(query).replace('市','') in normalized(location.address).replace('市','')
                    and (not expected_city or city_name(location.city)==city_name(expected_city))):
                return location
        if candidates:
            detail='；'.join(location.address for location in list(candidates.values())[:5])
            scope='城市、具体地点' if len({city_name(location.city) for location in candidates.values()})>1 else '具体入口/地址'
            message=f'“{query}”有多个符合条件的地点，请核对{scope}：{detail}'
        elif related:
            detail='；'.join(poi['name']+'（'+poi.get('cityname','')+poi.get('adname','')+poi.get('address','')+'）'
                            for poi in list(related.values())[:5])
            message=f'地图返回了名称不同的候选，尚未将其用作“{query}”：{detail}。请核对后回复“起点是完整地点名称”或“终点是完整地点名称”确认；不会自动改用其他机构或入口。'
        else:
            result='地图未返回可用候选' if not any(trace['returned'] for trace in diagnostics) else '地图有返回结果，但机构、入口、城市或补充地址未能同时匹配'
            message=f'{result}，暂时无法确认“{query}”；不会改用同名门或行政区中心。请提供地图上该入口的完整地点名称和具体地址。'
        raise PlaceResolutionError(message,code='location_ambiguous' if candidates else 'location_not_found',
            resolution={'query':query,'city':requested_city,'candidates':list(accepted.values())[:20]})
    return next(iter(candidates.values()))
