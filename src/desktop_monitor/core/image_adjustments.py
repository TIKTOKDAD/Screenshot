from __future__ import annotations

from PIL import Image, ImageDraw

from desktop_monitor.domain.models import MonitorJob


def _sanitize_rect(rect: tuple[int, int, int, int] | None) -> tuple[int, int, int, int] | None:
    if rect is None:
        return None

    left, top, right, bottom = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def _normalize_rect(rect: tuple[int, int, int, int] | None, width: int, height: int) -> tuple[int, int, int, int] | None:
    if rect is None or width <= 0 or height <= 0:
        return None

    left, top, right, bottom = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
    left = max(0, min(width - 1, left))
    top = max(0, min(height - 1, top))
    right = max(left + 1, min(width, right))
    bottom = max(top + 1, min(height, bottom))

    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def _background_fill(mode: str):
    if mode == "RGB":
        return (0, 0, 0)
    if mode == "RGBA":
        return (0, 0, 0, 255)
    return 0


def apply_job_capture_adjustments(image: Image.Image, job: MonitorJob) -> Image.Image:
    result = image.copy()

    source_w, source_h = result.size
    requested_crop = _sanitize_rect(job.crop_rect)
    crop_rect = _normalize_rect(requested_crop, source_w, source_h)
    offset_x = 0
    offset_y = 0
    if requested_crop is not None:
        req_left, req_top, req_right, req_bottom = requested_crop
        target_w = req_right - req_left
        target_h = req_bottom - req_top

        canvas = Image.new(result.mode, (target_w, target_h), color=_background_fill(result.mode))
        if crop_rect is not None:
            src_left, src_top, src_right, src_bottom = crop_rect
            cropped = result.crop((src_left, src_top, src_right, src_bottom))
            paste_x = src_left - req_left
            paste_y = src_top - req_top
            canvas.paste(cropped, (paste_x, paste_y))
        result = canvas
        offset_x = req_left
        offset_y = req_top

    if not job.mark_rects:
        return result

    draw = ImageDraw.Draw(result)
    result_w, result_h = result.size
    pen_width = max(2, min(result_w, result_h) // 180)

    for rect in job.mark_rects:
        normalized = _normalize_rect(rect, source_w, source_h)
        if normalized is None:
            continue

        left, top, right, bottom = normalized
        left -= offset_x
        right -= offset_x
        top -= offset_y
        bottom -= offset_y

        left = max(0, min(result_w - 1, left))
        top = max(0, min(result_h - 1, top))
        right = max(left + 1, min(result_w, right))
        bottom = max(top + 1, min(result_h, bottom))

        draw.rectangle([(left, top), (right, bottom)], outline="#d00000", width=pen_width)

    return result
