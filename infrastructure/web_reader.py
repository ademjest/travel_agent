from datetime import datetime, timezone
import hashlib
import html
import re

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


def read_page(url, *, diagnostics=None):
    LAST_FETCH_AT.set(None)
    data, final_url, mime = request_public(url, redirects=2,
        content_types=('text/html', 'text/plain', 'application/xhtml+xml'), max_bytes=2_000_000, timeout=20,
        diagnostics=diagnostics)
    text = data.decode('utf-8-sig', errors='replace')
    if mime != 'text/plain':
        match = re.search(r'<title[^>]*>(.*?)</title>',text,re.I|re.S)
        title = html.unescape(re.sub('<[^>]+>','',match[1])).strip() if match else ''
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
    text = text[:30_000]
    return {'url': final_url, 'text': text, 'content_hash': hashlib.sha256(data).hexdigest(),
            'retrieved_at': LAST_FETCH_AT.get() or datetime.now(timezone.utc).isoformat(), 'status': 'read'}
