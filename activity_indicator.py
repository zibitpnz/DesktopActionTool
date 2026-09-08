"""Click-through activity frames and bounded, local Windows session control.

The worker owns all visible windows. IPC uses a private HWND, never broadcast
messages or a network port. Screenshots hide the frames in the owning process
and wait for its DWM updates before the caller reads screen pixels.
"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from action_runtime import ActionAborted, ActionError, ActionLock
from win32_api import BITMAPINFO, BITMAPINFOHEADER

STATE_NAME = ".activity_session.json"
CLASS_PREFIX = "DesktopActionToolActivity-"
MESSAGE = 0x8000 + 73
PING, PULSE, BEGIN, END, HIDE, SHOW, STOP = range(1, 8)
CURRENT_CLIENT = None


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", w.BYTE), ("BlendFlags", w.BYTE),
                ("SourceConstantAlpha", w.BYTE), ("AlphaFormat", w.BYTE)]


def frame_pixels(width, height, border, opacity, gradient):
    """Top-down premultiplied BGRA; corners use the nearest monitor edge.

    Build repeated rows, rather than walking every desktop pixel in Python.
    The interior stays fully transparent. A one-pixel border stays visible.
    """
    border = min(border, width // 2, height // 2)
    if border < 1:
        raise ValueError("activity frame requires a surface of at least 2x2 pixels")
    maximum = round(255 * opacity / 100)
    colors = []
    for distance in range(border):
        alpha = round(maximum * (1 - distance / (border - 1))) if gradient and border > 1 else maximum
        colors.append(bytes((0, round(152 * alpha / 255), alpha, alpha)))
    stride = width * 4
    pixels = bytearray(stride * height)
    for distance, color in enumerate(colors):
        row = b"".join(colors[:distance]) + color * (width - 2 * distance) + b"".join(reversed(colors[:distance]))
        for y in (distance, height - 1 - distance):
            pixels[y * stride:(y + 1) * stride] = row
    middle = b"".join(colors) + bytes((width - 2 * border) * 4) + b"".join(reversed(colors))
    pixels[border * stride:(height - border) * stride] = middle * (height - 2 * border)
    return pixels


class Native:
    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        signatures = {
            "IsWindow": ([w.HWND], w.BOOL),
            "GetClassNameW": ([w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowThreadProcessId": ([w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
            "SendMessageTimeoutW": ([w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT, ctypes.POINTER(ctypes.c_size_t)], w.LPARAM),
            "CreateWindowExW": ([w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p], w.HWND),
            "DefWindowProcW": ([w.HWND, w.UINT, w.WPARAM, w.LPARAM], w.LPARAM),
            "DestroyWindow": ([w.HWND], w.BOOL),
            "ShowWindow": ([w.HWND, ctypes.c_int], w.BOOL),
            "SetWindowPos": ([w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT], w.BOOL),
            "UpdateLayeredWindow": ([w.HWND, w.HDC, ctypes.POINTER(w.POINT), ctypes.POINTER(w.SIZE),
                                     w.HDC, ctypes.POINTER(w.POINT), w.DWORD,
                                     ctypes.POINTER(BLENDFUNCTION), w.DWORD], w.BOOL),
            "SetWindowRgn": ([w.HWND, w.HRGN, w.BOOL], ctypes.c_int),
            "SetWindowDisplayAffinity": ([w.HWND, w.DWORD], w.BOOL),
            "GetDC": ([w.HWND], w.HDC),
            "ReleaseDC": ([w.HWND, w.HDC], ctypes.c_int),
            "FillRect": ([w.HDC, ctypes.POINTER(w.RECT), w.HBRUSH], ctypes.c_int),
            "DrawTextW": ([w.HDC, w.LPCWSTR, ctypes.c_int, ctypes.POINTER(w.RECT), w.UINT], ctypes.c_int),
            "ValidateRect": ([w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
            "GetClientRect": ([w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
            "UpdateWindow": ([w.HWND], w.BOOL),
            "PeekMessageW": ([ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT], w.BOOL),
            "TranslateMessage": ([ctypes.POINTER(w.MSG)], w.BOOL),
            "DispatchMessageW": ([ctypes.POINTER(w.MSG)], w.LPARAM),
            "GetAsyncKeyState": ([ctypes.c_int], ctypes.c_short),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.user, name)
            function.argtypes, function.restype = args, result
        for name, args, result in (
            ("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            ("GetProcessTimes", [w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)], w.BOOL),
            ("WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD),
            ("CloseHandle", [w.HANDLE], w.BOOL),
            ("GetModuleHandleW", [w.LPCWSTR], w.HMODULE),
        ):
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        for name, args, result in (
            ("CreateSolidBrush", [w.DWORD], w.HBRUSH),
            ("CreateCompatibleDC", [w.HDC], w.HDC),
            ("DeleteDC", [w.HDC], w.BOOL),
            ("CreateDIBSection", [w.HDC, ctypes.POINTER(BITMAPINFO), w.UINT,
                                  ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD], w.HBITMAP),
            ("GdiFlush", [], w.BOOL),
            ("CreateRectRgn", [ctypes.c_int] * 4, w.HRGN),
            ("CombineRgn", [w.HRGN, w.HRGN, w.HRGN, ctypes.c_int], ctypes.c_int),
            ("DeleteObject", [w.HGDIOBJ], w.BOOL),
            ("GetStockObject", [ctypes.c_int], w.HGDIOBJ),
            ("SelectObject", [w.HDC, w.HGDIOBJ], w.HGDIOBJ),
            ("SetTextColor", [w.HDC, w.DWORD], w.DWORD),
            ("SetBkMode", [w.HDC, ctypes.c_int], ctypes.c_int),
        ):
            function = getattr(self.gdi, name)
            function.argtypes, function.restype = args, result
        self.dwm.DwmFlush.argtypes, self.dwm.DwmFlush.restype = [], ctypes.c_long

    def flush(self):
        if self.dwm.DwmFlush() < 0:
            raise ActionError("INDICATOR_CAPTURE_FAILED", "could not synchronize activity frame updates")


class ProcessWatch:
    def __init__(self, native, pid):
        self.api = native.kernel
        self.handle = self.api.OpenProcess(0x100000 | 0x1000, False, pid)
        if not self.handle:
            raise OSError("cannot monitor process " + str(pid))
        created, exited, kernel, user = (w.FILETIME() for _ in range(4))
        if not self.api.GetProcessTimes(self.handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            self.close()
            raise OSError("cannot read process creation time")
        self.created = (created.dwHighDateTime << 32) | created.dwLowDateTime

    def alive(self):
        return bool(self.handle) and self.api.WaitForSingleObject(self.handle, 0) == 0x102

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def read_state(path):
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ActionError("INDICATOR_STATE_INVALID", "cannot read activity session state") from exc
    if (not isinstance(state, dict) or state.get("version") != 1
            or not re.fullmatch(r"[0-9a-f]{32}", str(state.get("session_id", "")))
            or any(type(state.get(key)) is not int or state[key] <= 0 for key in ("hwnd", "pid", "created"))
            or type(state.get("persistent")) is not bool):
        raise ActionError("INDICATOR_STATE_INVALID", "invalid activity session endpoint")
    target = state.get("target")
    if target is not None and (not isinstance(target, dict) or any(
            type(target.get(key)) is not int or target[key] <= 0
            for key in ("window_id", "process_id", "process_created"))):
        raise ActionError("INDICATOR_STATE_INVALID", "invalid activity session target")
    return state


def endpoint_matches(native, state):
    if not native.user.IsWindow(state["hwnd"]):
        return False
    pid = w.DWORD()
    native.user.GetWindowThreadProcessId(state["hwnd"], ctypes.byref(pid))
    name = ctypes.create_unicode_buffer(128)
    native.user.GetClassNameW(state["hwnd"], name, len(name))
    return pid.value == state["pid"] and name.value == CLASS_PREFIX + state["session_id"]


class Client:
    def __init__(self, state, native=None):
        self.state = state
        self.native = native or Native()
        self.watch = ProcessWatch(self.native, state["pid"])
        self.next_check = 0.0
        try:
            if self.watch.created != state["created"] or not self.valid_window():
                raise ActionError("INDICATOR_LOST", "activity session endpoint no longer exists")
        except BaseException:
            self.close()
            raise

    def valid_window(self):
        return self.watch.alive() and endpoint_matches(self.native, self.state)

    def request(self, command, *, timeout=1000):
        if not self.valid_window():
            raise ActionAborted("activity-session-ended")
        result = ctypes.c_size_t()
        if not self.native.user.SendMessageTimeoutW(self.state["hwnd"], MESSAGE, command, os.getpid(),
                                                    0x0002 | 0x0020, timeout, ctypes.byref(result)):
            raise ActionError("INDICATOR_UNAVAILABLE", "activity frame did not respond")
        if not result.value:
            raise ActionError("INDICATOR_UNAVAILABLE", "activity frame rejected the request")
        return result.value

    def check(self):
        if not self.watch.alive() or not self.native.user.IsWindow(self.state["hwnd"]):
            raise ActionAborted("activity-session-ended")
        now = time.monotonic()
        if now >= self.next_check:
            self.request(PULSE, timeout=250)
            self.next_check = now + 0.25

    def close(self):
        self.watch.close()

    def stop(self):
        if not self.valid_window():
            return
        self.request(STOP)
        deadline = time.monotonic() + 2
        while self.watch.alive() and self.native.user.IsWindow(self.state["hwnd"]):
            if time.monotonic() >= deadline:
                raise ActionError("INDICATOR_UNAVAILABLE", "activity frame did not finish closing")
            time.sleep(0.02)

    @contextmanager
    def hidden(self):
        self.request(HIDE, timeout=2000)
        try:
            yield
        finally:
            self.request(SHOW, timeout=2000)


def find_client(path):
    state = read_state(path)
    if state is None:
        return None
    try:
        return Client(state)
    except ActionError as exc:
        if exc.code != "INDICATOR_LOST":
            raise
        return None  # Stale PID/HWND is never sent a message or terminated.
    except OSError as exc:
        if endpoint_matches(Native(), state):
            raise ActionError("INDICATOR_UNAVAILABLE", "cannot monitor the existing activity frame process") from exc
        return None


def describe(client, monitor_count):
    return {**{key: client.state.get(key) for key in
               ("session_id", "pid", "persistent", "timeout_s", "owner_pid")},
            "monitor_count": monitor_count, "target": client.state.get("target")}


def bound_session(directory):
    """Read a live session without renewing it or changing its target."""
    path = Path(directory) / STATE_NAME
    with ActionLock(path):
        client = find_client(path)
        if client is None:
            return None
        try:
            return dict(client.state) if client.state["persistent"] else None
        finally:
            client.close()


def start_worker(path, args, *, persistent, check_cancelled=lambda: None):
    path = Path(path).resolve()
    identifier = uuid.uuid4().hex
    payload = {"path": str(path), "session_id": identifier, "persistent": persistent,
               "timeout_s": args.session_timeout_s, "width": args.activity_frame_width_px,
               "gradient": args.activity_frame_gradient_enabled,
               "opacity": args.activity_frame_opacity_percent,
               "target": getattr(args, "selected_identity", None),
               "owner_pid": args.session_owner_pid if persistent else os.getpid(),
               "abort_on_escape": not args.no_abort_key}
    process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--worker"],
                               stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
                               env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        process.stdin.write(json.dumps(payload))
        process.stdin.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            check_cancelled()
            if process.poll() is not None:
                raise ActionError("INDICATOR_START_FAILED", process.stderr.read().strip() or "activity frame failed to start")
            state = read_state(path)
            if state and state["session_id"] == identifier:
                client = Client(state)
                try:
                    client.request(PING)
                except BaseException:
                    client.close()
                    raise
                return client
            time.sleep(0.02)
        raise ActionError("INDICATOR_START_FAILED", "activity frame startup timed out")
    except BaseException:
        if process.poll() is None:
            process.terminate()  # Only the worker spawned by this call.
        process.wait(timeout=5)
        raise
    finally:
        for stream in (process.stdin, process.stderr):
            if stream is not None:
                stream.close()


def session_command(args, directory, *, check_cancelled=lambda: None):
    path = Path(directory) / STATE_NAME
    mode = next(name for name in ("session_start", "session_end", "session_status", "session_heartbeat") if getattr(args, name))
    if args.dry_run:
        return {"ok": True, "mode": "session-dry-run", "planned_action": mode.replace("_", "-"),
                "timeout_s": args.session_timeout_s, "owner_pid": args.session_owner_pid, "dry_run": True,
                "target": getattr(args, "selected_identity", None),
                "executable": mode != "session_start" or getattr(args, "selected_identity", None) is not None}
    with ActionLock(path):
        client = find_client(path)
        try:
            controller = getattr(args, "controller", None)
            if controller is not None and mode in ("session_end", "session_heartbeat"):
                controller.verify_session(client.state["session_id"] if client and client.state["persistent"] else None)
                check_cancelled()
            if mode == "session_start":
                if client is not None:
                    raise ActionError("SESSION_EXISTS", "an activity session is already running", "end it before starting another")
                if getattr(args, "selected_identity", None) is None:
                    raise ActionError("TARGET_REQUIRED", "session start requires an explicit target window", "use --session-start --window-id ID")
                client = start_worker(path, args, persistent=True, check_cancelled=check_cancelled)
            elif mode == "session_end":
                if client:
                    client.stop()
                    state = read_state(path)
                    if state and state["session_id"] == client.state["session_id"]:
                        path.unlink(missing_ok=True)
                return {"ok": True, "mode": "session-end", "active": False}
            elif mode == "session_heartbeat":
                if client is None or not client.state["persistent"]:
                    raise ActionError("SESSION_REQUIRED", "start an activity session first", "use --session-start")
                client.request(PULSE)
            monitor_count = client.request(PING) if client is not None else 0
            return {"ok": True, "mode": mode.replace("_", "-"), "active": client is not None,
                    "session": describe(client, monitor_count) if client else None}
        finally:
            if client is not None:
                client.close()


@contextmanager
def activity_scope(args, directory, operation, *, create=False):
    global CURRENT_CLIENT
    path = Path(directory) / STATE_NAME
    client = None
    previous = CURRENT_CLIENT
    started = False
    try:
        with ActionLock(path):
            client = find_client(path)
            controller = getattr(args, "controller", None)
            if (controller is not None and not getattr(args, "requires_target", False) and client is not None
                    and client.state.get("session_id") != controller.payload.get("session_id")):
                client.close()
                client = None
            if (getattr(args, "requires_target", False)
                    or controller is not None and controller.payload.get("session_id") is not None):
                expected = getattr(args, "expected_session", None)
                actual = client.state["session_id"] if client and client.state["persistent"] else None
                if expected != actual:
                    raise ActionError("SESSION_CHANGED", "activity session changed before the action", "inspect --session-status and select the target again")
                if actual and client.state.get("target") != getattr(args, "selected_identity", None):
                    raise ActionError("SESSION_TARGET_MISMATCH", "action does not match the session target", "end the session before selecting a different window")
            if client is None and create and args.activity_frame:
                client = start_worker(path, args, persistent=False, check_cancelled=operation.check)
                started = True
            if client is not None:
                client.request(BEGIN)
        CURRENT_CLIENT = client
        operation.monitor = client.check if client else None
        if started:
            operation.wait(args.activity_frame_lead_ms / 1000)
        yield client
    except BaseException:
        if client is not None:
            try:
                client.stop()
            except (Exception, KeyboardInterrupt):
                pass
        raise
    else:
        if client is not None:
            if started:
                client.stop()
            else:
                client.request(END)
    finally:
        operation.monitor = None
        CURRENT_CLIENT = previous
        if client is not None:
            client.close()


@contextmanager
def capture_without_frame():
    client = CURRENT_CLIENT
    temporary = False
    if client is None:
        client = find_client(Path(__file__).resolve().with_name(STATE_NAME))
        temporary = client is not None
    if client is None:
        yield
        return
    try:
        if temporary:
            client.request(BEGIN)
        with client.hidden():
            yield
    finally:
        if temporary:
            try:
                client.request(END)
            finally:
                client.close()


class Lease:
    """Separate idle time from calls whose process is still running."""
    def __init__(self, timeout, clock=time.monotonic):
        self.timeout, self.clock = timeout, clock
        self.pulse()

    def pulse(self):
        self.deadline = self.clock() + self.timeout

    def expired(self, busy=False):
        return not busy and self.clock() >= self.deadline


class FrameWorker:
    def __init__(self, payload):
        self.payload = payload
        self.path = Path(payload["path"]).resolve()
        if self.path.name != STATE_NAME or not re.fullmatch(r"[0-9a-f]{32}", payload["session_id"]):
            raise ValueError("invalid activity worker path or identifier")
        if not 1 <= payload["timeout_s"] <= 86400 or not 1 <= payload["width"] <= 32:
            raise ValueError("invalid activity worker settings")
        if payload["gradient"] not in (0, 1) or not 1 <= payload["opacity"] <= 100:
            raise ValueError("invalid activity frame transparency settings")
        self.api = Native()
        self.owner = ProcessWatch(self.api, payload["owner_pid"]) if payload["owner_pid"] else None
        self.lease = Lease(payload["timeout_s"])
        self.clients = {}
        self.hidden = {}
        self.frames = []
        self.rectangles = []
        self.control = None
        self.brush = None
        self.running = True
        self.failure = None
        self.class_name = CLASS_PREFIX + payload["session_id"]
        self.instance = self.api.kernel.GetModuleHandleW(None)
        self.callback_type = ctypes.WINFUNCTYPE(w.LPARAM, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        self.callback = self.callback_type(self.safe_proc)

    def monitors(self):
        rectangles = []
        callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HMONITOR, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)
        def collect(monitor, dc, rect, data):
            rectangles.append((rect.contents.left, rect.contents.top, rect.contents.right, rect.contents.bottom))
            return True
        callback = callback_type(collect)
        self.api.user.EnumDisplayMonitors.argtypes = [w.HDC, ctypes.POINTER(w.RECT), callback_type, w.LPARAM]
        self.api.user.EnumDisplayMonitors.restype = w.BOOL
        if not self.api.user.EnumDisplayMonitors(None, None, callback, 0) or not rectangles:
            raise OSError("cannot enumerate monitors for activity frame")
        return sorted(rectangles)

    def register(self):
        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", w.UINT), ("lpfnWndProc", self.callback_type), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                        ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR),
                        ("lpszClassName", w.LPCWSTR)]
        self.brush = self.api.gdi.CreateSolidBrush(0x0098FF)
        cls = WNDCLASS(0, self.callback, 0, 0, self.instance, None, None, None, None, self.class_name)
        self.api.user.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
        self.api.user.RegisterClassW.restype = w.ATOM
        if not self.brush or not self.api.user.RegisterClassW(ctypes.byref(cls)):
            raise ctypes.WinError(ctypes.get_last_error())

    def region(self, handle, width, height):
        border = self.payload["width"]
        outer = self.api.gdi.CreateRectRgn(0, 0, width, height)
        inner = self.api.gdi.CreateRectRgn(border, border, width - border, height - border)
        badge_left = max(border, (width - 580) // 2)
        badge = self.api.gdi.CreateRectRgn(badge_left, border, min(width - border, badge_left + 580), border + 30)
        try:
            if not all((outer, inner, badge)):
                raise OSError("could not create activity frame region")
            if not self.api.gdi.CombineRgn(outer, outer, inner, 4) or not self.api.gdi.CombineRgn(outer, outer, badge, 2):
                raise OSError("could not combine activity frame regions")
            if not self.api.user.SetWindowRgn(handle, outer, False):
                raise OSError("could not assign activity frame region")
            outer = None  # Windows owns the region after a successful call.
        finally:
            for region in (outer, inner, badge):
                if region:
                    self.api.gdi.DeleteObject(region)

    def rebuild(self, rectangles):
        for handle in self.frames:
            self.api.user.DestroyWindow(handle)
        self.frames.clear()
        self.rectangles = rectangles
        for left, top, right, bottom in rectangles:
            # Disabled + transparent layered windows neither take focus nor
            # participate in WindowFromPoint, including at the border itself.
            handle = self.api.user.CreateWindowExW(0x08000000 | 0x00080000 | 0x20 | 0x80 | 0x08,
                self.class_name, "", 0x80000000 | 0x08000000, left, top, right - left, bottom - top,
                None, None, self.instance, None)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            self.frames.append(handle)
            self.region(handle, right - left, bottom - top)
            self.render(handle, left, top, right - left, bottom - top)
            if sys.getwindowsversion().build >= 19041:
                self.api.user.SetWindowDisplayAffinity(handle, 0x11)
            if not self.hidden:
                self.api.user.SetWindowPos(handle, -1, 0, 0, 0, 0, 0x01 | 0x02 | 0x10 | 0x40)
        self.api.flush()

    def show(self, visible):
        for handle in self.frames:
            self.api.user.ShowWindow(handle, 8 if visible else 0)  # SW_SHOWNA / SW_HIDE.
        self.api.flush()

    def render(self, handle, x, y, width, height):
        dc = self.api.gdi.CreateCompatibleDC(None)
        if not dc:
            raise ctypes.WinError(ctypes.get_last_error())
        bitmap = old_bitmap = old_font = None
        try:
            info = BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            info.bmiHeader.biWidth, info.bmiHeader.biHeight = width, -height
            info.bmiHeader.biPlanes, info.bmiHeader.biBitCount = 1, 32
            address = ctypes.c_void_p()
            bitmap = self.api.gdi.CreateDIBSection(dc, ctypes.byref(info), 0, ctypes.byref(address), None, 0)
            if not bitmap or not address.value:
                raise ctypes.WinError(ctypes.get_last_error())
            old_bitmap = self.api.gdi.SelectObject(dc, bitmap)
            if not old_bitmap:
                raise ctypes.WinError(ctypes.get_last_error())
            border = self.payload["width"]
            pixels = frame_pixels(width, height, border, self.payload["opacity"], self.payload["gradient"])
            buffer = (ctypes.c_ubyte * len(pixels)).from_buffer(pixels)
            ctypes.memmove(address, buffer, len(pixels))
            left = max(border, (width - 580) // 2)
            label = w.RECT(left, border, min(width - border, left + 580), min(height, border + 30))
            if not self.api.user.FillRect(dc, ctypes.byref(label), self.brush):
                raise OSError("could not draw activity warning badge")
            old_font = self.api.gdi.SelectObject(dc, self.api.gdi.GetStockObject(17))
            self.api.gdi.SetTextColor(dc, 0)
            self.api.gdi.SetBkMode(dc, 1)
            text = "Работает DesktopActionTool — не трогайте мышь и клавиатуру"
            if not self.api.user.DrawTextW(dc, text, -1, ctypes.byref(label), 0x01 | 0x04 | 0x20 | 0x8000):
                raise OSError("could not draw activity warning text")
            # GDI text does not preserve DIB alpha. Flush before direct memory
            # access and make just the badge opaque, preserving the frame fade.
            if not self.api.gdi.GdiFlush():
                raise OSError("could not synchronize activity warning drawing")
            surface = (ctypes.c_ubyte * len(pixels)).from_address(address.value)
            for row in range(label.top, label.bottom):
                start = (row * width + label.left) * 4 + 3
                end = (row * width + label.right) * 4
                surface[start:end:4] = bytes([255]) * (label.right - label.left)
            destination, origin, size = w.POINT(x, y), w.POINT(0, 0), w.SIZE(width, height)
            blend = BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, per-pixel alpha.
            if not self.api.user.UpdateLayeredWindow(handle, None, ctypes.byref(destination), ctypes.byref(size),
                                                     dc, ctypes.byref(origin), 0, ctypes.byref(blend), 2):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            if old_font:
                self.api.gdi.SelectObject(dc, old_font)
            if old_bitmap:
                self.api.gdi.SelectObject(dc, old_bitmap)
            if bitmap:
                self.api.gdi.DeleteObject(bitmap)
            self.api.gdi.DeleteDC(dc)

    def safe_proc(self, handle, message, wp, lp):
        try:
            return self.proc(handle, message, wp, lp)
        except BaseException as exc:
            self.failure = str(exc)
            self.running = False
            return 0

    def proc(self, handle, message, wp, lp):
        if message == MESSAGE and handle == self.control:
            command, pid = int(wp), int(lp)
            if command == PING:
                return len(self.frames)
            if command == STOP:
                self.running = False
                self.show(False)
                return 1
            if not self.running:
                return 0
            if command not in (PULSE, BEGIN, END, HIDE, SHOW):
                return 0
            if command in (PULSE, BEGIN, END):
                self.lease.pulse()
            if command == BEGIN and pid not in self.clients:
                self.clients[pid] = ProcessWatch(self.api, pid)
            elif command == END:
                watch = self.clients.pop(pid, None)
                if watch:
                    watch.close()
                self.hidden.pop(pid, None)
                if not self.hidden:
                    self.show(True)
            elif command == HIDE:
                if pid not in self.clients:
                    return 0
                depth, since = self.hidden.get(pid, (0, time.monotonic()))
                self.hidden[pid] = (depth + 1, since)
                self.show(False)
            elif command == SHOW:
                depth, since = self.hidden.get(pid, (0, 0))
                if depth > 1:
                    self.hidden[pid] = (depth - 1, since)
                else:
                    self.hidden.pop(pid, None)
                if not self.hidden:
                    self.show(True)
            return 1
        if message == 0x000F and handle in self.frames:
            self.api.user.ValidateRect(handle, None)  # UpdateLayeredWindow retains the surface.
            return 0
        if message == 0x0084:
            return -1  # HTTRANSPARENT, in addition to WS_DISABLED.
        if message == 0x0021:
            return 3  # MA_NOACTIVATE.
        if message == 0x0010:
            self.running = False
            return 0
        return self.api.user.DefWindowProcW(handle, message, wp, lp)

    def tick(self):
        if self.owner and not self.owner.alive():
            self.running = False
        if any(not watch.alive() for watch in self.clients.values()):
            self.running = False
        if self.lease.expired(busy=bool(self.clients)):
            self.running = False
        if any(time.monotonic() - since > 5 for depth, since in self.hidden.values()):
            self.running = False  # Never restore a frame during a stuck capture.
        if (self.payload["persistent"] and not self.clients and self.payload["abort_on_escape"]
                and self.api.user.GetAsyncKeyState(0x1B) & 0x8000):
            self.running = False

    def run(self):
        try:
            self.register()
            self.control = self.api.user.CreateWindowExW(0x08000000 | 0x80, self.class_name, "", 0,
                                                        0, 0, 0, 0, None, None, self.instance, None)
            if not self.control:
                raise ctypes.WinError(ctypes.get_last_error())
            self.rebuild(self.monitors())
            process = ProcessWatch(self.api, os.getpid())
            try:
                state = {"version": 1, "session_id": self.payload["session_id"], "hwnd": int(self.control),
                         "pid": os.getpid(), "created": process.created, "persistent": self.payload["persistent"],
                         "timeout_s": self.payload["timeout_s"], "owner_pid": self.payload["owner_pid"],
                         "target": self.payload.get("target"),
                         "monitor_count": len(self.frames)}
            finally:
                process.close()
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state), encoding="utf-8")
            os.replace(temporary, self.path)
            message = w.MSG()
            next_monitors = time.monotonic() + 1
            while self.running:
                while self.api.user.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    self.api.user.TranslateMessage(ctypes.byref(message))
                    self.api.user.DispatchMessageW(ctypes.byref(message))
                self.tick()
                if self.running and time.monotonic() >= next_monitors:
                    rectangles = self.monitors()
                    if rectangles != self.rectangles:
                        self.rebuild(rectangles)
                    next_monitors = time.monotonic() + 1
                time.sleep(0.02)
            if self.failure:
                raise RuntimeError(self.failure)
        finally:
            for handle in self.frames:
                self.api.user.DestroyWindow(handle)
            if self.control:
                self.api.user.DestroyWindow(self.control)
            if self.brush:
                self.api.gdi.DeleteObject(self.brush)
            if self.owner:
                self.owner.close()
            for watch in self.clients.values():
                watch.close()
            try:
                with ActionLock(self.path):
                    state = read_state(self.path)
                    if state and state["session_id"] == self.payload["session_id"]:
                        self.path.unlink()
            except (OSError, ActionError):
                pass


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit("Use type_text.py --session-start or --activity-frame")
    from window_backend import initialize_dpi_awareness
    initialize_dpi_awareness()
    FrameWorker(json.load(sys.stdin)).run()
