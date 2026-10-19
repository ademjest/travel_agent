"""Write-only credentials, merged atomically into a fixed project .env."""
from contextlib import contextmanager
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import tempfile
import threading
import re
from urllib.parse import urlsplit

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from infrastructure.public_http import public_address, request_public
from core.model_pricing import parse_profiles


FIELDS = (
    'SEARCH_API_KEY', 'SEARCH_BASE_URL', 'LLM_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL_ID', 'AMAP_API_KEY',
    'LLM_INTENT_MODEL_ID', 'LLM_SUMMARY_MODEL_ID', 'LLM_RESEARCH_MODEL_ID', 'LLM_VISION_MODEL_ID',
    'LLM_PROVIDER_ALIAS', 'LLM_CONFIG_VERSION',
    'AI_BUDGET_TIER', 'AI_DAILY_TOKEN_LIMIT', 'AI_MONTHLY_TOKEN_LIMIT',
    'AI_SUMMARY_ENABLED', 'AI_VECTOR_ENABLED', 'AI_EMBEDDING_MODEL_DIR', 'AI_EMBEDDING_MIN_SIMILARITY',
    'LLM_INPUT_PRICE_PER_M', 'LLM_CACHED_INPUT_PRICE_PER_M', 'LLM_OUTPUT_PRICE_PER_M',
    'LLM_PRICE_CURRENCY', 'LLM_MODEL_PRICES', 'AI_DAILY_COST_LIMIT', 'AI_MONTHLY_COST_LIMIT', 'AI_COST_CURRENCY',
)
SECRET_FIELDS = {'SEARCH_API_KEY', 'LLM_API_KEY', 'AMAP_API_KEY'}
SEARCH_URL = 'https://api.tavily.com'
WEB_OVERRIDES_KEY = 'WEB_SETTINGS_OVERRIDE_KEYS'
START_ENV = {key: os.environ[key] for key in FIELDS if key in os.environ}
VERSION_KEY = secrets.token_bytes(32)
LOCK = threading.RLock()


class SettingsConflict(ValueError):
    pass


class SettingsInputError(ValueError):
    """A fixed, credential-free validation message that may be shown in the UI."""


def private_file(path):
    if os.name == 'nt':
        # No secret enters a shell command or process argument. Only the file path is passed.
        script = ('$p=$env:TRAVEL_ACL_TARGET; $acl=New-Object System.Security.AccessControl.FileSecurity; '
                  '$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User; '
                  '$rule=New-Object System.Security.AccessControl.FileSystemAccessRule($sid,"FullControl","Allow"); '
                  '$acl.SetAccessRuleProtection($true,$false); $acl.AddAccessRule($rule); '
                  '$ErrorActionPreference="Stop"; [System.IO.File]::SetAccessControl($p,$acl)')
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                env={**{k: v for k, v in os.environ.items() if k.upper() in
                                    {'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'USERPROFILE', 'PSMODULEPATH'}},
                                     'TRAVEL_ACL_TARGET': str(path)},
                                capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise ValueError('无法设置配置文件权限，未保存。')
    else:
        os.chmod(path, 0o600)


@contextmanager
def file_lock(path):
    with LOCK, path.open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SettingsConflict('其他进程正在保存，请稍后重试。') from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class EnvSettingsStore:
    def __init__(self, root, inherited=None):
        self.root = Path(root).resolve()
        self.path = self.root / '.env'
        self.inherited = dict(START_ENV if inherited is None else inherited)
        self.initial = self._read()
        self.runtime = self._effective(self.initial)[0]
        self._probe_lock = threading.Lock()

    def _read(self):
        for path in (self.path, self.root / '.env.settings.lock'):
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink > 1
                        or getattr(info, 'st_file_attributes', 0) & 0x400):
                    raise ValueError('配置路径必须是普通文件。')
        return self.path.read_bytes() if self.path.exists() else b''

    @staticmethod
    def _override_keys(local):
        try:
            keys = json.loads(local.get(WEB_OVERRIDES_KEY) or '[]')
        except (ValueError, TypeError):
            raise ValueError('网页配置覆盖记录格式无效。') from None
        if (not isinstance(keys, list) or any(not isinstance(key, str) or key not in FIELDS
                                             or key == 'SEARCH_BASE_URL' for key in keys)):
            raise ValueError('网页配置覆盖记录包含不支持的字段。')
        return set(keys)

    @staticmethod
    def _parse(raw):
        try:
            text = raw.decode('utf-8-sig')
            bindings = list(parse_stream(io.StringIO(text)))
            keys = [b.key for b in bindings if b.key in (*FIELDS, WEB_OVERRIDES_KEY)]
            if any(b.error for b in bindings) or len(keys) != len(set(keys)):
                raise ValueError()
            return bindings, dict(dotenv_values(stream=io.StringIO(text), interpolate=False))
        except (UnicodeError, ValueError):
            raise ValueError('配置格式异常或字段重复，未修改文件。') from None

    def _effective(self, raw):
        _, local = self._parse(raw)
        overrides = self._override_keys(local)
        parent_path = self.root.parent / '.env'
        parent = dict(dotenv_values(parent_path)) if parent_path.is_file() else {}
        # Expansion of existing values must match load_dotenv; new ${...} values are rejected.
        expanded = dict(dotenv_values(stream=io.StringIO(raw.decode('utf-8-sig'))))
        values, sources = {}, {}
        for key in FIELDS:
            source = ('project' if key in overrides and key in local else 'environment' if key in self.inherited
                      else 'project' if key in local else 'parent' if key in parent else 'default')
            values[key] = (self.inherited if source == 'environment' else expanded if source == 'project' else parent).get(key) or ''
            sources[key] = source
        values['SEARCH_BASE_URL'] = values['SEARCH_BASE_URL'] or SEARCH_URL
        defaults = {'AI_BUDGET_TIER': 'balanced', 'AI_DAILY_TOKEN_LIMIT': '0',
                    'AI_MONTHLY_TOKEN_LIMIT': '0', 'AI_SUMMARY_ENABLED': 'false',
                    'AI_VECTOR_ENABLED': 'false', 'AI_EMBEDDING_MIN_SIMILARITY': '0.5', 'LLM_PRICE_CURRENCY': 'USD'}
        for key, default in defaults.items():
            values[key] = values[key] or default
        return values, sources

    def apply_web_overrides(self):
        """Apply only explicitly saved Web overrides, after ordinary dotenv loading."""
        _, local = self._parse(self.initial)
        for key in self._override_keys(local):
            if key in local:
                os.environ[key] = self.runtime[key]

    def _version(self, raw):
        return hmac.new(VERSION_KEY, raw, hashlib.sha256).hexdigest()

    def status(self):
        raw = self._read()
        values, sources = self._effective(raw)
        _, local = self._parse(raw)
        overrides = self._override_keys(local)
        fields = {}
        for key in FIELDS:
            value = values[key]
            safe_value = '' if key in SECRET_FIELDS else value
            if key.endswith('_URL') and value:
                try:
                    self.validate(key, value, network=False)
                except ValueError:
                    safe_value = ''
            fields[key] = {'configured': bool(value), 'source': sources[key],
                'editable': key != 'SEARCH_BASE_URL', 'value': safe_value,
                'overrides_environment': key in overrides,
                'runtime_configured': bool(self.runtime.get(key)), 'pending_restart': value != self.runtime.get(key)}
        return {'version': self._version(raw), 'fields': fields, 'restart_required': values != self.runtime,
                'provider': 'tavily', 'scope': 'project', 'plaintext_on_disk': True}

    @staticmethod
    def validate(key, value, *, network=True):
        if key not in FIELDS or not isinstance(value, str) or len(value) > 4096:
            raise ValueError('配置字段或长度无效。')
        if any(ord(c) < 32 or ord(c) == 127 for c in value) or '${' in value:
            raise ValueError('配置不允许控制字符或环境插值语法。')
        if key == 'LLM_MODEL_PRICES':
            parse_profiles(value)
        if key == 'AI_BUDGET_TIER' and value and value not in {'economy', 'balanced', 'quality'}:
            raise ValueError('预算档位必须是 economy、balanced 或 quality。')
        if key == 'AI_EMBEDDING_MIN_SIMILARITY' and value:
            try:
                valid = 0 <= float(value) <= 1
            except ValueError:
                valid = False
            if not valid:
                raise ValueError('语义检索相似度门槛必须为0到1。')
        if key in {'AI_SUMMARY_ENABLED', 'AI_VECTOR_ENABLED'} and value.lower() not in {'', 'true', 'false'}:
            raise ValueError('开关值必须是 true 或 false。')
        if key in {'AI_DAILY_TOKEN_LIMIT', 'AI_MONTHLY_TOKEN_LIMIT'} and value:
            if not value.isdigit() or not 0 <= int(value) <= 10**12:
                raise ValueError('token 限额必须是 0 到 1000000000000 的整数。')
        if key in {'LLM_INPUT_PRICE_PER_M', 'LLM_CACHED_INPUT_PRICE_PER_M', 'LLM_OUTPUT_PRICE_PER_M', 'AI_DAILY_COST_LIMIT', 'AI_MONTHLY_COST_LIMIT'} and value:
            try:
                price = float(value)
            except ValueError:
                raise ValueError('模型费率必须是非负数字。') from None
            if not 0 <= price <= 10**9:
                raise ValueError('模型费率必须是非负数字。')
        if key in {'LLM_PRICE_CURRENCY','AI_COST_CURRENCY'} and value and not re.fullmatch(r'[A-Z]{3}', value.upper()):
            raise ValueError('费率币种必须是三位字母代码。')
        if key.endswith('_URL') and value:
            parsed = urlsplit(value)
            try:
                valid = (parsed.scheme == 'https' and parsed.hostname and parsed.username is None
                         and parsed.password is None and not parsed.query and not parsed.fragment and parsed.port in (None, 443))
            except ValueError:
                valid = False
            if not valid:
                raise ValueError('服务地址需要不含凭据或查询参数的 HTTPS 公网地址。')
            if key == 'SEARCH_BASE_URL' and value.rstrip('/') != SEARCH_URL:
                raise ValueError('当前搜索供应商仅支持 https://api.tavily.com。')
            if network:
                public_address(value)

    def _check_git(self):
        def git(*args):
            return subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, timeout=8)
        try:
            if git('rev-parse', '--is-inside-work-tree').returncode != 0:
                return
            if git('ls-files', '--error-unmatch', '--', '.env').returncode == 0:
                raise ValueError('.env 已被 Git 跟踪，须先解除跟踪，未保存新凭据。')
            if git('check-ignore', '-q', '--', '.env').returncode != 0:
                raise ValueError('.env 未被 Git 忽略，未保存新凭据。')
        except FileNotFoundError:
            if (self.root / '.git').exists():
                raise ValueError('无法检查 Git 配置，未保存。') from None

    def save(self, version, changes):
        if not isinstance(changes, dict) or any(key not in FIELDS for key in changes):
            raise ValueError('存在不支持的配置字段。')
        changes = {key: value for key, value in changes.items() if value != ''}
        for key, value in changes.items():
            self.validate(key, '' if value is None else value)
        with file_lock(self.root / '.env.settings.lock'):
            raw = self._read()
            if not isinstance(version, str) or not hmac.compare_digest(version, self._version(raw)):
                raise SettingsConflict('配置已经变化，请刷新后重试。')
            self._check_git()
            bindings, local = self._parse(raw)
            overrides = self._override_keys(local)
            effective, _ = self._effective(raw)
            for url_key, secret_key in [('LLM_BASE_URL', 'LLM_API_KEY'), ('SEARCH_BASE_URL', 'SEARCH_API_KEY')]:
                if url_key in changes and (changes[url_key] or '').rstrip('/') != effective[url_key].rstrip('/'):
                    if effective[secret_key] and not changes.get(secret_key):
                        raise SettingsInputError('更换服务地址时必须重新输入用于该地址的 Key。')
            if not changes:
                return self.status()
            overrides.update(key for key in changes if key in self.inherited and key != 'SEARCH_BASE_URL')
            if overrides:
                changes[WEB_OVERRIDES_KEY] = json.dumps(sorted(overrides))
            def line(key):
                value = changes[key] or ''  # explicit null suppresses parent fallback
                value = value.replace('\\', '\\\\').replace("'", "\\'")
                return f"{key}='{value}'\n"
            result = ''.join(line(b.key) if b.key in changes else b.original.string for b in bindings)
            if result and not result.endswith('\n'):
                result += '\n'
            result += ''.join(line(key) for key in changes if key not in local)
            _, checked = self._parse(result.encode())
            if any(checked.get(key) != (value or '') for key, value in changes.items()):
                raise ValueError('配置序列化校验失败，未保存。')
            fd, name = tempfile.mkstemp(prefix='.env.settings-', dir=self.root)
            temporary = Path(name)
            os.close(fd)
            try:
                private_file(temporary)
                with temporary.open('wb') as handle:
                    handle.write(result.encode('utf-8'))
                    handle.flush()
                    os.fsync(handle.fileno())
                if self._read() != raw:
                    raise SettingsConflict('保存期间配置被其他程序修改，请刷新。')
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
        return self.status()

    def test(self, service):
        values, _ = self._effective(self._read())
        if service == 'search':
            from infrastructure.search_client import SearchClient
            SearchClient(values['SEARCH_API_KEY'], values['SEARCH_BASE_URL']).search('武汉 官方 旅游')
        elif service == 'llm':
            key, url = values['LLM_API_KEY'], values['LLM_BASE_URL']
            if not key or not url:
                raise ValueError('尚未配置模型服务。')
            self.validate('LLM_BASE_URL', url)
            request_public(url.rstrip('/') + '/models', headers={'Authorization': 'Bearer ' + key}, max_bytes=200_000)
        elif service == 'amap':
            raise ValueError('高德请使用“一键测试全部服务”验证。')
        else:
            raise ValueError('不支持的服务测试。')
        return {'status': 'ok', 'detail': '服务请求成功；不代表所有模型/工具能力都可用。'}

    def test_all(self, store):
        if not self._probe_lock.acquire(blocking=False):
            raise SettingsConflict('服务测试正在运行，请等待完成后再试。')
        try:
            from infrastructure.service_probes import run_service_probes
            values, _ = self._effective(self._read())
            result = run_service_probes(values, store)
            result['restart_required'] = values != self.runtime
            return result
        finally:
            self._probe_lock.release()
