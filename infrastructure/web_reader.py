from datetime import datetime, timezone
import hashlib

from infrastructure.public_http import request_public
from services.booking_policy import PolicyTextParser


def read_page(url):
    data, final_url, mime = request_public(url, redirects=2,
        content_types=('text/html', 'text/plain', 'application/xhtml+xml'), max_bytes=2_000_000, timeout=20)
    text = data.decode('utf-8-sig', errors='replace')
    if mime != 'text/plain':
        parser = PolicyTextParser()
        parser.feed(text)
        text = parser.text()
    text = text[:30_000]
    return {'url': final_url, 'text': text, 'content_hash': hashlib.sha256(data).hexdigest(),
            'retrieved_at': datetime.now(timezone.utc).isoformat(), 'status': 'read'}
