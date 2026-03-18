from __future__ import annotations

from datetime import datetime
from pathlib import Path

from desktop_monitor.core.contracts import CaptureService, SnapshotRepository, StructuredExtractor, WindowGateway
from desktop_monitor.core.image_adjustments import apply_job_capture_adjustments
from desktop_monitor.domain.models import MonitorJob, PipelineOutput


class MonitorPipeline:
    def __init__(
        self,
        window_gateway: WindowGateway,
        capture_service: CaptureService,
        extractor: StructuredExtractor,
        snapshot_repository: SnapshotRepository,
    ) -> None:
        self.window_gateway = window_gateway
        self.capture_service = capture_service
        self.extractor = extractor
        self.snapshot_repository = snapshot_repository

    def execute(self, job: MonitorJob) -> PipelineOutput:
        window = self.window_gateway.get_window(job.window_hwnd)
        if window is None:
            raise RuntimeError(f"??[{job.name or job.job_id}]??????????????")

        image = self.capture_service.capture(job.window_hwnd)
        image = apply_job_capture_adjustments(image, job)
        captured_at = datetime.now()

        output_dir = Path(job.screenshot_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{captured_at:%Y%m%d_%H%M%S}_{job.job_id}_{job.window_hwnd}.png"
        screenshot_path = output_dir / filename
        image.save(screenshot_path)

        extraction = self.extractor.extract(image)

        output = PipelineOutput(
            job_id=job.job_id,
            job_name=job.name,
            captured_at=captured_at,
            window_hwnd=job.window_hwnd,
            window_title=window.title,
            screenshot_path=str(screenshot_path.resolve()),
            raw_text=extraction.raw_text,
            parsed_data=extraction.parsed_data,
            parse_mode=job.parse_mode,
            gateway_protocol=extraction.gateway_protocol,
            model_name=extraction.model_name,
            attempt_count=extraction.attempt_count,
            validation_errors=extraction.validation_errors,
        )
        self.snapshot_repository.save(output)
        return output
