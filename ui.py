"""BlueStacks-inspired PySide6 graphical user interface for SimpleDroid."""

from __future__ import annotations

from datetime import datetime
import hashlib
import os
from pathlib import Path
import threading
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QDragEnterEvent, QDropEvent, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

import adb_tools
from config import (
    ANDROID_VERSIONS,
    LOGS_DIR,
    RAM_OPTIONS_GB,
    RESOLUTION_PRESETS,
    SCREENSHOTS_DIR,
    AppConfig,
    get_asset_path,
    get_host_ram_gb,
    load_config,
    save_config,
)
from emulator import EmulatorController, read_log_tail
from sdk_manager import (
    SetupError,
    apply_avd_settings,
    check_hypervisor_support,
    get_adb_path,
    get_avd_android_version,
    get_emulator_path,
    is_avd_created,
    is_system_image_installed,
    open_windows_features_dialog,
    resolve_sdk_dir,
    run_first_time_setup,
)

APP_STYLESHEET = """
QMainWindow, QWidget#RootWidget {
    background-color: #0b1120;
    color: #e2e8f0;
    font-family: "Segoe UI", sans-serif;
}
QMenuBar {
    background-color: #0f172a;
    color: #cbd5e1;
    border-bottom: 1px solid #1e293b;
}
QMenuBar::item:selected {
    background-color: #1e293b;
}
QMenu {
    background-color: #0f172a;
    color: #e2e8f0;
    border: 1px solid #334155;
}
QMenu::item:selected {
    background-color: #2563eb;
}
QFrame#TopBar {
    background-color: #0f172a;
    border-bottom: 1px solid #1e293b;
}
QFrame#SideNav {
    background-color: #0f172a;
    border-right: 1px solid #1e293b;
}
QFrame#RightTools {
    background-color: #0f172a;
    border-left: 1px solid #1e293b;
}
QPushButton#NavBtn {
    text-align: left;
    padding: 10px 14px;
    border-radius: 8px;
    border: none;
    background-color: transparent;
    color: #94a3b8;
    font-size: 13px;
    font-weight: 600;
}
QPushButton#NavBtn:hover {
    background-color: #1e293b;
    color: #f8fafc;
}
QPushButton#NavBtn[active="true"] {
    background-color: #1d4ed8;
    color: #ffffff;
}
QPushButton#ToolBtn {
    background-color: #1e293b;
    color: #e2e8f0;
    border: 1px solid #334155;
    border-radius: 8px;
    font-size: 12px;
    font-weight: 600;
    padding: 6px;
}
QPushButton#ToolBtn:hover:enabled {
    background-color: #334155;
    border-color: #3DDC84;
}
QPushButton#ToolBtn:disabled {
    color: #475569;
    background-color: #0f172a;
    border-color: #1e293b;
}
QFrame#Card, QFrame#AppCard {
    background-color: #111c33;
    border: 1px solid #1e293b;
    border-radius: 12px;
}
QFrame#AppCard:hover {
    border: 1px solid #38bdf8;
    background-color: #162340;
}
QFrame#AddCard {
    background-color: #0f1b2e;
    border: 2px dashed #334155;
    border-radius: 12px;
}
QFrame#AddCard:hover {
    border: 2px dashed #3DDC84;
    background-color: #13253b;
}
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QListWidget {
    background-color: #0f172a;
    color: #f1f5f9;
    border: 1px solid #334155;
    border-radius: 8px;
    padding: 6px 10px;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
    border-color: #3DDC84;
}
QComboBox QAbstractItemView {
    background-color: #0f172a;
    color: #f1f5f9;
    selection-background-color: #2563eb;
}
QPushButton#PrimaryBtn {
    background-color: #16a34a;
    color: #ffffff;
    border: none;
    border-radius: 8px;
    padding: 7px 14px;
    font-weight: 600;
}
QPushButton#PrimaryBtn:hover:enabled {
    background-color: #15803d;
}
QPushButton#PrimaryBtn:disabled {
    background-color: #334155;
    color: #64748b;
}
QPushButton#SecondaryBtn {
    background-color: #1e293b;
    color: #e2e8f0;
    border: 1px solid #334155;
    border-radius: 8px;
    padding: 7px 14px;
    font-weight: 600;
}
QPushButton#SecondaryBtn:hover:enabled {
    background-color: #334155;
}
QPushButton#SecondaryBtn:disabled {
    color: #475569;
}
QPushButton#DangerBtn {
    background-color: #991b1b;
    color: #ffffff;
    border: none;
    border-radius: 8px;
    padding: 7px 14px;
    font-weight: 600;
}
QPushButton#DangerBtn:hover:enabled {
    background-color: #b91c1c;
}
QProgressBar {
    background-color: #0f172a;
    border: 1px solid #334155;
    border-radius: 8px;
    text-align: center;
    color: #f8fafc;
    height: 22px;
}
QProgressBar::chunk {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3DDC84, stop:1 #2563eb);
    border-radius: 7px;
}
QScrollArea {
    border: none;
    background: transparent;
}
"""


class _TaskBridge(QObject):
    """GUI-thread signal bridge for running blocking work on a daemon Python thread."""

    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(int, str)


def load_app_icon() -> QIcon:
    """Load SimpleDroid's app icon from assets/icon.ico (or assets/icon.png)."""
    for rel in ("assets/icon.ico", "assets/icon.png"):
        path = get_asset_path(rel)
        if path.is_file():
            return QIcon(str(path))
    return QIcon()


def _friendly_app_name(package_name: str) -> str:
    """Format a readable display title from an Android package ID."""
    parts = [p for p in package_name.split(".") if p not in ("com", "org", "net", "io", "app", "android")]
    raw = parts[-1] if parts else package_name
    return raw.replace("_", " ").replace("-", " ").title()


def _badge_color(package_name: str) -> str:
    palette = ("#2563eb", "#0891b2", "#059669", "#7c3aed", "#db2777", "#d97706", "#0d9488")
    digest = hashlib.md5(package_name.encode("utf-8")).digest()
    return palette[digest[0] % len(palette)]


def _format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


class DropZone(QFrame):
    """BlueStacks-style drag-and-drop banner for APKs and phone files."""

    files_dropped = Signal(list)
    install_apk_clicked = Signal()
    add_files_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setObjectName("DropZone")
        self._set_highlight(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(10)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        title = QLabel("Drag & Drop APK or Any Files here")
        title.setStyleSheet("font-size: 14px; font-weight: 700; color: #f8fafc; border: none;")
        subtitle = QLabel(
            "Drop .apk to install apps, or drop photos/videos/docs to send directly to phone"
        )
        subtitle.setStyleSheet("font-size: 12px; color: #94a3b8; border: none;")
        text_col.addWidget(title)
        text_col.addWidget(subtitle)

        self.apk_btn = QPushButton("+ Install APK")
        self.apk_btn.setObjectName("PrimaryBtn")
        self.apk_btn.setFixedHeight(36)
        self.apk_btn.clicked.connect(self.install_apk_clicked.emit)

        self.files_btn = QPushButton("+ Add Files")
        self.files_btn.setObjectName("SecondaryBtn")
        self.files_btn.setFixedHeight(36)
        self.files_btn.clicked.connect(self.add_files_clicked.emit)

        layout.addLayout(text_col, stretch=1)
        layout.addWidget(self.files_btn)
        layout.addWidget(self.apk_btn)

    def _set_highlight(self, active: bool) -> None:
        border = "#3DDC84" if active else "#334155"
        bg = "#132a3e" if active else "#111c33"
        self.setStyleSheet(
            f"#DropZone {{ border: 2px dashed {border}; border-radius: 12px; background: {bg}; }}"
        )

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
            if any(Path(p).is_file() for p in paths):
                self._set_highlight(True)
                event.acceptProposedAction()
                return
        event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._set_highlight(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self._set_highlight(False)
        paths = [
            u.toLocalFile()
            for u in event.mimeData().urls()
            if u.isLocalFile() and Path(u.toLocalFile()).is_file()
        ]
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
            return
        event.ignore()


class AddTileCard(QFrame):
    """Home-screen '+' card for quickly adding APKs or files directly to the phone."""

    add_apk_requested = Signal()
    add_files_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AddCard")
        self.setFixedHeight(148)
        self.setMinimumWidth(185)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        plus_lbl = QLabel("+")
        plus_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        plus_lbl.setStyleSheet(
            "font-size: 30px; font-weight: 700; color: #3DDC84; border: none; line-height: 28px;"
        )

        title_lbl = QLabel("Add to Phone")
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setStyleSheet("font-size: 13px; font-weight: 700; color: #f8fafc; border: none;")

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        apk_btn = QPushButton("APK")
        apk_btn.setObjectName("PrimaryBtn")
        apk_btn.setFixedHeight(30)
        apk_btn.setToolTip("Install an .apk app")
        apk_btn.clicked.connect(self.add_apk_requested.emit)

        files_btn = QPushButton("Files")
        files_btn.setObjectName("SecondaryBtn")
        files_btn.setFixedHeight(30)
        files_btn.setToolTip("Send photos, videos, or documents into phone storage")
        files_btn.clicked.connect(self.add_files_requested.emit)

        btn_row.addWidget(apk_btn, stretch=1)
        btn_row.addWidget(files_btn, stretch=1)

        layout.addStretch()
        layout.addWidget(plus_lbl)
        layout.addWidget(title_lbl)
        layout.addStretch()
        layout.addLayout(btn_row)


class AppTileCard(QFrame):
    """BlueStacks-style app launcher card for an installed third-party Android app."""

    launch_requested = Signal(str)
    uninstall_requested = Signal(str)

    def __init__(self, package_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.package_name = package_name
        self.setObjectName("AppCard")
        self.setFixedHeight(148)
        self.setMinimumWidth(185)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(10)

        initials = _friendly_app_name(package_name)[:2].upper() or "AP"
        avatar = QLabel(initials, self)
        avatar.setFixedSize(42, 42)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setStyleSheet(
            f"background-color: {_badge_color(package_name)}; color: white; "
            "font-weight: 700; font-size: 15px; border-radius: 10px; border: none;"
        )

        title_col = QVBoxLayout()
        title_col.setSpacing(1)
        name_lbl = QLabel(_friendly_app_name(package_name), self)
        name_lbl.setStyleSheet("font-size: 13px; font-weight: 700; color: #f8fafc; border: none;")
        pkg_lbl = QLabel(package_name, self)
        pkg_lbl.setStyleSheet("font-size: 11px; color: #64748b; border: none;")
        pkg_lbl.setToolTip(package_name)
        title_col.addWidget(name_lbl)
        title_col.addWidget(pkg_lbl)

        header.addWidget(avatar)
        header.addLayout(title_col, stretch=1)
        layout.addLayout(header)
        layout.addStretch()

        actions = QHBoxLayout()
        actions.setSpacing(8)
        launch_btn = QPushButton("Launch", self)
        launch_btn.setObjectName("PrimaryBtn")
        launch_btn.setFixedHeight(30)
        launch_btn.clicked.connect(lambda: self.launch_requested.emit(self.package_name))

        uninstall_btn = QPushButton("Uninstall", self)
        uninstall_btn.setObjectName("SecondaryBtn")
        uninstall_btn.setFixedHeight(30)
        uninstall_btn.clicked.connect(lambda: self.uninstall_requested.emit(self.package_name))

        actions.addWidget(launch_btn, stretch=1)
        actions.addWidget(uninstall_btn)
        layout.addLayout(actions)


class PhoneFileRow(QWidget):
    """Row representing a file stored in the Android phone's Download directory."""

    save_requested = Signal(str)
    delete_requested = Signal(str)

    def __init__(self, filename: str, size_bytes: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.filename = filename

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(10)

        name_lbl = QLabel(filename, self)
        name_lbl.setStyleSheet("font-size: 13px; font-weight: 600; color: #f1f5f9;")

        size_lbl = QLabel(_format_size(size_bytes), self)
        size_lbl.setStyleSheet("font-size: 12px; color: #94a3b8;")

        save_btn = QPushButton("Save to PC", self)
        save_btn.setObjectName("SecondaryBtn")
        save_btn.setFixedHeight(28)
        save_btn.clicked.connect(lambda: self.save_requested.emit(self.filename))

        del_btn = QPushButton("Delete", self)
        del_btn.setObjectName("DangerBtn")
        del_btn.setFixedHeight(28)
        del_btn.clicked.connect(lambda: self.delete_requested.emit(self.filename))

        layout.addWidget(name_lbl, stretch=1)
        layout.addWidget(size_lbl)
        layout.addWidget(save_btn)
        layout.addWidget(del_btn)


class MainWindow(QMainWindow):
    """Main BlueStacks-style SimpleDroid desktop window."""

    external_apk_signal = Signal(str)
    bring_to_front_signal = Signal()

    def __init__(
        self,
        associate_apk_cb: Callable[[], None] | None = None,
        initial_apk: str | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("SimpleDroid")
        self.setWindowIcon(load_app_icon())
        self.resize(960, 650)
        self.setMinimumSize(800, 560)
        self.setStyleSheet(APP_STYLESHEET)

        self._cfg = load_config()
        self._emulator = EmulatorController()
        self._associate_apk_cb = associate_apk_cb
        self._pending_tasks: list[tuple[str, str]] = []
        self._state = "stopped"  # stopped | booting | ready | stopping
        self._serial: str | None = None
        self._installed_packages: list[str] = []
        self._active_bridges: list[_TaskBridge] = []
        self._closing_after_stop = False
        self._cancel_boot = False

        self.external_apk_signal.connect(self.handle_external_apk, Qt.ConnectionType.QueuedConnection)
        self.bring_to_front_signal.connect(self._bring_to_front, Qt.ConnectionType.QueuedConnection)

        self._build_menu()
        self._build_ui()

        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(3000)
        self._watch_timer.timeout.connect(self._check_emulator_alive)

        if self._needs_setup():
            if initial_apk:
                self._pending_tasks.append(("apk", initial_apk))
            self._root_stack.setCurrentIndex(0)
            self._start_setup()
        else:
            self._root_stack.setCurrentIndex(1)
            self._set_state("stopped")
            self._sync_settings_form()
            if initial_apk:
                QTimer.singleShot(150, lambda p=initial_apk: self.handle_external_apk(p))

    def _needs_setup(self) -> bool:
        if not self._cfg.setup_complete:
            return True
        sdk_dir = resolve_sdk_dir(self._cfg)
        avd_ver = get_avd_android_version()
        return (
            get_adb_path(self._cfg) is None
            or get_emulator_path(self._cfg) is None
            or not is_system_image_installed(sdk_dir, self._cfg.android_version)
            or not is_avd_created()
            or (avd_ver is not None and avd_ver != self._cfg.android_version)
        )

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("File")
        install_act = QAction("Install APK...", self)
        install_act.triggered.connect(self._browse_and_install_apk)
        file_menu.addAction(install_act)

        add_files_act = QAction("Add Files to Phone...", self)
        add_files_act.triggered.connect(self._browse_and_add_files)
        file_menu.addAction(add_files_act)

        if self._associate_apk_cb is not None:
            assoc_act = QAction("Associate .apk Files with SimpleDroid", self)
            assoc_act.triggered.connect(self._on_associate_apk)
            file_menu.addAction(assoc_act)

        file_menu.addSeparator()
        exit_act = QAction("Exit", self)
        exit_act.triggered.connect(self.close)
        file_menu.addAction(exit_act)

        android_menu = menu_bar.addMenu("Android")
        files_act = QAction("Phone Files", self)
        files_act.triggered.connect(lambda: self._switch_page(1))
        android_menu.addAction(files_act)

        settings_act = QAction("Settings", self)
        settings_act.triggered.connect(lambda: self._switch_page(2))
        android_menu.addAction(settings_act)

        logs_act = QAction("View Logs", self)
        logs_act.triggered.connect(lambda: self._switch_page(3))
        android_menu.addAction(logs_act)

    def _build_ui(self) -> None:
        self._root_stack = QStackedWidget()
        self._root_stack.setObjectName("RootWidget")
        self.setCentralWidget(self._root_stack)

        self._root_stack.addWidget(self._build_setup_page())
        self._root_stack.addWidget(self._build_shell_page())

    def _build_setup_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("RootWidget")
        outer = QVBoxLayout(page)
        outer.setAlignment(Qt.AlignmentFlag.AlignCenter)

        card = QFrame(page)
        card.setObjectName("Card")
        card.setFixedWidth(500)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(14)

        logo_lbl = QLabel(card)
        logo_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        png_path = get_asset_path("assets/icon.png")
        if png_path.is_file():
            pix = QPixmap(str(png_path)).scaled(
                72, 72, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            logo_lbl.setPixmap(pix)

        title = QLabel("Setting up SimpleDroid", card)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 20px; font-weight: 700; color: #f8fafc; border: none;")

        self.setup_status_label = QLabel("Checking Windows virtualization and existing Android tools...", card)
        self.setup_status_label.setWordWrap(True)
        self.setup_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setup_status_label.setStyleSheet("color: #94a3b8; font-size: 13px; border: none;")

        self.setup_progress = QProgressBar(card)
        self.setup_progress.setRange(0, 100)

        self.setup_error_label = QLabel("", card)
        self.setup_error_label.setWordWrap(True)
        self.setup_error_label.setStyleSheet("color: #fca5a5; font-size: 12px; border: none;")
        self.setup_error_label.hide()

        btn_row = QHBoxLayout()
        btn_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.win_features_btn = QPushButton("Open Windows Features", card)
        self.win_features_btn.setObjectName("SecondaryBtn")
        self.win_features_btn.clicked.connect(self._open_windows_features)
        self.win_features_btn.hide()

        self.retry_setup_btn = QPushButton("Retry Setup", card)
        self.retry_setup_btn.setObjectName("PrimaryBtn")
        self.retry_setup_btn.clicked.connect(self._start_setup)
        self.retry_setup_btn.hide()

        btn_row.addWidget(self.win_features_btn)
        btn_row.addWidget(self.retry_setup_btn)

        layout.addWidget(logo_lbl)
        layout.addWidget(title)
        layout.addWidget(self.setup_status_label)
        layout.addWidget(self.setup_progress)
        layout.addWidget(self.setup_error_label)
        layout.addLayout(btn_row)
        outer.addWidget(card)
        return page

    def _build_shell_page(self) -> QWidget:
        shell = QWidget()
        shell.setObjectName("RootWidget")
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        top_bar = QFrame(shell)
        top_bar.setObjectName("TopBar")
        top_bar.setFixedHeight(60)
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(16, 8, 16, 8)
        top_layout.setSpacing(12)

        icon_lbl = QLabel(top_bar)
        png_path = get_asset_path("assets/icon.png")
        if png_path.is_file():
            pix = QPixmap(str(png_path)).scaled(
                32, 32, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            icon_lbl.setPixmap(pix)

        brand = QLabel("SimpleDroid", top_bar)
        brand.setStyleSheet("font-size: 16px; font-weight: 700; color: #f8fafc;")

        self.version_pill = QLabel(self._cfg.android_label(), top_bar)
        self.version_pill.setStyleSheet(
            "background-color: #1e293b; color: #3DDC84; border: 1px solid #334155; "
            "border-radius: 10px; padding: 3px 10px; font-size: 11px; font-weight: 600;"
        )

        self.status_badge = QLabel("● Stopped", top_bar)
        self.status_badge.setStyleSheet("font-size: 13px; font-weight: 600; color: #94a3b8;")

        self.power_btn = QPushButton("Start Android", top_bar)
        self.power_btn.setFixedSize(142, 38)
        self.power_btn.clicked.connect(self._on_power_clicked)

        top_layout.addWidget(icon_lbl)
        top_layout.addWidget(brand)
        top_layout.addWidget(self.version_pill)
        top_layout.addStretch()
        top_layout.addWidget(self.status_badge)
        top_layout.addWidget(self.power_btn)
        shell_layout.addWidget(top_bar)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        side_nav = QFrame(shell)
        side_nav.setObjectName("SideNav")
        side_nav.setFixedWidth(188)
        nav_layout = QVBoxLayout(side_nav)
        nav_layout.setContentsMargins(12, 16, 12, 16)
        nav_layout.setSpacing(6)

        self._nav_buttons: list[QPushButton] = []
        for idx, label in enumerate(("App Player", "Phone Files", "Settings", "Logs")):
            btn = QPushButton(label, side_nav)
            btn.setObjectName("NavBtn")
            btn.clicked.connect(lambda _=False, i=idx: self._switch_page(i))
            nav_layout.addWidget(btn)
            self._nav_buttons.append(btn)

        nav_layout.addStretch()

        self.hw_summary_lbl = QLabel("", side_nav)
        self.hw_summary_lbl.setWordWrap(True)
        self.hw_summary_lbl.setStyleSheet(
            "background-color: #111c33; border: 1px solid #1e293b; border-radius: 8px; "
            "padding: 8px; color: #94a3b8; font-size: 11px;"
        )
        nav_layout.addWidget(self.hw_summary_lbl)

        if self._associate_apk_cb is not None:
            assoc_btn = QPushButton("Associate .apk", side_nav)
            assoc_btn.setObjectName("SecondaryBtn")
            assoc_btn.clicked.connect(self._on_associate_apk)
            nav_layout.addWidget(assoc_btn)

        body.addWidget(side_nav)

        center_col = QVBoxLayout()
        center_col.setContentsMargins(0, 0, 0, 0)
        center_col.setSpacing(0)

        self.banner_frame = QFrame(shell)
        self.banner_frame.hide()
        banner_layout = QHBoxLayout(self.banner_frame)
        banner_layout.setContentsMargins(16, 8, 12, 8)
        banner_layout.setSpacing(10)
        self.banner_label = QLabel("", self.banner_frame)
        self.banner_label.setWordWrap(True)
        banner_close = QPushButton("Dismiss", self.banner_frame)
        banner_close.setObjectName("SecondaryBtn")
        banner_close.setFixedHeight(26)
        banner_close.clicked.connect(self.banner_frame.hide)
        banner_layout.addWidget(self.banner_label, stretch=1)
        banner_layout.addWidget(banner_close)
        center_col.addWidget(self.banner_frame)

        self._page_stack = QStackedWidget(shell)
        self._page_stack.addWidget(self._build_apps_page())
        self._page_stack.addWidget(self._build_files_page())
        self._page_stack.addWidget(self._build_settings_page())
        self._page_stack.addWidget(self._build_logs_page())
        center_col.addWidget(self._page_stack, stretch=1)
        body.addLayout(center_col, stretch=1)

        right_tools = QFrame(shell)
        right_tools.setObjectName("RightTools")
        right_tools.setFixedWidth(68)
        rt_layout = QVBoxLayout(right_tools)
        rt_layout.setContentsMargins(8, 14, 8, 14)
        rt_layout.setSpacing(8)

        tools_title = QLabel("Controls", right_tools)
        tools_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tools_title.setStyleSheet("font-size: 10px; color: #64748b; font-weight: 700;")
        rt_layout.addWidget(tools_title)

        self._device_tool_btns: list[QPushButton] = []
        controls = (
            ("Home", "Send Android Home", lambda: self._send_key(3, "Home")),
            ("Back", "Send Android Back", lambda: self._send_key(4, "Back")),
            ("Recent", "Recent Apps", lambda: self._send_key(187, "Recent Apps")),
            ("Vol +", "Volume Up", lambda: self._send_key(24, "Volume Up")),
            ("Vol -", "Volume Down", lambda: self._send_key(25, "Volume Down")),
            ("Rotate", "Toggle Portrait/Landscape", self._rotate_screen),
            ("Snap", "Take Screenshot", self._take_screenshot),
        )
        for label, tip, handler in controls:
            tbtn = QPushButton(label, right_tools)
            tbtn.setObjectName("ToolBtn")
            tbtn.setToolTip(tip)
            tbtn.setFixedHeight(38)
            tbtn.clicked.connect(handler)
            rt_layout.addWidget(tbtn)
            self._device_tool_btns.append(tbtn)

        rt_layout.addStretch()
        body.addWidget(right_tools)
        shell_layout.addLayout(body, stretch=1)

        footer = QFrame(shell)
        footer.setStyleSheet("background-color: #0f172a; border-top: 1px solid #1e293b;")
        footer.setFixedHeight(30)
        f_layout = QHBoxLayout(footer)
        f_layout.setContentsMargins(14, 2, 14, 2)
        self.footer_label = QLabel("Ready to start Android.", footer)
        self.footer_label.setStyleSheet("color: #94a3b8; font-size: 12px;")
        f_layout.addWidget(self.footer_label, stretch=1)
        shell_layout.addWidget(footer)

        self._switch_page(0)
        self._update_hw_summary()
        return shell

    def _build_apps_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        self.drop_zone = DropZone(page)
        self.drop_zone.files_dropped.connect(self._handle_dropped_paths)
        self.drop_zone.install_apk_clicked.connect(self._browse_and_install_apk)
        self.drop_zone.add_files_clicked.connect(self._browse_and_add_files)
        layout.addWidget(self.drop_zone)

        toolbar = QHBoxLayout()
        apps_heading = QLabel("My Apps & Games", page)
        apps_heading.setStyleSheet("font-size: 15px; font-weight: 700; color: #f8fafc;")

        self.search_input = QLineEdit(page)
        self.search_input.setPlaceholderText("Search installed apps...")
        self.search_input.setFixedWidth(220)
        self.search_input.textChanged.connect(self._render_app_grid)

        self.refresh_apps_btn = QPushButton("Refresh", page)
        self.refresh_apps_btn.setObjectName("SecondaryBtn")
        self.refresh_apps_btn.clicked.connect(self._refresh_apps)

        toolbar.addWidget(apps_heading)
        toolbar.addStretch()
        toolbar.addWidget(self.search_input)
        toolbar.addWidget(self.refresh_apps_btn)
        layout.addLayout(toolbar)

        self.apps_scroll = QScrollArea(page)
        self.apps_scroll.setWidgetResizable(True)
        self.apps_container = QWidget(self.apps_scroll)
        self.apps_grid = QGridLayout(self.apps_container)
        self.apps_grid.setContentsMargins(0, 0, 4, 0)
        self.apps_grid.setSpacing(12)
        self.apps_scroll.setWidget(self.apps_container)
        layout.addWidget(self.apps_scroll, stretch=1)
        return page

    def _build_files_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("Phone Files (/sdcard/Download)", page)
        title.setStyleSheet("font-size: 16px; font-weight: 700; color: #f8fafc;")

        self.add_phone_files_btn = QPushButton("+ Add Files to Phone", page)
        self.add_phone_files_btn.setObjectName("PrimaryBtn")
        self.add_phone_files_btn.clicked.connect(self._browse_and_add_files)

        self.refresh_files_btn = QPushButton("Refresh", page)
        self.refresh_files_btn.setObjectName("SecondaryBtn")
        self.refresh_files_btn.clicked.connect(self._refresh_phone_files)

        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.add_phone_files_btn)
        header.addWidget(self.refresh_files_btn)
        layout.addLayout(header)

        hint = QLabel(
            "Files you add here appear immediately in Android's Files / Downloads / Gallery apps.",
            page,
        )
        hint.setStyleSheet("color: #94a3b8; font-size: 12px;")
        layout.addWidget(hint)

        self.phone_files_list = QListWidget(page)
        layout.addWidget(self.phone_files_list, stretch=1)
        return page

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        top_row = QHBoxLayout()
        heading = QLabel("Engine & Performance Settings", page)
        heading.setStyleSheet("font-size: 17px; font-weight: 700; color: #f8fafc;")

        low_end_preset_btn = QPushButton("Optimize for Low-End PC", page)
        low_end_preset_btn.setObjectName("SecondaryBtn")
        low_end_preset_btn.clicked.connect(self._apply_low_end_preset_to_form)

        top_row.addWidget(heading)
        top_row.addStretch()
        top_row.addWidget(low_end_preset_btn)
        layout.addLayout(top_row)

        host_info = QLabel(
            f"Detected Host PC: {get_host_ram_gb():.1f} GB RAM · {os.cpu_count() or 4} CPU threads",
            page,
        )
        host_info.setStyleSheet("color: #94a3b8; font-size: 12px;")
        layout.addWidget(host_info)

        card = QFrame(page)
        card.setObjectName("Card")
        form = QFormLayout(card)
        form.setContentsMargins(20, 20, 20, 20)
        form.setSpacing(14)

        self.android_ver_combo = QComboBox(card)
        for key, info in ANDROID_VERSIONS.items():
            self.android_ver_combo.addItem(info["label"], key)
        form.addRow("Android version:", self.android_ver_combo)

        self.ram_combo = QComboBox(card)
        for gb in RAM_OPTIONS_GB:
            self.ram_combo.addItem(f"{gb} GB", gb)
        form.addRow("RAM allocation:", self.ram_combo)

        self.cores_spin = QSpinBox(card)
        self.cores_spin.setRange(1, max(1, os.cpu_count() or 4))
        form.addRow("CPU cores:", self.cores_spin)

        self.res_combo = QComboBox(card)
        for key, info in RESOLUTION_PRESETS.items():
            self.res_combo.addItem(info["label"], key)
        form.addRow("Display preset:", self.res_combo)

        self.gpu_check = QCheckBox("Enable hardware GPU acceleration (-gpu auto)", card)
        form.addRow("Graphics:", self.gpu_check)

        self.low_end_check = QCheckBox(
            "Low-end hardware boost (-no-boot-anim, -no-metrics, faster UI)", card
        )
        form.addRow("Performance:", self.low_end_check)

        self.clean_boot_check = QCheckBox(
            "Clean boot (skip quick-boot snapshot with -no-snapshot-load)", card
        )
        form.addRow("Boot mode:", self.clean_boot_check)

        layout.addWidget(card)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("Save & Apply Settings", page)
        save_btn.setObjectName("PrimaryBtn")
        save_btn.clicked.connect(self._save_settings_clicked)

        reset_btn = QPushButton("Reset Android (Wipe Data)", page)
        reset_btn.setObjectName("DangerBtn")
        reset_btn.clicked.connect(self._confirm_reset_android)

        btn_row.addWidget(save_btn)
        btn_row.addStretch()
        btn_row.addWidget(reset_btn)
        layout.addLayout(btn_row)
        layout.addStretch()
        return page

    def _build_logs_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("Emulator Logs", page)
        title.setStyleSheet("font-size: 16px; font-weight: 700; color: #f8fafc;")
        refresh_btn = QPushButton("Refresh", page)
        refresh_btn.setObjectName("SecondaryBtn")
        refresh_btn.clicked.connect(self._refresh_logs)
        open_dir_btn = QPushButton("Open Logs Folder", page)
        open_dir_btn.setObjectName("SecondaryBtn")
        open_dir_btn.clicked.connect(lambda: os.startfile(str(LOGS_DIR)))  # type: ignore[attr-defined]
        header.addWidget(title)
        header.addStretch()
        header.addWidget(refresh_btn)
        header.addWidget(open_dir_btn)
        layout.addLayout(header)

        self.logs_text = QPlainTextEdit(page)
        self.logs_text.setReadOnly(True)
        self.logs_text.setStyleSheet("font-family: Consolas, monospace; font-size: 12px;")
        layout.addWidget(self.logs_text, stretch=1)
        return page

    def _show_banner(self, message: str, is_error: bool = False) -> None:
        """Display a non-blocking banner (never freezes worker callbacks with modal dialogs)."""
        bg = "#450a0a" if is_error else "#052e16"
        border = "#dc2626" if is_error else "#16a34a"
        fg = "#fecaca" if is_error else "#bbf7d0"
        self.banner_frame.setStyleSheet(
            f"background-color: {bg}; border-bottom: 1px solid {border};"
        )
        self.banner_label.setStyleSheet(f"color: {fg}; font-size: 12px; font-weight: 600;")
        self.banner_label.setText(message)
        self.banner_frame.show()

    def _switch_page(self, index: int) -> None:
        self._page_stack.setCurrentIndex(index)
        for i, btn in enumerate(self._nav_buttons):
            btn.setProperty("active", "true" if i == index else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        if index == 1:
            self._refresh_phone_files()
        elif index == 2:
            self._sync_settings_form()
        elif index == 3:
            self._refresh_logs()

    def _refresh_logs(self) -> None:
        self.logs_text.setPlainText(read_log_tail(500))
        bar = self.logs_text.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _sync_settings_form(self) -> None:
        ver_idx = self.android_ver_combo.findData(self._cfg.android_version)
        self.android_ver_combo.setCurrentIndex(max(0, ver_idx))
        ram_idx = self.ram_combo.findData(self._cfg.ram_gb)
        self.ram_combo.setCurrentIndex(max(0, ram_idx))
        self.cores_spin.setValue(self._cfg.cpu_cores)
        res_idx = self.res_combo.findData(self._cfg.resolution_preset)
        self.res_combo.setCurrentIndex(max(0, res_idx))
        self.gpu_check.setChecked(self._cfg.gpu_acceleration)
        self.low_end_check.setChecked(self._cfg.low_end_mode)
        self.clean_boot_check.setChecked(self._cfg.clean_boot)

    def _apply_low_end_preset_to_form(self) -> None:
        ram_idx = self.ram_combo.findData(2)
        self.ram_combo.setCurrentIndex(max(0, ram_idx))
        self.cores_spin.setValue(min(2, max(1, os.cpu_count() or 2)))
        res_idx = self.res_combo.findData("phone_720p")
        self.res_combo.setCurrentIndex(max(0, res_idx))
        self.low_end_check.setChecked(True)
        self._show_banner(
            "Low-End preset filled (2 GB RAM, 2 Cores, 720p, Low-End Boost). "
            "For very weak PCs, also select 'Android 11 (Ultra-Light)' and click Save & Apply."
        )

    def _update_hw_summary(self) -> None:
        gpu_txt = "GPU Auto" if self._cfg.gpu_acceleration else "SW GPU"
        self.version_pill.setText(self._cfg.android_label())
        self.hw_summary_lbl.setText(
            f"{self._cfg.ram_gb} GB RAM · {self._cfg.cpu_cores} Cores\n{gpu_txt}"
        )

    def _bring_to_front(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _run_in_thread(
        self,
        fn: Callable[..., object],
        on_done: Callable[[object], None],
        on_error: Callable[[str], None],
        on_progress: Callable[[int, str], None] | None = None,
    ) -> None:
        bridge = _TaskBridge(self)
        self._active_bridges.append(bridge)

        def _cleanup() -> None:
            if bridge in self._active_bridges:
                self._active_bridges.remove(bridge)
            bridge.deleteLater()

        def _handle_done(res: object) -> None:
            try:
                on_done(res)
            finally:
                _cleanup()

        def _handle_err(err: str) -> None:
            try:
                on_error(err)
            finally:
                _cleanup()

        if on_progress is not None:
            bridge.progress.connect(on_progress, Qt.ConnectionType.QueuedConnection)
        bridge.finished.connect(_handle_done, Qt.ConnectionType.QueuedConnection)
        bridge.failed.connect(_handle_err, Qt.ConnectionType.QueuedConnection)

        def _runner() -> None:
            try:
                if on_progress is not None:
                    result = fn(lambda pct, msg: bridge.progress.emit(pct, msg))
                else:
                    result = fn()
                bridge.finished.emit(result)
            except Exception as exc:
                bridge.failed.emit(str(exc) or exc.__class__.__name__)

        threading.Thread(target=_runner, daemon=True).start()

    def _start_setup(self) -> None:
        self.setup_error_label.hide()
        self.win_features_btn.hide()
        self.retry_setup_btn.hide()
        self.setup_progress.setValue(0)
        self.setup_status_label.setText("Checking Windows virtualization and existing tools...")

        def task(progress_cb):
            return run_first_time_setup(self._cfg, progress_cb=progress_cb)

        def done(new_cfg: object) -> None:
            if isinstance(new_cfg, AppConfig):
                self._cfg = new_cfg
            self.setup_progress.setValue(100)
            self._root_stack.setCurrentIndex(1)
            self._set_state("stopped")
            self._sync_settings_form()
            self._update_hw_summary()
            if self._pending_tasks:
                if self._state == "stopped":
                    self._start_android()
                else:
                    self._process_pending_tasks()

        def fail(err: str) -> None:
            self.setup_status_label.setText("Setup paused")
            self.setup_error_label.setText(err)
            self.setup_error_label.show()
            hv = check_hypervisor_support()
            if not hv.supported:
                self.win_features_btn.show()
            self.retry_setup_btn.show()

        self._run_in_thread(
            task,
            on_done=done,
            on_error=fail,
            on_progress=self._on_setup_progress,
        )

    def _on_setup_progress(self, pct: int, msg: str) -> None:
        self.setup_progress.setValue(max(0, min(100, pct)))
        self.setup_status_label.setText(msg)

    def _open_windows_features(self) -> None:
        try:
            open_windows_features_dialog()
        except SetupError as exc:
            self._show_banner(str(exc), is_error=True)

    def _set_state(self, state: str) -> None:
        self._state = state
        for tbtn in self._device_tool_btns:
            tbtn.setEnabled(state == "ready")

        if state == "stopped":
            self._serial = None
            self._watch_timer.stop()
            self._installed_packages = []
            self._render_app_grid()
            self.phone_files_list.clear()
            self.status_badge.setText("● Stopped")
            self.status_badge.setStyleSheet("font-size: 13px; font-weight: 600; color: #94a3b8;")
            self.power_btn.setText("Start Android")
            self.power_btn.setEnabled(True)
            self.power_btn.setStyleSheet(
                "QPushButton { font-size: 13px; font-weight: 700; border-radius: 8px; "
                "background: #16a34a; color: white; border: none; }"
                "QPushButton:hover { background: #15803d; }"
            )
            self.refresh_apps_btn.setEnabled(False)
            self.refresh_files_btn.setEnabled(False)
            self.footer_label.setText("Android is stopped.")
        elif state == "booting":
            self.status_badge.setText("● Booting...")
            self.status_badge.setStyleSheet("font-size: 13px; font-weight: 600; color: #fbbf24;")
            self.power_btn.setText("Booting...")
            self.power_btn.setEnabled(False)
            self.power_btn.setStyleSheet(
                "QPushButton { font-size: 13px; font-weight: 700; border-radius: 8px; "
                "background: #334155; color: #cbd5e1; border: none; }"
            )
            self.refresh_apps_btn.setEnabled(False)
            self.refresh_files_btn.setEnabled(False)
            self.footer_label.setText("Starting Android Emulator...")
        elif state == "ready":
            self._watch_timer.start()
            self.status_badge.setText("● Ready")
            self.status_badge.setStyleSheet("font-size: 13px; font-weight: 600; color: #3DDC84;")
            self.power_btn.setText("Stop Android")
            self.power_btn.setEnabled(True)
            self.power_btn.setStyleSheet(
                "QPushButton { font-size: 13px; font-weight: 700; border-radius: 8px; "
                "background: #dc2626; color: white; border: none; }"
                "QPushButton:hover { background: #b91c1c; }"
            )
            self.refresh_apps_btn.setEnabled(True)
            self.refresh_files_btn.setEnabled(True)
            self.footer_label.setText("Android is running.")
        elif state == "stopping":
            self._watch_timer.stop()
            self.status_badge.setText("● Stopping...")
            self.status_badge.setStyleSheet("font-size: 13px; font-weight: 600; color: #fbbf24;")
            self.power_btn.setText("Stopping...")
            self.power_btn.setEnabled(False)
            self.refresh_apps_btn.setEnabled(False)
            self.refresh_files_btn.setEnabled(False)
            self.footer_label.setText("Saving snapshot and stopping Android...")

    def _on_power_clicked(self) -> None:
        if self._state == "stopped":
            self._start_android()
        elif self._state == "ready":
            self._stop_android()

    def _start_android(self, wipe_data: bool = False) -> None:
        if self._state != "stopped":
            return
        self._cancel_boot = False
        self._set_state("booting")

        def task() -> str:
            self._emulator.start(self._cfg, wipe_data=wipe_data)
            return self._emulator.wait_until_ready(
                self._cfg,
                wipe_data=wipe_data,
                cancel_check=lambda: self._cancel_boot,
            )

        def done(serial: object) -> None:
            self._serial = str(serial)
            self._set_state("ready")
            self._refresh_apps()
            if self._page_stack.currentIndex() == 1:
                self._refresh_phone_files()
            if self._pending_tasks:
                self._process_pending_tasks()

        def fail(err: str) -> None:
            self._emulator.stop(self._cfg, wait_sec=3.0)
            self._set_state("stopped")
            self._pending_tasks.clear()
            if not self._closing_after_stop:
                self._show_banner(err, is_error=True)

        self._run_in_thread(task, on_done=done, on_error=fail)

    def _stop_android(self, on_stopped: Callable[[], None] | None = None) -> None:
        if self._state == "stopped":
            if on_stopped:
                on_stopped()
            return
        self._cancel_boot = True
        self._set_state("stopping")

        def task() -> None:
            self._emulator.stop(self._cfg)

        def done(_: object) -> None:
            self._set_state("stopped")
            if self._closing_after_stop:
                self.close()
            elif on_stopped:
                on_stopped()

        def fail(err: str) -> None:
            self._set_state("stopped")
            if self._closing_after_stop:
                self.close()
            else:
                self._show_banner(err, is_error=True)

        self._run_in_thread(task, on_done=done, on_error=fail)

    def _check_emulator_alive(self) -> None:
        if self._state != "ready":
            return
        if self._emulator.is_process_running():
            return
        code = self._emulator.last_exit_code()
        self._set_state("stopped")
        if code not in (None, 0):
            self._show_banner(
                f"Android Emulator exited unexpectedly (code {code}). Check the Logs tab for details.",
                is_error=True,
            )

    def handle_external_apk(self, apk_path: str) -> None:
        """Install an APK from file association, drag-and-drop, or file picker."""
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self._enqueue_task("apk", apk_path)

    def _handle_dropped_paths(self, paths: list[str]) -> None:
        for p in paths:
            kind = "apk" if Path(p).suffix.lower() == ".apk" else "file"
            self._enqueue_task(kind, p)

    def _enqueue_task(self, kind: str, path: str) -> None:
        if not Path(path).is_file():
            self._show_banner(f"File not found: {path}", is_error=True)
            return

        if self._needs_setup():
            self._pending_tasks.append((kind, path))
            return

        if self._state == "stopped":
            self._pending_tasks.append((kind, path))
            self.footer_label.setText(f"Starting Android for {Path(path).name}...")
            self._start_android()
            return

        if self._state == "booting":
            self._pending_tasks.append((kind, path))
            self.footer_label.setText(f"Waiting for boot to process {Path(path).name}...")
            return

        if self._state == "ready":
            self._pending_tasks.append((kind, path))
            if len(self._pending_tasks) == 1:
                self._process_pending_tasks()

    def _process_pending_tasks(self) -> None:
        if self._state != "ready" or not self._pending_tasks:
            return
        kind, path = self._pending_tasks[0]
        if kind == "apk":
            self._install_apk_now(path)
        else:
            self._push_file_now(path)

    def _finish_current_task(self) -> None:
        if self._pending_tasks:
            self._pending_tasks.pop(0)
        if self._pending_tasks and self._state == "ready":
            self._process_pending_tasks()

    def _browse_and_install_apk(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Android APK",
            str(Path.home()),
            "Android Packages (*.apk)",
        )
        if file_path:
            self.handle_external_apk(file_path)

    def _browse_and_add_files(self) -> None:
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Select Files to Add to Phone (/sdcard/Download)",
            str(Path.home()),
            "All Files (*.*)",
        )
        for file_path in file_paths:
            self._enqueue_task("file", file_path)

    def _install_apk_now(self, apk_path: str) -> None:
        name = Path(apk_path).name
        self.footer_label.setText(f"Installing {name}...")

        def task() -> str:
            return adb_tools.install_apk(apk_path, self._cfg, serial=self._serial)

        def done(msg: object) -> None:
            text = str(msg)
            self.footer_label.setText(text)
            self._show_banner(text, is_error=False)
            self._refresh_apps()
            self._finish_current_task()

        def fail(err: str) -> None:
            self.footer_label.setText(f"Install failed: {name}")
            self._show_banner(err, is_error=True)
            self._finish_current_task()

        self._run_in_thread(task, on_done=done, on_error=fail)

    def _push_file_now(self, local_path: str) -> None:
        name = Path(local_path).name
        self.footer_label.setText(f"Sending {name} to phone...")

        def task() -> str:
            return adb_tools.push_file_to_phone(local_path, self._cfg, serial=self._serial)

        def done(msg: object) -> None:
            text = str(msg)
            self.footer_label.setText(text)
            self._show_banner(text, is_error=False)
            if self._page_stack.currentIndex() == 1:
                self._refresh_phone_files()
            self._finish_current_task()

        def fail(err: str) -> None:
            self.footer_label.setText(f"Failed to send {name}")
            self._show_banner(err, is_error=True)
            self._finish_current_task()

        self._run_in_thread(task, on_done=done, on_error=fail)

    def _refresh_apps(self) -> None:
        if self._state != "ready":
            return
        self.refresh_apps_btn.setEnabled(False)

        def task() -> list[str]:
            return adb_tools.list_third_party_packages(self._cfg, serial=self._serial)

        def done(pkgs: object) -> None:
            self.refresh_apps_btn.setEnabled(self._state == "ready")
            self._installed_packages = pkgs if isinstance(pkgs, list) else []
            self._render_app_grid()

        def fail(_: str) -> None:
            self.refresh_apps_btn.setEnabled(self._state == "ready")
            self.footer_label.setText("Could not refresh app list.")

        self._run_in_thread(task, on_done=done, on_error=fail)

    def _render_app_grid(self) -> None:
        while self.apps_grid.count():
            item = self.apps_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()

        cols = 3
        query = self.search_input.text().strip().lower()
        cards: list[QWidget] = []

        if not query:
            add_card = AddTileCard(self.apps_container)
            add_card.add_apk_requested.connect(self._browse_and_install_apk)
            add_card.add_files_requested.connect(self._browse_and_add_files)
            cards.append(add_card)

        if self._state == "ready":
            filtered = [
                p
                for p in self._installed_packages
                if not query or query in p.lower() or query in _friendly_app_name(p).lower()
            ]
            for pkg in filtered:
                card = AppTileCard(pkg, self.apps_container)
                card.launch_requested.connect(self._launch_app)
                card.uninstall_requested.connect(self._uninstall_app)
                cards.append(card)

        for idx, widget in enumerate(cards):
            self.apps_grid.addWidget(widget, idx // cols, idx % cols)

        if self._state != "ready":
            hint = QLabel(
                "Start Android above, or click '+' to install an APK / add files (Android starts automatically).",
                self.apps_container,
            )
            hint.setWordWrap(True)
            hint.setStyleSheet("color: #64748b; font-size: 13px; padding: 12px;")
            self.apps_grid.addWidget(hint, 1, 0, 1, cols)
        elif not self._installed_packages and not query:
            hint = QLabel(
                "No third-party apps installed yet. Click '+ APK' or drop an .apk file above.",
                self.apps_container,
            )
            hint.setWordWrap(True)
            hint.setStyleSheet("color: #64748b; font-size: 13px; padding: 12px;")
            self.apps_grid.addWidget(hint, 1, 0, 1, cols)

        self.apps_grid.setRowStretch((len(cards) // cols) + 2, 1)

    def _refresh_phone_files(self) -> None:
        self.phone_files_list.clear()
        if self._state != "ready":
            item = QListWidgetItem("Start Android to browse and transfer files on the phone.")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.phone_files_list.addItem(item)
            return

        self.refresh_files_btn.setEnabled(False)

        def task() -> list[tuple[str, int]]:
            return adb_tools.list_phone_files(self._cfg, serial=self._serial)

        def done(files: object) -> None:
            self.refresh_files_btn.setEnabled(self._state == "ready")
            self.phone_files_list.clear()
            entries = files if isinstance(files, list) else []
            if not entries:
                item = QListWidgetItem("No files in /sdcard/Download yet. Click '+ Add Files to Phone' above.")
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                self.phone_files_list.addItem(item)
                return

            for fname, size_bytes in entries:
                item = QListWidgetItem(self.phone_files_list)
                row = PhoneFileRow(fname, size_bytes, self.phone_files_list)
                row.save_requested.connect(self._save_phone_file_to_pc)
                row.delete_requested.connect(self._delete_phone_file)
                item.setSizeHint(row.sizeHint())
                self.phone_files_list.addItem(item)
                self.phone_files_list.setItemWidget(item, row)

        def fail(err: str) -> None:
            self.refresh_files_btn.setEnabled(self._state == "ready")
            self._show_banner(err, is_error=True)

        self._run_in_thread(task, on_done=done, on_error=fail)

    def _save_phone_file_to_pc(self, filename: str) -> None:
        dest, _ = QFileDialog.getSaveFileName(
            self,
            "Save Phone File to PC",
            str(Path.home() / "Downloads" / filename),
        )
        if not dest:
            return

        def task() -> Path:
            return adb_tools.pull_file_from_phone(filename, dest, self._cfg, serial=self._serial)

        self._run_in_thread(
            task,
            on_done=lambda p: self._show_banner(f"Saved {filename} to {p}"),
            on_error=lambda err: self._show_banner(err, is_error=True),
        )

    def _delete_phone_file(self, filename: str) -> None:
        def task() -> None:
            adb_tools.delete_phone_file(filename, self._cfg, serial=self._serial)

        def done(_: object) -> None:
            self._show_banner(f"Deleted {filename} from phone.")
            self._refresh_phone_files()

        self._run_in_thread(
            task,
            on_done=done,
            on_error=lambda err: self._show_banner(err, is_error=True),
        )

    def _launch_app(self, pkg: str) -> None:
        self.footer_label.setText(f"Launching {_friendly_app_name(pkg)}...")

        def task() -> None:
            adb_tools.launch_package(pkg, self._cfg, serial=self._serial)

        def done(_: object) -> None:
            self.footer_label.setText(f"Launched {pkg}.")

        def fail(err: str) -> None:
            self.footer_label.setText(f"Could not launch {pkg}.")
            self._show_banner(err, is_error=True)

        self._run_in_thread(task, on_done=done, on_error=fail)

    def _uninstall_app(self, pkg: str) -> None:
        reply = QMessageBox.question(
            self,
            "Uninstall App",
            f"Uninstall {_friendly_app_name(pkg)} ({pkg})?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.footer_label.setText(f"Uninstalling {pkg}...")

        def task() -> None:
            adb_tools.uninstall_package(pkg, self._cfg, serial=self._serial)

        def done(_: object) -> None:
            self.footer_label.setText(f"Uninstalled {pkg}.")
            self._show_banner(f"Uninstalled {pkg}.")
            self._refresh_apps()

        def fail(err: str) -> None:
            self.footer_label.setText(f"Uninstall failed: {pkg}")
            self._show_banner(err, is_error=True)

        self._run_in_thread(task, on_done=done, on_error=fail)

    def _send_key(self, keycode: int, label: str) -> None:
        if self._state != "ready":
            return

        def task() -> None:
            adb_tools.send_keyevent(keycode, self._cfg, serial=self._serial)

        self._run_in_thread(
            task,
            on_done=lambda _: self.footer_label.setText(f"Sent {label}."),
            on_error=lambda err: self.footer_label.setText(err.splitlines()[0]),
        )

    def _rotate_screen(self) -> None:
        if self._state != "ready":
            return

        def task() -> int:
            return adb_tools.toggle_orientation(self._cfg, serial=self._serial)

        self._run_in_thread(
            task,
            on_done=lambda r: self.footer_label.setText(
                "Rotated to Landscape." if r == 1 else "Rotated to Portrait."
            ),
            on_error=lambda err: self.footer_label.setText(err.splitlines()[0]),
        )

    def _take_screenshot(self) -> None:
        if self._state != "ready":
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = SCREENSHOTS_DIR / f"screenshot_{stamp}.png"

        def task() -> Path:
            return adb_tools.take_screenshot(dest, self._cfg, serial=self._serial)

        def done(path: object) -> None:
            self.footer_label.setText(f"Screenshot saved: {path}")
            self._show_banner(f"Screenshot saved to {path}")

        self._run_in_thread(
            task,
            on_done=done,
            on_error=lambda err: self._show_banner(err, is_error=True),
        )

    def _save_settings_clicked(self) -> None:
        if self._state in ("booting", "stopping"):
            self._show_banner(
                "Wait until Android finishes starting or stopping before changing settings.",
                is_error=True,
            )
            return

        prev_ver = self._cfg.android_version
        updated = AppConfig(
            setup_complete=self._cfg.setup_complete,
            sdk_path=self._cfg.sdk_path,
            jdk_path=self._cfg.jdk_path,
            android_version=str(self.android_ver_combo.currentData()),
            ram_gb=int(self.ram_combo.currentData()),
            cpu_cores=int(self.cores_spin.value()),
            resolution_preset=str(self.res_combo.currentData()),
            gpu_acceleration=self.gpu_check.isChecked(),
            clean_boot=self.clean_boot_check.isChecked(),
            low_end_mode=self.low_end_check.isChecked(),
        ).normalized()

        self._cfg = save_config(updated)
        apply_avd_settings(self._cfg)
        self._update_hw_summary()

        if updated.android_version != prev_ver or self._needs_setup():
            def proceed() -> None:
                self._root_stack.setCurrentIndex(0)
                self._start_setup()

            if self._state == "ready":
                self._stop_android(on_stopped=proceed)
            else:
                proceed()
            return

        if self._state == "ready":
            self._show_banner(
                "Settings saved. Hardware and display changes will take effect next time Android starts."
            )
        else:
            self._show_banner("Settings saved.")

    def _confirm_reset_android(self) -> None:
        reply = QMessageBox.warning(
            self,
            "Reset Android",
            "This will wipe all installed apps and user data inside SimpleDroid.\n\n"
            "Do you want to continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        if self._state == "ready":
            self._stop_android(on_stopped=lambda: self._start_android(wipe_data=True))
        elif self._state == "stopped":
            self._start_android(wipe_data=True)
        else:
            self._show_banner(
                "Wait until Android finishes starting or stopping before resetting.",
                is_error=True,
            )

    def _on_associate_apk(self) -> None:
        if self._associate_apk_cb is None:
            return
        try:
            self._associate_apk_cb()
            self._show_banner(
                ".apk files are now associated with SimpleDroid. Double-clicking any .apk installs it."
            )
        except Exception as exc:
            self._show_banner(str(exc), is_error=True)

    def closeEvent(self, event) -> None:
        if self._emulator.is_process_running() and not self._closing_after_stop:
            self._closing_after_stop = True
            event.ignore()
            self._stop_android()
            return
        event.accept()
