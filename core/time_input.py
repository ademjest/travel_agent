"""Deterministic Chinese calendar/time normalization; never invent a missing clock time."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo('Asia/Shanghai')
NUMBER = r'[零〇一二两三四五六七八九十\d]+'
RAIL_DEPARTURE_PERIODS = {'凌晨':(0,6),'早上':(6,10),'早晨':(6,10),'上午':(9,12),'中午':(11,14),
    '下午':(12,18),'傍晚':(17,20),'晚上':(18,24),'晚间':(18,24),
    **dict.fromkeys(('全天','整天','一整天','不限时间','不限时段','时间不限'),(0,24))}


def rail_departure_period(text):
    text=re.sub(r'\s+','',text).rstrip('。！!？?')
    text=re.sub(r'(?:出发|都可以|都行|均可)?[吧呀啊呢]?$','',text)
    return RAIL_DEPARTURE_PERIODS.get(text)


def number(text):
    if text.isdigit():
        return int(text)
    digits = dict(zip('零〇一二两三四五六七八九', (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    if '十' in text:
        before, after = text.split('十', 1)
        return (digits.get(before, 1) * 10) + digits.get(after, 0)
    if len(text) == 1 and text in digits:
        return digits[text]
    raise ValueError('时间数字不明确，请使用数字日期和时刻。')


def parse_time_window(text, *, dated=False):
    """Normalize a same-day window without borrowing reminder clock defaults."""
    text=re.sub(r'\s+','',text).strip('，,。！？!?')
    if dated:
        text=re.sub(rf'^(?:(?:20\d{{2}}-\d{{1,2}}-\d{{1,2}})|(?:(?:20\d{{2}}年)?{NUMBER}月{NUMBER}[日号]?)|(?:{NUMBER}[日号])|大后天|后天|明天|今天)','',text)
        text=re.sub(r'^(?:下周|本周|这周|周|星期)[一二三四五六日天]','',text)
        text=re.sub(r'(?:出发|启程|返程|返回|回来)$','',text)
        if not re.search('|'.join(RAIL_DEPARTURE_PERIODS)+r'|[点时:：]',text):return None
    if not text:return None
    period=rail_departure_period(text)
    if period is not None:return period
    names='|'.join(sorted(RAIL_DEPARTURE_PERIODS,key=len,reverse=True))
    parts=re.split(r'(?:至|到|—|–|~|～|-)',text)
    boundary=re.fullmatch(r'(.+?)(之前|以前|之后|以后|前|后)',text)
    if boundary:parts=[boundary[1]]
    if boundary and boundary[1] in RAIL_DEPARTURE_PERIODS:
        before=boundary[2] in {'之前','以前','前'}
        window=(0,RAIL_DEPARTURE_PERIODS[boundary[1]][0]) if before else (RAIL_DEPARTURE_PERIODS[boundary[1]][1],24)
        if not window[0]<window[1]:raise ValueError('时间范围为空，请核对边界。')
        return window
    if len(parts) not in (1,2) or len(parts)==1 and not boundary and not dated:
        raise ValueError('请明确出发时段或起止钟点；含糊或额外限制不能按全天查询。')
    inherited=''; clocks=[]
    for index,part in enumerate(parts):
        match=re.fullmatch(rf'(?P<period>{names})?(?P<hour>{NUMBER})(?:(?:[:：](?P<minute>\d{{2}}))|(?:[点时](?P<spoken>半|{NUMBER}分?)?))?',part)
        if not match:raise ValueError('时段格式不明确，请给出自然时段或具体时间范围。')
        stated=match['period'] or inherited
        if match['period']:inherited=match['period']
        hour=number(match['hour']); minute=int(match['minute']) if match['minute'] else 30 if match['spoken']=='半' else number(match['spoken'].removesuffix('分')) if match['spoken'] else 0
        if stated:
            lower,upper=RAIL_DEPARTURE_PERIODS[stated]
            if lower>=11 and hour<12:hour+=12
            if lower>=18 and hour==12:hour=24
            if not lower<=hour+minute/60<=upper:raise ValueError('自然时段与具体钟点冲突，请核对时间。')
        elif 1<=hour<=12 and not match['minute']:
            raise ValueError('这个钟点有上下半日歧义，请明确上午、下午或使用24小时制。')
        if not 0<=minute<60 or not 0<=hour<=24 or hour==24 and minute:
            raise ValueError('钟点无效，请核对小时和分钟。')
        clocks.append(hour+minute/60)
    if dated and len(clocks)==1 and not boundary:return (clocks[0],clocks[0])
    window=((0,clocks[0]) if boundary[2] in {'之前','以前','前'} else (clocks[0],24)) if boundary else tuple(clocks)
    if not 0<=window[0]<window[1]<=24:raise ValueError('同日时间范围必须先早后晚；跨日请明确日期。')
    return window


def format_time_window(window):
    return '至'.join(f'{int(round(hour*60))//60:02d}:{int(round(hour*60))%60:02d}' for hour in window)


@dataclass(frozen=True)
class ReminderTime:
    day: str = ''
    clock: str = ''
    error: str = ''
    needs_period: bool = False

    def instant(self):
        if self.day and self.clock and not self.needs_period:
            return datetime.fromisoformat(f'{self.day}T{self.clock}').replace(tzinfo=BEIJING)
        return None


def parse_reminder_time(text, now, *, day='', clock='', period_required=False):
    local = now.astimezone(BEIJING)
    unresolved_period = period_required
    try:
        relative = re.search(rf'({NUMBER})\s*(分钟|小时|天)后', text)
        if relative:
            amount = number(relative[1])
            if amount < 1:
                return ReminderTime('', '', '请指定至少一分钟之后的提醒时间。')
            delta = timedelta(**{{'分钟': 'minutes', '小时': 'hours', '天': 'days'}[relative[2]]: amount})
            instant = local + delta
            if instant.microsecond:
                instant = (instant + timedelta(seconds=1)).replace(microsecond=0)
            return ReminderTime(instant.date().isoformat(), instant.strftime('%H:%M:%S'))
        dated = re.search(r'(?<!\d)(20\d{2})-(\d{1,2})-(\d{1,2})(?!\d)', text)
        chinese_date = re.search(rf'(?:(20\d{{2}})年)?({NUMBER})月({NUMBER})[日号]?', text)
        if dated:
            day = date(*map(int, dated.groups())).isoformat()
        elif chinese_date:
            day = date(int(chinese_date[1] or local.year), number(chinese_date[2]), number(chinese_date[3])).isoformat()
        else:
            relative_day = next((value for value in ('大后天', '后天', '明天', '昨天', '今天', '今晚') if value in text), None)
            if relative_day:
                day = (local.date() + timedelta(days={'昨天': -1, '今天': 0, '今晚': 0, '明天': 1, '后天': 2, '大后天': 3}[relative_day])).isoformat()
            else:
                day_only = re.search(rf'({NUMBER})[日号]', text)
                weekday = re.search(r'(下周|本周|这周|周|星期)([一二三四五六日天])', text)
                if day_only:
                    reference = date.fromisoformat(day) if day else local.date()
                    day = date(reference.year, reference.month, number(day_only[1])).isoformat()
                elif weekday:
                    target = '一二三四五六日'.index(weekday[2].replace('天', '日'))
                    delta = target - local.weekday()
                    if weekday[1] == '下周':
                        delta += 7
                    elif weekday[1] in {'周', '星期'} and delta < 0:
                        delta += 7
                    day = (local.date() + timedelta(days=delta)).isoformat()
        colon = re.search(r'(?<!\d)(\d{1,2})[:：](\d{2})(?!\d)', text)
        spoken = re.search(rf'({NUMBER})[点时](半|一刻|三刻|(?:{NUMBER})分?)?', text)
        period = re.search(r'上午|早上|早晨|中午|下午|晚上|晚间|今晚|凌晨|傍晚', text)
        if colon or spoken or (period and clock):
            unresolved_period = False
            if colon:
                hour, minute = map(int, colon.groups())
            elif spoken:
                hour = number(spoken[1])
                part = spoken[2] or ''
                minute = {'': 0, '半': 30, '一刻': 15, '三刻': 45}.get(part)
                if minute is None:
                    minute = number(part.removesuffix('分'))
            else:
                hour, minute = map(int, clock.split(':')[:2])
                hour %= 12
            if spoken and not period and 1 <= hour <= 12:
                if not clock or period_required:
                    return ReminderTime(day, f'{hour:02d}:{minute:02d}', '你指的是上午还是下午/晚上？', True)
                if int(clock[:2]) >= 12 and hour < 12:
                    hour += 12
            if period and period[0] in {'晚上', '晚间', '今晚'} and hour == 12:
                day = ((date.fromisoformat(day) if day else local.date()) + timedelta(days=1)).isoformat()
                hour = 0
            if any(word in text for word in ('下午', '晚上', '晚间', '今晚', '傍晚')) and hour < 12:
                if hour != 0:
                    hour += 12
            if '中午' in text and hour < 11:
                hour += 12
            if '凌晨' in text and hour == 12:
                hour = 0
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                return ReminderTime(day, '', '时刻无效，请告诉我有效的几点几分。')
            clock = f'{hour:02d}:{minute:02d}'
        if not day and clock:
            day = local.date().isoformat()
        result = ReminderTime(day, clock,
                              '你指的是上午还是下午/晚上？' if unresolved_period and clock else '',
                              bool(unresolved_period and clock))
        if day and date.fromisoformat(day) < local.date():
            return ReminderTime('', clock, '这个日期已经过去，请重新指定提醒日期（跨年请写年份）。', result.needs_period)
        if result.instant() and result.instant() <= local:
            return ReminderTime(day, '', '这个提醒时间已经过去，请重新指定未来的时间。')
        return result
    except (ValueError, OverflowError):
        return ReminderTime('', '', '日期或时间无效，请使用明确的年月日和时刻。')


def is_time_answer(text):
    # Conversational suffixes must not turn a time clarification into an unrelated chat.
    text = re.sub(r'[吧呀啊呢][。！？!?\s]*$', '', text.strip())
    return bool(re.fullmatch(
        rf'(?:改成|改为|到|就|明天|后天|大后天|今天|今晚|下周|本周|这周|周|星期|日|天|上午|早上|早晨|中午|下午|晚上|凌晨|傍晚|'
        rf'{NUMBER}|年|月|日|号|点|时|分|半|一刻|三刻|分钟|小时|天|后|[\s:：\-。！!])+', text.strip()))


ABSOLUTE_TIME_FORMAT = "%Y-%m-%d %H:%M"


def parse_beijing_datetime_list(value: str) -> tuple[datetime, ...]:
    parts = [item.strip() for item in re.split(r"[,，]", value) if item.strip()]
    if not parts:
        raise ValueError("reminder time must use YYYY-MM-DD HH:MM")
    parsed = []
    for item in parts:
        try:
            local_time = datetime.strptime(item, ABSOLUTE_TIME_FORMAT)
        except ValueError as exc:
            raise ValueError(
                "reminder time must use YYYY-MM-DD HH:MM"
            ) from exc
        parsed.append(local_time.replace(tzinfo=BEIJING))
    return tuple(sorted(set(parsed)))
