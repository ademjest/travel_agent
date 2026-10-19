"""Server-owned plan resolution and read-only date context for research."""
from datetime import datetime, timedelta, timezone

from services.reminder_time import BEIJING, parse_reminder_time


def research_dates(request, event, duration_options=()):
    now = event.occurred_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=BEIJING)
    arrival = parse_reminder_time(request.get('arrival_date_text', ''), now)
    visit = parse_reminder_time(request.get('visit_start_date_text', ''), now, day=arrival.day)
    # An arrival date alone is not a sightseeing start date.
    arrival_day = arrival.day if request.get('arrival_date_text') else ''
    visit_day = visit.day if request.get('visit_start_date_text') else ''
    if arrival.error or visit.error:
        raise ValueError('研究日期尚不明确或已过去，请确认到达日期和开始游览日期（跨年请写年份）。')
    if arrival_day and visit_day and visit_day < arrival_day:
        raise ValueError('开始游览日期早于到达日期，请确认这两个日期。')
    days = duration_options[0] if len(duration_options) == 1 else None
    return {'timezone':'Asia/Shanghai', 'reference_date':now.astimezone(BEIJING).date().isoformat(),
            'arrival_date':arrival_day, 'visit_start_date':visit_day,
            'visit_end_date':(datetime.fromisoformat(visit_day) + timedelta(days=days-1)).date().isoformat()
                if visit_day and days else '',
            'basis':'server_calendar_from_user_text', 'year_inferred':bool(arrival_day or visit_day)
                and not any(str(year) in event.content for year in range(now.year, now.year+3))}


def resolve_plan(store, event, request):
    """Resolve only in the event's owner/scope. Ambiguity never picks a random plan."""
    artifacts = list(store.planning.list_for_owner(event))
    destination = store.planning._destination(event.content, '')
    if destination:
        artifacts = [a for a in artifacts if a['destination'] == destination]
    reference = request.get('plan_reference', '')
    index = request.get('plan_index')
    if index is not None:
        selected = [a for a in artifacts if a['ordinal'] == index]
        # Generic "this plan" does not make two non-adjacent plans unambiguous.
        if selected and reference and reference not in {'这个方案','该方案','原方案','这个行程','这些景点'}:
            return selected[0], 'explicit_reference'
        if not selected:
            return None, 'unavailable_reference'
    titled = [a for a in artifacts if a['title'] in event.content]
    if len(titled) == 1:
        return titled[0], 'explicit_title'
    with store._connect() as db:
        if event.reply_to_id:
            link = db.execute('''SELECT event_id FROM outbound_message_links
                WHERE platform=? AND scope_id=? AND message_id=? AND owner_id=?''',
                (event.platform,event.storage_scope_id,event.reply_to_id,event.sender_id)).fetchone()
            linked = [a for a in artifacts if a['source_event_id'] in
                      {event.reply_to_id,link['event_id'] if link else ''}]
            if len(linked) == 1:
                return linked[0], 'reply_link'
        last = db.execute('''SELECT user_msg_id FROM conversation_turns
            WHERE group_openid=? AND member_openid=? ORDER BY id DESC LIMIT 1''',
            (event.storage_scope_id,event.sender_id)).fetchone()
    recent = [a for a in artifacts if last and a['source_event_id'] == last['user_msg_id']]
    if len(recent) == 1:
        return recent[0], 'adjacent_plan'
    if len(artifacts) == 1:
        return artifacts[0], 'unique_plan'
    if not artifacts and index is None:
        trips = store.trips.list_for_owner(event)
        if destination:
            trips = [t for t in trips if t['spec'].get('destination') == destination]
        selected = store.trips.referenced(event) if event.reply_to_id else None
        if selected is None and len(trips) == 1:
            selected = trips[0]
        if selected:
            from services.trip_service import TripService
            return {'content':TripService.format_trip(selected), 'title':selected['title'],
                    'destination':selected['spec']['destination'],
                    'duration_options':[selected['spec']['day_count']], 'version':selected['version']}, 'saved_trip'
    return None, 'ambiguous_plan' if artifacts else 'missing_plan'
