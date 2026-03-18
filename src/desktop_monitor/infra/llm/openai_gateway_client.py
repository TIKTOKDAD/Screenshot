from __future__ import annotations

from dataclasses import dataclass
import base64
import json
from io import BytesIO
import time
from typing import Any
from urllib import error, request

from PIL import Image

from desktop_monitor.domain.models import AiGatewayConfig, normalize_gateway_protocol


@dataclass(slots=True)
class GatewayTextResponse:
    text: str
    response_json: dict[str, Any]
    used_structured_output: bool


class OpenAIGatewayClient:
    RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}

    def __init__(self, config: AiGatewayConfig) -> None:
        self.config = config
        self.protocol = normalize_gateway_protocol(config.protocol)

    def generate_json_text(
        self,
        image: Image.Image,
        extra_feedback: str = "",
        schema_payload: dict[str, Any] | None = None,
        include_schema: bool | None = None,
        image_detail: str | None = None,
        include_generation_controls: bool = True,
    ) -> GatewayTextResponse:
        use_schema = bool(schema_payload) if include_schema is None else bool(include_schema and schema_payload)
        payload, used_structured_output = self._build_payload(
            image=image,
            schema_payload=schema_payload,
            extra_feedback=extra_feedback,
            include_structured_output=use_schema,
            image_detail=image_detail,
            include_generation_controls=include_generation_controls,
        )
        try:
            response_json = self._post_json(payload)
            return GatewayTextResponse(
                text=self._extract_text(response_json),
                response_json=response_json,
                used_structured_output=used_structured_output,
            )
        except RuntimeError as exc:
            if not schema_payload or not self._should_fallback_without_schema(str(exc)):
                raise

        fallback_payload, _ = self._build_payload(
            image=image,
            schema_payload=None,
            extra_feedback=extra_feedback,
            include_structured_output=False,
            image_detail=image_detail,
            include_generation_controls=include_generation_controls,
        )
        response_json = self._post_json(fallback_payload)
        return GatewayTextResponse(
            text=self._extract_text(response_json),
            response_json=response_json,
            used_structured_output=False,
        )

    def healthcheck(self) -> str:
        image = Image.new("RGB", (32, 32), color="white")
        advanced_enabled = bool(
            self.config.enable_advanced_options
            or self.config.enable_generation_controls
            or self.config.enable_output_schema
        )
        schema = None
        if advanced_enabled and (self.config.output_schema_text or "").strip():
            schema = {
                "type": "object",
                "properties": {
                    "status": {"type": ["string", "null"]},
                },
                "required": ["status"],
                "additionalProperties": False,
            }
        result = self.generate_json_text(
            image=image,
            extra_feedback='只返回 {"status": "ok"}。',
            schema_payload=schema,
            include_schema=schema is not None,
            include_generation_controls=advanced_enabled,
            image_detail=self.config.image_detail,
        )
        return result.text

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        endpoint = self._resolve_endpoint()
        body = json.dumps(payload).encode("utf-8")
        body_size_kb = max(len(body) // 1024, 1)
        req = request.Request(
            endpoint,
            data=body,
            method="POST",
            headers=self._build_headers(),
        )
        timeout = max(int(self.config.timeout_seconds or 90), 5)
        max_attempts = 3
        backoff_seconds = 0.8
        raw = ""

        for attempt in range(1, max_attempts + 1):
            try:
                with request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8")
                break
            except error.HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")
                is_retryable = exc.code in self.RETRYABLE_HTTP_CODES
                if is_retryable and attempt < max_attempts:
                    time.sleep(backoff_seconds)
                    backoff_seconds *= 2
                    continue
                if exc.code == 502:
                    raise RuntimeError(
                        "HTTP 502: 网关上游暂时不可用（Upstream request failed）。"
                        "请稍后重试，或切换更稳定的网关/模型。"
                        f" 请求信息: protocol={self.protocol}, endpoint={endpoint}, body_kb={body_size_kb}."
                        f" 原始错误: {details[:400]}"
                    ) from exc
                raise RuntimeError(
                    f"HTTP {exc.code}: protocol={self.protocol}, endpoint={endpoint}, body_kb={body_size_kb}. "
                    f"details={details[:600]}"
                ) from exc
            except error.URLError as exc:
                if attempt < max_attempts:
                    time.sleep(backoff_seconds)
                    backoff_seconds *= 2
                    continue
                raise RuntimeError(
                    f"Network request failed: {exc.reason}. "
                    f"protocol={self.protocol}, endpoint={endpoint}, body_kb={body_size_kb}"
                ) from exc
            except TimeoutError as exc:
                if attempt < max_attempts:
                    time.sleep(backoff_seconds)
                    backoff_seconds *= 2
                    continue
                raise RuntimeError(
                    "Request timed out. Increase timeout or check gateway latency. "
                    f"protocol={self.protocol}, endpoint={endpoint}, body_kb={body_size_kb}"
                ) from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"网关返回的不是 JSON 响应：{raw[:300]}") from exc

        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"网关返回错误：{data['error']}")
        if not isinstance(data, dict):
            raise RuntimeError("网关响应必须是 JSON 对象。")
        return data

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
        }
        api_key = self.config.api_key.strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _resolve_endpoint(self) -> str:
        base = (self.config.base_url or "").strip().rstrip("/")
        if not base:
            raise RuntimeError("请先填写接口地址。")

        if self.protocol == "chat_completions":
            if base.endswith("/chat/completions"):
                return base
            return f"{base}/chat/completions"

        if base.endswith("/responses"):
            return base
        return f"{base}/responses"

    def _build_payload(
        self,
        image: Image.Image,
        schema_payload: dict[str, Any] | None,
        extra_feedback: str,
        include_structured_output: bool,
        image_detail: str | None,
        include_generation_controls: bool,
    ) -> tuple[dict[str, Any], bool]:
        model = self.config.model.strip()
        if not model:
            raise RuntimeError("请先填写模型名称。")

        data_uri = self._to_data_uri(image)
        normalized_detail = str(image_detail if image_detail is not None else self.config.image_detail).strip().lower()
        detail_value = normalized_detail if normalized_detail in {"low", "high", "auto"} else ""
        user_prompt = self.config.user_prompt.strip() or "请从这张截图中提取结构化数据。"
        if extra_feedback.strip():
            user_prompt = f"{user_prompt}\n\nValidation feedback:\n{extra_feedback.strip()}"

        if self.protocol == "chat_completions":
            payload: dict[str, Any] = {
                "model": model,
                "messages": [
                    {"role": "system", "content": self.config.system_prompt.strip()},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_prompt},
                            {
                                "type": "image_url",
                                "image_url": ({"url": data_uri, "detail": detail_value} if detail_value else {"url": data_uri}),
                            },
                        ],
                    },
                ],
            }
            if include_generation_controls:
                payload["temperature"] = float(self.config.temperature)
                payload["max_tokens"] = int(self.config.max_output_tokens)
            if include_structured_output and schema_payload is not None:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "desktop_monitor_result",
                        "schema": schema_payload,
                        "strict": True,
                    },
                }
            return payload, include_structured_output and schema_payload is not None

        payload = {
            "model": model,
            "input": [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": self.config.system_prompt.strip(),
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": user_prompt},
                        {
                            "type": "input_image",
                            "image_url": data_uri,
                            **({"detail": detail_value} if detail_value else {}),
                        },
                    ],
                },
            ],
        }
        if include_generation_controls:
            payload["temperature"] = float(self.config.temperature)
            payload["max_output_tokens"] = int(self.config.max_output_tokens)
        if include_structured_output and schema_payload is not None:
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "desktop_monitor_result",
                    "schema": schema_payload,
                    "strict": True,
                }
            }
        return payload, include_structured_output and schema_payload is not None

    @staticmethod
    def _to_data_uri(image: Image.Image) -> str:
        normalized = image if image.mode == "RGB" else image.convert("RGB")
        buffer = BytesIO()
        normalized.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    def _extract_text(self, response_json: dict[str, Any]) -> str:
        if self.protocol == "chat_completions":
            choices = response_json.get("choices")
            if isinstance(choices, list) and choices:
                message = choices[0].get("message", {})
                content = message.get("content", "")
                return self._flatten_text(content)
            raise RuntimeError(f"chat/completions response missing choices: {response_json}")

        output_text = response_json.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        output = response_json.get("output")
        if isinstance(output, list):
            parts: list[str] = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                flattened = self._flatten_text(content)
                if flattened:
                    parts.append(flattened)
            if parts:
                return "\n".join(parts).strip()


        raise RuntimeError(f"responses output did not contain usable text: {response_json}")

    def _flatten_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, dict):
            text_value = content.get("text")
            if isinstance(text_value, str):
                return text_value.strip()
            return ""
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item.strip())
                    continue
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("text"), str):
                    parts.append(str(item.get("text")).strip())
                    continue
                nested = item.get("content")
                if nested is not None:
                    nested_text = self._flatten_text(nested)
                    if nested_text:
                        parts.append(nested_text)
            return "\n".join(part for part in parts if part).strip()
        return ""

    @staticmethod
    def _should_fallback_without_schema(message: str) -> bool:
        lowered = message.lower()
        keywords = [
            "response_format",
            "json_schema",
            "text.format",
            "structured output",
            "unsupported parameter",
        ]
        return any(keyword in lowered for keyword in keywords)
