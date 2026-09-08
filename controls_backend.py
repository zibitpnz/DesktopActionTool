"""Read-only Win32 and UI Automation control enumeration."""
from __future__ import annotations
from action_runtime import ActionError
from window_backend import (
    get_window_text,
    get_window_class,
    get_window_rect,
    screen_to_client_point,
    handle_int,
)
from configuration import CONTROL_OVERLAY_MAX_DEFAULT

from win32_api import EnumChildWindowsProc, hwnd
import win32_api as api

from geometry import rect_from_points, intersect_rects


def enumerate_window_controls(
    window_id: int,
    include_hidden: bool = False,
    limit: int = CONTROL_OVERLAY_MAX_DEFAULT,
) -> dict[str, object]:
    if limit <= 0:
        raise ValueError("--controls-limit must be positive")

    window_rect = get_window_rect(window_id)
    window_bounds = rect_from_points(0, 0, window_rect["width"], window_rect["height"])
    controls: list[dict[str, object]] = []
    total_seen = 0

    def enum_child(child_handle: ctypes.c_void_p, _param: ctypes.c_void_p) -> bool:
        nonlocal total_seen
        if len(controls) >= limit:
            return False

        child_id = handle_int(child_handle)
        total_seen += 1
        visible = bool(api.user32.IsWindowVisible(hwnd(child_id)))
        if not include_hidden and not visible:
            return True

        try:
            screen_rect = get_window_rect(child_id)
        except OSError:
            return True

        if screen_rect["width"] <= 0 or screen_rect["height"] <= 0:
            return True

        window_rect_local = rect_from_points(
            screen_rect["left"] - window_rect["left"],
            screen_rect["top"] - window_rect["top"],
            screen_rect["right"] - window_rect["left"],
            screen_rect["bottom"] - window_rect["top"],
        )
        clipped_window_rect = intersect_rects(window_rect_local, window_bounds)
        if clipped_window_rect is None and not include_hidden:
            return True

        client_top_left = screen_to_client_point(window_id, screen_rect["left"], screen_rect["top"])
        client_bottom_right = screen_to_client_point(
            window_id,
            screen_rect["right"],
            screen_rect["bottom"],
        )
        client_rect = rect_from_points(
            client_top_left["x"],
            client_top_left["y"],
            client_bottom_right["x"],
            client_bottom_right["y"],
        )
        click_point_window = {
            "x": window_rect_local["left"] + window_rect_local["width"] // 2,
            "y": window_rect_local["top"] + window_rect_local["height"] // 2,
        }
        click_point_screen = {
            "x": screen_rect["left"] + screen_rect["width"] // 2,
            "y": screen_rect["top"] + screen_rect["height"] // 2,
        }
        controls.append(
            {
                "id": len(controls) + 1,
                "hwnd": child_id,
                "hex_hwnd": hex(child_id),
                "dlg_control_id": int(api.user32.GetDlgCtrlID(hwnd(child_id))),
                "name": get_window_text(child_id),
                "class_name": get_window_class(child_id),
                "is_visible": visible,
                "is_enabled": bool(api.user32.IsWindowEnabled(hwnd(child_id))),
                "screen_rect": screen_rect,
                "window_rect": window_rect_local,
                "client_rect": client_rect,
                "clipped_window_rect": clipped_window_rect,
                "click_point": click_point_window,
                "screen_click_point": click_point_screen,
            }
        )
        return True

    callback = EnumChildWindowsProc(enum_child)
    completed = bool(api.user32.EnumChildWindows(hwnd(window_id), callback, None))
    return {
        "source": "win32-child-windows",
        "include_hidden": include_hidden,
        "limit": limit,
        "completed": completed,
        "total_seen": total_seen,
        "returned_count": len(controls),
        "truncated": len(controls) >= limit and not completed,
        "controls": controls,
    }


def import_uiautomation():
    try:
        import uiautomation as auto  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ActionError("UIA_UNAVAILABLE",
            "UI Automation support requires the 'uiautomation' package; "
            "enable it with: uv sync --locked --extra uia; "
            "or install it with: python -m pip install -r requirements.txt",
            "run with uv run --locked --extra uia type_text.py; "
            "or install requirements.txt using the same Python interpreter"
        ) from exc
    auto.Logger.SetLogFile("")
    return auto


def uia_rect_to_dict(rect: object) -> dict[str, int]:
    left = int(getattr(rect, "left"))
    top = int(getattr(rect, "top"))
    right = int(getattr(rect, "right"))
    bottom = int(getattr(rect, "bottom"))
    return rect_from_points(left, top, right, bottom)


def safe_uia_value(control: object, attr_name: str, default: object = "") -> object:
    try:
        value = getattr(control, attr_name)
    except Exception:
        return default
    if callable(value):
        try:
            return value()
        except Exception:
            return default
    return value


def collect_uia_controls(
    window_id: int,
    include_offscreen: bool = False,
    limit: int = CONTROL_OVERLAY_MAX_DEFAULT,
    max_depth: int = 8,
    control_types: set[str] | None = None,
    name: str | None = None,
    automation_id: str | None = None,
) -> dict[str, object]:
    if limit <= 0:
        raise ValueError("--controls-limit must be positive")
    if max_depth < 0:
        raise ValueError("--uia-max-depth must be non-negative")

    auto = import_uiautomation()
    root = auto.ControlFromHandle(window_id)
    window_rect = get_window_rect(window_id)
    window_bounds = rect_from_points(0, 0, window_rect["width"], window_rect["height"])
    controls: list[dict[str, object]] = []
    total_seen = 0
    truncated = False

    def visit(control: object, depth: int) -> None:
        nonlocal total_seen, truncated
        if len(controls) >= limit:
            truncated = True
            return
        if depth > max_depth:
            return

        total_seen += 1
        try:
            screen_rect = uia_rect_to_dict(safe_uia_value(control, "BoundingRectangle"))
        except Exception:
            screen_rect = rect_from_points(0, 0, 0, 0)

        is_offscreen = bool(safe_uia_value(control, "IsOffscreen", False))
        if depth > 0 and screen_rect["width"] > 0 and screen_rect["height"] > 0:
            window_rect_local = rect_from_points(
                screen_rect["left"] - window_rect["left"],
                screen_rect["top"] - window_rect["top"],
                screen_rect["right"] - window_rect["left"],
                screen_rect["bottom"] - window_rect["top"],
            )
            clipped_window_rect = intersect_rects(window_rect_local, window_bounds)
            control_type = str(safe_uia_value(control, "ControlTypeName", ""))
            control_name = str(safe_uia_value(control, "Name", ""))
            control_automation_id = str(safe_uia_value(control, "AutomationId", ""))
            type_allowed = ((control_types is None or control_type in control_types)
                            and (name is None or control_name == name)
                            and (automation_id is None or control_automation_id == automation_id))
            if type_allowed and (
                include_offscreen or (not is_offscreen and clipped_window_rect is not None)
            ):
                client_top_left = screen_to_client_point(
                    window_id,
                    screen_rect["left"],
                    screen_rect["top"],
                )
                client_bottom_right = screen_to_client_point(
                    window_id,
                    screen_rect["right"],
                    screen_rect["bottom"],
                )
                client_rect = rect_from_points(
                    client_top_left["x"],
                    client_top_left["y"],
                    client_bottom_right["x"],
                    client_bottom_right["y"],
                )
                click_point_window = {
                    "x": window_rect_local["left"] + window_rect_local["width"] // 2,
                    "y": window_rect_local["top"] + window_rect_local["height"] // 2,
                }
                click_point_screen = {
                    "x": screen_rect["left"] + screen_rect["width"] // 2,
                    "y": screen_rect["top"] + screen_rect["height"] // 2,
                }
                controls.append(
                    {
                        "id": len(controls) + 1,
                        "name": control_name,
                        "control_type": control_type,
                        "automation_id": control_automation_id,
                        "class_name": str(safe_uia_value(control, "ClassName", "")),
                        "native_window_handle": int(
                            safe_uia_value(control, "NativeWindowHandle", 0) or 0
                        ),
                        "depth": depth,
                        "is_offscreen": is_offscreen,
                        "is_enabled": bool(safe_uia_value(control, "IsEnabled", False)),
                        "screen_rect": screen_rect,
                        "window_rect": window_rect_local,
                        "client_rect": client_rect,
                        "clipped_window_rect": clipped_window_rect,
                        "click_point": click_point_window,
                        "screen_click_point": click_point_screen,
                    }
                )
                if len(controls) >= limit:
                    truncated = True
                    return

        if depth >= max_depth:
            return
        try:
            children = control.GetChildren()
        except Exception:
            return
        for child in children:
            visit(child, depth + 1)
            if truncated:
                return

    visit(root, 0)
    return {
        "source": "windows-ui-automation",
        "include_offscreen": include_offscreen,
        "limit": limit,
        "max_depth": max_depth,
        "control_types": sorted(control_types) if control_types else None,
        "total_seen": total_seen,
        "returned_count": len(controls),
        "truncated": truncated,
        "controls": controls,
    }
