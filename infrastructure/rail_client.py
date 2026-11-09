"""Read-only single-leg adapter for the reviewed 12306 MCP contract."""
from datetime import date, datetime, timedelta
import asyncio
import json
import math
import re
from jsonschema import Draft202012Validator, SchemaError

from infrastructure.mcp_client import MCPUnavailable

MAX_RAIL_CANDIDATES = 100


# Fixed upstream contract: ff6439da6f63d7d72181abea4568abd69878c600.
# Runtime discovery must independently confirm these fields before any tools/call.
REQUIRED_TOOLS = {
    'get-station-code-of-citys': {'citys': 'string'},
    'get-station-code-by-names': {'stationNames': 'string'},
    'get-tickets': {'date': 'string', 'fromStation': 'string', 'toStation': 'string',
                    'trainFilterFlags': 'string', 'earliestStartTime': 'number',
                    'latestStartTime': 'number', 'sortFlag': 'string', 'limitedNum': 'number', 'format': 'string'},
}


def validate_rail_tools(tools):
    schemas = {t.name: t.inputSchema for t in tools}
    for name, fields in REQUIRED_TOOLS.items():
        schema = schemas.get(name, {})
        if schema.get('type') != 'object':
            raise MCPUnavailable('schema_mismatch')
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError:
            raise MCPUnavailable('schema_mismatch') from None
        props = schema.get('properties', {})
        if any(props.get(key, {}).get('type') != kind for key, kind in fields.items()):
            raise MCPUnavailable('schema_mismatch')
        if set(schema.get('required', [])) - set(fields):
            raise MCPUnavailable('schema_mismatch')
        # Remote schemas are data, never authority to fetch external references.
        nodes = [schema]
        while nodes:
            node = nodes.pop()
            if isinstance(node, dict):
                if '$ref' in node and not str(node['$ref']).startswith('#/'):
                    raise MCPUnavailable('schema_mismatch')
                nodes.extend(node.values())
            elif isinstance(node, list):
                nodes.extend(node)
    return {name: schemas[name] for name in REQUIRED_TOOLS}


def response_json(result):
    if result.isError:
        raise MCPUnavailable('provider_error')
    if result.structuredContent is not None:
        return result.structuredContent
    blocks = [part.text for part in result.content if part.type == 'text']
    if not blocks or sum(map(len, blocks)) > 2_000_000:
        raise MCPUnavailable('invalid_response')
    try:
        return json.loads(''.join(blocks))
    except ValueError:
        raise MCPUnavailable('invalid_response') from None


class RailClient:
    def __init__(self, mcp):
        self.mcp = mcp

    def query_sync(self, *args, **kwargs):
        return self.mcp.run_sync(lambda: self.query(*args,**kwargs))

    async def query(self, origin, destination, travel_date, *, origin_scope='city', destination_scope='city',
                    train_preference='high_speed', departure_window=(0, 24), limit=6, call=None):
        try:
            day = date.fromisoformat(travel_date)
            if (day.isoformat() != travel_date or train_preference not in ('high_speed','high_speed_only','d_train','conventional','all')
                    or type(limit) is not int or not 1 <= limit <= 20
                    or origin_scope not in ('city', 'station') or destination_scope not in ('city', 'station')
                    or any(not isinstance(v, str) or not v.strip() or len(v) > 40 or '|' in v for v in (origin, destination))
                    or len(departure_window) != 2
                    or any(type(v) not in (int, float) for v in departure_window)
                    or not 0 <= departure_window[0] < departure_window[1] <= 24):
                raise ValueError()
        except (ValueError, TypeError):
            return {'status': 'invalid_request', 'trains': []}
        codes = []
        caller=call or self.mcp.call
        provider_retries = 0
        active_tool = ''
        async def read(tool, arguments):
            nonlocal provider_retries, active_tool
            active_tool = tool
            for attempt in range(2):
                result = await call(tool,arguments,attempt=attempt) if call else await caller(tool,arguments)
                if not result.isError or attempt:
                    return response_json(result)
                # A returned error is a confirmed failure; transport errors are never replayed here.
                provider_retries += 1
                await asyncio.sleep(0.25)
        try:
            for field,value,scope in (('origin',origin,origin_scope),('destination',destination,destination_scope)):
                name, key = ('get-station-code-of-citys', 'citys') if scope == 'city' else ('get-station-code-by-names', 'stationNames')
                lookup=value.removesuffix('市') if scope=='city' else value
                data = await read(name, {key: lookup})
                entry = data.get(lookup.removesuffix('站') if scope == 'station' else lookup) if isinstance(data, dict) else None
                code = entry.get('station_code') if isinstance(entry, dict) else None
                if not isinstance(code, str) or not re.fullmatch('[A-Z]{3}', code):
                    raise MCPUnavailable('location_unresolved')
                codes.append(code)
            candidate_limit = MAX_RAIL_CANDIDATES
            arguments = {'date': travel_date, 'fromStation': codes[0], 'toStation': codes[1],
                'trainFilterFlags': {'high_speed':'GD','high_speed_only':'G','d_train':'D','conventional':'ZTKO','all':''}[train_preference],
                'earliestStartTime': departure_window[0], 'latestStartTime': departure_window[1],
                'sortFlag': 'startTime', 'limitedNum': candidate_limit, 'format': 'json'}
            data = await read('get-tickets', arguments)
            if isinstance(data,dict) and data.get('status')=='not_on_sale':
                return {'status':'not_on_sale','trains':[],'travel_date':travel_date,'provider_retries':provider_retries}
            if not isinstance(data, list) or len(data) > 1000:
                raise MCPUnavailable('invalid_response')
            all_trains=self.normalize_tickets(data,day,codes,(origin_scope,destination_scope),
                                              train_preference,departure_window,limit)
            trains=all_trains[:limit]
            truncated = len(data) >= candidate_limit
            return {'status': 'ok' if trains else 'incomplete' if truncated else 'empty', 'trains': trains,
                    'all_trains':all_trains,
                    'provider_retries':provider_retries,
                    'candidate_truncated': truncated,
                    'candidate_limit':candidate_limit,
                    'travel_date': travel_date, 'origin_scope': origin_scope, 'destination_scope': destination_scope,
                    'city_coverage': 'provider_city_code_unverified' if 'city' in (origin_scope, destination_scope) else 'exact_station',
                    'retrieved_at': datetime.now().astimezone().isoformat()}
        except MCPUnavailable as error:
            return {'status': 'error', 'error_code': error.code, 'trains': [],
                    'error_tool':active_tool,'provider_retries':provider_retries,
                    **({'error_field':field} if error.code=='location_unresolved' else {})}
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            return {'status': 'error', 'error_code': 'invalid_response', 'trains': [],
                    'error_tool':active_tool,'provider_retries':provider_retries}

    @staticmethod
    def normalize_tickets(data,day,codes,scopes,preference,window,limit=6):
        """Normalize a bounded provider snapshot for both new queries and historical views."""
        if not isinstance(data,list) or len(data)>1000:
            raise MCPUnavailable('invalid_response')
        selected=[]
        for row in data:
            if not isinstance(row,dict):
                raise MCPUnavailable('invalid_response')
            matches=True
            for scope,field,expected in zip(scopes,('from_station_telecode','to_station_telecode'),codes):
                if scope=='station':
                    actual=row.get(field)
                    if not isinstance(actual,str) or not re.fullmatch('[A-Z]{3}',actual):
                        raise MCPUnavailable('invalid_response')
                    matches=matches and actual==expected
            if matches:
                selected.append(RailClient._train(row,day))
        categories={'high_speed':('G','D'),'high_speed_only':('G',),'d_train':('D',),'conventional':('Z','T','K','Y','S')}
        trains=[row for row in selected if (preference=='all' or row['train_code'].startswith(categories[preference])
            or preference=='conventional' and row['train_code'][0].isdigit())
            and window[0]<=int(row['departure_at'][11:13])+int(row['departure_at'][14:16])/60<window[1]]
        if preference=='high_speed' and sum(row['train_code'].startswith('G') for row in trains)>=limit:
            trains=[row for row in trains if row['train_code'].startswith('G')]
        trains.sort(key=lambda row:(preference=='high_speed' and not row['train_code'].startswith('G'),row['departure_at'],row['train_code']))
        return trains

    @staticmethod
    def _train(row, day):
        code = row['start_train_code']
        if not isinstance(code, str) or not re.fullmatch('[A-Z0-9]{1,12}', code):
            raise ValueError()
        if any(not isinstance(row[key], str) or not re.fullmatch(r'\d{2}:\d{2}', row[key])
               for key in ('start_time', 'arrive_time')):
            raise ValueError()
        start = datetime.fromisoformat(day.isoformat() + 'T' + row['start_time'])
        hours, minutes = map(int, row['lishi'].split(':'))
        if not 0 <= hours <= 240 or not 0 <= minutes < 60:
            raise ValueError()
        # The upstream start_date may be the train's originating day; use the requested boarding day.
        arrival = start + timedelta(hours=hours, minutes=minutes)
        if arrival.strftime('%H:%M') != row['arrive_time']:
            raise ValueError()
        seats = []
        for value in row.get('prices') or []:
            price = value.get('price')
            price = price if type(price) in (int, float) and math.isfinite(price) and price >= 0 else None
            num = str(value.get('num')) if value.get('num') is not None else ''
            availability = ('available' if num in ('有', '充足') or num.isdigit() and int(num) > 0
                            else 'waitlist' if num == '候补' else 'sold_out' if num in ('无', '0') else 'unknown')
            seats.append({'name': value.get('seat_name'), 'price_cny': price, 'availability': availability})
        return {'train_code': code, 'origin_station': row['from_station'], 'destination_station': row['to_station'],
                'departure_at': start.isoformat(timespec='minutes') + '+08:00',
                'arrival_at': arrival.isoformat(timespec='minutes') + '+08:00',
                'duration_minutes': hours*60+minutes, 'seats': seats}
