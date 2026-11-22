"""Explicit application prompt assets. No routing, execution or model client lives here."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import ast
import hashlib
import inspect
import json
from pathlib import Path
from types import MappingProxyType

ROOT = Path(__file__).resolve().parents[1]
STAGE_OWNERS = {
    'hotel.quote_extract':'tools/hotel_prices/tool.py',
    'trip.answer':'tools/trip/presentation.py',
    'trip.compare': 'tools/trip/alternatives.py',
    'intent.compile': 'agents/intent_compiler.py', 'agent.respond': 'agents/travel_agent.py',
    **dict.fromkeys(('research.search_plan', 'research.targets', 'research.facts', 'research.synthesis'), 'tools/research/tool.py'),
    'research.write': 'tools/research/evidence.py',
    **dict.fromkeys(('trip.requirements', 'trip.plan', 'trip.local_day'), 'tools/trip/planner.py'),
    **dict.fromkeys(('reminder.personal', 'reminder.booking', 'reminder.scheduled_query'), 'tools/reminders/tool.py'),
    'materials.summary': 'tools/materials.py', 'materials.image': 'tools/materials.py',
    'reservation.image': 'tools/reservation/tool.py', 'memory.summary': 'app/conversation_context.py',
}
CONTRACT_FILES = {
    'hotel.quote_extract':('tools/hotel_prices/quotes.py','core/hotel_contract.py','core/budget_contract.py'),
    'trip.answer':('tools/trip/planner.py','tools/trip/constraints.py','tools/hotel_prices/tool.py','tools/hotel_prices/quotes.py','core/budget_contract.py'),
    'trip.compare': ('tools/trip/planner.py','tools/trip/constraints.py','tools/research/sources.py','tools/hotel_prices/tool.py','tools/hotel_prices/quotes.py','core/budget_contract.py'),
    'intent.compile': ('core/intent_ir.py', 'core/research_contract.py', 'core/preference_contract.py','core/hotel_contract.py','core/context_contract.py'),
    'agent.respond': ('tools/registry.py', 'core/tool_result.py','core/hotel_contract.py','core/context_contract.py'),
    **dict.fromkeys(('research.search_plan', 'research.targets', 'research.facts', 'research.synthesis'),
                    ('tools/research/sources.py', 'tools/research/evidence.py', 'core/research_contract.py',
                     'tools/research/official_links.py', 'services/official_sources.py', 'tools/search.py')),
    'research.write': ('tools/research/sources.py', 'tools/research/official_links.py', 'services/official_sources.py', 'tools/search.py'),
    **dict.fromkeys(('trip.requirements', 'trip.plan', 'trip.local_day'), ('tools/trip/constraints.py','tools/hotel_prices/tool.py','tools/hotel_prices/quotes.py','core/budget_contract.py')),
    **dict.fromkeys(('reminder.personal', 'reminder.booking', 'reminder.scheduled_query'), ('core/time_input.py',)),
    'materials.summary': (), 'materials.image': (), 'reservation.image': (),
    'memory.summary': ('core/summary_validation.py',),
}
CURRENT_SKILLS = ContextVar('application_skill_snapshot', default=None)


class SkillAssetError(ValueError):
    pass


class SkillVersionUnavailable(SkillAssetError):
    """A checkpoint cannot be continued using different or unversioned assets."""


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class SkillAsset:
    id: str
    version: str
    purpose: str
    blocks: object
    resources: object
    file_hashes: object
    bundle_hash: str

    def text(self, key):
        try:
            return self.blocks[key]
        except KeyError as error:
            raise SkillAssetError(f'Missing required text: {self.id}/{key}') from error

    def resource(self, key):
        # Return an independent value; callers cannot mutate the request snapshot.
        return json.loads(self.resources[key])

    def metadata(self):
        return {'stage_id': self.id, 'asset_version': self.version, 'asset_bundle_hash': self.bundle_hash}


@dataclass(frozen=True)
class SkillSnapshot:
    assets: object

    def metadata(self):
        return {key: value.metadata() for key, value in sorted(self.assets.items())}


class SkillManager:
    def __init__(self, root=ROOT, *, research_dir=None):
        self.root = Path(root).resolve()
        # Trusted constructor override retained for the existing research configuration tests.
        self.research_dir = Path(research_dir).resolve() if research_dir is not None else None
        try:
            manifest = json.loads((self.root/'skills/manifest.json').read_text(encoding='utf-8'))
            if set(manifest) != {'schema_version', 'stages'} or manifest['schema_version'] != 'application-skills-v1':
                raise SkillAssetError('Invalid application Skill manifest')
            entries = manifest['stages']
            self.entries = {entry['id']: entry for entry in entries}
            if len(entries) != len(self.entries) or set(self.entries) != set(STAGE_OWNERS):
                raise SkillAssetError('Application Skill registry must contain the explicit stages')
            for stage, entry in self.entries.items():
                if (set(entry) != {'id', 'owner', 'version', 'purpose', 'content', 'resources', 'input_fields',
                                  'output_contract_ref', 'regression_refs'}
                        or entry['owner'] != STAGE_OWNERS[stage] or not entry['version'] or not entry['purpose']
                        or not entry['content'] or not isinstance(entry['input_fields'], list)
                        or not entry['output_contract_ref'].startswith(STAGE_OWNERS[stage] + ':')):
                    raise SkillAssetError(f'Invalid stage declaration: {stage}')
                for reference in entry['regression_refs']:
                    if not reference.startswith(('tests/', 'evals/')) or not self._path(reference, asset=False).is_file():
                        raise SkillAssetError(f'Invalid regression reference: {stage}')
            # Read literal bindings once at startup. A missing repair block must fail
            # before the first model call, even when that repair is never reached.
            self.required_blocks = {stage: set() for stage in self.entries}
            for relative in set(STAGE_OWNERS.values()) | {'core/summary_validation.py'}:
                tree = ast.parse(self._path(relative, asset=False).read_text(encoding='utf-8'))
                for call in ast.walk(tree):
                    if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                            and call.func.id == 'skill_text' and len(call.args) == 2
                            and all(isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in call.args)):
                        stage, block = (arg.value for arg in call.args)
                        self.required_blocks[stage].add(block)
            self.snapshot()  # Startup validation; snapshots reread content on each new request.
        except (KeyError, TypeError, OSError, json.JSONDecodeError) as error:
            raise SkillAssetError('Cannot validate application Skill assets') from error

    def _path(self, relative, *, asset=True):
        if not isinstance(relative, str) or '\\' in relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
            raise SkillAssetError('Unsafe asset path')
        if asset and not (relative.startswith('skills/') or relative == 'prompts/conversation_summary.md'):
            raise SkillAssetError('Asset outside approved prompt directories')
        base, target = self.root, self.root/relative
        if self.research_dir is not None and relative.startswith('skills/travel-research/'):
            # Only the original two externally configurable files are overridden.
            if Path(relative).name in {'SKILL.md', 'sources.json'}:
                base, target = self.research_dir, self.research_dir/Path(relative).name
        target = target.resolve()
        if not target.is_relative_to(base) or not target.is_file():
            raise SkillAssetError(f'Missing or escaped asset: {relative}')
        return target

    def snapshot(self, stages=None):
        assets, reads = {}, {}
        for stage in (stages if stages is not None else self.entries):
            entry = self.entries[stage]
            blocks, resources, hashes = {}, {}, {}
            for relative in (STAGE_OWNERS[stage], *CONTRACT_FILES[stage]):
                path = self._path(relative, asset=False)
                if path not in reads:
                    reads[path] = path.read_bytes()
                hashes[relative] = hashlib.sha256(reads[path]).hexdigest()
            for category, target in (('content', blocks), ('resources', resources)):
                for key, relative in entry[category].items():
                    path = self._path(relative)
                    if path not in reads:
                        reads[path] = path.read_bytes()
                    raw = reads[path]
                    hashes[relative] = hashlib.sha256(raw).hexdigest()
                    text = raw.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
                    if category == 'resources':
                        json.loads(text)  # Current resources are JSON, never implicitly instructions.
                        target[key] = text
                    elif relative.endswith('.json'):
                        fragments = json.loads(text)
                        if (not isinstance(fragments, dict) or not fragments
                                or any(not isinstance(k, str) or not isinstance(v, str) or not v for k, v in fragments.items())
                                or blocks.keys() & fragments.keys()):
                            raise SkillAssetError(f'Invalid text fragments: {stage}')
                        blocks.update(fragments)
                    else:
                        if not text.strip() or key in blocks:
                            raise SkillAssetError(f'Empty or duplicate required text: {stage}')
                        blocks[key] = text
            if self.required_blocks[stage] - blocks.keys():
                raise SkillAssetError(f'Missing required protocol blocks: {stage}')
            bundle = digest({'id': stage, 'version': entry['version'], 'files': hashes,
                             'content': entry['content'], 'resources': entry['resources'],
                             'output_contract_ref': entry['output_contract_ref']})
            assets[stage] = SkillAsset(stage, entry['version'], entry['purpose'], MappingProxyType(blocks),
                                      MappingProxyType(resources), MappingProxyType(hashes), bundle)
        return SkillSnapshot(MappingProxyType(assets))

    def status(self):
        snapshot = self.snapshot()
        return {'schema_version': 'application-skills-v1', 'stages': [
            {**asset.metadata(), 'owner': self.entries[stage]['owner'], 'purpose': asset.purpose,
             'files': dict(asset.file_hashes)} for stage, asset in sorted(snapshot.assets.items())]}


_manager = None


def get_skill_manager():
    global _manager
    if _manager is None:
        _manager = SkillManager()
    return _manager


def skill_asset(stage):
    snapshot = CURRENT_SKILLS.get()
    return (snapshot if snapshot is not None else get_skill_manager().snapshot((stage,))).assets[stage]


def skill_text(stage, block):
    return skill_asset(stage).text(block)


@contextmanager
def skill_scope(manager=None):
    if CURRENT_SKILLS.get() is not None:
        yield CURRENT_SKILLS.get()
        return
    snapshot = (manager or get_skill_manager()).snapshot()
    token = CURRENT_SKILLS.set(snapshot)
    try:
        yield snapshot
    finally:
        CURRENT_SKILLS.reset(token)


def skill_request(function):
    """Freeze assets at an existing use-case boundary, including its repairs/substeps."""
    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def async_call(*args, **kwargs):
            with skill_scope(getattr(args[0], 'skill_manager', None) if args else None):
                return await function(*args, **kwargs)
        return async_call
    @wraps(function)
    def call(*args, **kwargs):
        with skill_scope(getattr(args[0], 'skill_manager', None) if args else None):
            return function(*args, **kwargs)
    return call
