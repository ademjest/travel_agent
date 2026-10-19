"""Resolve named route endpoints without silently downgrading their entity or precision."""
import math
import re
import logging
from difflib import SequenceMatcher


PLACE_RESOLUTION_VERSION='route-poi-v3'
POINT_PATTERN=r'大学|学院|学校|医院|小区|公司|中心|大厦|楼|路\d|街\d|\d+号|门|入口|园|机场|车站|火车站|地铁站|景区'
logger=logging.getLogger(__name__)


def normalized(value):
    text=re.sub(r'[\s（）()·，,。;；\-—–－]','',str(value or '')).lower()
    text=re.sub(r'(大学|学院|学校|医院|园区|小区|区|县|市)的',r'\1',text)
    return re.sub(r'(?<=门)(?:校门|出入口|入口)$','',text)


def city_name(value):
    return normalized(value).removesuffix('市')


def entity_text(query,poi):
    """Remove only the candidate's explicitly reported administrative prefix."""
    text=normalized(query)
    for field,suffix in (('pname','省'),('cityname','市'),('adname','')):
        name=normalized(poi.get(field,''))
        for variant in dict.fromkeys((name,name.removesuffix(suffix) if suffix else name)):
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


def resolve_route_location(client,query,city=''):
    from infrastructure.amap_client import Location, PlaceResolutionError
    parts=re.split(r'[，,]\s*(?:具体(?:地址)?(?:是|为|在)?|地址(?:是|为|在)?|位于)\s*[:：]?\s*',query,maxsplit=1)
    requested=parts[0]
    address_hint=parts[1] if len(parts)==2 else ''
    text=normalized(requested)
    is_point=bool(re.search(POINT_PATTERN,text)) and not re.fullmatch(r'.+(?:省|市|区|县|旗)',text)
    if not is_point:
        location=client.geocode(query,**({'city':city} if city else {}))
        if normalized(query) not in normalized(location.address):
            raise PlaceResolutionError(f'地点“{query}”与地图返回的“{location.address}”不一致，请提供具体地址。',code='location_mismatch')
        if city and city_name(city) not in city_name(location.city or location.address):
            raise PlaceResolutionError(f'地点“{query}”未能匹配到指定城市“{city}”。',code='location_mismatch')
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
                    and poi.get('cityname') and (not city or city_name(poi['cityname'])==city_name(city))
                    and not any(word in poi.get('type','') for word in ('公交','停车','商场','餐饮'))):
                anchors.append(poi)
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
    related={}
    diagnostics=[]
    stages=['text']+(['inputtips'] if parent_match and expected_city else [])
    for stage in stages:
        results=(client.search_places(city or expected_city,keyword,offset=20,extensions='all') if stage=='text'
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
        if candidates:
            break
    logger.info('Route place resolution: %s',{'query':query,'stages':diagnostics,'accepted':list(candidates)})
    if len(candidates)!=1:
        if not candidates and not re.search(r'门|入口',text) and re.search(r'[路街巷].*?\d+号',text):
            location=client.geocode(query,**({'city':expected_city} if expected_city else {}))
            if (location.level in {'门牌号','门牌'}
                    and normalized(query).replace('市','') in normalized(location.address).replace('市','')
                    and (not expected_city or city_name(location.city)==city_name(expected_city))):
                return location
        if candidates:
            detail='；'.join(location.address for location in list(candidates.values())[:5])
            message=f'“{query}”有多个符合条件的地点，请核对具体入口/地址：{detail}'
        elif related:
            detail='；'.join(poi['name']+'（'+poi.get('cityname','')+poi.get('adname','')+poi.get('address','')+'）'
                            for poi in list(related.values())[:5])
            message=f'地图返回了名称不同的候选，尚未将其用作“{query}”：{detail}。请核对后回复“起点是完整地点名称”或“终点是完整地点名称”确认；不会自动改用其他机构或入口。'
        else:
            result='地图未返回可用候选' if not any(trace['returned'] for trace in diagnostics) else '地图有返回结果，但机构、入口、城市或补充地址未能同时匹配'
            message=f'{result}，暂时无法确认“{query}”；不会改用同名门或行政区中心。请提供地图上该入口的完整地点名称和具体地址。'
        raise PlaceResolutionError(message,code='location_ambiguous' if candidates else 'location_not_found')
    return next(iter(candidates.values()))
