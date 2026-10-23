from datetime import datetime, timezone
import hashlib
import html
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from infrastructure.public_http import request_public, PublicHTTPError, LAST_FETCH_AT
from services.booking_policy import PolicyTextParser


def public_text_issue(text):
    """Recognize access/error-page headings, including HTTP-200 soft 404 pages."""
    heading = (text.strip().splitlines() or [''])[0].strip()[:240]
    if re.search(r'(?:^|[-|·]\s*)(?:你访问的页面不见了|页面不存在|内容已删除|笔记不存在|404(?:\s+Not Found)?|Page Not Found)\s*(?:$|[-|·])',heading,re.I):
        return 'page_unavailable'
    if re.fullmatch(r'(?:.{1,30}\s[-|·]\s*)?(?:安全验证|访问验证|访问受限|登录|用户登录|Just a moment\.*|Access denied|Sign in)(?:\s*[-|·].*)?',heading,re.I):
        return 'page_requires_access'
    footer = re.search(r'©|copyright|ICP备',text,re.I)
    if footer:
        navigation = {'搜索','发现','red','直播','发布','通知','消息','网页版','创作中心','业务合作',
                      '首页','登录','注册','更多','关于我们'}
        before = [line.strip().lower() for line in text[:footer.start()].splitlines() if line.strip()]
        if before and all(line in navigation for line in before):
            return 'navigation_without_content'
    return ''


class PageLinks(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.links, self.current, self.words = base, [], None, []
        self.publication = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        field = values.get('property') or values.get('name')
        if tag == 'meta' and field in {'article:published_time', 'datePublished', 'pubdate'} and values.get('content'):
            self.publication = {'kind': 'html_meta', 'field': field, 'raw_value': values['content'][:80]}
        if tag == 'base' and dict(attrs).get('href'):
            candidate = urljoin(self.base, dict(attrs)['href'])
            if urlsplit(candidate).scheme == 'https' and urlsplit(candidate).hostname == urlsplit(self.base).hostname:
                self.base = candidate
        if tag == 'a':
            self.current, self.words = dict(attrs).get('href'), []

    def handle_data(self, data):
        if self.current is not None:
            self.words.append(data)

    def handle_endtag(self, tag):
        if tag != 'a' or self.current is None:
            return
        url = urljoin(self.base, self.current).split('#')[0]
        parsed = urlsplit(url)
        if (len(self.links) < 200 and parsed.scheme == 'https' and parsed.hostname
                and not parsed.username and not parsed.password and not parsed.query):
            row = {'url': url, 'text': ' '.join(self.words).strip()[:200]}
            if row not in self.links:
                self.links.append(row)
        self.current = None


def read_page(url, *, diagnostics=None, url_allowed=None):
    LAST_FETCH_AT.set(None)
    data, final_url, mime = request_public(url, redirects=2,
        content_types=('text/html', 'text/plain', 'application/xhtml+xml'), max_bytes=2_000_000, timeout=20,
        diagnostics=diagnostics, **({'url_allowed': url_allowed} if url_allowed is not None else {}))
    text = data.decode('utf-8-sig', errors='replace')
    title, links, publication = '', [], None
    if mime != 'text/plain':
        match = re.search(r'<title[^>]*>(.*?)</title>',text,re.I|re.S)
        title = html.unescape(re.sub('<[^>]+>','',match[1])).strip() if match else ''
        extractor = PageLinks(final_url)
        extractor.feed(text)
        publication = extractor.publication
        links = sorted(extractor.links, key=lambda row: not bool(re.search(r'须知|开放|参观|公告|预约|visitor|visit|notice', row['text'], re.I)))[:40]
        issue = public_text_issue(title)
        if issue:
            raise PublicHTTPError(issue)
        parser = PolicyTextParser()
        parser.feed(text)
        text = parser.text()
        # The parser includes <title>; it must not make a navigation-only body
        # look like an article with substantive content.
        issue = public_text_issue(text.removeprefix(title).lstrip() if title else text)
        if issue:
            raise PublicHTTPError(issue)
        if len(text.strip())<80:
            raise PublicHTTPError('insufficient_public_text')
    if not text.strip():
        raise PublicHTTPError('insufficient_public_text')
    original_chars = len(text)
    text = text[:30_000]
    return {'url': final_url, 'text': text, 'content_hash': hashlib.sha256(data).hexdigest(),
            'retrieved_at': LAST_FETCH_AT.get() or datetime.now(timezone.utc).isoformat(), 'status': 'read',
            'title': title, 'links': links, 'original_chars': original_chars, 'truncated': original_chars > len(text),
            'published_at': publication['raw_value'] if publication else None, 'publication_evidence': publication}
