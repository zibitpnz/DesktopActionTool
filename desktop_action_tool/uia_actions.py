"""UI Automation patterns and optional read-only pointer geometry. No input calls."""
import math
from .action_runtime import ActionError

OPERATIONS = ("invoke", "set_value", "select", "set_toggle_state", "expand", "collapse")
CONDITIONS = ("exists", "missing", "enabled", "disabled", "visible", "hidden", "value_equals",
              "text_contains", "selected", "toggle_state", "expand_state")
TOGGLE = {0: "off", 1: "on", 2: "indeterminate"}
EXPAND = {0: "collapsed", 1: "expanded", 2: "partially_expanded", 3: "leaf"}
PATTERNS = {"invoke": "Invoke", "value": "Value", "text": "Text", "selection_item": "SelectionItem",
            "toggle": "Toggle", "expand_collapse": "ExpandCollapse"}
NEXT = "read the control state and refine the selector; do not replay an unconfirmed action"


def fail(code, message):
    raise ActionError(code, message, NEXT)


def validate_selector(selector, *, ancestors=True):
    if not isinstance(selector, dict) or not selector or set(selector) - {
            "name", "automation_id", "control_type", *(('ancestors',) if ancestors else ())}:
        raise ValueError("selector must contain exact name, automation_id or control_type")
    if not any(k in selector for k in ("name", "automation_id", "control_type")):
        raise ValueError("at least one control property is required")
    for key in ("name", "automation_id", "control_type"):
        if key in selector and (not isinstance(selector[key], str) or not 1 <= len(selector[key]) <= 512):
            raise ValueError(key + " must be a nonempty string of at most 512 characters")
    if 'ancestors' in selector:
        parents = selector['ancestors']
        if not isinstance(parents, list) or not 1 <= len(parents) <= 8:
            raise ValueError("ancestors must contain 1..8 selectors, from outermost to innermost")
        for parent in parents:
            validate_selector(parent, ancestors=False)
    return selector


def validate_expected(expected):
    if expected is None:
        return
    if not isinstance(expected, dict) or set(expected) != {"window", "runtime_id"}:
        raise ValueError("expected_control requires window identity and runtime_id")
    identity, rid = expected['window'], expected['runtime_id']
    if (not isinstance(identity, dict) or set(identity) != {'window_id', 'process_id', 'process_created'}
            or any(type(v) is not int or v <= 0 for v in identity.values())):
        raise ValueError("invalid expected window identity")
    if not isinstance(rid, list) or not 1 <= len(rid) <= 64 or any(type(v) is not int or not -2**31 <= v < 2**31 for v in rid):
        raise ValueError("invalid runtime_id")


def validate_condition(condition, value=None):
    if condition not in CONDITIONS:
        raise ValueError("unknown UIA condition")
    choices = {"toggle_state": set(TOGGLE.values()), "expand_state": set(EXPAND.values())}
    if condition in choices:
        if not isinstance(value, str) or value not in choices[condition]:
            raise ValueError("invalid state for " + condition)
    elif condition in ('value_equals', 'text_contains'):
        if not isinstance(value, str) or len(value) > 65536:
            raise ValueError("condition value must be a string of at most 65536 characters")
        if condition == 'text_contains' and not value:
            raise ValueError("text_contains requires nonempty text")
    elif value is not None:
        raise ValueError("this condition does not accept a value")


def matches(control, selector):
    fields = {'name': 'Name', 'automation_id': 'AutomationId', 'control_type': 'ControlTypeName'}
    return all(getattr(control, fields[k]) == v for k, v in selector.items() if k in fields)


def find_one(root, selector, max_depth=16, limit=2000, *, allow_missing=False, within_window=lambda control: True):
    """Complete a bounded traversal; partial trees never prove uniqueness/absence."""
    validate_selector(selector)
    remaining = [limit]

    def search(scope, properties):
        found, truncated = [], False
        def visit(parent, depth):
            nonlocal truncated
            child = parent.GetFirstChildControl()
            while child is not None:
                if remaining[0] <= 0 or depth > max_depth:
                    truncated = True
                    return
                remaining[0] -= 1
                if not within_window(child):
                    # Owned top-level windows can be nested in their owner's UIA tree.
                    # Their entire subtree belongs to a separately selected target.
                    child = child.GetNextSiblingControl()
                    continue
                if matches(child, properties):
                    found.append(child)
                    if len(found) > 1:
                        fail('CONTROL_AMBIGUOUS', 'multiple controls match the selector')
                visit(child, depth + 1)
                if truncated:
                    return
                child = child.GetNextSiblingControl()
        visit(scope, 1)
        if truncated:
            fail('CONTROL_SEARCH_TRUNCATED', 'control traversal exceeded its depth or node budget')
        return found[0] if found else None

    scope = root
    for parent in selector.get('ancestors', []):
        scope = search(scope, parent)
        if scope is None:
            break
    result = search(scope, selector) if scope is not None else None
    if result is None and not allow_missing:
        fail('CONTROL_NOT_FOUND', 'no control matches the selector')
    return result


def condition_matches(state, condition, value=None):
    if condition == 'exists':
        return state is not None
    if condition == 'missing':
        return state is None
    if state is None:
        return False
    keys = {'enabled': ('is_enabled', True), 'disabled': ('is_enabled', False),
            'visible': ('is_offscreen', False), 'hidden': ('is_offscreen', True),
            'selected': ('selected', True), 'toggle_state': ('toggle_state', value),
            'expand_state': ('expand_state', value)}
    if condition in keys:
        key, expected = keys[condition]
        if state.get(key) is None:
            fail('PROPERTY_UNAVAILABLE', 'cannot read ' + key)
        return state[key] == expected
    key = 'value' if condition == 'value_equals' else 'text'
    if state.get(key) is None:
        fail('PROPERTY_UNAVAILABLE', 'cannot read ' + key)
    if condition == 'value_equals':
        if state.get('value_truncated'):
            fail('CONTROL_VALUE_TRUNCATED', 'a truncated value cannot prove equality')
        return state[key] == value
    if value in state[key]:
        return True
    if state.get('text_truncated'):
        fail('CONTROL_TEXT_TRUNCATED', 'requested text is outside the bounded text read or absent')
    return False


class Controls:
    """One worker owns its COM apartment; all returned values are JSON data."""
    def __init__(self, window, *, automation=None, identity=None, native_root=None, geometry_backend=None):
        if automation is None:
            from .controls_backend import import_uiautomation
            automation = import_uiautomation()
        if identity is None:
            from .window_backend import window_identity
            identity = window_identity
        self.auto, self.identity, self.window = automation, identity, window
        if native_root is None:
            from .win32_api import user32
            native_root = lambda handle: int(user32.GetAncestor(handle, 2) or 0)  # GA_ROOT, not GA_ROOTOWNER.
        self.native_root = native_root
        self.geometry_backend = geometry_backend

    def check_window(self):
        if self.identity(self.window['window_id']) != self.window:
            fail('WINDOW_CHANGED', 'the selected window closed or its process changed')

    def pattern(self, control, name):
        # Query the actual provider, including patterns not listed on the library's
        # type-specific Control subclass. Never use its high-level input helpers.
        return control.GetPattern(getattr(self.auto.PatternId, name + 'Pattern'))

    def within_window(self, control):
        handle = control.NativeWindowHandle
        if not handle:
            return True  # Windowless elements are checked through their ancestry.
        root = self.native_root(handle)
        if not root:
            fail('CONTROL_CHANGED', 'the control native window no longer exists')
        return root == self.window['window_id']

    def check_membership(self, control):
        root_id = list(self.auto.ControlFromHandle(self.window['window_id']).GetRuntimeId())
        current = control
        for _ in range(64):
            if current is None:
                break
            if list(current.GetRuntimeId()) == root_id:
                return
            if not self.within_window(current):
                fail('CONTROL_CHANGED', 'the control belongs to a different top-level window; select it explicitly')
            current = current.GetParentControl()
        fail('CONTROL_CHANGED', 'cannot confirm the control belongs to the selected window')

    def locate(self, selector, expected=None, max_depth=16, limit=2000, allow_missing=False):
        self.check_window()
        root = self.auto.ControlFromHandle(self.window['window_id'])
        control = find_one(root, selector, max_depth, limit, allow_missing=allow_missing, within_window=self.within_window)
        self.check_window()
        if control is not None:
            self.check_membership(control)
        if expected is not None:
            validate_expected(expected)
            if (expected['window'] != self.window or control is None
                    or list(control.GetRuntimeId()) != expected['runtime_id']):
                fail('CONTROL_CHANGED', 'the expected control identity changed')
        return control

    def snapshot(self, control, *, include_text=False, max_chars=4096):
        if control is None:
            return None
        unavailable = {}
        def read(key, function):
            try:
                return function()
            except Exception:
                unavailable[key] = 'provider did not expose this property'
                return None
        patterns = {}
        for name, method in PATTERNS.items():
            patterns[name] = read(name, lambda method=method: self.pattern(control, method))
        state = {name: read(name, lambda attr=attr: getattr(control, attr)) for name, attr in
                 {'name': 'Name', 'automation_id': 'AutomationId', 'control_type': 'ControlTypeName',
                  'is_enabled': 'IsEnabled', 'is_offscreen': 'IsOffscreen', 'is_password': 'IsPassword'}.items()}
        state['runtime_id'] = read('runtime_id', lambda: list(control.GetRuntimeId()))
        state['window'] = self.window
        state['patterns'] = [name for name, pattern in patterns.items() if pattern is not None]
        for key, pattern, attr in (('is_read_only', 'value', 'IsReadOnly'), ('selected', 'selection_item', 'IsSelected'),
                                   ('toggle_state', 'toggle', 'ToggleState'), ('expand_state', 'expand_collapse', 'ExpandCollapseState')):
            state[key] = read(key, lambda pattern=pattern, attr=attr: getattr(patterns[pattern], attr))
        state['toggle_state'] = TOGGLE.get(state['toggle_state'])
        state['expand_state'] = EXPAND.get(state['expand_state'])
        if include_text:
            # Unknown password status fails closed too; never read a protected value.
            if state['is_password'] is not False:
                state.update(value=None, text=None, value_truncated=False, text_truncated=False)
                unavailable.update(value='password or unknown password status', text='password or unknown password status')
            else:
                value = read('value', lambda: patterns['value'].Value)
                text = read('text', lambda: patterns['text'].DocumentRange.GetText(max_chars + 1))
                if text is None and isinstance(value, str):
                    text = value
                    state['text_source'] = 'value'
                for key, data in (('value', value), ('text', text)):
                    state[key] = data[:max_chars] if isinstance(data, str) else None
                    state[key + '_truncated'] = isinstance(data, str) and len(data) > max_chars
        state['unavailable'] = {k: v for k, v in unavailable.items() if state.get(k) is None}
        return state

    def inspect(self, request):
        control = self.locate(request['selector'], request.get('expected_control'), request.get('max_depth', 16),
                              request.get('limit', 2000), request.get('allow_missing', False))
        state = self.snapshot(control, include_text=request.get('include_text', False), max_chars=request.get('max_chars', 4096))
        if state is not None and request.get('include_geometry'):
            try:
                geometry = self.geometry(control)
                state['geometry'] = {key: value for key, value in geometry.items() if key not in ('window_context', 'monitors')}
            except Exception as exc:
                state['geometry'] = {'coordinate_units': 'physical-pixels', 'pointer_point': None,
                                     'error_code': getattr(exc, 'code', 'CONTROL_GEOMETRY_UNAVAILABLE')}
                state['unavailable']['geometry'] = 'geometry could not be confirmed'
        return state

    def passive(self, control):
        return (control.ControlTypeName in ('TextControl', 'ImageControl') and control.IsKeyboardFocusable is False
                and not any(self.pattern(control, name) is not None for name in
                            ('Invoke', 'Value', 'SelectionItem', 'Toggle', 'ExpandCollapse')))

    def passive_descendants(self, control):
        # A tree item's full bounds/clickable point may cover an independent child.
        # Look only at its own decoration, never inside another interactive item.
        pending = [(control.GetFirstChildControl(), 1)]
        for _ in range(32):
            if not pending:
                return
            child, depth = pending.pop()
            if child is None:
                continue
            pending.append((child.GetNextSiblingControl(), depth))
            if self.within_window(child) and self.passive(child):
                yield child
                if depth < 8:
                    pending.append((child.GetFirstChildControl(), depth + 1))

    def point_belongs(self, control, point):
        """Accept the element itself or passive text/image descendants, fail closed."""
        hit = self.auto.ControlFromPoint(point['x'], point['y'])
        for _ in range(64):
            if hit is None or not self.within_window(hit):
                return False
            if self.auto.ControlsAreSame(hit, control):
                return True
            if not self.passive(hit):
                return False
            hit = hit.GetParentControl()
        return False

    def geometry(self, control, *, expected=None, required=False):
        # All UIA objects remain in this worker. Coordinates never authorize a click.
        if self.geometry_backend is None:
            from . import window_backend
            from .input_backend import cursor_position
            from types import SimpleNamespace
            self.geometry_backend = SimpleNamespace(cursor_position=cursor_position, **{name: getattr(window_backend, name) for name in
                ('verification_window_context', 'monitor_layout', 'root_window_at_point', 'active_window_id')})
        native = self.geometry_backend
        self.check_window()
        self.check_membership(control)
        raw = control.BoundingRectangle
        rect = {key: getattr(raw, key) for key in ('left', 'top', 'right', 'bottom')}
        if (any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 2**31 - 1 for v in rect.values())
                or rect['right'] <= rect['left'] or rect['bottom'] <= rect['top']):
            fail('CONTROL_GEOMETRY_UNAVAILABLE', 'control has no finite positive bounding rectangle')
        rect.update(width=rect['right'] - rect['left'], height=rect['bottom'] - rect['top'])
        center = lambda area: {'x': math.floor((area['left'] + area['right']) / 2),
                               'y': math.floor((area['top'] + area['bottom']) / 2)}
        inside = lambda area, point: area['left'] <= point['x'] < area['right'] and area['top'] <= point['y'] < area['bottom']
        data = {'coordinate_units': 'physical-pixels', 'screen_rect': rect, 'center_screen': center(rect),
                'pointer_point': None, 'point_source': None}
        if control.IsOffscreen is not False:
            data['unavailable'] = 'control is offscreen or its visibility is unknown'
        else:
            context = native.verification_window_context(self.window['window_id'])
            monitors = native.monitor_layout()
            data.update(window_context=context, monitors=monitors)
            def visible_center(bounds):
                intersections = []
                for monitor in monitors:
                    areas = (bounds, rect, context['rect'], monitor['rect'])
                    area = {key: fun(a[key] for a in areas) for key, fun in
                            (('left', max), ('top', max), ('right', min), ('bottom', min))}
                    if area['left'] < area['right'] and area['top'] < area['bottom']:
                        intersections.append(area)
                if not intersections:
                    return None
                area = max(intersections, key=lambda a: ((a['right']-a['left'])*(a['bottom']-a['top']), -a['left'], -a['top']))
                return center(area)
            def valid(point):
                return (point is not None and all(type(v) is int for v in point.values())
                        and inside(rect, point) and inside(context['rect'], point)
                        and any(inside(m['rect'], point) for m in monitors)
                        and native.root_window_at_point(point) == self.window['window_id'] and self.point_belongs(control, point))
            if expected is not None:
                if any(data[key] != expected[key] for key in ('screen_rect', 'window_context', 'monitors')):
                    fail('CONTROL_CHANGED', 'control geometry or display context changed after pointer preparation')
                point, source = expected['pointer_point'], expected['point_source']
            else:
                x, y, available = control.GetClickablePoint()
                if type(available) is not bool:
                    fail('CONTROL_POINT_UNAVAILABLE', 'provider returned an invalid clickable point')
                if available and (type(x) is not int or type(y) is not int):
                    fail('CONTROL_POINT_UNAVAILABLE', 'provider returned non-integer clickable coordinates')
                point, source = ({'x': x, 'y': y}, 'clickable') if available else (None, 'visible-center')
                if point is None:
                    point = visible_center(rect)
                if not valid(point):
                    point = None
                    for child in self.passive_descendants(control):
                        if child.IsOffscreen is not False:
                            continue
                        raw_child = child.BoundingRectangle
                        bounds = {key: getattr(raw_child, key) for key in ('left', 'top', 'right', 'bottom')}
                        if (any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 2**31 - 1 for v in bounds.values())
                                or bounds['right'] <= bounds['left'] or bounds['bottom'] <= bounds['top']):
                            continue
                        candidate = visible_center(bounds)
                        if valid(candidate):
                            point, source = candidate, 'passive-descendant-center'
                            break
            if valid(point):
                data.update(pointer_point=point, point_source=source)
            else:
                data['unavailable'] = 'point is outside the visible control or belongs to a different element/window'
            if native.verification_window_context(self.window['window_id']) != context or native.monitor_layout() != monitors:
                fail('CONTROL_CHANGED', 'window or display changed while reading geometry')
        self.check_window()
        self.check_membership(control)
        if data['pointer_point'] is None and (required or expected is not None):
            fail('CONTROL_POINT_UNAVAILABLE', data.get('unavailable', 'no confirmed pointer point'))
        return data

    def pointer_geometry(self, request):
        control, prepared = self.prepare(request)
        return {**prepared, 'geometry': None if prepared['noop'] else
                self.geometry(control, expected=request.get('pointer_geometry'), required=True)}

    def verify_pointer(self, control, expected):
        self.geometry(control, expected=expected, required=True)
        native = self.geometry_backend
        try:
            foreground = native.active_window_id()
        except ValueError:
            foreground = None
        if foreground != self.window['window_id']:
            fail('FOCUS_CHANGED', 'the accompanied UIA target is no longer in the foreground')
        if native.cursor_position() != expected['pointer_point']:
            fail('CURSOR_MOVED', 'cursor is no longer at the confirmed UIA point')

    def prepare(self, request):
        operation, value = request['operation'], request.get('value')
        control = self.locate(request['selector'], request.get('expected_control'), request.get('max_depth', 16), request.get('limit', 2000))
        state = self.snapshot(control, include_text=operation == 'set_value', max_chars=65536)
        if state['is_enabled'] is not True:
            fail('CONTROL_NOT_ENABLED', 'the selected control is disabled or its enabled state is unavailable')
        pattern_name = {'invoke': 'invoke', 'set_value': 'value', 'select': 'selection_item',
                        'set_toggle_state': 'toggle', 'expand': 'expand_collapse', 'collapse': 'expand_collapse'}[operation]
        if pattern_name not in state['patterns']:
            fail('PATTERN_UNSUPPORTED', 'the control does not expose ' + pattern_name)
        if operation == 'set_value':
            if state['is_password'] is not False:
                fail('CONTROL_PROTECTED', 'password controls are not supported by direct value operations')
            if state['is_read_only'] is not False:
                fail('CONTROL_READ_ONLY', 'the value is read-only or its write access is unknown')
            condition = ('value_equals', value)
        elif operation == 'select':
            condition = ('selected', None)
        elif operation == 'set_toggle_state':
            condition = ('toggle_state', value)
        elif operation in ('expand', 'collapse'):
            if state['expand_state'] == 'leaf':
                fail('PATTERN_UNSUPPORTED', 'a leaf cannot expand or collapse')
            condition = ('expand_state', 'expanded' if operation == 'expand' else 'collapsed')
        else:
            condition = None
        noop = condition is not None and condition_matches(state, *condition)
        expected = {'window': self.window, 'runtime_id': state['runtime_id']}
        validate_expected(expected)
        return control, {'control': state, 'expected_control': expected, 'noop': noop,
                         'condition': {'condition': condition[0], 'value': condition[1]} if condition else None}

    def perform(self, request, dispatch, returned):
        # Resolve again after the parent's delay/permit; never use stale list indices.
        control, prepared = self.prepare(request)
        if prepared['noop']:
            return {'called': False, 'prepared': prepared}
        if request.get('pointer_geometry') is not None:
            self.verify_pointer(control, request['pointer_geometry'])
        op = request['operation']
        pattern, method = {'invoke': ('Invoke', 'Invoke'), 'set_value': ('Value', 'SetValue'),
                           'select': ('SelectionItem', 'Select'), 'set_toggle_state': ('Toggle', 'Toggle'),
                           'expand': ('ExpandCollapse', 'Expand'), 'collapse': ('ExpandCollapse', 'Collapse')}[op]
        interface = self.pattern(control, pattern)
        if interface is None:
            fail('PATTERN_UNSUPPORTED', 'pattern disappeared before the action')
        self.check_window()
        dispatch()
        self.check_window()
        self.check_membership(control)
        if list(control.GetRuntimeId()) != prepared['expected_control']['runtime_id']:
            fail('CONTROL_CHANGED', 'control identity changed immediately before invocation')
        if request.get('pointer_geometry') is not None:
            self.verify_pointer(control, request['pointer_geometry'])
        # A provider exception after entry does not prove that no effect happened.
        args = [request['value']] if op == 'set_value' else []
        result = getattr(interface, method)(*args, waitTime=0)
        answer = {'called': True, 'provider_ok': bool(result)}
        returned(answer)
        return answer
