"""First-run environment discovery, duplicate cleanup, JDK/SDK setup, and AVD management."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from typing import Callable

from config import (
    ANDROID_REPO_BASE_URL,
    ANDROID_REPO_XML_URL,
    ANDROID_VERSIONS,
    AVD_DEVICE_PROFILE,
    AVD_DIR,
    AVD_NAME,
    AppConfig,
    DEFAULT_ANDROID_VERSION,
    DEFAULT_JDK_DIR,
    DEFAULT_SDK_DIR,
    RESOLUTION_PRESETS,
    TEMURIN_JDK17_ZIP_URL,
    ensure_app_dirs,
    save_config,
)

ProgressCallback = Callable[[int, str], None]

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DOWNLOAD_TIMEOUT_SEC = 60
USER_AGENT = "SimpleDroid/1.0"


class SetupError(RuntimeError):
    """Human-friendly setup error."""


@dataclass
class HypervisorStatus:
    supported: bool
    whpx_available: bool
    hypervisor_present: bool
    firmware_virt_enabled: bool | None
    message: str


def check_hypervisor_support() -> HypervisorStatus:
    """Check whether Windows Hypervisor Platform (WHPX) or Hyper-V is active."""
    whpx_available = _check_whpx_api()
    hypervisor_present = _check_wmi_hypervisor_present()
    firmware_virt = _check_firmware_virtualization()

    if whpx_available or hypervisor_present:
        return HypervisorStatus(
            supported=True,
            whpx_available=whpx_available,
            hypervisor_present=hypervisor_present,
            firmware_virt_enabled=firmware_virt,
            message="Windows virtualization (WHPX / Hyper-V) is enabled.",
        )

    if firmware_virt is False:
        msg = (
            "Hardware virtualization (VT-x / AMD-V) appears disabled in your BIOS/UEFI, "
            "or Windows Hypervisor Platform is not enabled.\n\n"
            "1. Enable virtualization in BIOS/UEFI if disabled.\n"
            "2. Open 'Turn Windows features on or off', check 'Windows Hypervisor Platform', "
            "click OK, and restart Windows."
        )
    else:
        msg = (
            "Windows Hypervisor Platform (WHPX) or Hyper-V is not enabled.\n\n"
            "Click 'Open Windows Features' below, check 'Windows Hypervisor Platform', "
            "click OK, and restart your PC before continuing."
        )

    return HypervisorStatus(
        supported=False,
        whpx_available=False,
        hypervisor_present=False,
        firmware_virt_enabled=firmware_virt,
        message=msg,
    )


def _check_whpx_api() -> bool:
    """Query WinHvPlatform.dll WHvGetCapability(HypervisorPresent) without admin rights."""
    try:
        winhv = ctypes.WinDLL("WinHvPlatform.dll")
    except OSError:
        return False

    try:
        whv_get_cap = winhv.WHvGetCapability
        whv_get_cap.argtypes = [
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        whv_get_cap.restype = ctypes.c_long

        present = ctypes.c_uint32(0)
        written = ctypes.c_uint32(0)
        hr = whv_get_cap(0, ctypes.byref(present), ctypes.sizeof(present), ctypes.byref(written))
        return hr == 0 and bool(present.value)
    except Exception:
        return False


def _check_wmi_hypervisor_present() -> bool:
    """Check Win32_ComputerSystem.HypervisorPresent via PowerShell CIM."""
    try:
        res = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_ComputerSystem).HypervisorPresent",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        return res.returncode == 0 and res.stdout.strip().lower() == "true"
    except Exception:
        return False


def _check_firmware_virtualization() -> bool | None:
    """Check Win32_Processor.VirtualizationFirmwareEnabled."""
    try:
        res = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_Processor | Select-Object -First 1).VirtualizationFirmwareEnabled",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        val = res.stdout.strip().lower()
        if val == "true":
            return True
        if val == "false":
            return False
    except Exception:
        pass
    return None


def open_windows_features_dialog() -> None:
    """Open Windows 'Turn Windows features on or off' (optionalfeatures.exe)."""
    try:
        subprocess.Popen(["optionalfeatures.exe"])
    except OSError as exc:
        raise SetupError(f"Could not launch optionalfeatures.exe: {exc}") from exc


def _is_java_17_plus(java_exe: Path) -> bool:
    """Verify the given java.exe is Java 17 or newer."""
    try:
        res = subprocess.run(
            [str(java_exe), "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        output = (res.stderr or "") + "\n" + (res.stdout or "")
        match = re.search(r'version\s+"(\d+)', output)
        if not match:
            return False
        return int(match.group(1)) >= 17
    except Exception:
        return False


def _find_jdk_in_dir(base: Path) -> Path | None:
    """Return JDK root inside base (either base itself or one subdirectory deep)."""
    if not base.is_dir():
        return None
    if (base / "bin" / "java.exe").is_file() and _is_java_17_plus(base / "bin" / "java.exe"):
        return base
    for child in sorted(base.iterdir()):
        if child.is_dir():
            java_exe = child / "bin" / "java.exe"
            if java_exe.is_file() and _is_java_17_plus(java_exe):
                return child
    return None


def _external_jdk_candidates(cfg: AppConfig | None = None) -> list[Path]:
    """Return ordered list of standard external JDK 17+ candidate directories."""
    candidates: list[Path] = []
    if cfg and cfg.jdk_path:
        p = Path(cfg.jdk_path)
        try:
            if p.resolve() != DEFAULT_JDK_DIR.resolve() and DEFAULT_JDK_DIR.resolve() not in p.resolve().parents:
                candidates.append(p)
        except OSError:
            candidates.append(p)

    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidates.append(Path(java_home))

    path_java = shutil.which("java")
    if path_java:
        candidates.append(Path(path_java).resolve().parent.parent)

    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    local_app_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    candidates.extend(
        [
            program_files / "Android" / "Android Studio" / "jbr",
            program_files / "Android" / "Android Studio" / "jre",
            program_files / "Eclipse Adoptium",
            program_files / "Microsoft",
            program_files / "Java",
            local_app_data / "Programs" / "Eclipse Adoptium",
        ]
    )
    return candidates


def discover_jdk_home(cfg: AppConfig | None = None, cleanup_duplicate: bool = False) -> Path | None:
    """Find an existing JDK 17+ on the machine, falling back to SimpleDroid's portable JDK."""
    for candidate in _external_jdk_candidates(cfg):
        found = _find_jdk_in_dir(candidate)
        if found is not None:
            if cleanup_duplicate and DEFAULT_JDK_DIR.exists():
                shutil.rmtree(DEFAULT_JDK_DIR, ignore_errors=True)
            return found

    return _find_jdk_in_dir(DEFAULT_JDK_DIR)


def _is_valid_sdk_dir(path: Path) -> bool:
    """Check whether path is an existing Android SDK directory."""
    if not path.is_dir():
        return False
    markers = ("platform-tools", "emulator", "cmdline-tools", "platforms")
    return any((path / m).is_dir() for m in markers)


def _external_sdk_candidates(cfg: AppConfig | None = None) -> list[Path]:
    """Return ordered list of standard external Android SDK candidate directories."""
    candidates: list[Path] = []
    if cfg and cfg.sdk_path:
        p = Path(cfg.sdk_path)
        try:
            if p.resolve() != DEFAULT_SDK_DIR.resolve():
                candidates.append(p)
        except OSError:
            candidates.append(p)

    local_app_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    candidates.append(local_app_data / "Android" / "Sdk")

    for env_var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        val = os.environ.get(env_var)
        if val:
            candidates.append(Path(val))

    candidates.extend(
        [
            Path.home() / "Android" / "Sdk",
            Path(r"C:\Android\Sdk"),
        ]
    )
    return candidates


def resolve_sdk_dir(cfg: AppConfig | None = None, cleanup_duplicate: bool = False) -> Path:
    """Return the Android SDK directory to use (reusing an existing standard SDK if present)."""
    for candidate in _external_sdk_candidates(cfg):
        if _is_valid_sdk_dir(candidate):
            if cleanup_duplicate and DEFAULT_SDK_DIR.exists():
                shutil.rmtree(DEFAULT_SDK_DIR, ignore_errors=True)
            return candidate

    return DEFAULT_SDK_DIR


def resolve_avd_home() -> Path:
    """Return the AVD directory containing SimpleDroid AVD, defaulting to %LOCALAPPDATA%\\SimpleDroid\\avd."""
    default_user_avd = Path.home() / ".android" / "avd"
    if (default_user_avd / f"{AVD_NAME}.ini").is_file() and (
        default_user_avd / f"{AVD_NAME}.avd"
    ).is_dir():
        return default_user_avd
    return AVD_DIR


def get_cmdline_tools_bin(sdk_dir: Path) -> Path | None:
    """Return path to cmdline-tools/latest/bin (or any cmdline-tools/<ver>/bin) if installed."""
    latest_bin = sdk_dir / "cmdline-tools" / "latest" / "bin"
    if (latest_bin / "sdkmanager.bat").is_file() and (latest_bin / "avdmanager.bat").is_file():
        return latest_bin

    cmdline_root = sdk_dir / "cmdline-tools"
    if cmdline_root.is_dir():
        for child in sorted(cmdline_root.iterdir(), reverse=True):
            bin_dir = child / "bin"
            if (bin_dir / "sdkmanager.bat").is_file() and (bin_dir / "avdmanager.bat").is_file():
                return bin_dir
    return None


def get_adb_path(cfg: AppConfig | None = None) -> Path | None:
    """Return path to adb.exe if installed in the active SDK."""
    sdk_dir = resolve_sdk_dir(cfg)
    adb = sdk_dir / "platform-tools" / "adb.exe"
    return adb if adb.is_file() else None


def get_emulator_path(cfg: AppConfig | None = None) -> Path | None:
    """Return path to emulator.exe if installed in the active SDK."""
    sdk_dir = resolve_sdk_dir(cfg)
    emu = sdk_dir / "emulator" / "emulator.exe"
    return emu if emu.is_file() else None


def is_avd_created() -> bool:
    """Check whether the SimpleDroid AVD exists in AVD_DIR or ~/.android/avd."""
    avd_home = resolve_avd_home()
    return (avd_home / f"{AVD_NAME}.ini").is_file() and (avd_home / f"{AVD_NAME}.avd").is_dir()


def get_avd_android_version() -> str | None:
    """Read which Android version key (e.g. 'android-35') the SimpleDroid AVD currently uses."""
    config_ini = resolve_avd_home() / f"{AVD_NAME}.avd" / "config.ini"
    if not config_ini.is_file():
        return None
    try:
        for line in config_ini.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("image.sysdir.1="):
                val = line.split("=", 1)[1].strip().replace("\\", "/")
                for key in ANDROID_VERSIONS:
                    if f"/{key}/" in f"/{val}":
                        return key
    except OSError:
        pass
    return None


def is_system_image_installed(sdk_dir: Path, android_version: str) -> bool:
    """Check if the specified Android version system image is installed on disk."""
    info = ANDROID_VERSIONS.get(android_version, ANDROID_VERSIONS[DEFAULT_ANDROID_VERSION])
    sys_img_dir = sdk_dir.joinpath(*info["rel_parts"])
    return sys_img_dir.is_dir() and any(sys_img_dir.iterdir())


def get_missing_sdk_packages(sdk_dir: Path, cfg: AppConfig | None = None) -> list[str]:
    """Return list of required SDK packages not yet present on disk."""
    normalized = (cfg or AppConfig()).normalized()
    missing: list[str] = []
    if not (sdk_dir / "platform-tools" / "adb.exe").is_file():
        missing.append("platform-tools")
    if not (sdk_dir / "emulator" / "emulator.exe").is_file():
        missing.append("emulator")
    if not is_system_image_installed(sdk_dir, normalized.android_version):
        missing.append(normalized.system_image_package())
    return missing


def get_sdk_subprocess_env(cfg: AppConfig | None = None) -> dict[str, str]:
    """Return a clean subprocess environment isolated from PyInstaller's _MEI/Qt variables."""
    env = os.environ.copy()

    for key in (
        "_MEIPASS2",
        "QT_PLUGIN_PATH",
        "QT_QPA_PLATFORM_PLUGIN_PATH",
        "QT_QPA_PLATFORM",
        "QML2_IMPORT_PATH",
        "QML_IMPORT_PATH",
        "QT_ENABLE_HIGHDPI_SCALING",
        "QT_SCALE_FACTOR",
        "QT_AUTO_SCREEN_SCALE_FACTOR",
    ):
        env.pop(key, None)

    meipass = getattr(sys, "_MEIPASS", None)
    raw_path = env.get("PATH", "")
    if raw_path:
        cleaned_entries: list[str] = []
        meipass_norm = os.path.normcase(os.path.abspath(meipass)) if meipass else None
        for entry in raw_path.split(os.pathsep):
            if not entry:
                continue
            entry_norm = os.path.normcase(os.path.abspath(entry))
            if meipass_norm and (entry_norm == meipass_norm or entry_norm.startswith(meipass_norm + os.sep)):
                continue
            if "\\_mei" in entry_norm or "/_mei" in entry_norm:
                continue
            cleaned_entries.append(entry)
        env["PATH"] = os.pathsep.join(cleaned_entries)

    sdk_dir = resolve_sdk_dir(cfg)
    jdk_home = discover_jdk_home(cfg)
    avd_home = resolve_avd_home()

    path_parts: list[str] = []
    if jdk_home is not None:
        env["JAVA_HOME"] = str(jdk_home)
        path_parts.append(str(jdk_home / "bin"))

    platform_tools = sdk_dir / "platform-tools"
    if platform_tools.exists():
        path_parts.append(str(platform_tools))

    cmdline_bin = get_cmdline_tools_bin(sdk_dir)
    if cmdline_bin is not None:
        path_parts.append(str(cmdline_bin))

    if path_parts:
        existing_path = env.get("PATH", "")
        env["PATH"] = os.pathsep.join(path_parts + ([existing_path] if existing_path else []))

    env["ANDROID_SDK_ROOT"] = str(sdk_dir)
    env["ANDROID_HOME"] = str(sdk_dir)
    env["ANDROID_AVD_HOME"] = str(avd_home)
    return env


def _download_file(
    url: str,
    dest_path: Path,
    label: str,
    progress_cb: ProgressCallback | None = None,
    pct_start: int = 0,
    pct_end: int = 100,
) -> None:
    """Download a file via HTTPS with throttled progress updates."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")

    try:
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SEC) as resp:
            total_header = resp.headers.get("Content-Length")
            total_bytes = int(total_header) if total_header and total_header.isdigit() else 0
            downloaded = 0
            chunk_size = 256 * 1024
            last_emit_time = 0.0
            last_pct = -1

            with tmp_path.open("wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        now = time.monotonic()
                        if total_bytes > 0:
                            frac = min(1.0, downloaded / total_bytes)
                            pct = pct_start + int((pct_end - pct_start) * frac)
                            if (
                                pct != last_pct
                                or (now - last_emit_time) >= 0.25
                                or downloaded == total_bytes
                            ):
                                last_pct = pct
                                last_emit_time = now
                                mb_done = downloaded / (1024 * 1024)
                                mb_total = total_bytes / (1024 * 1024)
                                progress_cb(pct, f"{label} ({mb_done:.1f} / {mb_total:.1f} MB)")
                        elif (now - last_emit_time) >= 0.25:
                            last_emit_time = now
                            mb_done = downloaded / (1024 * 1024)
                            progress_cb(pct_start, f"{label} ({mb_done:.1f} MB)")
        tmp_path.replace(dest_path)
    except urllib.error.HTTPError as exc:
        tmp_path.unlink(missing_ok=True)
        raise SetupError(f"Download failed for {label} (HTTP {exc.code}).") from exc
    except urllib.error.URLError as exc:
        tmp_path.unlink(missing_ok=True)
        raise SetupError(
            f"No internet connection or server unreachable while downloading {label}.\n"
            f"Details: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        tmp_path.unlink(missing_ok=True)
        raise SetupError(f"Download timed out for {label}. Check your internet connection.") from exc
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise SetupError(f"Disk error while saving {label}: {exc}") from exc


def _safe_extract_zip(zip_path: Path, target_dir: Path) -> None:
    """Extract zip archive safely, preventing path traversal."""
    target_resolved = target_dir.resolve()
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.infolist():
                member_path = (target_resolved / member.filename).resolve()
                if not str(member_path).startswith(str(target_resolved)):
                    raise SetupError("Unsafe path found in downloaded archive.")
            zf.extractall(target_resolved)
    except zipfile.BadZipFile as exc:
        raise SetupError(f"Downloaded archive {zip_path.name} is corrupted.") from exc


def ensure_portable_jdk(
    cfg: AppConfig | None = None,
    progress_cb: ProgressCallback | None = None,
    pct_start: int = 0,
    pct_end: int = 20,
) -> Path:
    """Reuse an existing JDK 17+ or download portable Temurin JDK 17 into %LOCALAPPDATA%\\SimpleDroid\\jdk."""
    existing = discover_jdk_home(cfg, cleanup_duplicate=True)
    if existing is not None:
        if progress_cb:
            progress_cb(pct_end, f"Using existing JDK 17+ at {existing}.")
        return existing

    ensure_app_dirs()
    DEFAULT_JDK_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = DEFAULT_JDK_DIR / "temurin-jdk17.zip"
    extract_tmp = DEFAULT_JDK_DIR / "_extract_tmp"
    if extract_tmp.exists():
        shutil.rmtree(extract_tmp, ignore_errors=True)
    extract_tmp.mkdir(parents=True, exist_ok=True)

    try:
        _download_file(
            TEMURIN_JDK17_ZIP_URL,
            zip_path,
            "Downloading portable JDK 17 (Eclipse Temurin)",
            progress_cb=progress_cb,
            pct_start=pct_start,
            pct_end=max(pct_start + 1, pct_end - 2),
        )
        if progress_cb:
            progress_cb(max(pct_start, pct_end - 1), "Extracting portable JDK 17...")
        _safe_extract_zip(zip_path, extract_tmp)

        for item in list(DEFAULT_JDK_DIR.iterdir()):
            if item not in (zip_path, extract_tmp):
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)

        extracted_items = list(extract_tmp.iterdir())
        if not extracted_items:
            raise SetupError("Portable JDK archive extracted empty contents.")

        for item in extracted_items:
            shutil.move(str(item), str(DEFAULT_JDK_DIR / item.name))

        jdk_home = _find_jdk_in_dir(DEFAULT_JDK_DIR)
        if jdk_home is None:
            raise SetupError("Portable JDK 17 was extracted, but java.exe was not found.")
        if progress_cb:
            progress_cb(pct_end, "Portable JDK 17 ready.")
        return jdk_home
    except SetupError as exc:
        raise SetupError(
            f"{exc}\n\n"
            "Manual fallback: Download an Eclipse Temurin JDK 17 ZIP for Windows x64 from "
            "https://adoptium.net/temurin/releases/?version=17 and extract it into:\n"
            f"{DEFAULT_JDK_DIR}"
        ) from exc
    finally:
        zip_path.unlink(missing_ok=True)
        shutil.rmtree(extract_tmp, ignore_errors=True)


def resolve_latest_cmdline_tools_url() -> str:
    """Resolve the official Google download URL for Windows Android SDK command-line tools."""
    req = urllib.request.Request(ANDROID_REPO_XML_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SEC) as resp:
            xml_bytes = resp.read()
    except urllib.error.URLError as exc:
        raise SetupError(
            f"Could not contact Google Android SDK repository.\nDetails: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise SetupError("Timed out contacting Google Android SDK repository.") from exc

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise SetupError(f"Failed to parse Android SDK repository manifest: {exc}") from exc

    for pkg in root.findall(".//remotePackage"):
        if pkg.attrib.get("path") != "cmdline-tools;latest":
            continue
        for archive in pkg.findall(".//archive"):
            host_os = archive.findtext("host-os")
            if host_os == "windows":
                filename = archive.findtext("./complete/url")
                if filename:
                    url = filename if filename.startswith("https://") else f"{ANDROID_REPO_BASE_URL}{filename}"
                    if not url.startswith("https://dl.google.com/"):
                        raise SetupError(f"Unexpected non-Google SDK download URL: {url}")
                    return url

    raise SetupError("Could not find Windows command-line tools in Google's SDK repository.")


def ensure_cmdline_tools(
    sdk_dir: Path,
    progress_cb: ProgressCallback | None = None,
    pct_start: int = 0,
    pct_end: int = 20,
) -> Path:
    """Ensure Android SDK command-line tools exist inside sdk_dir."""
    existing_bin = get_cmdline_tools_bin(sdk_dir)
    if existing_bin is not None:
        if progress_cb:
            progress_cb(pct_end, "Android SDK command-line tools already installed.")
        return existing_bin

    sdk_dir.mkdir(parents=True, exist_ok=True)
    if progress_cb:
        progress_cb(pct_start, "Checking latest Android command-line tools...")
    url = resolve_latest_cmdline_tools_url()

    zip_path = sdk_dir / "cmdline-tools.zip"
    extract_tmp = sdk_dir / "_cmdline_tmp"
    latest_dir = sdk_dir / "cmdline-tools" / "latest"

    if extract_tmp.exists():
        shutil.rmtree(extract_tmp, ignore_errors=True)
    extract_tmp.mkdir(parents=True, exist_ok=True)

    try:
        _download_file(
            url,
            zip_path,
            "Downloading Android SDK command-line tools",
            progress_cb=progress_cb,
            pct_start=pct_start + 1,
            pct_end=max(pct_start + 2, pct_end - 2),
        )
        if progress_cb:
            progress_cb(max(pct_start, pct_end - 1), "Extracting Android SDK command-line tools...")
        _safe_extract_zip(zip_path, extract_tmp)

        source_dir = extract_tmp / "cmdline-tools"
        if not source_dir.is_dir():
            subdirs = [d for d in extract_tmp.iterdir() if d.is_dir()]
            if not subdirs:
                raise SetupError("Android command-line tools archive had unexpected contents.")
            source_dir = subdirs[0]

        if latest_dir.exists():
            shutil.rmtree(latest_dir, ignore_errors=True)
        latest_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_dir), str(latest_dir))

        bin_dir = get_cmdline_tools_bin(sdk_dir)
        if bin_dir is None:
            raise SetupError("sdkmanager.bat was not found after extracting command-line tools.")
        if progress_cb:
            progress_cb(pct_end, "Android SDK command-line tools ready.")
        return bin_dir
    finally:
        zip_path.unlink(missing_ok=True)
        shutil.rmtree(extract_tmp, ignore_errors=True)


def accept_sdk_licenses(
    sdk_dir: Path,
    cfg: AppConfig | None = None,
    progress_cb: ProgressCallback | None = None,
    pct: int = 50,
) -> None:
    """Automatically accept Android SDK licenses via sdkmanager."""
    bin_dir = get_cmdline_tools_bin(sdk_dir)
    if bin_dir is None:
        raise SetupError("Android SDK command-line tools are missing.")

    sdkmanager = bin_dir / "sdkmanager.bat"
    if progress_cb:
        progress_cb(pct, "Accepting Android SDK licenses...")

    res = subprocess.run(
        [str(sdkmanager), f"--sdk_root={sdk_dir}", "--licenses"],
        input="y\n" * 40,
        capture_output=True,
        text=True,
        env=get_sdk_subprocess_env(cfg),
        timeout=120,
        creationflags=CREATE_NO_WINDOW,
    )
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        raise SetupError(f"Failed to accept Android SDK licenses.\n{err[-600:]}")


def install_sdk_packages(
    sdk_dir: Path,
    packages: list[str],
    cfg: AppConfig | None = None,
    progress_cb: ProgressCallback | None = None,
    pct_start: int = 10,
    pct_end: int = 90,
) -> None:
    """Install only the specified missing SDK packages into sdk_dir."""
    if not packages:
        if progress_cb:
            progress_cb(pct_end, "All required Android SDK packages are already installed.")
        return

    bin_dir = get_cmdline_tools_bin(sdk_dir)
    if bin_dir is None:
        raise SetupError("Android SDK command-line tools are missing.")

    sdkmanager = bin_dir / "sdkmanager.bat"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".txt",
        delete=False,
    ) as pkg_file:
        pkg_file.write("\n".join(packages) + "\n")
        pkg_file_path = Path(pkg_file.name)

    cmd = [
        str(sdkmanager),
        f"--sdk_root={sdk_dir}",
        f"--package_file={pkg_file_path}",
    ]
    pkg_label = ", ".join(packages)
    if progress_cb:
        progress_cb(pct_start, f"Installing missing SDK components: {pkg_label}...")

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=get_sdk_subprocess_env(cfg),
            creationflags=CREATE_NO_WINDOW,
        )
        assert proc.stdout is not None
        assert proc.stdin is not None

        try:
            proc.stdin.write("y\n" * 20)
            proc.stdin.flush()
            proc.stdin.close()
        except OSError:
            pass

        recent_lines: list[str] = []
        pct_re = re.compile(r"(\d{1,3})%")
        last_pct = -1

        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            recent_lines.append(line)
            if len(recent_lines) > 30:
                recent_lines.pop(0)

            match = pct_re.search(line)
            if match and progress_cb:
                raw_pct = min(100, max(0, int(match.group(1))))
                mapped = pct_start + int((pct_end - pct_start) * (raw_pct / 100.0))
                if mapped != last_pct:
                    last_pct = mapped
                    progress_cb(mapped, f"Installing {pkg_label}... {raw_pct}%")
            elif progress_cb and not line.startswith("[") and not line.startswith("WARNING:"):
                progress_cb(max(pct_start, last_pct), line[:90])

        returncode = proc.wait()
        if returncode != 0:
            tail = "\n".join(recent_lines[-10:])
            raise SetupError(f"Android SDK package installation failed.\n{tail}")
    finally:
        pkg_file_path.unlink(missing_ok=True)

    still_missing = get_missing_sdk_packages(sdk_dir, cfg)
    if still_missing:
        raise SetupError(
            f"SDK installation finished, but these components are still missing: {', '.join(still_missing)}"
        )

    if progress_cb:
        progress_cb(pct_end, "Missing Android SDK components installed.")


def create_avd_if_missing(
    cfg: AppConfig,
    progress_cb: ProgressCallback | None = None,
    pct_start: int = 90,
    pct_end: int = 100,
    force_recreate: bool = False,
) -> None:
    """Create or update the SimpleDroid AVD for the configured Android version."""
    normalized = cfg.normalized()
    sdk_dir = resolve_sdk_dir(normalized)
    current_avd_ver = get_avd_android_version()
    need_create = force_recreate or not is_avd_created() or (
        current_avd_ver is not None and current_avd_ver != normalized.android_version
    )

    if need_create:
        bin_dir = get_cmdline_tools_bin(sdk_dir)
        if bin_dir is None:
            raise SetupError("Android SDK command-line tools are missing.")

        if progress_cb:
            progress_cb(pct_start, f"Creating SimpleDroid virtual device ({normalized.android_label()})...")

        jdk_home = discover_jdk_home(normalized)
        avdmanager_jar = bin_dir.parent / "lib" / "avdmanager-classpath.jar"
        sys_pkg = normalized.system_image_package()
        if jdk_home is not None and avdmanager_jar.is_file():
            cmd = [
                str(jdk_home / "bin" / "java.exe"),
                f"-Dcom.android.sdkmanager.toolsdir={bin_dir.parent}",
                "-classpath",
                str(avdmanager_jar),
                "com.android.sdklib.tool.AvdManagerCli",
                "create",
                "avd",
                "-n",
                AVD_NAME,
                "-k",
                sys_pkg,
                "-d",
                AVD_DEVICE_PROFILE,
                "--force",
            ]
        else:
            cmd = [
                str(bin_dir / "avdmanager.bat"),
                "create",
                "avd",
                "-n",
                AVD_NAME,
                "-k",
                sys_pkg,
                "-d",
                AVD_DEVICE_PROFILE,
                "--force",
            ]

        res = subprocess.run(
            cmd,
            input="no\n",
            capture_output=True,
            text=True,
            env=get_sdk_subprocess_env(normalized),
            timeout=120,
            creationflags=CREATE_NO_WINDOW,
        )
        if res.returncode != 0 or not is_avd_created():
            err = (res.stderr or res.stdout or "").strip()
            raise SetupError(f"Failed to create SimpleDroid AVD.\n{err[-600:]}")

    apply_avd_settings(normalized)
    if progress_cb:
        progress_cb(pct_end, "SimpleDroid setup complete!")


def apply_avd_settings(cfg: AppConfig) -> None:
    """Update SimpleDroid.avd/config.ini with RAM, CPU cores, storage, resolution, and GPU."""
    avd_config_path = resolve_avd_home() / f"{AVD_NAME}.avd" / "config.ini"
    if not avd_config_path.is_file():
        return

    normalized = cfg.normalized()
    preset = RESOLUTION_PRESETS[normalized.resolution_preset]
    heap_mb = "256" if normalized.ram_gb <= 3 else "512"
    updates = {
        "hw.ramSize": str(normalized.ram_gb * 1024),
        "vm.heapSize": heap_mb,
        "hw.cpu.ncore": str(normalized.cpu_cores),
        "disk.dataPartition.size": "16G",
        "hw.lcd.width": str(preset["width"]),
        "hw.lcd.height": str(preset["height"]),
        "hw.lcd.density": str(preset["density"]),
        "hw.gpu.enabled": "yes",
        "hw.gpu.mode": "auto" if normalized.gpu_acceleration else "swiftshader_indirect",
        "hw.keyboard": "yes",
        "hw.audioInput": "no",
        "fastboot.forceColdBoot": "no",
    }

    lines = avd_config_path.read_text(encoding="utf-8", errors="replace").splitlines()
    seen: set[str] = set()
    new_lines: list[str] = []

    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        new_lines.append(line)

    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={value}")

    avd_config_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def run_first_time_setup(
    cfg: AppConfig,
    progress_cb: ProgressCallback | None = None,
) -> AppConfig:
    """Run setup, reusing existing JDK/SDK, cleaning own duplicates, and only doing remaining work."""
    ensure_app_dirs()
    hypervisor = check_hypervisor_support()
    if not hypervisor.supported:
        raise SetupError(hypervisor.message)

    normalized = cfg.normalized()
    existing_jdk = discover_jdk_home(normalized, cleanup_duplicate=True)
    sdk_dir = resolve_sdk_dir(normalized, cleanup_duplicate=True)
    normalized.sdk_path = str(sdk_dir)

    need_jdk = existing_jdk is None
    need_cmdline = get_cmdline_tools_bin(sdk_dir) is None
    missing_pkgs = get_missing_sdk_packages(sdk_dir, normalized)
    current_avd_ver = get_avd_android_version()
    need_avd = not is_avd_created() or (
        current_avd_ver is not None and current_avd_ver != normalized.android_version
    )

    steps: list[tuple[str, int]] = []
    if need_jdk:
        steps.append(("jdk", 20))
    if need_cmdline:
        steps.append(("cmdline", 15))
    if missing_pkgs:
        steps.append(("licenses", 5))
        steps.append(("packages", 55))
    if need_avd:
        steps.append(("avd", 10))

    if not steps:
        normalized.jdk_path = str(existing_jdk)
        apply_avd_settings(normalized)
        normalized.setup_complete = True
        saved = save_config(normalized)
        if progress_cb:
            progress_cb(100, "All components already installed. SimpleDroid is ready!")
        return saved

    total_weight = sum(w for _, w in steps)
    ranges: dict[str, tuple[int, int]] = {}
    cursor = 0.0
    for idx, (name, weight) in enumerate(steps):
        start_pct = int(round(cursor))
        cursor += (weight / total_weight) * 100.0
        end_pct = 100 if idx == len(steps) - 1 else int(round(cursor))
        ranges[name] = (start_pct, max(start_pct + 1, end_pct))

    if need_jdk:
        s, e = ranges["jdk"]
        jdk_home = ensure_portable_jdk(cfg=normalized, progress_cb=progress_cb, pct_start=s, pct_end=e)
    else:
        jdk_home = existing_jdk
    normalized.jdk_path = str(jdk_home)

    if need_cmdline:
        s, e = ranges["cmdline"]
        ensure_cmdline_tools(sdk_dir=sdk_dir, progress_cb=progress_cb, pct_start=s, pct_end=e)

    if missing_pkgs:
        ls, _ = ranges["licenses"]
        accept_sdk_licenses(sdk_dir=sdk_dir, cfg=normalized, progress_cb=progress_cb, pct=ls)
        ps, pe = ranges["packages"]
        install_sdk_packages(
            sdk_dir=sdk_dir,
            packages=missing_pkgs,
            cfg=normalized,
            progress_cb=progress_cb,
            pct_start=ps,
            pct_end=pe,
        )

    if need_avd:
        avs, ave = ranges["avd"]
        create_avd_if_missing(cfg=normalized, progress_cb=progress_cb, pct_start=avs, pct_end=ave)
    else:
        apply_avd_settings(normalized)

    normalized.setup_complete = True
    saved = save_config(normalized)
    if progress_cb:
        progress_cb(100, "SimpleDroid setup complete!")
    return saved
