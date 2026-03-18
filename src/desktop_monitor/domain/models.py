from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
import uuid

MappingSource = Literal["parsed", "system", "constant"]
ParseMode = Literal["ai_structured"]
GatewayProtocol = Literal["chat_completions", "responses"]
SchemaDraftType = Literal["TEXT", "INTEGER", "FLOAT", "BOOLEAN", "DATETIME", "JSON"]

DEFAULT_AI_SYSTEM_PROMPT = (
    "你是桌面数据抽取助手。只能根据截图和用户提示进行判断。"
    "只返回一个 JSON 对象，不要输出额外解释或 Markdown。"
)
DEFAULT_AI_USER_PROMPT = (
    "请从这张桌面截图中提取我关心的业务数据。"
    "如果某个字段缺失或无法确定，请返回 null，不要猜测。"
)


@dataclass(slots=True, frozen=True)
class WindowInfo:
    hwnd: int
    title: str


@dataclass(slots=True)
class DbFieldMapping:
    source_key: str
    db_column: str
    source_type: MappingSource = "parsed"
    constant_value: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "db_column": self.db_column,
            "source_type": self.source_type,
            "constant_value": self.constant_value,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "DbFieldMapping":
        source_type = str(data.get("source_type", "parsed") or "parsed").strip()
        if source_type not in {"parsed", "system", "constant"}:
            source_type = "parsed"
        return DbFieldMapping(
            source_key=str(data.get("source_key", "")).strip(),
            db_column=str(data.get("db_column", "")).strip(),
            source_type=source_type,  # type: ignore[arg-type]
            constant_value=str(data.get("constant_value", "") or ""),
        )


@dataclass(slots=True)
class AiGatewayConfig:
    protocol: GatewayProtocol = "responses"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = ""
    system_prompt: str = DEFAULT_AI_SYSTEM_PROMPT
    user_prompt: str = DEFAULT_AI_USER_PROMPT
    enable_advanced_options: bool = False
    enable_generation_controls: bool = False
    enable_output_schema: bool = False
    image_detail: str = ""
    output_schema_text: str = ""
    validation_rules_text: str = ""
    max_validation_retries: int = 2
    timeout_seconds: int = 90
    temperature: float = 0.1
    max_output_tokens: int = 1800

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "user_prompt": self.user_prompt,
            "enable_advanced_options": self.enable_advanced_options,
            "enable_generation_controls": self.enable_generation_controls,
            "enable_output_schema": self.enable_output_schema,
            "image_detail": self.image_detail,
            "output_schema_text": self.output_schema_text,
            "validation_rules_text": self.validation_rules_text,
            "max_validation_retries": self.max_validation_retries,
            "timeout_seconds": self.timeout_seconds,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "AiGatewayConfig":
        if not isinstance(data, dict):
            return AiGatewayConfig()
        detail = str(data.get("image_detail", "") or "").strip().lower()
        if detail not in {"", "low", "high", "auto"}:
            detail = ""
        return AiGatewayConfig(
            protocol=normalize_gateway_protocol(data.get("protocol")),
            base_url=str(data.get("base_url", "https://api.openai.com/v1") or "https://api.openai.com/v1").strip(),
            api_key=str(data.get("api_key", "") or ""),
            model=str(data.get("model", "") or "").strip(),
            system_prompt=str(data.get("system_prompt", DEFAULT_AI_SYSTEM_PROMPT) or DEFAULT_AI_SYSTEM_PROMPT),
            user_prompt=str(data.get("user_prompt", DEFAULT_AI_USER_PROMPT) or DEFAULT_AI_USER_PROMPT),
            enable_advanced_options=bool(data.get("enable_advanced_options", False)),
            enable_generation_controls=bool(data.get("enable_generation_controls", False)),
            enable_output_schema=bool(data.get("enable_output_schema", False)),
            image_detail=detail,
            output_schema_text=str(data.get("output_schema_text", "") or ""),
            validation_rules_text=str(data.get("validation_rules_text", "") or ""),
            max_validation_retries=max(int(data.get("max_validation_retries", 2) or 2), 0),
            timeout_seconds=max(int(data.get("timeout_seconds", 90) or 90), 5),
            temperature=max(float(data.get("temperature", 0.1) or 0.1), 0.0),
            max_output_tokens=max(int(data.get("max_output_tokens", 1800) or 1800), 128),
        )


@dataclass(slots=True)
class SchemaFieldDraft:
    source_key: str
    column_name: str
    json_type: str
    db_type: SchemaDraftType
    nullable: bool = True
    include: bool = True
    sample_value: str = ""
    present_count: int = 0


@dataclass(slots=True)
class SampleExtractionResult:
    sample_index: int
    screenshot_path: str
    raw_text: str
    parsed_data: dict[str, Any]
    validation_errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExtractionResult:
    raw_text: str
    parsed_data: dict[str, Any]
    validation_errors: list[str] = field(default_factory=list)
    attempt_count: int = 1
    gateway_protocol: str = ""
    model_name: str = ""


def create_job_id() -> str:
    return uuid.uuid4().hex[:8]


def normalize_parse_mode(value: str | None) -> ParseMode:
    # Keep accepting historical config values, but project runtime is AI-only.
    _ = value
    return "ai_structured"


def normalize_gateway_protocol(value: str | None) -> GatewayProtocol:
    protocol = str(value or "responses").strip().lower() or "responses"
    if protocol not in {"chat_completions", "responses"}:
        protocol = "responses"
    return protocol  # type: ignore[return-value]


def _rect_to_dict(rect: tuple[int, int, int, int]) -> dict[str, int]:
    left, top, right, bottom = rect
    return {
        "left": int(left),
        "top": int(top),
        "right": int(right),
        "bottom": int(bottom),
    }


def _rect_from_dict(data: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(data, dict):
        return None
    try:
        left = int(data.get("left", 0))
        top = int(data.get("top", 0))
        right = int(data.get("right", 0))
        bottom = int(data.get("bottom", 0))
    except (TypeError, ValueError):
        return None

    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


@dataclass(slots=True)
class MonitorJob:
    job_id: str = field(default_factory=create_job_id)
    name: str = ""
    enabled: bool = True
    window_hwnd: int = 0
    window_title: str = ""
    interval_seconds: int = 5
    parse_mode: ParseMode = "ai_structured"
    ai_config: AiGatewayConfig = field(default_factory=AiGatewayConfig)
    screenshot_dir: str = "captures"
    table_name: str = ""
    mappings: list[DbFieldMapping] = field(default_factory=list)
    crop_rect: tuple[int, int, int, int] | None = None
    mark_rects: list[tuple[int, int, int, int]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "name": self.name,
            "enabled": self.enabled,
            "window_hwnd": self.window_hwnd,
            "window_title": self.window_title,
            "interval_seconds": self.interval_seconds,
            "parse_mode": self.parse_mode,
            "ai_config": self.ai_config.to_dict(),
            "screenshot_dir": self.screenshot_dir,
            "table_name": self.table_name,
            "mappings": [mapping.to_dict() for mapping in self.mappings],
            "crop_rect": _rect_to_dict(self.crop_rect) if self.crop_rect is not None else None,
            "mark_rects": [_rect_to_dict(rect) for rect in self.mark_rects],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "MonitorJob":
        job_id = str(data.get("job_id", "") or "").strip() or create_job_id()
        name = str(data.get("name", "") or "").strip()

        parse_mode = normalize_parse_mode(data.get("parse_mode"))

        raw_mark_rects = data.get("mark_rects", [])
        if not isinstance(raw_mark_rects, list):
            raw_mark_rects = []

        return MonitorJob(
            job_id=job_id,
            name=name,
            enabled=bool(data.get("enabled", True)),
            window_hwnd=int(data.get("window_hwnd", 0) or 0),
            window_title=str(data.get("window_title", "") or ""),
            interval_seconds=max(int(data.get("interval_seconds", 5) or 5), 1),
            parse_mode=parse_mode,
            ai_config=AiGatewayConfig.from_dict(data.get("ai_config", {})),
            screenshot_dir=str(data.get("screenshot_dir", "captures") or "captures"),
            table_name=str(data.get("table_name", "") or ""),
            mappings=[DbFieldMapping.from_dict(x) for x in data.get("mappings", []) if isinstance(x, dict)],
            crop_rect=_rect_from_dict(data.get("crop_rect")),
            mark_rects=[
                rect
                for rect in (_rect_from_dict(item) for item in raw_mark_rects)
                if rect is not None
            ],
        )


@dataclass(slots=True)
class AppSettings:
    db_url: str = "sqlite:///../../data/monitor.db"
    jobs: list[MonitorJob] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_url": self.db_url,
            "jobs": [job.to_dict() for job in self.jobs],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "AppSettings":
        jobs = [MonitorJob.from_dict(x) for x in data.get("jobs", []) if isinstance(x, dict)]
        return AppSettings(
            db_url=str(data.get("db_url", "sqlite:///../../data/monitor.db") or "sqlite:///../../data/monitor.db"),
            jobs=jobs,
        )

    @staticmethod
    def from_legacy_monitor_dict(data: dict[str, Any]) -> "AppSettings":
        table_name = str(data.get("table_name", "monitor_records") or "monitor_records")
        legacy_job = MonitorJob(
            name=str(data.get("window_title", "LegacyJob") or "LegacyJob"),
            enabled=True,
            window_hwnd=int(data.get("window_hwnd", 0) or 0),
            window_title=str(data.get("window_title", "") or ""),
            interval_seconds=max(int(data.get("interval_seconds", 5) or 5), 1),
            parse_mode="ai_structured",
            ai_config=AiGatewayConfig(),
            screenshot_dir=str(data.get("screenshot_dir", "captures") or "captures"),
            table_name=table_name,
            mappings=[
                DbFieldMapping(source_type="system", source_key="captured_at", db_column="captured_at"),
                DbFieldMapping(source_type="system", source_key="window_hwnd", db_column="window_hwnd"),
                DbFieldMapping(source_type="system", source_key="window_title", db_column="window_title"),
                DbFieldMapping(source_type="system", source_key="screenshot_path", db_column="screenshot_path"),
                DbFieldMapping(source_type="system", source_key="raw_text", db_column="raw_text"),
                DbFieldMapping(source_type="system", source_key="parsed_json", db_column="parsed_data"),
            ],
        )
        return AppSettings(
            db_url=str(data.get("db_url", "sqlite:///../../data/monitor.db") or "sqlite:///../../data/monitor.db"),
            jobs=[legacy_job],
        )


@dataclass(slots=True)
class PipelineOutput:
    job_id: str
    job_name: str
    captured_at: datetime
    window_hwnd: int
    window_title: str
    screenshot_path: str
    raw_text: str
    parsed_data: dict[str, Any]
    parse_mode: ParseMode = "ai_structured"
    gateway_protocol: str = ""
    model_name: str = ""
    attempt_count: int = 1
    validation_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "job_name": self.job_name,
            "captured_at": self.captured_at.isoformat(),
            "window_hwnd": self.window_hwnd,
            "window_title": self.window_title,
            "screenshot_path": self.screenshot_path,
            "raw_text": self.raw_text,
            "parsed_data": self.parsed_data,
            "parse_mode": self.parse_mode,
            "gateway_protocol": self.gateway_protocol,
            "model_name": self.model_name,
            "attempt_count": self.attempt_count,
            "validation_errors": self.validation_errors,
        }
