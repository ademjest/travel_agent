"""Freeze implementation identity when this module is imported by a new process."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STARTED_AT = datetime.now(timezone.utc).isoformat()
_digest = hashlib.sha256()
for _folder in ('adapters', 'agents', 'app', 'core', 'infrastructure', 'services', 'tools'):
    for _file in sorted((ROOT / _folder).rglob('*.py')):
        _digest.update(_file.relative_to(ROOT).as_posix().encode())
        _digest.update(_file.read_bytes())
BACKEND_ID = _digest.hexdigest()[:16]
CAPABILITIES = ('runtime_status_v1', 'retrieval_status_v1', 'summary_freshness_v1', 'diagnostics_v1')


def runtime_status(store, model, configuration=None):
    routes=[]
    if configuration:
        for role in ('basic','regular','complex','vision'):
            profile=configuration.profile(configuration.roles.get(role) or (configuration.roles.get('regular') if role!='vision' else ''))
            if profile:routes.append({'role':role,'profile_id':profile.id,'connection_id':profile.connection_id,
                                     'model_id':profile.model_id,'declared_capabilities':list(profile.capabilities)})
    return {'backend_id': BACKEND_ID, 'started_at': STARTED_AT, 'capabilities': CAPABILITIES,
            'model': model, 'retrieval': dict(getattr(store, 'semantic_status', {'state': 'unknown'})),
            'models':{'config_version':configuration.version if configuration else '',
                      'legacy':configuration.legacy if configuration else False,'routes':routes,
                      'pricing':configuration.pricing_status() if configuration else []}}


def retrieval_status(store, scope):
    result = {'configuration': dict(getattr(store, 'semantic_status', {'state': 'unknown'}))}
    index = getattr(store, 'semantic_index', None)
    if index is not None and hasattr(index, 'snapshot'):
        result.update(index.snapshot(scope))
    return result
