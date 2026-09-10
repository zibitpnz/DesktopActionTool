"""Read local version and check the project's public GitHub release on demand."""
import argparse
from datetime import datetime, timezone
import json
import math
from .project_paths import PROJECT_ROOT
import re
import socket
import ssl
import time
import tomllib
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

REPOSITORY = 'zibitpnz/DesktopActionTool'
API_URL = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
RELEASES_URL = f'https://github.com/{REPOSITORY}/releases'
DEFAULT_TIMEOUT = 10
MAX_RESPONSE_BYTES = 1024 * 1024


def version_parts(value, *, tag=False):
    """Project releases use X.Y.Z (optionally prefixed by v for Git tags)."""
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('expected a numeric X.Y.Z version')
    prefix = 'v?' if tag else ''
    if not re.fullmatch(prefix + r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value, flags=re.ASCII):
        raise ValueError('expected a numeric X.Y.Z version')
    return tuple(int(part) for part in value.removeprefix('v').split('.'))


def current_version():
    project = PROJECT_ROOT / 'pyproject.toml'
    value = tomllib.loads(project.read_text(encoding='utf-8'))['project']['version']
    version_parts(value)
    return value


class GitHubRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urlsplit(newurl)
        if url.scheme != 'https' or url.netloc.lower() != 'api.github.com':
            raise HTTPError(req.full_url, code, 'unexpected update-check redirect', headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_release(timeout_s, check_cancelled):
    deadline = time.monotonic() + timeout_s
    request = Request(API_URL, method='GET', headers={
        'Accept': 'application/vnd.github+json', 'User-Agent': 'DesktopActionTool-update-check',
        'X-GitHub-Api-Version': '2026-03-10'})
    check_cancelled()
    with build_opener(GitHubRedirects()).open(request, timeout=timeout_s) as response:
        if response.status != 200:
            raise HTTPError(API_URL, response.status, 'unexpected response', response.headers, None)
        data = bytearray()
        while True:
            check_cancelled()
            if time.monotonic() >= deadline:
                raise TimeoutError('update check timed out')
            chunk = response.read(min(65536, MAX_RESPONSE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError('GitHub release response exceeds the size limit')
        check_cancelled()
        if time.monotonic() >= deadline:
            raise TimeoutError('update check timed out')
    return json.loads(data.decode('utf-8'))


def check_updates(*, timeout_s=DEFAULT_TIMEOUT, dry_run=False, check_cancelled=lambda: None):
    if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 1 <= timeout_s <= 30:
        raise ValueError('update timeout must be a finite number from 1 to 30 seconds')
    result = {'ok': False, 'mode': 'check-updates', 'repository': REPOSITORY,
              'current_version': None, 'latest_version': None, 'update_available': None,
              'status': 'unknown', 'releases_url': RELEASES_URL}
    def failure(code, message, **extra):
        return {**result, 'error_code': code, 'error': message, **extra}
    try:
        result['current_version'] = current_version()
    except (OSError, ValueError, KeyError, TypeError):
        return failure('LOCAL_VERSION_INVALID', 'cannot read a numeric project version from pyproject.toml')
    if dry_run:
        return {**result, 'ok': True, 'dry_run': True, 'status': 'not_checked',
                'request_url': API_URL, 'timeout_s': timeout_s}
    try:
        release = fetch_release(timeout_s, check_cancelled)
        check_cancelled()
    except HTTPError as exc:
        try:
            status = exc.code
            headers = exc.headers or {}
            limited = status == 429 or status == 403 and (
                headers.get('X-RateLimit-Remaining') == '0' or headers.get('Retry-After') is not None)
            if limited:
                return failure('UPDATE_RATE_LIMITED', 'GitHub request limit reached; retry later', http_status=status)
            if status == 404:
                return failure('UPDATE_RELEASE_NOT_FOUND', 'no latest public release found, or repository is unavailable', http_status=status)
            return failure('UPDATE_HTTP_ERROR', f'GitHub release request returned HTTP {status}', http_status=status)
        finally:
            exc.close()
    except (TimeoutError, socket.timeout):
        return failure('UPDATE_TIMEOUT', 'GitHub update check timed out')
    except URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return failure('UPDATE_TIMEOUT', 'GitHub update check timed out')
        if isinstance(exc.reason, ssl.SSLError):
            return failure('UPDATE_TLS_ERROR', 'could not verify the HTTPS connection to GitHub')
        return failure('UPDATE_NETWORK_ERROR', 'cannot reach GitHub; check the network or proxy and retry')
    except ssl.SSLError:
        return failure('UPDATE_TLS_ERROR', 'could not verify the HTTPS connection to GitHub')
    except OSError:
        return failure('UPDATE_NETWORK_ERROR', 'cannot reach GitHub; check the network or proxy and retry')
    except (ValueError, UnicodeError):
        return failure('UPDATE_RESPONSE_INVALID', 'GitHub returned an invalid or oversized release response')
    if not isinstance(release, dict) or release.get('draft') is not False or release.get('prerelease') is not False:
        return failure('UPDATE_RESPONSE_INVALID', 'GitHub did not return a published stable release')
    try:
        latest = version_parts(release.get('tag_name'), tag=True)
    except ValueError:
        return failure('UPDATE_VERSION_UNSUPPORTED', 'latest release tag is not vX.Y.Z or X.Y.Z; compare releases manually')
    published = release.get('published_at')
    try:
        if not isinstance(published, str) or len(published) > 40:
            raise ValueError('missing publication time')
        timestamp = datetime.fromisoformat(published.replace('Z', '+00:00'))
        if timestamp.tzinfo is None:
            raise ValueError('missing time zone')
        published_utc = timestamp.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return failure('UPDATE_RESPONSE_INVALID', 'GitHub release has no valid publication date')
    local = version_parts(result['current_version'])
    return {**result, 'ok': True, 'latest_version': '.'.join(map(str, latest)),
            'update_available': latest > local,
            'status': 'update_available' if latest > local else 'local_newer' if latest < local else 'up_to_date',
            'release_tag': release['tag_name'],
            'release_url': RELEASES_URL + '/tag/' + quote(release['tag_name'], safe=''),
            'published_at': published_utc,
            'checked_at': datetime.now(timezone.utc).isoformat()}


def network_timeout(value):
    try:
        timeout = float(value)
        if not math.isfinite(timeout) or not 1 <= timeout <= 30:
            raise ValueError()
        return timeout
    except ValueError:
        raise argparse.ArgumentTypeError('update timeout must be 1..30 seconds') from None


def add_arguments(parser):
    info = parser.add_argument_group('Version and updates (no desktop actions)')
    commands = info.add_mutually_exclusive_group()
    commands.add_argument('--version', action='store_true', help='Print the local project version as JSON; no network request.')
    commands.add_argument('--check-updates', action='store_true', help='Check the latest stable GitHub release; never installs or downloads assets.')
    info.add_argument('--update-timeout-s', type=network_timeout, help='Network timeout for --check-updates: 1..30 seconds, default 10.')


def run_cli(argv, *, check_cancelled=lambda: None):
    """Information commands bypass window state and desktop settings entirely."""
    if '--version' not in argv and '--check-updates' not in argv:
        return None
    parser = argparse.ArgumentParser(description='DesktopActionTool version and GitHub release check.')
    add_arguments(parser)
    parser.add_argument('--dry-run', action='store_true', help='Describe the check without accessing GitHub.')
    parser.add_argument('--quiet', action='store_true', help='Compatibility flag; output is always JSON.')
    args = parser.parse_args(argv)
    if args.update_timeout_s is not None and not args.check_updates:
        parser.error('--update-timeout-s requires --check-updates')
    check_cancelled()
    if args.version:
        try:
            result = {'ok': True, 'mode': 'version', 'version': current_version()}
        except (OSError, ValueError, KeyError, TypeError):
            result = {'ok': False, 'error_code': 'LOCAL_VERSION_INVALID',
                      'error': 'cannot read a numeric project version from pyproject.toml'}
    else:
        result = check_updates(timeout_s=args.update_timeout_s if args.update_timeout_s is not None else DEFAULT_TIMEOUT,
                               dry_run=args.dry_run, check_cancelled=check_cancelled)
    check_cancelled()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1
