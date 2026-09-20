from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta, timezone
import json
import re

from agents.trip_planner import TripPlanError
from core.tasks import TaskUpdate, location_answer, utc_now
from core.trip_updates import apply_constraints, MODES, PACE
from services.booking_reminder_service import DATE_PATTERN
from services.booking_policy import BookingPolicyResolver, PolicyUnavailable
from services.reminder_time import number, parse_reminder_time


TRIP_ID = r'TR-[a-f0-9]{12}'
CONFIRM_TRIP = {'确认行程修改', '确认行程提醒'}
TRIP_COMMANDS = {'查看行程', '查看我的行程', '我的行程', '取消行程', '重新查询行程预约规则',
                 '设置行程提醒', '按行程提醒预约', '刷新行程交通', '刷新行程预约规则', *CONFIRM_TRIP}


def trip_request(text):
    if re.search(r'R-\d{8}-\d{3}|A-\d{6}', text):
        return False
    stripped = re.sub(TRIP_ID, '', text).strip()
    return bool(stripped in TRIP_COMMANDS or re.search(
        r'(?:规划|制定|设计).{0,50}(?:行程|旅行|旅游)|安排.{0,40}(?:行程|[一二两三四五六七八九十\d]+[天日](?!后))|'
        r'(?:去|在).{1,20}(?:玩|游玩)[一二两三四五六七八九十\d]+天|'
        r'第[一二两三四五六七八九十\d]+天.{0,20}(?:改|换|不去|删除)|'
        r'(?:行程|旅程).{0,15}(?:改成|改为|调整|改到)|'
        r'(?:放到|挪到|放|挪)第[一二两三四五六七八九十\d]+天', text))


def trip_continuation(text, tasks):
    pending = [task for task in tasks if task.task_type == 'itinerary' and task.status == 'collecting']
    if text in CONFIRM_TRIP or text == '重新查询行程预约规则':
        candidates = [task for task in pending if 'confirmation' in task.missing_slots
                      and task.slots.get('candidate_trip')]
        trip_ids = {task.slots.get('trip_id') for task in candidates}
        if candidates and len(trip_ids) == 1:
            return candidates[0]
    if len(pending) != 1:
        return None
    task = pending[0]
    if text in CONFIRM_TRIP or text == '重新查询行程预约规则':
        return task
    if 'confirmation' in task.missing_slots and re.search(r'https://\S+', text):
        return task
    if ('confirmation' in task.missing_slots and not task.slots.get('candidate_trip', {}).get('spec', {}).get('start_date')
            and re.fullmatch(DATE_PATTERN + r'[。 ]*', text)):
        return task
    if 'destination' in task.missing_slots and location_answer(text):
        return task
    if 'duration' in task.missing_slots and re.fullmatch(r'[一二两三四五六七八九十\d]+[天日](?:游)?[。 ]*', text):
        return task
    if 'start_date' in task.missing_slots and (re.fullmatch(DATE_PATTERN + r'[。 ]*', text) or text == '先不定日期'):
        return task
    if 'trip_ref' in task.missing_slots and re.fullmatch(r'(?:第)?[一二两三四五六七八九十\d]+(?:个|条)?', text):
        return task
    if 'document_ref' in task.missing_slots and (re.fullmatch(r'(?:第)?[一二两三四五六七八九十\d]+(?:个|份)?', text)
            or text in task.slots.get('document_names', [])):
        return task
    if 'transport' in task.missing_slots and text in {'公共交通', '公交', '地铁', '步行', '驾车', '自驾', '开车'}:
        return task
    return None


class TripService:
    def __init__(self, store, planner, *, policy_resolver=None, clock=None):
        self.store = store
        self.planner = planner
        self.policy_resolver = policy_resolver or BookingPolicyResolver()
        self.clock = clock or utc_now

    @staticmethod
    def format_trip(trip, *, preview=False):
        spec = trip['spec']
        lines = [f"{'行程修改预览（尚未保存）' if preview else '旅行行程'}：{trip['title']}（{trip['trip_id']}，版本 {trip.get('version', 1)}）"]
        if spec.get('preferences'):
            lines.append(('规划约束（含所选资料）：' if spec.get('document_ids') else '你的要求：') + '；'.join(spec['preferences']))
        if spec.get('budget_text'):
            lines.append('预算目标：' + spec['budget_text'] + '；尚未核验实时房价、门票和交通费用。')
        if spec.get('applied_preferences'):
            from infrastructure.preference_repository import PreferenceRepository
            lines.append('已采用你的偏好：' + PreferenceRepository.describe(spec['applied_preferences']) + '（本次明确要求优先）。')
        if spec.get('pace'):
            lines.append('本次节奏：' + PACE[spec['pace']])
        transport = spec.get('transport_preferences', {})
        if transport:
            lines.append('本次交通：' + '；'.join(label + '、'.join(MODES[v] for v in transport.get(key, []))
                for key, label in (('preferred','优先'),('discouraged','尽量减少'),('forbidden','不使用')) if transport.get(key)))
        for key, label in (('indoor_days', '室内安排'), ('outdoor_days', '室外安排')):
            if spec.get(key):
                lines.append(label + '：' + '、'.join(f'第{day}天' for day in spec[key]))
        if spec.get('document_ids'):
            lines.append('需求参考文档：' + '、'.join('#' + value for value in spec['document_ids']))
        if spec.get('media_ids'):
            lines.append('需求参考图片：' + '、'.join('#' + value for value in spec['media_ids']))
        for day in trip['plan']['days']:
            lines.append(f"第 {day['day_index']} 天" + (f" · {day['date']}" if day['date'] else '（日期待定）'))
            if not day['activities']:
                lines.append('- 自由休整：按你的修改，暂不安排固定景点。')
            for activity in day['activities']:
                place = trip['sources'][activity['poi_id']]
                lines.append(f"- {activity['period']}：{place['name']}｜{place.get('address', '地址待核对')}；{activity['reason']}")
            for leg in day.get('legs', []):
                origin, destination = trip['sources'][leg['from_poi']]['name'], trip['sources'][leg['to_poi']]['name']
                mode = {'transit': '公共交通', 'walking': '步行', 'driving': '驾车'}[leg['mode']]
                if leg.get('error'):
                    lines.append(f'  {origin} → {destination}：{mode}段暂未核实，{leg["error"]}')
                else:
                    minutes = (leg['duration_seconds'] + 59) // 60
                    estimate = f'约 {minutes} 分钟' if minutes else '耗时尚未返回'
                    lines.append(f'  {origin} → {destination}：{mode}{estimate}（当前路网参考，出发当日需再核对）。')
            if spec.get('gentle'):
                lines.append('  建议在活动间安排休息，按实际体力减少步行。')
        for key, label in (('hotel_ids', '住宿候选'), ('restaurant_ids', '餐饮候选')):
            if trip['plan'].get(key):
                lines.append(label + '（地点资料，未核实实时价格和营业/库存）：')
                lines.extend(f"- {trip['sources'][poi_id]['name']}｜{trip['sources'][poi_id].get('address', '')}"
                             for poi_id in trip['plan'][key])
            elif spec.get('hotel_requested' if key == 'hotel_ids' else 'restaurant_requested'):
                lines.append(label + '：本次未查到可用地点，请补充地段或具体名称后再查询。')
        lines.extend(('地点依据：高德 POI 查询记录；开放时间、预约、价格和跨地点通行时间需另行核对。',
                      '可以说“第二天改为室内”“把行程改到10月2日开始”或“查看行程”。'))
        return '\n'.join(lines)

    def _task_update(self, task, event, slots, status, missing=()):
        return TaskUpdate('itinerary', status, task.initial_request if task else event.content,
                          slots, missing, task.task_id if task else '', task.version if task else None)

    def _question(self, task, event, claim, slots, missing, reply, *, trip=None, expected_trip=None, supersede_request=''):
        if 'confirmation' in missing and len(reply) > 7500:
            raise TripPlanError('本次行程与提醒变更太长，无法完整展示供核对。请减少本次修改范围后重试，尚未应用本次变更。')
        return self.store.trips.commit(event, claim, self._task_update(task, event, slots, 'collecting', missing),
                                       reply, trip=trip, expected_trip=expected_trip, supersede_request=supersede_request, now=self.clock())

    def update_constraints(self, event, claim, trip_id, expected_version, changes, *, source_text=None,
                           preference_update=None, semantic_task=None):
        current = self.store.trips.get(event, trip_id)
        if not current or current['status'] != 'active' or current['version'] != expected_version:
            raise TripPlanError('当前行程已经变化，请重新查看后再调整。')
        spec, summary = apply_constraints(current['spec'], changes, source_text or event.content)
        candidate = deepcopy(current)
        candidate.update(spec=spec, version=expected_version+1)
        if changes.keys() - {'transport'}:
            candidate['sources'] = {**current['sources'], **self.planner.collect_places(spec)}
            candidate['plan'] = self.planner.plan(spec, candidate['sources'], current['plan'], source_text or event.content)
        self.planner.add_route_evidence(candidate['plan'], spec, candidate['sources'], current['plan'])
        changes_to_reminders, notes, errors, fingerprints, _ = self._reminder_changes(event, candidate)
        slots = {'trip_id': trip_id, 'candidate_trip': candidate, 'expected_trip_version': expected_version,
                 'trip_already_saved': False, 'policy_fingerprints': fingerprints,
                 'linked_versions': {r['reminder_id']:r['version'] for r in self.store.trips.linked_reminders(event,trip_id)},
                 'reminder_versions': {r['reminder_id']:r['version'] for _,r in changes_to_reminders if 'version' in r}}
        if preference_update:
            slots['preference_update'] = preference_update
        if semantic_task:
            slots['semantic_task'] = {'task_id':semantic_task.task_id,'version':semantic_task.version}
        reply = ('已关联当前行程，保留原城市、日期与天数。本次仅调整这份行程，长期偏好未修改。\n'
                 + '；'.join(summary) + '\n\n' + self.format_trip(candidate, preview=True))
        if notes or errors:
            reply += '\n关联提醒尚未变更：\n' + '\n'.join([*notes,*errors])
        if preference_update:
            reply += '\n确认时同时保存长期偏好：' + self.store.preferences.describe(preference_update['values'])
        reply += '\n核对后回复“确认行程修改”；确认前仍使用原版本。'
        return self._question(None,event,claim,slots,('confirmation',),reply,
                              expected_trip=(trip_id,expected_version), supersede_request=event.content)

    def _select_trip(self, event, task, tasks):
        ids = re.findall(TRIP_ID, event.content)
        if ids:
            if len(set(ids)) != 1:
                raise TripPlanError('请一次选择一份行程。')
            trip = self.store.trips.get(event, ids[0])
            if not trip or trip['status'] != 'active':
                raise TripPlanError('没有找到你在本群拥有的这份有效行程。')
            return trip
        if task and task.slots.get('trip_id'):
            trip = self.store.trips.get(event, task.slots['trip_id'])
            if trip and trip['status'] == 'active':
                return trip
        if event.reply_to_id:
            for recent in tasks:
                if recent.task_type == 'itinerary' and recent.slots.get('trip_id'):
                    trip = self.store.trips.get(event, recent.slots['trip_id'])
                    if trip and trip['status'] == 'active':
                        return trip
        trips = self.store.trips.list_for_owner(event)
        if len(trips) == 1:
            return trips[0]
        return None

    def _reminder_changes(self, event, trip, *, force_refresh=False):
        linked = self.store.trips.linked_reminders(event, trip['trip_id'], include_cancelled=True)
        by_entity = {json.loads(row['source_json']).get('entity'): row for row in linked}
        targets = set(trip['spec'].get('reminder_entities', [])) | set(by_entity)
        changes, notes, errors, fingerprints = [], [], [], {}
        review = False
        for entity in sorted(target for target in targets if target):
            old = by_entity.get(entity)
            if old and old['status'] == 'cancelled':
                notes.append(f'{entity} 的提醒已由你取消，本次不会重新创建。')
                continue
            visits = [day['date'] for day in trip['plan']['days'] for activity in day['activities']
                      if trip['sources'][activity['poi_id']]['name'] == entity]
            if trip['status'] == 'cancelled' or not visits:
                if old and old['delivery_status'] != 'sent':
                    changes.append(('cancel', old))
                    notes.append(f"取消 {entity} 的关联提醒：该地点已不在本次行程中。")
                    review = True
                continue
            if len(visits) != 1 or not visits[0]:
                errors.append(f'{entity}：需要唯一、明确的参观日期。')
                continue
            visit_date = visits[0]
            old_source = json.loads(old['source_json']) if old else {}
            if old and old['delivery_status'] == 'sent':
                notes.append(f'{entity} 的历史提醒已发送，不会重新发送或撤回。')
                continue
            if old and old_source.get('visit_date') == visit_date and not force_refresh:
                continue
            if old and old_source.get('manual_time_override') and not force_refresh:
                source = {**old_source, 'visit_date': visit_date, 'trip_version': trip['version']}
                changes.append(('update', {**old, 'source': source,
                    'title': f'预约{entity}（参观日期 {visit_date}）'}))
                notes.append(f'{entity}：保留你人工指定的提醒时间，仅更新关联参观日期为 {visit_date}。')
                review = True
                continue
            try:
                policy = self.policy_resolver.resolve(entity, source_url=trip['spec'].get('policy_sources', {}).get(entity)
                    or old_source.get('policy', {}).get('source_url', ''))
                if date.fromisoformat(visit_date).weekday() in policy.closed_weekdays and not policy.holiday_exceptions:
                    raise PolicyUnavailable('该日期按常规安排闭馆，需要更换日期或核实特殊公告。')
                instant = policy.opening_at(date.fromisoformat(visit_date))
                if instant <= self.clock():
                    raise PolicyUnavailable('计算出的开约时刻已过，请现在核对预约渠道或指定其他提醒时间。')
                evidence = self.store.policies.record(policy)
            except PolicyUnavailable as exc:
                errors.append(f'{entity}：{exc}')
                continue
            fingerprints[entity] = policy.fingerprint
            if old and old_source.get('visit_date') == visit_date and force_refresh:
                keys = ('days_before', 'release_time', 'closed_weekdays', 'holiday_exceptions', 'caveats')
                previous = old_source.get('policy', {})
                if all(json.dumps(previous.get(key), sort_keys=True) == json.dumps(evidence.get(key), sort_keys=True) for key in keys):
                    notes.append(f'{entity}：已重新核查，适用规则无变化。')
                    continue
            source = {'kind': 'booking_policy', 'trip_id': trip['trip_id'], 'trip_version': trip['version'],
                      'entity': entity, 'visit_date': visit_date, 'policy': evidence, 'policy_fingerprint': policy.fingerprint}
            operation_key = f'{event.event_key}:trip:{trip["trip_id"]}:{entity}'
            reminder = {**(old or {}), 'reminder_id': old['reminder_id'] if old else self.store.reminders.new_id(operation_key),
                'source_operation_key': operation_key, 'title': f'预约{entity}（参观日期 {visit_date}）',
                'scheduled_at_utc': instant.astimezone(timezone.utc).isoformat(), 'source': source}
            if old_source.get('manual_time_override'):
                reminder['scheduled_at_utc'] = old['scheduled_at_utc']
                reminder['source']['manual_time_override'] = old_source['manual_time_override']
                notes.append(f"{entity}：保留人工提醒时间 {old['scheduled_at_utc']}（UTC）；以下为规则计算参考。")
            changes.append(('update' if old else 'create', reminder))
            timing_label = '规则推算参考' if old_source.get('manual_time_override') else '改为' if old else '拟定'
            notes.append(f"{entity}：参观 {visit_date}，{timing_label} {instant:%Y-%m-%d %H:%M}（北京时间）；来源 {policy.source_url}")
            if policy.caveats:
                notes.extend(policy.caveats)
            review = review or bool(old) or bool(policy.caveats)
        return changes, notes, errors, fingerprints, review

    def handle(self, event, claim, source_text_override='', force_confirmation=False):
        text = event.content.strip()
        tasks = self.store.tasks.recent(event.platform, event.storage_scope_id, event.sender_id, reply_to_id=event.reply_to_id)
        task = trip_continuation(text, tasks)
        if task is None and not trip_request(text):
            return None
        command = re.sub(TRIP_ID, '', text).strip()
        trips = self.store.trips.list_for_owner(event)
        if command == '我的行程':
            return '\n'.join(f"{trip['title']}（{trip['trip_id']}，版本 {trip['version']}）" for trip in trips) or '还没有保存的行程。'
        slots = deepcopy(task.slots) if task else {}
        if task and 'document_ref' in task.missing_slots:
            selection = re.fullmatch(r'(?:第)?([一二两三四五六七八九十\d]+)(?:个|份)?', text)
            index = number(selection[1]) - 1 if selection else -1
            options = slots['document_candidates']
            matched = [option for option in options if option['filename'] == text]
            if 0 <= index < len(options):
                selected = options[index]
            elif len(matched) == 1:
                selected = matched[0]
            else:
                return '请用列表中的序号选择具体文档版本。'
            slots['document_id'] = selected['id']
            text = slots['original_command']
            command = re.sub(TRIP_ID, '', text).strip()
        if task and 'trip_ref' in task.missing_slots:
            selection = re.fullmatch(r'(?:第)?([一二两三四五六七八九十\d]+)(?:个|条)?', text)
            index = number(selection[1]) - 1 if selection else -1
            ids = slots.get('trip_candidates', [])
            if not 0 <= index < len(ids):
                return '请回复列表中有效的行程序号。'
            slots['trip_id'] = ids[index]
            task = replace(task, slots=slots)
            text = slots['original_command']
            command = re.sub(TRIP_ID, '', text).strip()
        policy_link = bool(task and slots.get('candidate_trip') and re.search(r'https://\S+', text))
        pending_date = bool(task and slots.get('candidate_trip') and not slots['candidate_trip']['spec'].get('start_date')
                            and re.fullmatch(DATE_PATTERN + r'[。 ]*', text))
        pending_transport = bool(task and 'transport' in task.missing_slots)
        editing = bool(command in {'查看行程', '查看我的行程', '取消行程', *CONFIRM_TRIP, '重新查询行程预约规则',
                                    '设置行程提醒', '按行程提醒预约', '刷新行程交通', '刷新行程预约规则'} or policy_link or pending_date or pending_transport
                       or re.search(r'改|调整|换|挪|放到|放第|不去|删除', text))
        current = self._select_trip(event, task, tasks) if editing else None
        if editing and current is None:
            if not trips:
                return '还没有保存的行程。可以说“帮我规划武汉三天行程”。'
            slots.update(trip_candidates=[trip['trip_id'] for trip in trips], original_command=text)
            return self._question(task, event, claim, slots, ('trip_ref',),
                '要操作哪一份行程？回复序号：\n' + '\n'.join(f"{i}. {trip['title']}（{trip['trip_id']}）" for i, trip in enumerate(trips, 1)))
        if command in {'查看行程', '查看我的行程'}:
            reply = self.format_trip(current)
            if task and 'trip_ref' in task.missing_slots:
                return self.store.trips.commit(event, claim,
                    self._task_update(task, event, {'trip_id': current['trip_id']}, 'completed'), reply,
                    expected_trip=(current['trip_id'], current['version']), now=self.clock())
            return reply
        if command in CONFIRM_TRIP | {'重新查询行程预约规则'} and not (task and slots.get('candidate_trip')):
            return '没有仍然有效的行程变更预览。请先查看或修改行程，再确认具体变更。'
        if task and (command in CONFIRM_TRIP | {'重新查询行程预约规则'} or policy_link) and slots.get('candidate_trip'):
            candidate = slots['candidate_trip']
            if policy_link:
                urls = re.findall(r'https://[^\s，。<>]+', text)
                entities = [name for name in candidate['spec'].get('reminder_entities', []) if name in text]
                if not entities and len(candidate['spec'].get('reminder_entities', [])) == 1:
                    entities = candidate['spec']['reminder_entities']
                if len(urls) != 1 or len(entities) != 1:
                    return '请在链接前写明行程里的完整场馆名称，一次补充一个预约说明链接。'
                candidate['spec'].setdefault('policy_sources', {})[entities[0]] = urls[0]
            if current['version'] != slots['expected_trip_version']:
                raise TripPlanError('行程已变化，旧预览不能确认。请查看最新行程后重新修改。')
            linked_versions = {row['reminder_id']: row['version'] for row in self.store.trips.linked_reminders(event, current['trip_id'])}
            if linked_versions != slots.get('linked_versions', {}):
                raise TripPlanError('关联提醒集合已经变化，请重新查看并修改行程，不能使用旧预览。')
            changes, notes, errors, fingerprints, review = self._reminder_changes(event, candidate, force_refresh=slots.get('force_refresh', False))
            old_versions = slots.get('reminder_versions', {})
            if any(old_versions.get(row['reminder_id']) != row['version'] for _, row in changes if 'version' in row):
                raise TripPlanError('关联提醒已被修改，旧预览不能确认。请查看最新行程后重试。')
            if errors or policy_link or (slots.get('policy_fingerprints') != fingerprints):
                slots.update(policy_fingerprints=fingerprints, reminder_versions={row['reminder_id']: row['version'] for _, row in changes if 'version' in row})
                return self._question(task, event, claim, slots, ('confirmation',),
                    '规则或适用条件需要重新核对，尚未应用变更。\n' + '\n'.join([*notes, *errors]) + '\n核对后回复“确认行程提醒”。')
            if command == '重新查询行程预约规则':
                return self._question(task, event, claim, slots, ('confirmation',),
                    '\n'.join(notes) + '\n核对后回复“确认行程提醒”。')
            for _, reminder in changes:
                if reminder.get('source'):
                    reminder['source']['applicability_confirmed_by_user'] = True
            reply = ('已应用行程与提醒变更。\n' if not slots.get('trip_already_saved') else '已设置行程关联提醒。\n') + '\n'.join(notes)
            if slots.get('preference_update'):
                reply += '\n已同时保存长期偏好：' + self.store.preferences.describe(slots['preference_update']['values'])
            if not slots.get('trip_already_saved'):
                reply += '\n' + self.format_trip(candidate)
            return self.store.trips.commit(event, claim, self._task_update(task, event, {'trip_id': candidate['trip_id']}, 'completed'),
                reply, trip=None if slots.get('trip_already_saved') else candidate,
                expected_version=None if slots.get('trip_already_saved') else current['version'],
                expected_trip=(current['trip_id'], current['version']), reminder_changes=changes,
                preference_update=slots.get('preference_update'), now=self.clock())

        spec = deepcopy(current['spec'] if current else slots.get('spec', {}))
        candidate = deepcopy(current) if current else None
        targeted_day = re.search(r'第([一二两三四五六七八九十\d]+)天', text)
        transport_change = re.search(r'改(?:成|为).{0,8}(公共交通|公交|地铁|步行|驾车|自驾|开车)', text) if current else None
        if current and targeted_day and re.search(r'室内|室外', text):
            day_index = number(targeted_day[1])
            if not 1 <= day_index <= spec['day_count']:
                raise TripPlanError('这个天数不在当前行程内。')
            indoor = set(spec.get('indoor_days', []))
            outdoor = set(spec.get('outdoor_days', []))
            if '室内' in text:
                indoor.add(day_index)
                outdoor.discard(day_index)
            else:
                indoor.discard(day_index)
                outdoor.add(day_index)
            spec['indoor_days'] = sorted(indoor)
            spec['outdoor_days'] = sorted(outdoor)
            places = self.planner.collect_places(spec)
            places = {**current['sources'], **places}
            generated = self.planner.plan(spec, places, current['plan'], text)
            candidate.update(spec=spec, plan=generated, sources=places)
        elif current and targeted_day and re.search(r'不去|删除', text):
            target_index = number(targeted_day[1]) - 1
            if not 0 <= target_index < spec['day_count']:
                raise TripPlanError('目标天数不在当前行程中。')
            day = candidate['plan']['days'][target_index]
            matched = [activity for activity in day['activities'] if candidate['sources'][activity['poi_id']]['name'] in text]
            if len(matched) != 1:
                raise TripPlanError('请使用该天行程里的完整地点名称，明确要去掉哪一处。')
            activity = matched[0]
            name = candidate['sources'][activity['poi_id']]['name']
            day['activities'].remove(activity)
            spec.setdefault('activity_count_overrides', {})[str(target_index+1)] = len(day['activities'])
            spec['must_visit'] = [value for value in spec.get('must_visit', []) if value != name]
            if not day['activities']:
                spec['rest_days'] = sorted(set(spec.get('rest_days', [])) | {target_index+1})
            candidate['spec'] = spec
            candidate['plan'] = self.planner.validate_plan(candidate['plan'], spec, candidate['sources'])
        elif current and targeted_day and re.search(r'放到|挪到|放第|挪第', text):
            target_index = number(targeted_day[1]) - 1
            if not 0 <= target_index < spec['day_count']:
                raise TripPlanError('目标天数不在当前行程中。')
            matches = [(day, activity) for day in candidate['plan']['days'] for activity in day['activities']
                       if candidate['sources'][activity['poi_id']]['name'] in text]
            if len(matches) != 1:
                raise TripPlanError('请使用行程里的完整地点名称指定要移动的项目。')
            old_day, activity = matches[0]
            target_day = candidate['plan']['days'][target_index]
            if target_day is not old_day:
                old_day['activities'].remove(activity)
                used_periods = {item['period'] for item in target_day['activities']}
                if not old_day['activities'] or len(target_day['activities']) >= (2 if spec.get('gentle') else 3):
                    swapped = target_day['activities'].pop(0)
                    old_day['activities'].append({**swapped, 'period': activity['period']})
                    used_periods = {item['period'] for item in target_day['activities']}
                period = next(value for value in ('上午', '下午', '晚上') if value not in used_periods)
                target_day['activities'].append({**activity, 'period': period})
                spec.setdefault('activity_count_overrides', {}).update({
                    str(old_day['day_index']): len(old_day['activities']),
                    str(target_day['day_index']): len(target_day['activities'])})
            candidate['plan'] = self.planner.validate_plan(candidate['plan'], spec, candidate['sources'])
        elif current and (pending_date or re.search(r'行程.{0,15}(?:改到|改成|改为)', text)) and re.search(DATE_PATTERN, text):
            raw = re.search(DATE_PATTERN, text)[0]
            parsed = parse_reminder_time(raw, event.occurred_at or self.clock())
            if not parsed.day or parsed.error:
                raise TripPlanError(parsed.error or '请提供明确的行程开始日期。')
            spec.update(start_date=parsed.day, start_date_text=raw)
            if spec.get('end_date'):
                spec['end_date'] = (date.fromisoformat(parsed.day) + timedelta(days=spec['day_count']-1)).isoformat()
                spec['end_date_text'] = ''
            for day in candidate['plan']['days']:
                day['date'] = (date.fromisoformat(parsed.day) + timedelta(days=day['day_index']-1)).isoformat()
            candidate['spec'] = spec
        elif current and command == '取消行程':
            candidate['status'] = 'cancelled'
        elif current and command == '刷新行程预约规则':
            candidate['spec'] = spec
        elif current and command in {'设置行程提醒', '按行程提醒预约'}:
            spec['remind_booking'] = True
            spec['reminder_entities'] = spec.get('must_visit') or list(dict.fromkeys(
                candidate['sources'][activity['poi_id']]['name']
                for day in candidate['plan']['days'] for activity in day['activities']
                if re.search(r'博物馆|美术馆|风景名胜', candidate['sources'][activity['poi_id']].get('type', ''))))
            candidate['spec'] = spec
        elif current and (transport_change or pending_transport or command == '刷新行程交通'):
            if pending_transport:
                spec['transport_text'] = text
            elif transport_change:
                spec['transport_text'] = transport_change[1]
            if (transport_change or pending_transport) and 'transport_preferences' in spec:
                mode = 'transit' if re.search(r'公共交通|公交|地铁', spec['transport_text']) else 'walking' if '步行' in spec['transport_text'] else 'driving'
                spec['transport_preferences']['preferred'] = [mode]
                spec['transport_preferences']['forbidden'] = [v for v in spec['transport_preferences'].get('forbidden',[]) if v != mode]
            if not spec.get('transport_text'):
                return self._question(task, event, claim, {'trip_id': current['trip_id']}, ('transport',),
                    '这份行程打算使用公共交通、步行还是驾车？')
            candidate['spec'] = spec
        else:
            if task and text == '先不定日期':
                spec.update(start_date_text='', start_date='', defer_start_date=True)
            else:
                source_text = source_text_override
                if slots.get('document_id') or re.search(r'(?:根据|按).{0,50}(?:文档|资料|行程单|\.(?:md|txt|docx|xlsx))', text):
                    if not slots.get('document_id'):
                        options = self.store.trips.document_options(event)
                        matches = [option for option in options if option['filename'] in text]
                        if re.search(r'最新|刚上传', text):
                            matches = list(matches or options)[:1]
                        if not matches and len(options) == 1:
                            matches = list(options)
                        if len(matches) != 1:
                            if not options:
                                return '当前群还没有可用旅行文档，请先上传，或直接告诉我城市和天数。'
                            slots.update(document_candidates=list(options), document_names=[option['filename'] for option in options],
                                         original_command=text)
                            return self._question(task, event, claim, slots, ('document_ref',),
                                '请选择本次规划使用的文档版本：\n' + '\n'.join(
                                    f"{i}. {option['filename']}（文档#{option['id']}，{option['created_at']}）"
                                    for i, option in enumerate(options, 1)))
                        slots['document_id'] = matches[0]['id']
                    source_text = self.store.trips.document_context(event, slots['document_id'])
                spec = self.planner.extract(text, spec, event.occurred_at or self.clock(), source_text)
                if not current:
                    if re.search(r'先不定日期|暂不定日期|日期待定', text):
                        spec.update(start_date_text='', start_date='', defer_start_date=True)
                    self._apply_preferences(event, spec)
            if not current:
                spec['remind_booking'] = bool(spec.get('remind_booking') or re.search(r'提醒.{0,8}预约|预约.{0,8}提醒', text))
            missing = []
            if not spec.get('destination'):
                missing.append('destination')
            if not spec.get('day_count'):
                missing.append('duration')
            if (spec.get('start_date_text') or spec.get('remind_booking')) and not spec.get('start_date') and not spec.get('defer_start_date'):
                missing.append('start_date')
            slots['spec'] = spec
            slots.setdefault('trip_id', current['trip_id'] if current else self.store.trips.new_id(event.event_key))
            if missing:
                questions = {'destination': '想去哪个城市？', 'duration': '计划玩几天？', 'start_date': '具体哪天开始这段行程？请写明月日。'}
                return self._question(task, event, claim, slots, tuple(missing), ' '.join(questions[field] for field in missing))
            spec['reminder_entities'] = spec.get('must_visit', []) if spec.get('remind_booking') else spec.get('reminder_entities', [])
            places = self.planner.collect_places(spec)
            plan = self.planner.plan(spec, places, current['plan'] if current else None, text if current else '')
            if spec.get('remind_booking') and not spec.get('reminder_entities'):
                spec['reminder_entities'] = list(dict.fromkeys(places[activity['poi_id']]['name']
                    for day in plan['days'] for activity in day['activities']
                    if re.search(r'博物馆|美术馆|风景名胜', places[activity['poi_id']].get('type', ''))))
            candidate = {'trip_id': current['trip_id'] if current else slots['trip_id'],
                'title': f"{spec['destination']}{spec['day_count']}日行程", 'status': 'active', 'spec': spec, 'plan': plan, 'sources': places}
        candidate['version'] = current['version'] + 1 if current else 1
        if transport_change:
            candidate['spec']['transport_text'] = transport_change[1]
        if candidate['status'] == 'active':
            self.planner.add_route_evidence(candidate['plan'], candidate['spec'], candidate['sources'],
                current['plan'] if current and command != '刷新行程交通' else None)
        force_refresh = command == '刷新行程预约规则'
        changes, notes, errors, fingerprints, review = self._reminder_changes(event, candidate, force_refresh=force_refresh)
        if force_refresh and not changes and not errors:
            return '已核查行程预约规则，没有需要调整的关联提醒。\n' + '\n'.join(notes)
        if force_confirmation and not errors:
            review = True
        if errors or review:
            already_saved = current is None
            slots.update(trip_id=candidate['trip_id'], candidate_trip=candidate,
                         expected_trip_version=current['version'] if current else 1, trip_already_saved=already_saved,
                         policy_fingerprints=fingerprints, force_refresh=force_refresh,
                         linked_versions={row['reminder_id']: row['version'] for row in self.store.trips.linked_reminders(event, candidate['trip_id'])},
                         reminder_versions={row['reminder_id']: row['version'] for _, row in changes if 'version' in row})
            reply = self.format_trip(candidate, preview=bool(current)) + '\n关联提醒尚未变更：\n' + '\n'.join([*notes, *errors])
            reply += '\n请核对规则与日期后回复“确认行程提醒”；有规则缺口时可稍后说“重新查询行程预约规则”。'
            return self._question(task, event, claim, slots, ('confirmation',), reply, trip=candidate if already_saved else None)
        reply = ('已取消行程。' if candidate['status'] == 'cancelled' else self.format_trip(candidate))
        if notes:
            reply += '\n' + '\n'.join(notes)
        return self.store.trips.commit(event, claim, self._task_update(task, event, {'trip_id': candidate['trip_id']}, 'completed'),
            reply, trip=candidate, expected_version=current['version'] if current else None,
            reminder_changes=changes, now=self.clock())

    def _apply_preferences(self, event, spec):
        if 'preference_version' in spec:
            return
        state = self.store.preferences.defaults(event)
        values, applied = state['values'], {}
        text = event.content + ' ' + ' '.join(spec.get('preferences', []))
        if 'pace' in values and not re.search(r'轻松|适中|紧凑|慢|累|老人|父母|每天', text):
            spec['preferences'].append(values['pace'] + '节奏')
            spec['gentle'] = values['pace'] == '轻松'
            applied['pace'] = values['pace']
        if 'transport' in values and not spec.get('transport_text'):
            spec['transport_text'] = values['transport']
            applied['transport'] = values['transport']
        if 'night' in values and not re.search(r'夜|晚上', text):
            spec['no_night'] = values['night'] == '不安排夜游'
            spec['preferences'].append(values['night'])
            applied['night'] = values['night']
        if 'interests' in values and not spec.get('must_visit'):
            spec['preference_interests'] = values['interests']
            spec['preferences'].append('兴趣：' + '、'.join(values['interests']))
            applied['interests'] = values['interests']
        if 'budget' in values and not spec.get('budget_text'):
            b = values['budget']
            spec['budget_text'] = f"{b['category']} {b['basis']}{b['period']} {b['amount']} {b['currency']}"
            applied['budget'] = b
        spec.update(preference_version=state['version'], applied_preferences=applied)
        spec.setdefault('field_sources', {}).update({key: 'preference' for key in applied})
