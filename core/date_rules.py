"""Policy date windows. Parsing applicability never establishes source authority."""
from dataclasses import dataclass
from datetime import date
import re

DATED = re.compile(r'(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?')
ANNUAL = re.compile(r'每年(\d{1,2})月(\d{1,2})日\s*[-—~～至到]\s*(次年|来年)?(\d{1,2})月(\d{1,2})日')
TEMPORARY = re.compile(r'临时|调整|提前执行|特殊开放|特开|特设|暂停开放')


def policy_date(value):
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value or '')
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class DateWindow:
    quote: str
    basis: str = 'not_provided'
    start: date | None = None
    end: date | None = None
    recurrence: str = ''
    weekday: int | None = None
    exception: str = ''

    def state(self, target):
        target = policy_date(target)
        if not target or self.exception or self.basis == 'not_provided':
            return 'unknown'
        if self.start and self.end and self.start > self.end:
            return 'conflicting_range'
        if self.start and target < self.start:
            return 'not_yet_effective' if self.basis == 'explicit_start' else 'outside'
        if self.end and target > self.end:
            return 'outside'
        if self.weekday is not None and target.weekday() != self.weekday:
            return 'outside'
        return 'matching'


def date_window(text, target=None):
    """Parse explicit dates before recurrence; never infer years from retrieval time."""
    target = policy_date(target)
    dates = list(DATED.finditer(text))
    try:
        days = [date(*map(int, match.groups())) for match in dates]
        if len(days) == 2 and re.fullmatch(r'\s*[-—~～至到]\s*', text[dates[0].end():dates[1].start()]):
            return DateWindow(text, 'explicit_date_range', days[0], days[1])
        if len(days) > 1:
            return DateWindow(text, exception='multiple_windows_or_exception')
        if days:
            start, rest = days[0], text[dates[0].end():]
            end = re.match(r'\s*(?:[（(]周.[）)])?\s*[-—~～至到]\s*(次年|来年)?(\d{1,2})月(\d{1,2})日', rest)
            if end:
                finish = date(start.year + bool(end[1]), int(end[2]), int(end[3]))
                if finish < start:
                    return DateWindow(text, exception='ambiguous_date_window')
                return DateWindow(text, 'explicit_date_range', start, finish)
            # An explicit start stays open-ended, including "恢复另行通知".
            if re.match(r'\s*(?:起|开始|生效|施行)', rest):
                return DateWindow(text, 'explicit_start', start)
            if TEMPORARY.search(text) or re.search(r'免费开放|免费参观|主题免费|活动当日', text):
                return DateWindow(text, 'explicit_single_day_event', start, start)
        annuals = list(ANNUAL.finditer(text))
        if annuals:
            if len(annuals) != 1 or len(re.findall(r'\d{1,2}月\d{1,2}日', text)) != 2:
                return DateWindow(text, exception='multiple_windows_or_exception')
            annual = annuals[0]
            if not target:
                return DateWindow(text, recurrence=annual[0])
            year = target.year
            # Select the occurrence spanning the target year, before constructing leap dates.
            if annual[3] and (target.month, target.day) < (int(annual[1]), int(annual[2])):
                year -= 1
            start = date(year, int(annual[1]), int(annual[2]))
            finish = date(year + bool(annual[3]), int(annual[4]), int(annual[5]))
            if finish < start:
                return DateWindow(text, exception='ambiguous_annual_window')
            exception = 'unresolved_date_or_exception' if re.search(r'除外|节假日|另行|调整', text) else ''
            return DateWindow(text, 'explicit_annual_window', start, finish, annual[0], exception=exception)
        if re.search(r'即日起|暑期|寒假|旺季|淡季|冰季|国庆|节假日|除外|另行通知|\d{1,2}月', text):
            return DateWindow(text, exception='unresolved_date_or_exception')
        weekday = re.search(r'每周([一二三四五六日天]).{0,5}闭馆', text)
        if weekday:
            return DateWindow(text, 'explicit_weekly_rule', recurrence=weekday[0],
                              weekday='一二三四五六日'.index(weekday[1].replace('天', '日')))
        if re.search(r'长期有效|全年|常年|每日|每天|常规', text):
            return DateWindow(text, 'explicit_regular_rule', recurrence='daily')
    except ValueError:
        return DateWindow(text, exception='invalid_date_window')
    return DateWindow(text)


def temporal_rule(text, visit_date):
    window = date_window(text, visit_date)
    state = window.state(visit_date)
    temporary = bool(TEMPORARY.search(text))
    return {'kind': 'temporary' if temporary else 'regular',
            'effective_from': window.start.isoformat() if window.start else None,
            'effective_to': window.end.isoformat() if window.end else None,
            'applicability': 'matching' if state == 'matching' else
                             'outside' if state in {'outside', 'not_yet_effective'} else 'unknown',
            'recurrence': window.recurrence,
            'explicit_override': temporary and not re.search(r'可能|或许|拟|待定|预计|建议', text)}

