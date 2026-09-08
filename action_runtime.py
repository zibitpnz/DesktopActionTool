"""Action verification, cancellation, and per-workspace process locking.

Windows input stays in type_text.py; this module's protocol is testable with
synthetic window contexts, clocks, and temporary state files.
"""
from contextlib import AbstractContextManager
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid


class ActionError(ValueError):
    def __init__(self, code, message, next_step="create a new target screenshot, move, then verify the cursor"):
        super().__init__(message)
        self.code = code
        self.next_step = next_step


class ActionAborted(Exception):
    def __init__(self, reason="escape"):
        super().__init__(reason)
        self.reason = reason


class Cancellation:
    def __init__(self, escape_pressed, enabled=True, clock=time.monotonic, sleep=time.sleep):
        self.escape_pressed = escape_pressed
        self.enabled = enabled
        self.clock = clock
        self.sleep = sleep
        self.completed = {}
        self.held = {}
        self.guard = None
        self.monitor = None
        self.target_guard = None
        self.input_guard = None
        self.actual_cursor = None

    def check(self):
        if self.enabled and self.escape_pressed():
            raise ActionAborted()
        if self.monitor is not None:
            self.monitor()
        if self.target_guard is not None:
            self.target_guard()
        if self.guard is not None:
            self.guard()

    def wait(self, seconds):
        deadline = self.clock() + max(0, seconds)
        while True:
            self.check()
            remaining = deadline - self.clock()
            if remaining <= 0:
                return
            self.sleep(min(remaining, 0.02))

    def count(self, key, amount=1):
        self.completed[key] = self.completed.get(key, 0) + amount


class ActionLock(AbstractContextManager):
    """Named mutex: no lock files and no changes to the target application."""
    def __init__(self, path):
        self.name = "Local\\DesktopActionTool-" + hashlib.sha256(
            str(Path(path).resolve()).casefold().encode("utf-8")
        ).hexdigest()
        self.handle = None
        self.abandoned = False

    def __enter__(self):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        self.api.CreateMutexW.restype = ctypes.c_void_p
        self.api.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        self.api.WaitForSingleObject.restype = ctypes.c_ulong
        self.api.ReleaseMutex.argtypes = (ctypes.c_void_p,)
        self.api.ReleaseMutex.restype = ctypes.c_bool
        self.api.CloseHandle.argtypes = (ctypes.c_void_p,)
        self.api.CloseHandle.restype = ctypes.c_bool
        self.handle = self.api.CreateMutexW(None, False, self.name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        status = self.api.WaitForSingleObject(self.handle, 0)
        if status not in (0, 0x80):
            self.api.CloseHandle(self.handle)
            self.handle = None
            if status == 0x102:
                raise ActionError("BUSY", "another action is running for this tool", "wait for the current action to finish")
            raise ctypes.WinError(ctypes.get_last_error())
        self.abandoned = status == 0x80
        return self

    def __exit__(self, *exc):
        if self.handle:
            try:
                self.api.ReleaseMutex(self.handle)
            finally:
                self.api.CloseHandle(self.handle)
                self.handle = None


class ActionStore:
    VERSION = 2
    STAGES = {"target_previewed", "moved_unverified", "cursor_verified", "action_in_progress", "invalidated"}

    def __init__(self, path, context, cursor, foreground, window_at_point,
                 ttl=120, tolerance=0, wall_clock=time.time, clock=time.monotonic):
        self.path = Path(path)
        self.context = context
        self.cursor = cursor
        self.foreground = foreground
        self.window_at_point = window_at_point
        self.ttl = ttl
        self.tolerance = tolerance
        self.wall_clock = wall_clock
        self.clock = clock

    def read(self):
        try:
            with self.path.open(encoding="utf-8") as stream:
                state = json.load(stream)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise ActionError("STATE_CORRUPT", "cannot read verification state") from exc
        if not isinstance(state, dict) or state.get("version") != self.VERSION:
            raise ActionError("STATE_VERSION", "verification state requires a new target screenshot")
        if state.get("stage") not in self.STAGES:
            raise ActionError("STATE_CORRUPT", "unknown verification stage")
        return state

    def write(self, state):
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=True, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def summary(self):
        try:
            state = self.read()
        except ActionError as exc:
            return {"state_path": str(self.path), "valid": False,
                    "error_code": exc.code, "required_next_step": exc.next_step,
                    "pending_click_requires_cursor_screenshot": True,
                    "mouse_move_allowed_by_target_screenshot": False}
        stage = state.get("stage")
        return {"state_path": str(self.path), "version": state.get("version"),
                "stage": stage, "action_id": state.get("action_id"),
                "pending_click_requires_cursor_screenshot": stage != "cursor_verified",
                "mouse_move_allowed_by_target_screenshot": stage == "target_previewed",
                "verified_mouse_target": state.get("target"),
                "last_mouse_move": state.get("last_mouse_move"),
                "verification": state.get("verification"),
                "reason": state.get("reason"),
                "required_next_step": "create a target screenshot, move, verify cursor, then click"}

    def invalidate(self, reason):
        self.write({"version": self.VERSION, "stage": "invalidated", "reason": reason})

    def preview(self, window_id, screen_point, metadata, expected_context=None):
        point = self._point(screen_point)
        context = self.context(window_id)
        if expected_context is not None and expected_context != context:
            raise ActionError("WINDOW_CHANGED", "window changed after target capture")
        rect = context["rect"]
        if not (rect["left"] <= point["x"] < rect["right"] and rect["top"] <= point["y"] < rect["bottom"]):
            raise ActionError("TARGET_OUTSIDE", "target is outside the selected window")
        if metadata.get("drag_destination") is not None:
            self._check_drag_destination(metadata["drag_destination"], point, context)
        state = {"version": self.VERSION, "stage": "target_previewed",
                 "action_id": uuid.uuid4().hex, "context": context,
                 "created_at": self.wall_clock(), "created_monotonic": self.clock(),
                 "ttl_seconds": self.ttl, "tolerance_px": self.tolerance,
                 "target": {**metadata, "window_id": window_id, "screen_target": point}}
        self.write(state)
        return self.summary()

    @staticmethod
    def _point(point):
        if not isinstance(point, dict) or any(type(point.get(key)) is not int for key in ("x", "y")):
            raise ActionError("STATE_CORRUPT", "invalid point in verification state")
        return {"x": point["x"], "y": point["y"]}

    def _same_point(self, first, second, tolerance=0):
        first, second = self._point(first), self._point(second)
        return all(abs(first[key] - second[key]) <= tolerance for key in ("x", "y"))

    def _tolerance(self, state):
        return min(int(state["tolerance_px"]), self.tolerance)

    def _validate(self, stage):
        state = self.read()
        if not state:
            raise ActionError("STATE_REQUIRED", "action requires a verified target and cursor screenshot")
        if state["stage"] != stage:
            raise ActionError("VERIFICATION_REQUIRED", "action is not ready: " + state["stage"])
        try:
            wall_elapsed = self.wall_clock() - state["created_at"]
            elapsed = self.clock() - state["created_monotonic"]
            ttl = min(float(state["ttl_seconds"]), self.ttl)
            tolerance = min(int(state["tolerance_px"]), self.tolerance)
            if not all(math.isfinite(value) for value in (elapsed, wall_elapsed, ttl)) or ttl <= 0 or tolerance < 0:
                raise ValueError("invalid limits")
            if elapsed < 0 or wall_elapsed < 0 or abs(elapsed - wall_elapsed) > 2:
                raise ActionError("CLOCK_CHANGED", "verification clock changed; repeat the screenshot")
            if elapsed > ttl:
                raise ActionError("TARGET_EXPIRED", "target verification expired")
            window_id = state["context"]["window_id"]
            if not isinstance(state["action_id"], str) or not state["action_id"]:
                raise ValueError("missing action identity")
            if state["context"] != self.context(window_id):
                raise ActionError("WINDOW_CHANGED", "target window identity or geometry changed")
            self._point(state["target"]["screen_target"])
            if stage == "cursor_verified":
                if state["verification"]["window_id"] != window_id or not state.get("movement_complete"):
                    raise ValueError("incomplete cursor verification")
                if not self._same_point(state["verification"]["screen_cursor"], state["target"]["screen_target"], tolerance):
                    raise ValueError("cursor verification differs from target")
        except ActionError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ActionError("STATE_CORRUPT", "incomplete verification state") from exc
        return state

    def check_move(self, point, window_id=None):
        state = self._validate("target_previewed")
        if window_id is not None and window_id != state["context"]["window_id"]:
            raise ActionError("WINDOW_CHANGED", "requested window differs from verified window")
        if not self._same_point(point, state["target"]["screen_target"]):
            raise ActionError("TARGET_MISMATCH", "requested destination does not match verified target")
        return state

    def start_move(self, point, window_id=None):
        state = self.check_move(point, window_id)
        state.update(stage="moved_unverified", movement_complete=False,
                     last_mouse_move={"window_id": state["context"]["window_id"], "screen_moved_to": point})
        self.write(state)

    def finish_move(self):
        state = self._validate("moved_unverified")
        if not self._same_point(self.cursor(), state["target"]["screen_target"], self._tolerance(state)):
            raise ActionError("CURSOR_MISMATCH", "cursor did not reach the verified target")
        state["movement_complete"] = True
        self.write(state)

    def verify_cursor(self, window_id, point, metadata):
        state = self.read()
        if state.get("stage") != "moved_unverified":
            return self.summary()
        if state.get("context", {}).get("window_id") != window_id:
            return {**self.summary(), "cleared": False, "reason": "screenshot belongs to another window"}
        state = self._validate("moved_unverified")
        if not state.get("movement_complete"):
            raise ActionError("MOVEMENT_INCOMPLETE", "mouse movement was interrupted")
        target = state["target"]["screen_target"]
        if not self._same_point(point, target, self._tolerance(state)) or not self._same_point(self.cursor(), point, self._tolerance(state)):
            raise ActionError("CURSOR_MISMATCH", "screenshot cursor differs from verified target")
        state.update(stage="cursor_verified", verification={**metadata, "window_id": window_id, "screen_cursor": point})
        self.write(state)
        return {**self.summary(), "cleared": True}

    def check_click(self, window_id=None):
        state = self._validate("cursor_verified")
        return self._check_click_context(state, window_id)

    def check_consumed_click(self, window_id=None):
        state = self._validate("action_in_progress")
        return self._check_click_context(state, window_id)

    def _check_click_context(self, state, window_id):
        target_window = state["context"]["window_id"]
        if window_id is not None and window_id != target_window:
            raise ActionError("WINDOW_CHANGED", "click window differs from verified window")
        if self.foreground() != target_window:
            raise ActionError("FOCUS_CHANGED", "verified window is no longer active")
        point = self.cursor()
        if not self._same_point(point, state["verification"]["screen_cursor"], self._tolerance(state)):
            raise ActionError("CURSOR_MISMATCH", "cursor moved since screenshot verification")
        if self.window_at_point(point) != target_window:
            raise ActionError("TARGET_OCCLUDED", "another window covers the verified target")
        return state

    def consume_click(self, window_id=None):
        state = self.check_click(window_id)
        state["stage"] = "action_in_progress"
        self.write(state)

    def _check_drag_destination(self, destination, start, context):
        point = self._point(destination)
        rect = context["rect"]
        if not (rect["left"] <= point["x"] < rect["right"] and rect["top"] <= point["y"] < rect["bottom"]):
            raise ActionError("TARGET_OUTSIDE", "drag destination is outside the selected window")
        if point == start:
            raise ActionError("EMPTY_DRAG", "drag start and destination must differ")

    def check_drag(self, destination, window_id=None):
        state = self.check_click(window_id)
        verified = state["target"].get("drag_destination")
        if verified is None or not self._same_point(destination, verified):
            raise ActionError("DRAG_TARGET_REQUIRED", "drag requires a screenshot of both start and destination",
                              "capture --screenshot-target X Y --screenshot-drag-target X Y, move to start and verify cursor")
        self._check_drag_destination(verified, state["target"]["screen_target"], state["context"])
        self.check_drag_context(state, destination)
        return state

    def check_drag_context(self, state, destination):
        window_id = state["context"]["window_id"]
        if self.context(window_id) != state["context"] or self.foreground() != window_id:
            raise ActionError("WINDOW_CHANGED", "window geometry or focus changed during drag")
        if self.window_at_point(destination) != window_id:
            raise ActionError("TARGET_OCCLUDED", "drag destination is covered by another window")

    def consume_drag(self, destination, window_id=None):
        state = self.check_drag(destination, window_id)
        state["stage"] = "action_in_progress"
        self.write(state)
        return state
