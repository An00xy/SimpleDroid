"""Subprocess wrapper around Android Debug Bridge (adb) with friendly error handling."""

from __future__ import annotations

from pathlib import Path
import posixpath
import subprocess

from config import APP_DATA_DIR, AppConfig
from sdk_manager import get_adb_path, get_sdk_subprocess_env

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0)
PHONE_FILES_DIR = "/sdcard/Download"


class AdbError(RuntimeError):
    """Human-friendly ADB error."""


def _require_adb(cfg: AppConfig | None = None) -> Path:
    adb = get_adb_path(cfg)
    if adb is None:
        raise AdbError(
            "Android Debug Bridge (adb.exe) was not found in the configured SDK.\n"
            "Run first-time setup or check your SDK installation."
        )
    return adb


def _run_adb(
    args: list[str],
    cfg: AppConfig | None = None,
    serial: str | None = None,
    timeout: int = 30,
    target_emulator: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Execute an adb command cleanly without opening a console window."""
    adb = _require_adb(cfg)
    target_args = ["-s", serial] if serial else (["-e"] if target_emulator else [])
    cmd = [str(adb), *target_args, *args]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(APP_DATA_DIR),
            env=get_sdk_subprocess_env(cfg),
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdbError(f"ADB command timed out: adb {' '.join(args)}") from exc
    except OSError as exc:
        raise AdbError(f"Failed to run adb.exe: {exc}") from exc


def ensure_adb_server(cfg: AppConfig | None = None) -> None:
    """Start the local adb daemon cleanly, detached from SimpleDroid's handles."""
    adb = get_adb_path(cfg)
    if adb is None:
        return
    try:
        subprocess.run(
            [str(adb), "start-server"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(APP_DATA_DIR),
            env=get_sdk_subprocess_env(cfg),
            timeout=15,
            close_fds=True,
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


def find_running_emulator_serial(cfg: AppConfig | None = None) -> str | None:
    """Return the serial (e.g. 'emulator-5554') of an online emulator, or None."""
    adb = get_adb_path(cfg)
    if adb is None:
        return None
    try:
        res = subprocess.run(
            [str(adb), "devices"],
            capture_output=True,
            text=True,
            cwd=str(APP_DATA_DIR),
            env=get_sdk_subprocess_env(cfg),
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    for line in res.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0].startswith("emulator-") and parts[1] == "device":
            return parts[0]
    return None


def is_boot_completed(cfg: AppConfig | None = None, serial: str | None = None) -> bool:
    """Poll sys.boot_completed via adb to detect Android boot completion."""
    try:
        res = _run_adb(
            ["shell", "getprop", "sys.boot_completed"],
            cfg=cfg,
            serial=serial,
            timeout=8,
        )
    except AdbError:
        return False
    return res.returncode == 0 and res.stdout.strip() == "1"


def apply_low_end_optimizations(
    cfg: AppConfig | None = None,
    serial: str | None = None,
) -> None:
    """Reduce system animations so Android runs smoothly on low-end hardware."""
    settings = (
        ("window_animation_scale", "0"),
        ("transition_animation_scale", "0"),
        ("animator_duration_scale", "0"),
        ("fancy_ime_animations", "0"),
    )
    for key, val in settings:
        try:
            _run_adb(
                ["shell", "settings", "put", "global", key, val],
                cfg=cfg,
                serial=serial,
                timeout=8,
            )
        except AdbError:
            pass


def install_apk(apk_path: str | Path, cfg: AppConfig | None = None, serial: str | None = None) -> str:
    """Install an APK using `adb install -r -t <file>`. Returns status text or raises AdbError."""
    path = Path(apk_path)
    if not path.is_file():
        raise AdbError(f"APK file not found:\n{path}")
    if path.suffix.lower() != ".apk":
        raise AdbError(f"Selected file is not an .apk file:\n{path.name}")

    res = _run_adb(["install", "-r", "-t", str(path)], cfg=cfg, serial=serial, timeout=180)
    combined = "\n".join(p.strip() for p in (res.stdout, res.stderr) if p and p.strip())

    if res.returncode == 0 and "Success" in combined:
        return f"Installed {path.name} successfully."

    err_detail = combined or f"adb install exited with code {res.returncode}"
    raise AdbError(f"APK installation failed for {path.name}:\n\n{err_detail}")


def list_third_party_packages(
    cfg: AppConfig | None = None,
    serial: str | None = None,
) -> list[str]:
    """Return sorted package IDs of installed third-party Android apps."""
    res = _run_adb(["shell", "pm", "list", "packages", "-3"], cfg=cfg, serial=serial, timeout=20)
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        raise AdbError(f"Could not list installed apps.\n{err}")

    packages: list[str] = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            pkg = line[len("package:") :].strip()
            if pkg:
                packages.append(pkg)
    return sorted(set(packages), key=str.lower)


def launch_package(
    package_name: str,
    cfg: AppConfig | None = None,
    serial: str | None = None,
) -> None:
    """Launch an installed Android app via monkey launcher intent or resolved main activity."""
    res = _run_adb(
        ["shell", "monkey", "-p", package_name, "-c", "android.intent.category.LAUNCHER", "1"],
        cfg=cfg,
        serial=serial,
        timeout=20,
    )
    combined = "\n".join(p.strip() for p in (res.stdout, res.stderr) if p and p.strip())
    if res.returncode == 0 and "No activities found" not in combined and "monkey aborted" not in combined.lower():
        return

    resolved = _run_adb(
        ["shell", "cmd", "package", "resolve-activity", "--brief", package_name],
        cfg=cfg,
        serial=serial,
        timeout=15,
    )
    comp = ""
    for line in resolved.stdout.splitlines():
        line = line.strip()
        if "/" in line:
            comp = line
    if comp:
        start_res = _run_adb(
            ["shell", "am", "start", "-n", comp],
            cfg=cfg,
            serial=serial,
            timeout=15,
        )
        if start_res.returncode == 0 and "Error" not in (start_res.stderr or ""):
            return

    raise AdbError(
        f"Could not launch {package_name}.\n{combined or 'No launcher activity found.'}"
    )


def uninstall_package(
    package_name: str,
    cfg: AppConfig | None = None,
    serial: str | None = None,
) -> None:
    """Uninstall an Android app via `adb uninstall <pkg>`."""
    res = _run_adb(["uninstall", package_name], cfg=cfg, serial=serial, timeout=45)
    combined = "\n".join(p.strip() for p in (res.stdout, res.stderr) if p and p.strip())
    if res.returncode != 0 or "Success" not in combined:
        raise AdbError(
            f"Failed to uninstall {package_name}.\n{combined or f'Exit code {res.returncode}'}"
        )


def push_file_to_phone(
    local_path: str | Path,
    cfg: AppConfig | None = None,
    serial: str | None = None,
    remote_dir: str = PHONE_FILES_DIR,
) -> str:
    """Copy a local file directly into the Android phone's Download folder."""
    path = Path(local_path)
    if not path.is_file():
        raise AdbError(f"File not found:\n{path}")

    remote_path = posixpath.join(remote_dir, path.name)
    res = _run_adb(["push", str(path), remote_path], cfg=cfg, serial=serial, timeout=180)
    combined = "\n".join(p.strip() for p in (res.stdout, res.stderr) if p and p.strip())
    if res.returncode != 0:
        raise AdbError(f"Failed to add {path.name} to phone:\n{combined}")

    try:
        _run_adb(
            [
                "shell",
                "am",
                "broadcast",
                "-a",
                "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                "-d",
                f"file://{remote_path}",
            ],
            cfg=cfg,
            serial=serial,
            timeout=10,
        )
    except AdbError:
        pass

    return f"Added {path.name} to phone ({remote_path})."


def list_phone_files(
    cfg: AppConfig | None = None,
    serial: str | None = None,
    remote_dir: str = PHONE_FILES_DIR,
) -> list[tuple[str, int]]:
    """Return list of (filename, size_bytes) in the phone's Download directory."""
    res = _run_adb(
        ["shell", f"ls -lA {remote_dir}"],
        cfg=cfg,
        serial=serial,
        timeout=15,
    )
    if res.returncode != 0:
        return []

    files: list[tuple[str, int]] = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("total ") or line.startswith("d"):
            continue
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        size_str = parts[4]
        name = parts[7]
        if name.startswith("."):
            continue
        size_bytes = int(size_str) if size_str.isdigit() else 0
        files.append((name, size_bytes))
    return sorted(files, key=lambda item: item[0].lower())


def pull_file_from_phone(
    remote_name: str,
    local_dest: str | Path,
    cfg: AppConfig | None = None,
    serial: str | None = None,
    remote_dir: str = PHONE_FILES_DIR,
) -> Path:
    """Copy a file from the phone's Download folder to the PC."""
    remote_path = posixpath.join(remote_dir, remote_name)
    dest = Path(local_dest)
    res = _run_adb(["pull", remote_path, str(dest)], cfg=cfg, serial=serial, timeout=180)
    if res.returncode != 0:
        combined = "\n".join(p.strip() for p in (res.stdout, res.stderr) if p and p.strip())
        raise AdbError(f"Failed to download {remote_name} from phone:\n{combined}")
    return dest


def delete_phone_file(
    remote_name: str,
    cfg: AppConfig | None = None,
    serial: str | None = None,
    remote_dir: str = PHONE_FILES_DIR,
) -> None:
    """Delete a file from the phone's Download folder."""
    remote_path = posixpath.join(remote_dir, remote_name)
    res = _run_adb(["shell", "rm", "-f", f'"{remote_path}"'], cfg=cfg, serial=serial, timeout=15)
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        raise AdbError(f"Could not delete {remote_name}:\n{err}")


def send_keyevent(
    keycode: int,
    cfg: AppConfig | None = None,
    serial: str | None = None,
) -> None:
    """Send an Android input keyevent (e.g. Home=3, Back=4, Recents=187)."""
    res = _run_adb(
        ["shell", "input", "keyevent", str(keycode)],
        cfg=cfg,
        serial=serial,
        timeout=10,
    )
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        raise AdbError(f"Failed to send device key command.\n{err}")


def toggle_orientation(cfg: AppConfig | None = None, serial: str | None = None) -> int:
    """Toggle screen rotation between portrait (0) and landscape (1)."""
    _run_adb(
        ["shell", "settings", "put", "system", "accelerometer_rotation", "0"],
        cfg=cfg,
        serial=serial,
        timeout=10,
    )
    res = _run_adb(
        ["shell", "settings", "get", "system", "user_rotation"],
        cfg=cfg,
        serial=serial,
        timeout=10,
    )
    cur = res.stdout.strip()
    new_rot = 0 if cur == "1" else 1
    _run_adb(
        ["shell", "settings", "put", "system", "user_rotation", str(new_rot)],
        cfg=cfg,
        serial=serial,
        timeout=10,
    )
    return new_rot


def take_screenshot(
    dest_path: Path,
    cfg: AppConfig | None = None,
    serial: str | None = None,
) -> Path:
    """Capture a PNG screenshot from the running Android device."""
    adb = _require_adb(cfg)
    target_args = ["-s", serial] if serial else ["-e"]
    cmd = [str(adb), *target_args, "exec-out", "screencap", "-p"]
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            cwd=str(APP_DATA_DIR),
            env=get_sdk_subprocess_env(cfg),
            timeout=20,
            creationflags=CREATE_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise AdbError(f"Failed to capture screenshot: {exc}") from exc

    if res.returncode != 0 or not res.stdout:
        raise AdbError("Screenshot capture failed.")

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(res.stdout)
    return dest_path


def kill_emulator(cfg: AppConfig | None = None, serial: str | None = None) -> bool:
    """Ask the running emulator to shut down cleanly via `adb emu kill`."""
    try:
        res = _run_adb(["emu", "kill"], cfg=cfg, serial=serial, timeout=10)
        return res.returncode == 0
    except AdbError:
        return False
