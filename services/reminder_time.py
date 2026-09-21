"""Deterministic Chinese calendar/time normalization; never invent a missing clock time."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo('Asia/Shanghai')
NUMBER = r'[零〇一二两三四五六七八九十\d]+'


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
        period = re.search(r'上午|早上|早晨|中午|下午|晚上|今晚|凌晨|傍晚', text)
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
            if period and period[0] in {'晚上', '今晚'} and hour == 12:
                day = ((date.fromisoformat(day) if day else local.date()) + timedelta(days=1)).isoformat()
                hour = 0
            if any(word in text for word in ('下午', '晚上', '今晚', '傍晚')) and hour < 12:
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
