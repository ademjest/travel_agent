import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from adapters.onebot_app import OneBotReplyRenderer
from app.runtime_factory import build_runtime
from agents.travel_decision import decide_travel_action
from core.chat_transport import ChatEvent
from core.model_budget import MODEL_CALL
from core.settings import Settings
from core.tool_result import ToolResult
from infrastructure.memory_store import MemoryStore


REQUEST = '我今天下午六点半打算从武汉光谷8号小区去武汉杨家湾吃饭，告诉我出门需要带伞吗，还有去的路线是怎么样的，我打算坐地铁去'
ANSWER = '请随身带伞；地铁路线参考工具结果。天气仅为日级预报，不能确定18:30小区是否下雨。'


def call(name, arguments):
    return SimpleNamespace(id=name, function=SimpleNamespace(name=name,arguments=json.dumps(arguments,ensure_ascii=False)))


def response(content='', calls=()):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content,tool_calls=list(calls)))])


def intent(operations=None, action='read'):
    return {'schema_version':1,'action':action,'operations':operations if operations is not None else [
        {'domain':'forecast','operation':'query','location':'武汉'},
        {'domain':'transit','operation':'query','origin':'武汉光谷8号小区','destination':'武汉杨家湾','city':'武汉'}],
        'missing_fields':[],'requires_confirmation':False,'source_spans':['武汉','地铁','带伞']}


class FixtureClient:
    def __init__(self, ir, replies):
        self.ir,self.replies,self.requests=ir,list(replies),[]
        self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        purpose=MODEL_CALL.get().purpose
        self.requests.append((purpose,kwargs))
        if purpose=='intent_parse':
            return response(json.dumps(self.ir,ensure_ascii=False) if isinstance(self.ir,dict) else 'invalid JSON')
        return self.replies.pop(0)

    def close(self):
        pass


class CompoundTravelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=MemoryStore(Path(self.temp.name)/'test.db')
        self.env=patch.dict(os.environ,{'AI_SUMMARY_ENABLED':'false','AI_VECTOR_ENABLED':'false',
            'AI_DAILY_COST_LIMIT':'0','AI_MONTHLY_COST_LIMIT':'0','AI_DAILY_TOKEN_LIMIT':'0',
            'AI_MONTHLY_TOKEN_LIMIT':'0','LLM_INTENT_MODEL_ID':'','LLM_MODEL_PRICES':''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.calls=[]
        self.deliveries=[]

    def run_query(self, ir, replies=None, text=REQUEST):
        client=FixtureClient(ir,replies if replies is not None else [response(calls=[
            call('get_weather_forecast',{'location':'武汉'}),
            call('get_transit_route',{'origin':'武汉光谷8号小区','destination':'武汉杨家湾','city':'武汉'})]),response(ANSWER)])
        async def send(message):
            self.deliveries.append(message.payload['message'])
            return 'reply-'+str(len(self.deliveries))
        runtime=build_runtime(Settings('','',frozenset(),'','fixture','https://example.test','fixture'),
            platform='web',store=self.store,client=client,transport=SimpleNamespace(send=send),
            reply_renderer=OneBotReplyRenderer(),group_allowed=lambda _:True)
        def execute(name,args):
            self.calls.append((name,args))
            return ToolResult('completed',name,data='固定数据源快照：武汉日级小雨预报' if name=='get_weather_forecast' else '固定数据源快照：地铁路线已查到')
        runtime.application.travel_service.execute_result=execute
        asyncio.run(runtime.application.handle(ChatEvent('web','group','input','c','owner',text)))
        return client,runtime

    def test_original_sentence_runs_both_queries_after_one_intent_parse(self):
        client,_=self.run_query(intent())
        self.assertEqual(self.deliveries[-1],ANSWER)
        self.assertCountEqual(self.calls,[('get_weather_forecast',{'location':'武汉'}),
            ('get_transit_route',{'origin':'武汉光谷8号小区','destination':'武汉杨家湾','city':'武汉'})])
        self.assertEqual([purpose for purpose,_ in client.requests],['intent_parse','tool_reasoning','tool_answer'])
        prompt='\n'.join(str(message['content']) for message in client.requests[1][1]['messages'])
        self.assertIn('武汉光谷8号小区',prompt)
        self.assertIn('queries',prompt)
        self.assertIn('六点半',prompt)

    def test_explicit_two_city_rules_execute_both_without_intent_model(self):
        client,_=self.run_query(None,[],text='查询天气武汉；查询天气长沙')
        self.assertEqual(client.requests,[])
        self.assertCountEqual(self.calls,[('get_current_weather',{'location':'武汉'}),('get_current_weather',{'location':'长沙'})])

    def test_legacy_prefix_cannot_swallow_unknown_second_requirement(self):
        single=intent([{'domain':'weather','operation':'query','location':'武汉'}])
        single['source_spans']=['武汉']
        client,_=self.run_query(single,[],text='查询天气武汉；顺便推荐适合老人的玩法')
        self.assertEqual(self.calls,[])
        self.assertIn('没有执行',self.deliveries[-1])
        self.assertEqual(len(client.requests),2)

    def test_fallback_understands_umbrella_and_from_go_expression(self):
        decision=decide_travel_action(REQUEST)
        self.assertEqual(set(decision.intents),{'forecast','transit'})
        self.assertFalse(decision.needs_clarification)
        self.run_query(None)
        self.assertEqual(self.deliveries[-1],ANSWER)
        self.assertEqual(len(self.calls),2)

    def test_redundant_missing_input_is_not_sent_when_all_fields_are_known(self):
        self.run_query(intent(),[response(calls=[call('request_missing_input',{
            'question':'请告诉我出行起点和终点，以及所在城市。','missing_fields':['origin','destination','city']})]),
            response(calls=[call('get_weather_forecast',{'location':'武汉'}),
                call('get_transit_route',{'origin':'武汉光谷8号小区','destination':'武汉杨家湾','city':'武汉'})]),response(ANSWER)])
        self.assertEqual(self.deliveries[-1],ANSWER)
        self.assertEqual(len(self.calls),2)

    def test_genuine_missing_origin_is_asked_without_losing_known_fields(self):
        ir=intent([{'domain':'forecast','operation':'query','location':'武汉'},
            {'domain':'transit','operation':'query','destination':'武汉杨家湾','city':'武汉'}],action='clarify')
        self.run_query(ir,[response(calls=[call('request_missing_input',{
            'question':'你准备从哪里出发？','missing_fields':['origin']})])],
            text='今天武汉出门需要带伞吗？我想坐地铁去武汉杨家湾')
        self.assertEqual(self.deliveries[-1],'你准备从哪里出发？')
        with self.store._connect() as db:
            row=db.execute('SELECT * FROM travel_tasks').fetchone()
        self.assertEqual(json.loads(row['missing_slots_json']),['origin'])
        slots=json.loads(row['slots_json'])
        self.assertEqual((slots['location'],slots['destination'],slots['city']),('武汉','武汉杨家湾','武汉'))

    def test_read_intent_cannot_open_reservation_write_tools(self):
        self.run_query(intent(),[response(calls=[call('cancel_reservation_plan',{'plan_code':'R-20300101-001'})]),
            response(calls=[call('get_weather_forecast',{'location':'武汉'}),
                call('get_transit_route',{'origin':'武汉光谷8号小区','destination':'武汉杨家湾','city':'武汉'})]),response(ANSWER)])
        self.assertEqual(self.deliveries[-1],ANSWER)
        self.assertNotIn('cancel_reservation_plan',[name for name,_ in self.calls])

    def test_unsupported_or_ungrounded_ir_is_not_injected_as_verified_input(self):
        ir=intent()
        ir['operations'][1]['origin']='武汉天河机场'
        client,_=self.run_query(ir)
        self.assertEqual(self.deliveries[-1],ANSWER)
        prompt='\n'.join(str(m['content']) for m in client.requests[1][1]['messages'])
        self.assertNotIn('武汉天河机场',prompt)

    def test_mixed_read_write_ir_does_not_become_a_read_handoff(self):
        ir=intent()
        ir['operations'].append({'domain':'booking','operation':'cancel_plan'})
        client,_=self.run_query(ir)
        self.assertIn('尚未执行任何一项',self.deliveries[-1])
        self.assertIn('预约',self.deliveries[-1])
        self.assertEqual(self.calls,[])
        self.assertEqual([purpose for purpose,_ in client.requests],['intent_parse'])

    def test_weather_only_answer_cannot_silently_drop_the_route_query(self):
        self.run_query(intent(),[response(calls=[call('get_weather_forecast',{'location':'武汉'})]),
            response('请带伞。'),response(calls=[call('get_transit_route',{
                'origin':'武汉光谷8号小区','destination':'武汉杨家湾','city':'武汉'})]),response(ANSWER)])
        self.assertEqual(self.deliveries[-1],ANSWER)
        self.assertEqual(len(self.calls),2)

    def test_explicit_new_compound_request_does_not_take_over_unrelated_waiting_task(self):
        from core.tasks import TaskUpdate
        previous=ChatEvent('web','group','old','c','owner','查西宁地铁')
        self.store.tasks.prepare_result(previous,self.store.begin_event(previous.event_key),
            TaskUpdate('transit','collecting',previous.content,{'city':'西宁'},('origin','destination')),'请补充')
        self.run_query(intent())
        with self.store._connect() as db:
            rows=db.execute('SELECT status,initial_request FROM travel_tasks ORDER BY rowid').fetchall()
        self.assertEqual(len(rows),2)
        self.assertEqual((rows[0]['status'],rows[0]['initial_request']),('collecting','查西宁地铁'))
        self.assertEqual(rows[1]['status'],'completed')

    def test_umbrella_need_survives_when_intent_parser_only_returns_transit(self):
        ir=intent()
        ir['operations']=ir['operations'][1:]
        self.run_query(ir)
        self.assertEqual(self.deliveries[-1],ANSWER)
        self.assertEqual({name for name,_ in self.calls},{'get_weather_forecast','get_transit_route'})

    def test_known_endpoints_cannot_be_changed_before_parallel_execution(self):
        self.run_query(intent(),[response(calls=[call('get_weather_forecast',{'location':'武汉'}),
            call('get_transit_route',{'origin':'武汉天河机场','destination':'武汉杨家湾','city':'武汉'})]),
            response(calls=[call('get_transit_route',{'origin':'武汉光谷8号小区','destination':'武汉杨家湾','city':'武汉'})]),response(ANSWER)])
        self.assertEqual(self.deliveries[-1],ANSWER)
        self.assertEqual(len(self.calls),2)
        self.assertNotIn('武汉天河机场',str(self.calls))

    def test_numeric_address_reply_completes_same_task_and_keeps_city_destination(self):
        ir=intent([{'domain':'forecast','operation':'query','location':'武汉'},
            {'domain':'transit','operation':'query','destination':'武汉杨家湾','city':'武汉'}],action='clarify')
        client,runtime=self.run_query(ir,[response(calls=[call('request_missing_input',{
            'question':'你准备从哪里出发？','missing_fields':['origin']})])],
            text='今天武汉出门需要带伞吗？我想坐地铁去武汉杨家湾')
        client.ir=intent([{'domain':'forecast','operation':'query'},
            {'domain':'transit','operation':'query','origin':'武汉光谷8号小区'}])
        client.ir['source_spans']=['武汉光谷8号小区']
        client.replies=[response(calls=[call('get_weather_forecast',{'location':'武汉'}),
            call('get_transit_route',{'origin':'武汉光谷8号小区','destination':'武汉杨家湾','city':'武汉'})]),response(ANSWER)]
        asyncio.run(runtime.application.handle(ChatEvent('web','group','answer','c','owner','武汉光谷8号小区')))
        self.assertEqual(self.deliveries[-1],ANSWER)
        with self.store._connect() as db:
            rows=db.execute('SELECT status,version,slots_json FROM travel_tasks').fetchall()
        self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]['status'],rows[0]['version']),('completed',2))
        slots=json.loads(rows[0]['slots_json'])
        self.assertEqual((slots['origin'],slots['destination'],slots['city']),('武汉光谷8号小区','武汉杨家湾','武汉'))


if __name__=='__main__':
    unittest.main()
