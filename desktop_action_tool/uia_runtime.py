"""CLI contract and cancellable timing for direct control operations."""
from contextlib import nullcontext
import json
import math
from pathlib import Path
import sys
import time
import uuid

from .action_runtime import ActionAborted, ActionError
from .uia_actions import OPERATIONS, CONDITIONS, condition_matches, validate_selector, validate_expected, validate_condition
from .worker_client import UiaSession, UIA_RECOVERY_NAME, write_uia_marker

MODES = {'uia_control_state', 'uia_action', 'uia_wait_state', 'wait_ms'}
SETTINGS = {'uia_action_before_delay_ms': (0, 60000, int), 'uia_action_after_delay_ms': (0, 60000, int),
            'uia_call_timeout_s': (0.1, 120, float), 'uia_wait_timeout_s': (0.1, 120, float),
            'uia_poll_interval_ms': (20, 5000, int), 'uia_cursor_follow_enabled': (0, 1, int),
            'uia_cursor_pause_ms': (0, 60000, int)}


def number(value, low, high, kind, name):
    if type(value) not in ((int,) if kind is int else (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name} must be {low}..{high}' + (' (integer)' if kind is int else ' (finite number)'))
    return value


def mode(args):
    return next((name for name in MODES if (getattr(args, name, None) is not None if name == 'wait_ms'
                                          else bool(getattr(args, name, None)))), None)


def add_arguments(parser):
    group = parser.add_argument_group('Direct UI Automation (optional cursor motion, no simulated clicks or keys)')
    group.add_argument('--uia-control-state', action='store_true', help='Read one control and supported patterns as JSON.')
    group.add_argument('--uia-action', choices=OPERATIONS, help='Perform one direct UIA operation; never falls back to input.')
    group.add_argument('--uia-wait-state', choices=CONDITIONS, help='Wait for a control condition without input.')
    group.add_argument('--wait-ms', type=int, help='Interruptible pause, 0..60000 milliseconds; no UIA dependency.')
    group.add_argument('--uia-control-type', help='Exact UIA ControlTypeName for direct operations.')
    group.add_argument('--uia-selector', type=json.loads, help='JSON selector, optionally with ancestors from outermost to innermost.')
    group.add_argument('--uia-expected-control', type=json.loads, help='JSON window identity/runtime_id returned by --uia-control-state.')
    group.add_argument('--uia-value', help='Whole string for set_value or desired on/off/indeterminate state.')
    group.add_argument('--uia-value-stdin', action='store_true', help='Read the direct action value from stdin, up to 65536 characters.')
    group.add_argument('--uia-condition-value', help='Value for value_equals/text_contains/toggle_state/expand_state.')
    group.add_argument('--uia-expect', type=json.loads, help='Optional JSON {selector, condition, value} to verify Invoke.')
    group.add_argument('--uia-include-text', action='store_true', help='Explicitly read bounded non-password value/text.')
    group.add_argument('--uia-max-chars', type=int, default=4096, help='Text output limit: 1..65536; default 4096.')
    group.add_argument('--uia-search-depth', type=int, default=16, help='Complete search depth limit: 1..32; default 16.')
    group.add_argument('--uia-search-limit', type=int, default=2000, help='Visited-node budget: 1..10000; default 2000.')
    group.add_argument('--uia-before-delay-ms', type=int, help='Pause before lookup/action; defaults to settings (0 ms).')
    group.add_argument('--uia-after-delay-ms', type=int, help='Pause after every confirmed action; defaults to settings (500 ms).')
    group.add_argument('--uia-call-timeout-s', type=float, help='Maximum time per UIA phase; defaults to settings (5 s).')
    group.add_argument('--operation-timeout-s', type=float, help='Total budget for direct UIA/wait, including all pauses; default 60 s.')
    following = group.add_mutually_exclusive_group()
    following.add_argument('--uia-cursor-follow', dest='uia_cursor_follow', action='store_true', default=None,
                           help='Smoothly point at the UIA target before its pattern call; requires visible foreground target.')
    following.add_argument('--no-uia-cursor-follow', dest='uia_cursor_follow', action='store_false',
                           help='Disable cursor accompaniment for this action, overriding settings.')
    group.add_argument('--uia-cursor-pause-ms', type=int, help='Pause after pointer arrival before the pattern; settings default 250 ms.')
    group.add_argument('--uia-include-geometry', action='store_true', help='Read optional physical geometry with --uia-control-state; never moves the cursor.')


def apply_settings(args, settings):
    args.uia_cursor_explicit = args.uia_cursor_follow is not None or args.uia_cursor_pause_ms is not None
    args.uia_cursor_pause_explicit = args.uia_cursor_pause_ms is not None
    args.uia_smooth_explicit = args.smooth_move is not None
    if args.uia_cursor_follow is None:
        args.uia_cursor_follow = bool(args.uia_action and settings['uia_cursor_follow_enabled'])
    if args.uia_cursor_pause_ms is None:
        args.uia_cursor_pause_ms = settings['uia_cursor_pause_ms']
    args.initial_delay_explicit = args.initial_delay_s is not None
    if args.initial_delay_s is None:
        args.initial_delay_s = 3.0
    direct = mode(args)
    if args.timeout_s is None:
        args.timeout_s = settings['uia_wait_timeout_s'] if direct else 5.0
    if args.poll_interval_ms is None:
        args.poll_interval_ms = settings['uia_poll_interval_ms'] if direct else 100
    args.uia_timing_explicit = any(getattr(args, k) is not None for k in
                                 ('uia_before_delay_ms', 'uia_after_delay_ms', 'uia_call_timeout_s', 'operation_timeout_s'))
    args.uia_pause_explicit = args.uia_before_delay_ms is not None or args.uia_after_delay_ms is not None
    for key, setting in (('uia_before_delay_ms', 'uia_action_before_delay_ms'),
                         ('uia_after_delay_ms', 'uia_action_after_delay_ms'), ('uia_call_timeout_s', 'uia_call_timeout_s')):
        if getattr(args, key) is None:
            setattr(args, key, settings[setting])
    if args.operation_timeout_s is None:
        args.operation_timeout_s = 60


def validate(args):
    direct = mode(args)
    extras = (args.uia_control_type is not None or args.uia_selector is not None or args.uia_expected_control is not None
              or args.uia_value is not None or args.uia_value_stdin or args.uia_condition_value is not None
              or args.uia_expect is not None or args.uia_include_text or args.uia_timing_explicit
              or args.uia_cursor_explicit or args.uia_include_geometry)
    if not direct:
        if extras:
            raise ValueError('direct UIA options require --uia-control-state, --uia-action, --uia-wait-state or --wait-ms')
        return
    if args.initial_delay_explicit:
        raise ValueError('use --uia-before-delay-ms for direct UIA, not --initial-delay-s')
    if args.uia_smooth_explicit:
        raise ValueError('direct UIA uses --uia-cursor-follow; accompaniment is always smooth')
    if args.uia_cursor_explicit and not args.uia_action:
        raise ValueError('cursor accompaniment options require --uia-action')
    if args.uia_cursor_pause_explicit and not args.uia_cursor_follow:
        raise ValueError('cursor pause requires enabled cursor accompaniment')
    if args.uia_include_geometry and not args.uia_control_state:
        raise ValueError('--uia-include-geometry requires --uia-control-state')
    number(args.uia_cursor_pause_ms, 0, 60000, int, 'cursor_pause_ms')
    if args.uia_cursor_follow and args.min_mouse_move_duration_ms <= 0:
        raise ValueError('cursor accompaniment requires positive smooth movement duration')
    if args.uia_pause_explicit and not args.uia_action:
        raise ValueError('before/after pauses require --uia-action; use --wait-ms for a standalone pause')
    if args.uia_include_text and not args.uia_control_state:
        raise ValueError('--uia-include-text requires --uia-control-state')
    if args.uia_control_types or args.uia_include_offscreen:
        raise ValueError('direct UIA uses --uia-control-type and includes offscreen elements without legacy listing flags')
    number(args.operation_timeout_s, 1, 600, float, 'operation_timeout_s')
    if direct == 'wait_ms':
        number(args.wait_ms, 0, 60000, int, 'wait_ms')
        if args.wait_ms / 1000 >= args.operation_timeout_s:
            raise ValueError('operation timeout must exceed the requested pause')
        if any((args.uia_selector, args.uia_name, args.uia_automation_id, args.uia_control_type, args.uia_include_text,
                args.uia_value is not None, args.uia_value_stdin, args.uia_expected_control, args.uia_expect, args.uia_condition_value is not None)):
            raise ValueError('--wait-ms does not accept control options')
        return
    if args.uia_selector is not None and any(k is not None for k in (args.uia_name, args.uia_automation_id, args.uia_control_type)):
        raise ValueError('use either --uia-selector or individual selector fields')
    args.uia_selector = validate_selector(args.uia_selector if args.uia_selector is not None else {
        k: v for k, v in (('name', args.uia_name), ('automation_id', args.uia_automation_id), ('control_type', args.uia_control_type)) if v is not None})
    validate_expected(args.uia_expected_control)
    for value, low, high, kind, name in ((args.uia_before_delay_ms, 0, 60000, int, 'before_delay_ms'),
             (args.uia_after_delay_ms, 0, 60000, int, 'after_delay_ms'), (args.uia_call_timeout_s, 0.1, 120, float, 'call_timeout_s'),
             (args.timeout_s, 0.1, 120, float, 'timeout_s'), (args.poll_interval_ms, 20, 5000, int, 'poll_interval_ms'),
             (args.uia_max_chars, 1, 65536, int, 'max_chars'), (args.uia_search_depth, 1, 32, int, 'max_depth'),
             (args.uia_search_limit, 1, 10000, int, 'limit')):
        number(value, low, high, kind, name)
    if args.uia_value_stdin:
        if args.uia_value is not None:
            raise ValueError('choose one UIA value source')
        args.uia_value = sys.stdin.read(65537)
    if args.uia_action in ('set_value', 'set_toggle_state'):
        if not isinstance(args.uia_value, str) or len(args.uia_value) > 65536:
            raise ValueError('the UIA action requires a value of at most 65536 characters')
        if args.uia_action == 'set_toggle_state' and args.uia_value not in ('on', 'off', 'indeterminate'):
            raise ValueError('toggle state must be on, off or indeterminate')
    elif args.uia_value is not None or args.uia_value_stdin:
        raise ValueError('a value is only accepted by set_value/set_toggle_state')
    if args.uia_wait_state:
        validate_condition(args.uia_wait_state, args.uia_condition_value)
    elif args.uia_condition_value is not None:
        raise ValueError('--uia-condition-value requires --uia-wait-state')
    if args.uia_expect is not None:
        if args.uia_action != 'invoke' or not isinstance(args.uia_expect, dict) or set(args.uia_expect) - {'selector', 'condition', 'value'}:
            raise ValueError('--uia-expect is an Invoke condition with selector, condition and optional value')
        validate_selector(args.uia_expect.get('selector'))
        validate_condition(args.uia_expect.get('condition'), args.uia_expect.get('value'))
    cursor_pause = args.uia_cursor_pause_ms if args.uia_cursor_follow else 0
    if args.uia_action and (args.uia_before_delay_ms + args.uia_after_delay_ms + cursor_pause) / 1000 + args.activity_frame_lead_ms / 1000 >= args.operation_timeout_s:
        raise ValueError('operation timeout leaves no time after the configured pauses')


def plan(args):
    return {'ok': True, 'mode': mode(args).replace('_', '-'), 'backend': 'uia' if mode(args) != 'wait_ms' else 'timer',
            'window_id': args.window_id, 'operation': args.uia_action,
            'selector': args.uia_selector, 'execution_status': 'not_started', 'effect_status': 'not_checked',
            'changed': False, 'completed_calls': 0,
            'timing': {'before_delay_ms': args.uia_before_delay_ms if args.uia_action else 0,
                       'after_delay_ms': args.uia_after_delay_ms if args.uia_action else 0,
                       'call_timeout_s': args.uia_call_timeout_s, 'timeout_s': args.timeout_s,
                       'poll_interval_ms': args.poll_interval_ms, 'operation_timeout_s': args.operation_timeout_s},
            **({'cursor_follow': {'enabled': args.uia_cursor_follow,
                'status': 'planned' if args.dry_run and args.uia_cursor_follow else 'failed' if args.uia_cursor_follow else 'disabled',
                'steps': 0, 'duration_ms': 0, 'pause_ms': 0, 'final_position': None,
                'planned_pause_ms': args.uia_cursor_pause_ms if args.uia_cursor_follow else 0}} if args.uia_action else {}),
            **({'duration_ms': args.wait_ms} if args.wait_ms is not None else {})}


class Runner:
    def __init__(self, args, operation, directory, window, result, deadline, *, session_factory=None, cli=None):
        self.args, self.operation, self.directory, self.window = args, operation, Path(directory), window
        self.result, self.deadline, self.session_factory = result, deadline, session_factory or UiaSession
        self.operation_id = result['operation_id']
        self.worker, self.pending = None, False
        self.cli, self.cursor = cli, None

    def remaining(self):
        self.operation.check()
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise ActionError('OPERATION_TIMEOUT', 'the complete operation exceeded its deadline')
        return left

    def call(self, command, *, budget=None, progress=None, **params):
        limit = min(self.remaining(), self.args.uia_call_timeout_s)
        if budget is not None:
            limit = min(limit, budget - time.monotonic())
        if limit <= 0:
            raise ActionError('CONDITION_TIMEOUT', 'the control condition did not complete in time')
        result = self.worker.request({'command': command, **params}, limit, progress)
        self.remaining()
        if budget is not None and time.monotonic() > budget:
            raise ActionError('CONDITION_TIMEOUT', 'the control condition exceeded its deadline')
        return result

    def query(self, selector, *, expected=None, text=False, budget=None, geometry=False):
        return self.call('inspect', selector=selector, expected_control=expected, allow_missing=True,
                         include_text=text, include_geometry=geometry, max_chars=max(self.args.uia_max_chars, len(self.args.uia_value or '')),
                         max_depth=self.args.uia_search_depth, limit=self.args.uia_search_limit, budget=budget)

    def wait_for(self, expectation, expected=None, *, previous_toggle=None):
        deadline = min(self.deadline, time.monotonic() + self.args.timeout_s)
        while True:
            state = self.query(expectation['selector'], expected=expected, text=expectation['condition'] in ('value_equals', 'text_contains'), budget=deadline)
            if previous_toggle is not None:
                ready = state is not None and state.get('toggle_state') is not None and state['toggle_state'] != previous_toggle
            else:
                ready = condition_matches(state, expectation['condition'], expectation.get('value'))
            if ready:
                return state
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ActionError('CONDITION_TIMEOUT', 'the requested control state was not observed', 'read the current state; do not repeat a completed action')
            self.operation.wait(min(self.args.poll_interval_ms / 1000, remaining))

    def progress(self, message):
        if message['stage'] == 'dispatching':
            self.remaining()
            if self.cursor:
                self.cursor.check()
            write_uia_marker(self.directory, self.operation_id, 'dispatching', window=self.window,
                             operation=self.args.uia_action, completed_calls=self.result['completed_calls'])
            self.pending = True
            self.result.update(execution_status='unknown', changed=None)
            if self.cursor:
                # The provider may legitimately open a dialog/change focus once permitted.
                self.cursor.deactivate()
        else:
            self.record_return(message['result'])

    def record_return(self, answer):
        if not isinstance(answer, dict) or answer.get('called') is not True or type(answer.get('provider_ok')) is not bool:
            raise ActionError('UIA_WORKER_FAILED', 'invalid confirmation of a pattern call')
        if self.pending:
            self.result['completed_calls'] += int(answer.get('called', False))
        self.pending = False
        self.result['execution_status'] = 'returned'
        self.result['provider_ok'] = answer.get('provider_ok')

    def run(self):
        a = self.args
        if a.uia_action:
            self.operation.wait(a.uia_before_delay_ms / 1000)
        self.worker = self.session_factory(self.operation.check)
        self.call('init', window=self.window, directory=str(self.directory), operation_id=self.operation_id)
        if a.uia_control_state:
            state = self.query(a.uia_selector, expected=a.uia_expected_control, text=a.uia_include_text,
                               geometry=a.uia_include_geometry)
            if state is None:
                raise ActionError('CONTROL_NOT_FOUND', 'no control matches the selector')
            self.result['control'] = state
            if state.get('runtime_id'):
                self.result['expected_control'] = {'window': self.window, 'runtime_id': state['runtime_id']}
            return
        if a.uia_wait_state:
            self.result['control'] = self.wait_for({'selector': a.uia_selector, 'condition': a.uia_wait_state,
                                                   'value': a.uia_condition_value}, a.uia_expected_control)
            self.result['effect_status'] = 'verified'
            return
        request = {'selector': a.uia_selector, 'expected_control': a.uia_expected_control,
                   'operation': a.uia_action, 'value': a.uia_value, 'max_depth': a.uia_search_depth, 'limit': a.uia_search_limit}
        prepared = self.call('prepare', **request)
        request['expected_control'] = prepared['expected_control']
        self.result['expected_control'] = prepared['expected_control']
        self.result['control'] = {k: v for k, v in prepared['control'].items() if k not in ('value', 'text')}
        expectation = {'selector': a.uia_selector, **prepared['condition']} if prepared['condition'] else a.uia_expect
        self.result['noop'] = prepared['noop']
        seen = {prepared['control'].get('toggle_state')}
        previous_toggle = prepared['control'].get('toggle_state')
        if not prepared['noop'] and a.uia_cursor_follow:
            from .uia_cursor import CursorFollow
            pointer = self.call('pointer_geometry', **request)
            prepared['noop'] = self.result['noop'] = pointer['noop']
            if not pointer['noop']:
                request['pointer_geometry'] = pointer['geometry']
                self.cursor = CursorFollow(a, self.cli, self.operation, self.result['cursor_follow'], pointer['geometry'])
                self.cursor.run()
        if prepared['noop']:
            if a.uia_cursor_follow:
                self.result['cursor_follow']['status'] = 'skipped_noop'
            self.operation.wait(a.uia_after_delay_ms / 1000)
        else:
            for _ in range(3 if a.uia_action == 'set_toggle_state' else 1):
                if self.cursor:
                    self.cursor.activate()
                answer = self.call('perform', progress=self.progress, **request)
                if self.cursor:
                    self.cursor.deactivate()
                if not answer.get('called'):
                    self.result['noop'] = self.result['completed_calls'] == 0
                if answer.get('called') and not answer.get('provider_ok'):
                    raise ActionError('UIA_PROVIDER_FAILED', 'the pattern call returned an unsuccessful result', 'inspect the application before another action')
                self.operation.wait(a.uia_after_delay_ms / 1000)
                if a.uia_action != 'set_toggle_state' or not answer.get('called'):
                    break
                state = self.wait_for(expectation, request['expected_control'], previous_toggle=previous_toggle)
                current = state['toggle_state']
                if current == a.uia_value:
                    break
                if current in seen:
                    raise ActionError('TOGGLE_STATE_UNREACHABLE', 'the toggle cycle did not reach the requested state')
                seen.add(current)
                previous_toggle = current
            else:
                raise ActionError('TOGGLE_STATE_UNREACHABLE', 'toggle state was not reached in three calls')
        if expectation:
            state = self.wait_for(expectation, request['expected_control'] if a.uia_action != 'invoke' else None)
            if state is not None:
                for key in ('value', 'text'):
                    if isinstance(state.get(key), str) and len(state[key]) > a.uia_max_chars:
                        state[key], state[key + '_truncated'] = state[key][:a.uia_max_chars], True
            self.result.update(effect_status='verified', observed=state)
            if a.uia_action != 'invoke':
                self.result['changed'] = self.result['completed_calls'] > 0
        self.result['action_completed'] = self.result['execution_status'] == 'returned'

    def close(self):
        if self.cursor:
            self.cursor.close()
        if self.worker:
            self.worker.close()
        marker = self.directory / UIA_RECOVERY_NAME
        if marker.exists():
            saved = json.loads(marker.read_text(encoding='utf-8'))
            if saved.get('operation_id') == self.operation_id:
                if self.pending and saved.get('status') == 'returned':
                    self.record_return(saved['result'])
                if not self.pending:
                    marker.unlink()


def execute(args, cli):
    result = plan(args)
    if args.dry_run:
        return {**result, 'dry_run': True, 'patterns_checked': False,
                'executable': args.window_id is not None or args.wait_ms is not None}
    started = getattr(args, 'operation_started', time.monotonic())
    deadline = started + args.operation_timeout_s
    controller = args.controller
    if controller and 'operation_deadline' in controller.payload:
        deadline = min(deadline, controller.payload['operation_deadline'])
    result['operation_id'] = uuid.uuid4().hex
    operation = cli.Cancellation(cli.is_escape_down, enabled=not args.no_abort_key)
    operation.interaction_profile = getattr(args, 'interaction_profile', None)
    def check():
        if controller:
            controller.check()
        if time.monotonic() >= deadline:
            raise ActionError('OPERATION_TIMEOUT', 'the complete operation exceeded its deadline')
    operation.external_check = check
    runner = None
    try:
        window = None
        if args.wait_ms is None:
            if args.window_id is None:
                raise ActionError('TARGET_REQUIRED', 'select a window or bound session for direct UIA', 'use --window-id or explicit window selectors')
            window = getattr(args, 'selected_identity', None) or cli.window_identity(args.window_id)
            args.selected_identity = window
            operation.guard = cli.check_selected_window
        elif args.window_id is not None:
            args.selected_identity = cli.window_identity(args.window_id)
            operation.guard = cli.check_selected_window
        with cli.ActionLock(cli.ACTION_STATE_PATH) if args.uia_action else nullcontext():
            if args.uia_action:
                from .controller_runtime import check_recovery
                check_recovery(cli.ACTION_STATE_PATH.parent)
                if controller:
                    controller.verify_session(args.expected_session)
                cli.action_store().invalidate('direct UIA operation may change the interface')
            with cli.activity_scope(args, cli.ACTION_STATE_PATH.parent, operation, create=bool(args.uia_action)), cli.operation_session(operation):
                if args.wait_ms is not None:
                    operation.wait(args.wait_ms / 1000)
                else:
                    runner = Runner(args, operation, cli.ACTION_STATE_PATH.parent, window, result, deadline, cli=cli)
                    try:
                        runner.run()
                    finally:
                        runner.close()
                if args.screenshot_after:
                    operation.wait(args.screenshot_delay_ms / 1000)
                    result['screenshot'] = cli.capture_requested_screenshot(args, args.window_id, after_action=True)
                    result['screenshot_delay_ms'] = args.screenshot_delay_ms
                if controller:
                    result = controller.prepare_result(result, cli.action_store(), cli.ACTION_STATE_PATH.parent,
                                                       action_completed=result.get('action_completed', False))
    except (Exception, KeyboardInterrupt) as exc:
        unknown = runner is not None and runner.pending
        aborted = isinstance(exc, (ActionAborted, KeyboardInterrupt))
        result.update(ok=False, error_code='ACTION_OUTCOME_UNKNOWN' if unknown else 'ABORTED' if aborted else getattr(exc, 'code', 'UIA_FAILED'),
                      error=str(exc) or 'interrupted', aborted=aborted, effect_status='not_verified',
                      action_completed=result.get('execution_status') == 'returned',
                      required_next_step=('read the application state; do not replay; resolve ' + UIA_RECOVERY_NAME if unknown
                                          else 'read the control state; do not replay an already completed action'))
        if unknown:
            result.update(execution_status='unknown', changed=None)
    result['elapsed_ms'] = int((time.monotonic() - started) * 1000)
    return result
