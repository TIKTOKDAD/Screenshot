APP_STYLESHEET = """
QMainWindow {
    background: qlineargradient(x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 #eef6ff,
        stop: 0.55 #f8fbff,
        stop: 1 #f7f3ea);
}
QWidget {
    color: #1f2937;
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "SimHei";
    font-size: 13px;
}
QGroupBox {
    border: 1px solid #cbd5e1;
    border-radius: 12px;
    margin-top: 12px;
    background: rgba(255, 255, 255, 0.9);
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: #0f172a;
}
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QTableWidget {
    border: 1px solid #cbd5e1;
    border-radius: 8px;
    padding: 6px;
    background: #ffffff;
}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus, QTableWidget:focus {
    border: 1px solid #60a5fa;
}
QTableWidget {
    gridline-color: #e2e8f0;
}
QHeaderView::section {
    background: #e2ecff;
    color: #1e293b;
    padding: 8px;
    border: none;
    font-weight: 600;
}
QTabWidget::pane {
    border: 1px solid #cbd5e1;
    border-radius: 12px;
    background: rgba(255, 255, 255, 0.9);
    top: -1px;
}
QTabBar::tab {
    background: rgba(226, 236, 255, 0.7);
    color: #334155;
    border: 1px solid #cbd5e1;
    padding: 8px 14px;
    margin-right: 6px;
    border-top-left-radius: 10px;
    border-top-right-radius: 10px;
    font-weight: 600;
}
QTabBar::tab:selected {
    background: #ffffff;
    color: #0f172a;
    border-bottom-color: #ffffff;
}
QPushButton {
    border: none;
    border-radius: 9px;
    padding: 8px 14px;
    background: #2563eb;
    color: white;
    font-weight: 600;
}
QPushButton:hover {
    background: #1d4ed8;
}
QPushButton[variant="secondary"] {
    background: #e2e8f0;
    color: #1e293b;
}
QPushButton[variant="secondary"]:hover {
    background: #cbd5e1;
}
QPushButton[variant="success"] {
    background: #0f766e;
}
QPushButton[variant="success"]:hover {
    background: #115e59;
}
QPushButton[variant="danger"] {
    background: #b91c1c;
}
QPushButton[variant="danger"]:hover {
    background: #991b1b;
}
QPushButton:disabled {
    background: #94a3b8;
    color: #f8fafc;
}
QLabel#statusBadge, QLabel#infoPill {
    border-radius: 12px;
    padding: 5px 10px;
    background: #e2e8f0;
    color: #1e293b;
    font-weight: 700;
}
QLabel#previewLabel {
    border: 1px dashed #94a3b8;
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.65);
}
QLabel#sectionHint, QLabel#headerSubtitle {
    color: #475569;
}
"""
