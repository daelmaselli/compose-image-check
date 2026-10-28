#!/usr/bin/env python3
"""Compose image checker v1.8.0. Read-only, local or --ssh user@host.

Compares OCI rootfs diff_ids (not tag names) with published image configs.
Does not pull, build, recreate containers, or inspect local data volumes.
"""
import argparse
import json
import re
import shlex
import subprocess
import sys
import os
import time
import hashlib
import shutil
from collections import Counter
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path


DOCKER_BIN = None
COMPOSE_MODE = None
COMPOSE_V1_BIN = None
BUILDX_AVAILABLE = None


def safe_error(stderr):
    """Best-effort scrub; never emit full Docker output without --debug."""
    value = (stderr or '').strip()[:3000]
    value = re.sub(r'(?i)(https?://)[^\s/@]+:[^\s/@]+@', r'\1[credentials]@', value)
    value = re.sub(r'(?i)(token|password|passwd|secret|authorization|api[_-]?key)([=:\s]+)([^\s]+)', r'\1\2[redacted]', value)
    value = re.sub(r'(?i)([?&](?:token|key|signature|access_token|auth)=)[^&\s]+', r'\1[redacted]', value)
    return value


def resolve_docker():
    global DOCKER_BIN
    if DOCKER_BIN is not None:
        return DOCKER_BIN
    choices = [shutil.which('docker')]
    choices += ['/usr/local/bin/docker', '/usr/bin/docker', '/bin/docker', '/snap/bin/docker', '/opt/homebrew/bin/docker']
    DOCKER_BIN = next((c for c in choices if c and os.path.isfile(c) and os.access(c, os.X_OK)), '')
    if not DOCKER_BIN:
        raise RuntimeError('docker not found in PATH or standard locations; PATH=' + os.environ.get('PATH', ''))
    log_debug('docker resolved: ' + DOCKER_BIN + '; PATH=' + os.environ.get('PATH', ''))
    return DOCKER_BIN


def cmd(*args, cwd=None, timeout=90):
    argv = list(args)
    if argv and argv[0] == 'docker':
        argv[0] = resolve_docker()
    log_debug('running: ' + shlex.join(argv) + (' (cwd=' + cwd + ')' if cwd else ''))
    try:
        p = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('unable to run ' + shlex.join(argv[:4]) + ': ' + str(exc)) from exc
    if p.returncode:
        if DEBUG:
            log_debug('stderr (sanitized, max 3000 characters): ' + (safe_error(p.stderr) or '(empty)'))
        raise RuntimeError('command failed (exit {}) [{}]{}'.format(
            p.returncode, shlex.join(argv),
            ('; stderr: ' + safe_error(p.stderr)) if DEBUG else '; use --debug to view stderr'))
    return p.stdout


def js(*args, cwd=None):
    return json.loads(cmd(*args, cwd=cwd))


def inspect_image(ref):
    if not ref:
        return None
    try:
        return js('docker', 'image', 'inspect', ref)[0]
    except (RuntimeError, ValueError, IndexError):
        return None


def layers(image):
    if not isinstance(image, dict):
        return None
    fs = image.get('RootFS') or image.get('rootfs') or {}
    return fs.get('Layers') if 'Layers' in fs else fs.get('diff_ids')


def image_platform(image):
    if not image:
        return None
    arch = image.get('Architecture') or image.get('architecture')
    if not arch:
        return None
    return '/'.join(filter(None, [image.get('Os') or image.get('os') or 'linux',
                                  arch, image.get('Variant') or image.get('variant')]))


remote_cache = {}
dockerhub_throttled = False
CACHE_TTL = 6 * 3600
USE_DISK_CACHE = True
CACHE_PATH = Path.home() / '.cache' / 'compose-image-check' / 'registry-v2.json'
OLD_CACHE_PATH = CACHE_PATH.with_name('registry-v1.json')
disk_cache = {}
cache_dirty = False
HUB_INTERVAL = 8.0  # seconds between high-level Docker Hub operations
OTHER_INTERVAL = 1.0
last_registry_op = {}
metrics = Counter()
DEBUG = False
PROGRESS = False
VERBOSE = False


def log_debug(message):
    if DEBUG:
        print('DEBUG: ' + message, file=sys.stderr)


def throttle(ref, kind):
    # Limits start rate of operations initiated by this process. Buildx may
    # generate multiple HTTP calls internally, outside our direct control.
    registry = 'dockerhub' if is_dockerhub_ref(ref) else ref.split('/')[0]
    interval = HUB_INTERVAL if registry == 'dockerhub' else OTHER_INTERVAL
    previous = last_registry_op.get(registry)
    if previous is not None:
        wait = interval - (time.monotonic() - previous)
        if wait > 0:
            log_debug(f'{registry}: waiting {wait:.1f}s before {kind}')
            time.sleep(wait)
    last_registry_op[registry] = time.monotonic()
    metrics[kind] += 1
    log_debug(f'{registry}: starting {kind} for {ref}')



def load_cache():
    global disk_cache
    if not USE_DISK_CACHE:
        return
    try:
        data = json.loads(CACHE_PATH.read_text(encoding='utf-8'))
        disk_cache = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        disk_cache = {}
    # Import entries from v1.5.1 without renewing their timestamps. Both
    # versions hash (reference, platform) identically. A v1 entry has no
    # manifest_digest: it can be reused within the 6h TTL, then must be
    # refreshed (HEAD + Buildx) once to establish the digest baseline.
    try:
        old = json.loads(OLD_CACHE_PATH.read_text(encoding='utf-8'))
        if isinstance(old, dict):
            for key, record in old.items():
                if (key not in disk_cache and isinstance(record, dict)
                        and isinstance(record.get('checked'), (int, float))
                        and isinstance(record.get('layers'), list)):
                    disk_cache[key] = {'checked': record['checked'],
                                       'layers': record['layers'],
                                       'manifest_digest': None}
                    globals()['cache_dirty'] = True
    except (OSError, ValueError):
        pass


def save_cache():
    if not USE_DISK_CACHE or not cache_dirty:
        return
    try:
        CACHE_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = CACHE_PATH.with_name(CACHE_PATH.name + '.tmp-' + str(os.getpid()))
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        fd = os.open(temp, flags, 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(disk_cache, f, separators=(',', ':'))
            os.replace(temp, CACHE_PATH)
        finally:
            if temp.exists():
                temp.unlink()
    except OSError:
        pass  # Cache is an optimization only.


def cache_key(ref, plat):
    return hashlib.sha256(json.dumps([ref, plat], separators=(',', ':')).encode()).hexdigest()


def cached_record(ref, plat):
    if not USE_DISK_CACHE:
        return None
    record = disk_cache.get(cache_key(ref, plat))
    if not isinstance(record, dict) or not isinstance(record.get('layers'), list):
        return None
    return record


def cached_layers(ref, plat):
    record = cached_record(ref, plat)
    if not record or time.time() - record.get('checked', 0) > CACHE_TTL:
        return None
    return {'rootfs': {'diff_ids': record['layers']}}


def save_layers(ref, plat, obj, manifest_digest=None):
    global cache_dirty
    if not USE_DISK_CACHE:
        return
    val = layers(obj)
    if not isinstance(val, list):
        return
    disk_cache[cache_key(ref, plat)] = {'checked': time.time(), 'layers': val, 'manifest_digest': manifest_digest}
    cache_dirty = True



def is_dockerhub_ref(ref):
    registry = ref.split('/')[0]
    return '/' not in ref or ('.' not in registry and ':' not in registry and registry != 'localhost') or registry in ('docker.io', 'index.docker.io')


def classify_registry_error(stderr):
    message = stderr.lower()
    if '429' in message or 'too many requests' in message or 'toomanyrequests' in message:
        return 'RATE_LIMIT', 'HTTP 429: registry rate limit'
    if '401' in message or '403' in message or 'unauthorized' in message or 'denied' in message or 'authentication required' in message:
        return 'AUTH', 'registry access denied (401/403 or authentication required)'
    if 'manifest unknown' in message or 'not found' in message or '404' in message:
        return 'NOT_FOUND', 'tag or manifest not found'
    if 'timeout' in message or 'connection refused' in message or 'no such host' in message or 'network is unreachable' in message:
        return 'NETWORK', 'connection or DNS error'
    return 'REGISTRY_ERROR', 'unclassified registry/Buildx error'


def manifest_head(ref):
    """Resolve the digest of the tag's top-level manifest with HEAD only.

    This is a cheap freshness check. For multi-platform images the top-level
    digest may change because of another architecture; that causes an extra
    config lookup but never a false OK.
    """
    parts = ref.split('/')
    first = parts[0]
    if len(parts) == 1 or ('.' not in first and ':' not in first and first != 'localhost'):
        registry = 'registry-1.docker.io'
        repo = ref
        if '/' not in repo:
            repo = 'library/' + repo
    else:
        registry = first
        if registry in ('docker.io', 'index.docker.io'):
            registry = 'registry-1.docker.io'
        repo = '/'.join(parts[1:])
    # Image references with digests are already pinned by the caller.
    if '@sha256:' in repo:
        repo, tag = repo.split('@', 1)
    else:
        last = repo.rsplit('/', 1)[-1]
        tag = last.rsplit(':', 1)[-1] if ':' in last else 'latest'
        if ':' in last:
            repo = repo[:-(len(tag) + 1)]
    url = 'https://' + registry + '/v2/' + repo + '/manifests/' + tag
    accepts = ', '.join([
        'application/vnd.oci.image.index.v1+json',
        'application/vnd.oci.image.manifest.v1+json',
        'application/vnd.docker.distribution.manifest.list.v2+json',
        'application/vnd.docker.distribution.manifest.v2+json',
    ])
    headers = {'Accept': accepts, 'User-Agent': 'compose-image-check/1.7.0'}

    def request():
        req = urllib.request.Request(url, headers=headers, method='HEAD')
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.headers.get('Docker-Content-Digest')

    try:
        digest = request()
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            return None, classify_registry_error(str(exc.code) + ' ' + str(exc.reason))[0]
        challenge = exc.headers.get('WWW-Authenticate', '')
        if not challenge.lower().startswith('bearer '):
            return None, 'AUTH'
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = params.get('realm', '')
        if not realm.startswith('https://'):
            return None, 'AUTH'
        qs = {k: v for k, v in params.items() if k in ('service', 'scope')}
        token_url = realm + ('&' if '?' in realm else '?') + urllib.parse.urlencode(qs)
        try:
            with urllib.request.urlopen(urllib.request.Request(token_url, headers={'User-Agent': 'compose-image-check/1.7.0'}), timeout=20) as auth:
                data = json.load(auth)
            token = data.get('token') or data.get('access_token')
            if not token:
                return None, 'AUTH'
            headers['Authorization'] = 'Bearer ' + token
            digest = request()
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as auth_exc:
            if isinstance(auth_exc, urllib.error.HTTPError):
                return None, classify_registry_error(str(auth_exc.code) + ' ' + str(auth_exc.reason))[0]
            return None, 'NETWORK'
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, 'NETWORK'
    if not digest or not re.fullmatch(r'sha256:[a-fA-F0-9]{64}', digest):
        return None, 'FORMAT_ERROR'
    return digest, ''


def buildx_available():
    global BUILDX_AVAILABLE
    if BUILDX_AVAILABLE is None:
        proc = subprocess.run([resolve_docker(), 'buildx', 'version'],
                              capture_output=True, text=True, timeout=12, check=False)
        BUILDX_AVAILABLE = proc.returncode == 0
        log_debug('Buildx ' + ('available' if BUILDX_AVAILABLE else 'unavailable: using OCI registry fallback'))
    return BUILDX_AVAILABLE


def oci_config(ref, plat):
    """Read OCI manifest/config JSON (not layers) without Buildx.

    Uses anonymous registry token authentication for public repositories.
    Does not write tokens to cache or diagnostics.
    """
    parts = ref.split('/')
    first = parts[0]
    if len(parts) == 1 or ('.' not in first and ':' not in first and first != 'localhost'):
        registry = 'registry-1.docker.io'
        repo = ref if '/' in ref else 'library/' + ref
    else:
        registry = 'registry-1.docker.io' if first in ('docker.io', 'index.docker.io') else first
        repo = '/'.join(parts[1:])
    if '@' in repo:
        repo, tag = repo.rsplit('@', 1)
    elif ':' in repo.rsplit('/', 1)[-1]:
        repo, tag = repo.rsplit(':', 1)
    else:
        tag = 'latest'
    base = f'https://{registry}/v2/{repo}/'
    accepts = ', '.join((
        'application/vnd.oci.image.index.v1+json',
        'application/vnd.docker.distribution.manifest.list.v2+json',
        'application/vnd.oci.image.manifest.v1+json',
        'application/vnd.docker.distribution.manifest.v2+json'))
    bearer = None
    def fetch(path, accept):
        nonlocal bearer
        url = base + path
        if not url.startswith('https://'):
            raise ValueError('OCI URL is not HTTPS')
        for attempt in range(2):
            headers = {'Accept': accept, 'User-Agent': 'compose-image-check/1.7.5'}
            if bearer:
                headers['Authorization'] = 'Bearer ' + bearer
            throttle(ref, 'oci-get')
            try:
                req = urllib.request.Request(url, headers=headers, method='GET')
                with urllib.request.urlopen(req, timeout=30) as response:
                    if int(response.headers.get('Content-Length', 0)) > 8 * 1024 * 1024:
                        raise ValueError('registry response is too large')
                    data = response.read(8 * 1024 * 1024 + 1)
                    if len(data) > 8 * 1024 * 1024:
                        raise ValueError('registry response is too large')
                    return json.loads(data)
            except urllib.error.HTTPError as error:
                if error.code != 401 or attempt or not error.headers.get('WWW-Authenticate', '').lower().startswith('bearer '):
                    raise
                challenge = error.headers['WWW-Authenticate']
                params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
                realm = params.get('realm', '')
                if not realm.startswith('https://'):
                    raise ValueError('registry auth realm is not HTTPS')
                query = urllib.parse.urlencode({k: v for k, v in params.items() if k in ('service', 'scope')})
                token_url = realm + ('&' if '?' in realm else '?') + query
                throttle(ref, 'oci-auth')
                with urllib.request.urlopen(urllib.request.Request(token_url, headers={'User-Agent': 'compose-image-check/1.7.5'}), timeout=30) as response:
                    auth = json.load(response)
                bearer = auth.get('token') or auth.get('access_token')
                if not bearer:
                    raise ValueError('registry token is missing')
        raise ValueError('registry authentication failed')
    try:
        manifest = fetch('manifests/' + urllib.parse.quote(tag, safe=':@'), accepts)
        if 'manifests' in manifest:
            wanted = (plat or '').split('/')
            choices = []
            for entry in manifest['manifests']:
                platform = entry.get('platform') or {}
                if platform.get('os') == (wanted[0] if wanted else 'linux') and len(wanted) > 1 and platform.get('architecture') == wanted[1]:
                    variant = wanted[2] if len(wanted) > 2 else None
                    if not variant or not platform.get('variant') or variant == platform.get('variant'):
                        choices.append(entry)
            if not choices:
                return None, 'FORMAT_ERROR: OCI platform unavailable: ' + str(plat)
            manifest = fetch('manifests/' + choices[0]['digest'], accepts)
        config = manifest.get('config', {}).get('digest')
        if not config or not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', config):
            return None, 'FORMAT_ERROR: OCI config digest is missing'
        result = fetch('blobs/' + config, 'application/vnd.oci.image.config.v1+json, application/vnd.docker.container.image.v1+json')
        if layers(result) is None:
            return None, 'FORMAT_ERROR: rootfs diff_ids are missing from OCI config'
        return result, ''
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, OSError) as err:
        if isinstance(err, urllib.error.HTTPError):
            kind, reason = classify_registry_error(str(err.code) + ' ' + str(err.reason))
        elif isinstance(err, ValueError):
            kind, reason = 'FORMAT_ERROR', str(err)
        else:
            kind, reason = 'NETWORK', 'registry connection failed'
        log_debug('OCI: ' + kind + ': ' + safe_error(reason))
        return None, kind + ': ' + reason


def remote_config(ref, plat):
    global dockerhub_throttled
    key = (ref, plat)
    if key in remote_cache:
        return remote_cache[key]
    cached = cached_layers(ref, plat)
    if cached is not None:
        metrics['cache_hit'] += 1
        remote_cache[key] = (cached, 'CACHE')
        return remote_cache[key]
    if dockerhub_throttled and is_dockerhub_ref(ref):
        remote_cache[key] = (None, 'RATE_LIMIT: Docker Hub gia limitato, richiesta saltata')
        return remote_cache[key]
    previous = cached_record(ref, plat)
    throttle(ref, 'head')
    digest, head_err = manifest_head(ref)
    if head_err == 'RATE_LIMIT' and is_dockerhub_ref(ref):
        dockerhub_throttled = True
    if head_err and head_err != 'AUTH':
        remote_cache[key] = (None, head_err + ': unable to verify manifest (HEAD)')
        return remote_cache[key]
    # On private registries HEAD may be unavailable anonymously; Buildx can
    # still use credentials from Docker's configured credential store.
    if (previous and digest and previous.get('manifest_digest') == digest):
        obj = {'rootfs': {'diff_ids': previous['layers']}}
        save_layers(ref, plat, obj, digest)
        metrics['head_unchanged'] += 1
        remote_cache[key] = (obj, 'HEAD_OK')
        return remote_cache[key]
    if not buildx_available():
        result, error = oci_config(ref, plat)
        remote_cache[key] = (result, error)
        if result is not None:
            save_layers(ref, plat, result, digest)
        elif error.startswith('RATE_LIMIT') and is_dockerhub_ref(ref):
            dockerhub_throttled = True
        return remote_cache[key]
    # Buildx emits an object for single-platform images and a platform-indexed
    # map for multi-platform images. Handle both. Output is image config only.
    template = '{{json .Image}}'
    try:
        throttle(ref, 'buildx')
        proc = subprocess.run(
            [resolve_docker(), 'buildx', 'imagetools', 'inspect', '--format', template, ref],
            text=True, capture_output=True, timeout=90, check=False)
        if proc.returncode:
            log_debug('buildx stderr (sanitized): ' + safe_error(proc.stderr))
            code, reason = classify_registry_error(proc.stderr)
            if code == 'RATE_LIMIT' and is_dockerhub_ref(ref):
                dockerhub_throttled = True
            remote_cache[key] = (None, code + ': ' + reason)
            return remote_cache[key]
        try:
            obj = json.loads(proc.stdout)
        except ValueError:
            remote_cache[key] = (None, 'FORMAT_ERROR: Buildx output is not JSON')
            return remote_cache[key]
        if isinstance(obj, dict) and layers(obj) is not None:
            result = obj
        elif isinstance(obj, dict):
            result = obj.get(plat)
            if result is None and plat and plat.startswith('linux/arm64/'):
                result = obj.get('linux/arm64')
            if result is None and plat and plat.startswith('linux/arm/v'):
                result = obj.get('linux/arm')
        else:
            result = None
        if layers(result) is None:
            result = None
        remote_cache[key] = (result, '' if result else 'FORMAT_ERROR: remote platform/config unavailable')
        if result is not None:
            save_layers(ref, plat, result, digest)
    except (OSError, subprocess.TimeoutExpired) as exc:
        remote_cache[key] = (None, 'NETWORK: unable to run Buildx command or command timed out')
    return remote_cache[key]


def all_containers():
    try:
        ids = cmd('docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project').split()
        out = {}
        for start in range(0, len(ids), 80):
            for c in js('docker', 'container', 'inspect', *ids[start:start+80]):
                labels = c.get('Config', {}).get('Labels') or {}
                project, service = labels.get('com.docker.compose.project'), labels.get('com.docker.compose.service')
                if project and service and str(labels.get('com.docker.compose.oneoff')).lower() != 'true':
                    key = (project, service)
                    if key not in out or c.get('State', {}).get('Running'):
                        out[key] = c.get('Image')
        return out
    except (ValueError, RuntimeError):
        return {}


def detect_compose():
    """Prefer plugin v2; fall back to standalone compose v1."""
    global COMPOSE_MODE, COMPOSE_V1_BIN
    if COMPOSE_MODE:
        return COMPOSE_MODE
    try:
        cmd('docker', 'compose', 'version')
        COMPOSE_MODE = 'v2'
    except RuntimeError:
        candidates = [shutil.which('docker-compose'), '/usr/local/bin/docker-compose',
                      '/usr/bin/docker-compose', '/snap/bin/docker-compose']
        COMPOSE_V1_BIN = next((x for x in candidates if x and os.path.isfile(x)
                              and os.access(x, os.X_OK)), None)
        if not COMPOSE_V1_BIN:
            raise RuntimeError('Compose v2 is unavailable and docker-compose v1 was not found')
        cmd(COMPOSE_V1_BIN, 'version')
        COMPOSE_MODE = 'v1'
    log_debug('Compose detected: ' + COMPOSE_MODE)
    return COMPOSE_MODE


def discover_projects_legacy(include_all):
    """Discover Compose projects from Docker labels, without assuming directories."""
    ids = cmd('docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project').split()
    projects = {}
    for start in range(0, len(ids), 80):
        for c in js('docker', 'container', 'inspect', *ids[start:start+80]):
            labels = c.get('Config', {}).get('Labels') or {}
            project = labels.get('com.docker.compose.project')
            if not project or labels.get('com.docker.compose.oneoff', '').lower() == 'true':
                continue
            if not include_all and not c.get('State', {}).get('Running'):
                continue
            item = projects.setdefault(project, {'Name': project, 'ConfigFiles': [], 'WorkingDir': '', 'LegacyServices': {}})
            service = labels.get('com.docker.compose.service')
            if service:
                item['LegacyServices'][service] = c.get('Config', {}).get('Image')
            workdir = labels.get('com.docker.compose.project.working_dir')
            if workdir:
                item['WorkingDir'] = workdir
            raw = labels.get('com.docker.compose.project.config_files') or ''
            files = [x.strip() for x in raw.split(',') if x.strip()]
            for f in files:
                if not os.path.isabs(f) and workdir:
                    f = os.path.join(workdir, f)
                if f not in item['ConfigFiles']:
                    item['ConfigFiles'].append(f)
    return sorted(projects.values(), key=lambda p: p['Name'])


def get_project_config(call, compose_dir, mode):
    if mode == 'v2':
        return js(*(call + ['config', '--format', 'json']), cwd=str(compose_dir))
    # Standalone Compose 1.29 supports JSON, but older versions may not.
    try:
        obj = js(*(call + ['config', '--format', 'json']), cwd=str(compose_dir))
        if isinstance(obj, dict) and isinstance(obj.get('services'), dict):
            return obj
    except (ValueError, RuntimeError):
        pass
    try:
        import yaml
    except ImportError:
        raise RuntimeError('docker-compose v1 does not support JSON config; PyYAML is missing on the remote host')
    obj = yaml.safe_load(cmd(*(call + ['config']), cwd=str(compose_dir)))
    if not isinstance(obj, dict) or not isinstance(obj.get('services'), dict):
        raise RuntimeError('unrecognized docker-compose config format')
    return obj


def from_lines(path):
    try:
        content = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return None
    results = []
    for line in content.splitlines():
        m = re.match(r'^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?', line, re.I)
        if m:
            results.append((m.group(1), (m.group(2) or '').lower()))
    return results


def dockerfile_for(build, compose_dir):
    if isinstance(build, str):
        context, name = build, 'Dockerfile'
    else:
        if build.get('dockerfile_inline'):
            return None
        context = build.get('context', str(compose_dir))
        name = build.get('dockerfile', 'Dockerfile')
    p = Path(name)
    context_path = Path(context)
    if not context_path.is_absolute():
        context_path = compose_dir / context_path
    return p if p.is_absolute() else context_path / p


results = []


def row(status, name, ref, msg, action=''):
    results.append((status, name, ref, msg, action))


def print_report(show_ok, details=False):
    print('STATUS       PROJECT/SERVICE                            IMAGE / BASE                                 EXPLANATION')
    print('-' * 159)
    visible = 0
    for status, name, ref, msg, action in results:
        if status in ('OK', 'PINNED') and not show_ok:
            continue
        visible += 1
        print(f'{status:<12} {name:<42} {ref:<44} {msg}')
        if details and action:
            print(' ' * 12 + '  ACTION: ' + action)
    if not visible:
        print('No action required (use --show-ok to see all checks).')
    from collections import Counter
    counts = Counter(r[0] for r in results)
    if not VERBOSE:
        return
    print('\nSUMMARY: ' + ' | '.join(f'{status}: {counts[status]}' for status in
                                   ('PULL', 'BUILD?', 'RECREATE', 'CHECK', 'OK', 'PINNED') if counts[status]))
    print('NOTE: BUILD? = remote base differs from the build; this is a hint, not proof of the digest used.')
    print('      PULL = remote layers differ from the local tag; RECREATE = container uses a previous image ID.')
    print('      --details shows suggested commands; layer cache lasts 6h (including migrated v1 cache).')
    print('      HEAD/Buildx are logical operations: Buildx may generate multiple HTTP requests.')
    print('REGISTRY: ' + ' | '.join(f'{k}={metrics[k]}' for k in ('cache_hit', 'head', 'head_unchanged', 'buildx')))



def check_pull(name, ref, running_id, plat_override=None, action=''):
    local = inspect_image(ref)
    deployed = inspect_image(running_id)
    if not local:
        row('PULL', name, ref, 'tag not present locally; pull image', action)
        return
    plat = plat_override or image_platform(local)
    remote, reason = remote_config(ref, plat)
    if remote is None:
        row('CHECK', name, ref, reason + ' [' + str(plat) + ']')
        return
    suffix = (' [CACHE <=6h]' if reason == 'CACHE' else
              ' [HEAD: digest invariato]' if reason == 'HEAD_OK' else '')
    if layers(local) != layers(remote):
        row('PULL', name, ref, 'REMOTE layers differ from those of the LOCAL tag' + suffix, action)
    elif deployed and deployed.get('Id') != local.get('Id'):
        row('RECREATE', name, ref, 'registry = local tag; container uses a previous IMAGE ID' + suffix, action.split(' && ', 1)[-1])
    else:
        row('OK', name, ref, 'remote layers = local tag; container is current or not present' + suffix)


def check_build(name, build, image_ref, running_id, compose_dir, action='', project=None, service=None):
    path = dockerfile_for(build, compose_dir)
    if path is None:
        row('CHECK', name, '-', 'inline Dockerfile was not analyzed')
        return
    stages = from_lines(path)
    if not stages:
        row('CHECK', name, path.name, 'Dockerfile/FROM could not be read')
        return
    # A Compose build without `image:` defaults to <project>-<service>.
    # Always inspect the *current tagged image* before consulting the container:
    # after `compose build` the container may still reference an old image ID,
    # which might have been removed by Docker's image store.
    default_ref = f'{project}-{service}' if project and service else None
    candidates = list(dict.fromkeys(r for r in (image_ref, default_ref) if r))
    built = None
    built_ref = None
    for ref in candidates:
        built = inspect_image(ref)
        if built:
            built_ref = ref
            break
    # A build can finish successfully without updating the deployed container.
    # Compare IDs even when the old image is no longer inspectable.
    if built and running_id and built.get('Id') != running_id:
        up_action = action.split(' build --pull ', 1)
        up_action = (up_action[0] + ' up -d --no-deps ' + up_action[1]
                     if len(up_action) == 2 else '')
        row('RECREATE', name, built_ref,
            'newly built image differs from the one used by the container; '
            'run compose up -d (no additional build is needed to apply it)',
            up_action)
        return
    if not built:
        # Some old stacks may have only an image associated with the running
        # container and no tag in the local image store.
        built = inspect_image(running_id)
    if not built:
        row('CHECK', name, path.name,
            'built image not found: checked Compose tag and container image')
        return
    plat = image_platform(built)
    built_layers = layers(built)
    names = set()
    external = []
    for i, (ref, alias) in enumerate(stages):
        if ref.lower() != 'scratch' and ref.lower() not in names:
            external.append((i, ref))
        if alias:
            names.add(alias)
    if not external:
        row('CHECK', name, path.name, 'no comparable external base image')
        return
    for i, ref in external:
        if '$' in ref:
            row('CHECK', name, 'variable FROM', 'ARG in FROM: resolution is not implemented')
            continue
        remote, reason = remote_config(ref, plat)
        if remote is None:
            row('CHECK', name, ref, reason + ' [' + str(plat) + ']')
            continue
        base_layers = layers(remote)
        suffix = (' [CACHE <=6h]' if reason == 'CACHE' else
              ' [HEAD: digest invariato]' if reason == 'HEAD_OK' else '')
        if i != len(stages) - 1:
            row('CHECK', name, ref, 'intermediate-stage FROM: layers may not be present in the final image')
        elif built_layers is None or base_layers is None:
            row('CHECK', name, ref, 'layers are unavailable')
        elif len(built_layers) < len(base_layers) or built_layers[:len(base_layers)] != base_layers:
            local_base = inspect_image(ref)
            local_layers = layers(local_base)
            if local_layers is not None and built_layers[:len(local_layers)] == local_layers:
                explanation = ('build uses layers from the LOCAL base; the REMOTE base is different'
                               if local_layers != base_layers else
                               'local/remote base is identical, but the build does not include its prefix')
            else:
                explanation = 'REMOTE base differs from the build’s initial layers (provenance is uncertain)'
            row('BUILD?', name, ref, explanation + suffix, action)
        else:
            row('OK', name, ref, 'build prefix = REMOTE base layers' + suffix)
    final = stages[-1][0]
    if final.lower() in names:
        row('CHECK', name, final, 'final stage is internal: no direct comparison')


def progress(message):
    if PROGRESS:
        print('PROGRESS: ' + message, file=sys.stderr, flush=True)


def local_main(include_all, show_ok, details=False):
    if DEBUG:
        log_debug('local/remote execution: Python=' + sys.executable + '; uid=' + str(os.geteuid()) + '; cwd=' + os.getcwd())
        resolve_docker()
    load_cache()
    mode = detect_compose()
    projects = (js('docker', 'compose', 'ls', *(['--all'] if include_all else []), '--format', 'json')
                if mode == 'v2' else discover_projects_legacy(include_all))
    containers = all_containers()
    progress(f'{len(projects)} Compose projects detected')
    for project in projects:
        name = project.get('Name')
        files = project.get('ConfigFiles') or []
        if isinstance(files, str):
            files = [p.strip() for p in files.split(',') if p.strip()]
        if not name:
            continue
        if not files:
            for service, image_ref in sorted(project.get('LegacyServices', {}).items()):
                row('CHECK', name + '/' + service, image_ref or '-',
                'legacy Compose: config_files label is missing; cannot locate the Dockerfile')
            if not project.get('LegacyServices'):
                row('CHECK', name + '/*', '-', 'legacy Compose: Compose file path is unknown')
            continue
        compose_dir = Path(project.get('WorkingDir') or Path(files[0]).parent)
        call = (['docker', 'compose'] if mode == 'v2' else [COMPOSE_V1_BIN]) + ['-p', name]
        for filename in files:
            call += ['-f', filename]
        try:
            config = get_project_config(call, compose_dir, mode)
        except (RuntimeError, ValueError):
            row('CHECK', name + '/*', '-', 'unable to parse Compose; use --debug to see the reason')
            continue
        for service, cfg in sorted((config.get('services') or {}).items()):
            compose_label = str(files[0])
            compose_cmd = ('docker compose' if mode == 'v2' else 'docker-compose') + ' -p ' + shlex.quote(name) + ' -f ' + shlex.quote(compose_label)
            for extra in files[1:]:
                compose_cmd += ' -f ' + shlex.quote(str(extra))
            service_arg = shlex.quote(service)
            key = name + '/' + service
            progress(f'checking {key} ...')
            running = containers.get((name, service))
            if cfg.get('build'):
                check_build(key, cfg['build'], cfg.get('image'), running, compose_dir, compose_cmd + ' build --pull ' + service_arg, name, service)
            elif cfg.get('image'):
                if '@sha256:' in cfg['image']:
                    row('PINNED', key, cfg['image'].split('@')[0], 'digest is pinned in Compose; tag movement is not tracked')
                else:
                    check_pull(key, cfg['image'], running, cfg.get('platform'), compose_cmd + ' pull ' + service_arg + ' && ' + compose_cmd + ' up -d ' + service_arg)
            else:
                row('CHECK', key, '-', 'no image or build configuration')
    progress('checks complete; generating report')
    save_cache()
    print_report(show_ok, details)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--ssh', metavar='USER@HOST', action='append', help='SSH host to check (repeatable); runs locally if omitted')
    ap.add_argument('--all', action='store_true', help='Include stopped Compose projects (compatibility mode)')
    ap.add_argument('--show-ok', action='store_true', help='Also show OK and PINNED rows')
    ap.add_argument('--details', action='store_true', help='Show suggested commands (not executed)')
    ap.add_argument('--no-cache', action='store_true', help='Disable registry cache (check live metadata)')
    ap.add_argument('--hub-interval', type=float, default=8.0, metavar='SEC', help='Minimum interval between Docker Hub operations (default: 8 s)')
    ap.add_argument('--other-interval', type=float, default=1.0, metavar='SEC', help='Minimum interval for other registries (default: 1 s)')
    ap.add_argument('--verbose', '-v', action='store_true', help='Also show summary, notes, and registry counters')
    ap.add_argument('--debug', action='store_true', help='Show SSH diagnostics, Docker commands, sanitized stderr, and registry requests')
    ap.add_argument('--progress', action='store_true', help='Show live progress, including over SSH')
    ap.add_argument('--no-progress', action='store_true', help='Disable live progress')
    args = ap.parse_args()
    if not (0 <= args.hub_interval <= 300 and 0 <= args.other_interval <= 300):
        ap.error('intervals must be between 0 and 300 seconds')
    global USE_DISK_CACHE, HUB_INTERVAL, OTHER_INTERVAL, DEBUG, PROGRESS, VERBOSE
    HUB_INTERVAL, OTHER_INTERVAL, DEBUG = args.hub_interval, args.other_interval, args.debug
    USE_DISK_CACHE = not args.no_cache
    VERBOSE = args.verbose or args.debug
    PROGRESS = (args.progress or args.debug) and not args.no_progress
    if args.ssh:
        for host in args.ssh:
            if host.startswith('-') or any(c.isspace() for c in host):
                ap.error('invalid SSH host: ' + host)
        source = Path(__file__).read_bytes()
        # In multi-host mode the host header disambiguates the independent reports.
        many = len(args.ssh) > 1
        failed = []
        for index, host in enumerate(args.ssh):
            if many:
                if index:
                    print('', flush=True)
                print('=== ' + host + ' ===', flush=True)
            argv = ['ssh'] + (['-v'] if args.debug else []) + ['-T', '--', host, 'python3', '-u', '-']
            if args.all:
                argv.append('--all')
            if args.show_ok:
                argv.append('--show-ok')
            if args.details:
                argv.append('--details')
            if args.no_cache:
                argv.append('--no-cache')
            argv.extend(['--hub-interval', str(args.hub_interval), '--other-interval', str(args.other_interval)])
            if VERBOSE:
                argv.append('--verbose')
            if args.debug:
                argv.append('--debug')
            if PROGRESS:
                argv.append('--progress')
            if args.no_progress:
                argv.append('--no-progress')
            if args.debug:
                print('DEBUG local: SSH command: ' + shlex.join(argv) + ' (script sent via stdin)', file=sys.stderr, flush=True)
            try:
                # stdout/stderr go directly to terminal for live display.
                proc = subprocess.Popen(argv, stdin=subprocess.PIPE)
                proc.communicate(input=source, timeout=1800)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                rc = 2
                print('ERROR: SSH execution timed out for ' + host, file=sys.stderr, flush=True)
            except OSError as exc:
                rc = 2
                print('ERROR: SSH for ' + host + ': ' + str(exc), file=sys.stderr, flush=True)
            if args.debug:
                print('DEBUG local: SSH exited with status=' + str(rc), file=sys.stderr, flush=True)
            if rc:
                failed.append(host)
                print('ERROR: check failed on ' + host + ' (exit ' + str(rc) + ')', file=sys.stderr, flush=True)
        if failed:
            print('ERROR: hosts that did not complete: ' + ', '.join(failed), file=sys.stderr, flush=True)
            sys.exit(2)
        return
    local_main(args.all, args.show_ok, args.details)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(2)
