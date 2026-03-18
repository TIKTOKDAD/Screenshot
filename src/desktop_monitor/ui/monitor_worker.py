from __future__ import annotations

import time

from PySide6.QtCore import QObject, Signal, Slot

from desktop_monitor.core.pipeline import MonitorPipeline
from desktop_monitor.domain.models import MonitorJob


class MonitorWorker(QObject):
    log = Signal(str, str)
    status_changed = Signal(str, str)
    snapshot_ready = Signal(str, str)
    parsed_ready = Signal(str, dict)
    raw_text_ready = Signal(str, str)
    error = Signal(str, str)
    finished = Signal(str)

    def __init__(self, pipeline: MonitorPipeline, job: MonitorJob) -> None:
        super().__init__()
        self.pipeline = pipeline
        self.job = job
        self._running = False

    @Slot()
    def run(self) -> None:
        self._running = True
        self.status_changed.emit(self.job.job_id, "running")
        self.log.emit(self.job.job_id, "监控任务已启动。")

        while self._running:
            started = time.time()
            try:
                output = self.pipeline.execute(self.job)
                self.snapshot_ready.emit(self.job.job_id, output.screenshot_path)
                self.parsed_ready.emit(self.job.job_id, output.parsed_data)
                self.raw_text_ready.emit(self.job.job_id, output.raw_text)
                self.log.emit(
                    self.job.job_id,
                    f"完成采集: {output.captured_at:%Y-%m-%d %H:%M:%S} | 窗口={output.window_title}",
                )
            except Exception as exc:
                message = f"采集失败: {exc}"
                self.error.emit(self.job.job_id, message)

            next_run = started + self.job.interval_seconds
            while self._running and time.time() < next_run:
                time.sleep(0.2)

        self.status_changed.emit(self.job.job_id, "stopped")
        self.log.emit(self.job.job_id, "监控任务已停止。")
        self.finished.emit(self.job.job_id)

    def stop(self) -> None:
        self._running = False
