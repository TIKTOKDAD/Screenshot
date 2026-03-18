from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


def _bootstrap_package_path() -> None:
    # Allow `python src/desktop_monitor/main.py` to resolve the package imports.
    if __package__:
        return
    src_root = Path(__file__).resolve().parents[1]
    src_root_str = str(src_root)
    if src_root_str not in sys.path:
        sys.path.insert(0, src_root_str)


def _add_dll_dir(path: Path) -> None:
    if not path.exists():
        return
    try:
        os.add_dll_directory(str(path))
    except (AttributeError, FileNotFoundError, OSError):
        return


def _bootstrap_qt_runtime() -> None:
    root = Path(sys.executable).resolve().parent
    site_packages = root / "Lib" / "site-packages"
    pyside_dir = site_packages / "PySide6"
    shiboken_dir = site_packages / "shiboken6"

    _add_dll_dir(root)
    _add_dll_dir(pyside_dir)
    _add_dll_dir(shiboken_dir)

    # Anaconda ships an ICU build that is incompatible with PySide6 wheels.
    # Preload Windows ICU so Qt resolves against the system copy first.
    for dll_name in ("icuuc.dll", "icuin.dll"):
        dll_path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / dll_name
        if dll_path.exists():
            ctypes.WinDLL(str(dll_path))


def main() -> int:
    _bootstrap_package_path()
    _bootstrap_qt_runtime()

    from PySide6.QtWidgets import QApplication

    from desktop_monitor.ui.main_window import MainWindow
    from desktop_monitor.ui.style import APP_STYLESHEET

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
