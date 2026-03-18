from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
import re
from typing import Any

from PIL import Image

from desktop_monitor.core.contracts import StructuredExtractor
from desktop_monitor.domain.models import (
    ExtractionResult,
    MonitorJob,
    SampleExtractionResult,
    SchemaDraftType,
    SchemaFieldDraft,
)
from desktop_monitor.infra.llm.openai_gateway_client import OpenAIGatewayClient

_JSON_TYPE_ORDER = {
    "null": 0,
    "boolean": 1,
    "integer": 2,
    "number": 3,
    "datetime": 4,
    "string": 5,
    "array": 6,
    "object": 7,
}


class AiStructuredExtractor(StructuredExtractor):
    def __init__(self, job: MonitorJob) -> None:
        self.job = job
        self.client = OpenAIGatewayClient(job.ai_config)
        self.advanced_enabled = bool(
            job.ai_config.enable_advanced_options
            or job.ai_config.enable_generation_controls
            or job.ai_config.enable_output_schema
        )
        schema_text = (job.ai_config.output_schema_text or "").strip()
        self.schema_payload = load_output_schema(schema_text) if self.advanced_enabled and schema_text else None
        self.validation_config = parse_validation_rules(job.ai_config.validation_rules_text)

    def extract(self, image: Image.Image) -> ExtractionResult:
        attempts = max(self.job.ai_config.max_validation_retries, 0) + 1
        feedback = ""
        last_errors: list[str] = []
        last_raw = ""

        for attempt in range(1, attempts + 1):
            response = self.client.generate_json_text(
                image=image,
                extra_feedback=feedback,
                schema_payload=self.schema_payload,
                include_schema=self.schema_payload is not None,
                include_generation_controls=self.advanced_enabled,
                image_detail=self.job.ai_config.image_detail,
            )
            last_raw = response.text
            try:
                parsed_data = parse_json_object(response.text)
            except ValueError as exc:
                last_errors = [str(exc)]
            else:
                last_errors = validate_structured_payload(parsed_data, self.validation_config)
                if not last_errors:
                    return ExtractionResult(
                        raw_text=response.text,
                        parsed_data=parsed_data,
                        validation_errors=[],
                        attempt_count=attempt,
                        gateway_protocol=self.job.ai_config.protocol,
                        model_name=self.job.ai_config.model,
                    )

            feedback = build_retry_feedback(last_errors)

        joined_errors = "; ".join(last_errors) or "unknown validation error"
        raise RuntimeError(
            f"AI structured extraction failed after {attempts} attempts. "
            f"Last errors: {joined_errors}. Last raw output: {last_raw[:600]}"
        )


def build_extractor_for_job(job: MonitorJob) -> StructuredExtractor:
    # The project is AI-only: always route extraction through the gateway client.
    return AiStructuredExtractor(job)


def parse_json_object(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if not candidate:
        raise ValueError("模型返回了空响应。")

    fenced = re.search(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", candidate, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1).strip()

    for raw_candidate in (candidate, extract_first_json_block(candidate)):
        if not raw_candidate:
            continue
        try:
            payload = json.loads(raw_candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
        raise ValueError("模型返回了 JSON，但顶层值不是对象。")

    raise ValueError("在模型输出中没有找到有效的 JSON 对象。")


def extract_first_json_block(text: str) -> str:
    start_positions = [pos for pos in (text.find("{"), text.find("[")) if pos >= 0]
    if not start_positions:
        return ""

    start = min(start_positions)
    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char == opening:
            depth += 1
            continue
        if char == closing:
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return ""


def load_output_schema(schema_text: str) -> dict[str, Any]:
    raw = (schema_text or "").strip()
    if not raw:
        return {
            "type": "object",
            "additionalProperties": True,
        }

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"输出规范不是有效的 JSON：{exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("输出规范必须是 JSON 对象。")

    if isinstance(data.get("type"), str):
        return data

    return infer_json_schema_from_example(data)


def infer_json_schema_from_example(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        properties = {key: infer_json_schema_from_example(item) for key, item in value.items()}
        return {
            "type": "object",
            "properties": properties,
            "required": list(value.keys()),
            "additionalProperties": False,
        }
    if isinstance(value, list):
        if not value:
            item_schema: dict[str, Any] = {"type": ["string", "number", "integer", "boolean", "object", "array", "null"]}
        else:
            item_schema = infer_json_schema_from_example(value[0])
        return {
            "type": "array",
            "items": item_schema,
        }
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if value is None:
        return {"type": "null"}
    if isinstance(value, str) and looks_like_datetime(value):
        return {"type": "string", "format": "date-time"}
    return {"type": "string"}


def parse_validation_rules(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"校验规则不是有效的 JSON：{exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("校验规则必须是 JSON 对象。")
    return data


def validate_structured_payload(payload: dict[str, Any], validation_config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not validation_config:
        return errors

    required_fields = validation_config.get("required_fields", [])
    if isinstance(required_fields, list):
        for field in required_fields:
            field_name = str(field).strip()
            if field_name and field_name not in payload:
                errors.append(f"缺少必填字段：{field_name}")

    non_empty_fields = validation_config.get("non_empty_fields", [])
    if isinstance(non_empty_fields, list):
        for field in non_empty_fields:
            field_name = str(field).strip()
            if not field_name:
                continue
            value = payload.get(field_name)
            if value is None or (isinstance(value, str) and not value.strip()) or value == [] or value == {}:
                errors.append(f"Field must not be empty: {field_name}")

    field_types = validation_config.get("field_types", {})
    if isinstance(field_types, dict):
        for field_name, expected_type in field_types.items():
            if field_name not in payload:
                continue
            if not matches_expected_type(payload.get(field_name), str(expected_type)):
                errors.append(f"Field [{field_name}] does not match expected type {expected_type}")

    regex_rules = validation_config.get("regex_rules", {})
    if isinstance(regex_rules, dict):
        for field_name, pattern in regex_rules.items():
            if field_name not in payload or payload.get(field_name) is None:
                continue
            try:
                regex = re.compile(str(pattern))
            except re.error as exc:
                errors.append(f"Invalid regex for {field_name}: {exc}")
                continue
            if not regex.search(str(payload.get(field_name))):
                errors.append(f"Field [{field_name}] did not match its regex rule")

    numeric_ranges = validation_config.get("numeric_ranges", {})
    if isinstance(numeric_ranges, dict):
        for field_name, range_config in numeric_ranges.items():
            if field_name not in payload or payload.get(field_name) is None:
                continue
            value = payload.get(field_name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append(f"Field [{field_name}] is not numeric, cannot apply range checks")
                continue
            if isinstance(range_config, dict):
                if "min" in range_config and value < range_config["min"]:
                    errors.append(f"Field [{field_name}] is lower than min {range_config['min']}")
                if "max" in range_config and value > range_config["max"]:
                    errors.append(f"Field [{field_name}] is greater than max {range_config['max']}")

    return errors


def matches_expected_type(value: Any, expected_type: str) -> bool:
    normalized = expected_type.strip().lower()
    if normalized == "string":
        return isinstance(value, str)
    if normalized == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if normalized == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if normalized == "boolean":
        return isinstance(value, bool)
    if normalized == "object":
        return isinstance(value, dict)
    if normalized == "array":
        return isinstance(value, list)
    if normalized == "null":
        return value is None
    if normalized == "datetime":
        return isinstance(value, str) and looks_like_datetime(value)
    return True


def looks_like_datetime(value: str) -> bool:
    raw = value.strip()
    if not raw:
        return False
    candidates = [raw.replace("Z", "+00:00")]
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d",
    ]
    for candidate in candidates:
        try:
            datetime.fromisoformat(candidate)
            return True
        except ValueError:
            pass
    for fmt in formats:
        try:
            datetime.strptime(raw, fmt)
            return True
        except ValueError:
            continue
    return False


def build_retry_feedback(errors: list[str]) -> str:
    if not errors:
        return ""
    bullets = "\n".join(f"- {item}" for item in errors)
    return (
        "上一次输出未通过校验。请修正后只返回 JSON 对象。\n"
        f"存在问题：\n{bullets}"
    )


def infer_schema_drafts(samples: list[SampleExtractionResult]) -> list[SchemaFieldDraft]:
    field_names: list[str] = sorted({key for sample in samples for key in sample.parsed_data.keys()})
    drafts: list[SchemaFieldDraft] = []
    total = len(samples)
    for field_name in field_names:
        values = [sample.parsed_data.get(field_name) for sample in samples]
        non_null_values = [value for value in values if value is not None]
        json_type = infer_field_type(non_null_values)
        db_type = map_json_type_to_db(json_type)
        sample_value = format_sample_value(non_null_values[0] if non_null_values else None)
        drafts.append(
            SchemaFieldDraft(
                source_key=field_name,
                column_name=sanitize_identifier(field_name),
                json_type=json_type,
                db_type=db_type,
                nullable=len(non_null_values) < total,
                include=True,
                sample_value=sample_value,
                present_count=len(non_null_values),
            )
        )
    return drafts


def infer_field_type(values: list[Any]) -> str:
    if not values:
        return "string"

    kinds = Counter(classify_value(value) for value in values)
    if len(kinds) == 1:
        return next(iter(kinds.keys()))

    if "string" in kinds and "datetime" in kinds:
        return "datetime"
    if "number" in kinds and "integer" in kinds:
        return "number"

    dominant = max(kinds.items(), key=lambda item: (item[1], -_JSON_TYPE_ORDER.get(item[0], 999)))[0]
    return dominant


def classify_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str) and looks_like_datetime(value):
        return "datetime"
    return "string"


def map_json_type_to_db(json_type: str) -> SchemaDraftType:
    normalized = json_type.lower()
    if normalized == "boolean":
        return "BOOLEAN"
    if normalized == "integer":
        return "INTEGER"
    if normalized == "number":
        return "FLOAT"
    if normalized == "datetime":
        return "DATETIME"
    if normalized in {"object", "array"}:
        return "JSON"
    return "TEXT"


def format_sample_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def sanitize_identifier(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_").lower()
    if not normalized:
        normalized = "field"
    if normalized[0].isdigit():
        normalized = f"f_{normalized}"
    return normalized
