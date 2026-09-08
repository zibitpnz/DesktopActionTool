"""Pure geometry calculations in physical pixels."""
from __future__ import annotations

def rect_from_points(left: int, top: int, right: int, bottom: int) -> dict[str, int]:
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


def intersect_rects(first: dict[str, int], second: dict[str, int]) -> dict[str, int] | None:
    left = max(first["left"], second["left"])
    top = max(first["top"], second["top"])
    right = min(first["right"], second["right"])
    bottom = min(first["bottom"], second["bottom"])
    if right <= left or bottom <= top:
        return None
    return rect_from_points(left, top, right, bottom)


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(value, maximum))


def ease_in_out(t: float) -> float:
    if t < 0.5:
        return 4 * t * t * t
    return 1 - pow(-2 * t + 2, 3) / 2
