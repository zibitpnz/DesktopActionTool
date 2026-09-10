"""Cancellable cursor accompaniment for one prepared UIA action. Never clicks."""
import time

from .action_runtime import ActionError
from .input_backend import smooth_set_cursor_position
from .window_backend import monitor_layout


class CursorFollow:
    def __init__(self, args, cli, operation, result, geometry):
        self.args, self.cli, self.operation = args, cli, operation
        self.result, self.geometry = result, geometry
        self.expected_cursor = None
        self.saved = None
        self.result.update(point=geometry['pointer_point'], point_source=geometry['point_source'])

    def check(self):
        window = self.args.window_id
        self.cli.check_selected_window()
        try:
            foreground = self.cli.active_window_id()
        except ValueError:
            foreground = None  # Foreground can be temporarily absent while a window closes.
        if foreground != window:
            raise ActionError('FOCUS_CHANGED', 'cursor accompaniment requires the selected window in the foreground',
                              'inspect the target; focus it explicitly before a new action')
        if (self.cli.verification_window_context(window) != self.geometry['window_context']
                or monitor_layout() != self.geometry['monitors']):
            raise ActionError('WINDOW_CHANGED', 'window or display geometry changed during cursor accompaniment')
        if self.cli.root_window_at_point(self.geometry['pointer_point']) != window:
            raise ActionError('TARGET_OCCLUDED', 'the pointer destination is covered by another window')
        if self.expected_cursor is not None and self.cli.cursor_position() != self.expected_cursor:
            raise ActionError('CURSOR_MOVED', 'cursor moved outside the current accompaniment',
                              'inspect the control; do not replay an already completed action')

    def input_guard(self, kind, point):
        if kind != 'move':
            raise ActionError('INPUT_NOT_ALLOWED', 'cursor accompaniment never sends keys or mouse buttons')
        self.check()

    def moved(self, point):
        self.expected_cursor = point.copy()
        self.result['steps'] += 1

    def activate(self):
        if self.saved is not None:
            return
        self.check()
        self.saved = (self.operation.target_guard, self.operation.input_guard, self.operation.cursor_observer)
        self.operation.target_guard = self.check
        self.operation.input_guard = self.input_guard
        self.operation.cursor_observer = self.moved

    def deactivate(self):
        if self.saved is not None:
            self.operation.target_guard, self.operation.input_guard, self.operation.cursor_observer = self.saved
            self.saved = None

    def run(self):
        self.expected_cursor = self.cli.cursor_position().copy()
        self.activate()
        a, p = self.args, self.geometry['pointer_point']
        started = time.monotonic()
        try:
            if self.expected_cursor != p:
                smooth_set_cursor_position(self.expected_cursor['x'], self.expected_cursor['y'], p['x'], p['y'],
                    a.min_mouse_move_duration_ms, a.max_mouse_move_duration_ms, a.mouse_move_step_delay_ms,
                    a.mouse_move_jitter_px, a.mouse_move_jitter_stop_distance_px,
                    a.mouse_move_slow_zone_distance_px, a.mouse_move_slow_zone_min_speed_percent)
            self.check()
        finally:
            self.result['duration_ms'] = max(0, round((time.monotonic() - started) * 1000))
        paused = time.monotonic()
        try:
            self.operation.wait(a.uia_cursor_pause_ms / 1000)
        finally:
            self.result['pause_ms'] = max(0, round((time.monotonic() - paused) * 1000))
        self.check()
        self.result.update(status='completed', final_position=self.cli.cursor_position())

    def close(self):
        self.deactivate()
        if self.result['status'] != 'completed':
            self.result['status'] = 'partial' if self.result['steps'] else 'failed'
        try:
            self.result['final_position'] = self.cli.cursor_position()
        except Exception:
            self.result['final_position'] = None
