from __future__ import annotations

import mss
from PIL import Image

from desktop_monitor.core.contracts import CaptureService
from desktop_monitor.infra.window.window_service import WindowService


class WindowCaptureService(CaptureService):
    def __init__(self, window_service: WindowService) -> None:
        self.window_service = window_service

    def capture(self, hwnd: int) -> Image.Image:
        left, top, width, height = self.window_service.get_window_rect(hwnd)
        monitor = {
            "left": left,
            "top": top,
            "width": width,
            "height": height,
        }
        with mss.mss() as sct:
            shot = sct.grab(monitor)
            image = Image.frombytes("RGB", shot.size, shot.rgb)
        return image
