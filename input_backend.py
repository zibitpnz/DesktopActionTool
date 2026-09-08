"""Native keyboard and mouse events, cancellable motion and input tracking."""
from __future__ import annotations
import ctypes
import random
import time
from operation_runtime import (
    check_cancelled,
    interruptible_sleep,
    count_completed,
    current_operation,
)
from configuration import (
    INPUT_KEYBOARD,
    INPUT_MOUSE,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    MOUSEEVENTF_WHEEL,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    WHEEL_DELTA,
    VK_ESCAPE,
    VK_RETURN,
    VK_SHIFT,
    VK_CONTROL,
    VK_A,
)

from win32_api import KEYBDINPUT, MOUSEINPUT, INPUT_UNION, INPUT, POINT
import win32_api as api

from geometry import ease_in_out


def send_input(*inputs: INPUT) -> None:
    # Releases must remain possible after target loss, including cleanup of a
    # partially sent batch. Every batch containing new input is checked here,
    # immediately before the native API, independently of caller-side checks.
    kinds = set()
    for event in inputs:
        if event.type == INPUT_KEYBOARD and not event.union.ki.dwFlags & KEYEVENTF_KEYUP:
            kinds.add("keyboard")
        elif event.type == INPUT_MOUSE and event.union.mi.dwFlags & ~(MOUSEEVENTF_LEFTUP | MOUSEEVENTF_RIGHTUP):
            kinds.add("mouse")
    if kinds:
        check_cancelled()
        operation = current_operation()
        if operation is not None and operation.input_guard is not None:
            for kind in sorted(kinds):
                operation.input_guard(kind, None)
    array_type = INPUT * len(inputs)
    sent = api.user32.SendInput(len(inputs), array_type(*inputs), ctypes.sizeof(INPUT))
    if current_operation() is not None:
        for event in inputs[:sent]:
            if event.type == INPUT_KEYBOARD:
                key = event.union.ki
                identity = ("keyboard", key.wVk, key.wScan, bool(key.dwFlags & KEYEVENTF_UNICODE))
                if key.dwFlags & KEYEVENTF_KEYUP:
                    current_operation().held.pop(identity, None)
                else:
                    current_operation().held[identity] = INPUT(INPUT_KEYBOARD, INPUT_UNION(ki=KEYBDINPUT(
                        key.wVk, key.wScan, key.dwFlags | KEYEVENTF_KEYUP, 0, 0)))
            elif event.type == INPUT_MOUSE:
                for name, down, up in (("left", MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
                                       ("right", MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP)):
                    if event.union.mi.dwFlags & down:
                        current_operation().held[("mouse", name)] = mouse_button_event(up)
                    if event.union.mi.dwFlags & up:
                        current_operation().held.pop(("mouse", name), None)
        current_operation().count("input_events", sent)
    if sent != len(inputs):
        error = ctypes.get_last_error()
        raise OSError(error, f"SendInput sent {sent}/{len(inputs)} events")


def cursor_position() -> dict[str, int]:
    point = POINT()
    if not api.user32.GetCursorPos(ctypes.byref(point)):
        error = ctypes.get_last_error()
        raise OSError(error, "GetCursorPos failed")
    return {"x": int(point.x), "y": int(point.y)}


def set_cursor_position(x: int, y: int) -> None:
    check_cancelled()
    operation = current_operation()
    if operation is not None and operation.input_guard is not None:
        operation.input_guard("move", {"x": x, "y": y})
    if not api.user32.SetCursorPos(x, y):
        error = ctypes.get_last_error()
        raise OSError(error, f"SetCursorPos failed for x={x}, y={y}")


def smooth_set_cursor_position(
    start_x: int,
    start_y: int,
    target_x: int,
    target_y: int,
    min_duration_ms: int,
    max_duration_ms: int,
    step_delay_ms: int,
    jitter_px: int,
    jitter_stop_distance_px: int,
    slow_zone_distance_px: int,
    slow_zone_min_speed_percent: int,
) -> dict[str, int]:
    if start_x == target_x and start_y == target_y:
        set_cursor_position(target_x, target_y)
        return {
            "duration_ms": 0,
            "base_duration_ms": 0,
            "step_count": 1,
            "slow_zone_step_count": 0,
        }

    started = time.perf_counter()
    duration_ms = random.randint(min_duration_ms, max_duration_ms)
    step_count = max(2, duration_ms // max(step_delay_ms, 1))
    sleep_seconds = duration_ms / step_count / 1000
    min_speed_factor = slow_zone_min_speed_percent / 100
    slow_zone_step_count = 0

    dx = target_x - start_x
    dy = target_y - start_y
    distance = max((dx * dx + dy * dy) ** 0.5, 1)
    normal_x = -dy / distance
    normal_y = dx / distance
    curve_px = random.uniform(-0.18, 0.18) * min(distance, 320)

    for step in range(1, step_count + 1):
        check_cancelled()
        t = step / step_count
        eased = ease_in_out(t)
        curve = 4 * t * (1 - t) * curve_px

        base_x = start_x + dx * eased
        base_y = start_y + dy * eased
        distance_to_target = ((target_x - base_x) ** 2 + (target_y - base_y) ** 2) ** 0.5
        use_precision_zone = distance_to_target <= jitter_stop_distance_px
        use_slow_zone = slow_zone_distance_px > 0 and distance_to_target <= slow_zone_distance_px

        if use_precision_zone:
            x = base_x
            y = base_y
        else:
            x = base_x + normal_x * curve
            y = base_y + normal_y * curve

        if step < step_count and jitter_px > 0 and not use_precision_zone:
            x += random.uniform(-jitter_px, jitter_px)
            y += random.uniform(-jitter_px, jitter_px)

        set_cursor_position(round(x), round(y))
        count_completed("mouse_steps")
        if step < step_count:
            step_sleep_seconds = sleep_seconds
            if use_slow_zone:
                slow_zone_step_count += 1
                distance_factor = max(distance_to_target / slow_zone_distance_px, 0)
                speed_factor = max(distance_factor, min_speed_factor)
                step_sleep_seconds = sleep_seconds / speed_factor
            interruptible_sleep(step_sleep_seconds)

    set_cursor_position(target_x, target_y)
    return {
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "base_duration_ms": duration_ms,
        "step_count": step_count,
        "slow_zone_step_count": slow_zone_step_count,
    }


def mouse_wheel_event(delta: int) -> INPUT:
    mouse_data = ctypes.c_ulong(delta & 0xFFFFFFFF).value
    mouse_input = MOUSEINPUT(0, 0, mouse_data, MOUSEEVENTF_WHEEL, 0, 0)
    return INPUT(INPUT_MOUSE, INPUT_UNION(mi=mouse_input))


def mouse_button_event(flags: int) -> INPUT:
    mouse_input = MOUSEINPUT(0, 0, 0, flags, 0, 0)
    return INPUT(INPUT_MOUSE, INPUT_UNION(mi=mouse_input))


def click_mouse(button: str, min_hold_ms: int, max_hold_ms: int) -> int:
    if button == "left":
        down_flag = MOUSEEVENTF_LEFTDOWN
        up_flag = MOUSEEVENTF_LEFTUP
    elif button == "right":
        down_flag = MOUSEEVENTF_RIGHTDOWN
        up_flag = MOUSEEVENTF_RIGHTUP
    else:
        raise ValueError(f"unsupported mouse button: {button}")

    hold_ms = random.randint(min_hold_ms, max_hold_ms)
    check_cancelled()
    send_input(mouse_button_event(down_flag))
    try:
        interruptible_sleep(hold_ms / 1000)
    finally:
        send_input(mouse_button_event(up_flag))
    count_completed("clicks")
    return hold_ms


def jitter_cursor(base_x: int, base_y: int, jitter_px: int) -> None:
    if jitter_px <= 0:
        return

    dx = random.randint(-jitter_px, jitter_px)
    dy = random.randint(-jitter_px, jitter_px)
    if dx == 0 and dy == 0:
        if random.choice((True, False)):
            dx = random.choice((-1, 1))
        else:
            dy = random.choice((-1, 1))

    set_cursor_position(base_x + dx, base_y + dy)


def scroll_mouse(
    ticks: int,
    min_delay_ms: int,
    max_delay_ms: int,
    batch_size: int,
    min_batch_pause_ms: int,
    max_batch_pause_ms: int,
    jitter_enabled: bool,
    jitter_px: int,
) -> dict[str, int]:
    tick_count = abs(ticks)
    if tick_count == 0:
        return {"event_count": 0, "jitter_count": 0, "batch_count": 0}

    direction = 1 if ticks > 0 else -1
    base_cursor = cursor_position()
    jitter_count = 0
    batch_count = 0

    for batch_start in range(0, tick_count, batch_size):
        batch_count += 1
        batch_tick_count = min(batch_size, tick_count - batch_start)

        for index in range(batch_tick_count):
            check_cancelled()
            if jitter_enabled and jitter_px > 0:
                jitter_cursor(base_cursor["x"], base_cursor["y"], jitter_px)
                jitter_count += 1

            send_input(mouse_wheel_event(direction * WHEEL_DELTA))
            count_completed("scroll_events")
            if index < batch_tick_count - 1:
                delay = random.uniform(min_delay_ms, max_delay_ms) / 1000
                interruptible_sleep(delay)

        if batch_start + batch_tick_count < tick_count:
            delay = random.uniform(min_batch_pause_ms, max_batch_pause_ms) / 1000
            interruptible_sleep(delay)

    return {
        "event_count": tick_count,
        "jitter_count": jitter_count,
        "batch_count": batch_count,
    }


def key_event(vk: int, *, keyup: bool = False, extended: bool = False) -> INPUT:
    flags = (KEYEVENTF_KEYUP if keyup else 0) | (0x0001 if extended else 0)
    return INPUT(INPUT_KEYBOARD, INPUT_UNION(ki=KEYBDINPUT(vk, 0, flags, 0, 0)))


def unicode_event(char: str, *, keyup: bool = False) -> INPUT:
    if len(char) != 1 or ord(char) > 0xFFFF:
        raise ValueError("unicode_event requires one UTF-16 code unit")
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if keyup else 0)
    return INPUT(INPUT_KEYBOARD, INPUT_UNION(ki=KEYBDINPUT(0, ord(char), flags, 0, 0)))


def type_unicode_char(char: str) -> None:
    encoded = char.encode("utf-16-le")
    events = []
    for offset in range(0, len(encoded), 2):
        unit = chr(int.from_bytes(encoded[offset:offset + 2], "little"))
        events.extend((unicode_event(unit), unicode_event(unit, keyup=True)))
    # One SendInput batch prevents cooperative cancellation between surrogates.
    send_input(*events)


def press_shift_enter() -> None:
    send_input(
        key_event(VK_SHIFT),
        key_event(VK_RETURN),
        key_event(VK_RETURN, keyup=True),
        key_event(VK_SHIFT, keyup=True),
    )


def press_key(vk: int) -> None:
    send_input(key_event(vk), key_event(vk, keyup=True))


def press_ctrl_a() -> None:
    send_input(
        key_event(VK_CONTROL),
        key_event(VK_A),
        key_event(VK_A, keyup=True),
        key_event(VK_CONTROL, keyup=True),
    )


def is_escape_down() -> bool:
    return bool(api.user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)


def press_hotkey(keys):
    held = []
    try:
        for key in keys:
            check_cancelled()
            send_input(key_event(key["vk"], extended=key["extended"]))
            held.append(key)
    finally:
        for key in reversed(held):
            send_input(key_event(key["vk"], extended=key["extended"], keyup=True))
    count_completed("hotkeys")
