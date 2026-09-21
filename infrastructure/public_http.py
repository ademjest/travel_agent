"""Bounded HTTPS to a resolved public IP, preserving TLS hostname verification."""
import ipaddress
import json
import time
from urllib.parse import urlsplit, urljoin

import urllib3

from core.execution_scope import ensure_execution_active
from infrastructure.secure_download import resolve_host


class PublicHTTPError(ValueError):
    def __init__(self, code='unavailable'):
        self.code = code
        super().__init__('外部服务请求未完成（' + code + '）。')


def public_address(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or any(ord(c) < 32 for c in url)):
            raise ValueError()
        addresses = resolve_host(parsed.hostname)
        if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
            raise ValueError()
        return parsed, addresses[0]
    except (ValueError, OSError):
        raise PublicHTTPError('invalid_public_url') from None


def request_public(url, *, payload=None, headers=None, max_bytes=2_000_000, redirects=0,
                   content_types=('application/json',), timeout=20):
    started = time.monotonic()
    for turn in range(redirects + 1):
        ensure_execution_active()
        parsed, address = public_address(url)
        # Resolve once, connect to the validated IP, verify certificate against original hostname.
        pool = urllib3.HTTPSConnectionPool(address, port=443, server_hostname=parsed.hostname,
            assert_hostname=parsed.hostname, cert_reqs='CERT_REQUIRED', retries=False,
            timeout=urllib3.Timeout(connect=min(5, timeout), read=timeout, total=timeout))
        response = None
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            request_headers = {'Host': parsed.hostname, 'Accept-Encoding': 'identity',
                               'User-Agent': 'BeyondTravel/1.0', **(headers or {})}
            if body is not None:
                request_headers['Content-Type'] = 'application/json'
            response = pool.urlopen('POST' if body is not None else 'GET',
                (parsed.path or '/') + ('?' + parsed.query if parsed.query else ''),
                body=body, headers=request_headers, preload_content=False, redirect=False, retries=False)
            if response.status in (301, 302, 303, 307, 308):
                if headers or payload is not None or turn >= redirects:
                    raise PublicHTTPError('redirect_rejected')
                url = urljoin(url, response.headers.get('Location', ''))
                continue
            if not 200 <= response.status < 300:
                raise PublicHTTPError({401: 'unauthorized', 403: 'forbidden', 429: 'rate_limited'}.get(response.status, 'http_error'))
            mime = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
            if mime not in content_types:
                raise PublicHTTPError('unsupported_content')
            chunks, length = [], 0
            for chunk in response.stream(16384, decode_content=True):
                ensure_execution_active()
                length += len(chunk)
                if length > max_bytes or time.monotonic() - started > timeout:
                    raise PublicHTTPError('response_limit')
                chunks.append(chunk)
            return b''.join(chunks), url, mime
        except PublicHTTPError:
            raise
        except (urllib3.exceptions.HTTPError, OSError, ValueError):
            raise PublicHTTPError('network_error') from None
        finally:
            if response is not None:
                response.close()
            pool.close()
    raise PublicHTTPError()
