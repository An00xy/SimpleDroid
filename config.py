"""Configuration paths, hardware-aware presets, asset helpers, and persistent settings."""

from __future__ import annotations

import ctypes
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import sys
from typing import Any

APP_NAME = "SimpleDroid"
AVD_NAME = "SimpleDroid"
AVD_DEVICE_PROFILE = "pixel_5"

ANDROID_VERSIONS: dict[str, dict[str, Any]] = {
    "android-35": {
        "label": "Android 15 (Latest)",
        "package": "system-images;android-35;google_apis;x86_64",
        "rel_parts": ("system-images", "android-35", "google_apis", "x86_64"),
    },
    "android-34": {
        "label": "Android 14 (Balanced)",
        "package": "system-images;android-34;google_apis;x86_64",
        "rel_parts": ("system-images", "android-34", "google_apis", "x86_64"),
    },
    "android-36": {
        "label": "Android 16",
        "package": "system-images;android-36;google_apis;x86_64",
        "rel_parts": ("system-images", "android-36", "google_apis", "x86_64"),
    },
    "android-37.0": {
        "label": "Android 17 (Newest)",
        "package": "system-images;android-37.0;google_apis;x86_64",
        "rel_parts": ("system-images", "android-37.0", "google_apis", "x86_64"),
    },
    "android-30": {
        "label": "Android 11 (Ultra-Light for Low-End PCs)",
        "package": "system-images;android-30;google_apis;x86_64",
        "rel_parts": ("system-images", "android-30", "google_apis", "x86_64"),
    },
}

DEFAULT_ANDROID_VERSION = "android-35"

# Official Google Android SDK repository index (used to resolve latest cmdline-tools ZIP)
ANDROID_REPO_XML_URL = "https://dl.google.com/android/repository/repository2-1.xml"
ANDROID_REPO_BASE_URL = "https://dl.google.com/android/repository/"

# Official Eclipse Temurin (Adoptium) JDK 17 portable ZIP endpoint for Windows x64
TEMURIN_JDK17_ZIP_URL = (
    "https://api.adoptium.net/v3/binary/latest/17/ga/windows/x64/jdk/hotspot/normal/eclipse"
)

RAM_OPTIONS_GB = (2, 3, 4, 6, 8)

RESOLUTION_PRESETS: dict[str, dict[str, Any]] = {
    "phone_low": {
        "label": "Phone 540p (540 × 960 — Ultra-Light)",
        "width": 540,
        "height": 960,
        "density": 240,
    },
    "phone_720p": {
        "label": "Phone 720p (720 × 1280 — Low-End Smooth)",
        "width": 720,
        "height": 1280,
        "density": 320,
    },
    "phone_portrait": {
        "label": "Phone Portrait (1080 × 2340)",
        "width": 1080,
        "height": 2340,
        "density": 440,
    },
    "tablet_landscape": {
        "label": "Tablet Landscape (1920 × 1200)",
        "width": 1920,
        "height": 1200,
        "density": 240,
    },
}


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def get_host_ram_gb() -> float:
    """Return total physical system RAM in GiB."""
    try:
        stat = _MemoryStatusEx()
        stat.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return stat.ullTotalPhys / (1024 ** 3)
    except Exception:
        pass
    return 16.0


def is_low_end_host() -> bool:
    """Return True if host PC has <= 9 GB RAM or <= 4 logical CPU cores."""
    return get_host_ram_gb() <= 9.0 or (os.cpu_count() or 4) <= 4


def recommended_hardware_defaults() -> tuple[int, int, str]:
    """Return (ram_gb, cpu_cores, resolution_preset) tuned for the host hardware."""
    host_ram = get_host_ram_gb()
    host_cores = os.cpu_count() or 4

    if host_ram <= 9.0:
        ram_gb = 2
        res_preset = "phone_720p"
    elif host_ram <= 14.0:
        ram_gb = 3
        res_preset = "phone_720p"
    else:
        ram_gb = 4
        res_preset = "phone_portrait"

    if host_cores <= 2:
        cpu_cores = 1
    elif host_cores <= 4:
        cpu_cores = 2
    else:
        cpu_cores = min(4, max(2, host_cores // 2))

    return ram_gb, cpu_cores, res_preset


def get_asset_path(relative_path: str) -> Path:
    """Resolve an asset path both when running from source and in a PyInstaller bundle."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    candidate = base / relative_path
    if candidate.exists():
        return candidate
    return Path(__file__).resolve().parent / relative_path


def get_app_data_dir() -> Path:
    """Return %LOCALAPPDATA%\\SimpleDroid (falling back to home dir if needed)."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_NAME
    return Path.home() / "AppData" / "Local" / APP_NAME


APP_DATA_DIR = get_app_data_dir()
DEFAULT_SDK_DIR = APP_DATA_DIR / "sdk"
DEFAULT_JDK_DIR = APP_DATA_DIR / "jdk"
AVD_DIR = APP_DATA_DIR / "avd"
LOGS_DIR = APP_DATA_DIR / "logs"
SCREENSHOTS_DIR = APP_DATA_DIR / "screenshots"
CONFIG_FILE = APP_DATA_DIR / "config.json"
EMULATOR_LOG_FILE = LOGS_DIR / "emulator.log"


@dataclass
class AppConfig:
    setup_complete: bool = False
    sdk_path: str = ""
    jdk_path: str = ""
    android_version: str = DEFAULT_ANDROID_VERSION
    ram_gb: int = 4
    cpu_cores: int = 4
    resolution_preset: str = "phone_portrait"
    gpu_acceleration: bool = True
    clean_boot: bool = False
    low_end_mode: bool = True

    def normalized(self) -> AppConfig:
        max_cores = max(1, os.cpu_count() or 4)
        ram_gb = self.ram_gb if self.ram_gb in RAM_OPTIONS_GB else 4
        cpu_cores = max(1, min(int(self.cpu_cores), max_cores))
        preset = (
            self.resolution_preset
            if self.resolution_preset in RESOLUTION_PRESETS
            else "phone_portrait"
        )
        android_version = (
            self.android_version
            if self.android_version in ANDROID_VERSIONS
            else DEFAULT_ANDROID_VERSION
        )
        return AppConfig(
            setup_complete=bool(self.setup_complete),
            sdk_path=str(self.sdk_path or "").strip(),
            jdk_path=str(self.jdk_path or "").strip(),
            android_version=android_version,
            ram_gb=ram_gb,
            cpu_cores=cpu_cores,
            resolution_preset=preset,
            gpu_acceleration=bool(self.gpu_acceleration),
            clean_boot=bool(self.clean_boot),
            low_end_mode=bool(self.low_end_mode),
        )

    def system_image_package(self) -> str:
        ver = self.android_version if self.android_version in ANDROID_VERSIONS else DEFAULT_ANDROID_VERSION
        return str(ANDROID_VERSIONS[ver]["package"])

    def android_label(self) -> str:
        ver = self.android_version if self.android_version in ANDROID_VERSIONS else DEFAULT_ANDROID_VERSION
        return str(ANDROID_VERSIONS[ver]["label"])


def default_config_for_host() -> AppConfig:
    """Create initial AppConfig tuned for this PC's RAM and CPU."""
    ram_gb, cpu_cores, res_preset = recommended_hardware_defaults()
    return AppConfig(
        ram_gb=ram_gb,
        cpu_cores=cpu_cores,
        resolution_preset=res_preset,
        low_end_mode=True,
    ).normalized()


def ensure_app_dirs() -> None:
    """Create core SimpleDroid data directories (without creating unused sdk/jdk folders)."""
    for path in (APP_DATA_DIR, AVD_DIR, LOGS_DIR, SCREENSHOTS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def load_config() -> AppConfig:
    """Load persisted configuration from config.json, returning host-tuned defaults if missing."""
    ensure_app_dirs()
    if not CONFIG_FILE.exists():
        return default_config_for_host()
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return default_config_for_host()
        known = {k: raw[k] for k in AppConfig.__dataclass_fields__ if k in raw}
        return AppConfig(**known).normalized()
    except (OSError, ValueError, TypeError):
        return default_config_for_host()


def save_config(cfg: AppConfig) -> AppConfig:
    """Persist normalized configuration to config.json."""
    ensure_app_dirs()
    normalized = cfg.normalized()
    CONFIG_FILE.write_text(
        json.dumps(asdict(normalized), indent=2),
        encoding="utf-8",
    )
    return normalized
