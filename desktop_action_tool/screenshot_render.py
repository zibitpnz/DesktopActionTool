"""PNG encoding, image overlays, rulers and detail crops; no Windows calls."""
from __future__ import annotations
import struct
import zlib
from pathlib import Path
from .configuration import (
    SCREENSHOT_CROSSHAIR_BGR,
    SCREENSHOT_CROSSHAIR_ALPHA,
    SCREENSHOT_CROSSHAIR_HALF_WIDTH,
    CONTROL_OVERLAY_BGR,
    CONTROL_OVERLAY_TEXT_BGR,
    CONTROL_OVERLAY_BACKGROUND_BGR,
    CONTROL_HIGHLIGHT_BGR,
    CONTROL_HIGHLIGHT_TEXT_BGR,
    CONTROL_HIGHLIGHT_BACKGROUND_BGR,
    SCREENSHOT_TARGET_BGR,
    SCREENSHOT_TARGET_ALPHA,
    SCREENSHOT_TARGET_HALF_WIDTH,
    SCREENSHOT_TARGET_RADIUS_PX,
    SCREENSHOT_TARGET_GAP_PX,
    SCREENSHOT_RULER_BACKGROUND_BGR,
    SCREENSHOT_RULER_MINOR_BGR,
    SCREENSHOT_RULER_MAJOR_BGR,
    SCREENSHOT_RULER_TEXT_BGR,
    PIXEL_FONT,
)
from .geometry import clamp

def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type)
    checksum = zlib.crc32(data, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)


def write_png(path: Path, width: int, height: int, bgra: bytes) -> None:
    rows = []
    stride = width * 4
    for y in range(height):
        source_row = bgra[y * stride : (y + 1) * stride]
        rgb = bytearray(width * 3)
        target_index = 0
        for source_index in range(0, len(source_row), 4):
            blue = source_row[source_index]
            green = source_row[source_index + 1]
            red = source_row[source_index + 2]
            rgb[target_index] = red
            rgb[target_index + 1] = green
            rgb[target_index + 2] = blue
            target_index += 3
        rows.append(b"\x00" + bytes(rgb))

    raw = b"".join(rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(raw, level=6))
        + png_chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def blend_bgra_pixel(
    pixels: bytearray,
    pixel_index: int,
    overlay_bgr: tuple[int, int, int],
    alpha: float,
) -> None:
    inverse_alpha = 1 - alpha
    pixels[pixel_index] = round(pixels[pixel_index] * inverse_alpha + overlay_bgr[0] * alpha)
    pixels[pixel_index + 1] = round(
        pixels[pixel_index + 1] * inverse_alpha + overlay_bgr[1] * alpha
    )
    pixels[pixel_index + 2] = round(
        pixels[pixel_index + 2] * inverse_alpha + overlay_bgr[2] * alpha
    )


def set_bgra_pixel(
    pixels: bytearray,
    width: int,
    height: int,
    x: int,
    y: int,
    color_bgr: tuple[int, int, int],
) -> None:
    if not (0 <= x < width and 0 <= y < height):
        return
    index = (y * width + x) * 4
    pixels[index] = color_bgr[0]
    pixels[index + 1] = color_bgr[1]
    pixels[index + 2] = color_bgr[2]
    pixels[index + 3] = 255


def blend_bgra_point(
    pixels: bytearray,
    width: int,
    height: int,
    x: int,
    y: int,
    color_bgr: tuple[int, int, int],
    alpha: float,
) -> None:
    if not (0 <= x < width and 0 <= y < height):
        return
    blend_bgra_pixel(pixels, (y * width + x) * 4, color_bgr, alpha)


def draw_horizontal_line(
    pixels: bytearray,
    width: int,
    height: int,
    y: int,
    x1: int,
    x2: int,
    color_bgr: tuple[int, int, int],
    thickness: int = 1,
) -> None:
    left = max(min(x1, x2), 0)
    right = min(max(x1, x2), width - 1)
    for offset in range(thickness):
        line_y = y + offset
        if not 0 <= line_y < height:
            continue
        for x in range(left, right + 1):
            set_bgra_pixel(pixels, width, height, x, line_y, color_bgr)


def draw_vertical_line(
    pixels: bytearray,
    width: int,
    height: int,
    x: int,
    y1: int,
    y2: int,
    color_bgr: tuple[int, int, int],
    thickness: int = 1,
) -> None:
    top = max(min(y1, y2), 0)
    bottom = min(max(y1, y2), height - 1)
    for offset in range(thickness):
        line_x = x + offset
        if not 0 <= line_x < width:
            continue
        for y in range(top, bottom + 1):
            set_bgra_pixel(pixels, width, height, line_x, y, color_bgr)


def draw_rect_outline(
    pixels: bytearray,
    width: int,
    height: int,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    color_bgr: tuple[int, int, int],
    thickness: int = 1,
) -> None:
    draw_horizontal_line(pixels, width, height, y1, x1, x2, color_bgr, thickness)
    draw_horizontal_line(pixels, width, height, y2, x1, x2, color_bgr, thickness)
    draw_vertical_line(pixels, width, height, x1, y1, y2, color_bgr, thickness)
    draw_vertical_line(pixels, width, height, x2, y1, y2, color_bgr, thickness)


def draw_filled_rect(
    pixels: bytearray,
    width: int,
    height: int,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    color_bgr: tuple[int, int, int],
) -> None:
    left = max(min(x1, x2), 0)
    right = min(max(x1, x2), width - 1)
    top = max(min(y1, y2), 0)
    bottom = min(max(y1, y2), height - 1)
    for y in range(top, bottom + 1):
        for x in range(left, right + 1):
            set_bgra_pixel(pixels, width, height, x, y, color_bgr)


def text_size(text: str, scale: int = 2) -> dict[str, int]:
    if not text:
        return {"width": 0, "height": 0}
    char_width = 3 * scale
    char_gap = scale
    return {
        "width": len(text) * char_width + max(len(text) - 1, 0) * char_gap,
        "height": 5 * scale,
    }


def draw_text(
    pixels: bytearray,
    width: int,
    height: int,
    x: int,
    y: int,
    text: str,
    color_bgr: tuple[int, int, int],
    scale: int = 2,
) -> None:
    cursor_x = x
    for char in text:
        glyph = PIXEL_FONT.get(char)
        if glyph is None:
            cursor_x += 4 * scale
            continue
        for row_index, row in enumerate(glyph):
            for col_index, value in enumerate(row):
                if value != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        set_bgra_pixel(
                            pixels,
                            width,
                            height,
                            cursor_x + col_index * scale + dx,
                            y + row_index * scale + dy,
                            color_bgr,
                        )
        cursor_x += 4 * scale


def ruler_tick_values(length: int, step_px: int, major_step_px: int) -> list[dict[str, object]]:
    values = set(range(0, length + 1, step_px))
    values.add(length)
    return [
        {"value": value, "major": value % major_step_px == 0 or value == length}
        for value in sorted(values)
    ]


def add_screenshot_ruler(
    bgra: bytes,
    width: int,
    height: int,
    margin_px: int,
    step_px: int,
    major_step_px: int,
) -> tuple[bytes, dict[str, object]]:
    output_width = width + margin_px * 2
    output_height = height + margin_px * 2
    background_pixel = bytes(
        (
            SCREENSHOT_RULER_BACKGROUND_BGR[0],
            SCREENSHOT_RULER_BACKGROUND_BGR[1],
            SCREENSHOT_RULER_BACKGROUND_BGR[2],
            255,
        )
    )
    pixels = bytearray(background_pixel * output_width * output_height)

    source_stride = width * 4
    destination_stride = output_width * 4
    for y in range(height):
        source_start = y * source_stride
        destination_start = ((y + margin_px) * output_width + margin_px) * 4
        pixels[destination_start : destination_start + source_stride] = bgra[
            source_start : source_start + source_stride
        ]

    content_left = margin_px
    content_top = margin_px
    content_right = margin_px + width
    content_bottom = margin_px + height
    draw_horizontal_line(
        pixels,
        output_width,
        output_height,
        content_top - 1,
        content_left,
        content_right,
        SCREENSHOT_RULER_MAJOR_BGR,
    )
    draw_horizontal_line(
        pixels,
        output_width,
        output_height,
        content_bottom,
        content_left,
        content_right,
        SCREENSHOT_RULER_MAJOR_BGR,
    )
    draw_vertical_line(
        pixels,
        output_width,
        output_height,
        content_left - 1,
        content_top,
        content_bottom,
        SCREENSHOT_RULER_MAJOR_BGR,
    )
    draw_vertical_line(
        pixels,
        output_width,
        output_height,
        content_right,
        content_top,
        content_bottom,
        SCREENSHOT_RULER_MAJOR_BGR,
    )

    text_height = text_size("0")["height"]
    for tick in ruler_tick_values(width, step_px, major_step_px):
        value = int(tick["value"])
        is_major = bool(tick["major"])
        image_x = margin_px + value
        tick_length = 16 if is_major else 8
        color = SCREENSHOT_RULER_MAJOR_BGR if is_major else SCREENSHOT_RULER_MINOR_BGR
        thickness = 2 if is_major else 1
        draw_vertical_line(
            pixels, output_width, output_height, image_x, content_top - tick_length, content_top, color, thickness
        )
        draw_vertical_line(
            pixels, output_width, output_height, image_x, content_bottom, content_bottom + tick_length, color, thickness
        )

        if is_major:
            label = str(value)
            size = text_size(label)
            label_x = clamp(image_x - size["width"] // 2, 2, output_width - size["width"] - 2)
            label_staggered = major_step_px > 0 and value % (major_step_px * 2) != 0
            top_label_y = 8 + (text_height + 4 if label_staggered else 0)
            bottom_label_y = output_height - text_height - 8 - (
                text_height + 4 if label_staggered else 0
            )
            draw_text(
                pixels,
                output_width,
                output_height,
                label_x,
                top_label_y,
                label,
                SCREENSHOT_RULER_TEXT_BGR,
            )
            draw_text(
                pixels,
                output_width,
                output_height,
                label_x,
                bottom_label_y,
                label,
                SCREENSHOT_RULER_TEXT_BGR,
            )

    for tick in ruler_tick_values(height, step_px, major_step_px):
        value = int(tick["value"])
        is_major = bool(tick["major"])
        image_y = margin_px + value
        tick_length = 16 if is_major else 8
        color = SCREENSHOT_RULER_MAJOR_BGR if is_major else SCREENSHOT_RULER_MINOR_BGR
        thickness = 2 if is_major else 1
        draw_horizontal_line(
            pixels, output_width, output_height, image_y, content_left - tick_length, content_left, color, thickness
        )
        draw_horizontal_line(
            pixels, output_width, output_height, image_y, content_right, content_right + tick_length, color, thickness
        )

        if is_major:
            label = str(value)
            size = text_size(label)
            label_y = clamp(image_y - size["height"] // 2, 2, output_height - size["height"] - 2)
            draw_text(
                pixels,
                output_width,
                output_height,
                max(2, content_left - tick_length - size["width"] - 4),
                label_y,
                label,
                SCREENSHOT_RULER_TEXT_BGR,
            )
            draw_text(
                pixels,
                output_width,
                output_height,
                min(output_width - size["width"] - 2, content_right + tick_length + 4),
                label_y,
                label,
                SCREENSHOT_RULER_TEXT_BGR,
            )

    metadata = {
        "enabled": True,
        "margin_px": margin_px,
        "step_px": step_px,
        "major_step_px": major_step_px,
        "color": "#00dcff",
        "content_coord_origin": "window",
        "content_image_rect": {
            "left": margin_px,
            "top": margin_px,
            "right": margin_px + width,
            "bottom": margin_px + height,
            "x": margin_px,
            "y": margin_px,
            "width": width,
            "height": height,
        },
        "image_width": output_width,
        "image_height": output_height,
    }
    return bytes(pixels), metadata


def overlay_detail_created_note(bgra: bytes, width: int, height: int) -> bytes:
    pixels = bytearray(bgra)
    label = "DETAIL"
    scale = 1
    size = text_size(label, scale)
    x = 3
    y = 2
    box_width = size["width"] + 8
    box_height = size["height"] + 8
    draw_filled_rect(
        pixels,
        width,
        height,
        x,
        y,
        x + box_width,
        y + box_height,
        SCREENSHOT_RULER_BACKGROUND_BGR,
    )
    draw_horizontal_line(
        pixels,
        width,
        height,
        y,
        x,
        x + box_width,
        SCREENSHOT_TARGET_BGR,
        1,
    )
    draw_horizontal_line(
        pixels,
        width,
        height,
        y + box_height,
        x,
        x + box_width,
        SCREENSHOT_TARGET_BGR,
        1,
    )
    draw_vertical_line(
        pixels,
        width,
        height,
        x,
        y,
        y + box_height,
        SCREENSHOT_TARGET_BGR,
        1,
    )
    draw_vertical_line(
        pixels,
        width,
        height,
        x + box_width,
        y,
        y + box_height,
        SCREENSHOT_TARGET_BGR,
        1,
    )
    draw_text(
        pixels,
        width,
        height,
        x + 4,
        y + 4,
        label,
        SCREENSHOT_RULER_TEXT_BGR,
        scale,
    )
    return bytes(pixels)


def overlay_controls(bgra: bytes, width: int, height: int, controls: list[dict[str, object]]) -> bytes:
    pixels = bytearray(bgra)
    for control in controls:
        rect = control.get("clipped_window_rect")
        if not isinstance(rect, dict):
            continue
        left = int(rect["left"])
        top = int(rect["top"])
        right = int(rect["right"]) - 1
        bottom = int(rect["bottom"]) - 1
        draw_rect_outline(
            pixels,
            width,
            height,
            left,
            top,
            right,
            bottom,
            CONTROL_OVERLAY_BGR,
            2,
        )

        label = str(control["id"])
        scale = 2
        size = text_size(label, scale)
        label_x = clamp(left + 3, 0, max(0, width - size["width"] - 8))
        label_y = clamp(top + 3, 0, max(0, height - size["height"] - 8))
        draw_filled_rect(
            pixels,
            width,
            height,
            label_x,
            label_y,
            label_x + size["width"] + 7,
            label_y + size["height"] + 7,
            CONTROL_OVERLAY_BACKGROUND_BGR,
        )
        draw_rect_outline(
            pixels,
            width,
            height,
            label_x,
            label_y,
            label_x + size["width"] + 7,
            label_y + size["height"] + 7,
            CONTROL_OVERLAY_BGR,
            1,
        )
        draw_text(
            pixels,
            width,
            height,
            label_x + 4,
            label_y + 4,
            label,
            CONTROL_OVERLAY_TEXT_BGR,
            scale,
        )
    return bytes(pixels)


def selected_control_label(control: dict[str, object]) -> str:
    return str(control.get("id", "?"))


def overlay_selected_control(
    bgra: bytes,
    width: int,
    height: int,
    control: dict[str, object],
) -> bytes:
    pixels = bytearray(bgra)
    rect = control.get("clipped_window_rect")
    if not isinstance(rect, dict):
        rect = control.get("window_rect")
    if not isinstance(rect, dict):
        return bytes(pixels)

    left = int(rect["left"])
    top = int(rect["top"])
    right = int(rect["right"]) - 1
    bottom = int(rect["bottom"]) - 1
    draw_rect_outline(
        pixels,
        width,
        height,
        left,
        top,
        right,
        bottom,
        CONTROL_HIGHLIGHT_BGR,
        4,
    )

    label = selected_control_label(control)
    scale = 3
    size = text_size(label, scale)
    label_x = clamp(left + 5, 0, max(0, width - size["width"] - 10))
    label_y = clamp(top + 5, 0, max(0, height - size["height"] - 10))
    draw_filled_rect(
        pixels,
        width,
        height,
        label_x,
        label_y,
        label_x + size["width"] + 9,
        label_y + size["height"] + 9,
        CONTROL_HIGHLIGHT_BACKGROUND_BGR,
    )
    draw_rect_outline(
        pixels,
        width,
        height,
        label_x,
        label_y,
        label_x + size["width"] + 9,
        label_y + size["height"] + 9,
        CONTROL_HIGHLIGHT_BGR,
        2,
    )
    draw_text(
        pixels,
        width,
        height,
        label_x + 5,
        label_y + 5,
        label,
        CONTROL_HIGHLIGHT_TEXT_BGR,
        scale,
    )
    return bytes(pixels)


def crop_bgra(
    bgra: bytes,
    width: int,
    crop_left: int,
    crop_top: int,
    crop_width: int,
    crop_height: int,
) -> bytes:
    source_stride = width * 4
    crop_stride = crop_width * 4
    cropped = bytearray(crop_stride * crop_height)
    for y in range(crop_height):
        source_start = ((crop_top + y) * width + crop_left) * 4
        target_start = y * crop_stride
        cropped[target_start : target_start + crop_stride] = bgra[
            source_start : source_start + crop_stride
        ]
    return bytes(cropped)


def zoom_bgra_nearest(bgra: bytes, width: int, height: int, zoom: int) -> bytes:
    output_width = width * zoom
    output_height = height * zoom
    output = bytearray(output_width * output_height * 4)
    for y in range(height):
        source_row = bgra[y * width * 4 : (y + 1) * width * 4]
        expanded_row = bytearray(output_width * 4)
        for x in range(width):
            pixel = source_row[x * 4 : x * 4 + 4]
            for dx in range(zoom):
                target_index = (x * zoom + dx) * 4
                expanded_row[target_index : target_index + 4] = pixel
        for dy in range(zoom):
            target_start = ((y * zoom + dy) * output_width) * 4
            output[target_start : target_start + len(expanded_row)] = expanded_row
    return bytes(output)


def detail_tick_values(start: int, end: int, step_px: int) -> list[int]:
    first = ((start + step_px - 1) // step_px) * step_px
    return list(range(first, end + 1, step_px))


def add_target_detail_ruler(
    bgra: bytes,
    width: int,
    height: int,
    margin_px: int,
    crop_left: int,
    crop_top: int,
    crop_width: int,
    crop_height: int,
    zoom: int,
    step_px: int,
    major_step_px: int,
) -> tuple[bytes, dict[str, object]]:
    output_width = width + margin_px * 2
    output_height = height + margin_px * 2
    background_pixel = bytes(
        (
            SCREENSHOT_RULER_BACKGROUND_BGR[0],
            SCREENSHOT_RULER_BACKGROUND_BGR[1],
            SCREENSHOT_RULER_BACKGROUND_BGR[2],
            255,
        )
    )
    pixels = bytearray(background_pixel * output_width * output_height)

    source_stride = width * 4
    for y in range(height):
        source_start = y * source_stride
        destination_start = ((y + margin_px) * output_width + margin_px) * 4
        pixels[destination_start : destination_start + source_stride] = bgra[
            source_start : source_start + source_stride
        ]

    content_left = margin_px
    content_top = margin_px
    content_right = margin_px + width
    content_bottom = margin_px + height
    draw_horizontal_line(
        pixels, output_width, output_height, content_top - 1, content_left, content_right, SCREENSHOT_RULER_MAJOR_BGR
    )
    draw_horizontal_line(
        pixels, output_width, output_height, content_bottom, content_left, content_right, SCREENSHOT_RULER_MAJOR_BGR
    )
    draw_vertical_line(
        pixels, output_width, output_height, content_left - 1, content_top, content_bottom, SCREENSHOT_RULER_MAJOR_BGR
    )
    draw_vertical_line(
        pixels, output_width, output_height, content_right, content_top, content_bottom, SCREENSHOT_RULER_MAJOR_BGR
    )

    text_height = text_size("0")["height"]
    crop_right = crop_left + crop_width - 1
    crop_bottom = crop_top + crop_height - 1
    for value in detail_tick_values(crop_left, crop_right, step_px):
        is_major = value % major_step_px == 0 or value in (crop_left, crop_right)
        image_x = margin_px + (value - crop_left) * zoom
        tick_length = 16 if is_major else 8
        color = SCREENSHOT_RULER_MAJOR_BGR if is_major else SCREENSHOT_RULER_MINOR_BGR
        thickness = 2 if is_major else 1
        draw_vertical_line(
            pixels, output_width, output_height, image_x, content_top - tick_length, content_top, color, thickness
        )
        draw_vertical_line(
            pixels, output_width, output_height, image_x, content_bottom, content_bottom + tick_length, color, thickness
        )
        if is_major:
            label = str(value)
            size = text_size(label)
            label_x = clamp(image_x - size["width"] // 2, 2, output_width - size["width"] - 2)
            label_staggered = major_step_px > 0 and value % (major_step_px * 2) != 0
            top_label_y = 8 + (text_height + 4 if label_staggered else 0)
            bottom_label_y = output_height - text_height - 8 - (
                text_height + 4 if label_staggered else 0
            )
            draw_text(pixels, output_width, output_height, label_x, top_label_y, label, SCREENSHOT_RULER_TEXT_BGR)
            draw_text(pixels, output_width, output_height, label_x, bottom_label_y, label, SCREENSHOT_RULER_TEXT_BGR)

    for value in detail_tick_values(crop_top, crop_bottom, step_px):
        is_major = value % major_step_px == 0 or value in (crop_top, crop_bottom)
        image_y = margin_px + (value - crop_top) * zoom
        tick_length = 16 if is_major else 8
        color = SCREENSHOT_RULER_MAJOR_BGR if is_major else SCREENSHOT_RULER_MINOR_BGR
        thickness = 2 if is_major else 1
        draw_horizontal_line(
            pixels, output_width, output_height, image_y, content_left - tick_length, content_left, color, thickness
        )
        draw_horizontal_line(
            pixels, output_width, output_height, image_y, content_right, content_right + tick_length, color, thickness
        )
        if is_major:
            label = str(value)
            size = text_size(label)
            label_y = clamp(image_y - size["height"] // 2, 2, output_height - size["height"] - 2)
            draw_text(
                pixels,
                output_width,
                output_height,
                max(2, content_left - tick_length - size["width"] - 4),
                label_y,
                label,
                SCREENSHOT_RULER_TEXT_BGR,
            )
            draw_text(
                pixels,
                output_width,
                output_height,
                min(output_width - size["width"] - 2, content_right + tick_length + 4),
                label_y,
                label,
                SCREENSHOT_RULER_TEXT_BGR,
            )

    metadata = {
        "enabled": True,
        "margin_px": margin_px,
        "step_px": step_px,
        "major_step_px": major_step_px,
        "zoom": zoom,
        "content_coord_origin": "window",
        "crop_rect": {
            "left": crop_left,
            "top": crop_top,
            "right": crop_right,
            "bottom": crop_bottom,
            "x": crop_left,
            "y": crop_top,
            "width": crop_width,
            "height": crop_height,
        },
        "content_image_rect": {
            "left": margin_px,
            "top": margin_px,
            "right": margin_px + width,
            "bottom": margin_px + height,
            "x": margin_px,
            "y": margin_px,
            "width": width,
            "height": height,
        },
        "image_width": output_width,
        "image_height": output_height,
    }
    return bytes(pixels), metadata


def create_target_detail_screenshot(
    original_pixels: bytes,
    window_width: int,
    window_height: int,
    window_target: dict[str, int],
    crop_radius_px: int,
    zoom: int,
    ruler_step_px: int,
    ruler_major_step_px: int,
) -> tuple[bytes, dict[str, object]]:
    target_x = int(window_target["x"])
    target_y = int(window_target["y"])
    crop_left = max(0, target_x - crop_radius_px)
    crop_top = max(0, target_y - crop_radius_px)
    crop_right = min(window_width - 1, target_x + crop_radius_px)
    crop_bottom = min(window_height - 1, target_y + crop_radius_px)
    crop_width = crop_right - crop_left + 1
    crop_height = crop_bottom - crop_top + 1

    cropped = crop_bgra(original_pixels, window_width, crop_left, crop_top, crop_width, crop_height)
    zoomed = zoom_bgra_nearest(cropped, crop_width, crop_height, zoom)
    zoomed_width = crop_width * zoom
    zoomed_height = crop_height * zoom
    target_in_zoom = {
        "x": (target_x - crop_left) * zoom + zoom // 2,
        "y": (target_y - crop_top) * zoom + zoom // 2,
    }
    scaled_target_radius_px = SCREENSHOT_TARGET_RADIUS_PX * zoom
    zoomed = overlay_target_crosshair(
        zoomed,
        zoomed_width,
        zoomed_height,
        target_in_zoom["x"],
        target_in_zoom["y"],
        scaled_target_radius_px,
    )
    output, ruler_metadata = add_target_detail_ruler(
        zoomed,
        zoomed_width,
        zoomed_height,
        64,
        crop_left,
        crop_top,
        crop_width,
        crop_height,
        zoom,
        ruler_step_px,
        ruler_major_step_px,
    )
    metadata = {
        "enabled": True,
        "created": True,
        "crop_radius_px": crop_radius_px,
        "zoom": zoom,
        "ruler_step_px": ruler_step_px,
        "ruler_major_step_px": ruler_major_step_px,
        "target_radius_px": scaled_target_radius_px,
        "target_radius_source_px": SCREENSHOT_TARGET_RADIUS_PX,
        "target": {"x": target_x, "y": target_y},
        "target_in_zoomed_content": target_in_zoom,
        "ruler": ruler_metadata,
    }
    return output, metadata


def create_uia_highlight_control_screenshot(
    original_pixels: bytes,
    window_width: int,
    window_height: int,
    control: dict[str, object],
    padding_px: int,
    zoom: int,
) -> tuple[bytes, dict[str, object]]:
    rect = control.get("clipped_window_rect")
    if not isinstance(rect, dict):
        rect = control.get("window_rect")
    if not isinstance(rect, dict):
        raise ValueError("selected UIA control has no rectangle metadata")

    control_left = int(rect["left"])
    control_top = int(rect["top"])
    control_right = int(rect["right"]) - 1
    control_bottom = int(rect["bottom"]) - 1
    if control_right < control_left or control_bottom < control_top:
        raise ValueError("selected UIA control has an empty rectangle")

    crop_left = max(0, control_left - padding_px)
    crop_top = max(0, control_top - padding_px)
    crop_right = min(window_width - 1, control_right + padding_px)
    crop_bottom = min(window_height - 1, control_bottom + padding_px)
    crop_width = crop_right - crop_left + 1
    crop_height = crop_bottom - crop_top + 1

    cropped = crop_bgra(original_pixels, window_width, crop_left, crop_top, crop_width, crop_height)
    zoomed = zoom_bgra_nearest(cropped, crop_width, crop_height, zoom)
    zoomed_width = crop_width * zoom
    zoomed_height = crop_height * zoom

    control_in_crop = {
        "left": control_left - crop_left,
        "top": control_top - crop_top,
        "right": control_right - crop_left + 1,
        "bottom": control_bottom - crop_top + 1,
        "x": control_left - crop_left,
        "y": control_top - crop_top,
        "width": control_right - control_left + 1,
        "height": control_bottom - control_top + 1,
    }
    control_in_zoom = {
        "left": control_in_crop["left"] * zoom,
        "top": control_in_crop["top"] * zoom,
        "right": control_in_crop["right"] * zoom,
        "bottom": control_in_crop["bottom"] * zoom,
        "x": control_in_crop["left"] * zoom,
        "y": control_in_crop["top"] * zoom,
        "width": control_in_crop["width"] * zoom,
        "height": control_in_crop["height"] * zoom,
    }
    zoomed_pixels = bytearray(zoomed)
    draw_rect_outline(
        zoomed_pixels,
        zoomed_width,
        zoomed_height,
        control_in_zoom["left"],
        control_in_zoom["top"],
        control_in_zoom["right"] - 1,
        control_in_zoom["bottom"] - 1,
        CONTROL_HIGHLIGHT_BGR,
        max(2, min(4, zoom)),
    )

    metadata = {
        "enabled": True,
        "created": True,
        "padding_px": padding_px,
        "zoom": zoom,
        "content_coord_origin": "window",
        "crop_rect": {
            "left": crop_left,
            "top": crop_top,
            "right": crop_right,
            "bottom": crop_bottom,
            "x": crop_left,
            "y": crop_top,
            "width": crop_width,
            "height": crop_height,
        },
        "control_rect": {
            "left": control_left,
            "top": control_top,
            "right": control_right + 1,
            "bottom": control_bottom + 1,
            "x": control_left,
            "y": control_top,
            "width": control_right - control_left + 1,
            "height": control_bottom - control_top + 1,
        },
        "control_rect_in_crop": control_in_crop,
        "control_rect_in_zoomed_content": control_in_zoom,
        "image_width": zoomed_width,
        "image_height": zoomed_height,
        "clean_content": True,
        "target_overlay": False,
    }
    return bytes(zoomed_pixels), metadata


def overlay_cursor_crosshair(
    bgra: bytes,
    width: int,
    height: int,
    cursor_x: int,
    cursor_y: int,
) -> bytes:
    pixels = bytearray(bgra)
    stride = width * 4

    for offset in range(-SCREENSHOT_CROSSHAIR_HALF_WIDTH, SCREENSHOT_CROSSHAIR_HALF_WIDTH + 1):
        y = cursor_y + offset
        if 0 <= y < height:
            row_start = y * stride
            for x in range(width):
                blend_bgra_pixel(
                    pixels,
                    row_start + x * 4,
                    SCREENSHOT_CROSSHAIR_BGR,
                    SCREENSHOT_CROSSHAIR_ALPHA,
                )

        x = cursor_x + offset
        if 0 <= x < width:
            for y in range(height):
                blend_bgra_pixel(
                    pixels,
                    y * stride + x * 4,
                    SCREENSHOT_CROSSHAIR_BGR,
                    SCREENSHOT_CROSSHAIR_ALPHA,
                )

    return bytes(pixels)


def create_cursor_detail_screenshot(
    original_pixels: bytes,
    window_width: int,
    window_height: int,
    window_cursor: dict[str, int],
    crop_radius_px: int,
    zoom: int,
    ruler_step_px: int,
    ruler_major_step_px: int,
) -> tuple[bytes, dict[str, object]]:
    cursor_x = int(window_cursor["x"])
    cursor_y = int(window_cursor["y"])
    crop_left = max(0, cursor_x - crop_radius_px)
    crop_top = max(0, cursor_y - crop_radius_px)
    crop_right = min(window_width - 1, cursor_x + crop_radius_px)
    crop_bottom = min(window_height - 1, cursor_y + crop_radius_px)
    crop_width = crop_right - crop_left + 1
    crop_height = crop_bottom - crop_top + 1

    cropped = crop_bgra(original_pixels, window_width, crop_left, crop_top, crop_width, crop_height)
    zoomed = zoom_bgra_nearest(cropped, crop_width, crop_height, zoom)
    zoomed_width = crop_width * zoom
    zoomed_height = crop_height * zoom
    cursor_in_zoom = {
        "x": (cursor_x - crop_left) * zoom + zoom // 2,
        "y": (cursor_y - crop_top) * zoom + zoom // 2,
    }
    zoomed = overlay_cursor_crosshair(
        zoomed,
        zoomed_width,
        zoomed_height,
        cursor_in_zoom["x"],
        cursor_in_zoom["y"],
    )
    output, ruler_metadata = add_target_detail_ruler(
        zoomed,
        zoomed_width,
        zoomed_height,
        64,
        crop_left,
        crop_top,
        crop_width,
        crop_height,
        zoom,
        ruler_step_px,
        ruler_major_step_px,
    )
    metadata = {
        "enabled": True,
        "created": True,
        "crop_radius_px": crop_radius_px,
        "zoom": zoom,
        "ruler_step_px": ruler_step_px,
        "ruler_major_step_px": ruler_major_step_px,
        "cursor": {"x": cursor_x, "y": cursor_y},
        "cursor_in_zoomed_content": cursor_in_zoom,
        "color": "#00dcff",
        "alpha": SCREENSHOT_CROSSHAIR_ALPHA,
        "line_width": SCREENSHOT_CROSSHAIR_HALF_WIDTH * 2 + 1,
        "ruler": ruler_metadata,
    }
    return output, metadata


def overlay_target_crosshair(
    bgra: bytes,
    width: int,
    height: int,
    target_x: int,
    target_y: int,
    radius_px: int = SCREENSHOT_TARGET_RADIUS_PX,
    gap_px: int = SCREENSHOT_TARGET_GAP_PX,
    half_width: int = SCREENSHOT_TARGET_HALF_WIDTH,
) -> bytes:
    pixels = bytearray(bgra)
    radius_px = max(0, int(radius_px))
    gap_px = max(0, int(gap_px))
    half_width = max(0, int(half_width))
    gap = radius_px + gap_px

    for offset in range(-half_width, half_width + 1):
        y = target_y + offset
        if 0 <= y < height:
            for x in range(width):
                if abs(x - target_x) <= gap:
                    continue
                blend_bgra_point(
                    pixels,
                    width,
                    height,
                    x,
                    y,
                    SCREENSHOT_TARGET_BGR,
                    SCREENSHOT_TARGET_ALPHA,
                )

        x = target_x + offset
        if 0 <= x < width:
            for y in range(height):
                if abs(y - target_y) <= gap:
                    continue
                blend_bgra_point(
                    pixels,
                    width,
                    height,
                    x,
                    y,
                    SCREENSHOT_TARGET_BGR,
                    SCREENSHOT_TARGET_ALPHA,
                )

    inner_radius = max(0, radius_px - half_width)
    outer_radius = radius_px + half_width
    inner_squared = inner_radius * inner_radius
    outer_squared = outer_radius * outer_radius
    for y in range(target_y - outer_radius, target_y + outer_radius + 1):
        if not 0 <= y < height:
            continue
        dy = y - target_y
        for x in range(target_x - outer_radius, target_x + outer_radius + 1):
            if not 0 <= x < width:
                continue
            dx = x - target_x
            distance_squared = dx * dx + dy * dy
            if inner_squared <= distance_squared <= outer_squared:
                blend_bgra_point(
                    pixels,
                    width,
                    height,
                    x,
                    y,
                    SCREENSHOT_TARGET_BGR,
                    SCREENSHOT_TARGET_ALPHA,
                )

    return bytes(pixels)
