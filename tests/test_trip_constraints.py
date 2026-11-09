from copy import deepcopy
import unittest

from app.request_router import parse_rules
from tools.trip.constraints import apply_constraints, edit_constraints, edit_text, proposal_metadata


class TripConstraintTests(unittest.TestCase):
    def test_hotel_budget_defaults_are_server_owned_and_keep_original_amount(self):
        text='我希望酒店能高档一点，300-400价格区间，尽量是连锁酒店'
        for unit in (None, '', '人民币每间每晚', '元/间/晚', 'CNY/间/晚'):
            change={'value':'300-400','scope':'accommodation','source':'300-400'}
            if unit is not None:change['unit_text']=unit
            spec,summary=apply_constraints({}, {'budget':change}, text)
            budget=spec['budgets']['accommodation']
            self.assertEqual(budget['value'],'300-400')
            self.assertEqual(budget['source'],'300-400')
            self.assertEqual(budget['unit_text'],'人民币每间每晚')
            self.assertEqual(budget['unit_defaults'],{'currency':'人民币','basis':'每间','period':'每晚'})
            self.assertIn('默认',summary[0])
        for value in ('300-400元','人民币300-400','CNY 300-400'):
            spec,_=apply_constraints({}, {'budget':{'value':value,'scope':'accommodation','source':'300-400'}},text)
            self.assertEqual(spec['budgets']['accommodation']['value'],'300-400')
            self.assertEqual(spec['budgets']['accommodation']['unit_text'],'人民币每间每晚')

    def test_hotel_budget_partial_explicit_units_and_inherited_units_win(self):
        for unit,expected,defaults in (
            ('美元','美元每间每晚',{'basis':'每间','period':'每晚'}),
            ('每间每晚','人民币每间每晚',{'currency':'人民币'}),
            ('美元每人每天','美元每人每天',{}),
            ('人民币总额','人民币总额',{}),
            ('人民币（CNY）每间每晚','人民币（CNY）每间每晚',{}),
            ('迪拉姆每间每晚','迪拉姆每间每晚',{}),
            ('人民币/间/晚（每间每晚）','人民币/间/晚（每间每晚）',{}),
        ):
            with self.subTest(unit=unit):
                text='酒店预算300-400，'+unit
                spec,_=apply_constraints({}, {'budget':{'value':'300-400','scope':'accommodation','source':'300-400','unit_text':unit}},text)
                self.assertEqual(spec['budgets']['accommodation']['unit_text'],expected)
                self.assertEqual(spec['budgets']['accommodation'].get('unit_defaults',{}),defaults)
        previous={'budgets':{'accommodation':{'value':'100-200','unit_text':'美元每人每天'}}}
        spec,_=apply_constraints(previous,{'budget':{'value':'300-400','scope':'accommodation','source':'300-400'}},'酒店300-400')
        self.assertEqual(spec['budgets']['accommodation']['unit_text'],'美元每人每天')
        for unit in ('人民币每人每天','人民币每间每小时'):
            previous={'budgets':{'accommodation':{'value':'100-200','unit_text':unit}}}
            spec,_=apply_constraints(previous,{'budget':{'value':'300-400','scope':'accommodation','source':'300-400','unit_text':'美元'}},'酒店300-400美元')
            self.assertEqual(spec['budgets']['accommodation']['unit_text'],unit.replace('人民币','美元'))

    def test_hotel_defaults_do_not_authorize_fabricated_amount_or_foreign_unit(self):
        for patch in ({'value':'300-500'},{'value':'300-500元'},{'value':'300-400美元'},{'unit_text':'美元每间每晚'},{'unit_text':'人民币每人每天'}):
            change={'value':'300-400','scope':'accommodation','source':'300-400',**patch}
            with self.subTest(patch=patch),self.assertRaises(ValueError):
                apply_constraints({}, {'budget':change},'酒店300-400')
        spec,_=apply_constraints({}, {'budget':{'value':'300-400','scope':'food','source':'300-400'}},'餐饮300-400')
        self.assertNotIn('unit_text',spec['budgets']['food'])

    def test_other_budget_currency_is_not_borrowed_for_lodging(self):
        text='酒店300-400，餐饮500美元每人每天'
        for quote in ('300-400',text):
            changes={'budget':{'value':'300-400','scope':'accommodation','source':quote}}
            spec,_=apply_constraints({},changes,text)
            self.assertEqual(spec['budgets']['accommodation']['unit_text'],'人民币每间每晚')
        first=deepcopy(spec)
        changed,_=apply_constraints(spec,{'budget':{'value':'400-500','scope':'accommodation','source':'400-500'}},'酒店改成400-500')
        self.assertEqual(changed['budgets']['accommodation']['unit_defaults'],spec['budgets']['accommodation']['unit_defaults'])
        self.assertEqual(spec,first)
        explicit,_=apply_constraints(changed,{'budget':{'value':'400-500','scope':'accommodation','source':'400-500','unit_text':'美元每人每天'}},'400-500美元每人每天')
        self.assertEqual(explicit['budgets']['accommodation']['unit_text'],'美元每人每天')
        self.assertNotIn('unit_defaults',explicit['budgets']['accommodation'])

    def test_explicit_arrival_answer_resolves_previous_ambiguous_time(self):
        from tools.trip.constraints import intercity_request
        previous={'inbound':{'date_text':'11月12日下午','time_reference':'ambiguous','departure_window':'晚上'}}
        value={'inbound':{'arrival_window':{'value':'下午','source':'下午到达'}}}
        resolved=intercity_request(value,previous,'下午到达')
        self.assertEqual(resolved['inbound']['time_reference'],'arrival')
        self.assertEqual(resolved['inbound']['arrival_window'],'下午')
        self.assertNotIn('departure_window',resolved['inbound'])

    def test_current_budget_overrides_only_same_category_default(self):
        previous={'budget_text':'住宿 每人每天 300 CNY','applied_preferences':{
            'budget':{'category':'住宿','basis':'每人','period':'每天','amount':300,'currency':'CNY'}}}
        changed,_=apply_constraints(previous,edit_constraints('hotel_budget','200-400'),edit_text('hotel_budget','200-400'))
        self.assertNotIn('budget_text',changed)
        self.assertNotIn('budget',changed['applied_preferences'])
        self.assertEqual(changed['budgets']['accommodation']['value'],'200-400')
        self.assertIn('budget_text',previous)

    def test_structured_and_language_edits_share_the_constraint_contract(self):
        for field, value in [('hotel_budget','320'), ('hotel_budget','300-500'), ('night','false')]:
            text = edit_text(field, value)
            expected = parse_rules(text).ir.operations[0].values['changes']
            self.assertEqual(edit_constraints(field, value), expected)

    def test_false_is_explicit_and_candidate_does_not_modify_saved_spec(self):
        previous = {'no_night':False, 'preferences':[]}
        original = deepcopy(previous)
        changed, _ = apply_constraints(previous, edit_constraints('night','false'), '不要夜游')
        self.assertTrue(changed['no_night'])
        restored, _ = apply_constraints(changed, {'night':{'value':True,'source':'可以夜游'}}, '可以夜游')
        self.assertFalse(restored['no_night'])
        self.assertEqual(previous, original)
        with self.assertRaises(ValueError):
            edit_constraints('unknown', 'false')

    def test_proposal_preparation_is_pure_and_requires_an_actual_plan(self):
        self.assertIsNone(proposal_metadata('介绍一下自己', '例如：第1天去公园，第2天去博物馆'))
        result = proposal_metadata('规划武汉两天行程', '武汉两天参考方案\n第1天公园\n第2天博物馆')
        self.assertEqual((result['destination'], result['duration_options']), ('武汉', [2]))
