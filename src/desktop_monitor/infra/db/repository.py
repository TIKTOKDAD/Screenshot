from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, MetaData, Table, Text, create_engine, insert, inspect
from sqlalchemy.exc import NoSuchTableError
from sqlalchemy.sql.schema import Column

from desktop_monitor.domain.models import DbFieldMapping, PipelineOutput, SchemaFieldDraft


_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(name: str, label: str) -> None:
    if not _IDENTIFIER_PATTERN.match(name):
        raise ValueError(f"{label}只能包含字母、数字或下划线，且不能以数字开头。")


class SqlAlchemySchemaManager:
    TYPE_MAP = {
        "TEXT": Text,
        "INTEGER": Integer,
        "FLOAT": Float,
        "BOOLEAN": Boolean,
        "DATETIME": DateTime,
        "JSON": JSON,
    }

    DEFAULT_COLUMNS = [
        ("captured_at", DateTime, False, False),
        ("job_id", Text, False, False),
        ("job_name", Text, False, False),
        ("window_hwnd", Integer, False, False),
        ("window_title", Text, False, False),
        ("screenshot_path", Text, False, False),
        ("raw_text", Text, True, False),
        ("parse_mode", Text, False, False),
        ("model_name", Text, True, False),
        ("gateway_protocol", Text, True, False),
        ("attempt_count", Integer, False, False),
        ("validation_errors", JSON, True, False),
    ]

    def __init__(self, db_url: str) -> None:
        self.engine = create_engine(db_url, future=True)

    def create_table(self, table_name: str, drafts: list[SchemaFieldDraft]) -> list[dict[str, Any]]:
        _validate_identifier(table_name, "表名")
        if not drafts:
            raise ValueError("创建数据表时至少需要一个字段草案。")

        inspector = inspect(self.engine)
        if inspector.has_table(table_name):
            raise ValueError(f"数据表：{table_name}，数据库中已经存在。")

        metadata = MetaData()
        columns: list[Column[Any]] = []
        columns.append(Column("id", Integer, nullable=False, primary_key=True, autoincrement=False))
        taken: set[str] = {"id"}

        for name, column_type, nullable, is_primary_key in self.DEFAULT_COLUMNS:
            columns.append(Column(name, column_type, nullable=nullable, primary_key=is_primary_key, autoincrement=False))
            taken.add(name)

        for draft in drafts:
            if not draft.include:
                continue
            _validate_identifier(draft.column_name, "列名")
            if draft.column_name in taken:
                raise ValueError(f"列名重复：{draft.column_name}")
            taken.add(draft.column_name)
            column_type = self.TYPE_MAP.get(draft.db_type, Text)
            columns.append(Column(draft.column_name, column_type, nullable=draft.nullable))

        table = Table(table_name, metadata, *columns)
        metadata.create_all(self.engine, tables=[table])
        return self.describe_table(table_name)

    def describe_table(self, table_name: str) -> list[dict[str, Any]]:
        _validate_identifier(table_name, "表名")
        metadata = MetaData()
        try:
            table = Table(table_name, metadata, autoload_with=self.engine)
        except NoSuchTableError as exc:
            raise ValueError(f"数据表不存在：{table_name}") from exc

        return [
            {
                "name": column.name,
                "type": str(column.type),
                "nullable": bool(column.nullable),
                "primary_key": bool(column.primary_key),
            }
            for column in table.columns
        ]


class SqlAlchemyMappedRepository:
    SYSTEM_KEY_MAP = {
        "record_id_ts",
        "captured_at",
        "window_hwnd",
        "window_title",
        "screenshot_path",
        "raw_text",
        "job_id",
        "job_name",
        "parsed_json",
        "parse_mode",
        "model_name",
        "gateway_protocol",
        "attempt_count",
        "validation_json",
    }

    def __init__(
        self,
        db_url: str,
        table_name: str,
        mappings: list[DbFieldMapping],
    ) -> None:
        _validate_identifier(table_name, "表名")
        if not mappings:
            raise ValueError("数据库写入时至少需要一条字段映射。")

        self.engine = create_engine(db_url, future=True)
        self.metadata = MetaData()
        self.table_name = table_name
        self.mappings = mappings

        try:
            self.table = Table(table_name, self.metadata, autoload_with=self.engine)
        except NoSuchTableError as exc:
            raise ValueError(f"数据表不存在：{table_name}，无法写入。") from exc

        self._column_names = {col.name for col in self.table.columns}
        for mapping in self.mappings:
            if mapping.db_column not in self._column_names:
                raise ValueError(
                    f"找不到数据库列：{table_name}.{mapping.db_column}，"
                    f"请先确认目标表结构与映射配置。"
                )

    def save(self, output: PipelineOutput) -> None:
        payload = self._build_payload(output)

        with self.engine.begin() as conn:
            conn.execute(insert(self.table).values(**payload))

    def _build_payload(self, output: PipelineOutput) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for mapping in self.mappings:
            payload[mapping.db_column] = self._resolve_mapping_value(mapping, output)
        self._fill_required_defaults(payload)
        return payload

    def _fill_required_defaults(self, payload: dict[str, Any]) -> None:
        for column in self.table.columns:
            if column.nullable:
                continue
            name = str(column.name)
            if payload.get(name) is not None:
                continue
            payload[name] = self._default_value_for_column(column)

    @staticmethod
    def _default_value_for_column(column: Column[Any]) -> Any:
        if str(column.name) == "id":
            return int(datetime.now().timestamp() * 1_000_000)

        col_type = column.type
        if isinstance(col_type, DateTime):
            return datetime.now()
        if isinstance(col_type, Integer):
            return 0
        if isinstance(col_type, Float):
            return 0.0
        if isinstance(col_type, Boolean):
            return False
        if isinstance(col_type, JSON):
            return {}
        return ""

    def _resolve_mapping_value(self, mapping: DbFieldMapping, output: PipelineOutput) -> Any:
        if mapping.source_type == "parsed":
            return output.parsed_data.get(mapping.source_key)

        if mapping.source_type == "constant":
            return mapping.constant_value

        if mapping.source_key not in self.SYSTEM_KEY_MAP:
            raise ValueError(
                f"系统字段 [{mapping.source_key}] 不受支持。"
                "可选值：record_id_ts/captured_at/window_hwnd/window_title/screenshot_path/raw_text/job_id/job_name/parsed_json/parse_mode/model_name/gateway_protocol/attempt_count/validation_json"
            )

        if mapping.source_key == "record_id_ts":
            return int(datetime.now().timestamp() * 1_000_000)

        if mapping.source_key == "captured_at":
            return output.captured_at
        if mapping.source_key == "window_hwnd":
            return output.window_hwnd
        if mapping.source_key == "window_title":
            return output.window_title
        if mapping.source_key == "screenshot_path":
            return output.screenshot_path
        if mapping.source_key == "raw_text":
            return output.raw_text
        if mapping.source_key == "job_id":
            return output.job_id
        if mapping.source_key == "job_name":
            return output.job_name
        if mapping.source_key == "parsed_json":
            return output.parsed_data
        if mapping.source_key == "parse_mode":
            return output.parse_mode
        if mapping.source_key == "model_name":
            return output.model_name
        if mapping.source_key == "gateway_protocol":
            return output.gateway_protocol
        if mapping.source_key == "attempt_count":
            return output.attempt_count
        if mapping.source_key == "validation_json":
            return output.validation_errors

        return None
