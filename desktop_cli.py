#!/usr/bin/env python
"""Command-line orchestration for DesktopActionTool."""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from datetime import datetime
from pathlib import Path

if "--dry-run" in sys.argv:
    sys.dont_write_bytecode = True

from action_runtime import ActionAborted, ActionError, ActionLock, ActionStore, Cancellation
from activity_indicator import activity_scope, session_command, bound_session
from selection import filter_windows, filter_controls, require_unique
from worker_client import run_uia_worker
from keymap import parse_hotkey

from operation_runtime import (
    check_cancelled,
    interruptible_sleep,
    count_completed,
    operation_session,
    current_operation,
)
from window_backend import (
    initialize_dpi_awareness,
    window_identity,
    verification_window_context,
    root_window_at_point,
    window_to_screen_point,
    screen_to_window_point,
    point_to_screen,
    point_from_screen,
    window_coordinate_context,
    window_snapshot,
    list_windows,
    get_active_window,
    active_window_id,
    resolve_coordinate_window_id,
    resize_window,
    set_window_rect,
    minimize_window,
    focus_window,
    capture_window_pixels,
)
from input_backend import (
    send_input,
    cursor_position,
    set_cursor_position,
    smooth_set_cursor_position,
    mouse_button_event,
    click_mouse,
    scroll_mouse,
    key_event,
    type_unicode_char,
    press_shift_enter,
    press_key,
    press_ctrl_a,
    is_escape_down,
    press_hotkey,
)
from controls_backend import enumerate_window_controls

from configuration import (
    KEYEVENTF_KEYUP,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    VK_RETURN,
    VK_CONTROL,
    VK_BACK,
    VK_DELETE,
    KEY_ACTION_PAUSE_SECONDS,
    DEFAULT_SETTINGS,
    SCREENSHOT_CROSSHAIR_ALPHA,
    SCREENSHOT_CROSSHAIR_HALF_WIDTH,
    CONTROL_OVERLAY_MAX_DEFAULT,
    SCREENSHOT_TARGET_ALPHA,
    SCREENSHOT_TARGET_HALF_WIDTH,
    SCREENSHOT_TARGET_RADIUS_PX,
    SCREENSHOT_RULER_MARGIN_PX,
    SCREENSHOT_RULER_STEP_PX,
    SCREENSHOT_RULER_MAJOR_STEP_PX,
)
from win32_api import UINT, hwnd
import win32_api as api
from screenshot_render import (
    write_png,
    draw_text,
    add_screenshot_ruler,
    overlay_detail_created_note,
    overlay_controls,
    overlay_selected_control,
    create_target_detail_screenshot,
    create_uia_highlight_control_screenshot,
    overlay_cursor_crosshair,
    create_cursor_detail_screenshot,
    overlay_target_crosshair,
)
from selection import select_uia_control


DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("settings.json")
ACTION_STATE_PATH = Path(__file__).resolve().with_name(".action_state.json")
CURRENT_ARGS = None


def action_store() -> ActionStore:
    return ActionStore(ACTION_STATE_PATH, verification_window_context, cursor_position,
                       active_window_id, root_window_at_point,
                       ttl=getattr(CURRENT_ARGS, "verification_ttl_seconds", 120),
                       tolerance=getattr(CURRENT_ARGS, "verification_tolerance_px", 0))


def parse_window_id(value: str) -> int:
    try:
        window_id = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("window id must be decimal or 0x hex") from exc
    if window_id <= 0:
        raise argparse.ArgumentTypeError("window id must be positive")
    return window_id


def parse_csv_set(value: str | None) -> set[str] | None:
    if value is None:
        return None
    items = {item.strip() for item in value.split(",") if item.strip()}
    return items or None


def enumerate_uia_controls(
    window_id, include_offscreen=False, limit=CONTROL_OVERLAY_MAX_DEFAULT,
    max_depth=8, control_types=None, *, name=None, automation_id=None, timeout=None,
):
    before = verification_window_context(window_id)
    result = run_uia_worker({"window_id": window_id, "include_offscreen": include_offscreen,
                            "limit": limit, "max_depth": max_depth,
                            "control_types": sorted(control_types) if control_types is not None else None,
                            "name": name, "automation_id": automation_id},
                           timeout if timeout is not None else getattr(CURRENT_ARGS, "timeout_s", 5.0),
                           check_cancelled)
    if before != verification_window_context(window_id):
        raise ActionError("WINDOW_CHANGED", "window changed during UI Automation query")
    return result


def cleanup_screenshot_folder(screenshots_dir: Path, keep_count: int) -> dict[str, object]:
    if keep_count == -1:
        screenshots = [
            path for path in screenshots_dir.glob("screenshot_*_window_*.png") if path.is_file()
        ]
        return {
            "enabled": False,
            "skipped": True,
            "reason": "screenshot_keep_count is -1",
            "keep_count": keep_count,
            "before_count": len(screenshots),
            "kept_count": len(screenshots),
            "deleted_count": 0,
            "failed_count": 0,
            "failed": [],
        }

    screenshots = sorted(
        (path for path in screenshots_dir.glob("screenshot_*_window_*.png") if path.is_file()),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    delete_candidates = screenshots[keep_count:]
    deleted_count = 0
    failed = []

    for path in delete_candidates:
        try:
            path.unlink()
            deleted_count += 1
        except OSError as exc:
            failed.append({"path": str(path), "error": str(exc)})

    return {
        "enabled": True,
        "skipped": False,
        "keep_count": keep_count,
        "before_count": len(screenshots),
        "kept_count": min(len(screenshots), keep_count),
        "deleted_count": deleted_count,
        "failed_count": len(failed),
        "failed": failed,
    }


def screenshot_window(
    window_id: int,
    cursor_crosshair: bool = False,
    screenshot_target: list[int] | None = None,
    target_coord_origin: str = "window",
    ruler_enabled: bool = False,
    ruler_margin_px: int = SCREENSHOT_RULER_MARGIN_PX,
    ruler_step_px: int = SCREENSHOT_RULER_STEP_PX,
    ruler_major_step_px: int = SCREENSHOT_RULER_MAJOR_STEP_PX,
    screenshot_keep_count: int = DEFAULT_SETTINGS["screenshot_keep_count"],
    paired_target_screenshot_enabled: bool = False,
    paired_target_crop_radius_px: int = DEFAULT_SETTINGS["paired_target_crop_radius_px"],
    paired_target_zoom: int = DEFAULT_SETTINGS["paired_target_zoom"],
    paired_target_ruler_step_px: int = DEFAULT_SETTINGS["paired_target_ruler_step_px"],
    paired_target_ruler_major_step_px: int = DEFAULT_SETTINGS["paired_target_ruler_major_step_px"],
    paired_cursor_screenshot_enabled: bool = False,
    paired_cursor_crop_radius_px: int = DEFAULT_SETTINGS["paired_cursor_crop_radius_px"],
    paired_cursor_zoom: int = DEFAULT_SETTINGS["paired_cursor_zoom"],
    paired_cursor_ruler_step_px: int = DEFAULT_SETTINGS["paired_cursor_ruler_step_px"],
    paired_cursor_ruler_major_step_px: int = DEFAULT_SETTINGS["paired_cursor_ruler_major_step_px"],
    controls_overlay: bool = False,
    controls_include_hidden: bool = False,
    controls_limit: int = CONTROL_OVERLAY_MAX_DEFAULT,
    uia_controls_overlay: bool = False,
    uia_include_offscreen: bool = False,
    uia_max_depth: int = 8,
    uia_control_types: set[str] | None = None,
    uia_highlight_control_id: int | None = None,
    uia_highlight_automation_id: str | None = None,
    uia_highlight_name: str | None = None,
    uia_highlight_control_screenshot_enabled: bool = True,
    uia_highlight_control_padding_px: int = DEFAULT_SETTINGS["uia_highlight_control_padding_px"],
    uia_highlight_control_zoom: int = DEFAULT_SETTINGS["uia_highlight_control_zoom"],
    screenshot_drag_target: list[int] | None = None,
) -> dict[str, object]:
    started = time.perf_counter()
    window = window_snapshot(window_id)
    capture_context = verification_window_context(window_id)
    uia_highlight_requested = any(value is not None for value in (
        uia_highlight_control_id, uia_highlight_automation_id, uia_highlight_name))
    native_controls = (enumerate_window_controls(window_id, controls_include_hidden, controls_limit)
                       if controls_overlay else None)
    automation_controls = (enumerate_uia_controls(window_id, uia_include_offscreen, controls_limit,
                                                  uia_max_depth, uia_control_types)
                           if uia_controls_overlay or uia_highlight_requested else None)
    if verification_window_context(window_id) != capture_context:
        raise ActionError("WINDOW_CHANGED", "window changed before capture")
    capture_cursor = cursor_position() if cursor_crosshair else None
    rect, pixels = capture_window_pixels(window_id)
    if capture_context["rect"] != rect:
        raise ActionError("WINDOW_CHANGED", "window changed while capturing screenshot")
    original_pixels = pixels
    output_width = rect["width"]
    output_height = rect["height"]
    ruler_metadata: dict[str, object] = {"enabled": False}
    target_metadata: dict[str, object] = {"enabled": False}
    drag_metadata: dict[str, object] = {"enabled": False}
    target_detail_metadata: dict[str, object] = {
        "enabled": bool(paired_target_screenshot_enabled),
        "created": False,
    }
    cursor_detail_metadata: dict[str, object] = {
        "enabled": bool(paired_cursor_screenshot_enabled and cursor_crosshair),
        "created": False,
    }
    target_detail_pixels: bytes | None = None
    target_detail_width = 0
    target_detail_height = 0
    cursor_detail_pixels: bytes | None = None
    cursor_detail_width = 0
    cursor_detail_height = 0
    uia_highlight_control_pixels: bytes | None = None
    uia_highlight_control_width = 0
    uia_highlight_control_height = 0
    action_state_metadata = action_state_summary()
    controls_metadata: dict[str, object] = {"enabled": False}
    uia_controls_metadata: dict[str, object] = {"enabled": False}
    uia_control_highlight_metadata: dict[str, object] = {"enabled": False}
    uia_highlight_requested = (
        uia_highlight_control_id is not None
        or uia_highlight_automation_id is not None
        or uia_highlight_name is not None
    )
    uia_highlight_control_screenshot_metadata: dict[str, object] = {
        "enabled": bool(uia_highlight_requested and uia_highlight_control_screenshot_enabled),
        "created": False,
    }
    screen_cursor = cursor_position()
    if capture_cursor is not None and capture_cursor != screen_cursor:
        raise ActionError("CURSOR_MISMATCH", "cursor moved while capturing screenshot")
    window_cursor = {
        "x": screen_cursor["x"] - rect["left"],
        "y": screen_cursor["y"] - rect["top"],
    }
    cursor_inside = (
        0 <= window_cursor["x"] < rect["width"]
        and 0 <= window_cursor["y"] < rect["height"]
    )

    if controls_overlay:
        controls_metadata = native_controls
        controls_metadata["enabled"] = True
        pixels = overlay_controls(
            pixels,
            rect["width"],
            rect["height"],
            controls_metadata["controls"],
        )

    if uia_controls_overlay:
        uia_controls_metadata = automation_controls
        uia_controls_metadata["enabled"] = True
        pixels = overlay_controls(
            pixels,
            rect["width"],
            rect["height"],
            uia_controls_metadata["controls"],
        )

    if uia_highlight_requested:
        highlight_controls_metadata = automation_controls
        selected_control, selector = select_uia_control(
            highlight_controls_metadata,
            uia_highlight_control_id,
            uia_highlight_automation_id,
            uia_highlight_name,
        )
        window_target_raw = selected_control.get("click_point")
        screen_target_raw = selected_control.get("screen_click_point")
        if not isinstance(window_target_raw, dict) or not isinstance(screen_target_raw, dict):
            raise ValueError("selected UIA control has no click_point metadata")
        window_target = {
            "x": int(window_target_raw["x"]),
            "y": int(window_target_raw["y"]),
        }
        screen_target = {
            "x": int(screen_target_raw["x"]),
            "y": int(screen_target_raw["y"]),
        }
        target_inside = (
            0 <= window_target["x"] < rect["width"]
            and 0 <= window_target["y"] < rect["height"]
        )
        pixels = overlay_selected_control(
            pixels,
            rect["width"],
            rect["height"],
            selected_control,
        )
        if target_inside:
            pixels = overlay_target_crosshair(
                pixels,
                rect["width"],
                rect["height"],
                window_target["x"],
                window_target["y"],
            )
            if paired_target_screenshot_enabled:
                target_detail_pixels, target_detail_metadata = create_target_detail_screenshot(
                    original_pixels,
                    rect["width"],
                    rect["height"],
                    window_target,
                    paired_target_crop_radius_px,
                    paired_target_zoom,
                    paired_target_ruler_step_px,
                    paired_target_ruler_major_step_px,
                )
                target_detail_width = int(target_detail_metadata["ruler"]["image_width"])
                target_detail_height = int(target_detail_metadata["ruler"]["image_height"])
            if uia_highlight_control_screenshot_enabled:
                (
                    uia_highlight_control_pixels,
                    uia_highlight_control_screenshot_metadata,
                ) = create_uia_highlight_control_screenshot(
                    original_pixels,
                    rect["width"],
                    rect["height"],
                    selected_control,
                    uia_highlight_control_padding_px,
                    uia_highlight_control_zoom,
                )
                uia_highlight_control_width = int(
                    uia_highlight_control_screenshot_metadata["image_width"]
                )
                uia_highlight_control_height = int(
                    uia_highlight_control_screenshot_metadata["image_height"]
                )

        uia_control_highlight_metadata = {
            "enabled": True,
            "source": "windows-ui-automation",
            "selector": selector,
            "include_offscreen": uia_include_offscreen,
            "limit": controls_limit,
            "max_depth": uia_max_depth,
            "control_types": sorted(uia_control_types) if uia_control_types else None,
            "total_seen": highlight_controls_metadata.get("total_seen"),
            "returned_count": highlight_controls_metadata.get("returned_count"),
            "truncated": highlight_controls_metadata.get("truncated"),
            "drawn": target_inside,
            "selected_control": selected_control,
            "target": window_target,
            "coord_origin": "window",
            "screen_target": screen_target,
            "window_target": window_target,
            "target_inside": target_inside,
            "highlight_color": "#46ff3c",
            "target_color": "#ff0000",
        }
        if not target_inside and uia_highlight_control_screenshot_enabled:
            uia_highlight_control_screenshot_metadata = {
                "enabled": True,
                "created": False,
                "reason": "selected UIA control click_point is outside the window",
            }
        if paired_target_screenshot_enabled and not target_inside:
            target_detail_metadata = {
                "enabled": True,
                "created": False,
                "reason": "selected UIA control click_point is outside the window",
            }

    if cursor_crosshair and cursor_inside:
        pixels = overlay_cursor_crosshair(
            pixels,
            rect["width"],
            rect["height"],
            window_cursor["x"],
            window_cursor["y"],
        )
        if paired_cursor_screenshot_enabled:
            cursor_detail_pixels, cursor_detail_metadata = create_cursor_detail_screenshot(
                original_pixels,
                rect["width"],
                rect["height"],
                window_cursor,
                paired_cursor_crop_radius_px,
                paired_cursor_zoom,
                paired_cursor_ruler_step_px,
                paired_cursor_ruler_major_step_px,
            )
            cursor_detail_width = int(cursor_detail_metadata["ruler"]["image_width"])
            cursor_detail_height = int(cursor_detail_metadata["ruler"]["image_height"])
    elif paired_cursor_screenshot_enabled and cursor_crosshair:
        cursor_detail_metadata = {
            "enabled": True,
            "created": False,
            "reason": "cursor is outside the window",
        }

    if screenshot_target is not None:
        target_x, target_y = screenshot_target
        screen_target = point_to_screen(target_coord_origin, target_x, target_y, window_id)
        window_target = {
            "x": screen_target["x"] - rect["left"],
            "y": screen_target["y"] - rect["top"],
        }
        target_inside = (
            0 <= window_target["x"] < rect["width"]
            and 0 <= window_target["y"] < rect["height"]
        )
        if not target_inside:
            raise ActionError("TARGET_OUTSIDE", "target is outside the selected window")
        if target_inside:
            pixels = overlay_target_crosshair(
                pixels,
                rect["width"],
                rect["height"],
                window_target["x"],
                window_target["y"],
            )
            if paired_target_screenshot_enabled:
                target_detail_pixels, target_detail_metadata = create_target_detail_screenshot(
                    original_pixels,
                    rect["width"],
                    rect["height"],
                    window_target,
                    paired_target_crop_radius_px,
                    paired_target_zoom,
                    paired_target_ruler_step_px,
                    paired_target_ruler_major_step_px,
                )
                target_detail_width = int(target_detail_metadata["ruler"]["image_width"])
                target_detail_height = int(target_detail_metadata["ruler"]["image_height"])
        target_metadata = {
            "enabled": True,
            "drawn": target_inside,
            "target": {"x": target_x, "y": target_y},
            "coord_origin": target_coord_origin,
            "screen_target": screen_target,
            "window_target": window_target,
            "target_inside": target_inside,
            "color": "#ff0000",
            "alpha": SCREENSHOT_TARGET_ALPHA,
            "radius_px": SCREENSHOT_TARGET_RADIUS_PX,
            "line_width": SCREENSHOT_TARGET_HALF_WIDTH * 2 + 1,
        }
        if paired_target_screenshot_enabled and not target_inside:
            target_detail_metadata = {
                "enabled": True,
                "created": False,
                "reason": "target is outside the window",
            }

    if screenshot_drag_target is not None:
        destination = point_to_screen(target_coord_origin, *screenshot_drag_target, window_id)
        action_store()._check_drag_destination(destination, target_metadata["screen_target"], capture_context)
        local = {"x": destination["x"] - rect["left"], "y": destination["y"] - rect["top"]}
        pixels = overlay_target_crosshair(pixels, rect["width"], rect["height"], local["x"], local["y"])
        # Numeric labels distinguish start (1) and destination (2) in the shared image.
        labelled = bytearray(pixels)
        for label, point in (("1", target_metadata["window_target"]), ("2", local)):
            draw_text(labelled, rect["width"], rect["height"], max(0, point["x"] - 12), max(0, point["y"] - 20), label, (255, 255, 255), 2)
        pixels = bytes(labelled)
        drag_metadata = {"enabled": True, "drawn": True, "screen_target": destination,
                         "window_target": local, "start_label": "1", "destination_label": "2"}

    if ruler_enabled:
        pixels, ruler_metadata = add_screenshot_ruler(
            pixels,
            rect["width"],
            rect["height"],
            ruler_margin_px,
            ruler_step_px,
            ruler_major_step_px,
        )
        output_width = int(ruler_metadata["image_width"])
        output_height = int(ruler_metadata["image_height"])

    if (
        target_detail_pixels is not None
        or cursor_detail_pixels is not None
        or uia_highlight_control_pixels is not None
    ):
        pixels = overlay_detail_created_note(pixels, output_width, output_height)

    check_cancelled()
    if verification_window_context(window_id) != capture_context:
        raise ActionError("WINDOW_CHANGED", "window changed while preparing screenshot")
    if cursor_crosshair and cursor_position() != screen_cursor:
        raise ActionError("CURSOR_MISMATCH", "cursor moved while preparing screenshot")
    screenshots_dir = Path(__file__).resolve().with_name("screenshots")
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = screenshots_dir / f"screenshot_{timestamp}_window_{window_id}.png"
    write_png(path, output_width, output_height, pixels)
    if target_detail_pixels is not None:
        detail_path = screenshots_dir / f"screenshot_{timestamp}_window_{window_id}_target_detail.png"
        write_png(detail_path, target_detail_width, target_detail_height, target_detail_pixels)
        target_detail_metadata["path"] = str(detail_path)
        target_detail_metadata["width"] = target_detail_width
        target_detail_metadata["height"] = target_detail_height
    if cursor_detail_pixels is not None:
        cursor_detail_path = screenshots_dir / f"screenshot_{timestamp}_window_{window_id}_cursor_detail.png"
        write_png(cursor_detail_path, cursor_detail_width, cursor_detail_height, cursor_detail_pixels)
        cursor_detail_metadata["path"] = str(cursor_detail_path)
        cursor_detail_metadata["width"] = cursor_detail_width
        cursor_detail_metadata["height"] = cursor_detail_height
    if uia_highlight_control_pixels is not None:
        uia_control_path = screenshots_dir / f"screenshot_{timestamp}_window_{window_id}_uia_control.png"
        write_png(
            uia_control_path,
            uia_highlight_control_width,
            uia_highlight_control_height,
            uia_highlight_control_pixels,
        )
        uia_highlight_control_screenshot_metadata["path"] = str(uia_control_path)
        uia_highlight_control_screenshot_metadata["width"] = uia_highlight_control_width
        uia_highlight_control_screenshot_metadata["height"] = uia_highlight_control_height
        uia_control_highlight_metadata["control_screenshot_path"] = str(uia_control_path)
        uia_control_highlight_metadata["control_screenshot"] = (
            uia_highlight_control_screenshot_metadata
        )
    if bool(target_metadata.get("drawn")):
        action_state_metadata = mark_mouse_target_verified(
            window_id,
            str(target_metadata["coord_origin"]),
            int(target_metadata["target"]["x"]),
            int(target_metadata["target"]["y"]),
            target_metadata["screen_target"],
            target_metadata["window_target"],
            path,
            target_detail_metadata,
            drag_destination=drag_metadata.get("screen_target"),
            expected_context=capture_context,
        )
    elif bool(uia_control_highlight_metadata.get("drawn")):
        action_state_metadata = mark_mouse_target_verified(
            window_id,
            str(uia_control_highlight_metadata["coord_origin"]),
            int(uia_control_highlight_metadata["target"]["x"]),
            int(uia_control_highlight_metadata["target"]["y"]),
            uia_control_highlight_metadata["screen_target"],
            uia_control_highlight_metadata["window_target"],
            path,
            target_detail_metadata,
            expected_context=capture_context,
        )
    if cursor_crosshair and cursor_inside:
        cursor_detail_required = bool(paired_cursor_screenshot_enabled)
        cursor_detail_ready = bool(cursor_detail_metadata.get("created") and cursor_detail_metadata.get("path"))
        if not cursor_detail_required or cursor_detail_ready:
            action_state_metadata = clear_click_verification_after_cursor_screenshot(
                window_id,
                screen_cursor,
                window_cursor,
                cursor_detail_metadata,
            )
    cleanup_result = cleanup_screenshot_folder(screenshots_dir, screenshot_keep_count)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {
        "ok": True,
        "mode": "screenshot-window",
        "window_id": window_id,
        "window": window,
        "rect": rect,
        "screenshot_coord_origin": "window",
        "coordinate_context": window_coordinate_context(window_id),
        "cursor_crosshair": {
            "enabled": cursor_crosshair,
            "drawn": bool(cursor_crosshair and cursor_inside),
            "screen_cursor": screen_cursor,
            "window_cursor": window_cursor,
            "cursor_inside": cursor_inside,
            "color": "#00dcff",
            "alpha": SCREENSHOT_CROSSHAIR_ALPHA,
            "line_width": SCREENSHOT_CROSSHAIR_HALF_WIDTH * 2 + 1,
        },
        "target_crosshair": target_metadata,
        "drag_target_crosshair": drag_metadata,
        "target_detail_screenshot": target_detail_metadata,
        "cursor_detail_screenshot": cursor_detail_metadata,
        "controls_overlay": controls_metadata,
        "uia_controls_overlay": uia_controls_metadata,
        "uia_control_highlight": uia_control_highlight_metadata,
        "uia_highlight_control_screenshot": uia_highlight_control_screenshot_metadata,
        "ruler": ruler_metadata,
        "path": str(path),
        "width": output_width,
        "height": output_height,
        "content_width": rect["width"],
        "content_height": rect["height"],
        "screenshot_cleanup": cleanup_result,
        "action_state": action_state_metadata,
        "elapsed_ms": elapsed_ms,
    }


def list_controls_result(
    window_id: int,
    include_hidden: bool,
    limit: int,
) -> dict[str, object]:
    started = time.perf_counter()
    controls = enumerate_window_controls(window_id, include_hidden, limit)
    return {
        "ok": True,
        "mode": "list-controls",
        "window_id": window_id,
        "window": window_snapshot(window_id),
        "coordinate_context": window_coordinate_context(window_id),
        "controls": controls,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


def list_uia_controls_result(
    window_id: int,
    include_offscreen: bool,
    limit: int,
    max_depth: int,
    control_types: set[str] | None,
) -> dict[str, object]:
    started = time.perf_counter()
    controls = enumerate_uia_controls(
        window_id,
        include_offscreen,
        limit,
        max_depth,
        control_types,
        name=getattr(CURRENT_ARGS, "uia_name", None),
        automation_id=getattr(CURRENT_ARGS, "uia_automation_id", None),
    )
    return {
        "ok": True,
        "mode": "uia-list-controls",
        "window_id": window_id,
        "window": window_snapshot(window_id),
        "coordinate_context": window_coordinate_context(window_id),
        "controls": controls,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


def text_source_count(args: argparse.Namespace) -> int:
    return sum(
        value is not None
        for value in (
            args.text,
            args.text_file,
            True if args.stdin else None,
        )
    )


def has_mouse_action(args: argparse.Namespace) -> bool:
    return (
        args.mouse_move is not None
        or args.mouse_move_relative is not None
        or args.scroll_ticks is not None
        or args.click is not None
        or args.double_click is not None
        or args.drag_to is not None
    )


def keyboard_action_names(args: argparse.Namespace) -> list[str]:
    actions = []
    if args.hotkey is not None:
        actions.append("hotkey:" + args.hotkey)
    if args.select_all:
        actions.append("select-all")
    if args.press_delete:
        actions.append("delete")
    if args.press_backspace:
        actions.append("backspace")
    if args.press_enter:
        actions.append("enter")
    return actions


def has_keyboard_action(args: argparse.Namespace) -> bool:
    return bool(keyboard_action_names(args))


def execute_keyboard_actions(args: argparse.Namespace) -> list[str]:
    if args.window_id is not None:
        operation = current_operation()
        def guard():
            check_selected_window()
            if active_window_id() != args.window_id:
                raise ActionError("FOCUS_CHANGED", "keyboard target is no longer active", "focus the intended window and retry")
        guard()
        if operation is not None:
            operation.guard = guard
    executed = []
    if args.hotkey is not None:
        press_hotkey(args.hotkey_keys)
        executed.append("hotkey:" + args.hotkey)
    if args.select_all:
        check_cancelled()
        press_ctrl_a()
        executed.append("select-all")
        interruptible_sleep(KEY_ACTION_PAUSE_SECONDS)
    if args.press_delete:
        check_cancelled()
        press_key(VK_DELETE)
        executed.append("delete")
        interruptible_sleep(KEY_ACTION_PAUSE_SECONDS)
    if args.press_backspace:
        check_cancelled()
        press_key(VK_BACK)
        executed.append("backspace")
        interruptible_sleep(KEY_ACTION_PAUSE_SECONDS)
    if args.press_enter:
        check_cancelled()
        press_key(VK_RETURN)
        executed.append("enter")
        interruptible_sleep(KEY_ACTION_PAUSE_SECONDS)
    return executed


def run_double_click(args):
    store = action_store()
    api.user32.GetDoubleClickTime.argtypes = ()
    api.user32.GetDoubleClickTime.restype = UINT
    threshold_ms = int(api.user32.GetDoubleClickTime())
    hold_ms = min(args.max_click_hold_ms, max(1, threshold_ms // 8))
    gap_ms = max(1, min(50, threshold_ms // 4))
    if args.dry_run:
        preconditions = []
        try:
            store.check_click(args.window_id)
        except ActionError as exc:
            preconditions.append({"code": exc.code, "message": str(exc)})
        return {"ok": True, "mode": "mouse-dry-run", "planned_action": "double-click",
                "button": args.double_click, "planned_click_count": 2, "system_interval_ms": threshold_ms,
                "dry_run": True, "executable": not preconditions, "preconditions": preconditions}
    check_cancelled()
    store.consume_click(args.window_id)
    started = time.monotonic()
    click_mouse(args.double_click, hold_ms, hold_ms)
    interruptible_sleep(gap_ms / 1000)
    check_cancelled()
    store.check_consumed_click(args.window_id)
    if (time.monotonic() - started) * 1000 >= threshold_ms:
        raise ActionError("DOUBLE_CLICK_TIMEOUT", "first click completed, but the system double-click interval elapsed")
    click_mouse(args.double_click, hold_ms, hold_ms)
    store.invalidate("double-click completed")
    return {"ok": True, "mode": "double-click", "button": args.double_click, "click_count": 2,
            "system_interval_ms": threshold_ms, "elapsed_ms": int((time.monotonic() - started) * 1000),
            "action_state": store.summary()}


def run_drag(args):
    store = action_store()
    window_id = args.window_id if args.window_id is not None else active_window_id()
    destination = point_to_screen(args.coord_origin, *args.drag_to, window_id)
    start = cursor_position()
    if args.dry_run:
        preconditions = []
        try:
            store.check_drag(destination, window_id)
        except ActionError as exc:
            preconditions.append({"code": exc.code, "message": str(exc)})
        return {"ok": True, "mode": "mouse-dry-run", "planned_action": "drag", "dry_run": True,
                "window_id": window_id, "button": args.drag_button, "screen_start": start,
                "screen_destination": destination, "sequence": ["button-down", "move", "button-up"],
                "executable": not preconditions, "preconditions": preconditions}
    check_cancelled()
    state = store.consume_drag(destination, window_id)
    down, up = ((MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP) if args.drag_button == "left"
                else (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP))
    operation = current_operation()
    if operation is not None:
        operation.guard = lambda: store.check_drag_context(state, destination)
    try:
        send_input(mouse_button_event(down))
        try:
            movement = smooth_set_cursor_position(start["x"], start["y"], destination["x"], destination["y"],
                                                  args.min_mouse_move_duration_ms, args.max_mouse_move_duration_ms,
                                                  args.mouse_move_step_delay_ms, 0, 0,
                                                  args.mouse_move_slow_zone_distance_px,
                                                  args.mouse_move_slow_zone_min_speed_percent)
            check_cancelled()
            if cursor_position() != destination:
                raise ActionError("CURSOR_MISMATCH", "drag did not reach its destination")
        finally:
            send_input(mouse_button_event(up))
    finally:
        if operation is not None:
            operation.guard = None
            try:
                operation.actual_cursor = cursor_position()
            except OSError:
                operation.actual_cursor = None
    count_completed("drags")
    store.invalidate("drag completed")
    return {"ok": True, "mode": "drag", "button": args.drag_button, "window_id": window_id,
            "screen_start": start, "screen_destination": destination, "actual_cursor": cursor_position(),
            "movement": movement, "action_state": store.summary()}


def read_text(args: argparse.Namespace) -> str:
    sources = text_source_count(args)
    if sources != 1:
        raise ValueError("pass exactly one text source: --text, --text-file, or --stdin")

    if args.text is not None:
        return args.text
    if args.stdin:
        return sys.stdin.read()
    return Path(args.text_file).read_text(encoding=args.encoding)


def load_settings(path: str) -> dict[str, int]:
    settings = DEFAULT_SETTINGS.copy()
    config_path = Path(path)

    if not config_path.exists():
        return settings

    raw_settings = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw_settings, dict):
        raise ValueError("settings file must contain a JSON object")

    for key in DEFAULT_SETTINGS:
        if key in raw_settings:
            if key in {"screenshot_delay_ms", "activity_frame_enabled", "activity_frame_width_px",
                       "activity_frame_gradient_enabled", "activity_frame_opacity_percent",
                       "activity_frame_lead_ms", "activity_session_timeout_s"} and type(raw_settings[key]) is not int:
                raise ValueError(key + " must be an integer")
            settings[key] = int(raw_settings[key])

    return settings


def apply_settings(args: argparse.Namespace) -> None:
    settings = load_settings(args.config)
    args.verification_ttl_seconds = settings["verification_ttl_seconds"]
    args.verification_tolerance_px = settings["verification_tolerance_px"]
    if settings["activity_frame_enabled"] not in (0, 1):
        raise ValueError("activity_frame_enabled must be 0 or 1")
    if args.activity_frame is None:
        args.activity_frame = bool(settings["activity_frame_enabled"])
    args.activity_frame_width_px = settings["activity_frame_width_px"]
    args.activity_frame_gradient_enabled = settings["activity_frame_gradient_enabled"]
    args.activity_frame_opacity_percent = settings["activity_frame_opacity_percent"]
    args.activity_frame_lead_ms = settings["activity_frame_lead_ms"]
    args.session_timeout_explicit = args.session_timeout_s is not None
    if args.session_timeout_s is None:
        args.session_timeout_s = settings["activity_session_timeout_s"]
    if args.min_delay_ms is None:
        args.min_delay_ms = settings["min_delay_ms"]
    if args.max_delay_ms is None:
        args.max_delay_ms = settings["max_delay_ms"]
    if args.min_scroll_delay_ms is None:
        args.min_scroll_delay_ms = settings["min_scroll_delay_ms"]
    if args.max_scroll_delay_ms is None:
        args.max_scroll_delay_ms = settings["max_scroll_delay_ms"]
    if args.scroll_batch_size is None:
        args.scroll_batch_size = settings["scroll_batch_size"]
    if args.min_scroll_batch_pause_ms is None:
        args.min_scroll_batch_pause_ms = settings["min_scroll_batch_pause_ms"]
    if args.max_scroll_batch_pause_ms is None:
        args.max_scroll_batch_pause_ms = settings["max_scroll_batch_pause_ms"]
    if args.scroll_jitter_px is None:
        args.scroll_jitter_px = settings["scroll_jitter_px"]
    if args.min_click_hold_ms is None:
        args.min_click_hold_ms = settings["min_click_hold_ms"]
    if args.max_click_hold_ms is None:
        args.max_click_hold_ms = settings["max_click_hold_ms"]
    if args.min_mouse_move_duration_ms is None:
        args.min_mouse_move_duration_ms = settings["min_mouse_move_duration_ms"]
    if args.max_mouse_move_duration_ms is None:
        args.max_mouse_move_duration_ms = settings["max_mouse_move_duration_ms"]
    if args.mouse_move_step_delay_ms is None:
        args.mouse_move_step_delay_ms = settings["mouse_move_step_delay_ms"]
    if args.mouse_move_jitter_px is None:
        args.mouse_move_jitter_px = settings["mouse_move_jitter_px"]
    if args.mouse_move_jitter_stop_distance_px is None:
        args.mouse_move_jitter_stop_distance_px = settings["mouse_move_jitter_stop_distance_px"]
    if args.mouse_move_slow_zone_distance_px is None:
        args.mouse_move_slow_zone_distance_px = settings["mouse_move_slow_zone_distance_px"]
    if args.mouse_move_slow_zone_min_speed_percent is None:
        args.mouse_move_slow_zone_min_speed_percent = settings[
            "mouse_move_slow_zone_min_speed_percent"
        ]
    if args.screenshot_keep_count is None:
        args.screenshot_keep_count = settings["screenshot_keep_count"]
    args.screenshot_delay_explicit = args.screenshot_delay_ms is not None
    if args.screenshot_delay_ms is None:
        args.screenshot_delay_ms = settings["screenshot_delay_ms"]
    args.paired_target_screenshot_enabled = bool(settings["paired_target_screenshot_enabled"])
    args.paired_target_crop_radius_px = settings["paired_target_crop_radius_px"]
    args.paired_target_zoom = settings["paired_target_zoom"]
    args.paired_target_ruler_step_px = settings["paired_target_ruler_step_px"]
    args.paired_target_ruler_major_step_px = settings["paired_target_ruler_major_step_px"]
    args.paired_cursor_screenshot_enabled = bool(settings["paired_cursor_screenshot_enabled"])
    args.paired_cursor_crop_radius_px = settings["paired_cursor_crop_radius_px"]
    args.paired_cursor_zoom = settings["paired_cursor_zoom"]
    args.paired_cursor_ruler_step_px = settings["paired_cursor_ruler_step_px"]
    args.paired_cursor_ruler_major_step_px = settings["paired_cursor_ruler_major_step_px"]
    args.uia_highlight_control_screenshot_enabled = bool(
        settings["uia_highlight_control_screenshot_enabled"]
    )
    args.uia_highlight_control_padding_px = settings["uia_highlight_control_padding_px"]
    args.uia_highlight_control_zoom = settings["uia_highlight_control_zoom"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect Windows applications and perform verified desktop actions."
    )
    parser.add_argument(
        "--list-windows",
        action="store_true",
        help="List visible top-level windows as JSON and exit.",
    )
    parser.add_argument(
        "--active-window",
        action="store_true",
        help="Return the current foreground window as JSON and exit.",
    )
    parser.add_argument(
        "--cursor-position",
        action="store_true",
        help="Return the current mouse cursor position as JSON and exit.",
    )
    parser.add_argument(
        "--list-controls",
        action="store_true",
        help="List Win32 child controls for --window-id, or the active window if omitted.",
    )
    parser.add_argument(
        "--uia-list-controls",
        action="store_true",
        help="List Windows UI Automation controls for --window-id, or the active window if omitted.",
    )
    parser.add_argument(
        "--window-id",
        type=parse_window_id,
        help="Target HWND from --list-windows. Decimal and 0x hex are accepted.",
    )
    parser.add_argument("--window-title", help="Exact window title; combine with --process-name to narrow selection.")
    parser.add_argument("--process-name", help="Executable name (case-insensitive), e.g. notepad.exe.")
    parser.add_argument("--uia-name", help="Exact UIA name for --uia-list-controls or --wait-control.")
    parser.add_argument("--uia-automation-id", help="Exact AutomationId for --uia-list-controls or --wait-control.")
    parser.add_argument("--wait-control", action="store_true", help="Wait for exactly one visible, enabled UIA control; does not focus or click.")
    parser.add_argument("--timeout-s", type=float, default=5.0, help="Total timeout for UIA reads or waiting (default: 5 seconds).")
    parser.add_argument("--poll-interval-ms", type=int, default=100, help="Delay between --wait-control queries (default: 100 ms).")
    parser.add_argument("--hotkey", help="One key or combination, e.g. Ctrl+S, Alt+Tab, Ctrl+Shift+Home.")
    parser.add_argument("--double-click", choices=("left", "right"), help="Double-click one verified target using the system double-click interval.")
    parser.add_argument("--screenshot-drag-target", nargs=2, type=int, metavar=("X", "Y"), help="Add drag destination (label 2) to a --screenshot-target preview (label 1).")
    parser.add_argument("--drag-to", nargs=2, type=int, metavar=("X", "Y"), help="Drag from the verified cursor to a destination verified in the same screenshot.")
    parser.add_argument("--drag-button", choices=("left", "right"), default="left")
    sessions = parser.add_mutually_exclusive_group()
    sessions.add_argument("--session-start", action="store_true", help="Bind a session to an explicitly selected window and show its activity frame between calls.")
    sessions.add_argument("--session-end", action="store_true", help="End the activity session; a running action observes cancellation.")
    sessions.add_argument("--session-status", action="store_true", help="Read activity session status without renewing its lease.")
    sessions.add_argument("--session-heartbeat", action="store_true", help="Renew an existing activity session while reviewing screenshots.")
    parser.add_argument("--session-timeout-s", type=int, help="Idle timeout for --session-start (default: activity_session_timeout_s, 120 seconds).")
    parser.add_argument("--session-owner-pid", type=int, help="For --session-start: also end when this long-lived controller process exits.")
    frames = parser.add_mutually_exclusive_group()
    frames.add_argument("--activity-frame", dest="activity_frame", action="store_true", help="Show an activity frame during an action (enabled by default).")
    frames.add_argument("--no-activity-frame", dest="activity_frame", action="store_false", help="Do not create a frame for this action; an existing session remains active.")
    parser.set_defaults(activity_frame=None)
    parser.add_argument(
        "--focus-only",
        action="store_true",
        help="Focus --window-id and exit without typing.",
    )
    parser.add_argument(
        "--resize-window",
        nargs=2,
        type=int,
        metavar=("WIDTH", "HEIGHT"),
        help="Resize --window-id, or the active window if --window-id is omitted.",
    )
    parser.add_argument(
        "--set-window-rect",
        nargs=4,
        type=int,
        metavar=("X", "Y", "WIDTH", "HEIGHT"),
        help="Move and resize --window-id, or the active window if --window-id is omitted.",
    )
    parser.add_argument(
        "--minimize-window",
        action="store_true",
        help="Minimize --window-id, or the active window if --window-id is omitted.",
    )
    parser.add_argument(
        "--screenshot-window",
        action="store_true",
        help="Save a PNG screenshot of --window-id, or the active window if omitted.",
    )
    parser.add_argument(
        "--screenshot-after",
        action="store_true",
        help="Capture the action window after successful input, focus, resize, or window movement. Mouse movement includes a cursor crosshair.",
    )
    parser.add_argument(
        "--screenshot-delay-ms",
        type=int,
        metavar="MS",
        help="Wait after the action before --screenshot-after (settings: screenshot_delay_ms; default: 500 ms; 0: immediately).",
    )
    parser.add_argument(
        "--screenshot-cursor-crosshair",
        "--screenshot-crosshair",
        action="store_true",
        dest="screenshot_cursor_crosshair",
        help="Draw translucent cyan crosshair lines at the cursor position on screenshots.",
    )
    parser.add_argument(
        "--screenshot-target",
        nargs=2,
        type=int,
        metavar=("X", "Y"),
        help="Draw a virtual red target at X Y in --coord-origin space without moving the mouse.",
    )
    parser.add_argument(
        "--screenshot-ruler",
        action="store_true",
        help="Add an outer coordinate ruler around the screenshot.",
    )
    parser.add_argument(
        "--controls-overlay",
        action="store_true",
        help="Draw Win32 child control rectangles and numeric IDs on the screenshot.",
    )
    parser.add_argument(
        "--uia-controls-overlay",
        action="store_true",
        help="Draw Windows UI Automation control rectangles and numeric IDs on the screenshot.",
    )
    parser.add_argument(
        "--uia-highlight-control-id",
        type=int,
        help="Highlight exactly one UIA control by numeric id from --uia-list-controls.",
    )
    parser.add_argument(
        "--uia-highlight-automation-id",
        help="Highlight exactly one UIA control by AutomationId.",
    )
    parser.add_argument(
        "--uia-highlight-name",
        help="Highlight exactly one UIA control by exact Name.",
    )
    parser.add_argument(
        "--controls-include-hidden",
        action="store_true",
        help="Include hidden/off-window child controls in --list-controls and controls overlay metadata.",
    )
    parser.add_argument(
        "--controls-limit",
        type=int,
        default=CONTROL_OVERLAY_MAX_DEFAULT,
        help="Maximum number of child controls to return or draw.",
    )
    parser.add_argument(
        "--uia-include-offscreen",
        action="store_true",
        help="Include offscreen UIA controls in --uia-list-controls and UIA overlay metadata.",
    )
    parser.add_argument(
        "--uia-max-depth",
        type=int,
        default=8,
        help="Maximum depth for UIA tree traversal.",
    )
    parser.add_argument(
        "--uia-control-types",
        help="Comma-separated UIA ControlTypeName filter, for example: ButtonControl,EditControl.",
    )
    parser.add_argument(
        "--screenshot-ruler-margin-px",
        type=int,
        default=SCREENSHOT_RULER_MARGIN_PX,
        help="Outer ruler margin in pixels.",
    )
    parser.add_argument(
        "--screenshot-ruler-step-px",
        type=int,
        default=SCREENSHOT_RULER_STEP_PX,
        help="Minor ruler tick step in pixels.",
    )
    parser.add_argument(
        "--screenshot-ruler-major-step-px",
        type=int,
        default=SCREENSHOT_RULER_MAJOR_STEP_PX,
        help="Major ruler tick and label step in pixels.",
    )
    parser.add_argument(
        "--screenshot-keep-count",
        type=int,
        help="How many newest screenshots to keep in the screenshots folder.",
    )
    parser.add_argument("--text", help="Text to type.")
    parser.add_argument("--text-file", help="UTF-8 text file to type.")
    parser.add_argument("--stdin", action="store_true", help="Read text from stdin.")
    parser.add_argument("--encoding", default="utf-8", help="Encoding for --text-file.")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="JSON settings file. Defaults to settings.json next to this script.",
    )
    parser.add_argument(
        "--coord-origin",
        choices=("screen", "window", "client"),
        default="screen",
        help="Coordinate origin for --mouse-move and --cursor-position.",
    )
    parser.add_argument("--min-delay-ms", type=int)
    parser.add_argument("--max-delay-ms", type=int)
    parser.add_argument("--min-scroll-delay-ms", type=int)
    parser.add_argument("--max-scroll-delay-ms", type=int)
    parser.add_argument("--scroll-batch-size", type=int)
    parser.add_argument("--min-scroll-batch-pause-ms", type=int)
    parser.add_argument("--max-scroll-batch-pause-ms", type=int)
    parser.add_argument("--scroll-jitter-px", type=int)
    parser.add_argument("--min-click-hold-ms", type=int)
    parser.add_argument("--max-click-hold-ms", type=int)
    parser.add_argument("--min-mouse-move-duration-ms", type=int)
    parser.add_argument("--max-mouse-move-duration-ms", type=int)
    parser.add_argument("--mouse-move-step-delay-ms", type=int)
    parser.add_argument("--mouse-move-jitter-px", type=int)
    parser.add_argument("--mouse-move-jitter-stop-distance-px", type=int)
    parser.add_argument("--mouse-move-slow-zone-distance-px", type=int)
    parser.add_argument("--mouse-move-slow-zone-min-speed-percent", type=int)
    parser.add_argument("--initial-delay-s", type=float, default=3.0)
    parser.add_argument(
        "--newline",
        choices=("shift-enter", "enter", "unicode"),
        default="shift-enter",
        help="How to type line breaks. shift-enter is safest for Telegram Web.",
    )
    parser.add_argument(
        "--no-abort-key",
        action="store_true",
        help="Disable Esc as an emergency stop key.",
    )
    parser.add_argument(
        "--send-enter-at-end",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--select-all",
        action="store_true",
        help="Press Ctrl+A in the focused field.",
    )
    parser.add_argument(
        "--press-delete",
        "--delete",
        action="store_true",
        dest="press_delete",
        help="Press the Delete key.",
    )
    parser.add_argument(
        "--press-backspace",
        "--backspace",
        action="store_true",
        dest="press_backspace",
        help="Press the Backspace key.",
    )
    parser.add_argument(
        "--press-enter",
        "--enter",
        action="store_true",
        dest="press_enter",
        help="Press the Enter key as a separate action.",
    )
    parser.add_argument(
        "--mouse-move",
        nargs=2,
        type=int,
        metavar=("X", "Y"),
        help="Move mouse cursor to coordinates X Y in --coord-origin space.",
    )
    parser.add_argument(
        "--mouse-move-relative",
        nargs=2,
        type=int,
        metavar=("DX", "DY"),
        help="Move mouse cursor by DX DY pixels from the current position.",
    )
    parser.add_argument(
        "--smooth-move",
        action="store_true",
        help="Move mouse cursor smoothly when used with --mouse-move or --mouse-move-relative.",
    )
    parser.add_argument(
        "--click",
        choices=("left", "right"),
        help="Click the left or right mouse button at the current cursor position.",
    )
    parser.add_argument(
        "--scroll-ticks",
        type=int,
        help="Scroll the mouse wheel by this many wheel ticks.",
    )
    parser.add_argument(
        "--scroll-direction",
        choices=("up", "down"),
        default="down",
        help="Wheel direction for --scroll-ticks.",
    )
    parser.add_argument(
        "--scroll-jitter",
        action="store_true",
        help="Slightly move the cursor around its position while scrolling.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Return a plan without input, window changes, or file writes.")
    parser.add_argument("--quiet", action="store_true", help="Do not write progress to stderr.")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.activity_frame_gradient_enabled not in (0, 1):
        raise ValueError("activity_frame_gradient_enabled must be 0 or 1")
    if not 1 <= args.activity_frame_opacity_percent <= 100:
        raise ValueError("activity_frame_opacity_percent must be 1..100")
    if not 1 <= args.activity_frame_width_px <= 32 or not 0 <= args.activity_frame_lead_ms <= 60000:
        raise ValueError("activity frame width must be 1..32 px and lead time 0..60000 ms")
    if not 1 <= args.session_timeout_s <= 86400:
        raise ValueError("--session-timeout-s must be 1..86400 seconds")
    if args.session_timeout_explicit and not args.session_start:
        raise ValueError("--session-timeout-s requires --session-start")
    if args.session_owner_pid is not None and (not args.session_start or not 0 < args.session_owner_pid <= 0xFFFFFFFF):
        raise ValueError("--session-owner-pid requires --session-start and a positive process ID")
    if any(getattr(args, name) for name in SESSION_MODES):
        if args.screenshot_after or (not args.session_start and
                (args.window_id is not None or args.window_title is not None or args.process_name is not None)):
            raise ValueError("only --session-start accepts window selection; session commands do not accept --screenshot-after")
    if args.screenshot_delay_ms < 0 or not math.isfinite(args.screenshot_delay_ms / 1000):
        raise ValueError("--screenshot-delay-ms must be finite and non-negative")
    if args.screenshot_delay_explicit and not args.screenshot_after:
        raise ValueError("--screenshot-delay-ms requires --screenshot-after")
    if args.screenshot_after:
        if args.screenshot_window or window_mode(args) in READ_MODES or args.minimize_window:
            raise ValueError("--screenshot-after requires an input action, focus, resize, or window movement")
        if not (has_mouse_action(args) or has_keyboard_action(args) or text_source_count(args)
                or args.focus_only or args.resize_window is not None or args.set_window_rect is not None):
            raise ValueError("--screenshot-after requires an action")
    if args.screenshot_drag_target is not None and (not args.screenshot_window or args.screenshot_target is None):
        raise ValueError("--screenshot-drag-target requires --screenshot-window and --screenshot-target")
    if args.hotkey is not None:
        args.hotkey_keys = parse_hotkey(args.hotkey)
        if text_source_count(args) or any((args.select_all, args.press_delete, args.press_backspace, args.press_enter)):
            raise ValueError("--hotkey must be a separate keyboard command; verify text before sending Enter")
    if not math.isfinite(args.timeout_s) or args.timeout_s <= 0 or args.poll_interval_ms <= 0:
        raise ValueError("--timeout-s and --poll-interval-ms must be finite and positive")
    if (args.uia_name is not None or args.uia_automation_id is not None) and not (args.uia_list_controls or args.wait_control):
        raise ValueError("--uia-name and --uia-automation-id require --uia-list-controls or --wait-control")
    if args.wait_control and args.uia_name is None and args.uia_automation_id is None and not args.uia_control_types:
        raise ValueError("--wait-control requires a name, AutomationId, or control type")
    selectors = args.window_title is not None or args.process_name is not None
    if selectors and args.window_id is not None:
        raise ValueError("--window-id cannot be combined with window selectors")
    if selectors and args.active_window:
        raise ValueError("--active-window cannot be combined with window selectors; use --list-windows")
    if args.window_title == "" or args.process_name == "":
        raise ValueError("window selectors must not be empty")
    modes = [name for name in WINDOW_MODES if getattr(args, name)]
    if len(modes) > 1:
        raise ValueError("choose exactly one window/read mode")
    if modes and (has_mouse_action(args) or has_keyboard_action(args) or text_source_count(args)):
        raise ValueError("window/read modes cannot be combined with input actions")
    if has_mouse_action(args) and has_keyboard_action(args):
        raise ValueError("mouse and keyboard actions require separate calls")
    if sum(value is not None for value in (args.mouse_move, args.mouse_move_relative, args.click, args.double_click, args.drag_to, args.scroll_ticks)) > 1:
        raise ValueError("choose one mouse action per call")
    if text_source_count(args) > 1:
        raise ValueError("choose one text source")
    if not modes and not has_mouse_action(args) and not has_keyboard_action(args) and not text_source_count(args):
        raise ValueError("choose a command or pass text; see --help")
    if args.verification_ttl_seconds <= 0 or args.verification_tolerance_px < 0:
        raise ValueError("verification TTL must be positive and tolerance non-negative")
    if args.send_enter_at_end:
        raise ValueError(
            "--send-enter-at-end is disabled; type text first, verify screenshot, "
            "then run --press-enter separately"
        )
    if args.focus_only and has_keyboard_action(args):
        raise ValueError("--focus-only cannot be combined with keyboard actions")
    if args.screenshot_cursor_crosshair and not (args.screenshot_window or args.screenshot_after):
        raise ValueError("--screenshot-cursor-crosshair requires --screenshot-window or --screenshot-after")
    if args.screenshot_target is not None and not args.screenshot_window:
        raise ValueError("--screenshot-target requires --screenshot-window")
    if args.screenshot_ruler and not (args.screenshot_window or args.screenshot_after):
        raise ValueError("--screenshot-ruler requires --screenshot-window or --screenshot-after")
    if args.controls_overlay and not (args.screenshot_window or args.screenshot_after):
        raise ValueError("--controls-overlay requires --screenshot-window or --screenshot-after")
    if args.uia_controls_overlay and not (args.screenshot_window or args.screenshot_after):
        raise ValueError("--uia-controls-overlay requires --screenshot-window or --screenshot-after")
    uia_highlight_count = sum(
        selector is not None
        for selector in (
            args.uia_highlight_control_id,
            args.uia_highlight_automation_id,
            args.uia_highlight_name,
        )
    )
    if uia_highlight_count > 1:
        raise ValueError(
            "use only one UIA highlight selector: --uia-highlight-control-id, "
            "--uia-highlight-automation-id, or --uia-highlight-name"
        )
    if args.uia_highlight_control_id is not None and args.uia_highlight_control_id <= 0:
        raise ValueError("--uia-highlight-control-id must be positive")
    if uia_highlight_count and not args.screenshot_window:
        raise ValueError("UIA control highlight requires --screenshot-window")
    if uia_highlight_count and (args.controls_overlay or args.uia_controls_overlay):
        raise ValueError("UIA control highlight cannot be combined with controls overlay")
    if uia_highlight_count and args.screenshot_target is not None:
        raise ValueError("UIA control highlight cannot be combined with --screenshot-target")
    if uia_highlight_count and (args.list_controls or args.uia_list_controls):
        raise ValueError("UIA control highlight cannot be combined with control listing")
    if args.controls_limit <= 0:
        raise ValueError("--controls-limit must be positive")
    if args.uia_max_depth < 0:
        raise ValueError("--uia-max-depth must be non-negative")
    if args.list_controls and args.uia_list_controls:
        raise ValueError("--list-controls cannot be combined with --uia-list-controls")
    if args.list_controls:
        if (
            args.focus_only
            or args.screenshot_window
            or args.resize_window is not None
            or args.set_window_rect is not None
            or args.minimize_window
        ):
            raise ValueError("--list-controls cannot be combined with other window modes")
        if has_mouse_action(args) or has_keyboard_action(args) or text_source_count(args):
            raise ValueError("--list-controls cannot be combined with actions or text input")
    if args.uia_list_controls:
        if (
            args.focus_only
            or args.screenshot_window
            or args.resize_window is not None
            or args.set_window_rect is not None
            or args.minimize_window
        ):
            raise ValueError("--uia-list-controls cannot be combined with other window modes")
        if has_mouse_action(args) or has_keyboard_action(args) or text_source_count(args):
            raise ValueError("--uia-list-controls cannot be combined with actions or text input")
    if args.screenshot_ruler_margin_px <= 0:
        raise ValueError("--screenshot-ruler-margin-px must be positive")
    if args.screenshot_ruler_step_px <= 0:
        raise ValueError("--screenshot-ruler-step-px must be positive")
    if args.screenshot_ruler_major_step_px <= 0:
        raise ValueError("--screenshot-ruler-major-step-px must be positive")
    if args.screenshot_keep_count != -1 and args.screenshot_keep_count <= 0:
        raise ValueError("--screenshot-keep-count must be positive or -1")
    if args.paired_target_crop_radius_px <= 0:
        raise ValueError("paired_target_crop_radius_px must be positive")
    if args.paired_target_zoom <= 0:
        raise ValueError("paired_target_zoom must be positive")
    if args.paired_target_ruler_step_px <= 0:
        raise ValueError("paired_target_ruler_step_px must be positive")
    if args.paired_target_ruler_major_step_px <= 0:
        raise ValueError("paired_target_ruler_major_step_px must be positive")
    if args.paired_cursor_crop_radius_px <= 0:
        raise ValueError("paired_cursor_crop_radius_px must be positive")
    if args.paired_cursor_zoom <= 0:
        raise ValueError("paired_cursor_zoom must be positive")
    if args.paired_cursor_ruler_step_px <= 0:
        raise ValueError("paired_cursor_ruler_step_px must be positive")
    if args.paired_cursor_ruler_major_step_px <= 0:
        raise ValueError("paired_cursor_ruler_major_step_px must be positive")
    if args.uia_highlight_control_padding_px < 0:
        raise ValueError("uia_highlight_control_padding_px must be non-negative")
    if args.uia_highlight_control_zoom <= 0:
        raise ValueError("uia_highlight_control_zoom must be positive")
    if args.screenshot_window:
        if (
            args.focus_only
            or args.resize_window is not None
            or args.set_window_rect is not None
            or args.minimize_window
        ):
            raise ValueError(
                "--screenshot-window cannot be combined with focus, resize, rect, or minimize modes"
            )
        if has_mouse_action(args) or has_keyboard_action(args) or text_source_count(args):
            raise ValueError(
                "--screenshot-window cannot be combined with actions or text input"
            )
    if args.minimize_window:
        if args.focus_only or args.resize_window is not None or args.set_window_rect is not None:
            raise ValueError(
                "--minimize-window cannot be combined with focus, resize, or rect modes"
            )
        if has_mouse_action(args) or has_keyboard_action(args) or text_source_count(args):
            raise ValueError("--minimize-window cannot be combined with actions or text input")
    if args.resize_window is not None and args.set_window_rect is not None:
        raise ValueError("--resize-window cannot be combined with --set-window-rect")
    if args.resize_window is not None:
        width, height = args.resize_window
        if width <= 0 or height <= 0:
            raise ValueError("--resize-window width and height must be positive")
        if has_mouse_action(args) or has_keyboard_action(args) or text_source_count(args):
            raise ValueError("--resize-window cannot be combined with actions or text input")
    if args.set_window_rect is not None:
        _x, _y, width, height = args.set_window_rect
        if width <= 0 or height <= 0:
            raise ValueError("--set-window-rect width and height must be positive")
        if has_mouse_action(args) or has_keyboard_action(args) or text_source_count(args):
            raise ValueError("--set-window-rect cannot be combined with actions or text input")
    if has_mouse_action(args) and text_source_count(args):
        raise ValueError("mouse actions cannot be combined with text input in one call")
    if args.press_enter:
        if text_source_count(args):
            raise ValueError(
                "--press-enter cannot be combined with text input; type text first, "
                "verify screenshot, then press Enter separately"
            )
        if args.select_all or args.press_delete or args.press_backspace:
            raise ValueError("--press-enter cannot be combined with other keyboard actions")
        if has_mouse_action(args):
            raise ValueError("--press-enter cannot be combined with mouse actions")
    if args.scroll_ticks is not None and args.scroll_ticks < 0:
        raise ValueError("--scroll-ticks must be non-negative")
    if args.scroll_jitter and args.scroll_ticks is None:
        raise ValueError("--scroll-jitter requires --scroll-ticks")
    if args.min_delay_ms < 0 or args.max_delay_ms < 0:
        raise ValueError("delays must be non-negative")
    if args.min_delay_ms > args.max_delay_ms:
        raise ValueError("--min-delay-ms must be less than or equal to --max-delay-ms")
    if args.min_scroll_delay_ms < 0 or args.max_scroll_delay_ms < 0:
        raise ValueError("scroll delays must be non-negative")
    if args.min_scroll_delay_ms > args.max_scroll_delay_ms:
        raise ValueError(
            "--min-scroll-delay-ms must be less than or equal to --max-scroll-delay-ms"
        )
    if args.scroll_batch_size <= 0:
        raise ValueError("--scroll-batch-size must be positive")
    if args.min_scroll_batch_pause_ms < 0 or args.max_scroll_batch_pause_ms < 0:
        raise ValueError("scroll batch pauses must be non-negative")
    if args.min_scroll_batch_pause_ms > args.max_scroll_batch_pause_ms:
        raise ValueError(
            "--min-scroll-batch-pause-ms must be less than or equal to "
            "--max-scroll-batch-pause-ms"
        )
    if args.scroll_jitter_px < 0:
        raise ValueError("--scroll-jitter-px must be non-negative")
    if args.min_click_hold_ms < 0 or args.max_click_hold_ms < 0:
        raise ValueError("click hold delays must be non-negative")
    if args.min_click_hold_ms > args.max_click_hold_ms:
        raise ValueError("--min-click-hold-ms must be less than or equal to --max-click-hold-ms")
    if args.mouse_move is not None and args.mouse_move_relative is not None:
        raise ValueError("--mouse-move cannot be combined with --mouse-move-relative")
    if args.click is not None and (args.mouse_move is not None or args.mouse_move_relative is not None):
        raise ValueError("--click cannot be combined with mouse movement; move first, then click")
    if args.smooth_move and args.mouse_move is None and args.mouse_move_relative is None and args.drag_to is None:
        raise ValueError("--smooth-move requires --mouse-move or --mouse-move-relative")
    if args.min_mouse_move_duration_ms < 0 or args.max_mouse_move_duration_ms < 0:
        raise ValueError("mouse move durations must be non-negative")
    if args.min_mouse_move_duration_ms > args.max_mouse_move_duration_ms:
        raise ValueError(
            "--min-mouse-move-duration-ms must be less than or equal to "
            "--max-mouse-move-duration-ms"
        )
    if args.mouse_move_step_delay_ms <= 0:
        raise ValueError("--mouse-move-step-delay-ms must be positive")
    if args.mouse_move_jitter_px < 0:
        raise ValueError("--mouse-move-jitter-px must be non-negative")
    if args.mouse_move_jitter_stop_distance_px < 0:
        raise ValueError("--mouse-move-jitter-stop-distance-px must be non-negative")
    if args.mouse_move_slow_zone_distance_px < 0:
        raise ValueError("--mouse-move-slow-zone-distance-px must be non-negative")
    if not 1 <= args.mouse_move_slow_zone_min_speed_percent <= 100:
        raise ValueError("--mouse-move-slow-zone-min-speed-percent must be between 1 and 100")
    if not math.isfinite(args.initial_delay_s) or args.initial_delay_s < 0:
        raise ValueError("--initial-delay-s must be non-negative")


def progress(args: argparse.Namespace, message: str) -> None:
    if not args.quiet:
        print(message, file=sys.stderr, flush=True)


def write_result(result: dict[str, object]) -> None:
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")), flush=True)


def load_action_state() -> dict[str, object]:
    return action_store().read()


def write_action_state(state: dict[str, object]) -> None:
    action_store().write(state)


def clear_action_state() -> None:
    action_store().invalidate("verification cleared")


def action_state_summary(state=None) -> dict[str, object]:
    return action_store().summary()


def mark_mouse_target_verified(window_id, target_coord_origin, target_x, target_y,
                               screen_target, window_target, screenshot_path,
                               target_detail_metadata, *, drag_destination=None, expected_context=None):
    return action_store().preview(window_id, screen_target, {
        "coord_origin": target_coord_origin, "target": {"x": target_x, "y": target_y},
        "window_target": window_target, "screenshot_path": str(screenshot_path),
        "target_detail_path": target_detail_metadata.get("path"),
        "drag_destination": drag_destination,
    }, expected_context=expected_context)


def points_equal(first, second) -> bool:
    return isinstance(first, dict) and isinstance(second, dict) and all(
        first.get(key) == second.get(key) for key in ("x", "y")
    )


def ensure_mouse_move_allowed_by_action_state(planned_screen_cursor, planned_window_cursor, compare_window_id):
    action_store().check_move(planned_screen_cursor, compare_window_id)


def mark_click_verification_required(args, moved_to, screen_moved_to, move_mode):
    action_store().finish_move()
    return action_state_summary()


def ensure_click_allowed_by_action_state(window_id=None):
    action_store().check_click(window_id)


def clear_click_verification_after_cursor_screenshot(window_id, screen_cursor, window_cursor,
                                                      cursor_detail_metadata):
    return action_store().verify_cursor(window_id, screen_cursor, {
        "window_cursor": window_cursor, "cursor_detail_path": cursor_detail_metadata.get("path")
    })


def cursor_position_result(args: argparse.Namespace) -> dict[str, object]:
    screen_cursor = cursor_position()
    coordinate_window_id = resolve_coordinate_window_id(args.coord_origin, args.window_id)
    local_cursor = point_from_screen(
        args.coord_origin,
        screen_cursor["x"],
        screen_cursor["y"],
        coordinate_window_id,
    )
    return {
        "ok": True,
        "mode": "cursor-position",
        "coord_origin": args.coord_origin,
        "cursor": local_cursor,
        "screen_cursor": screen_cursor,
        "coordinate_window": (
            window_coordinate_context(coordinate_window_id)
            if coordinate_window_id is not None
            else None
        ),
    }


def run_mouse_actions(args: argparse.Namespace) -> dict[str, object]:
    started = time.perf_counter()
    focus_result = None
    initial_screen_cursor = cursor_position()
    coordinate_window_id = None
    coordinate_window = None
    input_cursor = (
        {"x": args.mouse_move[0], "y": args.mouse_move[1]}
        if args.mouse_move is not None
        else None
    )
    input_delta = (
        {"dx": args.mouse_move_relative[0], "dy": args.mouse_move_relative[1]}
        if args.mouse_move_relative is not None
        else None
    )
    move_mode = (
        "absolute"
        if args.mouse_move is not None
        else "relative"
        if args.mouse_move_relative is not None
        else None
    )

    if args.dry_run:
        planned_screen_cursor = None
        planned_cursor = None
        if args.coord_origin != "screen":
            coordinate_window_id = resolve_coordinate_window_id(args.coord_origin, args.window_id)
            coordinate_window = window_coordinate_context(coordinate_window_id)
        if args.mouse_move is not None:
            planned_screen_cursor = point_to_screen(
                args.coord_origin,
                args.mouse_move[0],
                args.mouse_move[1],
                coordinate_window_id,
            )
        elif args.mouse_move_relative is not None:
            planned_screen_cursor = {
                "x": initial_screen_cursor["x"] + args.mouse_move_relative[0],
                "y": initial_screen_cursor["y"] + args.mouse_move_relative[1],
            }

        if planned_screen_cursor is not None:
            planned_cursor = point_from_screen(
                args.coord_origin,
                planned_screen_cursor["x"],
                planned_screen_cursor["y"],
                coordinate_window_id,
            )

        initial_cursor = point_from_screen(
            args.coord_origin,
            initial_screen_cursor["x"],
            initial_screen_cursor["y"],
            coordinate_window_id,
        )
        planned_scroll_ticks = 0
        if args.scroll_ticks is not None:
            direction = 1 if args.scroll_direction == "up" else -1
            planned_scroll_ticks = direction * args.scroll_ticks
        planned_scroll_batch_count = (
            (abs(planned_scroll_ticks) + args.scroll_batch_size - 1)
            // args.scroll_batch_size
            if args.scroll_ticks is not None
            else 0
        )

        return {
            "ok": True,
            "mode": "mouse-dry-run",
            "initial_cursor": initial_cursor,
            "initial_screen_cursor": initial_screen_cursor,
            "coord_origin": args.coord_origin,
            "move_mode": move_mode,
            "input_cursor": input_cursor,
            "input_delta": input_delta,
            "planned_cursor": planned_cursor,
            "planned_screen_cursor": planned_screen_cursor,
            "coordinate_window": coordinate_window,
            "smooth_move": bool(args.smooth_move),
            "min_mouse_move_duration_ms": args.min_mouse_move_duration_ms,
            "max_mouse_move_duration_ms": args.max_mouse_move_duration_ms,
            "mouse_move_step_delay_ms": args.mouse_move_step_delay_ms,
            "mouse_move_jitter_px": args.mouse_move_jitter_px,
            "mouse_move_jitter_stop_distance_px": args.mouse_move_jitter_stop_distance_px,
            "mouse_move_slow_zone_distance_px": args.mouse_move_slow_zone_distance_px,
            "mouse_move_slow_zone_min_speed_percent": (
                args.mouse_move_slow_zone_min_speed_percent
            ),
            "planned_scroll_ticks": planned_scroll_ticks,
            "planned_scroll_batch_count": planned_scroll_batch_count,
            "min_scroll_delay_ms": args.min_scroll_delay_ms,
            "max_scroll_delay_ms": args.max_scroll_delay_ms,
            "scroll_batch_size": args.scroll_batch_size,
            "min_scroll_batch_pause_ms": args.min_scroll_batch_pause_ms,
            "max_scroll_batch_pause_ms": args.max_scroll_batch_pause_ms,
            "scroll_jitter": bool(args.scroll_jitter),
            "scroll_jitter_px": args.scroll_jitter_px,
            "planned_click": args.click,
            "planned_key_actions": keyboard_action_names(args),
            "min_click_hold_ms": args.min_click_hold_ms,
            "max_click_hold_ms": args.max_click_hold_ms,
            "window_id": args.window_id,
            "action_state": action_state_summary(),
            "elapsed_ms": 0,
        }

    if args.click is not None:
        ensure_click_allowed_by_action_state(args.window_id)

    initial_screen_cursor = cursor_position()
    if args.coord_origin != "screen":
        coordinate_window_id = resolve_coordinate_window_id(args.coord_origin, args.window_id)
        coordinate_window = window_coordinate_context(coordinate_window_id)
    initial_cursor = point_from_screen(
        args.coord_origin,
        initial_screen_cursor["x"],
        initial_screen_cursor["y"],
        coordinate_window_id,
    )

    moved_to = None
    screen_moved_to = None
    move_result = None
    if args.mouse_move is not None or args.mouse_move_relative is not None:
        if args.mouse_move is not None:
            target = point_to_screen(
                args.coord_origin,
                args.mouse_move[0],
                args.mouse_move[1],
                coordinate_window_id,
            )
        else:
            target = {
                "x": initial_screen_cursor["x"] + args.mouse_move_relative[0],
                "y": initial_screen_cursor["y"] + args.mouse_move_relative[1],
            }
        x = target["x"]
        y = target["y"]
        compare_window_id = coordinate_window_id if coordinate_window_id is not None else args.window_id
        planned_window_cursor = (
            screen_to_window_point(compare_window_id, x, y)
            if compare_window_id is not None
            else None
        )
        ensure_mouse_move_allowed_by_action_state(
            {"x": x, "y": y},
            planned_window_cursor,
            compare_window_id,
        )
        action_store().start_move({"x": x, "y": y}, compare_window_id)
        if args.smooth_move:
            move_result = smooth_set_cursor_position(
                initial_screen_cursor["x"],
                initial_screen_cursor["y"],
                x,
                y,
                args.min_mouse_move_duration_ms,
                args.max_mouse_move_duration_ms,
                args.mouse_move_step_delay_ms,
                args.mouse_move_jitter_px,
                args.mouse_move_jitter_stop_distance_px,
                args.mouse_move_slow_zone_distance_px,
                args.mouse_move_slow_zone_min_speed_percent,
            )
        else:
            set_cursor_position(x, y)
            move_result = {
                "duration_ms": 0,
                "base_duration_ms": 0,
                "step_count": 1,
                "slow_zone_step_count": 0,
            }
        moved_to = point_from_screen(args.coord_origin, x, y, coordinate_window_id)
        screen_moved_to = {"x": x, "y": y}

    click_hold_ms = None
    if args.click is not None:
        action_store().consume_click(args.window_id)
        click_hold_ms = click_mouse(
            args.click,
            args.min_click_hold_ms,
            args.max_click_hold_ms,
        )

    if args.click is not None:
        action_store().invalidate("click completed")

    scroll_ticks = 0
    scroll_event_count = 0
    scroll_jitter_count = 0
    scroll_batch_count = 0
    if args.scroll_ticks is not None:
        direction = 1 if args.scroll_direction == "up" else -1
        scroll_ticks = direction * args.scroll_ticks
        scroll_result = scroll_mouse(
            scroll_ticks,
            args.min_scroll_delay_ms,
            args.max_scroll_delay_ms,
            args.scroll_batch_size,
            args.min_scroll_batch_pause_ms,
            args.max_scroll_batch_pause_ms,
            args.scroll_jitter,
            args.scroll_jitter_px,
        )
        scroll_event_count = scroll_result["event_count"]
        scroll_jitter_count = scroll_result["jitter_count"]
        scroll_batch_count = scroll_result["batch_count"]

    key_actions = execute_keyboard_actions(args)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    final_screen_cursor = cursor_position()
    final_cursor = point_from_screen(
        args.coord_origin,
        final_screen_cursor["x"],
        final_screen_cursor["y"],
        coordinate_window_id,
    )
    action_state_metadata = action_state_summary()
    if moved_to is not None:
        action_state_metadata = mark_click_verification_required(
            args,
            moved_to,
            screen_moved_to,
            move_mode,
        )
    return {
        "ok": True,
        "mode": "mouse",
        "initial_cursor": initial_cursor,
        "initial_screen_cursor": initial_screen_cursor,
        "final_cursor": final_cursor,
        "final_screen_cursor": final_screen_cursor,
        "coord_origin": args.coord_origin,
        "coordinate_window": coordinate_window,
        "move_mode": move_mode,
        "input_cursor": input_cursor,
        "input_delta": input_delta,
        "moved_to": moved_to,
        "screen_moved_to": screen_moved_to,
        "smooth_move": bool(args.smooth_move),
        "mouse_move_duration_ms": move_result["duration_ms"] if move_result else None,
        "mouse_move_base_duration_ms": move_result["base_duration_ms"] if move_result else None,
        "mouse_move_step_count": move_result["step_count"] if move_result else None,
        "mouse_move_jitter_stop_distance_px": args.mouse_move_jitter_stop_distance_px,
        "mouse_move_slow_zone_distance_px": args.mouse_move_slow_zone_distance_px,
        "mouse_move_slow_zone_min_speed_percent": args.mouse_move_slow_zone_min_speed_percent,
        "mouse_move_slow_zone_step_count": (
            move_result["slow_zone_step_count"] if move_result else None
        ),
        "click": args.click,
        "click_hold_ms": click_hold_ms,
        "key_actions": key_actions,
        "key_action_count": len(key_actions),
        "min_click_hold_ms": args.min_click_hold_ms,
        "max_click_hold_ms": args.max_click_hold_ms,
        "scroll_ticks": scroll_ticks,
        "scroll_event_count": scroll_event_count,
        "scroll_batch_size": args.scroll_batch_size,
        "scroll_batch_count": scroll_batch_count,
        "min_scroll_batch_pause_ms": args.min_scroll_batch_pause_ms,
        "max_scroll_batch_pause_ms": args.max_scroll_batch_pause_ms,
        "scroll_jitter": bool(args.scroll_jitter),
        "scroll_jitter_count": scroll_jitter_count,
        "scroll_jitter_px": args.scroll_jitter_px,
        "scroll_direction": args.scroll_direction if args.scroll_ticks is not None else None,
        "min_scroll_delay_ms": args.min_scroll_delay_ms,
        "max_scroll_delay_ms": args.max_scroll_delay_ms,
        "window_id": args.window_id,
        "window_focused": bool(focus_result["focused"]) if focus_result else None,
        "action_state": action_state_metadata,
        "elapsed_ms": elapsed_ms,
    }


def run_keyboard_actions(args: argparse.Namespace) -> dict[str, object]:
    started = time.perf_counter()
    focus_result = None
    planned_actions = keyboard_action_names(args)

    if args.dry_run:
        return {
            "ok": True,
            "mode": "keyboard-dry-run",
            "planned_key_actions": planned_actions,
            "key_action_count": len(planned_actions),
            "window_id": args.window_id,
            "elapsed_ms": 0,
        }

    progress(
        args,
        f"Focus the target input. Keyboard actions start in {args.initial_delay_s:.1f}s.",
    )
    interruptible_sleep(args.initial_delay_s)

    key_actions = execute_keyboard_actions(args)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {
        "ok": True,
        "mode": "keyboard",
        "key_actions": key_actions,
        "key_action_count": len(key_actions),
        "window_id": args.window_id,
        "window_focused": bool(focus_result["focused"]) if focus_result else None,
        "elapsed_ms": elapsed_ms,
    }


def type_text(args: argparse.Namespace, text: str) -> dict[str, object]:
    text.encode("utf-16-le")  # Validate the whole input before focusing or clearing.
    started = time.perf_counter()
    typed = 0
    aborted = False
    focus_result = None
    planned_key_actions = keyboard_action_names(args)

    if args.dry_run:
        return {
            "ok": True,
            "mode": "dry-run",
            "planned_key_actions": planned_key_actions,
            "key_action_count": len(planned_key_actions),
            "typed_chars": 0,
            "planned_chars": len(text.replace("\r", "")),
            "aborted": False,
            "min_delay_ms": args.min_delay_ms,
            "max_delay_ms": args.max_delay_ms,
            "final_enter_sent": False,
            "window_id": args.window_id,
            "elapsed_ms": 0,
        }

    progress(args, f"Focus the target input. Typing starts in {args.initial_delay_s:.1f}s.")
    interruptible_sleep(args.initial_delay_s)

    key_actions = execute_keyboard_actions(args)

    for char in text:
        check_cancelled()

        if char == "\n":
            if args.newline == "shift-enter":
                press_shift_enter()
            elif args.newline == "enter":
                send_input(key_event(VK_RETURN), key_event(VK_RETURN, keyup=True))
            else:
                type_unicode_char(char)
        elif char == "\r":
            continue
        else:
            type_unicode_char(char)

        typed += 1
        count_completed("typed_chars")
        delay = random.uniform(args.min_delay_ms, args.max_delay_ms) / 1000
        interruptible_sleep(delay)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {
        "ok": not aborted,
        "mode": "type",
        "typed_chars": typed,
        "planned_chars": len(text.replace("\r", "")),
        "aborted": aborted,
        "key_actions": key_actions,
        "key_action_count": len(key_actions),
        "min_delay_ms": args.min_delay_ms,
        "max_delay_ms": args.max_delay_ms,
        "final_enter_sent": False,
        "window_id": args.window_id,
        "window_focused": bool(focus_result["focused"]) if focus_result else None,
        "elapsed_ms": elapsed_ms,
    }


READ_MODES = {"list_windows", "active_window", "cursor_position", "list_controls", "uia_list_controls", "wait_control"}
SESSION_MODES = {"session_start", "session_end", "session_status", "session_heartbeat"}
WINDOW_MODES = READ_MODES | SESSION_MODES | {"focus_only", "resize_window", "set_window_rect", "minimize_window", "screenshot_window"}


def window_mode(args):
    return next((name for name in sorted(WINDOW_MODES) if getattr(args, name)), None)


def build_action_plan(args):
    mode = window_mode(args)
    if args.wait_control:
        window_id = args.window_id if args.window_id is not None else active_window_id()
        return {"ok": True, "mode": "wait-control-dry-run", "dry_run": True,
                "planned_action": "wait-control", "window_id": window_id,
                "window": window_snapshot(window_id), "timeout_s": args.timeout_s,
                "selector": {"name": args.uia_name, "automation_id": args.uia_automation_id,
                             "control_types": args.uia_control_types}}
    if args.double_click is not None:
        return run_double_click(args)
    if args.drag_to is not None:
        return run_drag(args)
    if mode in READ_MODES:
        return {**execute_action(args), "dry_run": True}
    if args.window_id is not None:
        if not api.user32.IsWindow(hwnd(args.window_id)):
            raise ValueError("window not found: " + str(args.window_id))
        window_snapshot(args.window_id)
    if mode:
        window_id = args.window_id if args.window_id is not None else active_window_id()
        window = window_snapshot(window_id)
        preconditions = []
        if mode == "screenshot_window" and window.get("is_minimized"):
            preconditions.append({"code": "WINDOW_MINIMIZED", "message": "restore the window before capturing"})
        result = {"ok": True, "mode": "window-dry-run", "window_id": window_id,
                  "window": window, "planned_action": mode.replace("_", "-"),
                  "parameters": {name: getattr(args, name) for name in (
                      "resize_window", "set_window_rect", "screenshot_target", "coord_origin",
                      "screenshot_drag_target",
                      "screenshot_keep_count", "screenshot_cursor_crosshair", "screenshot_ruler")}}
    elif has_mouse_action(args):
        result = run_mouse_actions(args)
        result["planned_action"] = "mouse"
        preconditions = []
        try:
            if args.click is not None:
                action_store().check_click(args.window_id)
            if args.mouse_move is not None or args.mouse_move_relative is not None:
                comparison_window = args.window_id
                if result.get("coordinate_window"):
                    comparison_window = result["coordinate_window"]["id"]
                action_store().check_move(result["planned_screen_cursor"], comparison_window)
        except ActionError as exc:
            preconditions.append({"code": exc.code, "message": str(exc), "required_next_step": exc.next_step})
    elif has_keyboard_action(args) and not text_source_count(args):
        result = run_keyboard_actions(args)
        result["planned_action"] = "keyboard"
        preconditions = []
    else:
        result = type_text(args, read_text(args))
        result["planned_action"] = "type"
        preconditions = []
    return {**result, "dry_run": True, "executable": not preconditions, "preconditions": preconditions}


def main() -> int:
    global CURRENT_ARGS
    previous_args = CURRENT_ARGS
    args = None
    operation = None
    try:
        args = parse_args()
        apply_settings(args)
        validate_args(args)
        CURRENT_ARGS = args
        initialize_dpi_awareness()
        resolve_window_selection(args)
        resolve_action_target(args)
        if window_mode(args) in SESSION_MODES:
            operation = Cancellation(is_escape_down, enabled=not args.no_abort_key)
            result = session_command(args, ACTION_STATE_PATH.parent, check_cancelled=operation.check)
        elif args.dry_run:
            result = build_action_plan(args)
            if args.requires_target and args.window_id is None:
                result["executable"] = False
                result.setdefault("preconditions", []).append({"code": "TARGET_REQUIRED", "message": "select a target window or start a bound session"})
            if args.screenshot_after:
                result["screenshot_delay_ms"] = args.screenshot_delay_ms
                result["screenshot"] = {
                    "planned": True, "window_id": args.window_id,
                    "window_source": screenshot_window_source(args),
                    "cursor_crosshair": post_action_crosshair(args),
                    "ruler": args.screenshot_ruler,
                }
        elif window_mode(args) in READ_MODES:
            operation = Cancellation(is_escape_down, enabled=not args.no_abort_key)
            operation.guard = check_selected_window
            with activity_scope(args, ACTION_STATE_PATH.parent, operation), operation_session(operation):
                result = execute_action(args)
        else:
            with ActionLock(ACTION_STATE_PATH):
                try:
                    operation = Cancellation(is_escape_down, enabled=not args.no_abort_key)
                    operation.guard = check_selected_window
                    with activity_scope(args, ACTION_STATE_PATH.parent, operation,
                                        create=window_mode(args) != "screenshot_window"), operation_session(operation):
                        configure_input_guards(args, operation)
                        if (has_keyboard_action(args) or text_source_count(args) or args.scroll_ticks is not None
                                or window_mode(args) in {"focus_only", "resize_window", "set_window_rect", "minimize_window"}):
                            action_store().invalidate("window or input action changed the target context")
                        result = execute_action_with_screenshot(args)
                except (Exception, KeyboardInterrupt):
                    try:
                        action_store().invalidate("action failed or interrupted")
                    except OSError:
                        pass
                    raise
        write_result(result)
        return 2 if result.get("aborted") else 0 if result.get("ok") else 1
    except (ActionAborted, KeyboardInterrupt) as exc:
        write_result({"ok": False, "error": "interrupted", "aborted": True, "error_code": "ABORTED",
                      "reason": getattr(exc, "reason", "keyboard-interrupt"),
                      "actual_cursor": operation.actual_cursor if operation else None,
                      "completed": operation.completed if operation else {}, "action_state": action_state_summary(),
                      **post_action_failure_details(exc)})
        return 2
    except Exception as exc:
        write_result({"ok": False, "error": str(exc), "error_code": getattr(exc, "code", "ACTION_FAILED"),
                      "required_next_step": getattr(exc, "next_step", None), "action_state": action_state_summary(),
                      "completed": operation.completed if operation else {},
                      "candidates": getattr(exc, "candidates", []),
                      "actual_cursor": operation.actual_cursor if operation else None,
                      "aborted": getattr(exc, "aborted", False), "release_errors": getattr(exc, "release_errors", []),
                      **post_action_failure_details(exc)})
        return 2 if getattr(exc, "aborted", False) else 1
    finally:
        CURRENT_ARGS = previous_args


def wait_control(args):
    started = time.monotonic()
    deadline = started + args.timeout_s
    window_id = args.window_id if args.window_id is not None else active_window_id()
    identity = window_identity(window_id)
    while True:
        check_cancelled()
        if identity != window_identity(window_id):
            raise ActionError("WINDOW_CHANGED", "window changed while waiting for a control")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ActionError("CONTROL_TIMEOUT", "no ready control appeared before timeout",
                              "check the selector or increase --timeout-s")
        metadata = enumerate_uia_controls(window_id, args.uia_include_offscreen, args.controls_limit,
                                          args.uia_max_depth, parse_csv_set(args.uia_control_types),
                                          name=args.uia_name, automation_id=args.uia_automation_id,
                                          timeout=remaining)
        matches = filter_controls(metadata, args.uia_name, args.uia_automation_id, ready_only=True)["controls"]
        if metadata.get("truncated"):
            raise ActionError("CONTROL_SEARCH_TRUNCATED", "control search was truncated",
                              "narrow the selector or increase --controls-limit")
        if matches:
            control = require_unique(matches, "CONTROL")
            return {"ok": True, "mode": "wait-control", "window_id": window_id,
                    "control": control, "elapsed_ms": int((time.monotonic() - started) * 1000)}
        interruptible_sleep(min(args.poll_interval_ms / 1000, max(0, deadline - time.monotonic())))


def resolve_window_selection(args):
    if args.list_windows:
        return
    if args.window_title is None and args.process_name is None:
        if args.window_id is not None and (has_keyboard_action(args) or text_source_count(args)) and not args.dry_run:
            args.selected_identity = window_identity(args.window_id)
        return
    candidates = filter_windows(list_windows(), args.window_title, args.process_name)
    selected = require_unique(candidates, "WINDOW")
    args.window_id = selected["id"]
    args.selected_identity = window_identity(args.window_id)
    # A window can close or change title during enumeration.
    if not filter_windows([window_snapshot(args.window_id)], args.window_title, args.process_name):
        raise ActionError("WINDOW_CHANGED", "window changed during selection")


def check_selected_window():
    identity = getattr(CURRENT_ARGS, "selected_identity", None)
    if identity and identity != window_identity(identity["window_id"]):
        raise ActionError("WINDOW_CHANGED", "selected window process changed", "select the window again")


def requires_target(args):
    return bool(has_mouse_action(args) or has_keyboard_action(args) or text_source_count(args)
                or args.focus_only or args.resize_window is not None
                or args.set_window_rect is not None or args.minimize_window)


def resolve_action_target(args):
    """Never inherit the ambient foreground for an action or replace a binding."""
    args.requires_target = requires_target(args)
    args.expected_session = None
    if args.session_start:
        if args.window_id is not None:
            args.selected_identity = window_identity(args.window_id)
        return
    if window_mode(args) in SESSION_MODES or args.list_windows or args.active_window:
        return
    session = bound_session(ACTION_STATE_PATH.parent)
    target = session.get("target") if session else None
    if args.requires_target and session:
        args.expected_session = session["session_id"]
        if target is None:
            raise ActionError("SESSION_TARGET_REQUIRED", "existing session has no target binding", "end it and use --session-start --window-id ID")
        if args.window_id is not None and args.window_id != target["window_id"]:
            raise ActionError("SESSION_TARGET_MISMATCH", "requested window differs from the session target", "end the session before selecting another window")
        args.window_id = target["window_id"]
        args.selected_identity = target.copy()
    elif args.window_id is None and target and not args.requires_target:
        args.window_id = target["window_id"]
        args.selected_identity = target.copy()
    if args.requires_target:
        if args.window_id is None:
            if args.dry_run:
                return
            raise ActionError("TARGET_REQUIRED", "actions require an explicit target window or a bound session",
                              "use --window-id ID, window selectors, or --session-start --window-id ID")
        if getattr(args, "selected_identity", None) is None:
            args.selected_identity = window_identity(args.window_id)
        check_selected_window()


def configure_input_guards(args, operation):
    if not (has_mouse_action(args) or has_keyboard_action(args) or text_source_count(args)):
        return
    window_id = args.window_id
    context = verification_window_context(window_id)

    def check_context():
        check_selected_window()
        if active_window_id() != window_id:
            raise ActionError("FOCUS_CHANGED", "target window is no longer active", "inspect the window; use --focus-only explicitly before retrying")
        if verification_window_context(window_id) != context:
            raise ActionError("WINDOW_CHANGED", "target window geometry or visibility changed during input")

    def check_input(kind, point):
        check_context()
        if kind == "mouse":
            if args.click is not None or args.double_click is not None:
                action_store().check_consumed_click(window_id)
            if root_window_at_point(cursor_position()) != window_id:
                raise ActionError("TARGET_OCCLUDED", "mouse input would reach another window")
        elif kind == "move":
            if args.drag_to is not None or args.scroll_ticks is not None:
                target = point
            else:
                target = action_store().read().get("target", {}).get("screen_target")
            if target is None or root_window_at_point(target) != window_id:
                raise ActionError("TARGET_OCCLUDED", "mouse destination is covered by another window")

    operation.target_guard = check_context
    operation.input_guard = check_input
    check_context()


def post_action_crosshair(args):
    return args.screenshot_cursor_crosshair or args.mouse_move is not None or args.mouse_move_relative is not None


def screenshot_window_source(args):
    if args.window_id is not None:
        return "selected-window"
    if any(value is not None for value in (args.mouse_move, args.mouse_move_relative, args.click, args.double_click, args.drag_to)):
        return "verified-target-window"
    return "active-window-at-action-start"


def post_action_failure_details(exc):
    if not hasattr(exc, "action_result"):
        return {}
    return {"action_completed": True, "action_result": exc.action_result,
            "failed_stage": "screenshot-after",
            "required_next_step": "inspect action_result and the current window; do not repeat the completed action"}


def execute_action_with_screenshot(args):
    if not args.screenshot_after:
        return execute_action(args)
    started = time.perf_counter()
    window_id = args.window_id
    if screenshot_window_source(args) == "verified-target-window":
        window_id = action_store().read().get("context", {}).get("window_id")
    if window_id is None:
        window_id = active_window_id()
    identity = window_identity(window_id)
    result = execute_action(args)
    if not result.get("ok") or result.get("aborted"):
        return result
    try:
        # Input guards end with the action: focus may change legitimately after
        # a hotkey or click. Capture the original HWND, never a replacement.
        def guard():
            if window_identity(window_id) != identity:
                raise ActionError("WINDOW_CHANGED", "screenshot window closed or its process changed")
        operation = current_operation()
        if operation is not None:
            operation.target_guard = None
            operation.input_guard = None
            operation.guard = guard
        guard()
        progress(args, f"Action completed. Screenshot in {args.screenshot_delay_ms} ms.")
        interruptible_sleep(args.screenshot_delay_ms / 1000)
        check_cancelled()
        guard()
        screenshot = capture_requested_screenshot(args, window_id, after_action=True)
        guard()
        if ((args.mouse_move is not None or args.mouse_move_relative is not None)
                and screenshot["action_state"].get("stage") != "cursor_verified"):
            raise ActionError("VERIFICATION_REQUIRED", "post-move screenshot did not verify the cursor")
    except (Exception, KeyboardInterrupt) as exc:
        exc.action_result = result
        raise
    return {**result, "action_completed": True, "screenshot": screenshot,
            "screenshot_delay_ms": args.screenshot_delay_ms, "action_state": action_state_summary(),
            "total_elapsed_ms": int((time.perf_counter() - started) * 1000)}


def capture_requested_screenshot(args, window_id, *, after_action=False):
    return screenshot_window(
        window_id,
        post_action_crosshair(args) if after_action else args.screenshot_cursor_crosshair,
        args.screenshot_target,
        args.coord_origin,
        args.screenshot_ruler,
        args.screenshot_ruler_margin_px,
        args.screenshot_ruler_step_px,
        args.screenshot_ruler_major_step_px,
        args.screenshot_keep_count,
        args.paired_target_screenshot_enabled,
        args.paired_target_crop_radius_px,
        args.paired_target_zoom,
        args.paired_target_ruler_step_px,
        args.paired_target_ruler_major_step_px,
        args.paired_cursor_screenshot_enabled,
        args.paired_cursor_crop_radius_px,
        args.paired_cursor_zoom,
        args.paired_cursor_ruler_step_px,
        args.paired_cursor_ruler_major_step_px,
        args.controls_overlay,
        args.controls_include_hidden,
        args.controls_limit,
        args.uia_controls_overlay,
        args.uia_include_offscreen,
        args.uia_max_depth,
        parse_csv_set(args.uia_control_types),
        args.uia_highlight_control_id,
        args.uia_highlight_automation_id,
        args.uia_highlight_name,
        args.uia_highlight_control_screenshot_enabled,
        args.uia_highlight_control_padding_px,
        args.uia_highlight_control_zoom,
        args.screenshot_drag_target,
    )


def execute_action(args):
    check_selected_window()
    if args.wait_control:
        return wait_control(args)
    if args.double_click is not None:
        return run_double_click(args)
    if args.drag_to is not None:
        return run_drag(args)
    if args.list_windows:
        windows = filter_windows(list_windows(), args.window_title, args.process_name)
        result = {
            "ok": True,
            "mode": "list-windows",
            "count": len(windows),
            "windows": windows,
        }
        return result

    if args.active_window:
        result = get_active_window()
        return result

    if args.cursor_position:
        result = cursor_position_result(args)
        return result

    if args.list_controls:
        window_id = args.window_id if args.window_id is not None else active_window_id()
        result = list_controls_result(
            window_id,
            args.controls_include_hidden,
            args.controls_limit,
        )
        return result

    if args.uia_list_controls:
        window_id = args.window_id if args.window_id is not None else active_window_id()
        result = list_uia_controls_result(
            window_id,
            args.uia_include_offscreen,
            args.controls_limit,
            args.uia_max_depth,
            parse_csv_set(args.uia_control_types),
        )
        return result

    if args.focus_only:
        result = focus_window(args.window_id)
        return result

    if args.resize_window is not None:
        width, height = args.resize_window
        window_id = args.window_id if args.window_id is not None else active_window_id()
        result = resize_window(window_id, width, height)
        return result

    if args.set_window_rect is not None:
        x, y, width, height = args.set_window_rect
        window_id = args.window_id if args.window_id is not None else active_window_id()
        result = set_window_rect(window_id, x, y, width, height)
        return result

    if args.minimize_window:
        window_id = args.window_id if args.window_id is not None else active_window_id()
        result = minimize_window(window_id)
        return result

    if args.screenshot_window:
        window_id = args.window_id if args.window_id is not None else active_window_id()
        return capture_requested_screenshot(args, window_id)

    if has_mouse_action(args):
        result = run_mouse_actions(args)
        return result

    if has_keyboard_action(args) and not text_source_count(args):
        result = run_keyboard_actions(args)
        return result

    text = read_text(args)
    result = type_text(args, text)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
