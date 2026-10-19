import json
import re

from infrastructure.public_http import request_public, PublicHTTPError


class SearchClient:
    def __init__(self, key='', base_url='https://api.tavily.com'):
        self.key = key
        self.base_url = base_url.rstrip('/') or 'https://api.tavily.com'
        self.diagnostics = None

    @property
    def configured(self):
        return bool(self.key) and self.base_url == 'https://api.tavily.com'

    def search(self, query, *, include_domains=(), exclude_domains=(), language=None):
        if not self.configured:
            raise ValueError('网页搜索未配置，请在服务配置中设置 Tavily Key；也可提供公开 HTTPS 页面链接。')
        payload = {'query': query[:500], 'search_depth': 'basic', 'max_results': 5,
                   'include_answer': False, 'include_raw_content': False}
        if language is not None:
            if not isinstance(language,str) or not re.fullmatch(r'[a-z]{2,3}(?:-[a-z]{2})?',language):
                raise ValueError('搜索语言无效。')
            payload['language'] = language
        for key, domains in (('include_domains',include_domains),('exclude_domains',exclude_domains)):
            if (not isinstance(domains,(list,tuple)) or len(domains)>20
                    or any(not isinstance(d,str) or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}',d) for d in domains)):
                raise ValueError('搜索域名过滤无效。')
            if domains:
                payload[key] = list(dict.fromkeys(domains))
        if payload.get('include_domains'):
            payload['include_domains_mode'] = 'restrict'
        data, _, _ = request_public(self.base_url + '/search',
            headers={'Authorization': 'Bearer ' + self.key},
            payload=payload, max_bytes=300_000, diagnostics=self.diagnostics)
        try:
            value = json.loads(data)
            rows = value['results']
            if not isinstance(rows, list):
                raise ValueError()
            return [{'url': row['url'][:2000], 'title': row.get('title', '')[:200],
                     'snippet': row.get('content', '')[:1500], 'published_at': row.get('published_date') or ''}
                    for row in rows[:5] if isinstance(row, dict) and isinstance(row.get('url'), str)]
        except (KeyError, ValueError, TypeError):
            raise PublicHTTPError('invalid_response') from None
