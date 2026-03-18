from __future__ import annotations

from typing import Protocol

from PIL import Image

from desktop_monitor.domain.models import ExtractionResult, PipelineOutput, WindowInfo


class WindowGateway(Protocol):
    def list_windows(self) -> list[WindowInfo]: ...

    def get_window(self, hwnd: int) -> WindowInfo | None: ...

    def get_window_rect(self, hwnd: int) -> tuple[int, int, int, int]: ...


class CaptureService(Protocol):
    def capture(self, hwnd: int) -> Image.Image: ...


class StructuredExtractor(Protocol):
    def extract(self, image: Image.Image) -> ExtractionResult: ...


class SnapshotRepository(Protocol):
    def save(self, output: PipelineOutput) -> None: ...
