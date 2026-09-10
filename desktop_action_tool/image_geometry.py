"""Image coordinates and immutable provenance in physical source pixels."""
from copy import deepcopy
import math

from .action_runtime import ActionError


def error(code, message):
    raise ActionError(code, message, 'inspect image metadata and correct the request; do not recapture automatically')


def number(value, name, low, high, *, integer=False):
    if type(value) not in ((int,) if integer else (int, float)) or not math.isfinite(value) or not low <= value <= high:
        error('IMAGE_ARGUMENT_INVALID', f'{name} must be {"an integer" if integer else "a finite number"} between {low} and {high}')
    return value


def rectangle(value, width, height):
    if not isinstance(value, dict) or set(value) != {'x', 'y', 'width', 'height'}:
        error('REGION_INVALID', 'rectangle requires x, y, width, height in source image pixels')
    x, y, w, h = (value[key] for key in ('x', 'y', 'width', 'height'))
    if any(type(v) is not int for v in (x, y, w, h)) or min(x, y) < 0 or min(w, h) <= 0 or x + w > width or y + h > height:
        error('REGION_INVALID', 'rectangle is outside the source image; no implicit clipping is performed')
    return [x, y, w, h]


def intersection(a, b):
    x, y = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return [x, y, right - x, bottom - y] if right > x and bottom > y else None


def transform_mappings(mappings, sx, sy, ox, oy, clip):
    """Map capture coordinates to output coordinates, clipping non-content margins."""
    result = []
    for original in mappings:
        item = deepcopy(original)
        x, y, w, h = item['content_rect']
        visible = intersection([x * sx + ox, y * sy + oy, w * sx, h * sy], clip)
        if visible is None:
            continue
        item['scale'] = [item['scale'][0] * sx, item['scale'][1] * sy]
        item['offset'] = [item['offset'][0] * sx + ox, item['offset'][1] * sy + oy]
        item['content_rect'] = visible
        result.append(item)
    if len(result) > 256:
        error('IMAGE_LIMIT_EXCEEDED', 'image provenance exceeds 256 capture mappings')
    return result


def source_point(mapping, x, y):
    left, top, width, height = mapping['content_rect']
    if not (left <= x < left + width and top <= y < top + height):
        return None
    return [(x - mapping['offset'][0]) / mapping['scale'][0],
            (y - mapping['offset'][1]) / mapping['scale'][1]]


def resize_plan(width, height, options, reference=None):
    choices = [key for key in ('scale', 'width', 'height', 'canvas', 'reference_image_id') if key in options]
    if len(choices) != 1:
        error('IMAGE_ARGUMENT_INVALID', 'choose exactly one size: scale, width, height, canvas or reference_image_id')
    key = choices[0]
    if key == 'scale':
        factor = number(options[key], key, 0.001, 1000)
        content = [max(1, int(width * factor + 0.5)), max(1, int(height * factor + 0.5))]
        canvas = content
    elif key in ('width', 'height'):
        value = number(options[key], key, 1, 32768, integer=True)
        factor = value / (width if key == 'width' else height)
        content = [max(1, int(width * factor + 0.5)), max(1, int(height * factor + 0.5))]
        canvas = content
    else:
        value = options['canvas'] if key == 'canvas' else reference
        if not isinstance(value, dict) or set(value) != {'width', 'height'}:
            error('IMAGE_ARGUMENT_INVALID', 'canvas requires width and height')
        canvas = [number(value[k], k, 1, 32768, integer=True) for k in ('width', 'height')]
        factor = min(canvas[0] / width, canvas[1] / height)
        content = [min(canvas[0], max(1, int(width * factor + 0.5))), min(canvas[1], max(1, int(height * factor + 0.5)))]
    offset = [(canvas[0] - content[0]) // 2, (canvas[1] - content[1]) // 2]
    return {'width': canvas[0], 'height': canvas[1], 'content_width': content[0], 'content_height': content[1],
            'scale': [content[0] / width, content[1] / height], 'offset': offset,
            'content_rect': [*offset, *content]}


def layout_plan(sizes, options):
    normalized = options.get('normalize', 'none') == 'match_larger'
    common = {'width': max(s[0] for s in sizes), 'height': max(s[1] for s in sizes)}
    plans = [resize_plan(w, h, {'canvas': common} if normalized else {'scale': 1}) for w, h in sizes]
    layout = options.get('layout', 'horizontal')
    cols = len(plans) if layout == 'horizontal' else 1 if layout == 'vertical' else options.get('columns', math.ceil(math.sqrt(len(plans))))
    if not 1 <= cols <= len(plans):
        error('IMAGE_ARGUMENT_INVALID', 'columns must not exceed the number of images')
    rows = math.ceil(len(plans) / cols)
    gap, padding = options.get('gap', 16), options.get('padding', 16)
    label_height = 24 if options.get('labels', True) else 0
    widths = [max(p['width'] for i, p in enumerate(plans) if i % cols == col) for col in range(cols)]
    heights = [max(p['height'] + label_height for p in plans[row * cols:(row + 1) * cols]) for row in range(rows)]
    for i, plan in enumerate(plans):
        row, col = divmod(i, cols)
        plan['position'] = [padding + sum(widths[:col]) + gap * col, padding + sum(heights[:row]) + gap * row + label_height]
    return {'width': 2 * padding + sum(widths) + gap * (cols - 1),
            'height': 2 * padding + sum(heights) + gap * (rows - 1),
            'items': plans, 'label_height': label_height, 'columns': cols}
