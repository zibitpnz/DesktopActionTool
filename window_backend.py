"""Window identity, physical coordinates, capture and window operations."""
from __future__ import annotations
import ctypes
import time
from pathlib import Path
from action_runtime import ActionError
from operation_runtime import interruptible_sleep, check_cancelled
from configuration import (
    SW_SHOW,
    SW_MINIMIZE,
    SW_RESTORE,
    SWP_NOMOVE,
    SWP_NOZORDER,
    PROCESS_QUERY_LIMITED_INFORMATION,
    BI_RGB,
    DIB_RGB_COLORS,
    SRCCOPY,
    CAPTUREBLT,
)

from win32_api import (
    UINT,
    DWORD,
    POINT,
    RECT,
    BITMAPINFOHEADER,
    BITMAPINFO,
    kernel32,
    gdi32,
    EnumWindowsProc,
    hwnd,
)
import win32_api as api



def initialize_dpi_awareness() -> None:
    """Use physical pixels, including when Python's manifest set a default."""
    try:
        api.user32.SetProcessDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
        api.user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        api.user32.SetThreadDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
        api.user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        api.user32.GetThreadDpiAwarenessContext.argtypes = ()
        api.user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        api.user32.AreDpiAwarenessContextsEqual.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        api.user32.AreDpiAwarenessContextsEqual.restype = ctypes.c_bool
        per_monitor_v2 = ctypes.c_void_p(-4)
        api.user32.SetProcessDpiAwarenessContext(per_monitor_v2)
        if not api.user32.SetThreadDpiAwarenessContext(per_monitor_v2):
            raise ActionError("DPI_UNSUPPORTED", "cannot enable per-monitor-v2 coordinates", "use Windows 10 version 1703 or newer")
        if not api.user32.AreDpiAwarenessContextsEqual(api.user32.GetThreadDpiAwarenessContext(), per_monitor_v2):
            raise ActionError("DPI_UNSUPPORTED", "unexpected thread DPI awareness")
    except AttributeError as exc:
        raise ActionError("DPI_UNSUPPORTED", "Windows DPI APIs are unavailable", "use Windows 10 version 1703 or newer") from exc


def window_dpi_metadata(window_id: int) -> dict[str, object]:
    api.user32.GetDpiForWindow.argtypes = (ctypes.c_void_p,)
    api.user32.GetDpiForWindow.restype = UINT
    api.user32.MonitorFromWindow.argtypes = (ctypes.c_void_p, DWORD)
    api.user32.MonitorFromWindow.restype = ctypes.c_void_p
    dpi = api.user32.GetDpiForWindow(hwnd(window_id))
    monitor = api.user32.MonitorFromWindow(hwnd(window_id), 2)
    if not dpi or not monitor:
        raise ActionError("WINDOW_CHANGED", "cannot obtain window DPI/monitor")
    return {"awareness": "per-monitor-v2", "coordinate_units": "physical-pixels",
            "window_dpi": int(dpi), "monitor_id": hex(int(monitor))}


def window_identity(window_id: int) -> dict[str, int]:
    if not api.user32.IsWindow(hwnd(window_id)):
        raise ActionError("WINDOW_CHANGED", "selected window no longer exists")
    process_id = get_process_id(window_id)
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    times = [ctypes.c_ulonglong() for _ in range(4)]
    try:
        if not kernel32.GetProcessTimes(process, *(ctypes.byref(value) for value in times)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(process)
    return {"window_id": window_id, "process_id": process_id, "process_created": times[0].value}


def verification_window_context(window_id: int) -> dict[str, object]:
    if not api.user32.IsWindowVisible(hwnd(window_id)):
        raise ActionError("WINDOW_CHANGED", "verified window is hidden or closed")
    dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
    dwm.DwmGetWindowAttribute.argtypes = (ctypes.c_void_p, DWORD, ctypes.c_void_p, DWORD)
    dwm.DwmGetWindowAttribute.restype = ctypes.c_long
    cloaked = DWORD()
    if dwm.DwmGetWindowAttribute(hwnd(window_id), 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) < 0:
        raise ActionError("WINDOW_CHANGED", "cannot verify window visibility")
    if cloaked.value:
        raise ActionError("WINDOW_CHANGED", "verified window is cloaked")
    if api.user32.IsIconic(hwnd(window_id)):
        raise ActionError("WINDOW_CHANGED", "verified window is minimized")
    return {**window_identity(window_id), "rect": get_window_rect(window_id),
            "client_origin": client_to_screen_point(window_id, 0, 0),
            "client_rect": get_client_rect(window_id), "dpi": window_dpi_metadata(window_id)}


def root_window_at_point(point: dict[str, int]) -> int:
    handle = api.user32.WindowFromPoint(POINT(point["x"], point["y"]))
    return int(api.user32.GetAncestor(handle, 2) or handle or 0)


def get_window_text(window_id: int) -> str:
    handle = hwnd(window_id)
    length = api.user32.GetWindowTextLengthW(handle)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    api.user32.GetWindowTextW(handle, buffer, len(buffer))
    return buffer.value


def get_window_class(window_id: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    api.user32.GetClassNameW(hwnd(window_id), buffer, len(buffer))
    return buffer.value


def get_process_id(window_id: int) -> int:
    process_id = DWORD()
    api.user32.GetWindowThreadProcessId(hwnd(window_id), ctypes.byref(process_id))
    return int(process_id.value)


def get_process_name(process_id: int) -> str:
    process_handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, process_id
    )
    if not process_handle:
        return ""

    try:
        size = DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            process_handle, 0, buffer, ctypes.byref(size)
        ):
            return ""
        return Path(buffer.value).name
    finally:
        kernel32.CloseHandle(process_handle)


def get_window_rect(window_id: int) -> dict[str, int]:
    rect = RECT()
    if not api.user32.GetWindowRect(hwnd(window_id), ctypes.byref(rect)):
        error = ctypes.get_last_error()
        raise OSError(error, f"GetWindowRect failed for window {window_id}")

    left = int(rect.left)
    top = int(rect.top)
    right = int(rect.right)
    bottom = int(rect.bottom)
    return {
        "left": left,
        "top": top,
        "right": right,
        "bottom": bottom,
        "x": left,
        "y": top,
        "width": right - left,
        "height": bottom - top,
    }


def get_client_rect(window_id: int) -> dict[str, int]:
    rect = RECT()
    if not api.user32.GetClientRect(hwnd(window_id), ctypes.byref(rect)):
        error = ctypes.get_last_error()
        raise OSError(error, f"GetClientRect failed for window {window_id}")

    left = int(rect.left)
    top = int(rect.top)
    right = int(rect.right)
    bottom = int(rect.bottom)
    return {
        "left": left,
        "top": top,
        "right": right,
        "bottom": bottom,
        "x": left,
        "y": top,
        "width": right - left,
        "height": bottom - top,
    }


def client_to_screen_point(window_id: int, x: int, y: int) -> dict[str, int]:
    point = POINT(x, y)
    if not api.user32.ClientToScreen(hwnd(window_id), ctypes.byref(point)):
        error = ctypes.get_last_error()
        raise OSError(error, f"ClientToScreen failed for window {window_id}")
    return {"x": int(point.x), "y": int(point.y)}


def screen_to_client_point(window_id: int, x: int, y: int) -> dict[str, int]:
    point = POINT(x, y)
    if not api.user32.ScreenToClient(hwnd(window_id), ctypes.byref(point)):
        error = ctypes.get_last_error()
        raise OSError(error, f"ScreenToClient failed for window {window_id}")
    return {"x": int(point.x), "y": int(point.y)}


def window_to_screen_point(window_id: int, x: int, y: int) -> dict[str, int]:
    rect = get_window_rect(window_id)
    return {"x": rect["left"] + x, "y": rect["top"] + y}


def screen_to_window_point(window_id: int, x: int, y: int) -> dict[str, int]:
    rect = get_window_rect(window_id)
    return {"x": x - rect["left"], "y": y - rect["top"]}


def handle_int(value: object) -> int:
    return int(value.value if hasattr(value, "value") else value)


def point_to_screen(coord_origin: str, x: int, y: int, window_id: int | None) -> dict[str, int]:
    if coord_origin == "screen":
        return {"x": x, "y": y}
    if window_id is None:
        raise ValueError(f"--coord-origin {coord_origin} requires an active window or --window-id")
    if coord_origin == "window":
        return window_to_screen_point(window_id, x, y)
    if coord_origin == "client":
        return client_to_screen_point(window_id, x, y)
    raise ValueError(f"unsupported coordinate origin: {coord_origin}")


def point_from_screen(coord_origin: str, x: int, y: int, window_id: int | None) -> dict[str, int]:
    if coord_origin == "screen":
        return {"x": x, "y": y}
    if window_id is None:
        raise ValueError(f"--coord-origin {coord_origin} requires an active window or --window-id")
    if coord_origin == "window":
        return screen_to_window_point(window_id, x, y)
    if coord_origin == "client":
        return screen_to_client_point(window_id, x, y)
    raise ValueError(f"unsupported coordinate origin: {coord_origin}")


def window_coordinate_context(window_id: int) -> dict[str, object]:
    window = window_snapshot(window_id)
    rect = window["rect"]
    client_rect = get_client_rect(window_id)
    client_origin = client_to_screen_point(window_id, 0, 0)
    return {
        "id": window_id,
        "hex_id": hex(window_id),
        "title": window["title"],
        "class_name": window["class_name"],
        "rect": rect,
        "client_rect": client_rect,
        "client_origin": client_origin,
        "client_offset": {
            "x": client_origin["x"] - rect["left"],
            "y": client_origin["y"] - rect["top"],
        },
        "process_id": window["process_id"],
        "process_name": window["process_name"],
        "is_minimized": window["is_minimized"],
        "dpi": window_dpi_metadata(window_id),
    }


def window_snapshot(window_id: int) -> dict[str, object]:
    process_id = get_process_id(window_id)
    return {
        "id": window_id,
        "hex_id": hex(window_id),
        "title": get_window_text(window_id),
        "class_name": get_window_class(window_id),
        "rect": get_window_rect(window_id),
        "process_id": process_id,
        "process_name": get_process_name(process_id),
        "is_minimized": bool(api.user32.IsIconic(hwnd(window_id))),
    }


def list_windows() -> list[dict[str, object]]:
    windows: list[dict[str, object]] = []

    def enum_window(window_handle: int, _lparam: int) -> bool:
        window_id = int(window_handle)
        if not api.user32.IsWindowVisible(hwnd(window_id)):
            return True

        title = get_window_text(window_id).strip()
        if not title:
            return True

        windows.append(window_snapshot(window_id))
        return True

    callback = EnumWindowsProc(enum_window)
    if not api.user32.EnumWindows(callback, None):
        error = ctypes.get_last_error()
        raise OSError(error, "EnumWindows failed")

    windows.sort(key=lambda item: (str(item["process_name"]).lower(), str(item["title"]).lower()))
    return windows


def get_active_window() -> dict[str, object]:
    window_id = int(api.user32.GetForegroundWindow() or 0)
    if not window_id:
        return {
            "ok": False,
            "mode": "active-window",
            "error": "no active foreground window",
        }

    return {
        "ok": True,
        "mode": "active-window",
        "window": window_snapshot(window_id),
    }


def active_window_id() -> int:
    window_id = int(api.user32.GetForegroundWindow() or 0)
    if not window_id:
        raise ValueError("no active foreground window")
    return window_id


def resolve_coordinate_window_id(coord_origin: str, window_id: int | None) -> int | None:
    if coord_origin == "screen":
        return None
    return window_id if window_id is not None else active_window_id()


def resize_window(window_id: int, width: int, height: int) -> dict[str, object]:
    started = time.perf_counter()
    handle = hwnd(window_id)
    if not api.user32.IsWindow(handle):
        raise ValueError(f"window not found: {window_id}")

    before = window_snapshot(window_id)
    check_cancelled()
    if not api.user32.SetWindowPos(
        handle,
        None,
        0,
        0,
        width,
        height,
        SWP_NOMOVE | SWP_NOZORDER,
    ):
        error = ctypes.get_last_error()
        raise OSError(error, f"SetWindowPos failed for window {window_id}")

    interruptible_sleep(0.1)
    after = window_snapshot(window_id)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {
        "ok": True,
        "mode": "resize-window",
        "window_id": window_id,
        "requested_width": width,
        "requested_height": height,
        "before": before,
        "after": after,
        "elapsed_ms": elapsed_ms,
    }


def set_window_rect(window_id: int, x: int, y: int, width: int, height: int) -> dict[str, object]:
    started = time.perf_counter()
    handle = hwnd(window_id)
    if not api.user32.IsWindow(handle):
        raise ValueError(f"window not found: {window_id}")

    before = window_snapshot(window_id)
    check_cancelled()
    if not api.user32.SetWindowPos(
        handle,
        None,
        x,
        y,
        width,
        height,
        SWP_NOZORDER,
    ):
        error = ctypes.get_last_error()
        raise OSError(error, f"SetWindowPos failed for window {window_id}")

    interruptible_sleep(0.1)
    after = window_snapshot(window_id)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {
        "ok": True,
        "mode": "set-window-rect",
        "window_id": window_id,
        "requested_rect": {
            "x": x,
            "y": y,
            "width": width,
            "height": height,
        },
        "before": before,
        "after": after,
        "elapsed_ms": elapsed_ms,
    }


def minimize_window(window_id: int) -> dict[str, object]:
    started = time.perf_counter()
    handle = hwnd(window_id)
    if not api.user32.IsWindow(handle):
        raise ValueError(f"window not found: {window_id}")

    before = window_snapshot(window_id)
    check_cancelled()
    if not api.user32.ShowWindow(handle, SW_MINIMIZE):
        error = ctypes.get_last_error()
        raise OSError(error, f"ShowWindow(SW_MINIMIZE) failed for window {window_id}")

    interruptible_sleep(0.1)
    after = window_snapshot(window_id)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {
        "ok": True,
        "mode": "minimize-window",
        "window_id": window_id,
        "before": before,
        "after": after,
        "elapsed_ms": elapsed_ms,
    }


def focus_window(window_id: int) -> dict[str, object]:
    started = time.perf_counter()
    handle = hwnd(window_id)
    if not api.user32.IsWindow(handle):
        raise ValueError(f"window not found: {window_id}")

    snapshot = window_snapshot(window_id)
    was_minimized = bool(snapshot["is_minimized"])
    check_cancelled()
    api.user32.ShowWindow(handle, SW_RESTORE if was_minimized else SW_SHOW)

    current_thread_id = kernel32.GetCurrentThreadId()
    target_thread_id = api.user32.GetWindowThreadProcessId(handle, None)
    foreground_handle = api.user32.GetForegroundWindow()
    foreground_thread_id = (
        api.user32.GetWindowThreadProcessId(foreground_handle, None)
        if foreground_handle
        else 0
    )

    attached_thread_ids: list[int] = []
    for thread_id in {int(target_thread_id), int(foreground_thread_id)}:
        if thread_id and thread_id != int(current_thread_id):
            if api.user32.AttachThreadInput(current_thread_id, thread_id, True):
                attached_thread_ids.append(thread_id)

    try:
        check_cancelled()
        api.user32.BringWindowToTop(handle)
        check_cancelled()
        set_foreground_ok = bool(api.user32.SetForegroundWindow(handle))
    finally:
        for thread_id in attached_thread_ids:
            api.user32.AttachThreadInput(current_thread_id, thread_id, False)

    interruptible_sleep(0.2)
    focused = int(api.user32.GetForegroundWindow() or 0) == window_id
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    return {
        "ok": focused,
        "mode": "focus",
        "window": snapshot,
        "was_minimized": was_minimized,
        "set_foreground_ok": set_foreground_ok,
        "focused": focused,
        "elapsed_ms": elapsed_ms,
    }


def capture_window_pixels(window_id: int) -> tuple[dict[str, int], bytes]:
    from activity_indicator import capture_without_frame
    with capture_without_frame():
        return _capture_window_pixels(window_id)


def _capture_window_pixels(window_id: int) -> tuple[dict[str, int], bytes]:
    rect = get_window_rect(window_id)
    width = rect["width"]
    height = rect["height"]
    if width <= 0 or height <= 0:
        raise ValueError(f"window has invalid size: {width}x{height}")
    if api.user32.IsIconic(hwnd(window_id)):
        raise ValueError("cannot screenshot a minimized window")

    screen_dc = api.user32.GetDC(None)
    if not screen_dc:
        error = ctypes.get_last_error()
        raise OSError(error, "GetDC failed")

    memory_dc = None
    bitmap = None
    old_object = None
    try:
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        if not memory_dc:
            error = ctypes.get_last_error()
            raise OSError(error, "CreateCompatibleDC failed")

        bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        if not bitmap:
            error = ctypes.get_last_error()
            raise OSError(error, "CreateCompatibleBitmap failed")

        old_object = gdi32.SelectObject(memory_dc, bitmap)
        if not old_object:
            error = ctypes.get_last_error()
            raise OSError(error, "SelectObject failed")

        if not gdi32.BitBlt(
            memory_dc,
            0,
            0,
            width,
            height,
            screen_dc,
            rect["left"],
            rect["top"],
            SRCCOPY | CAPTUREBLT,
        ):
            error = ctypes.get_last_error()
            raise OSError(error, "BitBlt failed")

        bitmap_info = BITMAPINFO()
        bitmap_info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bitmap_info.bmiHeader.biWidth = width
        bitmap_info.bmiHeader.biHeight = -height
        bitmap_info.bmiHeader.biPlanes = 1
        bitmap_info.bmiHeader.biBitCount = 32
        bitmap_info.bmiHeader.biCompression = BI_RGB

        buffer = (ctypes.c_ubyte * (width * height * 4))()
        lines = gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            height,
            buffer,
            ctypes.byref(bitmap_info),
            DIB_RGB_COLORS,
        )
        if lines != height:
            error = ctypes.get_last_error()
            raise OSError(error, f"GetDIBits returned {lines}/{height} lines")

        return rect, bytes(buffer)
    finally:
        if old_object and memory_dc:
            gdi32.SelectObject(memory_dc, old_object)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        api.user32.ReleaseDC(None, screen_dc)
