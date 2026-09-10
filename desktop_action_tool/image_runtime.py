"""Image CLI and execution. A separate owned worker bounds native processing."""
import argparse
import base64
import json
from pathlib import Path
import sys
import time
import uuid

from .action_runtime import ActionAborted, ActionError
from .image_contract import IMAGE_TOOLS, SAVE_TOOLS, validate
from .image_geometry import error, resize_plan
from .image_store import ImageStore, settings, digest
from .project_paths import PROJECT_ROOT

FLAGS = {'--' + name.replace('_', '-'): name for name in IMAGE_TOOLS}


def failure(exc, request_id=None):
    if isinstance(exc, (ActionAborted, KeyboardInterrupt)):
        code, message = 'IMAGE_OPERATION_INTERRUPTED', 'image operation was cancelled'
    else:
        code, message = getattr(exc, 'code', 'IMAGE_OPERATION_FAILED'), str(exc)
    return {'ok': False, 'error_code': code, 'error': message, 'saved': None,
        'request_id': request_id, 'required_next_step': getattr(exc, 'next_step',
        'inspect read_image(request_id) before retrying a save; do not repeat desktop input')}


def deliver(store, result, args, enabled, limit=None):
    if args.get('dry_run') or not result.get('image_id'):
        return result
    meta, raw = store.get(result['image_id'], pixels=enabled)
    if args.get('include_metadata'):
        result['metadata'] = meta
    view = None
    if 'view_width' in args or 'view_height' in args:
        from .image_processing import load, render_resize, resize_details
        sizing = {'width': args['view_width']} if 'view_width' in args else {'height': args['view_height']}
        plan = resize_plan(meta['width'], meta['height'], sizing)
        store.budget([(meta['width'], meta['height']), (plan['width'], plan['height'])])
        _, original = load(store, meta['image_id'])
        try:
            with render_resize(original, plan, store.options['image_resampling']) as image:
                view = store.save(image, 'view', temporary=True,
                    details=resize_details(meta, plan, store.options['image_resampling']))
        finally:
            original.close()
        _, raw = store.get(view['image_id'], pixels=enabled)
        result['view'] = {**store.describe(view), 'transform': view['transform']}
    result['delivery'] = {'requested': enabled and args.get('return_image', True), 'delivered': False}
    if enabled and args.get('return_image', True):
        from .controller_runtime import MAX_IMAGE_BYTES
        limit = MAX_IMAGE_BYTES if limit is None else limit
        if len(raw) > limit:
            result['delivery'].update(error_code='IMAGE_DELIVERY_TOO_LARGE', bytes=len(raw), limit_bytes=limit,
                required_next_step='image is saved; use read_image with an explicit view_width or view_height')
        else:
            result['_controller_images'] = [{'mimeType': 'image/png', 'data': base64.b64encode(raw).decode('ascii')}]
            result['delivery'].update(delivered=True, bytes=len(raw), image_id=(view or meta)['image_id'])
    return result


def execute(name, args, root=PROJECT_ROOT, *, config=None, controller=None, deliver_images=False):
    from . import image_processing as processing
    if name != 'rebuild':
        validate(name, args)
    options = settings(root, config)
    args = dict(args)
    if name in ('resize_image', 'compose_images'):
        args.setdefault('resampling', options['image_resampling'])
    if name == 'compose_images' and len(args['image_ids']) > options['image_max_inputs']:
        error('IMAGE_LIMIT_EXCEEDED', 'too many compose inputs for image_max_inputs')
    deadline = time.monotonic() + args.get('operation_timeout_s', options['image_processing_timeout_s'])
    def check():
        if controller:
            controller.check()
        if time.monotonic() >= deadline:
            error('IMAGE_TIMEOUT', 'image processing deadline expired')
    store = ImageStore(root, options, check)
    if name == 'rebuild':
        return store.rebuild()
    if name in SAVE_TOOLS and not args.get('dry_run'):
        args.setdefault('request_id', uuid.uuid4().hex)
    request_id = args.get('request_id')
    fingerprint = digest(json.dumps({'operation': name, 'arguments': {k: v for k, v in args.items()
        if k not in ('request_id', 'return_image', 'operation_timeout_s', 'dry_run')}},
        sort_keys=True, allow_nan=False).encode('utf-8'))
    result = None
    try:
        with store.locked(readonly=bool(args.get('dry_run'))):
            check()
            previous = store.repeat(request_id, fingerprint) if name in SAVE_TOOLS and not args.get('dry_run') else None
            if not previous and 'output_path' in args:
                path = store.output_path(args['output_path'], 'img_' + '0' * 32, False)
                if path.exists() or path.with_suffix('.json').exists():
                    error('IMAGE_ALREADY_EXISTS', 'PNG or JSON already exists; choose another output name')
            if previous:
                result = {**store.describe(previous), 'replayed': True}
            elif name == 'capture_image':
                if args.get('dry_run'):
                    result = {'dry_run': True, 'target': args['target'], 'preconditions': ['live window geometry has not been inspected']}
                else:
                    from .image_capture import capture
                    result = capture(store, args, controller)
            elif name == 'preview_region':
                show = args.get('show_overlay', bool(options['image_overlay_enabled']))
                if show and not args.get('dry_run'):
                    from .activity_indicator import bound_session
                    state = bound_session(root)
                    if (state and state.get('profile') == 'background') or (controller and controller.payload.get('read_only')):
                        error('PROFILE_VIOLATION', 'screen frames are forbidden in background and read-only operation')
                result = processing.preview(store, args)
                if show and not args.get('dry_run'):
                    from .region_overlay import show as show_frame
                    try:
                        meta, _ = store.get(args['image_id'])
                        result['overlay'] = show_frame(store, meta, result['selection_rect'], args.get('overlay_ms', options['image_overlay_duration_ms']))
                    except (ActionError, OSError) as exc:
                        result['overlay'] = {'shown': False, **failure(exc)}
                else:
                    result['overlay'] = {'shown': False}
            elif name == 'save_region':
                result = processing.crop(store, args, fingerprint)
            elif name == 'resize_image':
                result = processing.resize(store, args, fingerprint)
            elif name == 'compose_images':
                result = processing.compose(store, args, fingerprint)
            else:
                identifier = args.get('image_id')
                if identifier is None:
                    record = store.index['requests'].get(request_id)
                    if not record:
                        pending = next((r for r in store.index['pending'].values() if r.get('request_id') == request_id), None)
                        if pending:
                            store.record_path(pending)
                            return {'ok': False, 'operation': name, 'error_code': 'IMAGE_INCOMPLETE',
                                'error': 'request has an incomplete PNG/JSON pair', 'saved': False, 'request_id': request_id,
                                'image_id': pending['metadata']['image_id'], 'image_path': pending['image_path'],
                                'metadata_path': pending['metadata_path'],
                                'required_next_step': 'inspect these pending files before retrying; never overwrite unknown output'}
                        error('IMAGE_NOT_FOUND', 'request has no completed registered image; inspect pending output before retrying')
                    identifier = record['image_id']
                meta, _ = store.get(identifier)
                result = {**store.describe(meta), **({'dry_run': True} if args.get('dry_run') else {})}
            result.update(ok=True, operation=name)
            if request_id:
                result['request_id'] = request_id
            try:
                return deliver(store, result, args, deliver_images, controller.payload.get('max_image_bytes') if controller else None)
            except (ActionError, ActionAborted, OSError) as exc:
                if result.get('saved'):
                    result['delivery'] = {'delivered': False, **failure(exc, request_id)}
                    return result
                raise
    except (ActionError, ActionAborted, OSError, ValueError, KeyboardInterrupt) as exc:
        response = failure(exc, request_id)
        if result and result.get('saved'):
            response.update({k: v for k, v in result.items() if k not in ('ok', '_controller_images')})
        return response


def add_help(parser):
    group = parser.add_argument_group('Image regions and comparison (optional images extra)')
    group.add_argument('--images-help', action='store_true', help='show image commands and coordinate workflow')
    group.add_argument('--capture-image', action='store_true', help='capture a clean PNG+JSON; see --images-help')
    for flag in FLAGS:
        if flag != '--capture-image':
            group.add_argument(flag, nargs='?', help='see --images-help for parameters')


def parse(argv):
    parser = argparse.ArgumentParser(description='Registered PNG regions, resize and composition; --images-help explains the workflow')
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--capture-image', action='store_true')
    for flag in ('preview-region', 'save-region', 'resize-image', 'read-image'):
        modes.add_argument('--' + flag)
    modes.add_argument('--compose-images', nargs='+')
    modes.add_argument('--image-operation', choices=sorted(IMAGE_TOOLS))
    modes.add_argument('--image-store-rebuild', action='store_true')
    parser.add_argument('--image-request', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--config')
    parser.add_argument('--quiet', action='store_true')
    parser.add_argument('--dry-run', action='store_true', default=None)
    for flag in ('return-image', 'show-overlay', 'labels', 'include-metadata'):
        parser.add_argument('--' + flag, action=argparse.BooleanOptionalAction, default=None)
    for flag in ('window-id', 'capture-delay-ms', 'overlay-ms', 'width', 'height', 'view-width', 'view-height', 'columns', 'padding', 'gap'):
        parser.add_argument('--' + flag, type=int)
    for flag in ('scale', 'operation-timeout-s'):
        parser.add_argument('--' + flag, type=float)
    for flag in ('window-title', 'process-name', 'session-id', 'area', 'output-path', 'request-id', 'reference-image-id', 'fit', 'resampling', 'background', 'layout', 'normalize'):
        parser.add_argument('--' + flag)
    parser.add_argument('--rectangle', nargs=4, type=int, metavar=('X', 'Y', 'WIDTH', 'HEIGHT'))
    parser.add_argument('--canvas', nargs=2, type=int, metavar=('WIDTH', 'HEIGHT'))
    namespace = vars(parser.parse_args(argv))
    config = namespace.pop('config')
    namespace.pop('quiet')
    internal, request = namespace.pop('image_operation'), namespace.pop('image_request')
    rebuild = namespace.pop('image_store_rebuild')
    name, args = internal, {}
    for operation in IMAGE_TOOLS:
        value = namespace.pop(operation)
        if value:
            name = operation
            if operation != 'capture_image':
                args['image_ids' if operation == 'compose_images' else 'region_id' if operation == 'save_region' else 'image_id'] = value
    namespace = {k: v for k, v in namespace.items() if v is not None}
    if rebuild:
        if namespace or request:
            parser.error('--image-store-rebuild accepts only --config and --quiet')
        return 'rebuild', {}, config
    if internal:
        if not request or namespace:
            parser.error('internal image operation requires only --image-request JSON on stdin')
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            parser.error('image request exceeds 64 KiB')
        return name, json.loads(raw), config
    if request:
        parser.error('--image-request requires --image-operation')
    target = {k: namespace.pop(k) for k in ('window_id', 'window_title', 'process_name', 'session_id') if k in namespace}
    if target:
        args['target'] = target
    if 'rectangle' in namespace:
        namespace['rectangle'] = dict(zip(('x', 'y', 'width', 'height'), namespace['rectangle']))
    if 'canvas' in namespace:
        namespace['canvas'] = dict(zip(('width', 'height'), namespace['canvas']))
    args.update(namespace)
    return name, args, config


def run_cli(argv, *, root=PROJECT_ROOT, controller=None):
    present = {a.split('=', 1)[0] for a in argv}
    if '--images-help' in present:
        from .image_contract import HELP
        print(HELP)
        parse(['--help'])
    if not present.intersection({*FLAGS, '--image-operation', '--image-request', '--image-store-rebuild'}):
        return None
    try:
        name, args, config = parse(argv)
        from .image_worker import supervise
        if name != 'rebuild':
            validate(name, args)
        result = supervise(name, args, root, config, controller)
    except (ActionError, ActionAborted, OSError, ValueError, KeyboardInterrupt) as exc:
        result = failure(exc)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0 if result['ok'] else 1
