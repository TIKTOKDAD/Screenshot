from __future__ import annotations

import json
from pathlib import Path

from desktop_monitor.domain.models import AiGatewayConfig, AppSettings


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (Path.home() / ".desktop_monitor" / "settings.json")
        self.ai_config_path = self.path.with_name("ai_config.json")

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = settings.to_dict()
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self) -> AppSettings | None:
        if not self.path.exists():
            return None

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        if not isinstance(data, dict):
            return None

        if "jobs" in data:
            return AppSettings.from_dict(data)

        # Backward compatibility for single-task legacy config.
        if "window_hwnd" in data:
            return AppSettings.from_legacy_monitor_dict(data)

        return None

    def save_ai_config(self, ai_config: AiGatewayConfig) -> None:
        self.ai_config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = ai_config.to_dict()
        self.ai_config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_ai_config(self) -> AiGatewayConfig | None:
        if not self.ai_config_path.exists():
            return None

        try:
            data = json.loads(self.ai_config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        if not isinstance(data, dict):
            return None

        return AiGatewayConfig.from_dict(data)
