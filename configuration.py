"""Default settings and rendering constants."""

INPUT_KEYBOARD = 1

INPUT_MOUSE = 0

KEYEVENTF_KEYUP = 0x0002

KEYEVENTF_UNICODE = 0x0004

MOUSEEVENTF_WHEEL = 0x0800

MOUSEEVENTF_LEFTDOWN = 0x0002

MOUSEEVENTF_LEFTUP = 0x0004

MOUSEEVENTF_RIGHTDOWN = 0x0008

MOUSEEVENTF_RIGHTUP = 0x0010

WHEEL_DELTA = 120

VK_ESCAPE = 0x1B

VK_RETURN = 0x0D

VK_SHIFT = 0x10

VK_CONTROL = 0x11

VK_BACK = 0x08

VK_DELETE = 0x2E

VK_A = 0x41

KEY_ACTION_PAUSE_SECONDS = 0.05

DEFAULT_SETTINGS = {
    "verification_ttl_seconds": 120,
    "verification_tolerance_px": 0,
    "min_delay_ms": 50,
    "max_delay_ms": 100,
    "min_scroll_delay_ms": 20,
    "max_scroll_delay_ms": 80,
    "scroll_batch_size": 10,
    "min_scroll_batch_pause_ms": 250,
    "max_scroll_batch_pause_ms": 700,
    "scroll_jitter_px": 2,
    "min_click_hold_ms": 35,
    "max_click_hold_ms": 90,
    "min_mouse_move_duration_ms": 350,
    "max_mouse_move_duration_ms": 900,
    "mouse_move_step_delay_ms": 12,
    "mouse_move_jitter_px": 10,
    "mouse_move_jitter_stop_distance_px": 50,
    "mouse_move_slow_zone_distance_px": 50,
    "mouse_move_slow_zone_min_speed_percent": 25,
    "screenshot_keep_count": 10,
    "screenshot_delay_ms": 500,
    "activity_frame_enabled": 1,
    "activity_frame_width_px": 12,
    "activity_frame_gradient_enabled": 1,
    "activity_frame_opacity_percent": 90,
    "activity_frame_lead_ms": 500,
    "activity_session_timeout_s": 120,
    "paired_target_screenshot_enabled": 0,
    "paired_target_crop_radius_px": 40,
    "paired_target_zoom": 8,
    "paired_target_ruler_step_px": 5,
    "paired_target_ruler_major_step_px": 5,
    "paired_cursor_screenshot_enabled": 0,
    "paired_cursor_crop_radius_px": 40,
    "paired_cursor_zoom": 8,
    "paired_cursor_ruler_step_px": 5,
    "paired_cursor_ruler_major_step_px": 5,
    "uia_highlight_control_screenshot_enabled": 1,
    "uia_highlight_control_padding_px": 8,
    "uia_highlight_control_zoom": 4,
}

SW_SHOW = 5

SW_MINIMIZE = 6

SW_RESTORE = 9

SWP_NOMOVE = 0x0002

SWP_NOZORDER = 0x0004

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

BI_RGB = 0

DIB_RGB_COLORS = 0

SRCCOPY = 0x00CC0020

CAPTUREBLT = 0x40000000

SCREENSHOT_CROSSHAIR_BGR = (255, 220, 0)

SCREENSHOT_CROSSHAIR_ALPHA = 0.45

SCREENSHOT_CROSSHAIR_HALF_WIDTH = 1

CONTROL_OVERLAY_BGR = (0, 210, 255)

CONTROL_OVERLAY_TEXT_BGR = (255, 255, 255)

CONTROL_OVERLAY_BACKGROUND_BGR = (40, 40, 40)

CONTROL_OVERLAY_MAX_DEFAULT = 120

CONTROL_HIGHLIGHT_BGR = (60, 255, 70)

CONTROL_HIGHLIGHT_TEXT_BGR = (255, 255, 255)

CONTROL_HIGHLIGHT_BACKGROUND_BGR = (20, 60, 20)

SCREENSHOT_TARGET_BGR = (0, 0, 255)

SCREENSHOT_TARGET_ALPHA = 0.45

SCREENSHOT_TARGET_HALF_WIDTH = 2

SCREENSHOT_TARGET_RADIUS_PX = 19

SCREENSHOT_TARGET_GAP_PX = 4

SCREENSHOT_RULER_MARGIN_PX = 48

SCREENSHOT_RULER_STEP_PX = 50

SCREENSHOT_RULER_MAJOR_STEP_PX = 50

SCREENSHOT_RULER_BACKGROUND_BGR = (42, 42, 42)

SCREENSHOT_RULER_MINOR_BGR = (135, 135, 135)

SCREENSHOT_RULER_MAJOR_BGR = SCREENSHOT_CROSSHAIR_BGR

SCREENSHOT_RULER_TEXT_BGR = (235, 235, 235)

PIXEL_FONT = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    "-": ("000", "000", "111", "000", "000"),
    "A": ("010", "101", "111", "101", "101"),
    "C": ("111", "100", "100", "100", "111"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "110", "100", "111"),
    "I": ("111", "010", "010", "010", "111"),
    "L": ("100", "100", "100", "100", "111"),
    "R": ("110", "101", "110", "101", "101"),
    "T": ("111", "010", "010", "010", "010"),
}
