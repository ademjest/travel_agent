from test_travel_agent import execution_policy
"""Facts, roles and uncertainty must survive a contradictory model response."""
from dataclasses import replace
import unittest

from tools.travel_query import project_read_answer
from core.tool_result import ToolResult
from core.settings import Settings
from agents.travel_agent import TravelAgent
from test_travel_agent import FakeClient,completion,assistant_message,tool_call


WEATHER=ToolResult('completed','get_current_weather',data='【当前天气】武汉\n天气：晴\n温度：20℃\n湿度：60%\n发布时间：2030-09-09 10:00',
                   payload={'temperature':'20','humidity':'60','weather':'晴','report_time':'2030-09-09 10:00'})
FORECAST=ToolResult('completed','get_weather_forecast',data='【天气预报】武汉\n发布时间：2030-09-09 10:00\n2030-09-10：白天晴，夜间雨；夜间8℃，白天20℃。')
ROUTE=ToolResult('completed','get_driving_route',data='起点：武汉站\n终点：湖北省博物馆\n距离：10.0 公里\n预计耗时：10 分钟\n查询时间：2030-09-09 10:00\n当前估计不代表未来班次。',
                 payload={'origin':{'query':'武汉站'},'destination':{'query':'湖北省博物馆'},'distance_meters':10000,'duration_seconds':600})
TRAFFIC=ToolResult('completed','get_route_traffic',data=ROUTE.data+'\n路况数据覆盖不足，不能据此判断道路是否封闭或保证安全。',payload=ROUTE.payload)


class ReadAnswerTests(unittest.TestCase):
    def test_model_values_units_dates_and_roles_never_replace_service_facts(self):
        for receipt,candidate,forbidden in [
            (WEATHER,'武汉当前40℃。','40℃'),(WEATHER,'武汉20°F。','20°F'),
            (WEATHER,'湿度为20%，气温60℃。','60℃'),
            (FORECAST,'2030-09-09白天8℃，夜间20℃。','2030-09-09白天8℃'),
            (ROUTE,'从湖北省博物馆到武汉站100公里，预计10小时。','100公里'),
            (TRAFFIC,'道路绝对安全且保证不封路。','绝对安全'),
            (replace(ROUTE,action='get_walking_route'),'步行预计10小时。','10小时'),
            (replace(ROUTE,action='get_transit_route'),'保证18:30有车准点发车。','18:30'),
        ]:
            with self.subTest(candidate=candidate):
                answer=project_read_answer(candidate,(receipt,))
                self.assertIn(receipt.data,answer.reply)
                self.assertNotIn(forbidden,answer.reply)

    def test_retains_supported_suggestion_and_blocks_factual_claim_in_advice(self):
        result=project_read_answer('武汉40℃。建议途中适当休息。建议道路绝对安全。建议收费五十元。',(WEATHER,))
        self.assertIn('建议途中适当休息',result.reply)
        self.assertNotIn('40℃',result.reply)
        self.assertNotIn('绝对安全',result.reply)
        self.assertNotIn('五十元',result.reply)
        self.assertEqual(result.status,'degraded')

    def test_tool_success_without_facts_does_not_certify_an_answer(self):
        for text in ('','查询成功','【当前天气】武汉'):
            result=project_read_answer('武汉40℃。',(ToolResult('completed','get_current_weather',data=text),))
            self.assertEqual(result.status,'unverified')
            self.assertNotIn('40℃',result.reply)

    def test_partial_failure_and_timestamp_stay_visible(self):
        failure=ToolResult('failed','get_driving_route',data='工具错误：路线数据暂不可用')
        result=project_read_answer('都已完成。',(WEATHER,failure))
        self.assertIn('2030-09-09 10:00',result.reply)
        self.assertIn('尚未完成',result.reply)
        self.assertIn('路线数据暂不可用',result.reply)

    def test_empty_success_cannot_borrow_facts_from_another_operation(self):
        empty=ToolResult('completed','get_driving_route',data='查询成功')
        result=project_read_answer('都已完成。',(WEATHER,empty))
        self.assertEqual(result.status,'degraded')
        self.assertIn('missing_facts',result.reasons)
        self.assertIn('20℃',result.reply)
        self.assertIn('尚未完成',result.reply)

    def test_payload_must_contain_domain_facts_not_just_metadata(self):
        invalid=[replace(WEATHER,payload={}),
                 replace(WEATHER,payload={'weather':'未知','temperature':'未知','report_time':'2030-09-09 10:00'}),
                 replace(ROUTE,payload={'request_id':'upstream-id'}),
                 replace(FORECAST,payload={'days':[{'day_temperature':'20'}]})]
        for receipt in invalid:
            with self.subTest(receipt=receipt):
                self.assertEqual(project_read_answer('',(receipt,)).status,'unverified')

    def test_known_empty_place_result_is_valid(self):
        receipt=ToolResult('completed','search_travel_places',data='未找到匹配地点。',payload={'places':[]})
        self.assertEqual(project_read_answer('',(receipt,)).status,'grounded')

    def test_long_facts_are_not_cut_into_misleading_fragments(self):
        result=project_read_answer('',(replace(WEATHER,data=WEATHER.data+'；限制条件'*2000,artifact_id='tool-owned'),))
        self.assertLess(len(result.reply),6500)
        self.assertIn('answer_budget',result.reasons)
        self.assertIn('tool-owned',result.reply)

    def test_real_agent_loop_handles_20_to_40_injection(self):
        client=FakeClient([completion(assistant_message(tool_calls=[tool_call('q','get_current_weather','{"location":"武汉"}')])),
                           completion(assistant_message('武汉当前40℃，适合出行。'))])
        agent=TravelAgent(Settings('','',frozenset(),'','offline','https://example.test','script'),lambda *args:WEATHER,client)
        result=agent.run('武汉现在天气怎么样', decision_override=execution_policy(('weather',), ('get_current_weather',)))
        self.assertIn('20℃',result.reply)
        self.assertNotIn('40℃',result.reply)
        self.assertEqual(result.answer_status,'degraded')
        self.assertEqual(len(client.completions.requests),2)


if __name__=='__main__':unittest.main()
