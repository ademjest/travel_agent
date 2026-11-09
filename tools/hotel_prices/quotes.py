"""Owned hotel reference quotes: evidence validation, pure evaluation and one bounded lookup."""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import unicodedata
from urllib.parse import urlsplit,parse_qs
from zoneinfo import ZoneInfo


VERSION='hotel-quotes-v2'
EVIDENCE_VERSION='hotel-reference-evidence-v5'
STAY_FIELDS={'check_in','check_out','rooms','adults','children_ages','room_requirement',
             'breakfast_requirement','cancellation_requirement'}
LABELS={'check_in':'入住日期','check_out':'退房日期','rooms':'房间数','adults':'成人数','children_ages':'儿童年龄（无儿童也请明确）'}
PUBLIC_CONDITIONS={
    'room_requirement':{'大床':'大床房','大床房':'大床房','双床':'双床房','双床房':'双床房','单人房':'单人房','家庭房':'家庭房','套房':'套房','无障碍房':'无障碍房','不限':'不限'},
    'breakfast_requirement':{'含早':'含早餐','含早餐':'含早餐','双早':'含两份早餐','含双早':'含两份早餐','不含早':'不含早餐','不含早餐':'不含早餐','不限':'不限'},
    'cancellation_requirement':{'可取消':'可取消','免费取消':'可免费取消','可免费取消':'可免费取消','不可取消':'不可取消','不限':'不限'}}


def public_stay(stay):
    """Only declared counts/dates and controlled condition labels cross provider/model boundaries."""
    result={key:stay.get(key) for key in LABELS}
    for field,labels in PUBLIC_CONDITIONS.items():
        value=stay.get(field)
        result[field]='未说明' if value is None else labels.get(value,'其他要求仅在本地保留，仍待核对')
    return result


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def normalized(text):
    return re.sub(r'[\W_]','',unicodedata.normalize('NFKC',str(text))).casefold()


def store_name(text,city):
    value=normalized(text);prefix=normalized(city).removesuffix('市')
    if prefix and value.startswith(prefix):value=value[len(prefix):].removeprefix('市')
    return value


def lodging_target(place):
    """AMap 10xxxx is accommodation; a conflicting hierarchy is not hotel identity."""
    code=str(place.get('typecode') or '')
    hierarchy=str(place.get('type') or '').split(';')
    if code:return bool(re.fullmatch(r'10\d{4}',code) and (not hierarchy[0] or hierarchy[0]=='住宿服务'))
    return hierarchy[0]=='住宿服务'


def target_key(target):
    return fingerprint({'city':normalized(target.get('city','')).removesuffix('市'),'name':normalized(target.get('name','')),
        'address_hint':re.sub(r'\s+','',unicodedata.normalize('NFKC',target.get('address_hint',''))).casefold()})


def target_matches(place,target):
    if not lodging_target(place) or normalized(place.get('cityname','')).removesuffix('市')!=normalized(target.get('city','')).removesuffix('市'):return False
    hint=target.get('address_hint')
    if not hint:return True
    clean=lambda value:re.sub(r'\s+','',unicodedata.normalize('NFKC',value)).casefold()
    address=clean(''.join(str(place.get(k) or '') for k in ('pname','cityname','adname','address')))
    return bool(re.search(r'(?<!\d)'+re.escape(clean(hint))+r'(?!\d)',address))


def candidate_choices(state,text,targets=None):
    """Resolve only displayed, selectable identities; typed refinements need current name evidence."""
    if state.get('selection_status') not in {'awaiting','invalid'}:return []
    if targets is not None:
        if len(targets)!=1 or not targets[0].get('name') or normalized(targets[0]['name']) not in normalized(text):return []
        target=targets[0]
        return [i for i,p in enumerate(state.get('hotel_candidates',[]),1)
                if store_name(p.get('name',''),p.get('cityname',''))==store_name(target['name'],target.get('city') or p.get('cityname',''))
                and target_matches(p,{**target,'city':target.get('city') or p.get('cityname','')})]
    return [i for i,p in enumerate(state.get('hotel_candidates',[]),1) if lodging_target(p) and normalized(text) in
            {normalized(p['name']),normalized(p['name']+' · '+p.get('address',''))}]


def unique_targets(targets):
    return list({target_key(t):t for t in targets}.values())


def unique_places(places):
    return list({p['id']:p for p in places if p.get('id')}.values())


def same_address(observed,target,city):
    """Remove only administrative affixes already evidenced by the map identity."""
    prefixes=[target.get('pname',''),target.get('cityname') or city,target.get('adname','')]
    def street(value):
        text=unicodedata.normalize('NFKC',str(value))
        # Navigation annotations are not a postal address or independent route evidence.
        text=normalized(re.sub(r'\([^()]*地铁站[^()]*步行[^()]*\)','',text))
        for prefix in prefixes:
            prefix=normalized(prefix)
            if prefix and text.startswith(prefix):text=text[len(prefix):]
        for prefix in reversed(prefixes):
            prefix=normalized(prefix)
            if prefix and text.endswith(prefix):text=text[:-len(prefix)]
        return text
    left,right=street(observed),street(target.get('address',''))
    return bool(left and right and left==right)


def amount(value):
    if isinstance(value,bool) or value is None:raise ValueError('金额缺失或类型无效。')
    try:result=Decimal(str(value))
    except InvalidOperation:raise ValueError('金额无效。') from None
    if not result.is_finite() or result<0 or result.as_tuple().exponent < -2:raise ValueError('金额须为非负且最多两位小数。')
    return result


def safe_url(value):
    if not isinstance(value,str):return None
    try:
        parsed=urlsplit(value)
        port=parsed.port
    except ValueError:return None
    host=(parsed.hostname or '').lower()
    official=any(host==domain or host.endswith('.'+domain) for domain in ('ctrip.com','trip.com'))
    return value if parsed.scheme=='https' and official and not parsed.username and not parsed.password and port in {None,443} else None


def _evidence(raw,text,limit=12000,start=0):
    if not isinstance(text,str) or not text or len(text)>limit or raw.find(text,start)<0:raise ValueError('原文证据不完整或无法定位。')
    position=raw.index(text,start)
    return {'start':position,'end':position+len(text),'text':text}


def _date_present(value,text,*,short=False,context_year=None):
    d=date.fromisoformat(value)
    explicit=[tuple(map(int,m)) for m in re.findall(r'(?<!\d)(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})[日号]?',text)]
    matching={year for year,month,day in explicit if (month,day)==(d.month,d.day)}
    if matching:return d.year in matching
    years={year for year,_,_ in explicit}
    if not short and not (len(years)==1 and d.year in years or context_year==d.year):return False
    return any(v in text for v in (f'{d.month}月{d.day}日',f'{d.month}月{d.day}号',f'{d.month:02}-{d.day:02}',f'{d.month}/{d.day}',f'{d.month:02}/{d.day:02}'))


def _plain_markdown(text):
    return re.sub(r'(\*\*|__|`)(.*?)\1',r'\2',text)


def _money_present(value,text):
    text=_plain_markdown(text)
    text=re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))','',text)
    if re.fullmatch(r'\s*\d+(?:\.\d+)?\s*',text):return amount(text.strip())==value
    return any(amount(n)==value for n in re.findall(r'(?<![\d.])(-?\d+(?:\.\d+)?)\s*(?:元|CNY|RMB|USD|HKD|美元|港元)',text,re.I))


def exact_room_amount(value,text):
    if re.search(r'人均|每人|每床|[/／](?:人|床|(?:\d+)?小时)',text):return False
    if re.search(r'早餐[^\d；;，,\n。]{0,8}(?:价|费)',text):return False
    if re.search(r'\d+(?:\.\d+)?(?:元|RMB|CNY)?\s*[-—–~～至到]\s*\d+(?:\.\d+)?\s*(?:元|RMB|CNY)',text,re.I):return False
    return _money_present(value,text)


def complete_fees(text,*,basis='per_room_night'):
    clauses=re.split(r'[，,；;\n|]',text)
    included=any(re.search(r'含全部税费|全部税费已含|税费已包含|税费已含|已含税费|含税及服务费',c) and
                 (basis=='total' or not re.search(r'总价|总额|合计',c) or re.search(r'每晚|各晚|所有报价|全部报价',c)) and
                 not re.search(r'不|未|未知|待确认|是否|部分|另',c) for c in clauses)
    extra=any('费' in c and re.search(r'不含|未含|另付|另收|另计',c) and not re.search(r'可选|自选|加购|早餐',c) for c in clauses)
    return included and not extra


def validate_quote_facts(value,raw,targets,stay,city):
    """Validate quoted table cells or dated prose, not a provider-specific Markdown extractor."""
    if not isinstance(value,dict) or 'offers' not in value or value.keys()-{'offers','price_absence'} or not isinstance(value['offers'],list) or len(value['offers'])>6:
        raise ValueError('酒店报价提取结构无效。')
    offers=[];clues=[];rejected=[];counts={};hotel_ids={};poi_hotels={}
    for item in value['offers']:
        try:
            if not isinstance(item,dict):raise ValueError('报价不是对象。')
            fields={'target_id','hotel_name','address','city','hotel_text','stay_text','check_in','check_out','adults',
                'rooms','children_ages','room_name','price_basis','currency','nightly_prices','reported_total',
                'price_header','price_row','terms_text','taxes_and_fees_status','breakfast','cancellation','eligibility','source_url','provider_hotel_id','average_price','from_price'}
            if item.keys()-fields:raise ValueError('提取含未登记字段。')
            poi=item.get('target_id');target=targets.get(poi)
            if not target:raise ValueError('报价未指向本批候选。')
            if not item.get('nightly_prices') and all(item.get(k) is None for k in ('reported_total','average_price','from_price')):continue
            block=item.get('hotel_text');evidence=_evidence(raw,block)
            if any(normalized(t['name']) in normalized(block) for pid,t in targets.items() if pid!=poi):
                raise ValueError('同一产品证据跨越不同酒店段落。')
            if (normalized(item.get('hotel_name'))!=normalized(target['name']) or
                    normalized(item.get('city')).removesuffix('市')!=normalized(city).removesuffix('市') or
                    any(normalized(item.get(k,'')) not in normalized(block) for k in ('hotel_name','address'))):
                raise ValueError('门店城市、完整名称或地址不一致，尚未唯一绑定。')
            if normalized(city).removesuffix('市') not in normalized(block):raise ValueError('酒店原文身份片段没有城市依据。')
            identity_pending=not target.get('address') or not same_address(item.get('address'),target,city)
            # Identity may follow the products. Bind to its enclosing named document section.
            headings=list(re.finditer(r'(?m)^\s*(#{1,6})[ \t]+([^\n]+)',raw))
            anchors=[h for h in headings if h.start()<=evidence['start'] and normalized(target['name']) in normalized(h[2])]
            anchor=anchors[-1] if anchors else None
            start=anchor.start() if anchor else evidence['start'];end=len(raw)
            for heading in headings:
                other=any(normalized(t['name']) in normalized(heading[2]) for pid,t in targets.items() if pid!=poi)
                other=other or bool(re.search(r'(?:酒店|宾馆|旅馆)(?:[(（][^()（）]*[)）])?[>》]?[ \t]*$',heading[2]) and normalized(target['name']) not in normalized(heading[2]))
                if heading.start()>start and other:
                    end=heading.start();break
            hotel_body=raw[start:end]
            if counts.get(poi,0)>=2:raise ValueError('每家最多接受两个产品。')
            room=item.get('room_name')
            if not isinstance(room,str) or not room or room not in hotel_body or normalized(room)==normalized(item['hotel_name']):raise ValueError('房型缺少同门店原文依据。')
            stay_text=item.get('stay_text');stay_evidence=_evidence(raw,stay_text)
            dates_known=all(item.get(k)==stay[k] and _date_present(stay[k],stay_text) for k in ('check_in','check_out'))
            basis=item.get('price_basis') or 'unknown';currency=item.get('currency') or 'unknown'
            if basis not in {'per_room_night','average','total','from','unknown'} or currency not in {'CNY','USD','HKD','unknown'}:raise ValueError('报价口径无效。')
            header=item.get('price_header') or '';row=item.get('price_row') or ''
            row_evidence=_evidence(raw,row,start=start)
            if row not in hotel_body:raise ValueError('价格行未绑定该房型。')
            if any(normalized(t['name']) in normalized(row) for pid,t in targets.items() if pid!=poi):raise ValueError('价格片段属于不同酒店。')
            products=[h for h in headings if start<h.start()<=row_evidence['start'] and normalized(room) in normalized(h[2])]
            product_start=row_evidence['start'];product_body=row
            if products:
                product=products[-1]
                finish=next((h.start() for h in headings if h.start()>product.start() and len(h[1])<=len(product[1])),end)
                if row_evidence['end']>min(end,finish):raise ValueError('产品价格片段跨越不同房型或套餐。')
                product_start=product.start();product_body=raw[product_start:min(end,finish)]
            elif not normalized(re.sub(r'^[\s|*#\-]+','',row)).startswith(normalized(room)):
                raise ValueError('价格片段没有可核对的房型归属。')
            terms=_evidence(raw,item.get('terms_text') or row,start=product_start)
            if terms['text'] not in product_body:raise ValueError('条款不属于同一房型产品。')
            occupancy_text=product_body+'\n'+stay_text
            occupancy=(item.get('rooms')==stay['rooms'] and type(item.get('adults')) is int and item['adults']>=stay['adults']
                       and re.search(str(item['rooms'])+r'\s*间',occupancy_text)
                       and re.search(str(item['adults'])+r'\s*(?:位|名|个)?(?:成人|成年人)',occupancy_text))
            declared_adults={int(n) for n in re.findall(r'(\d+)\s*(?:位|名|个)?(?:成人|成年人)',product_body)}
            if declared_adults and item.get('adults') not in declared_adults:occupancy=False
            if stay['children_ages']:
                occupancy=occupancy and item.get('children_ages')==stay['children_ages'] and all(re.search(str(n)+r'\s*岁',occupancy_text) for n in stay['children_ages'])
            currencies={code for code,pattern in (('CNY',r'人民币|CNY|RMB|(?<![美港])元|￥|¥'),
                ('USD',r'美元|USD'),('HKD',r'港元|港币|HKD')) if re.search(pattern,row+header,re.I)}
            if currency!='unknown' and currencies!={currency}:
                raise ValueError('币种缺少依据或相互冲突。')
            if basis=='per_room_night' and re.search(r'钟点|小时房',room):raise ValueError('钟点房不能作为连续房晚报价。')
            price_scope_pending=bool(basis=='per_room_night' and re.search(r'起价|起步价|最低价|均价',row))
            if header:
                _evidence(raw,header,start=start)
                if header not in hotel_body or hotel_body.index(header)>hotel_body.index(row):raise ValueError('价格表头不属于该门店产品。')
            cells=[c.strip() for c in row.strip().strip('|').split('|')]
            heads=[c.strip() for c in header.strip().strip('|').split('|')] if header else []
            nights=item.get('nightly_prices',[])
            if not isinstance(nights,list) or len(nights)>14:raise ValueError('逐晚价格结构无效。')
            validated=[]
            prose=_plain_markdown(product_body)
            product_dates='\n'.join(c for c in prose.splitlines() if re.search(r'入住|退房|适用日期',c) and re.search(r'\d{4}[年/-]\d|\d+月\d',c))
            scoped_dates=(all(_date_present(stay[k],product_dates) for k in ('check_in','check_out')) if product_dates
                else start<=stay_evidence['start'] and stay_evidence['end']<=product_start)
            room_prices=[]
            for line in prose.splitlines():
                field=re.match(r'\s*[-•]?\s*(?:每晚(?:日期)?(?:价格|房价|房费)?(?:及币种)?|房价|房费|每间每晚(?:价格)?)\s*[:：]\s*(.+)',line)
                if field:room_prices.append(re.split(r'[；;，,]',re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))','',field[1]),maxsplit=1)[0])
            for night in nights:
                if not isinstance(night,dict) or set(night)!={'date','amount'}:raise ValueError('逐晚价格字段无效。')
                d=date.fromisoformat(night['date']);price=amount(night['amount'])
                if not date.fromisoformat(stay['check_in'])<=d<date.fromisoformat(stay['check_out']):raise ValueError('逐晚日期越界。')
                if heads:
                    columns=[i for i,h in enumerate(heads) if _date_present(night['date'],h,short=True)]
                    if len(columns)!=1 or len(cells)!=len(heads) or not exact_room_amount(price,cells[columns[0]]):raise ValueError('日期与表格金额列无法相互核对。')
                else:
                    clauses=re.split(r'[；;\n，,]',re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))','',_plain_markdown(row)))
                    if product_dates and not scoped_dates:raise ValueError('本房型适用日期与查询日期不一致。')
                    dated=any(_date_present(night['date'],c,short=True) and exact_room_amount(price,c) and not any(
                        _date_present(n['date'],c,short=True) for n in nights if n['date']!=night['date']) and not re.search(
                        r'(?:早餐|餐费|押金|停车费|加床费|服务费)\s*(?:价格|费用)?\s*[:：]?\s*(?:约)?\d',c) for c in clauses)
                    uniform=dates_known and scoped_dates and any(re.search(r'每晚同价|各晚同价|每晚均为|各晚均为',c) and exact_room_amount(price,c) for c in clauses)
                    single=(dates_known and scoped_dates and len(nights)==1 and
                        (date.fromisoformat(stay['check_out'])-date.fromisoformat(stay['check_in'])).days==1 and
                        any(exact_room_amount(price,value) for value in room_prices))
                    if not (dated or uniform or single):raise ValueError('逐晚金额没有可对应的日期或明确同价上下文。')
                if not price and '免费' not in row:raise ValueError('零元缺少明确免费依据。')
                validated.append({'date':night['date'],'amount':str(price),'unit':'CNY/room/night' if currency=='CNY' else currency,
                                  **({'derivation':'explicit_uniform_rate'} if not heads and not dated and uniform else {})})
            if len({n['date'] for n in validated})!=len(validated):raise ValueError('逐晚日期重复。')
            reported=None
            if item.get('reported_total') is not None:
                reported=amount(item['reported_total'])
                total_cols=[i for i,h in enumerate(heads) if '总价' in h or '合计' in h]
                proof=cells[total_cols[0]] if len(total_cols)==1 and len(cells)==len(heads) else '\n'.join(
                    c for c in re.split(r'[；;\n]',product_body) if re.search(r'总价|合计|全程房费|总额',c))
                if not _money_present(reported,proof) or not reported and '免费' not in proof:raise ValueError('供应商总价无原文依据。')
            calculated=sum((amount(n['amount']) for n in validated),Decimal(0)) if validated else None
            required=(date.fromisoformat(stay['check_out'])-date.fromisoformat(stay['check_in'])).days
            complete=basis=='per_room_night' and len(validated)==required
            total_conflict=bool(complete and reported is not None and calculated!=reported)
            extra_prices={}
            for field in ('average_price','from_price'):
                if item.get(field) is not None:
                    number=amount(item[field])
                    if not _money_present(number,row) or not number and '免费' not in row:raise ValueError('均价或起价缺少原文依据。')
                    extra_prices[field]=str(number)
            taxes=item.get('taxes_and_fees_status','unknown')
            if taxes not in {'included','excluded','unknown'}:raise ValueError('税费状态无效。')
            if taxes=='included' and not complete_fees(product_body,basis=basis):taxes='unknown'
            if total_conflict and taxes!='included':total_conflict=False
            if taxes=='excluded' and not re.search(r'不含税|另付税费|未含税',terms['text']):taxes='unknown'
            for field in ('breakfast','cancellation','eligibility'):
                if item.get(field) and (not isinstance(item[field],str) or item[field] not in product_body):raise ValueError('产品条款缺少原文依据。')
            provider_id=item.get('provider_hotel_id');url=safe_url(item.get('source_url'))
            if provider_id is not None:
                parsed=urlsplit(url) if url else None
                ids=[v for k,values in parse_qs(parsed.query).items() if k.lower()=='hotelid' for v in values] if parsed else []
                ids += re.findall(r'/hotels/(\d+)\.html',parsed.path) if parsed else []
                if not isinstance(provider_id,str) or not provider_id.isdigit() or int(provider_id)<=0 or provider_id not in ids:
                    raise ValueError('供应商酒店ID没有同门店链接依据。')
            if url and url not in hotel_body:raise ValueError('链接不属于门店证据。')
            conflicted=set()
            if not identity_pending and not price_scope_pending:
                if provider_id in hotel_ids and hotel_ids[provider_id]!=poi:conflicted.update((poi,hotel_ids[provider_id]))
                if provider_id and poi in poi_hotels and poi_hotels[poi]!=provider_id:conflicted.add(poi)
                if provider_id:hotel_ids[provider_id]=poi
                if provider_id:poi_hotels[poi]=provider_id
            entry={k:item.get(k) for k in ('hotel_name','address','room_name','price_basis','currency','breakfast','cancellation','eligibility')}
            entry.update(price_basis=basis,currency=currency,rate_plan_terms=product_body,amap_poi_id=poi,provider_hotel_id=provider_id,source_url=url,nightly_prices=validated,
                amount_qualifier='approximate' if re.search(r'(?:约|预计|估计)\s*[￥¥]?\s*\d',_plain_markdown(row)) else 'exact',
                reported_total=str(reported) if reported is not None else None,calculated_total=str(calculated) if complete else None,
                **extra_prices,
                taxes_and_fees_status=taxes,identity_status='bound',extraction_status='validated',
                applicability_status='total_conflict' if total_conflict else 'applicable' if (complete or basis=='total' and reported is not None) and dates_known and occupancy else 'incomplete_conditions',
                evidence_spans=[evidence,stay_evidence,row_evidence,terms])
            entry['offer_id']='HQ-'+fingerprint(entry)[:20]
            if identity_pending or price_scope_pending:
                reasons=[]
                if identity_pending:
                    reason='来源地址与地图地址未严格对应，门店身份尚未独立核实。';reasons.append(reason)
                    rejected.append({'target_id':poi,'reason':reason,'code':'address_unverified','reported_address':item.get('address'),'target_address':target.get('address')})
                if price_scope_pending:
                    reason='来源含起价或均价标记，价格口径尚未确认，不能作为目标日期的独立逐晚房型价。';reasons.append(reason)
                    rejected.append({'target_id':poi,'reason':reason,'code':'price_basis_unverified'})
                if validated or reported is not None or extra_prices:
                    clue={k:v for k,v in entry.items() if k not in {'amap_poi_id','offer_id'}}
                    clue.update(reference_id='HC-'+fingerprint(entry)[:20],requested_target_ref=poi,reported_city=item.get('city'),target_address=target.get('address'),
                        reported_stay={**{k:item.get(k) if dates_known else None for k in ('check_in','check_out')},
                            **{k:item.get(k) if occupancy else None for k in ('rooms','adults')}},
                        identity_status='unverified' if identity_pending else 'bound',applicability_status='not_verified',unresolved_reasons=reasons+
                            ([] if entry['applicability_status']=='applicable' else ['日期、人数或价格覆盖仍需核对。']))
                    if price_scope_pending:
                        amounts=list(dict.fromkeys(n['amount'] for n in validated)) or ([str(reported)] if reported is not None else list(extra_prices.values()))
                        clue.update(reference_amounts=amounts,nightly_prices=[],reported_total=None,calculated_total=None,price_basis='unknown')
                    clues.append(clue)
                counts[poi]=counts.get(poi,0)+1
                continue
            if conflicted:
                for current in [*offers,entry]:
                    if current['amap_poi_id'] in conflicted:
                        current.update(identity_status='ambiguous',applicability_status='identity_conflict')
            offers.append(entry);counts[poi]=counts.get(poi,0)+1
        except (ValueError,TypeError,KeyError,IndexError,InvalidOperation) as error:
            rejected.append({'target_id':item.get('target_id') if isinstance(item,dict) else None,'reason':str(error)})
    absence=value.get('price_absence',[]);unavailable=[]
    if not isinstance(absence,list) or len(absence)>3:
        rejected.append({'target_id':None,'reason':'来源缺价说明结构无效。','code':'invalid_price_absence'});absence=[]
    for item in absence:
        try:
            if not isinstance(item,dict) or set(item)!={'target_id','evidence_text'}:raise ValueError('来源缺价说明字段无效。')
            target=targets.get(item['target_id']);span=_evidence(raw,item['evidence_text'])
            proof=span['text']
            if not target or normalized(target['name']) not in normalized(proof) or any(
                    normalized(t['name']) in normalized(proof) for pid,t in targets.items() if pid!=item['target_id']):
                raise ValueError('来源缺价说明没有唯一查询门店依据。')
            if not all(_date_present(stay[k],proof) for k in ('check_in','check_out')):raise ValueError('来源缺价说明没有目标日期依据。')
            clauses=[re.sub(r'[*_`]','',line) for line in proof.splitlines() if '早餐' not in line]
            if not any(re.search(r'^\s*[-#]?\s*(?:房价|房费|每晚(?:日期)?价格|房型价格)[^。；\n]{0,35}(?:未知|未返回|未提供|无法获取)|(?:未返回|未提供|无法获取)[^。；\n]{0,20}(?:房价|房费|房型价格)',line) for line in clauses):
                raise ValueError('原文没有明确的住宿价格未知声明。')
            unavailable.append({'requested_target_ref':item['target_id'],'reported_hotel_name':target['name'],'evidence_span':span})
        except (ValueError,KeyError,TypeError) as error:
            rejected.append({'target_id':item.get('target_id') if isinstance(item,dict) else None,'reason':str(error),'code':'invalid_price_absence'})
    return {'offers':offers,'reference_clues':clues,'unavailable_prices':unavailable,'rejected':rejected}


def validate_quotes(value,raw,targets,stay,city):
    result=validate_quote_facts(value,raw,targets,stay,city)
    return result['offers'],result['rejected']


def project_quote(snapshot):
    """Recheck saved model facts locally; never rewrite source, history or accounting."""
    if (snapshot.get('evidence_version')!=EVIDENCE_VERSION and isinstance(snapshot.get('extracted_facts'),dict)
            and all(snapshot.get(k) for k in ('raw_text','targets','stay','city'))):
        try:result=validate_quote_facts(snapshot['extracted_facts'],snapshot['raw_text'],snapshot['targets'],snapshot['stay'],snapshot['city'])
        except ValueError:pass
        else:snapshot={**snapshot,**result,'evidence_version':EVIDENCE_VERSION}
    names='、'.join(dict.fromkeys(p['reported_hotel_name'] for p in snapshot.get('unavailable_prices',[])))
    unavailable_note=('供应商原文将以下酒店部分房型的目标日期房价标注为未知：'+names+'。展示起价不等于目标日期的房型实价，未据此判定售罄；早餐费用不作为住宿房价。') if names else ''
    if snapshot.get('budget_block'):status='budget_blocked';note=snapshot.get('note','模型预算阻断，未继续提取；已有原文仍可本地读取。')
    elif snapshot.get('submission_status') in {'submitted','result_unknown','submission_unknown'}:status='submission_unknown';note='上次提交结果未知，可能已处理，本次未自动重发。'
    elif snapshot.get('offers'):status='offers_available';note='已有来源报价；具体适用性、税费与预算结论按产品逐项核对。'
    elif snapshot.get('reference_clues'):status='reference_only';note='已有原文参考价格，门店或产品条件仍待核对，不用于预算通过或可订判断。'
    elif snapshot.get('extraction_status')=='failed':status='extraction_failed';note='供应商原文已保存，结构化提取未完成；不能据此断言供应商没有价格。'
    elif unavailable_note:status='source_price_unknown';note=unavailable_note
    elif snapshot.get('rejected'):status='evidence_rejected';note='已取得原文，但提取事实未通过证据核验；可查看原文，不能据此断言没有房价或已经售罄。'
    elif snapshot.get('raw_text'):status='no_structured_price';note='已取得供应商资料，尚无可核验的结构化房价；可从“查看原文”读取完整内容。'
    elif snapshot.get('request_id'):status='provider_incomplete';note=snapshot.get('note') or '供应商查询未取得确认结果，未自动重发。'
    else:status='not_submitted';note=snapshot.get('note') or '本次尚未提交酒店报价查询。'
    return {**snapshot,'result_status':status,'result_note':note,'unavailable_note':unavailable_note}


def reusable(snapshot,stay,now):
    if not isinstance(snapshot.get('retrieved_at'),str):return False
    try:
        retrieved=datetime.fromisoformat(snapshot['retrieved_at'].replace('Z','+00:00'))
        return 0<=(now-retrieved).total_seconds()<=1800 and date.fromisoformat(stay['check_in'])>=now.astimezone(ZoneInfo('Asia/Shanghai')).date()
    except (KeyError,ValueError,TypeError):return False


def preferred_snapshot(spec,snapshots):
    """Source facts are atomic; merging their individual JSON fields could manufacture a rate."""
    available=[q for q in snapshots if q]
    def order(q):
        same=all(spec.get('hotel_stay',{}).get(k)==q.get('stay',{}).get(k) for k in STAY_FIELDS)
        same=same and normalized(q.get('city',spec.get('destination',''))).removesuffix('市')==normalized(spec.get('destination','')).removesuffix('市')
        return same,q.get('recorded_at') or q.get('retrieved_at') or ''
    return deepcopy(max(available,key=order)) if available else None


def condition_met(field,required,offer):
    if re.fullmatch(r'(?:房型|早餐|退改)?(?:不限|无要求|不要求|可选)(?:房型|早餐|退改)?',required):return True
    actual=str(offer.get({'room_requirement':'room_name','breakfast_requirement':'breakfast','cancellation_requirement':'cancellation'}[field]) or '')
    if re.search(r'未知|待确认|未明确|未提供|不保证|不能保证|随机|以.+为准',actual):return False
    for match in re.finditer(re.escape(required),actual):
        prefix=actual[:match.start()]
        if not re.search(r'(?:不|未|非|无|不包|不含|未含|不包含|不包括|不可|不能|不支持|不提供)$',prefix):return True
    return False


def evaluate(spec,snapshot,now=None,*,places=None):
    """No I/O. Keep numeric room charges separate from full-condition reference matches."""
    from core.budget_contract import budget_bounds, hotel_comparison_unit
    now=now or datetime.now(timezone.utc);stay=spec.get('hotel_stay',{})
    stale=not reusable(snapshot,stay,now)
    same=all(stay.get(k)==snapshot.get('stay',{}).get(k) for k in STAY_FIELDS)
    same=same and normalized(snapshot.get('city',spec.get('destination',''))).removesuffix('市')==normalized(spec.get('destination','')).removesuffix('市')
    calendar={key:spec.get(key) for key in ('start_date','end_date','day_count','date_is_tentative')}
    if stay.get('itinerary_calendar') is not None and stay['itinerary_calendar']!=calendar:same=False
    budget=spec.get('budgets',{}).get('accommodation',{})
    bounds=budget.get('bounds') or (budget_bounds(budget['value'],budget.get('source','')) if budget.get('value') else None)
    output=[]
    for offer in snapshot.get('offers',[]):
        reasons=[];numeric='not_comparable';prices=[amount(n['amount']) for n in offer['nightly_prices']]
        approximate=(offer.get('amount_qualifier')=='approximate' or
                     bool(re.search(r'(?:约|预计|估计)\s*[￥¥]?\s*\d',offer.get('rate_plan_terms',offer['evidence_spans'][2]['text']))))
        fee_status=offer['taxes_and_fees_status']
        if fee_status=='included' and not complete_fees(offer.get('rate_plan_terms',offer['evidence_spans'][3]['text']),basis=offer['price_basis']):fee_status='unknown'
        current=places.get(offer['amap_poi_id']) if places is not None else None
        identity_matches=places is None or bool(current and normalized(current['name'])==normalized(offer['hotel_name']) and same_address(offer['address'],current,spec.get('destination','')))
        total_budget=bool(re.fullmatch(r'(?:人民币|CNY|RMB|元)(?:住宿)?(?:全程)?(?:总价|总额|总预算)',normalized(budget.get('unit_text','')),re.I))
        comparable=hotel_comparison_unit(budget.get('unit_text',''))=='per_room_night_cny' and offer['price_basis']=='per_room_night'
        if total_budget:
            total=offer.get('reported_total') or offer.get('calculated_total')
            prices=[amount(total)] if total is not None else []
            comparable=True
        if bounds and prices and offer.get('identity_status')=='bound' and offer['currency']=='CNY' and comparable and bounds['kind']!='approximate' and not approximate:
            below=lambda n:bounds['lower'] is not None and (n<Decimal(str(bounds['lower'])) or n==Decimal(str(bounds['lower'])) and not bounds['lower_inclusive'])
            above=lambda n:bounds['upper'] is not None and (n>Decimal(str(bounds['upper'])) or n==Decimal(str(bounds['upper'])) and not bounds['upper_inclusive'])
            numeric='over' if any(above(n) for n in prices) else 'below' if any(below(n) for n in prices) else 'within'
        if bounds and numeric=='not_comparable':reasons.append('当前价格口径或近似预算不足以直接比较；未用均价或总价代替每晚核对')
        if approximate:reasons.append('来源金额为约价，不能确认为所需产品的明确应付金额')
        if numeric=='over':reasons.append('已列房费高于预算上限；不代表其他未返回房型均超预算')
        if numeric=='below':reasons.append('已列房费低于严格预算下限；不是最终含税总额结论')
        if stale:reasons.append('历史参考报价，已超过本地30分钟复用窗口或入住日已过')
        if not same:reasons.append('报价入住条件与当前要求不同')
        if offer.get('identity_status')!='bound':reasons.append('供应商酒店ID相互冲突，门店身份待确认；未借用该价格判定预算')
        if not identity_matches:reasons.append('报价门店与当前候选身份不同，旧价不参与当前匹配')
        if offer['applicability_status'] in {'incomplete_conditions','total_conflict'}:reasons.append('日期、人数或逐晚覆盖尚不完整' if offer['applicability_status']!='total_conflict' else '供应商总价与逐晚加总冲突')
        if fee_status!='included':reasons.append('税费及完整应付金额待确认')
        for field in ('room_requirement','breakfast_requirement','cancellation_requirement'):
            required=stay.get(field)
            if required and not condition_met(field,required,offer):reasons.append('产品尚未明确满足'+{'room_requirement':'房型','breakfast_requirement':'早餐','cancellation_requirement':'退改'}[field]+'要求')
        if (offer.get('eligibility') and offer['eligibility'] not in {'不限','无会员或优惠券限制','无门槛'}
                or re.search(r'会员|券后|优惠券|专享',offer.get('rate_plan_terms',offer['evidence_spans'][2]['text']))):reasons.append('会员或优惠资格尚未核实')
        eligible=identity_matches and offer.get('identity_status')=='bound' and offer['applicability_status']=='applicable'
        status='historical' if stale else 'different_stay' if not same else 'pending' if not eligible else 'excluded' if numeric=='over' or numeric=='below' and fee_status=='included' else 'reference_match' if numeric=='within' and not reasons else 'reference_available' if not bounds and not reasons else 'pending'
        delta=None
        if numeric=='over':delta=max(prices)-Decimal(str(bounds['upper']))
        if numeric=='below':delta=Decimal(str(bounds['lower']))-min(prices)
        output.append({**offer,'amount_qualifier':'approximate' if approximate else 'exact','taxes_and_fees_status':fee_status,'numeric_comparison':numeric,'status':status,'issues':reasons,
                       'budget_difference':str(delta) if delta is not None else None,'comparison_basis':'stay_total' if total_budget else 'room_night'})
    return output
