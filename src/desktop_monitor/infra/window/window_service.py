from __future__ import annotations

from typing import Iterable

import pygetwindow as gw

from desktop_monitor.domain.models import WindowInfo


class WindowService:
    def list_windows(self) -> list[WindowInfo]:
        collected: dict[int, WindowInfo] = {}
        for window in self._safe_windows(gw.getAllWindows()):
            title = (window.title or "").strip()
            hwnd = int(getattr(window, "_hWnd", 0) or 0)
            if not title or not hwnd:
                continue
            if not self._is_visible(window):
                continue
            width = int(getattr(window, "width", 0) or 0)
            height = int(getattr(window, "height", 0) or 0)
            if width <= 0 or height <= 0:
                continue
            collected[hwnd] = WindowInfo(hwnd=hwnd, title=title)
        return sorted(collected.values(), key=lambda item: item.title.lower())

    def get_window(self, hwnd: int) -> WindowInfo | None:
        try:
            window = gw.Win32Window(hwnd)
        except Exception:
            return None

        title = (getattr(window, "title", "") or "").strip()
        if not title:
            return None
        return WindowInfo(hwnd=hwnd, title=title)

    def get_window_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        window = gw.Win32Window(hwnd)
        left = int(getattr(window, "left", 0) or 0)
        top = int(getattr(window, "top", 0) or 0)
        width = int(getattr(window, "width", 0) or 0)
        height = int(getattr(window, "height", 0) or 0)
        if width <= 0 or height <= 0:
            raise ValueError("目标窗口宽高无效，可能已最小化或不可见。")
        return left, top, width, height

    @staticmethod
    def _is_visible(window: object) -> bool:
        visible = getattr(window, "isVisible", True)
        if callable(visible):
            try:
                return bool(visible())
            except Exception:
                return False
        return bool(visible)

    @staticmethod
    def _safe_windows(items: Iterable[object]) -> Iterable[object]:
        for item in items:
            if item is None:
                continue
            yield item
