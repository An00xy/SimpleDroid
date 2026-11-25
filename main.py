"""Entry point, taskbar icon, single-instance IPC, and Windows .apk association for SimpleDroid."""

from __future__ import annotations

import ctypes
from pathlib import Path
import socket
import sys
import threading
import winreg

from config import APP_DATA_DIR, ensure_app_dirs, get_asset_path

IPC_PORT_FILE = APP_DATA_DIR / "ipc.port"


def _set_windows_app_id() -> None:
    """Set explicit Windows AppUserModelID so the taskbar uses SimpleDroid's custom icon."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SimpleDroid.App")
    except Exception:
        pass


def _get_launch_command() -> str:
    """Return the Windows shell open command for .apk file association."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" "%1"'

    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    runner = pythonw if pythonw.is_file() else exe
    script = Path(__file__).resolve()
    return f'"{runner}" "{script}" "%1"'


def _get_apk_icon_value() -> str:
    """Return the DefaultIcon registry value using SimpleDroid's app icon."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}",0'
    ico_path = get_asset_path("assets/icon.ico")
    if ico_path.is_file():
        return str(ico_path)
    return f'"{sys.executable}",0'


def register_apk_association() -> None:
    """Associate .apk files with SimpleDroid in HKCU (no administrator rights required)."""
    cmd = _get_launch_command()
    icon_val = _get_apk_icon_value()
    prog_id = "SimpleDroid.APK"

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\.apk") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, prog_id)

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{prog_id}") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "Android Application Package")

    with winreg.CreateKey(
        winreg.HKEY_CURRENT_USER, rf"Software\Classes\{prog_id}\DefaultIcon"
    ) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, icon_val)

    with winreg.CreateKey(
        winreg.HKEY_CURRENT_USER, rf"Software\Classes\{prog_id}\shell\open\command"
    ) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, cmd)

    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
    except Exception:
        pass


def _parse_initial_apk(argv: list[str]) -> str | None:
    for arg in argv[1:]:
        cleaned = arg.strip().strip('"')
        if not cleaned:
            continue
        p = Path(cleaned).expanduser()
        if p.suffix.lower() == ".apk":
            return str(p.resolve())
    return None


def _send_to_existing_instance(payload: str) -> bool:
    """Send an APK path or SHOW command to an already-running instance before loading Qt."""
    if not IPC_PORT_FILE.is_file():
        return False
    try:
        port = int(IPC_PORT_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6) as conn:
            conn.sendall((payload + "\n").encode("utf-8"))
            conn.recv(16)
        return True
    except OSError:
        return False


def _start_ipc_server(window) -> socket.socket | None:
    """Listen on localhost TCP so double-clicking .apk files routes cleanly to the active window."""
    ensure_app_dirs()
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        port = srv.getsockname()[1]
        IPC_PORT_FILE.write_text(str(port), encoding="utf-8")
    except OSError:
        return None

    def _serve() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            with conn:
                try:
                    data = b""
                    while not data.endswith(b"\n"):
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    conn.sendall(b"OK\n")
                    text = data.decode("utf-8", errors="replace").strip()
                    if text == "SHOW":
                        window.bring_to_front_signal.emit()
                    elif text:
                        window.external_apk_signal.emit(text)
                except OSError:
                    pass

    threading.Thread(target=_serve, daemon=True).start()
    return srv


def main() -> int:
    initial_apk = _parse_initial_apk(sys.argv)

    if _send_to_existing_instance(initial_apk or "SHOW"):
        return 0

    from PySide6.QtWidgets import QApplication, QMessageBox
    from ui import MainWindow, load_app_icon

    _set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName("SimpleDroid")
    app.setWindowIcon(load_app_icon())

    def _excepthook(exc_type, exc_value, _tb) -> None:
        QMessageBox.critical(
            None,
            "SimpleDroid Error",
            f"An unexpected error occurred:\n\n{exc_value or exc_type.__name__}",
        )

    sys.excepthook = _excepthook

    window = MainWindow(
        associate_apk_cb=register_apk_association,
        initial_apk=initial_apk,
    )
    srv = _start_ipc_server(window)
    window.show()
    try:
        return app.exec()
    finally:
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
        IPC_PORT_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
