"""Explicit release updater. Stdlib only; also runs as a saved recovery script.

Do not import application modules here: their files/environment may be replaced.
Network releases and process/environment operations are separate for offline tests.
"""
import argparse
from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid
import zipfile

REPOSITORY = 'zibitpnz/DesktopActionTool'
API = f'https://api.github.com/repos/{REPOSITORY}/releases/'
DOWNLOAD = f'https://github.com/{REPOSITORY}/releases/download/'
MAX_ARCHIVE = 64 * 1024 * 1024
ROOT_FILES = frozenset(('.gitignore', '.python-version', 'LICENSE', 'README.md',
    'install.bat', 'install.ps1', 'update.bat', 'update.ps1', 'mcp_server.py',
    'type_text.py', 'pyproject.toml', 'requirements.txt', 'settings.json', 'uv.lock'))
REQUIRED = ROOT_FILES - {'update.bat', 'update.ps1'} | {
    'desktop_action_tool/__init__.py', 'desktop_action_tool/desktop_cli.py',
    'desktop_action_tool/mcp_server.py'}
NEW_REQUIRED = REQUIRED | {'update.bat', 'update.ps1',
    'desktop_action_tool/update_runtime.py', 'desktop_action_tool/maintenance.py'}


class UpdateError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def fail(code, message, **details):
    raise UpdateError(code, message, **details)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def version(value):
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(
            r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value):
        fail('VERSION_INVALID', 'Expected a numeric X.Y.Z version.')
    return tuple(map(int, value.split('.')))


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_path(root, relative):
    """Validate lexical containment AND every existing ancestor before mutation.

    Do not resolve first: that would hide a junction at the requested location.
    """
    root = Path(os.path.abspath(root))
    candidate = Path(os.path.abspath(root / relative))
    if not candidate.is_relative_to(root) or candidate == root:
        fail('PATH_UNSAFE', 'Path is outside the installation directory.')
    for item in (root, *root.parents):
        # Inspect ancestors too; resolving first would conceal a redirected root.
        if item.is_symlink() or item.is_junction():
            fail('PATH_UNSAFE', 'Installation root must not be a link or junction.')
    current = root
    for part in candidate.relative_to(root).parts:
        current /= part
        if current.is_symlink() or current.is_junction():
            fail('PATH_UNSAFE', 'Links and junctions are not supported.', path=str(current))
        try:
            attributes = current.lstat().st_file_attributes
        except FileNotFoundError:
            continue
        if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            fail('PATH_UNSAFE', 'Reparse points are not supported.', path=str(current))
    return candidate


def check_tree(root, relative):
    target = safe_path(root, relative)
    if not target.is_dir():
        fail('ENVIRONMENT_MISSING', 'Expected a normal environment directory. Run install.bat first.')
    for parent, directories, files in os.walk(target, followlinks=False):
        for name in directories + files:
            safe_path(root, (Path(parent) / name).relative_to(root))
    return target


def atomic_write(root, relative, data):
    target = safe_path(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.update-', suffix='.tmp', dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        safe_path(root, relative)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(root, relative, data):
    atomic_write(root, relative, json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))


def read_json(root, relative):
    path = safe_path(root, relative)
    if path.stat().st_size > 2 * 1024 * 1024:
        fail('JOURNAL_INVALID', 'Updater journal exceeds the size limit.')
    return json.loads(path.read_text(encoding='utf-8'))


def allowed_name(name):
    return name in ROOT_FILES or bool(re.fullmatch(r'desktop_action_tool/[a-z_][a-z0-9_]*\.py', name))


def unpack_release(data, expected_version, *, candidate=False):
    """Return verified bytes, never extractall() into a user directory."""
    if len(data) > MAX_ARCHIVE:
        fail('ARCHIVE_INVALID', 'Release archive exceeds the size limit.')
    files, seen, total = {}, set(), 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > 256:
                fail('ARCHIVE_INVALID', 'Too many entries in the release archive.')
            for entry in archive.infolist():
                name = entry.filename
                mode = entry.external_attr >> 16
                if entry.orig_filename != name or name.casefold() in seen or entry.flag_bits & 1 or stat.S_ISLNK(mode):
                    fail('ARCHIVE_INVALID', 'Duplicate, encrypted or linked archive entry.')
                seen.add(name.casefold())
                if entry.is_dir():
                    if name != 'desktop_action_tool/':
                        fail('ARCHIVE_INVALID', 'Unexpected archive directory.')
                    continue
                if not allowed_name(name) or stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    fail('ARCHIVE_INVALID', 'Unexpected release file.', entry=name[:160])
                total += entry.file_size
                if entry.file_size > 8 * 1024 * 1024 or total > 128 * 1024 * 1024:
                    fail('ARCHIVE_INVALID', 'Expanded release exceeds the size limit.')
                files[name] = archive.read(entry)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
        fail('ARCHIVE_INVALID', 'Invalid release ZIP: ' + type(exc).__name__)
    required = NEW_REQUIRED if candidate else REQUIRED
    if not required <= files.keys():
        fail('ARCHIVE_INVALID', 'Release is missing required files.', missing=sorted(required - files.keys()))
    try:
        project = tomllib.loads(files['pyproject.toml'].decode('utf-8'))['project']
        lock = tomllib.loads(files['uv.lock'].decode('utf-8'))
        pin = files['.python-version'].decode('ascii').strip()
        if project['name'] != 'desktopactiontool' or project['version'] != expected_version:
            raise ValueError('project identity')
        version(pin)
        if not any(p.get('name') == 'desktopactiontool' and p.get('version') == expected_version
                   for p in lock.get('package', [])):
            raise ValueError('lock identity')
        json.loads(files['settings.json'])
    except (KeyError, TypeError, ValueError, UnicodeError):
        fail('ARCHIVE_INVALID', 'Release project, lock, Python pin or settings are inconsistent.')
    return files


class ReleaseRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        hosts = {'api.github.com'} if urlsplit(req.full_url).hostname == 'api.github.com' else {
            'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
        if target.scheme != 'https' or target.netloc.lower() not in hosts:
            fail('NETWORK_ERROR', 'Unexpected release download redirect.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHubReleases:
    def fetch(self, url, limit):
        deadline = time.monotonic() + 120
        request = Request(url, headers={'Accept': 'application/vnd.github+json' if url.startswith(API) else 'application/octet-stream',
            'User-Agent': 'DesktopActionTool-updater', 'X-GitHub-Api-Version': '2026-03-10'})
        try:
            with build_opener(ReleaseRedirects()).open(request, timeout=20) as response:
                output = bytearray()
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError()
                    chunk = response.read(min(65536, limit + 1 - len(output)))
                    if not chunk:
                        return bytes(output)
                    output.extend(chunk)
                    if len(output) > limit:
                        fail('RESPONSE_TOO_LARGE', 'GitHub response exceeds the size limit.')
        except HTTPError as exc:
            code = exc.code
            exc.close()
            fail('HTTP_ERROR', f'GitHub returned HTTP {code}; check availability or rate limits.', http_status=code)
        except (OSError, URLError):
            fail('NETWORK_ERROR', 'Cannot download from GitHub over verified HTTPS; check network/proxy and retry.')

    def release(self, installed=None):
        raw = self.fetch(API + ('tags/v' + installed if installed else 'latest'), 1024 * 1024)
        try:
            data = json.loads(raw)
            tag = data['tag_name']
            value = tag.removeprefix('v')
            version(value)
            if tag != 'v' + value or data['draft'] is not False or data['prerelease'] is not False or not data['published_at']:
                raise ValueError()
            if installed and installed != value:
                raise ValueError()
            if not isinstance(data['assets'], list) or len(data['assets']) > 256 or any(not isinstance(a, dict) for a in data['assets']):
                raise ValueError()
            return {'version': value, 'tag': tag, 'assets': data['assets'],
                    'url': f'https://github.com/{REPOSITORY}/releases/tag/{tag}'}
        except (KeyError, TypeError, ValueError, AttributeError):
            fail('RELEASE_INVALID', 'GitHub did not return the expected published stable release.')

    def asset(self, release, name, limit):
        matches = [a for a in release['assets'] if a.get('name') == name]
        if len(matches) != 1:
            fail('ASSET_MISSING', 'The release must contain one ' + name)
        asset = matches[0]
        url = DOWNLOAD + release['tag'] + '/' + name
        digest = asset.get('digest', '')
        if (asset.get('browser_download_url') != url or asset.get('state') != 'uploaded'
                or type(asset.get('size')) is not int or not 0 < asset['size'] <= limit
                or not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest)):
            fail('ASSET_INVALID', 'Release asset URL, size or GitHub SHA-256 is invalid.')
        data = self.fetch(url, limit)
        if len(data) != asset['size'] or sha(data) != digest[7:]:
            fail('CHECKSUM_MISMATCH', 'Downloaded asset does not match its GitHub size/digest.')
        return data

    def files(self, release, *, candidate=False):
        name = 'DesktopActionTool-' + release['tag'] + '.zip'
        sums = self.asset(release, 'SHA256SUMS.txt', 65536)
        try:
            matches = re.findall(r'^([0-9a-fA-F]{64}) [ *]' + re.escape(name) + r'\r?$', sums.decode('ascii'), re.MULTILINE)
        except UnicodeError:
            matches = []
        if len(matches) != 1:
            fail('CHECKSUM_INVALID', 'SHA256SUMS.txt must contain exactly one checksum for the release ZIP.')
        data = self.asset(release, name, MAX_ARCHIVE)
        if sha(data) != matches[0].lower():
            fail('CHECKSUM_MISMATCH', 'Release ZIP does not match SHA256SUMS.txt.')
        return unpack_release(data, release['version'], candidate=candidate)


@contextmanager
def updater_lock(root):
    from ctypes import wintypes as w
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    for name, args, result in [('CreateMutexW', [ctypes.c_void_p, w.BOOL, w.LPCWSTR], w.HANDLE),
            ('WaitForSingleObject', [w.HANDLE, w.DWORD], w.DWORD),
            ('ReleaseMutex', [w.HANDLE], w.BOOL), ('CloseHandle', [w.HANDLE], w.BOOL)]:
        getattr(api, name).argtypes, getattr(api, name).restype = args, result
    handle = api.CreateMutexW(None, False, 'Local\\DesktopActionTool-update-' + sha(str(root).casefold().encode()))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        result = api.WaitForSingleObject(handle, 0)
        if result == 258:
            fail('UPDATER_BUSY', 'Another updater is already running for this installation.')
        if result not in (0, 128):
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = True
        yield
    finally:
        if acquired:
            api.ReleaseMutex(handle)
        api.CloseHandle(handle)


class ChildJob:
    """Own only our uv/probe descendants, including if the updater is terminated."""
    def __init__(self, process):
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [('user', ctypes.c_int64), ('job', ctypes.c_int64), ('flags', w.DWORD),
                ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t), ('active', w.DWORD),
                ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', ctypes.c_uint64 * 6), ('memory', ctypes.c_size_t * 4)]
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        for name, args, result in [('CreateJobObjectW', [ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
                ('SetInformationJobObject', [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
                ('AssignProcessToJobObject', [w.HANDLE, w.HANDLE], w.BOOL), ('CloseHandle', [w.HANDLE], w.BOOL)]:
            getattr(self.api, name).argtypes, getattr(self.api, name).restype = args, result
        self.handle = self.api.CreateJobObjectW(None, None)
        limits = Extended()
        limits.basic.flags = 0x2000
        if not self.handle or not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def clean_environment():
    # uv configuration from the user's shell must not redirect the target env.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(('UV_', 'PYTHON'))
           and k.upper() not in {'VIRTUAL_ENV', 'DESKTOPACTION_CONTROLLER', 'DESKTOPACTION_MAINTENANCE_TOKEN'}}
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
    return env


def run_child(command, cwd, *, timeout=60, environment=None, log=None):
    """Logs go to a file; bounded probes return UTF-8, never inherited console input."""
    with tempfile.TemporaryFile() as capture:
        process = subprocess.Popen(list(map(str, command)), cwd=cwd, env=environment or clean_environment(),
            stdin=subprocess.DEVNULL, stdout=capture, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        job = None
        try:
            job = ChildJob(process)
            process.wait(timeout=timeout)
        finally:
            if job:
                job.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            capture.seek(0)
            output = capture.read(2 * 1024 * 1024)
            if log:
                # log lives in our verified transaction directory.
                with open(log, 'ab') as stream:
                    stream.write(output + b'\n')
        if process.returncode:
            fail('COMMAND_FAILED', 'Environment command failed; inspect the transaction log.', exit_code=process.returncode)
        if len(output) >= 2 * 1024 * 1024:
            fail('COMMAND_FAILED', 'Environment probe output exceeds the size limit.')
        return output.decode('utf-8').strip()


class WindowsEnvironment:
    def __init__(self, uv=None, offline=False):
        self.explicit_uv, self.offline, self.uv = uv, offline, None

    def idle(self, root):
        # Inspect only process identity; never print full command lines/user text.
        script = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(pythonw?|uv)([0-9.]*)?\\.exe$' } | Select-Object ProcessId,ExecutablePath,CommandLine) | ConvertTo-Json -Compress"
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        try:
            output = run_child([powershell, '-NoProfile', '-NonInteractive', '-Command', script], root, timeout=30)
            processes = json.loads(output or '[]')
            if isinstance(processes, dict):
                processes = [processes]
            prefix, busy = str(root).casefold() + '\\', []
            for process in processes:
                if process['ProcessId'] == os.getpid():
                    continue
                executable = (process.get('ExecutablePath') or '').casefold()
                command = (process.get('CommandLine') or '').casefold().replace('/', '\\')
                if not executable or not command:
                    fail('PROCESS_CHECK_FAILED', 'A Python/uv process cannot be inspected; retry with matching access rights.',
                         process_ids=[process['ProcessId']])
                if executable.startswith(prefix) or prefix in command:
                    busy.append(process['ProcessId'])
            if busy:
                fail('APPLICATION_BUSY', 'Close this installation\'s MCP/CLI sessions and retry. No processes were stopped.', process_ids=busy)
        except (ValueError, KeyError, subprocess.TimeoutExpired):
            fail('PROCESS_CHECK_FAILED', 'Cannot inspect running processes; stop MCP/CLI and retry.')

    def components(self, root):
        check_tree(root, '.venv')
        probe = "import importlib.metadata as m,json,sys; print(json.dumps({'prefix':sys.prefix,'packages':{d.metadata['Name'].lower():d.version for d in m.distributions()}}))"
        info = json.loads(run_child([root / '.venv/Scripts/python.exe', '-I', '-B', '-c', probe], root))
        if Path(info['prefix']).resolve() != root / '.venv':
            fail('ENVIRONMENT_INVALID', 'Python does not belong to this installation.')
        packages = info['packages']
        if ('uiautomation' in packages) != ('comtypes' in packages):
            fail('ENVIRONMENT_INVALID', 'Incomplete UI Automation environment. Repair with install.bat first.')
        return [extra for extra, package in [('images', 'pillow'), ('mcp', 'mcp'), ('uia', 'uiautomation')] if package in packages]

    def resolve_uv(self, root):
        candidates = [self.explicit_uv] if self.explicit_uv else [shutil.which('uv.exe'),
            Path.home() / '.local/bin/uv.exe', safe_path(root, '.tools/uv-0.12.10.exe')]
        for candidate in candidates:
            if not candidate or not Path(candidate).is_file():
                continue
            path = Path(candidate).resolve()
            if path.is_relative_to(root / '.venv'):
                fail('UV_INVALID', 'uv must be installed outside the replaced .venv.')
            if path == root / '.tools/uv-0.12.10.exe' and sha(path.read_bytes()) != 'a8bf95637ba520491de06713d718a55b90f18d127980b9531fd8fc5a8e99dc1d':
                fail('UV_INVALID', 'Cached uv checksum mismatch.')
            output = run_child([path, '--version'], root)
            match = re.match(r'uv ([0-9]+\.[0-9]+\.[0-9]+)', output)
            if not match or version(match[1]) < (0, 12, 10):
                fail('UV_INVALID', 'uv 0.12.10 or newer is required.')
            self.uv = path
            return
        fail('UV_MISSING', 'Install uv first or provide -UvPath pointing to uv.exe.')

    def install(self, root, extras, token, log):
        command = [self.uv, '--no-config', '--directory', root, 'sync', '--project', root,
                   '--locked', '--no-default-groups', '--python', (root / '.python-version').read_text().strip()]
        for extra in extras:
            command += ['--extra', extra]
        if self.offline:
            command.append('--offline')
        environment = clean_environment()
        environment['DESKTOPACTION_MAINTENANCE_TOKEN'] = token
        run_child(command, root, timeout=600, environment=environment, log=log)
        self.verify(root, extras, token, log)

    def verify(self, root, extras, token, log):
        environment = clean_environment()
        environment['DESKTOPACTION_MAINTENANCE_TOKEN'] = token
        # No window enumeration, screenshots, frame, mouse or keyboard input.
        probe = '''
import importlib.metadata as m, json, pathlib, struct, sys, tomllib
root = pathlib.Path(sys.argv[1]).resolve()
extras = json.loads(sys.argv[2])
assert pathlib.Path(sys.prefix).resolve() == root / '.venv'
assert struct.calcsize('P') == 8
assert '.'.join(map(str, sys.version_info[:3])) == (root / '.python-version').read_text().strip()
project = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']
for extra in extras:
    for requirement in project['optional-dependencies'][extra]:
        package, expected = requirement.split('==')
        assert m.version(package) == expected
sys.path.insert(0, str(root))
if 'images' in extras:
    from PIL import Image
    import io
    stream = io.BytesIO(); Image.new('RGB', (2,2)).save(stream, format='PNG')
if 'uia' in extras:
    from desktop_action_tool.uia_worker import use_memory_com_cache
    use_memory_com_cache()
    import uiautomation
if 'mcp' in extras:
    from desktop_action_tool.mcp_server import create_server
    from desktop_action_tool.mcp_bridge import Bridge
    create_server(Bridge(root))
print('environment verified')
'''
        python = root / '.venv/Scripts/python.exe'
        run_child([python, '-I', '-B', '-c', probe, root, json.dumps(extras)], root, environment=environment, log=log)
        for entry, arguments in [('type_text.py', ['--version']), ('type_text.py', ['--help']),
                ('type_text.py', ['--text', 'update probe', '--dry-run', '--quiet']),
                ('mcp_server.py', ['--version'])]:
            result = run_child([python, '-B', root / entry, *arguments], root, environment=environment, log=log)
            if '--version' in arguments and json.loads(result)['version'] != local_version(root):
                fail('PROBE_FAILED', 'Entry point reports the wrong release version.')
            if '--dry-run' in arguments:
                data = json.loads(result)
                if not data.get('ok') or data.get('typed_chars') != 0:
                    fail('PROBE_FAILED', 'CLI dry-run failed.')


def local_version(root):
    value = tomllib.loads(safe_path(root, 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    version(value)
    return value


class Updater:
    state_dir = Path('.tools/updater')
    active_path = state_dir / 'active.json'
    last_path = state_dir / 'last.json'

    def __init__(self, root, releases=None, environment=None):
        self.root = Path(os.path.abspath(root))
        safe_path(self.root, self.state_dir)
        self.releases = releases or GitHubReleases()
        self.environment = environment or WindowsEnvironment()

    def load_transaction(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{32}', identifier):
            fail('JOURNAL_INVALID', 'Invalid backup identifier.')
        relative = self.state_dir / 'transactions' / identifier
        data = read_json(self.root, relative / 'transaction.json')
        if data.get('schema') != 1 or data.get('id') != identifier or data.get('root') != str(self.root):
            fail('JOURNAL_INVALID', 'Backup belongs to another installation or journal format.')
        for key in ('old', 'new'):
            mapping = data.get(key)
            if not isinstance(mapping, dict) or not mapping or len(mapping) > 256:
                fail('JOURNAL_INVALID', 'Invalid backup manifest.')
            for name, digest in mapping.items():
                if not allowed_name(name) or name == 'settings.json' or not re.fullmatch('[0-9a-f]{64}', digest):
                    fail('JOURNAL_INVALID', 'Unsafe backup manifest.')
        return data, relative

    def status(self):
        for label, path in [('incomplete', self.active_path), ('last', self.last_path)]:
            if safe_path(self.root, path).exists():
                record = read_json(self.root, path)
                data, relative = self.load_transaction(record['id'])
                return {'ok': True, 'status': data['phase'], 'incomplete': label == 'incomplete',
                        'backup_id': data['id'], 'from_version': data['from_version'], 'to_version': data['to_version'],
                        'backup_path': str(self.root / relative), 'updated_at': data['updated_at']}
        return {'ok': True, 'status': 'no_updates_recorded', 'incomplete': False}

    def check(self):
        current = local_version(self.root)
        release = self.releases.release()
        latest = release['version']
        comparison = version(latest) > version(current)
        return {'ok': True, 'status': 'update_available' if comparison else 'up_to_date' if current == latest else 'local_newer',
                'current_version': current, 'latest_version': latest, 'update_available': comparison, 'release_url': release['url']}, release

    def assert_files(self, old, new):
        conflicts = []
        for name in sorted(old.keys() | new.keys()):
            target = safe_path(self.root, name)
            if name in old:
                if not target.is_file() or sha(target.read_bytes()) != old[name]:
                    conflicts.append(name)
            elif target.exists():
                conflicts.append(name)
        if conflicts:
            fail('LOCAL_CHANGES', 'Release files have local changes or collide with new files. Preserve/reconcile them manually before updating.', files=conflicts)

    def save(self, data, relative, phase=None):
        if phase:
            data['phase'] = phase
        data['updated_at'] = now()
        write_json(self.root, relative / 'transaction.json', data)

    def activate(self, data):
        write_json(self.root, self.active_path, {'schema': 1, 'id': data['id'], 'token': data['token'], 'owner_pid': os.getpid()})

    def finish(self, data, relative, phase):
        self.save(data, relative, phase)
        if phase != 'aborted':
            write_json(self.root, self.last_path, {'id': data['id']})
        safe_path(self.root, self.active_path).unlink(missing_ok=True)

    def update(self):
        if safe_path(self.root, self.active_path).exists():
            fail('RECOVERY_REQUIRED', 'An incomplete update exists. Run update.bat -Status, then -Rollback.')
        result, release = self.check()
        if not result['update_available']:
            return result
        print('Checking release files and installed components...', file=sys.stderr, flush=True)
        self.environment.idle(self.root)
        extras = self.environment.components(self.root)
        self.environment.resolve_uv(self.root)
        old_files = self.releases.files(self.releases.release(result['current_version']))
        new_files = self.releases.files(release, candidate=True)
        old = {n: sha(b) for n, b in old_files.items() if n != 'settings.json'}
        new = {n: sha(b) for n, b in new_files.items() if n != 'settings.json'}
        self.assert_files(old, new)
        identifier = uuid.uuid4().hex
        relative = self.state_dir / 'transactions' / identifier
        candidate = safe_path(self.root, relative / 'candidate')
        candidate.mkdir(parents=True)
        data = {'schema': 1, 'id': identifier, 'root': str(self.root), 'from_version': result['current_version'],
                'to_version': release['version'], 'old': old, 'new': new, 'extras': extras,
                'token': uuid.uuid4().hex, 'environment_phase': 'untouched', 'updated_at': now()}
        self.save(data, relative, 'preparing')
        for name, content in new_files.items():
            atomic_write(self.root, relative / 'candidate' / name, content)
        settings = safe_path(self.root, 'settings.json')
        if settings.exists():
            atomic_write(self.root, relative / 'candidate/settings.json', settings.read_bytes())
        log = safe_path(self.root, relative / 'update.log')
        # Keep an independent recovery entry point and the base interpreter identity.
        atomic_write(self.root, self.state_dir / 'recovery.py', Path(__file__).read_bytes())
        write_json(self.root, self.state_dir / 'recovery.json', {'python': sys.executable, 'root': str(self.root)})
        print('Testing the candidate in an isolated environment...', file=sys.stderr, flush=True)
        try:
            self.environment.install(candidate, extras, data['token'], log)
            # The staged venv cannot be moved into place (absolute paths inside it).
            # It is disposable; only remove this owned, checked directory.
            staged_env = check_tree(self.root, relative / 'candidate/.venv')
            shutil.rmtree(staged_env)
            self.assert_files(old, new)
            for name in old:
                atomic_write(self.root, relative / 'code' / name, safe_path(self.root, name).read_bytes())
            self.save(data, relative, 'prepared')
            # Persistent marker is written before the last process scan or any replacement.
            self.activate(data)
            self.environment.idle(self.root)
            self.assert_files(old, new)
            current_env = check_tree(self.root, '.venv')
            backup_env = safe_path(self.root, relative / 'environment')
            data['environment_phase'] = 'saving'
            self.save(data, relative, 'replacing')
            os.rename(current_env, backup_env)
            data['environment_phase'] = 'saved'
            self.save(data, relative)
            print('Installing the release; the previous environment is backed up...', file=sys.stderr, flush=True)
            for name in sorted(old.keys() | new.keys()):
                target = safe_path(self.root, name)
                # Refuse an edit made after preflight; rollback also checks for conflicts.
                expected = old.get(name)
                observed = sha(target.read_bytes()) if target.is_file() else None
                if observed != expected:
                    fail('LOCAL_CHANGES', 'A source file changed during the update.', files=[name])
                if name in new:
                    atomic_write(self.root, name, new_files[name])
                else:
                    target.unlink()
            self.save(data, relative, 'installing')
            self.environment.install(self.root, extras, data['token'], log)
            self.assert_files(new, {})
            self.finish(data, relative, 'committed')
            return {**result, 'status': 'updated', 'installed_version': release['version'], 'extras': extras,
                    'backup_id': identifier, 'backup_path': str(self.root / relative), 'reconnect_mcp': True}
        except (Exception, KeyboardInterrupt) as exc:
            if data['environment_phase'] == 'untouched':
                self.finish(data, relative, 'aborted')
                raise
            try:
                self.restore(data, relative)
            except (Exception, KeyboardInterrupt) as recovery:
                self.save(data, relative, 'recovery_required')
                fail('RECOVERY_REQUIRED', 'Update failed; automatic rollback needs attention. Run -Status and -Rollback.',
                     backup_id=identifier, cause=type(exc).__name__, recovery_error=str(recovery))
            fail('UPDATE_ROLLED_BACK', 'Update failed; previous source files and environment were restored.',
                 backup_id=identifier, cause=exc.code if isinstance(exc, UpdateError) else type(exc).__name__)

    def restore(self, data, relative):
        old, new = data['old'], data['new']
        # Validate ALL files before restoring ANY; never overwrite later user edits.
        conflicts = []
        for name in old.keys() | new.keys():
            target = safe_path(self.root, name)
            current = sha(target.read_bytes()) if target.is_file() else None
            if current not in (None, old.get(name), new.get(name)) or target.exists() and not target.is_file():
                conflicts.append(name)
            if name in old:
                backup = safe_path(self.root, relative / 'code' / name)
                if not backup.is_file() or sha(backup.read_bytes()) != old[name]:
                    fail('BACKUP_INVALID', 'Source backup is missing or modified.', files=[name])
        if conflicts:
            fail('ROLLBACK_CONFLICT', 'Source files changed after update; preserve/reconcile these edits before rollback.', files=sorted(conflicts))
        self.activate(data)
        self.environment.idle(self.root)
        self.save(data, relative, 'restoring')
        current_env = safe_path(self.root, '.venv')
        backup_env = safe_path(self.root, relative / 'environment')
        state = data['environment_phase']
        if state == 'saving' and not backup_env.exists():
            # Crash before the first rename: no runtime files were touched yet.
            if not current_env.is_dir():
                fail('BACKUP_INVALID', 'Original environment cannot be found.')
            data['environment_phase'] = 'restored'
        elif state not in ('restored', 'untouched'):
            if backup_env.exists():
                check_tree(self.root, relative / 'environment')
                if current_env.exists():
                    check_tree(self.root, '.venv')
                    discarded = safe_path(self.root, relative / ('environment-after-' + uuid.uuid4().hex))
                    os.rename(current_env, discarded)
                data['environment_phase'] = 'restoring'
                self.save(data, relative)
                os.rename(backup_env, current_env)
            elif state != 'restoring' or not current_env.is_dir():
                fail('BACKUP_INVALID', 'Original environment backup cannot be found.')
            data['environment_phase'] = 'restored'
        self.save(data, relative)
        for name in sorted(old.keys() | new.keys()):
            target = safe_path(self.root, name)
            observed = sha(target.read_bytes()) if target.is_file() else None
            if observed not in (None, old.get(name), new.get(name)):
                fail('ROLLBACK_CONFLICT', 'A source file changed during rollback.', files=[name])
            if name in old:
                atomic_write(self.root, name, safe_path(self.root, relative / 'code' / name).read_bytes())
            else:
                target.unlink(missing_ok=True)
        self.environment.verify(self.root, data['extras'], data['token'], safe_path(self.root, relative / 'update.log'))
        self.finish(data, relative, 'rolled_back')

    def rollback(self):
        active = safe_path(self.root, self.active_path).exists()
        pointer = self.active_path if active else self.last_path
        if not safe_path(self.root, pointer).exists():
            fail('BACKUP_MISSING', 'There is no update backup for this installation.')
        data, relative = self.load_transaction(read_json(self.root, pointer)['id'])
        if data['phase'] in ('aborted', 'rolled_back'):
            if active:
                safe_path(self.root, self.active_path).unlink()
            return {'ok': True, 'status': data['phase'], 'backup_id': data['id']}
        if data['environment_phase'] == 'untouched':
            self.finish(data, relative, 'aborted')
            return {'ok': True, 'status': 'aborted', 'backup_id': data['id']}
        if data['phase'] == 'committed':
            self.assert_files(data['new'], {})
        self.restore(data, relative)
        return {'ok': True, 'status': 'rolled_back', 'installed_version': data['from_version'],
                'backup_id': data['id'], 'reconnect_mcp': True}


def main(argv=None):
    sys.dont_write_bytecode = True
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--action', choices=('check', 'update', 'rollback', 'status'), default='check')
    parser.add_argument('--uv')
    parser.add_argument('--offline', action='store_true', help='Use cached Python/packages; release checks still require HTTPS.')
    args = parser.parse_args(argv)
    try:
        if os.name != 'nt' or sys.version_info < (3, 14) or ctypes.sizeof(ctypes.c_void_p) != 8:
            fail('PLATFORM_UNSUPPORTED', 'Updater requires Windows x64 and Python 3.14 or newer.')
        root = Path(os.path.abspath(args.root))
        if Path(sys.executable).resolve().is_relative_to(root / '.venv'):
            fail('PYTHON_IN_ENVIRONMENT', 'Run update.bat with a base Python outside the replaced .venv.')
        updater = Updater(root, environment=WindowsEnvironment(args.uv, args.offline))
        if args.action == 'status':
            # Journal files are replaced atomically; status is useful while the
            # exclusive updater is still installing or waiting for dependencies.
            result = updater.status()
        else:
            with updater_lock(root):
                if args.action == 'check':
                    result, _ = updater.check()
                else:
                    result = getattr(updater, args.action)()
    except UpdateError as exc:
        result = {'ok': False, 'error_code': exc.code, 'error': str(exc), **exc.details}
    except KeyboardInterrupt:
        result = {'ok': False, 'error_code': 'CANCELLED', 'error': 'Updater interrupted. Run -Status before restarting the tool.'}
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        result = {'ok': False, 'error_code': 'UPDATE_FAILED', 'error': type(exc).__name__ + ': ' + str(exc)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
