"""Bounded file operations; no input, focus changes or implicit screen capture."""
import io
from .action_runtime import ActionError

from .image_geometry import rectangle, resize_plan, layout_plan, transform_mappings, error


def pillow():
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        error('IMAGE_PROCESSING_UNAVAILABLE', 'install image support: uv sync --locked --extra mcp --extra uia --extra images')
    return Image, ImageDraw


def parent(meta):
    return {key: meta[key] for key in ('image_id', 'sha256', 'width', 'height', 'kind', 'created_at')}


def require_clean(meta):
    if meta.get('annotations'):
        error('IMAGE_ANNOTATED_SOURCE', 'use source_image_id, not the annotated preview image_id, for further processing')


def load(store, identifier):
    Image, _ = pillow()
    meta, raw = store.get(identifier, pixels=True)
    try:
        with Image.open(io.BytesIO(raw), formats=['PNG']) as image:
            image.load()
            store.check()
            return meta, image.convert('RGB')
    except ActionError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError):
        error('IMAGE_FORMAT_UNSUPPORTED', 'registered PNG could not be decoded within its limits')


def render_resize(image, plan, resampling, background='#ffffff'):
    Image, _ = pillow()
    method = Image.Resampling.LANCZOS if resampling == 'smooth' else Image.Resampling.NEAREST
    content = image.resize((plan['content_width'], plan['content_height']), resample=method)
    try:
        result = Image.new('RGB', (plan['width'], plan['height']), background)
        result.paste(content, tuple(plan['offset']))
        return result
    finally:
        content.close()


def resize_details(meta, plan, method):
    return {'parents': [parent(meta)], 'annotations': bool(meta.get('annotations')),
            'mappings': transform_mappings(meta['mappings'], *plan['scale'], *plan['offset'],
            [0, 0, plan['width'], plan['height']]),
            'transform': {'operation': 'resize', **plan, 'resampling': method,
                          'filter': 'lanczos' if method == 'smooth' else 'nearest'}}


def preview(store, args):
    meta, _ = store.get(args['image_id'])
    require_clean(meta)
    rect = rectangle(args['rectangle'], meta['width'], meta['height'])
    store.budget([(meta['width'], meta['height'])] * 3)
    if args.get('dry_run'):
        return {'dry_run': True, 'selection_rect': rect, 'width': meta['width'], 'height': meta['height']}
    _, image = load(store, args['image_id'])
    Image, ImageDraw = pillow()
    try:
        tint = Image.new('RGB', image.size, '#000000')
        result = Image.blend(image, tint, 0.45)
        tint.close()
        try:
            x, y, width, height = rect
            with image.crop((x, y, x + width, y + height)) as content:
                result.paste(content, (x, y))
            draw = ImageDraw.Draw(result)
            draw.rectangle((x, y, x + width - 1, y + height - 1), outline='#00aaff', width=2)
            store.check()
            region = store.region(args['image_id'], rect)
            saved = store.save(result, 'preview', temporary=True, details={'parents': [parent(meta)],
                'mappings': [], 'selection_rect': rect, 'region_id': region['region_id'],
                'preview_transform': {'scale': [1, 1], 'offset': [0, 0]}, 'annotations': True})
            return {**store.describe(saved), 'region_id': region['region_id'], 'selection_rect': rect,
                    'source_image_id': args['image_id'], 'expires_epoch': region['expires_epoch'],
                    'preview_transform': saved['preview_transform']}
        finally:
            result.close()
    finally:
        image.close()


def crop(store, args, fingerprint):
    region, meta = store.get_region(args['region_id'])
    require_clean(meta)
    x, y, width, height = region['rect']
    store.budget([(meta['width'], meta['height']), (width, height)])
    if args.get('dry_run'):
        return {'dry_run': True, 'selection_rect': region['rect'], 'width': width, 'height': height}
    _, image = load(store, region['image_id'])
    try:
        with image.crop((x, y, x + width, y + height)) as result:
            details = {'parents': [parent(meta)], 'selection_rect': region['rect'], 'region_id': args['region_id'],
                       'mappings': transform_mappings(meta['mappings'], 1, 1, -x, -y, [0, 0, width, height]),
                       'transform': {'operation': 'crop', 'scale': [1, 1], 'offset': [-x, -y]}}
            saved = store.save(result, 'region', output=args.get('output_path'), details=details,
                               request_id=args.get('request_id'), fingerprint=fingerprint)
            return store.describe(saved)
    finally:
        image.close()


def resize(store, args, fingerprint):
    meta, _ = store.get(args['image_id'])
    require_clean(meta)
    reference = None
    if 'reference_image_id' in args:
        ref, _ = store.get(args['reference_image_id'])
        reference = {key: ref[key] for key in ('width', 'height')}
    plan = resize_plan(meta['width'], meta['height'], args, reference)
    store.budget([(meta['width'], meta['height']), (plan['width'], plan['height'])])
    if args.get('dry_run'):
        return {'dry_run': True, 'transform': plan, 'width': plan['width'], 'height': plan['height']}
    _, image = load(store, args['image_id'])
    try:
        with render_resize(image, plan, args['resampling'], args.get('background', '#ffffff')) as result:
            store.check()
            saved = store.save(result, 'resized', output=args.get('output_path'),
                details=resize_details(meta, plan, args['resampling']), request_id=args.get('request_id'), fingerprint=fingerprint)
            return {**store.describe(saved), 'transform': saved['transform']}
    finally:
        image.close()


def compose(store, args, fingerprint):
    records = [store.get(identifier)[0] for identifier in args['image_ids']]
    for meta in records:
        require_clean(meta)
    sizes = [(m['width'], m['height']) for m in records]
    plan = layout_plan(sizes, args)
    store.budget([*sizes, (plan['width'], plan['height']),
                  (max(p['width'] for p in plan['items']), max(p['height'] for p in plan['items']))])
    if args.get('dry_run'):
        return {'dry_run': True, 'layout': plan, 'width': plan['width'], 'height': plan['height']}
    Image, ImageDraw = pillow()
    canvas = Image.new('RGB', (plan['width'], plan['height']), args.get('background', '#ffffff'))
    mappings, items = [], []
    try:
        for i, (meta, item_plan) in enumerate(zip(records, plan['items'])):
            store.check()
            _, original = load(store, meta['image_id'])
            try:
                with render_resize(original, item_plan, args['resampling'], args.get('background', '#ffffff')) as content:
                    x, y = item_plan['position']
                    canvas.paste(content, (x, y))
                    label = chr(65 + i)
                    if plan['label_height']:
                        ImageDraw.Draw(canvas).text((x, y - plan['label_height'] + 4),
                            f'{label}: {meta["width"]}x{meta["height"]}', fill='#202020')
                    ox, oy = item_plan['offset']
                    mappings.extend(transform_mappings(meta['mappings'], *item_plan['scale'], x + ox, y + oy,
                                                       [x, y, item_plan['width'], item_plan['height']]))
                    items.append({'label': label, **parent(meta), 'placement': item_plan, 'resampling': args['resampling']})
            finally:
                original.close()
        if len(mappings) > 256:
            error('IMAGE_LIMIT_EXCEEDED', 'composite has more than 256 capture mappings')
        saved = store.save(canvas, 'composite', output=args.get('output_path'), details={'parents': [parent(m) for m in records],
            'mappings': mappings, 'layout': {'type': args.get('layout', 'horizontal'), 'normalize': args.get('normalize', 'none'),
                                          'items': items, 'columns': plan['columns']}},
            request_id=args.get('request_id'), fingerprint=fingerprint)
        return {**store.describe(saved), 'items': [{'image_id': item['image_id'], 'label': item['label'],
                                                'placement': item['placement']} for item in items]}
    finally:
        canvas.close()
