"""Registered PNG/JSON pairs; one mutex protects processing, cleanup and recovery."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import time
import uuid

from .action_runtime import ActionLock
from .image_geometry import error, number
from .release_info import current_version

DEFAULTS = {
    'image_output_directory': 'screenshots/saved', 'image_output_roots': [],
    'image_region_ttl_s': 120, 'image_temporary_ttl_s': 1800, 'image_temporary_max_mib': 256,
    'image_max_pixels': 32000000, 'image_max_inputs': 16, 'image_working_max_mib': 512,
    'image_processing_timeout_s': 60, 'image_resampling': 'smooth',
    'image_overlay_enabled': 0, 'image_overlay_duration_ms': 3000,
    'image_overlay_freshness_s': 120,
}
ID = re.compile(r'^(?:img|reg)_[0-9a-f]{32}$')
MAX_JSON = 16 * 1024 * 1024
MAX_FILE = 64 * 1024 * 1024


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode('utf-8')
    if len(data) > MAX_JSON:
        error('IMAGE_LIMIT_EXCEEDED', 'image index or metadata exceeds 16 MiB')
    return data


def read_bytes(path, limit=MAX_FILE):
    with Path(path).open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        error('IMAGE_LIMIT_EXCEEDED', 'image or metadata exceeds its file-size limit')
    return data


def no_links(path, root):
    current = Path(path)
    while current != root:
        if current.is_symlink() or current.is_junction():
            error('IMAGE_PATH_NOT_ALLOWED', 'image paths cannot traverse a symlink or junction')
        if current.parent == current:
            break
        current = current.parent


def settings(root, config=None):
    path = Path(config) if config else Path(root) / 'settings.json'
    values = json.loads(read_bytes(path, MAX_JSON)) if path.exists() else {}
    if not isinstance(values, dict):
        error('IMAGE_ARGUMENT_INVALID', 'settings must be an object')
    result = {**DEFAULTS, **{k: v for k, v in values.items() if k.startswith('image_')}}
    bounds = {'image_region_ttl_s': (1, 3600), 'image_temporary_ttl_s': (1, 604800),
              'image_temporary_max_mib': (1, 16384), 'image_max_pixels': (1, 128000000),
              'image_max_inputs': (1, 16), 'image_working_max_mib': (16, 4096),
              'image_processing_timeout_s': (1, 600), 'image_overlay_enabled': (0, 1),
              'image_overlay_duration_ms': (0, 30000), 'image_overlay_freshness_s': (1, 120)}
    for key, (low, high) in bounds.items():
        number(result[key], key, low, high, integer=True)
    if result['image_resampling'] not in ('smooth', 'nearest'):
        error('IMAGE_ARGUMENT_INVALID', 'image_resampling must be smooth or nearest')
    if not isinstance(result['image_output_directory'], str) or not result['image_output_directory']:
        error('IMAGE_ARGUMENT_INVALID', 'image_output_directory must be a directory path')
    if not isinstance(result['image_output_roots'], list) or any(not isinstance(p, str) or not p for p in result['image_output_roots']):
        error('IMAGE_ARGUMENT_INVALID', 'image_output_roots must be a list of directory paths')
    result['capture_delay_ms'] = number(values.get('screenshot_delay_ms', 500), 'screenshot_delay_ms', 0, 60000, integer=True)
    return result


class ImageStore:
    def __init__(self, root, options=None, check=lambda: None, clock=time.time):
        self.root = Path(root).resolve()
        self.options = options or settings(self.root)
        self.check, self.clock = check, clock
        self.folder = self.root / 'screenshots/.image_store'
        self.temporary = self.root / 'screenshots/temporary'
        self.index_path = self.folder / 'index.json'
        self.output = self.absolute(self.options['image_output_directory'])
        self.roots = [self.output, *(self.absolute(p) for p in self.options['image_output_roots'])]
        self.index = None

    def absolute(self, path):
        candidate = Path(path)
        return Path(os.path.abspath(candidate if candidate.is_absolute() else self.root / candidate))

    @contextmanager
    def locked(self, *, readonly=False):
        no_links(self.folder, self.root)
        no_links(self.temporary, self.root)
        with ActionLock(self.index_path):
            no_links(self.index_path, self.root)
            self.index = {'schema_version': 1, 'images': {}, 'regions': {}, 'requests': {}, 'pending': {}}
            if self.index_path.exists():
                try:
                    self.index = json.loads(read_bytes(self.index_path, MAX_JSON))
                    if self.index.get('schema_version') != 1 or any(not isinstance(self.index[k], dict) for k in ('images', 'regions', 'requests', 'pending')):
                        raise ValueError('invalid index')
                except (OSError, ValueError, KeyError):
                    error('IMAGE_INCOMPLETE', 'image index is invalid; use explicit --image-store-rebuild after inspection')
            if not readonly:
                self.recover()
                self.cleanup()
            yield self

    def flush(self):
        no_links(self.folder, self.root)
        self.folder.mkdir(parents=True, exist_ok=True)
        staging = self.folder / 'index.tmp'
        no_links(staging, self.root)
        with staging.open('wb') as stream:
            stream.write(encoded(self.index))
            stream.flush()
            os.fsync(stream.fileno())
        staging.replace(self.index_path)

    def safe_path(self, value, *, temporary=False):
        path = self.absolute(value)
        no_links(path, self.root)
        resolved = path.resolve()
        roots = [self.temporary.resolve()] if temporary else [p.resolve() for p in self.roots]
        if not any(resolved.is_relative_to(p) and resolved != p for p in roots):
            error('IMAGE_PATH_NOT_ALLOWED', 'output must be inside image_output_directory or image_output_roots')
        if path.suffix.lower() != '.png' or os.path.isreserved(str(path)) or not path.stem:
            error('IMAGE_PATH_NOT_ALLOWED', 'output must be a non-reserved .png filename')
        return path

    def output_path(self, output, identifier, temporary):
        if output is None:
            value = (self.temporary if temporary else self.output) / (identifier + '.png')
        else:
            value = Path(output)
            if not value.is_absolute():
                value = self.output / value
            if not value.suffix:
                value = value.with_suffix('.png')
        return self.safe_path(value, temporary=temporary)

    def record_path(self, record):
        path = self.safe_path(record['image_path'], temporary=record['metadata']['storage'] == 'temporary')
        if Path(record['metadata_path']) != path.with_suffix('.json'):
            error('IMAGE_CHANGED', 'registered sidecar path changed')
        no_links(path.with_suffix('.json'), self.root)
        return path

    def get(self, identifier, *, pixels=False):
        self.check()
        if not isinstance(identifier, str) or not ID.fullmatch(identifier) or not identifier.startswith('img_'):
            error('IMAGE_NOT_FOUND', 'invalid image_id')
        record = self.index['images'].get(identifier)
        if record is None:
            error('IMAGE_NOT_FOUND', 'image is absent or temporary source expired')
        path = self.record_path(record)
        try:
            raw_meta = read_bytes(path.with_suffix('.json'), MAX_JSON)
            if digest(raw_meta) != record['metadata_sha256']:
                error('IMAGE_CHANGED', 'image sidecar changed after registration')
            meta = json.loads(raw_meta)
            raw = read_bytes(path)
        except (FileNotFoundError, json.JSONDecodeError):
            error('IMAGE_NOT_FOUND', 'registered PNG or sidecar is missing')
        if meta != record['metadata'] or digest(raw) != meta['sha256']:
            error('IMAGE_CHANGED', 'registered image changed after capture')
        if raw[:8] != b'\x89PNG\r\n\x1a\n' or len(raw) < 24:
            error('IMAGE_FORMAT_UNSUPPORTED', 'only registered PNG images are supported')
        size = [int.from_bytes(raw[16:20], 'big'), int.from_bytes(raw[20:24], 'big')]
        if size != [meta['width'], meta['height']]:
            error('IMAGE_CHANGED', 'PNG dimensions differ from metadata')
        self.budget([size])
        self.check()
        return deepcopy(meta), raw if pixels else None

    def budget(self, sizes):
        for width, height in sizes:
            if min(width, height) <= 0 or max(width, height) > 32768 or width * height > self.options['image_max_pixels']:
                error('IMAGE_LIMIT_EXCEEDED', 'image dimensions exceed the configured pixel limit')
        if sum(w * h for w, h in sizes) * 12 > self.options['image_working_max_mib'] * 1024 * 1024:
            error('IMAGE_LIMIT_EXCEEDED', 'estimated decoded working buffers exceed image_working_max_mib')

    def repeat(self, request_id, fingerprint):
        if request_id is None:
            return None
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request_id):
            error('IMAGE_ARGUMENT_INVALID', 'request_id must contain 1-128 letters, digits, underscores or hyphens')
        record = self.index['requests'].get(request_id)
        if record:
            if record['fingerprint'] != fingerprint:
                error('IMAGE_REQUEST_CONFLICT', 'request_id was already used with different parameters')
            return self.get(record['image_id'])[0]
        for pending in self.index['pending'].values():
            if pending.get('request_id') == request_id:
                error('IMAGE_INCOMPLETE', 'this request has an incomplete output; inspect its pending files before retrying')
        return None

    def save(self, image, kind, *, output=None, temporary=False, details=None, request_id=None, fingerprint=None):
        self.check()
        self.budget([image.size])
        identifier = 'img_' + uuid.uuid4().hex
        path = self.output_path(output, identifier, temporary)
        sidecar = path.with_suffix('.json')
        no_links(sidecar, self.root)
        if path.exists() or sidecar.exists():
            error('IMAGE_ALREADY_EXISTS', 'PNG or JSON already exists; choose another output name')
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        raw = buffer.getvalue()
        if len(raw) > MAX_FILE:
            error('IMAGE_LIMIT_EXCEEDED', 'encoded PNG exceeds 64 MiB')
        metadata = {'schema_version': 1, 'image_id': identifier, 'kind': kind, 'image_file': path.name,
                    'sha256': digest(raw), 'width': image.width, 'height': image.height, 'pixel_mode': image.mode,
                    'created_at': utc(), 'created_epoch': self.clock(), 'tool_version': current_version(),
                    'storage': 'temporary' if temporary else 'saved', 'mappings': [], 'parents': [],
                    **deepcopy(details or {})}
        if kind == 'source':
            metadata['mappings'] = [{'capture_id': identifier, 'capture_sha256': metadata['sha256'],
                'capture': deepcopy(metadata['capture']), 'scale': [1, 1], 'offset': [0, 0],
                'content_rect': [0, 0, image.width, image.height]}]
        if request_id is not None:
            metadata['request'] = {'request_id': request_id, 'fingerprint': fingerprint}
        raw_meta = encoded(metadata)
        if temporary:
            occupied = sum(r.get('bytes', 0) for r in self.index['images'].values() if r['metadata']['storage'] == 'temporary')
            if occupied + len(raw) + len(raw_meta) > self.options['image_temporary_max_mib'] * 1024 * 1024:
                error('IMAGE_LIMIT_EXCEEDED', 'temporary image budget is full; active and saved images were preserved')
        record = {'image_path': str(path), 'metadata_path': str(sidecar), 'metadata_sha256': digest(raw_meta),
                  'metadata': metadata, 'bytes': len(raw) + len(raw_meta), 'request_id': request_id,
                  'fingerprint': fingerprint}
        self.index['pending'][identifier] = record
        self.flush()
        path.parent.mkdir(parents=True, exist_ok=True)
        stages = [path.parent / ('.' + identifier + '.png.tmp'), path.parent / ('.' + identifier + '.json.tmp')]
        try:
            for stage, data in zip(stages, (raw, raw_meta)):
                with stage.open('xb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            self.check()
            # Windows rename refuses an existing destination, including a racing writer.
            stages[0].rename(path)
            stages[1].rename(sidecar)
            self.finish(identifier, record)
        except BaseException:
            # A complete pair can be recovered after a lost response. Never overwrite
            # or delete a final file whose ownership/outcome is not known.
            raise
        return deepcopy(metadata)

    def finish(self, identifier, record):
        self.index['images'][identifier] = record
        self.index['pending'].pop(identifier, None)
        if record.get('request_id'):
            self.index['requests'][record['request_id']] = {'image_id': identifier, 'fingerprint': record['fingerprint']}
        self.flush()

    def recover(self):
        for identifier, record in list(self.index['pending'].items()):
            self.check()
            if not ID.fullmatch(identifier) or not identifier.startswith('img_'):
                error('IMAGE_INCOMPLETE', 'invalid pending image identifier')
            path = self.record_path(record)
            destinations = [path, path.with_suffix('.json')]
            expected = [record['metadata']['sha256'], record['metadata_sha256']]
            stages = [path.parent / ('.' + identifier + suffix) for suffix in ('.png.tmp', '.json.tmp')]
            for stage in stages:
                no_links(stage, self.root)
            if all((d.exists() and digest(read_bytes(d)) == h) or
                   (not d.exists() and s.exists() and digest(read_bytes(s)) == h)
                   for d, s, h in zip(destinations, stages, expected)):
                for destination, stage in zip(destinations, stages):
                    if not destination.exists():
                        stage.rename(destination)
                self.finish(identifier, record)
            elif not any(d.exists() for d in destinations):
                # No final output was committed. These UUID staging files are ours;
                # a later retry can safely start afresh, including after cancellation.
                for stage in stages:
                    stage.unlink(missing_ok=True)
                del self.index['pending'][identifier]
                self.flush()

    def rebuild(self):
        """Explicit maintenance: recover complete registered pairs, never import PNGs."""
        with ActionLock(self.index_path):
            rebuilt = {'schema_version': 1, 'images': {}, 'regions': {}, 'requests': {}, 'pending': {}}
            skipped = 0
            for root in dict.fromkeys([self.temporary, *self.roots]):
                no_links(root, self.root)
                if not root.exists():
                    continue
                # os.walk does not follow junctions/symlinks; prune them explicitly.
                for folder, directories, files in os.walk(root, followlinks=False):
                    directories[:] = [d for d in directories if not (Path(folder) / d).is_symlink() and not (Path(folder) / d).is_junction()]
                    for filename in files:
                        self.check()
                        if not filename.endswith('.json'):
                            continue
                        sidecar = Path(folder) / filename
                        try:
                            no_links(sidecar, self.root)
                            raw_meta = read_bytes(sidecar, MAX_JSON)
                            meta = json.loads(raw_meta)
                            identifier = meta['image_id']
                            if meta['schema_version'] != 1 or not ID.fullmatch(identifier) or not identifier.startswith('img_'):
                                raise ValueError('invalid identity')
                            path = self.safe_path(sidecar.with_suffix('.png'), temporary=meta['storage'] == 'temporary')
                            raw = read_bytes(path)
                            if meta['image_file'] != path.name or digest(raw) != meta['sha256'] or raw[:8] != b'\x89PNG\r\n\x1a\n':
                                raise ValueError('pair mismatch')
                            if (meta['storage'] not in ('saved', 'temporary') or meta['kind'] not in ('source', 'region', 'resized', 'composite', 'preview', 'view')
                                    or not isinstance(meta['mappings'], list) or len(meta['mappings']) > 256
                                    or not isinstance(meta['parents'], list) or type(meta['created_epoch']) not in (int, float)):
                                raise ValueError('invalid metadata')
                            if [int.from_bytes(raw[16:20], 'big'), int.from_bytes(raw[20:24], 'big')] != [meta['width'], meta['height']]:
                                raise ValueError('dimension mismatch')
                            self.budget([(meta['width'], meta['height'])])
                            if identifier in rebuilt['images']:
                                raise ValueError('duplicate identity')
                            request = meta.get('request', {})
                            rid = request.get('request_id')
                            if rid and (not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', rid) or rid in rebuilt['requests']):
                                raise ValueError('duplicate request')
                            rebuilt['images'][identifier] = {'image_path': str(path), 'metadata_path': str(sidecar),
                                'metadata_sha256': digest(raw_meta), 'metadata': meta, 'bytes': len(raw) + len(raw_meta),
                                'request_id': rid, 'fingerprint': request.get('fingerprint')}
                            if rid:
                                rebuilt['requests'][rid] = {'image_id': identifier, 'fingerprint': request['fingerprint']}
                        except (OSError, ValueError, KeyError, TypeError):
                            skipped += 1
                            if skipped > 10000:
                                error('IMAGE_LIMIT_EXCEEDED', 'too many unrelated sidecars under configured output roots')
            self.index = rebuilt
            self.flush()
            return {'ok': True, 'recovered_images': len(rebuilt['images']), 'skipped_sidecars': skipped,
                    'regions_expired': True}

    def cleanup(self):
        now, changed = self.clock(), False
        for identifier, region in list(self.index['regions'].items()):
            if region['expires_epoch'] <= now:
                del self.index['regions'][identifier]
                changed = True
        leased = {region['image_id'] for region in self.index['regions'].values()}
        for identifier, record in list(self.index['images'].items()):
            meta = record['metadata']
            if meta['storage'] != 'temporary' or identifier in leased or now - meta['created_epoch'] < self.options['image_temporary_ttl_s']:
                continue
            path = self.record_path(record)
            for p, sha in ((path, meta['sha256']), (path.with_suffix('.json'), record['metadata_sha256'])):
                if p.exists() and digest(read_bytes(p)) == sha:
                    p.unlink()  # Only this registered temporary image, never saved files.
            del self.index['images'][identifier]
            changed = True
        if changed:
            self.flush()

    def region(self, image_id, rect):
        meta, _ = self.get(image_id)
        identifier = 'reg_' + uuid.uuid4().hex
        self.index['regions'][identifier] = {'region_id': identifier, 'image_id': image_id,
            'sha256': meta['sha256'], 'rect': rect, 'expires_epoch': self.clock() + self.options['image_region_ttl_s']}
        self.flush()
        return deepcopy(self.index['regions'][identifier])

    def get_region(self, identifier):
        if not isinstance(identifier, str) or not ID.fullmatch(identifier) or not identifier.startswith('reg_'):
            error('REGION_INVALID', 'invalid region_id')
        record = self.index['regions'].get(identifier)
        if record is None or record['expires_epoch'] <= self.clock():
            error('REGION_EXPIRED', 'region expired; preview the saved source again')
        meta, _ = self.get(record['image_id'])
        if meta['sha256'] != record['sha256']:
            error('IMAGE_CHANGED', 'region source changed')
        return deepcopy(record), meta

    def describe(self, meta):
        record = self.index['images'][meta['image_id']]
        return {'image_id': meta['image_id'], 'image_path': record['image_path'], 'metadata_path': record['metadata_path'],
                'width': meta['width'], 'height': meta['height'], 'sha256': meta['sha256'],
                'kind': meta['kind'], 'saved': True, 'storage': meta['storage']}
