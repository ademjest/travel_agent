import unittest
import json
from pathlib import Path
from unittest.mock import Mock, patch

from infrastructure.amap_client import AmapClient, AmapError
from infrastructure.place_resolution import matches


ORIGIN='华中科技大学启明门'
DESTINATION='武汉东西湖区航天信息产业研发中心'


def poi(name, identifier, location, *, city='武汉市', district='洪山区', adcode='420111', address='', kind='科教文化服务'):
    return {'id':identifier,'name':name,'location':location,'cityname':city,'adname':district,
            'adcode':adcode,'address':address,'type':kind,'pname':'湖北省' if city=='武汉市' else '广东省'}


class RoutePlaceResolutionTests(unittest.TestCase):
    def setUp(self):
        self.client=AmapClient('offline-fixture')
        self.campus=poi('华中科技大学','campus','114.40,30.50',kind='科教文化服务;学校;高等院校')
        self.gates=[poi('华中科技大学(启明门)','gate','114.41,30.50',kind='通行设施;门')]
        self.destinations=[poi('航天信息产业研发中心','building','114.12,30.67',district='东西湖区',adcode='420112',kind='商务住宅;办公楼')]
        self.calls=[]
        self.tips=[]
        self.client._get=Mock(side_effect=self.get)
        sleeper=patch('infrastructure.amap_client.time.sleep',return_value=None)
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def get(self,path,params):
        self.calls.append((path,params))
        if path=='/v3/assistant/inputtips':
            return {'status':'1','tips':self.tips}
        if path=='/v3/place/text':
            if params['keywords']==ORIGIN.replace('启明门',''):
                return {'status':'1','pois':[self.campus]}
            if '启明门' in params['keywords']:
                return {'status':'1','pois':self.gates}
            return {'status':'1','pois':self.destinations}
        if path=='/v3/geocode/geo':
            address=params['address']
            if '启明门' in address:
                return {'status':'1','geocodes':[{'formatted_address':'广东省佛山市三水区启明门','location':'112.90,23.10','adcode':'440607','city':'佛山市','level':'兴趣点'}]}
            return {'status':'1','geocodes':[{'formatted_address':'湖北省武汉市东西湖区','location':'114.10,30.68','adcode':'420112','city':'武汉市','level':'区县'}]}
        return {'status':'1','route':{'paths':[{'distance':'40000','cost':{'duration':'2700','tolls':'0'}}]}}

    def assert_no_route_request(self):
        self.assertFalse(any(path.startswith('/v5/direction') or path.startswith('/v3/direction') for path,_ in self.calls))

    def test_unique_wrong_city_gate_is_rejected_before_route_api(self):
        self.gates=[poi('启明门','wrong','112.90,23.10',city='佛山市',district='三水区',adcode='440607')]
        with self.assertRaises(AmapError):
            self.client.driving_route(ORIGIN,DESTINATION)
        self.assert_no_route_request()

    def test_same_city_unrelated_gate_is_not_accepted_as_university_gate(self):
        self.gates=[poi('启明门','other','114.41,30.50',address='某小区')]
        with self.assertRaises(AmapError):
            self.client.driving_route(ORIGIN,DESTINATION)
        self.assert_no_route_request()

    def test_campus_center_cannot_replace_specific_gate(self):
        self.gates=[self.campus]
        with self.assertRaises(AmapError):
            self.client.driving_route(ORIGIN,DESTINATION)
        self.assert_no_route_request()

    def test_district_center_cannot_replace_research_building(self):
        self.destinations=[poi('东西湖区','district','114.10,30.68',district='东西湖区',adcode='420112',kind='地名地址信息;行政地名')]
        with self.assertRaises(AmapError):
            self.client.driving_route(ORIGIN,DESTINATION)
        self.assert_no_route_request()

    def test_verified_poi_coordinates_are_used_and_original_query_is_preserved(self):
        route=self.client.driving_route(ORIGIN,DESTINATION)
        self.assertEqual(route.origin.query,ORIGIN)
        self.assertIn('华中科技大学',route.origin.address)
        self.assertIn('航天信息产业研发中心',route.destination.address)
        params=[params for path,params in self.calls if path=='/v5/direction/driving'][0]
        self.assertEqual(params['origin'],'114.410000,30.500000')
        self.assertEqual(params['destination'],'114.120000,30.670000')
        self.assertTrue(any(params.get('city')=='武汉市' and params.get('citylimit')=='true'
                            for path,params in self.calls if path=='/v3/place/text' and '启明门' in params['keywords']))

    def test_multiple_qualified_gates_require_confirmation(self):
        self.gates.append(poi('华中科技大学(启明门)','gate-2','114.42,30.50',kind='通行设施;门'))
        with self.assertRaises(AmapError):
            self.client.driving_route(ORIGIN,DESTINATION)
        self.assert_no_route_request()

    def test_foreign_candidate_is_filtered_even_if_citylimit_provider_returns_it(self):
        self.gates.append(poi('华中科技大学启明门','bad','112.90,23.10',city='佛山市',district='三水区',adcode='440607',kind='通行设施;门'))
        route=self.client.driving_route(ORIGIN,DESTINATION)
        self.assertIn('武汉',route.origin.address)
        self.assertNotIn('佛山',route.origin.address)

    def test_correction_text_can_use_linking_particle_without_losing_parent(self):
        route=self.client.driving_route('华中科技大学的启明门',DESTINATION)
        self.assertEqual(route.origin.query,'华中科技大学的启明门')
        self.assertIn('华中科技大学',route.origin.address)

    def test_same_city_implausible_route_is_not_published(self):
        original=self.get
        def get(path,params):
            if path=='/v5/direction/driving':
                return {'status':'1','route':{'paths':[{'distance':'1018900','cost':{'duration':'38640'}}]}}
            return original(path,params)
        self.client._get.side_effect=get
        with self.assertRaises(AmapError) as error:
            self.client.driving_route(ORIGIN,DESTINATION)
        self.assertEqual(error.exception.code,'route_data_inconsistent')

    def test_legitimate_cross_city_route_is_not_forced_into_origin_city(self):
        original=self.get
        self.destinations=[poi('航天信息产业研发中心','foreign','113.30,23.10',city='广州市',district='天河区',adcode='440106',kind='商务住宅;办公楼')]
        def get(path,params):
            if path=='/v3/geocode/geo' and params['address']=='广州天河区':
                self.calls.append((path,params))
                return {'status':'1','geocodes':[{'formatted_address':'广东省广州市天河区','location':'113.3,23.1','adcode':'440106','city':'广州市','level':'区县'}]}
            if path=='/v5/direction/driving':
                self.calls.append((path,params))
                return {'status':'1','route':{'paths':[{'distance':'1018900','cost':{'duration':'38640'}}]}}
            return original(path,params)
        self.client._get.side_effect=get
        route=self.client.driving_route(ORIGIN,'广州天河区航天信息产业研发中心')
        self.assertEqual((route.origin.city,route.destination.city),('武汉市','广州市'))
        self.assertEqual(route.distance_meters,1018900)

    def test_transit_and_walking_use_the_same_verified_endpoint_resolver(self):
        self.gates=[poi('启明门','wrong','112.90,23.10',city='佛山市',district='三水区',adcode='440607')]
        for mode in ('walking','transit'):
            with self.subTest(mode=mode):
                with self.assertRaises(AmapError):
                    self.client.non_driving_route(ORIGIN,DESTINATION,mode=mode,city='武汉')
                self.assert_no_route_request()

    def test_gate_description_does_not_require_exact_redundant_wording_in_poi_name(self):
        route=self.client.driving_route('华中科技大学的启明门校门',DESTINATION)
        self.assertEqual(route.origin.poi_id,'gate')
        self.assertEqual(route.origin.query,'华中科技大学的启明门校门')

    def test_hyphen_and_city_linking_particle_are_normalized(self):
        self.assertTrue(matches('华中科技大学-南大门',poi('华中科技大学(南大门)','gate','114.41,30.50')))
        self.assertTrue(matches('武汉的航天信息产业研发中心',self.destinations[0]))

    def test_real_inputtips_gate_is_used_when_text_search_returns_campus(self):
        self.gates=[self.campus]
        self.tips=[{'id':'B0FFG0R3RE','name':'华中科技大学东校区(启明门)',
                    'address':'喻家湖东路','district':'湖北省武汉市洪山区','adcode':'420111',
                    'location':'114.428533,30.509527','typecode':'991400'},
                   {'id':'B0MUYRQYS7','name':'华中科技大学启明学院启明门',
                    'address':'珞喻路1037号华中科技大学','district':'湖北省武汉市洪山区','adcode':'420111',
                    'location':'114.429825,30.509375','typecode':'991400'}]
        route=self.client.driving_route(ORIGIN,DESTINATION)
        self.assertEqual(route.origin.poi_id,'B0FFG0R3RE')
        self.assertEqual(route.origin.coordinates,'114.428533,30.509527')

    def test_inputtips_requires_valid_city_type_id_and_coordinates(self):
        self.gates=[self.campus]
        valid={'id':'gate','name':'华中科技大学(启明门)','address':'喻家湖东路',
               'district':'湖北省武汉市洪山区','adcode':'420111','location':'114.428533,30.509527','typecode':'991400'}
        for changes in ({'district':'广东省佛山市三水区'},{'id':[]},{'location':[]},{'typecode':'150700'},
                        {'name':'启明门'},{'location':'nan,30.5'}):
            with self.subTest(changes=changes):
                self.tips=[{**valid,**changes}]
                with self.assertRaises(AmapError):
                    self.client.driving_route(ORIGIN,DESTINATION)
                self.assert_no_route_request()

    def test_gate_cannot_be_replaced_by_shop_with_gate_in_address(self):
        self.gates=[poi('开锁店','shop','114.41,30.50',address='华中科技大学启明门对面',kind='生活服务')]
        with self.assertRaises(AmapError):
            self.client.driving_route(ORIGIN,DESTINATION)
        self.assert_no_route_request()

    def test_explicit_gate_address_is_retained_and_checked(self):
        self.gates=[poi('华中科技大学(南大门)','south','114.413517,30.507940',address='珞喻路1037号华中科技大学')]
        self.client.search_places=Mock(side_effect=lambda city,keyword,**kwargs: (self.campus,) if keyword=='华中科技大学' else tuple(self.gates))
        query='华中科技大学-南大门，具体是武汉市洪山区珞喻路1037号'
        from infrastructure.place_resolution import resolve_route_location
        self.assertEqual(resolve_route_location(self.client,query).query,query)
        with self.assertRaises(AmapError):
            resolve_route_location(self.client,'华中科技大学-南大门，具体是佛山市三水区珞喻路1037号')

    def test_similar_institution_is_suggested_but_not_silently_accepted(self):
        self.destinations=[poi('航天三院信息产业研发中心','B0J6NHHO7R','114.126456,30.671173',
                               district='东西湖区',adcode='420112',address='滨河北路9号',kind='科教文化服务;科研机构')]
        with self.assertRaises(AmapError) as error:
            self.client.driving_route(ORIGIN,DESTINATION)
        self.assertIn('航天三院信息产业研发中心',str(error.exception))
        self.assertIn('终点',str(error.exception))
        self.assert_no_route_request()

    def test_city_prefix_does_not_require_city_adcode_to_equal_district_adcode(self):
        original=self.get
        def get(path,params):
            if path=='/v3/geocode/geo' and params['address'] in {'武汉','武汉市'}:
                return {'status':'1','geocodes':[{'formatted_address':'湖北省武汉市','location':'114.3,30.6',
                                                'adcode':'420100','city':'武汉市','level':'市'}]}
            return original(path,params)
        self.client._get.side_effect=get
        for query in ('武汉的航天信息产业研发中心','武汉市航天信息产业研发中心'):
            with self.subTest(query=query):
                route=self.client.driving_route(ORIGIN,query)
                self.assertEqual(route.destination.adcode,'420112')

    def test_resolution_log_distinguishes_filtered_results(self):
        self.gates=[self.campus]
        with self.assertLogs('infrastructure.place_resolution',level='INFO') as captured:
            with self.assertRaises(AmapError):
                self.client.driving_route(ORIGIN,DESTINATION)
        self.assertIn('name_mismatch',''.join(captured.output))
        self.assertIn('inputtips',''.join(captured.output))

    def test_route_search_requests_adcode_fields_absent_from_base_response(self):
        original=self.get
        def get(path,params):
            data=original(path,params)
            if path=='/v3/place/text' and params['extensions']=='base':
                data={**data,'pois':[{key:value for key,value in result.items() if key!='adcode'} for result in data['pois']]}
            return data
        self.client._get.side_effect=get
        self.assertEqual(self.client.driving_route(ORIGIN,DESTINATION).destination.adcode,'420112')
        self.assertTrue(all(params['extensions']=='all' for path,params in self.calls if path=='/v3/place/text'))


class LiveRouteSnapshotTests(unittest.TestCase):
    """Replay real response shapes without spending API quota during unit tests."""
    def setUp(self):
        fixture=json.loads((Path(__file__).parent/'fixtures'/'amap_route_places_2026-10-11.json').read_text(encoding='utf-8'))
        self.responses={(item['path'],json.dumps(item['params'],sort_keys=True,ensure_ascii=False)):
                        {'status':'1',**{key:item[key] for key in ('pois','tips','geocodes') if key in item}}
                        for item in fixture['requests']}
        self.client=AmapClient('offline-live-snapshot')
        self.client._get=Mock(side_effect=lambda path,params:self.responses[(path,json.dumps(params,sort_keys=True,ensure_ascii=False))])
        sleeper=patch('infrastructure.amap_client.time.sleep',return_value=None)
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_real_gate_and_research_center_responses_are_accepted(self):
        from infrastructure.place_resolution import resolve_route_location
        for query,identifier in [('华中科技大学启明门','B0FFG0R3RE'),('华中科技大学-南大门','B001B0JWWZ'),
                                 ('华中科技大学-南大门，具体是武汉市洪山区珞喻路1037号','B001B0JWWZ'),
                                 ('武汉的航天三院信息产业研发中心','B0J6NHHO7R')]:
            with self.subTest(query=query):
                location=resolve_route_location(self.client,query)
                self.assertEqual((location.poi_id,location.city),(identifier,'武汉市'))
                self.assertTrue(location.adcode)

    def test_real_short_institution_name_requires_confirmation(self):
        from infrastructure.place_resolution import resolve_route_location
        with self.assertRaises(AmapError) as error:
            resolve_route_location(self.client,'武汉东西湖区的航天信息产业研发中心')
        self.assertIn('航天三院信息产业研发中心',str(error.exception))
        self.assertIn('确认',str(error.exception))


if __name__=='__main__':
    unittest.main()
