from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import Mock
import json,unittest

from core.time_input import parse_time_window
from tools.travel_query import rail_parameters
from tools.trip.constraints import date_range,duration_days
from tools.trip.planner import TripPlanner,TripPlanError

NOW=datetime(2026,10,29,7,tzinfo=timezone.utc)
QUESTION='我想从武汉去南京玩两天，计划30号下午出发，11月1号下午回来，帮我规划一下旅程。'


def extract(text,value,previous=None,now=NOW):
    response=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value,ensure_ascii=False)))])
    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(return_value=response))))
    return TripPlanner(client,'scripted',None).extract(text,previous or {},now)


def original(duration='玩两天'):
    return {'destination':'南京','duration_text':duration,'start_date_text':'30号下午','end_date_text':'11月1号下午',
        'intercity':{'origin':{'value':'武汉','source':'从武汉去南京'},
          'scope':{'value':'round_trip','source':'30号下午出发，11月1号下午回来'},
          'day_count_basis':{'value':'destination_days','source':'南京玩两天'},
          'outbound':{'date_text':{'value':'30号下午','source':'30号下午出发'}},
          'inbound':{'date_text':{'value':'11月1号下午','source':'11月1号下午回来'}}}}


class SharedWindowTests(unittest.TestCase):
    def test_periods_and_aliases_are_shared_with_rail(self):
        for raw,expected in (('早上',(6,10)),('早晨',(6,10)),('上午',(9,12)),('下午',(12,18)),
            ('晚上',(18,24)),('晚间',(18,24)),('全天',(0,24))):
            with self.subTest(raw=raw):
                self.assertEqual(parse_time_window(raw),expected)
                args,missing=rail_parameters({'origin':'武汉','destination':'南京','travel_date':'10月30日','departure_window':raw},NOW)
                self.assertFalse(missing);self.assertEqual(args['departure_window'],expected)

    def test_clocks_minutes_and_inherited_units(self):
        for raw,expected in (('13点到18点',(13,18)),('13至18点',(13,18)),('下午1点到6点',(13,18)),
                ('13:30至18:00',(13.5,18)),('上午9点到下午3点',(9,15)),('晚上7点到11点',(19,23)),
                ('08:00至11:30',(8,11.5)),('18点前',(0,18)),('晚上前',(0,18))):
            with self.subTest(raw=raw):self.assertEqual(parse_time_window(raw),expected)

    def test_invalid_or_ambiguous_input_is_never_all_day(self):
        for raw in ('1点到6点','上午13点到18点','23点到1点','25点到26点','13:60至18:00','晚一点','全天但上午不行'):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):parse_time_window(raw)
                self.assertEqual(rail_parameters({'origin':'武汉','destination':'南京','travel_date':'10月30日','departure_window':raw},NOW)[1],('departure_window',))

    def test_duration_is_unique_and_not_a_calendar_day_or_fraction(self):
        for raw in ('两天','玩两天','游玩两天','三天两夜'):
            self.assertEqual(duration_days(raw),3 if '三' in raw else 2)
        for raw in ('两三天','两天半','0.5天','-2天','负两天','11月1日','两天后','两天或三天'):
            with self.subTest(raw=raw),self.assertRaises(ValueError):duration_days(raw)

    def test_date_ranges_keep_endpoint_precision(self):
        for raw,windows in (('10月30日下午至11月1日下午',((12,18),(12,18))),
                ('10月30日13点至11月1日18点',((13,13),(18,18))),
                ('10月30日晚间7点至11月1日晚间11点',((19,19),(23,23)))):
            interval=date_range(raw,NOW)
            self.assertEqual(interval['days'],3)
            self.assertEqual((interval['start_window'],interval['end_window']),windows)


class TripWindowContractTests(unittest.TestCase):
    def test_ambiguous_return_preserves_date_without_a_departure_assumption(self):
        value=original()
        value['intercity']['inbound']['time_reference']={'value':'ambiguous','source':'下午回来'}
        spec=extract(QUESTION,value)
        back=TripPlanner.intercity_legs(spec,NOW)['inbound']
        self.assertEqual(back['travel_date'],'2026-11-01')
        self.assertEqual(back['time_reference'],'ambiguous')
        self.assertEqual(back['departure_window'],'')
        self.assertEqual(back['arrival_window'],'')

    def test_recorded_duration_variants_produce_identical_parameters(self):
        results=[]
        for duration in ('玩两天','两天'):
            spec=extract(QUESTION,original(duration))
            self.assertEqual((spec['day_count'],spec['destination_day_count']),(3,2))
            self.assertEqual((spec['start_date'],spec['end_date']),('2026-10-30','2026-11-01'))
            legs=TripPlanner.intercity_legs(spec,NOW)
            self.assertEqual([v['departure_window'] for v in legs.values()],['12:00至18:00']*2)
            results.append(legs)
        self.assertEqual(*results)

    def test_interval_without_duration_is_sufficient_and_not_user_supplied_days(self):
        text='从武汉去南京，10月30日下午至11月1日下午，帮我规划旅程'
        spec=extract(text,{'destination':'南京','start_date_text':'10月30日下午','end_date_text':'11月1日下午',
            'intercity':{'origin':{'value':'武汉','source':'从武汉去南京'}}})
        self.assertEqual(spec['day_count'],3)
        self.assertNotIn('requested_day_count',spec)
        legs=TripPlanner.intercity_legs(spec,NOW)
        self.assertEqual(legs['outbound']['travel_date'],'2026-10-30')
        self.assertEqual(legs['inbound']['arrival_window'],'12:00至18:00')
        later=extract('轻松一点',{'preferences':['轻松一点']},spec,datetime(2026,10,31,7,tzinfo=timezone.utc))
        self.assertEqual(later['start_date'],'2026-10-30')
        self.assertNotIn('requested_day_count',later)

    def test_explicit_calendar_wins_over_conflicting_duration(self):
        text='南京四天，10月30日下午到11月1日下午'
        spec=extract(text,{'destination':'南京','duration_text':'四天'})
        self.assertEqual((spec['day_count'],spec['requested_day_count']),(3,4))
        self.assertIn('按明确起止时间',spec['duration_adjustment'])
        self.assertEqual(spec['duration_text'],'四天')

    def test_new_duration_is_not_replaced_with_an_old_number(self):
        previous=extract('南京两天',{'destination':'南京','duration_text':'两天'})
        result=extract('改成玩三天',{'duration_text':'玩三天'},previous)
        self.assertEqual(result['day_count'],3)
        with self.assertRaises(TripPlanError):extract('改成两三天',{'duration_text':'两三天'},previous)

    def test_a_new_explicit_range_replaces_a_previous_calendar(self):
        old=extract('南京10月30日下午至11月1日下午',{'destination':'南京'})
        spec=extract('改为11月2日早上至11月3日晚上',{},old)
        self.assertEqual((spec['start_date'],spec['end_date'],spec['day_count']),('2026-11-02','2026-11-03',2))
        self.assertEqual(spec['duration_text'],'2天')
        self.assertNotIn('requested_day_count',spec)

    def test_same_date_reversed_time_is_invalid(self):
        with self.assertRaises(TripPlanError):extract('南京10月30日18点至10月30日13点',{'destination':'南京'})

    def test_return_window_changes_without_moving_the_outbound_day(self):
        from tools.trip.constraints import intercity_request
        spec=extract(QUESTION,original())
        initial=TripPlanner.intercity_legs(spec,NOW)
        spec['intercity_request']=intercity_request({'inbound':{'departure_window':{'value':'晚上','source':'晚上'}}},spec['intercity_request'],'晚上')
        changed=TripPlanner.intercity_legs(spec,NOW)
        self.assertEqual(initial['outbound'],changed['outbound'])
        self.assertEqual(changed['inbound']['departure_window'],'晚上')
        self.assertEqual(spec['planning_window']['end_window'],(18,24))

    def test_hard_end_cannot_expand_for_a_late_train(self):
        from test_intercity_planning import train
        spec=extract('南京四天，10月30日下午到11月1日下午',{'destination':'南京','duration_text':'四天'})
        spec['intercity_request']={'day_count_basis':'destination_days'}
        result={'inbound':{'status':'completed','candidates':[train('G1','南京','武汉','2026-11-01T17:00','2026-11-01T20:00')]}}
        TripPlanner.choose_transport(spec,result)
        self.assertEqual(result['completeness'],'conflict')
        self.assertEqual((spec['end_date'],spec['day_count']),('2026-11-01',3))
