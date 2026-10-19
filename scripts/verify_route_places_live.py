"""Explicit, bounded Amap integration check; never calls a language model or production DB."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from dotenv import dotenv_values
from infrastructure.amap_client import AmapClient, PlaceResolutionError
from infrastructure.place_resolution import resolve_route_location, PLACE_RESOLUTION_VERSION


class RecordingClient(AmapClient):
    def __init__(self,key):
        super().__init__(key)
        self.requests=[]
        self.snapshots={}

    def _get(self,path,params):
        signature=(path,json.dumps(params,sort_keys=True,ensure_ascii=False))
        if signature in self.snapshots:
            return self.snapshots[signature]
        if len(self.requests)>=35:
            raise RuntimeError('Live verification request limit reached')
        data=super()._get(path,params)
        fields=('id','name','address','formatted_address','city','cityname','pname','adname','district',
                'adcode','location','type','typecode','level')
        records={key:[{field:item[field] for field in fields if field in item} for item in data.get(key,[])]
                 for key in ('pois','tips','geocodes') if isinstance(data.get(key),list)}
        self.requests.append({'path':path,'params':params,'count':data.get('count'),**records})
        self.snapshots[signature]=data
        return data


def main(output):
    client=RecordingClient(dotenv_values(ROOT/'.env').get('AMAP_API_KEY',''))
    cases=[]
    places={}
    inputs=[('qiming','华中科技大学启明门','B0FFG0R3RE'),
            ('south','华中科技大学-南大门','B001B0JWWZ'),
            ('south_address','华中科技大学-南大门，具体是武汉市洪山区珞喻路1037号','B001B0JWWZ'),
            ('center','武汉的航天三院信息产业研发中心','B0J6NHHO7R')]
    for label,query,identifier in inputs:
        try:
            location=resolve_route_location(client,query)
            passed=location.poi_id==identifier and location.city=='武汉市'
            cases.append({'case':label,'passed':passed,'location':asdict(location)})
            if passed:
                places[label]=location
        except Exception as error:
            cases.append({'case':label,'passed':False,'error':str(error)})
    try:
        resolve_route_location(client,'武汉东西湖区的航天信息产业研发中心')
        cases.append({'case':'institution_confirmation','passed':False,'error':'Different institution name was silently accepted'})
    except PlaceResolutionError as error:
        cases.append({'case':'institution_confirmation','passed':'航天三院信息产业研发中心' in str(error),'reply':str(error)})
    except Exception as error:
        cases.append({'case':'institution_confirmation','passed':False,'error':str(error)})
    for origin in ('qiming','south_address'):
        label=origin+'_driving'
        if origin not in places or 'center' not in places:
            cases.append({'case':label,'passed':False,'error':'Endpoint verification failed'})
            continue
        try:
            route=client.driving_route_locations(places[origin],places['center'])
            cases.append({'case':label,'passed':0<route.distance_meters<200000 and route.duration_seconds>0,
                          'origin':asdict(route.origin),'destination':asdict(route.destination),
                          'distance_meters':route.distance_meters,'duration_seconds':route.duration_seconds})
        except Exception as error:
            cases.append({'case':label,'passed':False,'error':str(error)})
    report={'generated_at':datetime.now(timezone.utc).isoformat(),'resolver':PLACE_RESOLUTION_VERSION,
            'passed':all(case['passed'] for case in cases),'cases':cases,'http_requests':len(client.requests),
            'requests':client.requests,'note':'Real Amap only; repeated identical lookups reuse this run’s snapshots. No model calls, production DB writes, or API keys in report.'}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:report[key] for key in ('passed','http_requests','cases')},ensure_ascii=False))
    raise SystemExit(0 if report['passed'] else 1)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    main(parser.parse_args().output)
