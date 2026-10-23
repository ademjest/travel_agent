"""Audited public identities, never learned from a search title or a model claim."""
from datetime import date
import json
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit
import re

REGISTRY_PATH = Path(__file__).resolve().parents[1] / 'data/official_sources.json'


def public_policy_url(url):
    """Only public URLs: never forward a signed/login/key-bearing policy link."""
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password
                and parsed.port in (None, 443) and not any(ord(char) < 32 for char in url)
                and not any(re.search(r'key|token|auth|secret|password|signature|^sig$|^code$', key, re.I)
                            for key, _ in parse_qsl(parsed.query)))
    except (ValueError, TypeError):
        return False


def scoped_url(url, entry):
    try:
        parsed = urlsplit(url)
        path = unquote(parsed.path or '/')
        if (not public_policy_url(url)
                or parsed.port not in (None, 443) or '\\' in path or '..' in path.split('/') or '%' in path
                or parsed.query or any(ord(char) < 32 for char in url)):
            return False
        host = parsed.hostname.lower().rstrip('.')
        hosts = entry['allowed_hosts']
        host_allowed = host in hosts or (entry.get('allow_subdomains', False)
            and any(host.endswith('.' + allowed) for allowed in hosts))
        return host_allowed and any(prefix == '/' or path == prefix.rstrip('/')
            or path.startswith(prefix.rstrip('/') + '/') for prefix in entry['path_prefixes'])
    except (ValueError, KeyError, TypeError):
        return False


class OfficialSources:
    def __init__(self, entries=None, *, path=REGISTRY_PATH):
        if entries is None:
            value = json.loads(path.read_text(encoding='utf-8'))
            if value.get('schema_version') != 'official-sources-v1':
                raise ValueError('Invalid official registry version')
            entries = value['entries']
        self.entries = entries
        for entry in entries:
            required = ('entity_key', 'canonical_name', 'city', 'allowed_hosts', 'path_prefixes',
                        'policy_urls', 'identity_evidence_url', 'identity_evidence_quote', 'verified_at', 'registry_version')
            if any(not entry.get(key) for key in required):
                raise ValueError('Incomplete official identity record')
            date.fromisoformat(entry['verified_at'])
            if not public_policy_url(entry['identity_evidence_url']):
                raise ValueError('Invalid public identity evidence URL')
            if not all(scoped_url(url, entry) for url in entry['policy_urls'] + entry.get('notice_index_urls', [])):
                raise ValueError('Registry URL outside reviewed scope')

    def match(self, name, city=''):
        city = city.removesuffix('市')
        matches = [entry for entry in self.entries if name in [entry['canonical_name'], *entry.get('aliases', [])]
                   and (not city or entry['city'].removesuffix('市') == city)]
        return matches[0] if len(matches) == 1 else None

    def verify(self, name, url, city=''):
        entry = self.match(name, city)
        if entry is None or not scoped_url(url, entry):
            return {'status': 'unverified', 'reason': 'entity_or_final_url_not_registered'}
        return {'status': 'verified', 'entity_key': entry['entity_key'], 'operator_name': entry.get('operator_name', ''),
                'registry_version': entry['registry_version'], 'verified_at': entry['verified_at'],
                'evidence_url': entry['identity_evidence_url'], 'evidence_quote': entry['identity_evidence_quote']}

    def policy_sources(self):
        return {entry['canonical_name']: entry['policy_urls'][0] for entry in self.entries}
