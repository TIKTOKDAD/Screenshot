from __future__ import annotations

import json
import re
import time
import hashlib
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


def _bootstrap_package_path() -> None:
    # Allow direct execution: `python src/desktop_monitor/ui/main_window.py`.
    if __package__:
        return
    src_root = Path(__file__).resolve().parents[2]
    src_root_str = str(src_root)
    if src_root_str not in sys.path:
        sys.path.insert(0, src_root_str)


_bootstrap_package_path()

from PIL import Image
from PySide6.QtCore import QObject, QPoint, QRect, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import inspect, text

from desktop_monitor.core.image_adjustments import apply_job_capture_adjustments
from desktop_monitor.core.pipeline import MonitorPipeline
from desktop_monitor.core.structured_extraction import (
    build_extractor_for_job,
    infer_schema_drafts,
    load_output_schema,
    parse_json_object,
    parse_validation_rules,
)
from desktop_monitor.domain.models import (
    AiGatewayConfig,
    AppSettings,
    DEFAULT_AI_SYSTEM_PROMPT,
    DEFAULT_AI_USER_PROMPT,
    DbFieldMapping,
    MonitorJob,
    SampleExtractionResult,
    SchemaFieldDraft,
    WindowInfo,
    create_job_id,
)
from desktop_monitor.infra.capture.window_capture import WindowCaptureService
from desktop_monitor.infra.db.repository import SqlAlchemyMappedRepository, SqlAlchemySchemaManager
from desktop_monitor.infra.llm.openai_gateway_client import OpenAIGatewayClient
from desktop_monitor.infra.window.window_service import WindowService
from desktop_monitor.ui.monitor_worker import MonitorWorker
from desktop_monitor.ui.preview_editor import ClickableLabel, PreviewEditorDialog
from desktop_monitor.utils.config_store import ConfigStore


class UiActionWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, task: Callable[[], Any]) -> None:
        super().__init__()
        self._task = task

    @Slot()
    def run(self) -> None:
        try:
            result = self._task()
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("桌面数据监控器")
        self.setMinimumSize(980, 620)
        self._compact_ui = False

        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self._compact_ui = available.height() <= 800
            target_w = min(1460, max(980, available.width() - 40))
            target_h = min(920, max(620, available.height() - 60))
            target_w = min(target_w, available.width())
            target_h = min(target_h, available.height())
            self.resize(target_w, target_h)
        else:
            self.resize(1460, 920)

        self.window_service = WindowService()
        self.config_store = ConfigStore()

        self._windows: dict[int, WindowInfo] = {}
        self._jobs: dict[str, MonitorJob] = {}
        self._editing_job_id: str | None = None
        self._pending_window_hwnd: int | None = None

        self._threads: dict[str, QThread] = {}
        self._workers: dict[str, MonitorWorker] = {}
        self._action_thread: QThread | None = None
        self._action_worker: UiActionWorker | None = None
        self._action_cancel_requested = False
        self._current_action_name = ""
        self._current_action_error_title = ""
        self._current_action_success_handler: Callable[[Any], None] | None = None

        self._last_pixmaps: dict[str, QPixmap] = {}
        self._last_image_paths: dict[str, str] = {}
        self._preview_raw_baseline: dict[str, bool] = {}
        self._preview_labels: dict[str, str] = {}
        self._last_preview_job_id: str | None = None
        self._editor_crop_rect: tuple[int, int, int, int] | None = None
        self._editor_mark_rects: list[tuple[int, int, int, int]] = []

        self._syncing_job_table = False
        self._latest_sample_results: list[SampleExtractionResult] = []
        self._latest_schema_drafts: list[SchemaFieldDraft] = []

        self._build_ui()
        self._apply_compact_ui_if_needed()
        self._wire_operation_click_logs()
        self.refresh_windows()
        self._load_settings_on_startup()

    def _apply_compact_ui_if_needed(self) -> None:
        if not self._compact_ui:
            return

        if hasattr(self, "system_prompt_edit"):
            self.system_prompt_edit.setMinimumHeight(72)
        if hasattr(self, "user_prompt_edit"):
            self.user_prompt_edit.setMinimumHeight(86)
        if hasattr(self, "output_schema_edit"):
            self.output_schema_edit.setMinimumHeight(90)
        if hasattr(self, "validation_rules_edit"):
            self.validation_rules_edit.setMinimumHeight(90)
        if hasattr(self, "preview_label"):
            self.preview_label.setMinimumHeight(180)

        if hasattr(self, "_main_splitter"):
            self._main_splitter.setSizes([760, 430])

    def _wire_operation_click_logs(self) -> None:
        button_ops = {
            "refresh_btn": "刷新可选窗口列表",
            "capture_dir_btn": "选择截图目录",
            "save_job_btn": "保存当前任务配置",
            "capture_edit_init_btn": "执行单次截图并进入编辑初始化",
            "capture_consistency_btn": "校验截图输入一致性",
            "save_ai_profile_btn": "保存AI配置",
            "load_ai_profile_btn": "加载AI配置",
            "sample_batch_btn": "执行一次采样并生成字段草案",
            "ai_prompt_backfill_btn": "基于启用字段执行AI提示词回填",
            "create_table_btn": "创建数据表",
            "expand_schema_table_btn": "放大查看字段表",
            "precheck_btn": "执行环境自检",
            "gateway_test_btn": "测试AI网关",
            "ai_probe_test_btn": "执行AI测试",
            "capture_test_btn": "执行单次截图",
            "parse_test_btn": "执行识别解析",
            "db_test_btn": "执行写入数据库测试",
            "db_connect_test_btn": "测试数据库连接",
            "start_selected_jobs_btn": "启动勾选任务",
            "stop_selected_jobs_btn": "停止勾选任务",
            "delete_job_btn": "删除当前选中任务",
            "start_btn": "启动全部已启用任务",
            "stop_btn": "停止全部任务",
            "save_btn": "保存全部配置到本地",
            "load_btn": "加载本地配置",
            "cancel_action_btn": "取消当前异步操作",
        }

        for attr_name, operation in button_ops.items():
            button = getattr(self, attr_name, None)
            if not isinstance(button, QPushButton):
                continue
            button.clicked.connect(
                lambda checked=False, name=button.text().strip() or attr_name, op=operation: self._append_log(
                    f"点击按钮：{name}，操作：{op}"
                )
            )

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)

        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(8)

        header_layout = QHBoxLayout()
        header_text_layout = QVBoxLayout()
        header_text_layout.setSpacing(2)

        title = QLabel("桌面数据监控器")
        title.setStyleSheet("font-size: 22px; font-weight: 700; color: #0f172a;")

        subtitle = QLabel("选择窗口、完成 AI 识别测试和 1 次采样建表后，再启动持续监控。")
        subtitle.setObjectName("headerSubtitle")

        self.status_badge = QLabel("已停止")
        self.status_badge.setObjectName("statusBadge")

        header_text_layout.addWidget(title)
        header_text_layout.addWidget(subtitle)

        header_layout.addLayout(header_text_layout)
        header_layout.addStretch(1)
        header_layout.addWidget(self.status_badge)
        root_layout.addLayout(header_layout)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._wrap_panel_with_scroll(self._build_left_panel()))
        splitter.addWidget(self._wrap_panel_with_scroll(self._build_right_panel()))
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([900, 560])
        self._main_splitter = splitter
        root_layout.addWidget(splitter, 1)

    @staticmethod
    def _wrap_panel_with_scroll(panel: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        area.setWidget(panel)
        return area

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setSpacing(10)

        layout.addWidget(self._build_db_group())
        layout.addWidget(self._build_jobs_group(), 2)
        layout.addWidget(self._build_editor_tabs(), 3)
        layout.addWidget(self._build_test_group())
        layout.addWidget(self._build_action_row())
        return panel

    def _build_db_group(self) -> QGroupBox:
        group = QGroupBox("数据库连接")
        layout = QGridLayout(group)
        layout.setColumnStretch(1, 1)

        hint = QLabel("建议先连通数据库，再进行 1 次采样建表。也支持填写已有目标表。")
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)

        self.db_url_edit = QLineEdit("sqlite:///../../data/monitor.db")
        self.db_url_edit.setPlaceholderText("示例: sqlite:///../../data/monitor.db")
        self.db_connect_test_btn = QPushButton("测试连接")
        self._set_button_variant(self.db_connect_test_btn, "secondary")
        self.db_connect_test_btn.clicked.connect(self.test_database_connection)

        layout.addWidget(hint, 0, 0, 1, 3)
        layout.addWidget(QLabel("连接串"), 1, 0)
        layout.addWidget(self.db_url_edit, 1, 1)
        layout.addWidget(self.db_connect_test_btn, 1, 2)
        return group

    def _build_jobs_group(self) -> QGroupBox:
        group = QGroupBox("监控任务列表")
        layout = QVBoxLayout(group)
        layout.setSpacing(8)

        hint = QLabel("选中某个任务后，左侧可继续编辑配置，右侧会显示最近一次截图与解析结果。")
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)

        self.jobs_table = QTableWidget(0, 7)
        self.jobs_table.setHorizontalHeaderLabels([
            "启用",
            "任务ID",
            "任务名",
            "窗口",
            "频率",
            "目标表",
            "模式",
        ])
        self.jobs_table.verticalHeader().setVisible(False)
        self.jobs_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.jobs_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.jobs_table.itemSelectionChanged.connect(self._on_job_selection_changed)
        self.jobs_table.itemChanged.connect(self._on_jobs_table_item_changed)

        header = self.jobs_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)

        buttons = QHBoxLayout()
        self.start_selected_jobs_btn = QPushButton("启动勾选任务")
        self.stop_selected_jobs_btn = QPushButton("停止勾选任务")
        self.delete_job_btn = QPushButton("删除任务")

        self._set_button_variant(self.start_selected_jobs_btn, "success")
        self._set_button_variant(self.stop_selected_jobs_btn, "danger")
        self._set_button_variant(self.delete_job_btn, "danger")

        self.start_selected_jobs_btn.clicked.connect(self.start_monitoring)
        self.stop_selected_jobs_btn.clicked.connect(self.stop_selected_monitoring)
        self.delete_job_btn.clicked.connect(self.delete_job)

        buttons.addWidget(self.start_selected_jobs_btn)
        buttons.addWidget(self.stop_selected_jobs_btn)
        buttons.addWidget(self.delete_job_btn)
        buttons.addStretch(1)

        layout.addWidget(hint)
        layout.addWidget(self.jobs_table)
        layout.addLayout(buttons)
        return group

    def _build_editor_tabs(self) -> QTabWidget:
        self.editor_tabs = QTabWidget()
        self.editor_tabs.setDocumentMode(True)
        self.editor_tabs.addTab(self._build_editor_tab(), "任务配置")
        self.editor_tabs.addTab(self._build_ai_tab(), "AI 结构化")
        self.editor_tabs.addTab(self._build_ai_probe_tab(), "AI 测试")
        self.editor_tabs.addTab(self._build_mapping_tab(), "一次采样建表")
        self._update_parse_mode_ui()
        return self.editor_tabs

    def _build_ai_probe_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        hint = QLabel(
            "用于定位网关兼容问题：可单独开关 schema，并切换图片 detail 参数。"
            "建议先用白底图测试连通，再用当前预览图复现真实请求。"
        )
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)

        form = QWidget()
        form_layout = QGridLayout(form)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setHorizontalSpacing(10)
        form_layout.setVerticalSpacing(8)
        form_layout.setColumnStretch(1, 1)

        self.ai_probe_image_source_combo = QComboBox()
        self.ai_probe_image_source_combo.addItem("使用当前预览图", "preview")
        self.ai_probe_image_source_combo.addItem("使用白底测试图 (32x32)", "blank")

        self.ai_probe_schema_check = QCheckBox("启用 Schema 结构化输出")
        self.ai_probe_schema_check.setChecked(True)

        self.ai_probe_detail_combo = QComboBox()
        self.ai_probe_detail_combo.addItem("high", "high")
        self.ai_probe_detail_combo.addItem("low", "low")
        self.ai_probe_detail_combo.addItem("auto", "auto")
        self.ai_probe_detail_combo.addItem("不传 detail 参数", "")

        self.ai_probe_test_btn = QPushButton("执行 AI 测试")
        self._set_button_variant(self.ai_probe_test_btn, "secondary")
        self.ai_probe_test_btn.clicked.connect(self.test_ai_probe_current_job)

        form_layout.addWidget(QLabel("测试图片来源"), 0, 0)
        form_layout.addWidget(self.ai_probe_image_source_combo, 0, 1)
        form_layout.addWidget(QLabel("Schema 开关"), 1, 0)
        form_layout.addWidget(self.ai_probe_schema_check, 1, 1)
        form_layout.addWidget(QLabel("detail 参数"), 2, 0)
        form_layout.addWidget(self.ai_probe_detail_combo, 2, 1)

        layout.addWidget(hint)
        layout.addWidget(form)
        layout.addWidget(self.ai_probe_test_btn)
        layout.addStretch(1)
        return page

    def _build_editor_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        hint = QLabel(
            "先配置窗口、频率、截图目录和目标表，再做截图与解析测试。"
            "确认无误后再启动持续监控。"
        )
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)

        form = QWidget()
        form_layout = QGridLayout(form)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setHorizontalSpacing(10)
        form_layout.setVerticalSpacing(8)
        form_layout.setColumnStretch(1, 1)

        self.job_id_label = QLabel("(新任务)")
        self.job_name_edit = QLineEdit("")
        self.job_enabled_check = QCheckBox("启用该任务")
        self.job_enabled_check.setChecked(True)

        self.window_combo = QComboBox()
        self.window_combo.setMinimumWidth(360)
        self.refresh_btn = QPushButton("刷新窗口")
        self._set_button_variant(self.refresh_btn, "secondary")
        self.refresh_btn.clicked.connect(self.refresh_windows)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 86400)
        self.interval_spin.setValue(5)
        self.interval_spin.setSuffix(" 秒")

        self.capture_dir_edit = QLineEdit("captures")
        self.capture_dir_btn = QPushButton("选择目录")
        self._set_button_variant(self.capture_dir_btn, "secondary")
        self.capture_dir_btn.clicked.connect(self._choose_capture_dir)

        self.table_name_edit = QLineEdit("")
        self.table_name_edit.setPlaceholderText("输入要写入的目标表名")

        form_layout.addWidget(QLabel("任务 ID"), 0, 0)
        form_layout.addWidget(self.job_id_label, 0, 1)
        form_layout.addWidget(self.job_enabled_check, 0, 2)

        form_layout.addWidget(QLabel("任务名称"), 1, 0)
        form_layout.addWidget(self.job_name_edit, 1, 1, 1, 2)

        form_layout.addWidget(QLabel("目标窗口"), 2, 0)
        form_layout.addWidget(self.window_combo, 2, 1)
        form_layout.addWidget(self.refresh_btn, 2, 2)

        form_layout.addWidget(QLabel("截图频率"), 3, 0)
        form_layout.addWidget(self.interval_spin, 3, 1, 1, 2)

        self.capture_adjustment_summary_label = QLabel("未配置裁剪或红框标注")
        self.capture_adjustment_summary_label.setObjectName("sectionHint")
        self.capture_adjustment_summary_label.setWordWrap(True)
        form_layout.addWidget(QLabel("截图调整"), 4, 0)
        form_layout.addWidget(self.capture_adjustment_summary_label, 4, 1, 1, 2)

        form_layout.addWidget(QLabel("截图目录"), 5, 0)
        form_layout.addWidget(self.capture_dir_edit, 5, 1)
        form_layout.addWidget(self.capture_dir_btn, 5, 2)

        form_layout.addWidget(QLabel("目标表名"), 6, 0)
        form_layout.addWidget(self.table_name_edit, 6, 1, 1, 2)

        editor_actions = QHBoxLayout()
        self.capture_edit_init_btn = QPushButton("单次截图并编辑（初始化）")
        self.capture_consistency_btn = QPushButton("校验截图一致性")
        self.save_job_btn = QPushButton("保存任务")
        self._set_button_variant(self.capture_edit_init_btn, "secondary")
        self._set_button_variant(self.capture_consistency_btn, "secondary")
        self._set_button_variant(self.save_job_btn, "success")
        self.capture_edit_init_btn.clicked.connect(self.capture_and_edit_for_task_config)
        self.capture_consistency_btn.clicked.connect(self.validate_capture_consistency_current_job)
        self.save_job_btn.clicked.connect(self.save_job)
        editor_actions.addWidget(self.capture_edit_init_btn)
        editor_actions.addWidget(self.capture_consistency_btn)
        editor_actions.addWidget(self.save_job_btn)
        editor_actions.addStretch(1)

        layout.addWidget(hint)
        layout.addWidget(form)
        layout.addLayout(editor_actions)
        layout.addStretch(1)
        return page

    def _build_ai_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        hint = QLabel(
            "支持 OpenAI-compatible 的 chat/completions 和 /v1/responses。"
            "模型需支持图片输入，识别结果会先做本地校验。"
        )
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)

        form = QWidget()
        form_layout = QGridLayout(form)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setHorizontalSpacing(10)
        form_layout.setVerticalSpacing(8)
        form_layout.setColumnStretch(1, 1)

        self.gateway_protocol_combo = QComboBox()
        self.gateway_protocol_combo.addItem("Responses 接口 (/v1/responses)", "responses")
        self.gateway_protocol_combo.addItem("Chat Completions 接口", "chat_completions")

        self.base_url_edit = QLineEdit("https://api.openai.com/v1")
        self.base_url_edit.setPlaceholderText("例如：https://your-gateway.example.com/v1")

        self.api_key_edit = QLineEdit("")
        self.api_key_edit.setPlaceholderText("输入你的网关密钥")
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)

        self.model_edit = QLineEdit("")
        self.model_edit.setPlaceholderText("例如：gpt-4.1-mini / gpt-4.1 / 你的视觉模型")

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(5, 600)
        self.timeout_spin.setValue(90)
        self.timeout_spin.setSuffix(" 秒")
        self.timeout_spin.setMinimumWidth(96)

        self.retry_spin = QSpinBox()
        self.retry_spin.setRange(0, 10)
        self.retry_spin.setValue(2)
        self.retry_spin.setSuffix(" 次")
        self.retry_spin.setMinimumWidth(96)

        self.temperature_spin = QDoubleSpinBox()
        self.temperature_spin.setRange(0.0, 2.0)
        self.temperature_spin.setSingleStep(0.1)
        self.temperature_spin.setValue(0.1)
        self.temperature_spin.setMinimumWidth(96)

        self.max_tokens_spin = QSpinBox()
        self.max_tokens_spin.setRange(128, 16384)
        self.max_tokens_spin.setValue(1800)
        self.max_tokens_spin.setSuffix(" 个")
        self.max_tokens_spin.setMinimumWidth(110)

        request_row = QWidget()
        request_row_layout = QHBoxLayout(request_row)
        request_row_layout.setContentsMargins(0, 0, 0, 0)
        request_row_layout.setSpacing(8)
        request_row_layout.addWidget(QLabel("请求超时"))
        request_row_layout.addWidget(self.timeout_spin)
        request_row_layout.addSpacing(16)
        request_row_layout.addWidget(QLabel("重试次数"))
        request_row_layout.addWidget(self.retry_spin)
        request_row_layout.addStretch(1)

        generation_row = QWidget()
        generation_row_layout = QHBoxLayout(generation_row)
        generation_row_layout.setContentsMargins(0, 0, 0, 0)
        generation_row_layout.setSpacing(8)
        generation_row_layout.addWidget(QLabel("温度"))
        generation_row_layout.addWidget(self.temperature_spin)
        generation_row_layout.addSpacing(16)
        generation_row_layout.addWidget(QLabel("最大输出"))
        generation_row_layout.addWidget(self.max_tokens_spin)
        generation_row_layout.addStretch(1)

        detail_row = QWidget()
        detail_row_layout = QHBoxLayout(detail_row)
        detail_row_layout.setContentsMargins(0, 0, 0, 0)
        detail_row_layout.setSpacing(8)
        self.image_detail_combo = QComboBox()
        self.image_detail_combo.addItem("不传 detail 参数", "")
        self.image_detail_combo.addItem("high", "high")
        self.image_detail_combo.addItem("low", "low")
        self.image_detail_combo.addItem("auto", "auto")
        detail_row_layout.addWidget(QLabel("detail 参数"))
        detail_row_layout.addWidget(self.image_detail_combo)
        detail_row_layout.addStretch(1)

        self.enable_advanced_options_check = QCheckBox("启用高级选项（生成控制 / 输出规范 / detail）")
        self.enable_advanced_options_check.setChecked(False)
        self.enable_advanced_options_check.toggled.connect(self._update_ai_option_visibility)

        form_layout.addWidget(QLabel("网关协议"), 0, 0)
        form_layout.addWidget(self.gateway_protocol_combo, 0, 1)
        form_layout.addWidget(QLabel("接口地址"), 1, 0)
        form_layout.addWidget(self.base_url_edit, 1, 1)
        form_layout.addWidget(QLabel("API 密钥"), 2, 0)
        form_layout.addWidget(self.api_key_edit, 2, 1)
        form_layout.addWidget(QLabel("模型名称"), 3, 0)
        form_layout.addWidget(self.model_edit, 3, 1)
        form_layout.addWidget(QLabel("请求控制"), 4, 0)
        form_layout.addWidget(request_row, 4, 1)
        form_layout.addWidget(QLabel("高级选项"), 5, 0)
        form_layout.addWidget(self.enable_advanced_options_check, 5, 1)

        self.system_prompt_edit = QTextEdit()
        self.system_prompt_edit.setPlainText(DEFAULT_AI_SYSTEM_PROMPT)
        self.system_prompt_edit.setPlaceholderText("告诉模型它的角色、禁止事项和输出要求。")
        self.system_prompt_edit.setMinimumHeight(96)

        self.user_prompt_edit = QTextEdit()
        self.user_prompt_edit.setPlainText(DEFAULT_AI_USER_PROMPT)
        self.user_prompt_edit.setPlaceholderText("描述你希望模型从截图中提取哪些业务字段和判断规则。")
        self.user_prompt_edit.setMinimumHeight(116)

        self.output_schema_edit = QTextEdit()
        self.output_schema_edit.setPlaceholderText(
            """{
  "order_no": "A001",
  "amount": 12.34,
  "status": "paid"
}

也可以直接填写 JSON Schema。"""
        )
        self.output_schema_edit.setMinimumHeight(126)
        self.output_schema_label = QLabel("输出规范（JSON 示例或 JSON Schema）")

        self.advanced_options_panel = QWidget()
        advanced_layout = QVBoxLayout(self.advanced_options_panel)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.setSpacing(6)
        advanced_layout.addWidget(QLabel("生成控制"))
        advanced_layout.addWidget(generation_row)
        advanced_layout.addWidget(detail_row)
        advanced_layout.addWidget(self.output_schema_label)
        advanced_layout.addWidget(self.output_schema_edit)

        self.validation_rules_edit = QTextEdit()
        self.validation_rules_edit.setPlaceholderText(
            """{
  "required_fields": ["order_no", "amount"],
  "field_types": {"amount": "number"},
  "regex_rules": {"order_no": "^[A-Z0-9-]+$"},
  "numeric_ranges": {"amount": {"min": 0}}
}"""
        )
        self.validation_rules_edit.setMinimumHeight(126)

        layout.addWidget(hint)
        layout.addWidget(form)
        layout.addWidget(QLabel("系统提示词"))
        layout.addWidget(self.system_prompt_edit)
        layout.addWidget(QLabel("提取提示词"))
        layout.addWidget(self.user_prompt_edit)
        layout.addWidget(self.advanced_options_panel)
        layout.addWidget(QLabel("结构化校验规则（JSON）"))
        layout.addWidget(self.validation_rules_edit)

        ai_profile_actions = QHBoxLayout()
        self.save_ai_profile_btn = QPushButton("保存AI配置")
        self.load_ai_profile_btn = QPushButton("加载AI配置")
        self._set_button_variant(self.save_ai_profile_btn, "secondary")
        self._set_button_variant(self.load_ai_profile_btn, "secondary")
        self.save_ai_profile_btn.clicked.connect(self.save_ai_profile)
        self.load_ai_profile_btn.clicked.connect(self.load_ai_profile)
        ai_profile_actions.addWidget(self.save_ai_profile_btn)
        ai_profile_actions.addWidget(self.load_ai_profile_btn)
        ai_profile_actions.addStretch(1)
        layout.addLayout(ai_profile_actions)
        self._update_ai_option_visibility()
        return page

    def _build_mapping_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        hint = QLabel(
            "推荐流程：先保存任务配置 -> 在下方选择任务 -> 采 1 次样本 -> 自动推断字段草案 -> 一键创建数据表。"
            "写库时将按字段名自动映射，无需手工维护映射表。"
        )
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)

        wizard_hint = QLabel(
            "采样必须使用已保存任务。你可以修改列名/类型/是否可空，再创建表。"
        )
        wizard_hint.setObjectName("sectionHint")
        wizard_hint.setWordWrap(True)

        task_row = QHBoxLayout()
        self.sample_job_combo = QComboBox()
        self.sample_job_combo.setMinimumWidth(320)
        self.sample_job_combo.addItem("请选择任务配置（先保存任务）", "")
        self.sample_job_combo.currentIndexChanged.connect(self._on_sample_job_changed)
        task_row.addWidget(QLabel("采样任务"))
        task_row.addWidget(self.sample_job_combo)
        task_row.addStretch(1)

        wizard_actions = QHBoxLayout()
        self.sample_batch_btn = QPushButton("采 1 次样本")
        self.ai_prompt_backfill_btn = QPushButton("AI回填提示词")
        self.create_table_btn = QPushButton("创建数据表")
        self.expand_schema_table_btn = QPushButton("放大查看字段表")
        self._set_button_variant(self.sample_batch_btn, "secondary")
        self._set_button_variant(self.ai_prompt_backfill_btn, "secondary")
        self._set_button_variant(self.create_table_btn, "success")
        self._set_button_variant(self.expand_schema_table_btn, "secondary")
        self.sample_batch_btn.clicked.connect(self.generate_samples_and_schema)
        self.ai_prompt_backfill_btn.clicked.connect(self.backfill_prompt_from_schema)
        self.create_table_btn.clicked.connect(self.create_table_from_schema)
        self.expand_schema_table_btn.clicked.connect(self.open_schema_table_zoom)
        wizard_actions.addWidget(self.sample_batch_btn)
        wizard_actions.addWidget(self.ai_prompt_backfill_btn)
        wizard_actions.addWidget(self.create_table_btn)
        wizard_actions.addWidget(self.expand_schema_table_btn)
        wizard_actions.addStretch(1)

        self.schema_table = QTableWidget(0, 8)
        self.schema_table.setHorizontalHeaderLabels([
            "启用",
            "来源字段",
            "列名",
            "JSON 类型",
            "SQL 类型",
            "可为空",
            "示例值",
            "出现次数",
        ])
        self.schema_table.verticalHeader().setVisible(False)
        schema_header = self.schema_table.horizontalHeader()
        schema_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        schema_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        schema_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        schema_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        schema_header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        schema_header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        schema_header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        schema_header.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)

        layout.addWidget(hint)
        layout.addWidget(wizard_hint)
        layout.addLayout(task_row)
        layout.addLayout(wizard_actions)
        layout.addWidget(self.schema_table)
        return page

    def _build_test_group(self) -> QGroupBox:
        group = QGroupBox("检查与测试")
        layout = QVBoxLayout(group)
        layout.setSpacing(8)

        hint = QLabel(
            "建议顺序：环境自检 -> 测试网关 -> 单次截图 -> 识别解析 -> 写入数据库。"
        )
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)

        self.precheck_btn = QPushButton("环境自检")
        self.gateway_test_btn = QPushButton("测试网关")
        self.capture_test_btn = QPushButton("单次截图")
        self.parse_test_btn = QPushButton("识别解析")
        self.db_test_btn = QPushButton("写入数据库")

        self._set_button_variant(self.precheck_btn, "secondary")
        self._set_button_variant(self.gateway_test_btn, "secondary")
        self._set_button_variant(self.capture_test_btn, "secondary")
        self._set_button_variant(self.parse_test_btn, "secondary")
        self._set_button_variant(self.db_test_btn, "success")

        self.precheck_btn.clicked.connect(self.precheck_current_job)
        self.gateway_test_btn.clicked.connect(self.test_gateway_current_job)
        self.capture_test_btn.clicked.connect(self.test_capture_current_job)
        self.parse_test_btn.clicked.connect(self.test_parse_current_job)
        self.db_test_btn.clicked.connect(self.test_database_current_job)

        buttons.addWidget(self.precheck_btn)
        buttons.addWidget(self.gateway_test_btn)
        buttons.addWidget(self.capture_test_btn)
        buttons.addWidget(self.parse_test_btn)
        buttons.addWidget(self.db_test_btn)

        action_state_row = QHBoxLayout()
        self.action_status_label = QLabel("空闲")
        self.action_status_label.setObjectName("infoPill")
        self.cancel_action_btn = QPushButton("取消当前操作")
        self._set_button_variant(self.cancel_action_btn, "danger")
        self.cancel_action_btn.setEnabled(False)
        self.cancel_action_btn.clicked.connect(self.cancel_current_action)
        action_state_row.addWidget(QLabel("操作状态"))
        action_state_row.addWidget(self.action_status_label)
        action_state_row.addStretch(1)
        action_state_row.addWidget(self.cancel_action_btn)

        layout.addWidget(hint)
        layout.addLayout(buttons)
        layout.addLayout(action_state_row)
        return group

    def _build_action_row(self) -> QWidget:
        panel = QWidget()
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        self.start_btn = QPushButton("启动全部已启用任务")
        self.stop_btn = QPushButton("停止全部任务")
        self.save_btn = QPushButton("保存配置")
        self.load_btn = QPushButton("加载配置")

        self._set_button_variant(self.start_btn, "success")
        self._set_button_variant(self.stop_btn, "danger")
        self._set_button_variant(self.save_btn, "secondary")
        self._set_button_variant(self.load_btn, "secondary")

        self.stop_btn.setEnabled(False)

        self.start_btn.clicked.connect(self.start_monitoring)
        self.stop_btn.clicked.connect(self.stop_monitoring)
        self.save_btn.clicked.connect(self.save_settings)
        self.load_btn.clicked.connect(self.load_settings)

        layout.addWidget(self.start_btn)
        layout.addWidget(self.stop_btn)
        layout.addStretch(1)
        layout.addWidget(self.save_btn)
        layout.addWidget(self.load_btn)
        return panel

    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setSpacing(10)

        preview_group = QGroupBox("截图预览")
        preview_layout = QVBoxLayout(preview_group)
        self.preview_info_label = QLabel("当前预览任务: -")
        self.preview_info_label.setObjectName("infoPill")
        self.preview_label = ClickableLabel("等待截图...")
        self.preview_label.setObjectName("previewLabel")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(240)
        self.preview_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preview_label.setToolTip("点击可编辑当前预览图，支持裁剪和红框标注")
        self.preview_label.clicked.connect(self.edit_current_preview)
        preview_layout.addWidget(self.preview_info_label)
        preview_layout.addWidget(self.preview_label)

        parsed_group = QGroupBox("最新结构化结果")
        parsed_layout = QVBoxLayout(parsed_group)
        self.parsed_text = QTextEdit()
        self.parsed_text.setReadOnly(True)
        self.parsed_text.setPlaceholderText("识别后的结构化 JSON 会显示在这里")
        parsed_layout.addWidget(self.parsed_text)

        sample_group = QGroupBox("采样结果")
        sample_layout = QVBoxLayout(sample_group)
        self.sample_results_text = QTextEdit()
        self.sample_results_text.setReadOnly(True)
        self.sample_results_text.setPlaceholderText("采集样本后，这里会汇总结构化结果和字段草案")
        sample_layout.addWidget(self.sample_results_text)

        raw_group = QGroupBox("原始模型输出 / OCR 文本")
        raw_layout = QVBoxLayout(raw_group)
        self.raw_text = QTextEdit()
        self.raw_text.setReadOnly(True)
        self.raw_text.setPlaceholderText("模型原始输出或 OCR 文本会显示在这里")
        raw_layout.addWidget(self.raw_text)

        log_group = QGroupBox("运行日志")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setPlaceholderText("运行日志和错误信息会显示在这里")
        log_layout.addWidget(self.log_text)

        layout.addWidget(preview_group, 2)
        layout.addWidget(parsed_group, 1)
        layout.addWidget(sample_group, 1)
        layout.addWidget(raw_group, 1)
        layout.addWidget(log_group, 1)
        return panel

    def _set_button_variant(self, button: QPushButton, variant: str) -> None:


        button.setProperty("variant", variant)
        button.style().unpolish(button)
        button.style().polish(button)

    def _set_manual_actions_enabled(self, enabled: bool) -> None:
        for name in [
            "capture_edit_init_btn",
            "capture_consistency_btn",
            "precheck_btn",
            "gateway_test_btn",
            "ai_probe_test_btn",
            "capture_test_btn",
            "parse_test_btn",
            "db_test_btn",
            "sample_batch_btn",
            "ai_prompt_backfill_btn",
            "create_table_btn",
            "expand_schema_table_btn",
        ]:
            if hasattr(self, name):
                getattr(self, name).setEnabled(enabled)

    def _set_action_status(self, text: str) -> None:
        if hasattr(self, "action_status_label"):
            self.action_status_label.setText(text)

    def _update_ai_option_visibility(self) -> None:
        running = bool(self._threads)
        advanced_enabled = bool(
            getattr(self, "enable_advanced_options_check", None)
            and self.enable_advanced_options_check.isChecked()
        )

        for name in ["temperature_spin", "max_tokens_spin", "image_detail_combo", "output_schema_edit"]:
            if hasattr(self, name):
                getattr(self, name).setEnabled(advanced_enabled and not running)

        if hasattr(self, "advanced_options_panel"):
            self.advanced_options_panel.setVisible(advanced_enabled)

    def cancel_current_action(self) -> None:
        if self._action_thread is None:
            return
        self._action_cancel_requested = True
        action_name = self._current_action_name or "当前操作"
        self._append_log(f"已请求取消：{action_name}，将尽快停止。")
        self._set_action_status(f"取消中: {action_name}")
        if hasattr(self, "cancel_action_btn"):
            self.cancel_action_btn.setEnabled(False)

    def _run_async_action(
        self,
        action_name: str,
        task: Callable[[], Any],
        on_success: Callable[[Any], None],
        error_title: str,
    ) -> None:
        if self._action_thread is not None:
            QMessageBox.information(self, "提示", "已有操作正在执行，请稍候。")
            return

        self._set_manual_actions_enabled(False)
        self._action_cancel_requested = False
        self._current_action_name = action_name
        self._current_action_error_title = error_title
        self._current_action_success_handler = on_success
        self._set_action_status(f"执行中: {action_name}")
        if hasattr(self, "cancel_action_btn"):
            self.cancel_action_btn.setEnabled(True)

        thread = QThread(self)
        worker = UiActionWorker(task)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.finished.connect(self._on_async_action_finished)
        worker.failed.connect(self._on_async_action_failed)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        def clear_refs() -> None:
            self._action_thread = None
            self._action_worker = None

        thread.finished.connect(clear_refs)

        self._action_thread = thread
        self._action_worker = worker
        thread.start()

    @Slot(object)
    def _on_async_action_finished(self, result: object) -> None:
        try:
            action_name = self._current_action_name or "当前操作"
            if self._action_cancel_requested:
                self._append_log(f"{action_name}已取消。")
                return
            handler = self._current_action_success_handler
            if handler is not None:
                handler(result)
        finally:
            self._finalize_async_action()

    @Slot(str)
    def _on_async_action_failed(self, message: str) -> None:
        try:
            action_name = self._current_action_name or "当前操作"
            if message == "操作已取消。" or self._action_cancel_requested:
                self._append_log(f"{action_name}已取消。")
                return
            self._append_log(f"{action_name}失败：{message}")
            self._show_validation_error(self._current_action_error_title or "操作失败", message)
        finally:
            self._finalize_async_action()

    def _finalize_async_action(self) -> None:
        thread = self._action_thread
        if thread is not None and thread.isRunning():
            thread.quit()

        self._set_manual_actions_enabled(True)
        if hasattr(self, "cancel_action_btn"):
            self.cancel_action_btn.setEnabled(False)
        if self._action_cancel_requested:
            self._set_action_status("已取消")
        else:
            self._set_action_status("空闲")

        self._action_cancel_requested = False
        self._current_action_name = ""
        self._current_action_error_title = ""
        self._current_action_success_handler = None

    def _update_parse_mode_ui(self) -> None:
        ai_enabled = True
        running = bool(self._threads)

        if hasattr(self, "editor_tabs"):
            self.editor_tabs.setTabEnabled(1, True)
            self.editor_tabs.setTabEnabled(2, True)
            self.editor_tabs.setTabEnabled(3, True)

        for name in [
            "gateway_protocol_combo",
            "base_url_edit",
            "api_key_edit",
            "model_edit",
            "timeout_spin",
            "retry_spin",
            "system_prompt_edit",
            "user_prompt_edit",
            "validation_rules_edit",
            "enable_advanced_options_check",
            "gateway_test_btn",
            "ai_probe_image_source_combo",
            "ai_probe_schema_check",
            "ai_probe_detail_combo",
            "ai_probe_test_btn",
            "sample_batch_btn",
            "create_table_btn",
            "sample_job_combo",
            "schema_table",
        ]:
            if hasattr(self, name):
                getattr(self, name).setEnabled(ai_enabled and not running)

        self._update_ai_option_visibility()

    def _add_schema_draft_row(self, draft: SchemaFieldDraft) -> None:
        row = self.schema_table.rowCount()
        self.schema_table.insertRow(row)

        is_fixed_id = draft.source_key == "record_id_ts" and draft.column_name == "id"

        include_item = QTableWidgetItem()
        if is_fixed_id:
            include_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
            include_item.setCheckState(Qt.CheckState.Checked)
        else:
            include_item.setFlags(include_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            include_item.setCheckState(Qt.CheckState.Checked if draft.include else Qt.CheckState.Unchecked)
        self.schema_table.setItem(row, 0, include_item)

        source_text = "系统时间戳主键" if is_fixed_id else draft.source_key
        source_item = QTableWidgetItem(source_text)
        column_item = QTableWidgetItem(draft.column_name)
        json_type_item = QTableWidgetItem(draft.json_type)
        if is_fixed_id:
            source_item.setFlags(source_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            column_item.setFlags(column_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            json_type_item.setFlags(json_type_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

        self.schema_table.setItem(row, 1, source_item)
        self.schema_table.setItem(row, 2, column_item)
        self.schema_table.setItem(row, 3, json_type_item)

        type_combo = QComboBox()
        type_combo.addItems(["TEXT", "INTEGER", "FLOAT", "BOOLEAN", "DATETIME", "JSON"])
        type_combo.setCurrentText(draft.db_type)
        if is_fixed_id:
            type_combo.setEnabled(False)
        self.schema_table.setCellWidget(row, 4, type_combo)

        nullable_item = QTableWidgetItem()
        if is_fixed_id:
            nullable_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            nullable_item.setCheckState(Qt.CheckState.Unchecked)
        else:
            nullable_item.setFlags(nullable_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            nullable_item.setCheckState(Qt.CheckState.Checked if draft.nullable else Qt.CheckState.Unchecked)
        self.schema_table.setItem(row, 5, nullable_item)
        self.schema_table.setItem(row, 6, QTableWidgetItem(draft.sample_value))
        self.schema_table.setItem(row, 7, QTableWidgetItem(str(draft.present_count)))

    def _populate_schema_drafts(self, drafts: list[SchemaFieldDraft]) -> None:
        fixed_id_draft = SchemaFieldDraft(
            source_key="record_id_ts",
            column_name="id",
            json_type="timestamp(us)",
            db_type="INTEGER",
            nullable=False,
            include=True,
            sample_value="自动生成: 插入时当前时间戳(微秒)",
            present_count=0,
        )
        all_drafts = [fixed_id_draft] + list(drafts)
        self._latest_schema_drafts = all_drafts
        self.schema_table.setRowCount(0)
        for draft in all_drafts:
            self._add_schema_draft_row(draft)

    def _collect_schema_drafts(self) -> list[SchemaFieldDraft]:
        drafts: list[SchemaFieldDraft] = []
        for row in range(self.schema_table.rowCount()):
            include_item = self.schema_table.item(row, 0)
            source_item = self.schema_table.item(row, 1)
            column_item = self.schema_table.item(row, 2)
            json_type_item = self.schema_table.item(row, 3)
            nullable_item = self.schema_table.item(row, 5)
            sample_item = self.schema_table.item(row, 6)
            present_item = self.schema_table.item(row, 7)
            type_widget = self.schema_table.cellWidget(row, 4)

            source_key = source_item.text().strip() if source_item else ""
            column_name = column_item.text().strip() if column_item else ""
            json_type = json_type_item.text().strip() if json_type_item else "string"
            db_type = type_widget.currentText().strip() if isinstance(type_widget, QComboBox) else "TEXT"
            sample_value = sample_item.text() if sample_item else ""
            present_count = int(present_item.text()) if present_item and present_item.text().strip().isdigit() else 0
            include = include_item.checkState() == Qt.CheckState.Checked if include_item else True
            nullable = nullable_item.checkState() == Qt.CheckState.Checked if nullable_item else True

            if not source_key and not column_name:
                continue
            if not source_key or not column_name:
                raise ValueError(f"草案第 {row + 1} 行需要同时填写来源字段和列名。")

            if column_name == "id" and source_key in {"系统时间戳主键", "record_id_ts"}:
                continue

            drafts.append(
                SchemaFieldDraft(
                    source_key=source_key,
                    column_name=column_name,
                    json_type=json_type or "string",
                    db_type=db_type if db_type in {"TEXT", "INTEGER", "FLOAT", "BOOLEAN", "DATETIME", "JSON"} else "TEXT",
                    nullable=nullable,
                    include=include,
                    sample_value=sample_value,
                    present_count=present_count,
                )
            )
        return drafts

    def _show_sample_results(self, samples: list[SampleExtractionResult]) -> None:
        payload = []
        for sample in samples:
            payload.append(
                {
                    "sample_index": sample.sample_index,
                    "screenshot_path": sample.screenshot_path,
                    "validation_errors": sample.validation_errors,
                    "data": sample.parsed_data,
                }
            )
        self.sample_results_text.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))

    def open_schema_table_zoom(self) -> None:
        if self.schema_table.rowCount() <= 0:
            QMessageBox.information(self, "提示", "当前没有可放大查看的字段草案。")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("字段草案（放大查看）")
        dialog.resize(1280, 760)

        root = QVBoxLayout(dialog)
        hint = QLabel("此视图用于放大查看字段草案。请在主表中进行编辑。")
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        zoom_table = QTableWidget(self.schema_table.rowCount(), self.schema_table.columnCount(), dialog)
        zoom_table.setHorizontalHeaderLabels(
            [self.schema_table.horizontalHeaderItem(i).text() for i in range(self.schema_table.columnCount())]
        )
        zoom_table.verticalHeader().setVisible(False)
        zoom_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        zoom_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        zoom_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)

        header = zoom_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)

        for row in range(self.schema_table.rowCount()):
            for col in range(self.schema_table.columnCount()):
                source_item = self.schema_table.item(row, col)
                if source_item is not None:
                    clone = QTableWidgetItem(source_item.text())
                    clone.setFlags(clone.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    if source_item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                        clone.setFlags(clone.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        clone.setCheckState(source_item.checkState())
                    zoom_table.setItem(row, col, clone)
                    continue

                source_widget = self.schema_table.cellWidget(row, col)
                if isinstance(source_widget, QComboBox):
                    text_item = QTableWidgetItem(source_widget.currentText())
                    text_item.setFlags(text_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    zoom_table.setItem(row, col, text_item)

        root.addWidget(zoom_table, 1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dialog.accept)
        close_row.addWidget(close_btn)
        root.addLayout(close_row)

        dialog.exec()

    def refresh_windows(self) -> None:
        previous_hwnd = self.window_combo.currentData() if hasattr(self, "window_combo") else 0

        windows = self.window_service.list_windows()
        self._windows = {w.hwnd: w for w in windows}

        self.window_combo.clear()
        self.window_combo.addItem("请选择目标窗口", 0)
        for item in windows:
            label = f"{item.title}  (HWND={item.hwnd})"
            self.window_combo.addItem(label, item.hwnd)

        target_hwnd = previous_hwnd
        if target_hwnd not in self._windows and self._pending_window_hwnd in self._windows:
            target_hwnd = self._pending_window_hwnd

        if target_hwnd in self._windows:
            idx = self.window_combo.findData(target_hwnd)
            if idx >= 0:
                self.window_combo.setCurrentIndex(idx)
        else:
            self.window_combo.setCurrentIndex(0)

        self._pending_window_hwnd = None
        self._append_log(f"窗口列表已刷新，共 {len(windows)} 个可监控窗口。")

    def new_job(self) -> None:
        self._editing_job_id = None
        self._clear_editor()
        ai_config = self.config_store.load_ai_config()
        if ai_config is not None:
            self._apply_ai_config(ai_config)
        self.jobs_table.clearSelection()
        self.editor_tabs.setCurrentIndex(0)

    def capture_and_edit_for_task_config(self) -> None:
        try:
            job = self._collect_job_from_editor(require_window=True, require_storage=False)
            db_url = self.db_url_edit.text().strip() or "sqlite:///../../data/monitor.db"
            self._preflight_job(job, db_url=db_url, require_extraction=False, require_storage=False)
            screenshot_path, _, _ = self._capture_once(job, suffix="config", apply_adjustments=True)
            self._show_capture_result(job, screenshot_path, raw_baseline=False)
            self._append_log("配置编辑已按当前任务配置生成标准截图，保证同一任务截图尺寸与处理一致。")
            self.edit_current_preview(suppress_non_raw_warning=True)
        except Exception as exc:
            self._append_log(f"单次截图并编辑失败：{exc}")
            self._show_validation_error("单次截图并编辑失败", str(exc))

    def save_job(self) -> None:
        existing_ids = set(self._jobs.keys())
        input_name = self.job_name_edit.text().strip()

        if not input_name:
            self._editing_job_id = None
        else:
            matched_job_id = next(
                (item.job_id for item in self._jobs.values() if item.name.strip() == input_name),
                None,
            )
            self._editing_job_id = matched_job_id

        try:
            job = self._sync_editor_job_or_raise(require_window=True, require_storage=False)
        except Exception as exc:
            self._show_validation_error("保存任务失败", str(exc))
            return

        if job is None:
            QMessageBox.information(self, "提示", "当前没有可保存的任务内容。")
            return

        action = "更新" if job.job_id in existing_ids else "新增"
        self._append_log(f"任务已{action}并显示在列表: {self._job_tag(job.job_id)}")

    def delete_job(self) -> None:
        job_id = self._selected_job_id()
        if not job_id:
            QMessageBox.information(self, "提示", "请先在任务列表中选择要删除的任务。")
            return

        if job_id in self._workers:
            QMessageBox.warning(self, "提示", "该任务正在运行，请先停止全部任务。")
            return

        self._jobs.pop(job_id, None)
        self._last_pixmaps.pop(job_id, None)
        self._last_image_paths.pop(job_id, None)
        self._preview_raw_baseline.pop(job_id, None)
        self._preview_labels.pop(job_id, None)
        next_job_id = next(iter(self._jobs.keys()), None)
        self._refresh_jobs_table(select_job_id=next_job_id)
        if next_job_id and next_job_id in self._jobs:
            self._load_job_into_editor(self._jobs[next_job_id])
        else:
            self.new_job()
        self._append_log(f"任务已删除: {job_id}")

    def start_monitoring(self) -> None:
        if self._threads:
            QMessageBox.information(self, "提示", "监控任务已经在运行。")
            return

        try:
            self._sync_editor_job_or_raise(require_window=True, require_storage=False)
        except Exception as exc:
            self._append_log(f"启动前校验失败: {exc}")
            self._show_validation_error("启动前校验失败", str(exc))
            return

        enabled_jobs = [job for job in self._jobs.values() if job.enabled]
        if not enabled_jobs:
            QMessageBox.warning(self, "提示", "没有可运行任务。请先保存并启用至少一个任务。")
            return

        db_url = self.db_url_edit.text().strip() or "sqlite:///../../data/monitor.db"
        runtimes: list[tuple[MonitorJob, MonitorPipeline]] = []

        try:
            for job in enabled_jobs:
                self._preflight_job(job, db_url=db_url, require_extraction=True, require_storage=True)
                runtimes.append((job, self._build_pipeline(job, db_url=db_url)))
        except Exception as exc:
            self._append_log(f"启动失败: {exc}")
            self._show_validation_error("启动失败", str(exc))
            return

        self._set_running(True)

        for job, pipeline in runtimes:
            worker = MonitorWorker(pipeline, job)
            thread = QThread(self)
            worker.moveToThread(thread)

            thread.started.connect(worker.run)
            worker.status_changed.connect(self._on_status_changed)
            worker.snapshot_ready.connect(self._on_snapshot_ready)
            worker.parsed_ready.connect(self._on_parsed_ready)
            worker.raw_text_ready.connect(self._on_raw_text_ready)
            worker.error.connect(self._on_error)
            worker.log.connect(self._on_worker_log)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            worker.finished.connect(self._on_worker_finished)
            thread.finished.connect(lambda job_id=job.job_id: self._on_thread_finished(job_id))

            self._workers[job.job_id] = worker
            self._threads[job.job_id] = thread

        for thread in self._threads.values():
            thread.start()

        self._append_log(f"已启动 {len(runtimes)} 个任务。")
        self._update_status_badge()

    def stop_monitoring(self) -> None:
        if not self._workers:
            QMessageBox.information(self, "提示", "当前没有正在运行的任务。")
            return

        self._stop_jobs(list(self._workers.keys()), action_label="停止全部任务")

    def stop_selected_monitoring(self) -> None:
        if not self._workers:
            QMessageBox.information(self, "提示", "当前没有正在运行的任务。")
            return

        checked_job_ids = [job.job_id for job in self._jobs.values() if job.enabled]
        target_ids = [job_id for job_id in checked_job_ids if job_id in self._workers]
        if not target_ids:
            QMessageBox.information(self, "提示", "没有可停止的勾选运行任务。")
            return

        self._stop_jobs(target_ids, action_label="停止勾选任务")

    def _stop_jobs(self, job_ids: list[str], action_label: str) -> None:
        active_ids = [job_id for job_id in job_ids if job_id in self._workers]
        if not active_ids:
            return

        for job_id in active_ids:
            worker = self._workers.get(job_id)
            if worker is not None:
                worker.stop()

        self._append_log(f"{action_label}：已发出停止指令，任务数={len(active_ids)}，等待安全退出。")
        QTimer.singleShot(2500, lambda ids=list(active_ids), label=action_label: self._force_stop_if_still_running(ids, label))

    def _force_stop_if_still_running(self, job_ids: list[str], action_label: str) -> None:
        forced = 0
        for job_id in job_ids:
            thread = self._threads.get(job_id)
            if thread is not None and thread.isRunning():
                thread.terminate()
                forced += 1

        if forced > 0:
            self._append_log(f"{action_label}：有 {forced} 个任务未及时退出，已执行强制停止。")

    def save_settings(self) -> None:
        try:
            self._sync_editor_job_or_raise(require_window=True, require_storage=True)
        except Exception as exc:
            self._append_log(f"保存配置失败: {exc}")
            self._show_validation_error("保存配置失败", str(exc))
            return

        settings = AppSettings(
            db_url=self.db_url_edit.text().strip() or "sqlite:///../../data/monitor.db",
            jobs=list(self._jobs.values()),
        )
        self.config_store.save(settings)
        self._append_log("配置已写入本地。")
        QMessageBox.information(self, "提示", "配置已保存。")

    def _persist_settings_silent(self) -> None:
        settings = AppSettings(
            db_url=self.db_url_edit.text().strip() or "sqlite:///../../data/monitor.db",
            jobs=list(self._jobs.values()),
        )
        self.config_store.save(settings)

    def save_ai_profile(self) -> None:
        try:
            ai_config = self._collect_ai_config()
            self.config_store.save_ai_config(ai_config)
        except Exception as exc:
            self._append_log(f"保存AI配置失败: {exc}")
            self._show_validation_error("保存AI配置失败", str(exc))
            return

        self._append_log("AI 配置已单独保存。")
        QMessageBox.information(self, "提示", "AI 配置已保存。")

    def load_ai_profile(self) -> None:
        ai_config = self.config_store.load_ai_config()
        if ai_config is None:
            QMessageBox.information(self, "提示", "未找到已保存的 AI 配置。")
            return

        self._apply_ai_config(ai_config)
        self._append_log("AI 配置已加载。")
        QMessageBox.information(self, "提示", "AI 配置已加载。")

    def load_settings(self) -> None:
        settings = self.config_store.load()
        if settings is None:
            QMessageBox.information(self, "提示", "未找到已保存配置。")
            return

        self._apply_settings(settings)
        self._append_log("配置已加载。")

    def test_database_connection(self) -> None:
        db_url = self.db_url_edit.text().strip() or "sqlite:///../../data/monitor.db"

        def task() -> dict[str, Any]:
            if self._action_cancel_requested:
                raise RuntimeError("操作已取消。")
            manager = SqlAlchemySchemaManager(db_url)
            with manager.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            inspector = inspect(manager.engine)
            table_names = inspector.get_table_names()
            return {
                "dialect": manager.engine.dialect.name,
                "table_count": len(table_names),
            }

        def on_success(result: dict[str, Any]) -> None:
            dialect = str(result.get("dialect", "unknown"))
            table_count = int(result.get("table_count", 0))
            self._append_log(f"数据库连接成功：{dialect}，当前表数量={table_count}")
            QMessageBox.information(
                self,
                "提示",
                f"数据库连接成功。\n数据库类型: {dialect}\n当前表数量: {table_count}",
            )

        self._run_async_action("测试数据库连接", task, on_success, "数据库连接失败")

    def precheck_current_job(self) -> None:
        try:
            has_storage = bool(self.table_name_edit.text().strip())
            job = self._collect_job_from_editor(require_window=True, require_storage=has_storage)
            db_url = self.db_url_edit.text().strip() or "sqlite:///../../data/monitor.db"
            self._preflight_job(job, db_url=db_url, require_extraction=True, require_storage=has_storage)
        except Exception as exc:
            self._append_log(f"环境自检失败：{exc}")
            self._show_validation_error("环境自检失败", str(exc))
            return

        self._append_log(f"环境自检通过：{job.name or job.job_id}")
        QMessageBox.information(self, "提示", "当前任务的环境检查已通过。")

    def test_gateway_current_job(self) -> None:
        try:
            job = self._collect_job_from_editor(require_window=False, require_storage=False)
            self._validate_ai_config(job)
        except Exception as exc:
            self._append_log(f"测试网关失败：{exc}")
            self._show_validation_error("测试网关失败", str(exc))
            return

        def task() -> str:
            if self._action_cancel_requested:
                raise RuntimeError("操作已取消。")
            return OpenAIGatewayClient(job.ai_config).healthcheck()

        def on_success(raw_text: str) -> None:
            self.raw_text.setPlainText(raw_text)
            self._append_log(f"测试网关通过：{job.ai_config.protocol} / {job.ai_config.model}")
            QMessageBox.information(self, "提示", "网关联通成功，模型已返回可解析的 JSON 响应。")

        self._run_async_action("测试网关", task, on_success, "测试网关失败")

    def test_ai_probe_current_job(self) -> None:
        try:
            job = self._collect_job_from_editor(require_window=False, require_storage=False)
            self._validate_ai_config(job)

            source_mode = str(self.ai_probe_image_source_combo.currentData() or "preview")
            include_schema = self.ai_probe_schema_check.isChecked()
            image_detail = str(self.ai_probe_detail_combo.currentData() or "")

            schema_payload: dict[str, Any] | None = None
            if include_schema:
                schema_payload = load_output_schema(job.ai_config.output_schema_text)

            if source_mode == "preview":
                selected_job_id = self._selected_job_id()
                target_job_id = selected_job_id if selected_job_id in self._last_image_paths else self._last_preview_job_id
                if not target_job_id:
                    raise ValueError("未找到可用预览图。请先执行一次截图或识别。")
                image_path = self._last_image_paths.get(target_job_id, "").strip()
                if not image_path:
                    raise ValueError("当前预览图路径为空。请先执行一次截图。")
            else:
                image_path = ""
        except Exception as exc:
            self._append_log(f"AI测试失败：{exc}")
            self._show_validation_error("AI测试失败", str(exc))
            return

        def task() -> dict[str, Any]:
            if self._action_cancel_requested:
                raise RuntimeError("操作已取消。")

            if source_mode == "preview" and image_path:
                with Image.open(image_path) as opened:
                    image = opened.convert("RGB")
            else:
                image = Image.new("RGB", (32, 32), color="white")

            result = OpenAIGatewayClient(job.ai_config).generate_json_text(
                image=image,
                schema_payload=schema_payload,
                include_schema=include_schema,
                image_detail=image_detail,
            )
            return {
                "text": result.text,
                "used_structured_output": result.used_structured_output,
                "image_source": source_mode,
                "detail": image_detail or "(omitted)",
                "response_json": result.response_json,
            }

        def on_success(result: dict[str, Any]) -> None:
            self.raw_text.setPlainText(str(result.get("text", "")))
            self.parsed_text.setPlainText(
                json.dumps(
                    {
                        "probe": "ai_gateway",
                        "protocol": job.ai_config.protocol,
                        "model": job.ai_config.model,
                        "image_source": result.get("image_source", ""),
                        "detail": result.get("detail", ""),
                        "used_structured_output": bool(result.get("used_structured_output", False)),
                        "response": result.get("response_json", {}),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            self._append_log(
                "AI测试完成："
                f"protocol={job.ai_config.protocol}, model={job.ai_config.model}, "
                f"schema={'on' if include_schema else 'off'}, detail={image_detail or '(omitted)'}"
            )
            QMessageBox.information(self, "提示", "AI 测试完成，请查看原始输出和结构化结果区域。")

        self._run_async_action("AI测试", task, on_success, "AI测试失败")

    def test_capture_current_job(self) -> None:
        try:
            job = self._collect_job_from_editor(require_window=True, require_storage=False)
            db_url = self.db_url_edit.text().strip() or "sqlite:///../../data/monitor.db"
            self._preflight_job(job, db_url=db_url, require_extraction=False, require_storage=False)
        except Exception as exc:
            self._append_log(f"单次截图失败：{exc}")
            self._show_validation_error("单次截图失败", str(exc))
            return

        def task() -> str:
            if self._action_cancel_requested:
                raise RuntimeError("操作已取消。")
            screenshot_path, _, _ = self._capture_once(job)
            if self._action_cancel_requested:
                raise RuntimeError("操作已取消。")
            return screenshot_path

        def on_success(screenshot_path: str) -> None:
            self._show_capture_result(job, screenshot_path)
            self._append_log(f"单次截图完成：{job.name or job.job_id} -> {screenshot_path}")

        self._run_async_action("单次截图", task, on_success, "单次截图失败")

    def validate_capture_consistency_current_job(self) -> None:
        try:
            job = self._collect_job_from_editor(require_window=True, require_storage=False)
            db_url = self.db_url_edit.text().strip() or "sqlite:///../../data/monitor.db"
            self._preflight_job(job, db_url=db_url, require_extraction=False, require_storage=False)
        except Exception as exc:
            self._append_log(f"截图一致性校验失败：{exc}")
            self._show_validation_error("截图一致性校验失败", str(exc))
            return

        sample_count = 3

        def task() -> dict[str, Any]:
            if self._action_cancel_requested:
                raise RuntimeError("操作已取消。")

            sizes: list[tuple[int, int]] = []
            modes: list[str] = []
            hashes: list[str] = []
            paths: list[str] = []

            for index in range(sample_count):
                if self._action_cancel_requested:
                    raise RuntimeError("操作已取消。")
                screenshot_path, image, _ = self._capture_once(job, suffix=f"consistency{index + 1}")
                normalized = image if image.mode == "RGB" else image.convert("RGB")
                sizes.append(tuple(int(v) for v in normalized.size))
                modes.append(normalized.mode)
                hashes.append(hashlib.sha256(normalized.tobytes()).hexdigest())
                paths.append(screenshot_path)
                time.sleep(0.12)

            return {
                "sizes": sizes,
                "modes": modes,
                "hashes": hashes,
                "paths": paths,
            }

        def on_success(result: dict[str, Any]) -> None:
            sizes = [tuple(item) for item in result.get("sizes", [])]
            modes = [str(item) for item in result.get("modes", [])]
            hashes = [str(item) for item in result.get("hashes", [])]
            paths = [str(item) for item in result.get("paths", [])]

            unique_sizes = sorted(set(sizes))
            unique_modes = sorted(set(modes))
            spec_consistent = len(unique_sizes) == 1 and len(unique_modes) == 1
            hash_consistent = len(set(hashes)) == 1 if hashes else False

            if paths:
                self._show_capture_result(job, paths[-1], raw_baseline=False)

            message_lines = [
                f"采样次数: {len(sizes)}",
                f"输入尺寸集合: {unique_sizes}",
                f"输入模式集合: {unique_modes}",
                f"像素哈希是否完全一致: {'是' if hash_consistent else '否'}",
            ]

            if spec_consistent:
                self._append_log(
                    "截图一致性校验通过：同一任务配置下，送模型输入规格（宽高/模式）保持一致。"
                )
                if not hash_consistent:
                    self._append_log(
                        "提示：像素哈希存在差异，通常由窗口内容实时变化引起，不影响输入规格一致性。"
                    )
                QMessageBox.information(self, "提示", "\n".join(message_lines + ["\n结论：输入规格一致。"]))
            else:
                self._append_log(
                    "截图一致性校验未通过：同一任务配置下，送模型输入规格出现变化，请重新初始化截图配置。"
                )
                QMessageBox.warning(self, "截图一致性校验未通过", "\n".join(message_lines + ["\n结论：输入规格不一致。"]))

        self._run_async_action("校验截图一致性", task, on_success, "截图一致性校验失败")

    def test_parse_current_job(self) -> None:
        try:
            job = self._collect_job_from_editor(require_window=True, require_storage=False)
            db_url = self.db_url_edit.text().strip() or "sqlite:///../../data/monitor.db"
            self._preflight_job(job, db_url=db_url, require_extraction=True, require_storage=False)
        except Exception as exc:
            self._append_log(f"识别解析失败：{exc}")
            self._show_validation_error("识别解析失败", str(exc))
            return

        def task() -> dict[str, Any]:
            if self._action_cancel_requested:
                raise RuntimeError("操作已取消。")
            screenshot_path, image, window_title = self._capture_once(job)
            if self._action_cancel_requested:
                raise RuntimeError("操作已取消。")
            extractor = build_extractor_for_job(job)
            extraction = extractor.extract(image)
            if self._action_cancel_requested:
                raise RuntimeError("操作已取消。")
            return {
                "screenshot_path": screenshot_path,
                "window_title": window_title,
                "raw_text": extraction.raw_text,
                "attempt_count": extraction.attempt_count,
                "validation_errors": extraction.validation_errors,
                "parsed_data": extraction.parsed_data,
            }

        def on_success(result: dict[str, Any]) -> None:
            self._show_capture_result(job, str(result["screenshot_path"]))
            self.raw_text.setPlainText(str(result["raw_text"]))
            self.parsed_text.setPlainText(
                json.dumps(
                    {
                        "job_id": job.job_id,
                        "job_name": job.name,
                        "window_title": result["window_title"],
                        "parse_mode": job.parse_mode,
                        "attempt_count": result["attempt_count"],
                        "validation_errors": result["validation_errors"],
                        "data": result["parsed_data"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            parsed_data = result.get("parsed_data", {})
            size = len(parsed_data) if isinstance(parsed_data, dict) else 0
            self._append_log(f"识别解析完成：{job.name or job.job_id}，字段数={size}")

        self._run_async_action("识别解析", task, on_success, "识别解析失败")

    def generate_samples_and_schema(self) -> None:
        try:
            selected_job_id = str(self.sample_job_combo.currentData() or "").strip() if hasattr(self, "sample_job_combo") else ""
            if not selected_job_id:
                raise ValueError("请先在一次采样建表中选择已保存的任务配置。")

            job = self._jobs.get(selected_job_id)
            if job is None:
                raise ValueError("所选任务配置不存在，请先保存任务后再采样。")

            # Keep subsequent parse/test operations aligned with the sampled task config.
            self._load_job_into_editor(job)

            db_url = self.db_url_edit.text().strip() or "sqlite:///../../data/monitor.db"
            self._preflight_job(job, db_url=db_url, require_extraction=True, require_storage=False)
        except Exception as exc:
            self._append_log(f"生成字段草案失败：{exc}")
            self._show_validation_error("生成字段草案失败", str(exc))
            return

        def task() -> dict[str, Any]:
            extractor = build_extractor_for_job(job)
            samples: list[SampleExtractionResult] = []
            last_image_path = ""
            last_raw_text = ""
            for sample_index in range(1, 2):
                if self._action_cancel_requested:
                    raise RuntimeError("操作已取消。")
                screenshot_path, image, _ = self._capture_once(job, suffix=f"sample{sample_index}")
                if self._action_cancel_requested:
                    raise RuntimeError("操作已取消。")
                extraction = extractor.extract(image)
                if self._action_cancel_requested:
                    raise RuntimeError("操作已取消。")
                samples.append(
                    SampleExtractionResult(
                        sample_index=sample_index,
                        screenshot_path=screenshot_path,
                        raw_text=extraction.raw_text,
                        parsed_data=extraction.parsed_data,
                        validation_errors=extraction.validation_errors,
                    )
                )
                last_image_path = screenshot_path
                last_raw_text = extraction.raw_text

            drafts = infer_schema_drafts(samples)
            if not drafts:
                raise ValueError("本次样本没有生成可用的字段草案。")

            return {
                "samples": samples,
                "drafts": drafts,
                "last_image_path": last_image_path,
                "last_raw_text": last_raw_text,
            }

        def on_success(result: dict[str, Any]) -> None:
            samples = result["samples"]
            drafts = result["drafts"]
            last_image_path = str(result.get("last_image_path", ""))
            last_raw_text = str(result.get("last_raw_text", ""))

            self._latest_sample_results = samples
            self._show_sample_results(samples)
            self._populate_schema_drafts(drafts)
            if last_image_path:
                self._show_capture_result(job, last_image_path)
            if last_raw_text:
                self.raw_text.setPlainText(last_raw_text)
            if not job.table_name.strip():
                suggested = self._suggest_table_name(job)
                job.table_name = suggested
                if self._editing_job_id == job.job_id:
                    self.table_name_edit.setText(suggested)
                self._refresh_jobs_table(select_job_id=job.job_id)

            self._append_log(f"采样分析完成：{job.name or job.job_id}，草案数={len(drafts)}")
            self.editor_tabs.setCurrentIndex(3)
            QMessageBox.information(self, "提示", "已根据 1 次样本生成字段草案。")

        self._run_async_action("一次采样分析", task, on_success, "生成字段草案失败")

    def create_table_from_schema(self) -> None:
        try:
            selected_job_id = str(self.sample_job_combo.currentData() or "").strip() if hasattr(self, "sample_job_combo") else ""
            if not selected_job_id:
                raise ValueError("请先在一次采样建表中选择任务配置。")

            job = self._jobs.get(selected_job_id)
            if job is None:
                raise ValueError("所选任务配置不存在，请先保存任务后再创建数据表。")

            table_name = job.table_name.strip()
            if not table_name:
                raise ValueError("所选任务未配置目标表名，请先在任务配置中填写并保存。")

            all_drafts = self._collect_schema_drafts()
            drafts = [draft for draft in all_drafts if draft.include]
            if not drafts:
                raise ValueError("请先勾选至少一个字段草案。")

            # 回填界面仅保留勾选字段，避免未启用字段影响后续建表调整。
            drafts_snapshot = list(drafts)
            sample_snapshot = list(self._latest_sample_results)

            # Align editor fields with the selected task for predictable backfill behavior.
            self._load_job_into_editor(job)
            self._populate_schema_drafts(drafts_snapshot)
            if sample_snapshot:
                self._show_sample_results(sample_snapshot)

            db_url = self.db_url_edit.text().strip() or "sqlite:///../../data/monitor.db"
            manager = SqlAlchemySchemaManager(db_url)
            description = manager.create_table(table_name, drafts)
            self._apply_auto_validation_rules(drafts)

            job.ai_config.user_prompt = self.user_prompt_edit.toPlainText().strip() or DEFAULT_AI_USER_PROMPT
            job.ai_config.validation_rules_text = self.validation_rules_edit.toPlainText().strip()
            self._jobs[job.job_id] = job
            self._refresh_jobs_table(select_job_id=job.job_id)
            self._persist_settings_silent()

            self.parsed_text.setPlainText(
                json.dumps(
                    {
                        "table_name": table_name,
                        "columns": description,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        except Exception as exc:
            self._append_log(f"创建数据表失败：{exc}")
            self._show_validation_error("创建数据表失败", str(exc))
            return

        self._append_log(f"已创建数据表：{table_name}")
        self._append_log("已自动保存任务配置。")
        QMessageBox.information(self, "提示", f"数据表 {table_name} 已创建。")

    def backfill_prompt_from_schema(self) -> None:
        try:
            selected_job_id = str(self.sample_job_combo.currentData() or "").strip() if hasattr(self, "sample_job_combo") else ""
            if not selected_job_id:
                raise ValueError("请先在一次采样建表中选择任务配置。")

            job = self._jobs.get(selected_job_id)
            if job is None:
                raise ValueError("所选任务配置不存在，请先保存任务后再回填提示词。")

            all_drafts = self._collect_schema_drafts()
            drafts = [draft for draft in all_drafts if draft.include]
            if not drafts:
                raise ValueError("请先勾选至少一个字段草案。")

            drafts_snapshot = list(drafts)
            sample_snapshot = list(self._latest_sample_results)

            self._load_job_into_editor(job)
            self._populate_schema_drafts(drafts_snapshot)
            if sample_snapshot:
                self._show_sample_results(sample_snapshot)

            try:
                recommended_prompt = self._recommend_user_prompt_via_ai(job, drafts)
            except Exception as prompt_exc:
                self._append_log(f"提示词智能回填失败，已回退本地规则：{prompt_exc}")
                self._apply_auto_prompt_keywords(drafts)
            else:
                self._apply_ai_recommended_prompt(recommended_prompt)
                self._append_log("已基于启用字段调用模型生成提取提示词并合并回填。")

            self._apply_auto_validation_rules(drafts)
            job.ai_config.user_prompt = self.user_prompt_edit.toPlainText().strip() or DEFAULT_AI_USER_PROMPT
            job.ai_config.validation_rules_text = self.validation_rules_edit.toPlainText().strip()
            self._jobs[job.job_id] = job
            self._refresh_jobs_table(select_job_id=job.job_id)
            self._persist_settings_silent()
        except Exception as exc:
            self._append_log(f"AI回填提示词失败：{exc}")
            self._show_validation_error("AI回填提示词失败", str(exc))
            return

        self._append_log("已自动保存任务配置，后续自动化识别将使用当前完整提示词。")
        QMessageBox.information(self, "提示", "AI 提示词回填完成。")

    def _apply_auto_prompt_keywords(self, drafts: list[SchemaFieldDraft]) -> None:
        keywords = [draft.source_key.strip() for draft in drafts if draft.source_key.strip()]
        if not keywords:
            return

        unique_keywords = sorted(set(keywords))
        keyword_line = "、".join(unique_keywords)
        auto_block = (
            "[AUTO_KEYWORDS_BEGIN]\n"
            f"请重点提取以下字段：{keyword_line}。\n"
            "若字段缺失或无法判断，请返回 null。\n"
            "[AUTO_KEYWORDS_END]"
        )

        current = self.user_prompt_edit.toPlainText().strip() or DEFAULT_AI_USER_PROMPT
        updated = self._replace_or_append_block(
            text=current,
            begin_marker="[AUTO_KEYWORDS_BEGIN]",
            end_marker="[AUTO_KEYWORDS_END]",
            block=auto_block,
        )
        self.user_prompt_edit.setPlainText(updated)

    def _apply_ai_recommended_prompt(self, recommended_prompt: str) -> None:
        text = (recommended_prompt or "").strip()
        if not text:
            raise ValueError("模型返回的回填提示词为空。")

        ai_block = (
            "[AUTO_MODEL_PROMPT_BEGIN]\n"
            f"{text}\n"
            "[AUTO_MODEL_PROMPT_END]"
        )
        current = self.user_prompt_edit.toPlainText().strip() or DEFAULT_AI_USER_PROMPT
        updated = self._replace_or_append_block(
            text=current,
            begin_marker="[AUTO_MODEL_PROMPT_BEGIN]",
            end_marker="[AUTO_MODEL_PROMPT_END]",
            block=ai_block,
        )
        self.user_prompt_edit.setPlainText(updated)

    def _recommend_user_prompt_via_ai(self, job: MonitorJob, drafts: list[SchemaFieldDraft]) -> str:
        enabled_drafts = [draft for draft in drafts if draft.include and draft.source_key.strip()]
        if not enabled_drafts:
            raise ValueError("没有可用于生成提示词的启用字段。")

        field_lines = []
        for draft in enabled_drafts:
            nullable_text = "可为空" if draft.nullable else "必填"
            field_lines.append(
                f"- 字段名: {draft.source_key}; 列名: {draft.column_name}; JSON类型: {draft.json_type}; {nullable_text}"
            )

        advisor_config = AiGatewayConfig(
            protocol=job.ai_config.protocol,
            base_url=job.ai_config.base_url,
            api_key=job.ai_config.api_key,
            model=job.ai_config.model,
            system_prompt=(
                "你是资深数据抽取提示词工程师。"
                "你的任务是生成一个可直接用于截图结构化抽取的中文提取提示词。"
                "提示词必须准确约束字段，不允许模型臆测。"
            ),
            user_prompt=(
                "请根据以下字段定义生成提取提示词。"
                "仅输出 JSON 对象。\n\n"
                f"字段定义:\n{chr(10).join(field_lines)}\n\n"
                "要求:\n"
                "1) 明确逐字段提取要求。\n"
                "2) 对缺失字段统一返回 null。\n"
                "3) 不允许输出额外解释。\n"
                "4) 包含对数字、时间等格式的稳健约束。"
            ),
            enable_advanced_options=True,
            enable_generation_controls=False,
            enable_output_schema=True,
            image_detail="",
            output_schema_text="",
            validation_rules_text="",
            max_validation_retries=0,
            timeout_seconds=max(int(job.ai_config.timeout_seconds), 20),
            temperature=0.0,
            max_output_tokens=max(int(job.ai_config.max_output_tokens), 512),
        )

        schema_payload = {
            "type": "object",
            "properties": {
                "recommended_user_prompt": {"type": "string"},
            },
            "required": ["recommended_user_prompt"],
            "additionalProperties": False,
        }

        response = OpenAIGatewayClient(advisor_config).generate_json_text(
            image=Image.new("RGB", (24, 24), color="white"),
            schema_payload=schema_payload,
            include_schema=True,
            include_generation_controls=False,
            image_detail="",
        )
        payload = parse_json_object(response.text)
        prompt_text = str(payload.get("recommended_user_prompt", "")).strip()
        if not prompt_text:
            raise ValueError("模型未返回可用的 recommended_user_prompt。")
        return prompt_text

    def _apply_auto_validation_rules(self, drafts: list[SchemaFieldDraft]) -> None:
        required_fields = [draft.source_key.strip() for draft in drafts if draft.source_key.strip() and not draft.nullable]
        field_types: dict[str, str] = {}
        for draft in drafts:
            key = draft.source_key.strip()
            if not key:
                continue
            mapped = self._map_json_type_for_validation(draft.json_type)
            if mapped:
                field_types[key] = mapped

        existing_text = self.validation_rules_edit.toPlainText().strip()
        payload: dict[str, Any] = {}
        if existing_text:
            try:
                loaded = json.loads(existing_text)
                if isinstance(loaded, dict):
                    payload = loaded
            except json.JSONDecodeError:
                payload = {}

        if required_fields:
            payload["required_fields"] = sorted(set(required_fields))
        if field_types:
            payload["field_types"] = dict(sorted(field_types.items(), key=lambda x: x[0]))

        self.validation_rules_edit.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))

    @staticmethod
    def _replace_or_append_block(text: str, begin_marker: str, end_marker: str, block: str) -> str:
        begin_pos = text.find(begin_marker)
        end_pos = text.find(end_marker)
        if begin_pos >= 0 and end_pos > begin_pos:
            end_pos += len(end_marker)
            prefix = text[:begin_pos].rstrip()
            suffix = text[end_pos:].lstrip()
            parts = [part for part in [prefix, block, suffix] if part]
            return "\n\n".join(parts)

        if not text:
            return block
        return f"{text}\n\n{block}"

    @staticmethod
    def _map_json_type_for_validation(json_type: str) -> str:
        normalized = (json_type or "").strip().lower()
        if normalized in {"string", "integer", "number", "boolean", "object", "array", "null", "datetime"}:
            return normalized
        if normalized == "float":
            return "number"
        return "string"

    def test_database_current_job(self) -> None:
        try:
            job = self._collect_job_from_editor(require_window=True, require_storage=True)
            db_url = self.db_url_edit.text().strip() or "sqlite:///../../data/monitor.db"
            self._preflight_job(job, db_url=db_url, require_extraction=True, require_storage=True)
        except Exception as exc:
            self._append_log(f"写入数据库失败：{exc}")
            self._show_validation_error("写入数据库失败", str(exc))
            return

        def task() -> dict[str, Any]:
            if self._action_cancel_requested:
                raise RuntimeError("操作已取消。")
            pipeline = self._build_pipeline(job, db_url=db_url)
            output = pipeline.execute(job)
            if self._action_cancel_requested:
                raise RuntimeError("操作已取消。")
            return output.to_dict()

        def on_success(output_dict: dict[str, Any]) -> None:
            screenshot_path = str(output_dict.get("screenshot_path", ""))
            if screenshot_path:
                self._show_capture_result(job, screenshot_path)
            self.raw_text.setPlainText(str(output_dict.get("raw_text", "")))
            self.parsed_text.setPlainText(json.dumps(output_dict, ensure_ascii=False, indent=2))
            self._append_log(f"写入数据库完成：{job.name or job.job_id} -> {job.table_name}")
            QMessageBox.information(self, "提示", f"数据已写入 {job.table_name} 并完成预览。")

        self._run_async_action("写入数据库", task, on_success, "写入数据库失败")

    def _suggest_table_name(self, job: MonitorJob) -> str:
        seed = re.sub(r"[^A-Za-z0-9_]+", "_", (job.name or f"job_{job.job_id}").strip()).strip("_").lower()
        seed = seed or f"job_{job.job_id}"
        if seed[0].isdigit():
            seed = f"job_{seed}"
        return seed

    def _load_settings_on_startup(self) -> None:
        settings = self.config_store.load()
        if settings is None:
            self.new_job()
            if self.model_edit.text().strip():
                self._append_log("已自动加载 AI 配置。")
            return

        self._apply_settings(settings)
        self._append_log("已自动加载上次配置。")

    def _apply_settings(self, settings: AppSettings) -> None:
        self.db_url_edit.setText(settings.db_url)

        unique_jobs: dict[str, MonitorJob] = {}
        for job in settings.jobs:
            jid = job.job_id or create_job_id()
            if jid in unique_jobs:
                jid = create_job_id()
                job.job_id = jid
            unique_jobs[jid] = job
        self._jobs = unique_jobs

        selected_job_id = next(iter(self._jobs.keys()), None)
        self._refresh_jobs_table(select_job_id=selected_job_id)

        if selected_job_id:
            self._load_job_into_editor(self._jobs[selected_job_id])
        else:
            self.new_job()

        self.refresh_windows()

    def _refresh_jobs_table(self, select_job_id: str | None) -> None:
        self._syncing_job_table = True
        self.jobs_table.setRowCount(0)

        jobs_sorted = sorted(self._jobs.values(), key=lambda x: (x.name.lower(), x.job_id))
        for job in jobs_sorted:
            row = self.jobs_table.rowCount()
            self.jobs_table.insertRow(row)

            enabled_item = QTableWidgetItem()
            enabled_item.setFlags(enabled_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            enabled_item.setCheckState(Qt.CheckState.Checked if job.enabled else Qt.CheckState.Unchecked)

            self.jobs_table.setItem(row, 0, enabled_item)
            self.jobs_table.setItem(row, 1, QTableWidgetItem(job.job_id))
            self.jobs_table.setItem(row, 2, QTableWidgetItem(job.name or "(未命名任务)"))
            self.jobs_table.setItem(row, 3, QTableWidgetItem(job.window_title or str(job.window_hwnd)))
            self.jobs_table.setItem(row, 4, QTableWidgetItem(str(job.interval_seconds)))
            self.jobs_table.setItem(row, 5, QTableWidgetItem(job.table_name))
            mode_label = "AI 结构化"
            self.jobs_table.setItem(row, 6, QTableWidgetItem(mode_label))

        self._syncing_job_table = False
        self._select_job(select_job_id)
        self._refresh_sample_job_options(select_job_id)

    def _select_job(self, job_id: str | None) -> None:
        if not job_id:
            return
        for row in range(self.jobs_table.rowCount()):
            item = self.jobs_table.item(row, 1)
            if item and item.text() == job_id:
                self.jobs_table.selectRow(row)
                break

    def _on_jobs_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._syncing_job_table or item.column() != 0:
            return

        job_id_item = self.jobs_table.item(item.row(), 1)
        if job_id_item is None:
            return

        job_id = job_id_item.text().strip()
        job = self._jobs.get(job_id)
        if job is None:
            return

        job.enabled = item.checkState() == Qt.CheckState.Checked
        self._append_log(f"任务状态已更新: {self._job_tag(job_id)} -> {'启用' if job.enabled else '禁用'}")

    def _on_job_selection_changed(self) -> None:
        job_id = self._selected_job_id()
        if not job_id:
            return

        job = self._jobs.get(job_id)
        if job is None:
            return

        self.editor_tabs.setCurrentIndex(0)
        self._load_job_into_editor(job)
        if hasattr(self, "sample_job_combo"):
            sample_idx = self.sample_job_combo.findData(job_id)
            if sample_idx >= 0:
                self.sample_job_combo.setCurrentIndex(sample_idx)
        self._render_preview()

    def _on_sample_job_changed(self) -> None:
        # Selection is consumed by generate_samples_and_schema; no immediate side effects needed.
        return

    def _refresh_sample_job_options(self, preferred_job_id: str | None = None) -> None:
        if not hasattr(self, "sample_job_combo"):
            return

        current = str(self.sample_job_combo.currentData() or "")
        target = preferred_job_id or current
        self.sample_job_combo.blockSignals(True)
        self.sample_job_combo.clear()
        self.sample_job_combo.addItem("请选择任务配置（先保存任务）", "")

        jobs_sorted = sorted(self._jobs.values(), key=lambda x: (x.name.lower(), x.job_id))
        for job in jobs_sorted:
            label = f"{job.name or '(未命名任务)'} / {job.job_id}"
            self.sample_job_combo.addItem(label, job.job_id)

        idx = self.sample_job_combo.findData(target)
        self.sample_job_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.sample_job_combo.blockSignals(False)

    def _selected_job_id(self) -> str | None:
        selected_rows = self.jobs_table.selectionModel().selectedRows()
        if not selected_rows:
            return None

        row = selected_rows[0].row()
        item = self.jobs_table.item(row, 1)
        if item is None:
            return None
        return item.text().strip() or None

    def _load_job_into_editor(self, job: MonitorJob) -> None:
        self._editing_job_id = job.job_id
        self.job_id_label.setText(job.job_id)
        self.job_name_edit.setText(job.name)
        self.job_enabled_check.setChecked(job.enabled)

        self.interval_spin.setValue(max(1, job.interval_seconds))
        self.capture_dir_edit.setText(job.screenshot_dir)
        self.table_name_edit.setText(job.table_name)
        self._editor_crop_rect = tuple(job.crop_rect) if job.crop_rect is not None else None
        self._editor_mark_rects = [tuple(rect) for rect in job.mark_rects]
        self._update_capture_adjustment_summary()

        self._apply_ai_config(job.ai_config)

        self.schema_table.setRowCount(0)
        self.sample_results_text.clear()

        self._pending_window_hwnd = job.window_hwnd or None
        idx = self.window_combo.findData(job.window_hwnd)
        if idx >= 0:
            self.window_combo.setCurrentIndex(idx)
        else:
            self.window_combo.setCurrentIndex(0)

        self._update_parse_mode_ui()

    def _clear_editor(self) -> None:
        self.job_id_label.setText("(新任务)")
        self.job_name_edit.clear()
        self.job_enabled_check.setChecked(True)

        self.window_combo.setCurrentIndex(0)
        self.interval_spin.setValue(5)
        self.capture_dir_edit.setText("captures")
        self.table_name_edit.clear()

        self._apply_ai_config(AiGatewayConfig())

        self.schema_table.setRowCount(0)
        self.sample_results_text.clear()
        self.raw_text.clear()
        self.parsed_text.clear()
        self._latest_sample_results = []
        self._latest_schema_drafts = []
        self._editor_crop_rect = None
        self._editor_mark_rects = []
        self._update_capture_adjustment_summary()
        self._update_parse_mode_ui()

    def _collect_job_from_editor(self, require_window: bool, require_storage: bool = True) -> MonitorJob:
        job_id = self._editing_job_id or create_job_id()

        hwnd_data = self.window_combo.currentData()
        hwnd = int(hwnd_data) if hwnd_data is not None else 0
        if require_window and hwnd == 0:
            raise ValueError("请选择目标窗口。")

        window_title = self._windows.get(hwnd).title if hwnd in self._windows else ""
        name = self.job_name_edit.text().strip() or f"job-{job_id}"
        ai_config = self._collect_ai_config()

        table_name = self.table_name_edit.text().strip()
        if require_storage:
            if not table_name:
                raise ValueError("请填写目标表名。")

        return MonitorJob(
            job_id=job_id,
            name=name,
            enabled=self.job_enabled_check.isChecked(),
            window_hwnd=hwnd,
            window_title=window_title,
            interval_seconds=int(self.interval_spin.value()),
            parse_mode="ai_structured",
            ai_config=ai_config,
            screenshot_dir=self.capture_dir_edit.text().strip() or "captures",
            table_name=table_name,
            mappings=[],
            crop_rect=tuple(self._editor_crop_rect) if self._editor_crop_rect is not None else None,
            mark_rects=[tuple(rect) for rect in self._editor_mark_rects],
        )

    def _collect_ai_config(self) -> AiGatewayConfig:
        advanced_enabled = self.enable_advanced_options_check.isChecked()
        return AiGatewayConfig(
            protocol=str(self.gateway_protocol_combo.currentData() or "responses"),
            base_url=self.base_url_edit.text().strip() or "https://api.openai.com/v1",
            api_key=self.api_key_edit.text(),
            model=self.model_edit.text().strip(),
            system_prompt=self.system_prompt_edit.toPlainText().strip() or DEFAULT_AI_SYSTEM_PROMPT,
            user_prompt=self.user_prompt_edit.toPlainText().strip() or DEFAULT_AI_USER_PROMPT,
            enable_advanced_options=advanced_enabled,
            enable_generation_controls=advanced_enabled,
            enable_output_schema=advanced_enabled,
            image_detail=str(self.image_detail_combo.currentData() or ""),
            output_schema_text=self.output_schema_edit.toPlainText().strip(),
            validation_rules_text=self.validation_rules_edit.toPlainText().strip(),
            max_validation_retries=int(self.retry_spin.value()),
            timeout_seconds=int(self.timeout_spin.value()),
            temperature=float(self.temperature_spin.value()),
            max_output_tokens=int(self.max_tokens_spin.value()),
        )

    def _apply_ai_config(self, ai_config: AiGatewayConfig) -> None:
        protocol_index = self.gateway_protocol_combo.findData(ai_config.protocol)
        self.gateway_protocol_combo.setCurrentIndex(protocol_index if protocol_index >= 0 else 0)
        self.base_url_edit.setText(ai_config.base_url)
        self.api_key_edit.setText(ai_config.api_key)
        self.model_edit.setText(ai_config.model)
        self.timeout_spin.setValue(ai_config.timeout_seconds)
        self.retry_spin.setValue(ai_config.max_validation_retries)
        advanced_enabled = bool(
            ai_config.enable_advanced_options
            or ai_config.enable_generation_controls
            or ai_config.enable_output_schema
        )
        self.enable_advanced_options_check.setChecked(advanced_enabled)
        self.temperature_spin.setValue(ai_config.temperature)
        self.max_tokens_spin.setValue(ai_config.max_output_tokens)
        detail_index = self.image_detail_combo.findData(ai_config.image_detail)
        self.image_detail_combo.setCurrentIndex(detail_index if detail_index >= 0 else 0)
        self.system_prompt_edit.setPlainText(ai_config.system_prompt)
        self.user_prompt_edit.setPlainText(ai_config.user_prompt)
        self.output_schema_edit.setPlainText(ai_config.output_schema_text)
        self.validation_rules_edit.setPlainText(ai_config.validation_rules_text)
        self._update_ai_option_visibility()

    def _sync_editor_job_or_raise(self, require_window: bool, require_storage: bool = True) -> MonitorJob | None:
        if self._is_editor_empty():
            return None

        job = self._collect_job_from_editor(require_window=require_window, require_storage=require_storage)
        self._jobs[job.job_id] = job
        self._editing_job_id = job.job_id
        self._refresh_jobs_table(select_job_id=job.job_id)
        return job

    def _is_editor_empty(self) -> bool:
        has_name = bool(self.job_name_edit.text().strip())
        has_table = bool(self.table_name_edit.text().strip())
        has_window = bool(self.window_combo.currentData())
        has_model = bool(self.model_edit.text().strip())
        return not (has_name or has_table or has_window or has_model)

    def _preflight_job(
        self,
        job: MonitorJob,
        db_url: str,
        require_extraction: bool,
        require_storage: bool,
    ) -> None:
        if job.window_hwnd <= 0:
            raise ValueError("请选择目标窗口。")

        window = self.window_service.get_window(job.window_hwnd)
        if window is None:
            raise ValueError(f"任务 [{job.name}] 对应的窗口不存在或已关闭。")

        self.window_service.get_window_rect(job.window_hwnd)
        job.window_title = window.title

        capture_dir = Path(job.screenshot_dir)
        capture_dir.mkdir(parents=True, exist_ok=True)

        self._validate_ai_config(job)

        if require_storage:
            self._ensure_auto_mappings(job, db_url)
            self._validate_mappings(job)
            SqlAlchemyMappedRepository(
                db_url=db_url,
                table_name=job.table_name,
                mappings=job.mappings,
            )

    def _validate_ai_config(self, job: MonitorJob) -> None:
        if not job.ai_config.base_url.strip():
            raise ValueError("AI 结构化模式必须填写接口地址。")
        if not job.ai_config.model.strip():
            raise ValueError("AI 结构化模式必须填写模型名称。")
        advanced_enabled = bool(
            job.ai_config.enable_advanced_options
            or job.ai_config.enable_generation_controls
            or job.ai_config.enable_output_schema
        )
        if advanced_enabled and (job.ai_config.output_schema_text or "").strip():
            load_output_schema(job.ai_config.output_schema_text)
        parse_validation_rules(job.ai_config.validation_rules_text)

    def _validate_mappings(self, job: MonitorJob) -> None:
        if not job.table_name.strip():
            raise ValueError("请填写目标表名。")
        if not job.mappings:
            raise ValueError("未生成可用映射。请先完成一次采样并创建数据表，或确保表结构与输出字段同名。")

        seen_columns: set[str] = set()
        known_parsed_keys = self._expected_parsed_keys(job)
        for index, mapping in enumerate(job.mappings, start=1):
            if mapping.db_column in seen_columns:
                raise ValueError(f"数据库列 [{mapping.db_column}] 重复，请保证每个映射目标列唯一。")
            seen_columns.add(mapping.db_column)

            if mapping.source_type == "system" and mapping.source_key not in SqlAlchemyMappedRepository.SYSTEM_KEY_MAP:
                raise ValueError(f"映射第 {index} 行使用了不支持的系统字段：{mapping.source_key}")

            if mapping.source_type == "parsed" and known_parsed_keys and mapping.source_key not in known_parsed_keys:
                raise ValueError(
                    f"映射第 {index} 行引用了不存在的解析字段 [{mapping.source_key}]，请检查输出规范或规则配置。"
                )

    def _ensure_auto_mappings(self, job: MonitorJob, db_url: str) -> None:
        if not job.table_name.strip():
            return

        manager = SqlAlchemySchemaManager(db_url)
        columns = manager.describe_table(job.table_name)
        table_columns = {str(item.get("name", "")).strip() for item in columns if str(item.get("name", "")).strip()}
        if not table_columns:
            raise ValueError(f"目标表为空或不可读：{job.table_name}")

        mappings: list[DbFieldMapping] = []
        used_columns: set[str] = set()

        for source_key, db_column in [
            ("record_id_ts", "id"),
            ("captured_at", "captured_at"),
            ("job_id", "job_id"),
            ("job_name", "job_name"),
            ("window_hwnd", "window_hwnd"),
            ("window_title", "window_title"),
            ("screenshot_path", "screenshot_path"),
            ("raw_text", "raw_text"),
            ("parse_mode", "parse_mode"),
            ("model_name", "model_name"),
            ("gateway_protocol", "gateway_protocol"),
            ("attempt_count", "attempt_count"),
            ("validation_json", "validation_errors"),
            ("parsed_json", "parsed_json"),
        ]:
            if db_column in table_columns:
                mappings.append(DbFieldMapping(source_type="system", source_key=source_key, db_column=db_column))
                used_columns.add(db_column)

        for source_key, db_column in self._expected_parsed_column_pairs(job):
            if db_column in table_columns and db_column not in used_columns:
                mappings.append(DbFieldMapping(source_type="parsed", source_key=source_key, db_column=db_column))
                used_columns.add(db_column)

        # Fallback: when draft context is unavailable (e.g. app restart),
        # map remaining business columns by same-name from parsed JSON.
        for column_name in sorted(table_columns):
            if column_name in used_columns:
                continue
            mappings.append(DbFieldMapping(source_type="parsed", source_key=column_name, db_column=column_name))
            used_columns.add(column_name)

        job.mappings = mappings

    def _expected_parsed_column_pairs(self, job: MonitorJob) -> list[tuple[str, str]]:
        drafts = [draft for draft in self._collect_schema_drafts() if draft.include]
        if drafts:
            return [(draft.source_key, draft.column_name) for draft in drafts if draft.source_key and draft.column_name]

        advanced_enabled = bool(
            job.ai_config.enable_advanced_options
            or job.ai_config.enable_generation_controls
            or job.ai_config.enable_output_schema
        )
        if not advanced_enabled:
            return []

        raw_schema = (job.ai_config.output_schema_text or "").strip()
        if raw_schema:
            schema = load_output_schema(raw_schema)
            properties = schema.get("properties")
            if isinstance(properties, dict):
                return [(str(key), str(key)) for key in properties.keys() if str(key).strip()]

        return []

    def _expected_parsed_keys(self, job: MonitorJob) -> set[str]:
        draft_keys = {draft.source_key for draft in self._collect_schema_drafts() if draft.include}
        if draft_keys:
            return draft_keys

        advanced_enabled = bool(
            job.ai_config.enable_advanced_options
            or job.ai_config.enable_generation_controls
            or job.ai_config.enable_output_schema
        )
        if not advanced_enabled:
            return set()

        raw_schema = (job.ai_config.output_schema_text or "").strip()
        if not raw_schema:
            return set()
        schema = load_output_schema(raw_schema)
        properties = schema.get("properties")
        if isinstance(properties, dict):
            return {str(key) for key in properties.keys()}
        return set()

    def _build_pipeline(self, job: MonitorJob, db_url: str) -> MonitorPipeline:
        capture_service = WindowCaptureService(self.window_service)
        extractor = build_extractor_for_job(job)
        repository = SqlAlchemyMappedRepository(
            db_url=db_url,
            table_name=job.table_name,
            mappings=job.mappings,
        )
        return MonitorPipeline(
            window_gateway=self.window_service,
            capture_service=capture_service,
            extractor=extractor,
            snapshot_repository=repository,
        )

    def _capture_once(self, job: MonitorJob, suffix: str = "manual", apply_adjustments: bool = True):
        window = self.window_service.get_window(job.window_hwnd)
        if window is None:
            raise ValueError("目标窗口不存在或已关闭，无法截图。")

        image = WindowCaptureService(self.window_service).capture(job.window_hwnd)
        if apply_adjustments:
            image = apply_job_capture_adjustments(image, job)
        captured_at = datetime.now()

        output_dir = Path(job.screenshot_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{captured_at:%Y%m%d_%H%M%S}_{job.job_id}_{job.window_hwnd}_{suffix}.png"
        screenshot_path = output_dir / filename
        image.save(screenshot_path)

        job.window_title = window.title
        return str(screenshot_path.resolve()), image, window.title

    def _show_capture_result(self, job: MonitorJob, image_path: str, raw_baseline: bool = False) -> None:
        pixmap = QPixmap(image_path)
        if pixmap.isNull():
            raise ValueError("截图文件已生成，但预览加载失败。")

        label = job.name.strip() or job.window_title.strip() or job.job_id
        self._preview_labels[job.job_id] = label
        self._last_image_paths[job.job_id] = image_path
        self._preview_raw_baseline[job.job_id] = bool(raw_baseline)
        self._last_pixmaps[job.job_id] = pixmap
        self._last_preview_job_id = job.job_id
        self._render_preview()

    def _show_validation_error(self, title: str, message: str) -> None:
        self._focus_tab_for_message(message)
        QMessageBox.warning(self, title, message)

    def _focus_tab_for_message(self, message: str) -> None:
        lowered = message.lower()
        target_index = 0

        if any(keyword in lowered for keyword in ["base url", "接口地址", "gateway", "网关", "model", "模型", "schema", "输出规范", "validation", "校验", "responses", "chat/completions"]):
            target_index = 1
        elif any(keyword in lowered for keyword in ["mapping", "映射", "database", "数据库", "table", "表", "column", "列", "schema draft", "字段草案", "建表"]):
            target_index = 3

        self.editor_tabs.setCurrentIndex(target_index)

        if target_index == 0:
            if "window" in lowered or "窗口" in message:
                self.window_combo.setFocus()
            else:
                self.table_name_edit.setFocus()
        elif target_index == 1:
            self.base_url_edit.setFocus()
        elif target_index == 2:
            self.ai_probe_test_btn.setFocus()
        elif target_index == 3:
            self.schema_table.setFocus()
        else:
            self.schema_table.setFocus()

    def _on_status_changed(self, job_id: str, status: str) -> None:
        self._append_log(f"状态变更：{self._job_tag(job_id)} -> {status}")
        self._update_status_badge()

    def _on_worker_log(self, job_id: str, message: str) -> None:
        self._append_log(f"[{self._job_tag(job_id)}] {message}")

    def _on_snapshot_ready(self, job_id: str, image_path: str) -> None:
        pixmap = QPixmap(image_path)
        if pixmap.isNull():
            return

        self._last_image_paths[job_id] = image_path
        self._preview_raw_baseline[job_id] = False
        self._last_pixmaps[job_id] = pixmap
        if job_id in self._jobs:
            self._preview_labels[job_id] = self._jobs[job_id].name.strip() or self._jobs[job_id].window_title or job_id
        self._last_preview_job_id = job_id
        self._render_preview()

    def edit_current_preview(self, suppress_non_raw_warning: bool = False) -> None:
        selected_job_id = self._selected_job_id()
        target_job_id = selected_job_id if selected_job_id in self._last_pixmaps else self._last_preview_job_id
        if not target_job_id or target_job_id not in self._last_pixmaps:
            QMessageBox.information(self, "提示", "当前没有可编辑的截图预览。")
            return

        source = self._last_pixmaps[target_job_id]
        if source.isNull():
            QMessageBox.warning(self, "提示", "当前预览图无效，无法编辑。")
            return

        if (not suppress_non_raw_warning) and (not self._preview_raw_baseline.get(target_job_id, False)):
            reply = QMessageBox.question(
                self,
                "提示",
                "当前预览图可能已应用裁剪或红框。\n"
                "建议使用“任务配置 -> 单次截图并编辑”基于原始截图调整，避免偏移累积。\n\n"
                "是否继续编辑当前预览图？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        active_job = self._jobs.get(target_job_id)
        current_crop = active_job.crop_rect if active_job is not None else self._editor_crop_rect
        current_marks = active_job.mark_rects if active_job is not None else self._editor_mark_rects
        source_is_raw = self._preview_raw_baseline.get(target_job_id, False)
        source_bounds = QRect(0, 0, source.width(), source.height())

        crop_origin = QPoint(0, 0)
        map_back_to_original = False
        initial_crop = self._tuple_to_qrect(current_crop)
        initial_marks: list[QRect] = []

        base_crop = self._tuple_to_qrect(current_crop)
        if (
            not source_is_raw
            and base_crop is not None
            and abs(base_crop.width() - source.width()) <= 2
            and abs(base_crop.height() - source.height()) <= 2
        ):
            crop_origin = base_crop.topLeft()
            map_back_to_original = True
            initial_crop = QRect(source_bounds)
            for rect in current_marks:
                qrect = self._tuple_to_qrect(rect)
                if qrect is None:
                    continue
                shifted = qrect.translated(-crop_origin.x(), -crop_origin.y()).intersected(source_bounds)
                if shifted.width() > 1 and shifted.height() > 1:
                    initial_marks.append(shifted)
        else:
            for rect in current_marks:
                qrect = self._tuple_to_qrect(rect)
                if qrect is not None:
                    initial_marks.append(qrect)

        dialog = PreviewEditorDialog(
            source,
            self,
            initial_crop_rect=initial_crop,
            initial_mark_rects=initial_marks,
        )
        if dialog.exec() != PreviewEditorDialog.DialogCode.Accepted:
            return

        result = dialog.result
        if result is None or result.pixmap.isNull():
            return

        result_crop = result.crop_rect
        result_marks = list(result.mark_rects)
        if map_back_to_original:
            if result_crop is not None:
                result_crop = result_crop.translated(crop_origin)
            result_marks = [rect.translated(crop_origin) for rect in result_marks]

        crop_rect = self._qrect_to_tuple(result_crop)
        mark_rects = [
            rect_tuple
            for rect_tuple in (self._qrect_to_tuple(rect) for rect in result_marks)
            if rect_tuple is not None
        ]

        self._editor_crop_rect = crop_rect
        self._editor_mark_rects = mark_rects
        self._update_capture_adjustment_summary()
        if active_job is not None:
            active_job.crop_rect = crop_rect
            active_job.mark_rects = [tuple(rect) for rect in mark_rects]

        self._last_pixmaps[target_job_id] = result.pixmap
        self._preview_raw_baseline[target_job_id] = False
        image_path = self._last_image_paths.get(target_job_id, "").strip()
        if image_path:
            ok = result.pixmap.save(image_path, "PNG")
            if not ok:
                self._append_log(f"预览编辑已应用，但写回文件失败：{image_path}")
            else:
                self._append_log(f"预览编辑已写回截图：{image_path}")
        else:
            self._append_log("预览编辑已应用（当前会话内有效）。")

        self._render_preview()

    def _on_parsed_ready(self, job_id: str, data: dict) -> None:
        payload = {
            "job_id": job_id,
            "job_name": self._jobs.get(job_id).name if job_id in self._jobs else "",
            "parse_mode": self._jobs.get(job_id).parse_mode if job_id in self._jobs else "",
            "data": data,
        }
        self.parsed_text.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))

    def _on_raw_text_ready(self, job_id: str, text: str) -> None:
        self.raw_text.setPlainText(f"[job={self._job_tag(job_id)}]\n{text}")


    def _on_error(self, job_id: str, message: str) -> None:
        self._append_log(f"[{self._job_tag(job_id)}] {message}")

    def _on_worker_finished(self, job_id: str) -> None:
        self._append_log(f"任务已结束：{self._job_tag(job_id)}")

    def _on_thread_finished(self, job_id: str) -> None:
        thread = self._threads.pop(job_id, None)
        if thread is not None:
            thread.deleteLater()

        self._workers.pop(job_id, None)

        if not self._threads:
            self._set_running(False)

        self._update_status_badge()

    def _set_running(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)

        self.save_job_btn.setEnabled(not running)
        self.start_selected_jobs_btn.setEnabled(not running)
        self.stop_selected_jobs_btn.setEnabled(running)
        self.delete_job_btn.setEnabled(not running)
        self.jobs_table.setEnabled(True)

        self.save_btn.setEnabled(not running)
        self.load_btn.setEnabled(not running)
        self.db_url_edit.setEnabled(not running)
        self.db_connect_test_btn.setEnabled(not running)
        self.editor_tabs.setEnabled(not running)

        self.refresh_btn.setEnabled(not running)
        self.capture_dir_btn.setEnabled(not running)
        self.capture_edit_init_btn.setEnabled(not running)
        self.capture_consistency_btn.setEnabled(not running)

        self.precheck_btn.setEnabled(not running)
        self.gateway_test_btn.setEnabled(not running)
        self.capture_test_btn.setEnabled(not running)
        self.parse_test_btn.setEnabled(not running)
        self.db_test_btn.setEnabled(not running)

        self._update_parse_mode_ui()

    def _update_status_badge(self) -> None:
        running_count = len(self._threads)
        if running_count > 0:
            self.status_badge.setText(f"运行中 {running_count}")
            self.status_badge.setStyleSheet(
                "border-radius:12px; padding:5px 10px; background:#dcfce7; color:#166534; font-weight:700;"
            )
        else:
            self.status_badge.setText("已停止")
            self.status_badge.setStyleSheet("")

    def _choose_capture_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择截图目录")
        if path:
            self.capture_dir_edit.setText(path)

    def _append_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{timestamp}] {message}")

    def _render_preview(self) -> None:
        selected_job_id = self._selected_job_id()
        target_job_id = selected_job_id if selected_job_id in self._last_pixmaps else self._last_preview_job_id

        if not target_job_id or target_job_id not in self._last_pixmaps:
            self.preview_label.setText("等待截图...")
            self.preview_label.setPixmap(QPixmap())
            self.preview_info_label.setText("当前预览任务: -")
            return

        pixmap = self._last_pixmaps[target_job_id]
        scaled = pixmap.scaled(
            self.preview_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview_label.setPixmap(scaled)
        self.preview_info_label.setText(f"当前预览任务: {self._job_tag(target_job_id)}")

    def _job_tag(self, job_id: str) -> str:
        job = self._jobs.get(job_id)
        if job is not None:
            name = job.name.strip() or "未命名"
            return f"{name}/{job_id}"

        label = self._preview_labels.get(job_id, "").strip()
        if label and label != job_id:
            return f"{label}/{job_id}"
        return job_id

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._render_preview()

    @staticmethod
    def _qrect_to_tuple(rect: QRect | None) -> tuple[int, int, int, int] | None:
        if rect is None:
            return None
        normalized = rect.normalized()
        left = int(normalized.x())
        top = int(normalized.y())
        right = left + int(normalized.width())
        bottom = top + int(normalized.height())
        if right <= left or bottom <= top:
            return None
        return (left, top, right, bottom)

    @staticmethod
    def _tuple_to_qrect(rect: tuple[int, int, int, int] | None) -> QRect | None:
        if rect is None:
            return None
        left, top, right, bottom = rect
        if right <= left or bottom <= top:
            return None
        return QRect(left, top, right - left, bottom - top)

    def _update_capture_adjustment_summary(self) -> None:
        if not hasattr(self, "capture_adjustment_summary_label"):
            return

        crop_desc = "未配置"
        if self._editor_crop_rect is not None:
            left, top, right, bottom = self._editor_crop_rect
            crop_desc = f"x={left}, y={top}, w={right - left}, h={bottom - top}"

        mark_count = len(self._editor_mark_rects)
        self.capture_adjustment_summary_label.setText(f"裁剪区: {crop_desc} | 红框数量: {mark_count}")


if __name__ == "__main__":
    from desktop_monitor.main import main as app_main

    raise SystemExit(app_main())
