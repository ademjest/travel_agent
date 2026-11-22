import asyncio
from copy import deepcopy
import json
import unittest
from mcp.types import CallToolResult, TextContent

from infrastructure.mcp_client import MCPUnavailable
from infrastructure.rail_client import RailClient, response_json


ROW={'start_train_code':'D17','start_date':'2026-10-28','start_time':'23:30','arrive_time':'01:00','lishi':'01:30',
     'from_station':'出发南','to_station':'到达东','from_station_telecode':'AAA','to_station_telecode':'BBB',
     'prices':[{'seat_name':'二等座','price':None,'num':'--'}]}


class Tickets:
    def __init__(self, rows=None):
        self.rows=deepcopy([ROW] if rows is None else rows)
        self.calls=[]
    async def call(self,name,arguments):
        self.calls.append((name,arguments))
        if name=='get-tickets': data=self.rows
        else:
            value=next(iter(arguments.values()))
            data={value.removesuffix('站'):{'station_code':'AAA' if value.startswith('出发') else 'BBB'}}
        return CallToolResult(content=[TextContent(type='text',text=json.dumps(data))])


class RailTests(unittest.IsolatedAsyncioTestCase):
    async def test_display_limit_does_not_discard_normalized_candidates_or_limit_the_fetch(self):
        from pathlib import Path
        fixture=json.loads((Path(__file__).parent/'fixtures/rail_city_pagination_2026-11-01.json').read_text(encoding='utf-8'))
        source=Tickets(fixture['trains'])
        for count in (1,6):
            result=await RailClient(source).query('武汉','西安','2026-11-01',train_preference='high_speed_only',limit=count)
            self.assertEqual(len(result['trains']),count)
            self.assertEqual(len(result['all_trains']),47)
            self.assertEqual(result['all_trains'][-1]['departure_at'][11:16],'19:21')
            self.assertFalse(result['candidate_truncated'])
        self.assertTrue(all(args['limitedNum']==100 for name,args in source.calls if name=='get-tickets'))
    async def test_explicit_provider_failure_has_one_identical_retry(self):
        source=Tickets();original=source.call;attempts=[]
        async def intermittent(name,arguments):
            if name=='get-tickets':
                attempts.append(dict(arguments))
                if len(attempts)==1:
                    return CallToolResult(isError=True,content=[TextContent(type='text',text='arbitrary upstream failure')])
            return await original(name,arguments)
        source.call=intermittent
        result=await RailClient(source).query('出发','到达','2026-10-30',train_preference='d_train')
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['provider_retries'],1)
        self.assertEqual(attempts,[attempts[0],attempts[0]])

    async def test_repeated_provider_failure_is_typed_and_bounded(self):
        source=Tickets();original=source.call;attempts=[]
        async def failed(name,arguments):
            if name=='get-tickets':
                attempts.append(arguments)
                return CallToolResult(isError=True,content=[TextContent(type='text',text='private provider detail')])
            return await original(name,arguments)
        source.call=failed
        result=await RailClient(source).query('出发','到达','2026-10-30')
        self.assertEqual((result['status'],result['error_code'],result['error_tool']),('error','provider_error','get-tickets'))
        self.assertEqual(len(attempts),2)
        self.assertNotIn('private',json.dumps(result))

    async def test_malformed_success_is_not_retried(self):
        source=Tickets();original=source.call;attempts=[]
        async def malformed(name,arguments):
            if name=='get-tickets':
                attempts.append(arguments)
                return CallToolResult(content=[TextContent(type='text',text='<html>not ticket data</html>')])
            return await original(name,arguments)
        source.call=malformed
        result=await RailClient(source).query('出发','到达','2026-10-30')
        self.assertEqual(result['error_code'],'invalid_response')
        self.assertEqual(len(attempts),1)

    async def test_default_prioritizes_g_and_explicit_high_speed_never_uses_d(self):
        d=deepcopy(ROW);d['start_train_code']='D1'
        g=deepcopy(ROW);g['start_train_code']='G2'
        source=Tickets([d,g]);rail=RailClient(source)
        result=await rail.query('出发','到达','2026-10-30',limit=1)
        self.assertEqual([r['train_code'] for r in result['trains']],['G2'])
        result=await rail.query('出发','到达','2026-10-30',train_preference='high_speed_only')
        self.assertEqual([r['train_code'] for r in result['trains']],['G2'])
        self.assertEqual(source.calls[-1][1]['trainFilterFlags'],'G')
        result=await rail.query('出发','到达','2026-10-30',train_preference='d_train')
        self.assertEqual([r['train_code'] for r in result['trains']],['D1'])

    async def test_midnight_uses_boarding_date_missing_fares_remain_unknown(self):
        source=Tickets()
        result=await RailClient(source).query('出发南','到达东','2026-10-30',origin_scope='station',destination_scope='station')
        self.assertEqual(result['status'],'ok')
        train=result['trains'][0]
        self.assertEqual(train['arrival_at'],'2026-10-31T01:00+08:00')
        self.assertIsNone(train['seats'][0]['price_cny'])
        self.assertEqual(train['seats'][0]['availability'],'unknown')
        self.assertEqual(source.calls[-1][1]['format'],'json')
        self.assertEqual(source.calls[-1][1]['trainFilterFlags'],'GD')

    async def test_exact_station_excludes_same_city_other_station(self):
        wrong=deepcopy(ROW);wrong['to_station_telecode']='CCC'
        source=Tickets([wrong,ROW])
        result=await RailClient(source).query('出发南站','到达东','2026-10-30',origin_scope='station',destination_scope='station',limit=1)
        self.assertEqual(len(result['trains']),1)
        self.assertEqual(result['trains'][0]['destination_station'],'到达东')
        source=Tickets([wrong])
        result=await RailClient(source).query('出发南','到达东','2026-10-30',origin_scope='station',destination_scope='station')
        self.assertEqual(result['status'],'empty')

    async def test_missing_exact_station_code_is_error_not_empty(self):
        row=deepcopy(ROW);del row['from_station_telecode']
        result=await RailClient(Tickets([row])).query('出发南','到达东','2026-10-30',origin_scope='station')
        self.assertEqual(result['error_code'],'invalid_response')

    async def test_candidate_cap_never_claims_no_trains(self):
        row=deepcopy(ROW);row['to_station_telecode']='CCC'
        result=await RailClient(Tickets([row]*100)).query('出发南','到达东','2026-10-30',destination_scope='station',limit=1)
        self.assertEqual(result['status'],'incomplete')

    async def test_city_scope_and_category_are_independent(self):
        row=deepcopy(ROW);row['start_train_code']='Z1'
        source=Tickets([row])
        rail=RailClient(source)
        self.assertEqual((await rail.query('出发市','到达市','2026-10-30'))['status'],'empty')
        result=await rail.query('出发市','到达市','2026-10-30',train_preference='all')
        self.assertEqual(result['status'],'ok')
        self.assertEqual(source.calls[0][0],'get-station-code-of-citys')
        self.assertEqual(result['city_coverage'],'provider_city_code_unverified')

    async def test_empty_and_invalid_requests_are_separate(self):
        source=Tickets([]);rail=RailClient(source)
        self.assertEqual((await rail.query('出发','到达','2026-10-30'))['status'],'empty')
        source.calls.clear()
        for day in ('明天','2026-2-01','2026-02-30'):
            self.assertEqual((await rail.query('出发','到达',day))['status'],'invalid_request')
        self.assertEqual(source.calls,[])

    async def test_provider_error_and_cancellation_are_not_empty_or_retried(self):
        class Broken:
            async def call(self,*args): raise MCPUnavailable('unavailable')
        self.assertEqual((await RailClient(Broken()).query('出发','到达','2026-10-30'))['status'],'error')
        class Cancelled:
            async def call(self,*args): raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await RailClient(Cancelled()).query('出发','到达','2026-10-30')

    def test_text_errors_multiple_blocks_structured_data_and_malformed(self):
        result=CallToolResult(content=[TextContent(type='text',text='['),TextContent(type='text',text=']')])
        self.assertEqual(response_json(result),[])
        for result in (CallToolResult(content=[TextContent(type='text',text='Error: no tickets data')]),
                       CallToolResult(isError=True,content=[])):
            with self.assertRaises(MCPUnavailable): response_json(result)

    async def test_known_seat_states_and_missing_prices(self):
        row=deepcopy(ROW)
        row['prices']=[{'seat_name':'测试','num':v,'price':float('nan')} for v in ('有','无','候补',None,0,3)]
        result=await RailClient(Tickets([row])).query('出发','到达','2026-10-30')
        self.assertEqual([s['availability'] for s in result['trains'][0]['seats']],
                         ['available','sold_out','waitlist','unknown','sold_out','available'])
        self.assertTrue(all(s['price_cny'] is None for s in result['trains'][0]['seats']))
