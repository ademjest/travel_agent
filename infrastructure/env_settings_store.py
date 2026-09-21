"""Write-only credentials, merged atomically into a fixed project .env."""
from contextlib import contextmanager
import hashlib
import hmac
import io
import os
from pathlib import Path
import secrets
import stat
import subprocess
import tempfile
import threading
from urllib.parse import urlsplit

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from infrastructure.public_http import public_address, request_public


FIELDS = ('SEARCH_API_KEY', 'SEARCH_BASE_URL', 'LLM_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL_ID', 'AMAP_API_KEY')
SECRET_FIELDS = {'SEARCH_API_KEY', 'LLM_API_KEY', 'AMAP_API_KEY'}
SEARCH_URL = 'https://api.tavily.com'
START_ENV = {key: os.environ[key] for key in FIELDS if key in os.environ}
VERSION_KEY = secrets.token_bytes(32)
LOCK = threading.RLock()


class SettingsConflict(ValueError):
    pass


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

    def _read(self):
        for path in (self.path, self.root / '.env.settings.lock'):
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink > 1
                        or getattr(info, 'st_file_attributes', 0) & 0x400):
                    raise ValueError('配置路径必须是普通文件。')
        return self.path.read_bytes() if self.path.exists() else b''

    @staticmethod
    def _parse(raw):
        try:
            text = raw.decode('utf-8-sig')
            bindings = list(parse_stream(io.StringIO(text)))
            keys = [b.key for b in bindings if b.key in FIELDS]
            if any(b.error for b in bindings) or len(keys) != len(set(keys)):
                raise ValueError()
            return bindings, dict(dotenv_values(stream=io.StringIO(text), interpolate=False))
        except (UnicodeError, ValueError):
            raise ValueError('配置格式异常或字段重复，未修改文件。') from None

    def _effective(self, raw):
        _, local = self._parse(raw)
        parent_path = self.root.parent / '.env'
        parent = dict(dotenv_values(parent_path)) if parent_path.is_file() else {}
        # Expansion of existing values must match load_dotenv; new ${...} values are rejected.
        expanded = dict(dotenv_values(stream=io.StringIO(raw.decode('utf-8-sig'))))
        values, sources = {}, {}
        for key in FIELDS:
            source = 'environment' if key in self.inherited else 'project' if key in local else 'parent' if key in parent else 'default'
            values[key] = (self.inherited if source == 'environment' else expanded if source == 'project' else parent).get(key) or ''
            sources[key] = source
        values['SEARCH_BASE_URL'] = values['SEARCH_BASE_URL'] or SEARCH_URL
        return values, sources

    def _version(self, raw):
        return hmac.new(VERSION_KEY, raw, hashlib.sha256).hexdigest()

    def status(self):
        raw = self._read()
        values, sources = self._effective(raw)
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
                'editable': sources[key] != 'environment', 'value': safe_value,
                'runtime_configured': bool(self.runtime.get(key)), 'pending_restart': value != self.runtime.get(key)}
        return {'version': self._version(raw), 'fields': fields, 'restart_required': values != self.runtime,
                'provider': 'tavily', 'scope': 'project', 'plaintext_on_disk': True}

    @staticmethod
    def validate(key, value, *, network=True):
        if key not in FIELDS or not isinstance(value, str) or len(value) > 4096:
            raise ValueError('配置字段或长度无效。')
        if any(ord(c) < 32 or ord(c) == 127 for c in value) or '${' in value:
            raise ValueError('配置不允许控制字符或环境插值语法。')
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
            if key in self.inherited:
                raise ValueError('该字段由启动环境控制，请在启动环境修改。')
            self.validate(key, '' if value is None else value)
        with file_lock(self.root / '.env.settings.lock'):
            raw = self._read()
            if not isinstance(version, str) or not hmac.compare_digest(version, self._version(raw)):
                raise SettingsConflict('配置已经变化，请刷新后重试。')
            self._check_git()
            bindings, local = self._parse(raw)
            effective, _ = self._effective(raw)
            for url_key, secret_key in [('LLM_BASE_URL', 'LLM_API_KEY'), ('SEARCH_BASE_URL', 'SEARCH_API_KEY')]:
                if url_key in changes and (changes[url_key] or '').rstrip('/') != effective[url_key].rstrip('/'):
                    if effective[secret_key] and not changes.get(secret_key):
                        raise ValueError('更换服务地址时必须重新输入用于该地址的 Key。')
            if not changes:
                return self.status()
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
            # Existing Amap API requires key in URL. Keep it out of generic test/logging path.
            raise ValueError('高德请在重启后使用天气查询验证，本面板不执行 URL 携带密钥的连接测试。')
        else:
            raise ValueError('不支持的服务测试。')
        return {'status': 'ok', 'detail': '服务请求成功；不代表所有模型/工具能力都可用。'}
