from copy import deepcopy
from datetime import datetime, timezone
import unittest

from tools.trip.planner import TripPlanner
from test_travel_agent import FakeClient
from test_trips import model_response


class TripPlannerContractTests(unittest.TestCase):
    def test_declined_categories_stay_disabled_when_extractor_omits_them(self):
        planner = TripPlanner(FakeClient([model_response({'destination':'南京','duration_text':'两天'})]), 'test', None)
        spec = planner.extract('南京两天，不需要酒店餐饮', {}, datetime(2030, 9, 1, tzinfo=timezone.utc))
        self.assertFalse(spec['hotel_requested'])
        self.assertFalse(spec['restaurant_requested'])

    def test_reference_text_does_not_reactivate_explicitly_disabled_recommendations(self):
        previous = {'destination':'武汉', 'duration_text':'两天', 'day_count':2,
                    'start_date_text':'', 'preferences':[], 'must_visit':[],
                    'hotel_requested':False, 'restaurant_requested':False}
        original = deepcopy(previous)
        planner = TripPlanner(FakeClient([model_response({'preferences':['轻松一点']})]), 'test', None)
        result = planner.extract('轻松一点', previous, datetime(2030, 9, 1, tzinfo=timezone.utc),
                                 '旅行资料介绍了酒店、餐厅和公园。')
        self.assertFalse(result['hotel_requested'])
        self.assertFalse(result['restaurant_requested'])
        self.assertEqual(previous, original)

    def test_explicit_structured_request_can_enable_a_previously_disabled_category(self):
        previous = {'destination':'武汉', 'duration_text':'两天', 'day_count':2,
                    'hotel_requested':False, 'preferences':[], 'must_visit':[]}
        planner = TripPlanner(FakeClient([model_response({
            'hotel':{'value':'需要住宿','source':'需要住宿','requested':True}})]), 'test', None)
        result = planner.extract('这次需要住宿', previous, datetime(2030, 9, 1, tzinfo=timezone.utc))
        self.assertTrue(result['hotel_requested'])
