"""Public policy evidence and deterministic booking-window calculation."""
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import re
from urllib.parse import urlparse

import requests
from core.execution_scope import ensure_execution_active

from infrastructure.secure_download import download_https
from services.reminder_time import BEIJING


# This is a source registry, not a hard-coded booking policy. Rules are read afresh.
OFFICIAL_SOURCES = {
    '湖北省博物馆': 'https://www.hbww.org.cn/fuwu/index.html',
}


class PolicyUnavailable(ValueError):
    pass


class PolicyTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'noscript'}:
            self.hidden += 1
        if tag in {'p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'section'} and not self.hidden:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'noscript'} and self.hidden:
            self.hidden -= 1
        if tag in {'p', 'div', 'li', 'section'} and not self.hidden:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def text(self):
        return '\n'.join(' '.join(line.split()) for line in ''.join(self.parts).splitlines() if line.strip())


@dataclass(frozen=True)
class BookingPolicy:
    entity: str
    source_url: str
    excerpt: str
    days_before: int
    release_time: str
    retrieved_at: str
    fingerprint: str
    official: bool
    caveats: tuple[str, ...] = ()
    closed_weekdays: tuple[int, ...] = ()
    holiday_exceptions: bool = False

    def opening_at(self, visit_date: date) -> datetime:
        hour, minute = map(int, self.release_time.split(':'))
        return datetime.combine(visit_date - timedelta(days=self.days_before), time(hour, minute), BEIJING)

    def to_dict(self):
        return asdict(self)


class BookingPolicyResolver:
    def __init__(self, *, sources=None, fetch_text=None, clock=None):
        self.sources = dict(OFFICIAL_SOURCES if sources is None else sources)
        self.fetch_text = fetch_text or self._fetch_text
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _fetch_text(url):
        ensure_execution_active()
        # Do not downgrade certificate failures to HTTP or disable certificate verification.
        with requests.Session() as session:
            session.trust_env = False
            data, _ = download_https(session, url, max_bytes=2_000_000, timeout=(10, 20),
                max_redirects=0, allowed_content_types={'text/html', 'text/plain', 'application/xhtml+xml'})
        parser = PolicyTextParser()
        parser.feed(data.decode('utf-8-sig', errors='replace'))
        return parser.text()

    def resolve(self, entity, *, source_url=''):
        url = source_url or self.sources.get(entity, '')
        canonical = self.sources.get(entity, '')
        if canonical.endswith('/index.html') and url.rstrip('/') == canonical.removesuffix('/index.html'):
            url = canonical
        if not url:
            raise PolicyUnavailable('还没有找到该场馆可核实的官方说明，请提供官方预约说明的 HTTPS 页面链接。')
        parsed_url = urlparse(url)
        if parsed_url.scheme != 'https' or not parsed_url.hostname or parsed_url.username or parsed_url.password:
            raise PolicyUnavailable('请提供公开的 HTTPS 预约说明页面链接。')
        try:
            text = self.fetch_text(url)
        except ValueError as exc:
            raise PolicyUnavailable('暂时无法读取预约说明，请稍后重试或提供可读取的官方说明链接。') from exc
        if entity not in text:
            raise PolicyUnavailable('该页面没有明确对应到这个场馆，请提供能核对场馆名称的预约说明。')
        if re.search(r'暂停预约|停止预约|暂停开放|闭馆公告', text):
            raise PolicyUnavailable('页面含暂停预约或闭馆信息，请先核对开放安排，暂不创建开约提醒。')
        # Select one personal-admission paragraph. Guide/team reservations are distinct products.
        candidates = []
        for line in text.splitlines():
            if not re.search(r'(?:预约|门票).{0,15}提前\s*\d{1,2}\s*天', line):
                continue
            if re.search(r'讲解员|电话预约', line) or ('团队' in line and not re.search(r'散客|个人', line)):
                continue
            candidates.append(line)
        if not candidates:
            raise PolicyUnavailable('没有读到可明确计算的个人入馆预约窗口，不能把团队或讲解预约规则代入。')
        rules = []
        for line in candidates:
            days = re.findall(r'(?:预约|门票).{0,15}?提前\s*(\d{1,2})\s*天', line)
            release = re.findall(r'(?:每日|每天)\s*(\d{1,2})(?:(?:[:：])(\d{2})|(?:点|时)(?:(\d{1,2})分)?)\s*(?:开始)?放(?:最新可预约日的门票|票)', line)
            # Some sites split the release sentence into its own paragraph; only use the immediate next line.
            excerpt = line
            if not release:
                index = text.splitlines().index(line)
                next_line = text.splitlines()[index + 1:index + 2]
                if next_line:
                    excerpt += '\n' + next_line[0]
                    release = re.findall(r'(?:每日|每天)\s*(\d{1,2})(?:(?:[:：])(\d{2})|(?:点|时)(?:(\d{1,2})分)?)\s*(?:开始)?放(?:最新可预约日的门票|票)', next_line[0])
            if len(set(days)) != 1 or len(set(release)) != 1:
                continue
            hour, minute, spoken_minute = release[0]
            count = int(days[0])
            h, m = int(hour), int(minute or spoken_minute or 0)
            if not 1 <= count <= 60 or not 0 <= h < 24 or not 0 <= m < 60:
                continue
            if re.search(r'含(?:当日|当天)|工作日|自然月|暂停预约|停止预约', excerpt):
                raise PolicyUnavailable('页面的包含当天、工作日或暂停规则需要进一步核实，暂不自动计算。')
            rules.append((count, f'{h:02d}:{m:02d}', excerpt))
        if len({(days, clock) for days, clock, _ in rules}) != 1:
            raise PolicyUnavailable('页面规则不完整或存在不同放票时刻，请核对适用的预约说明。')
        days, clock, excerpt = rules[0]
        official = url == self.sources.get(entity)
        caveats = []
        if not official:
            caveats.append('这是你提供的页面，程序尚未独立核实其官方身份。')
        if re.search(r'暑期|寒假|节假日|另行通知|试行|调整', text):
            caveats.append('页面同时含季节或节假日说明，需要你核对该规则适用于所选参观日期。')
        fingerprint = hashlib.sha256(f'{entity}\n{url}\n{text}'.encode()).hexdigest()
        closed_weekdays = tuple(sorted({
            '一二三四五六日'.index(day.replace('天', '日'))
            for day in re.findall(r'每周(?:周)?([一二三四五六日天])闭馆', text)
        }))
        return BookingPolicy(entity, url, excerpt[:4000], days, clock,
                             self.clock().isoformat(), fingerprint, official, tuple(caveats),
                             closed_weekdays, '国家法定节假日除外' in text)
