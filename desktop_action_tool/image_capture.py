"""Explicit window capture and historical geometry; never focuses or sends input."""
import time

from .action_runtime import ActionLock
from .image_geometry import error
from .image_store import utc


def wait(check, seconds):
    deadline = time.monotonic() + seconds
    while True:
        check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.02, remaining))


def covered(rect, monitors):
    """Union coverage, including negative coordinates and monitor gaps."""
    x, y, width, height = rect
    end_x, end_y = x + width, y + height
    boxes = [m['rect'] for m in monitors]
    edges = sorted({x, end_x, *(max(x, min(end_x, b[k])) for b in boxes for k in ('left', 'right'))})
    for left, right in zip(edges, edges[1:]):
        intervals = sorted((max(y, b['top']), min(end_y, b['bottom'])) for b in boxes
                           if b['left'] <= left and b['right'] >= right and b['bottom'] > y and b['top'] < end_y)
        bottom = y
        for start, end in intervals:
            if start > bottom:
                return False
            bottom = max(bottom, end)
        if bottom < end_y:
            return False
    return True


def target_window(root, target, controller=None):
    from . import activity_indicator as indicator, window_backend as windows
    from .selection import filter_windows, require_unique
    if 'session_id' in target:
        state = indicator.bound_session(root)
        if not state or state['session_id'] != target['session_id'] or not state.get('target'):
            error('SESSION_CHANGED', 'capture session is absent, changed or has no bound target')
        if controller:
            controller.verify_session(target['session_id'])
        identity = windows.window_identity(state['target']['window_id'])
        if identity != state['target']:
            error('WINDOW_CHANGED', 'session target was replaced')
        return identity['window_id']
    if 'window_id' in target:
        return target['window_id']
    selected = require_unique(filter_windows(windows.list_windows(), target.get('window_title'), target.get('process_name')), 'WINDOW')
    return selected['id']


def capture(store, args, controller=None):
    from . import window_backend as windows
    from .image_processing import pillow
    Image, _ = pillow()
    with ActionLock(store.root / '.action_state.json'):
        windows.initialize_dpi_awareness()
        window_id = target_window(store.root, args['target'], controller)
        context = windows.verification_window_context(window_id)
        monitors = windows.monitor_layout()
        if args.get('area', 'window') == 'client':
            origin, client = context['client_origin'], context['client_rect']
            rect = {'left': origin['x'], 'top': origin['y'], 'width': client['width'], 'height': client['height']}
        else:
            rect = dict(context['rect'])
        rect['right'], rect['bottom'] = rect['left'] + rect['width'], rect['top'] + rect['height']
        screen_rect = [rect['left'], rect['top'], rect['width'], rect['height']]
        store.budget([(rect['width'], rect['height'])] * 2)
        if not covered(screen_rect, monitors):
            error('IMAGE_SOURCE_NOT_VISIBLE', 'capture rectangle extends outside physical monitors or crosses a monitor gap')
        if args.get('dry_run'):
            return {'dry_run': True, 'screen_rect': screen_rect, 'width': rect['width'], 'height': rect['height']}
        delay_ms = args.get('capture_delay_ms', store.options.get('capture_delay_ms', 500))
        wait(store.check, delay_ms / 1000)
        if windows.verification_window_context(window_id) != context or windows.monitor_layout() != monitors:
            error('IMAGE_SOURCE_CHANGED', 'window geometry or monitors changed during capture delay')
        store.check()
        _, raw = windows.capture_screen_pixels(rect)
        if windows.verification_window_context(window_id) != context or windows.monitor_layout() != monitors:
            error('IMAGE_SOURCE_CHANGED', 'window geometry or monitors changed during capture')
        with Image.frombytes('RGB', (rect['width'], rect['height']), raw, 'raw', 'BGRX') as image:
            saved = store.save(image, 'source', output=args.get('output_path'), temporary='output_path' not in args,
                details={'capture': {'method': 'visible_screen', 'area': args.get('area', 'window'),
                    'captured_at': utc(), 'captured_epoch': store.clock(), 'screen_rect': screen_rect,
                    'coordinate_units': 'physical-pixels', 'window': context, 'monitors': monitors,
                    'occlusion': 'not-verified', 'capture_delay_ms': delay_ms}})
            return {**store.describe(saved), 'capture': saved['capture']}
