"""Android Emulator lifecycle management, crash recovery, log capture, and boot detection."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import time
from typing import IO

import adb_tools
from config import APP_DATA_DIR, AVD_NAME, EMULATOR_LOG_FILE, LOGS_DIR, AppConfig
from sdk_manager import (
    apply_avd_settings,
    get_emulator_path,
    get_sdk_subprocess_env,
    is_avd_created,
    resolve_avd_home,
)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class EmulatorError(RuntimeError):
    """Human-friendly emulator error."""


class EmulatorController:
    """Manages the background Android Emulator process and its log file."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self._log_handle: IO[bytes] | None = None

    def is_process_running(self) -> bool:
        """Return True if our launched emulator subprocess is currently alive."""
        return self._proc is not None and self._proc.poll() is None

    def last_exit_code(self) -> int | None:
        """Return the exit code if the emulator process exited, else None."""
        if self._proc is None:
            return None
        return self._proc.poll()

    def _find_avd_pids(self) -> list[int]:
        """Return PIDs of running emulator/qemu processes for the SimpleDroid AVD."""
        try:
            res = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    (
                        "Get-CimInstance Win32_Process | "
                        "Where-Object { ($_.Name -match '^(emulator|qemu-system)') -and "
                        "($_.CommandLine -match '-avd\\s+\"?SimpleDroid\"?') } | "
                        "Select-Object -ExpandProperty ProcessId"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=CREATE_NO_WINDOW,
            )
            pids: list[int] = []
            for line in res.stdout.splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.append(int(line))
            return pids
        except Exception:
            return []

    def _wait_for_avd_exit(self, timeout_sec: float = 12.0) -> bool:
        """Wait until all SimpleDroid emulator/qemu processes have exited."""
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if not self._find_avd_pids():
                return True
            time.sleep(0.5)
        return not self._find_avd_pids()

    def _force_kill_avd_processes(self) -> None:
        """Force-terminate any hung SimpleDroid emulator/qemu processes."""
        pids = self._find_avd_pids()
        if not pids:
            return
        for pid in pids:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=5,
                    creationflags=CREATE_NO_WINDOW,
                )
            except Exception:
                pass
        time.sleep(0.5)

    def _ensure_clean_before_start(self, cfg: AppConfig) -> None:
        """Stop any leftover SimpleDroid emulator process and remove stale lock files."""
        if adb_tools.find_running_emulator_serial(cfg) or self._find_avd_pids():
            adb_tools.kill_emulator(cfg)
            if not self._wait_for_avd_exit(timeout_sec=10.0):
                self._force_kill_avd_processes()

        self._cleanup_stale_locks()

    def _cleanup_stale_locks(self) -> None:
        """Remove leftover QEMU lock files or lock directories from previous crashed sessions."""
        avd_path = resolve_avd_home() / f"{AVD_NAME}.avd"
        if not avd_path.is_dir():
            return
        for lock_entry in avd_path.rglob("*.lock"):
            try:
                if lock_entry.is_dir():
                    shutil.rmtree(lock_entry, ignore_errors=True)
                else:
                    lock_entry.unlink(missing_ok=True)
            except OSError:
                pass

    def _build_command(
        self,
        emu_exe: Path,
        cfg: AppConfig,
        *,
        wipe_data: bool = False,
        force_cold_boot: bool = False,
        force_software_gpu: bool = False,
    ) -> list[str]:
        gpu_mode = (
            "swiftshader_indirect"
            if (force_software_gpu or not cfg.gpu_acceleration)
            else "auto"
        )
        cmd = [
            str(emu_exe),
            "-avd",
            AVD_NAME,
            "-gpu",
            gpu_mode,
            "-memory",
            str(cfg.ram_gb * 1024),
            "-cores",
            str(cfg.cpu_cores),
        ]
        if cfg.low_end_mode:
            cmd.extend(["-no-boot-anim", "-no-metrics"])
        if wipe_data:
            cmd.extend(["-wipe-data", "-no-snapshot-save"])
        elif cfg.clean_boot or force_cold_boot:
            cmd.append("-no-snapshot-load")
        return cmd

    def _spawn(self, cmd: list[str], cfg: AppConfig, append_log: bool = False) -> None:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self._close_log_handle()
        mode = "ab" if append_log else "wb"
        self._log_handle = EMULATOR_LOG_FILE.open(mode)
        header = f"\n=== Starting SimpleDroid Emulator: {' '.join(cmd)} ===\n".encode("utf-8")
        self._log_handle.write(header)
        self._log_handle.flush()

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                cwd=str(APP_DATA_DIR),
                env=get_sdk_subprocess_env(cfg),
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            self._close_log_handle()
            raise EmulatorError(f"Failed to start Android Emulator: {exc}") from exc

    def start(
        self,
        cfg: AppConfig,
        wipe_data: bool = False,
        force_cold_boot: bool = False,
        force_software_gpu: bool = False,
        append_log: bool = False,
    ) -> None:
        """Start the Android Emulator in the background and capture stdout/stderr to log file."""
        if self.is_process_running():
            return

        emu_exe = get_emulator_path(cfg)
        if emu_exe is None:
            raise EmulatorError(
                "Android Emulator (emulator.exe) was not found.\n"
                "Please complete setup or check your SDK path."
            )
        if not is_avd_created():
            raise EmulatorError(
                f"Virtual device '{AVD_NAME}' was not found.\n"
                "Please re-run first-time setup."
            )

        normalized = cfg.normalized()
        self._ensure_clean_before_start(normalized)
        apply_avd_settings(normalized)

        if wipe_data:
            snapshots_dir = resolve_avd_home() / f"{AVD_NAME}.avd" / "snapshots"
            if snapshots_dir.is_dir():
                shutil.rmtree(snapshots_dir, ignore_errors=True)

        adb_tools.ensure_adb_server(normalized)
        cmd = self._build_command(
            emu_exe,
            normalized,
            wipe_data=wipe_data,
            force_cold_boot=force_cold_boot,
            force_software_gpu=force_software_gpu,
        )
        self._spawn(cmd, normalized, append_log=append_log)

    def wait_until_ready(
        self,
        cfg: AppConfig,
        timeout_sec: int = 210,
        cancel_check=None,
        wipe_data: bool = False,
    ) -> str:
        """Poll `sys.boot_completed` with automatic recovery if snapshot or GPU crashes at boot."""
        normalized = cfg.normalized()
        retried_cold = False
        retried_sw_gpu = False
        deadline = time.monotonic() + timeout_sec

        while time.monotonic() < deadline:
            if cancel_check and cancel_check():
                raise EmulatorError("Startup was cancelled.")

            exit_code = self.last_exit_code()
            if exit_code is not None:
                tail = read_log_tail(30)

                if not retried_cold and not wipe_data and not normalized.clean_boot:
                    retried_cold = True
                    default_snap = (
                        resolve_avd_home() / f"{AVD_NAME}.avd" / "snapshots" / "default_boot"
                    )
                    if default_snap.is_dir():
                        shutil.rmtree(default_snap, ignore_errors=True)
                    self.start(
                        normalized,
                        force_cold_boot=True,
                        force_software_gpu=retried_sw_gpu,
                        append_log=True,
                    )
                    deadline = time.monotonic() + timeout_sec
                    continue

                if not retried_sw_gpu and normalized.gpu_acceleration:
                    retried_sw_gpu = True
                    self.start(
                        normalized,
                        wipe_data=wipe_data,
                        force_cold_boot=True,
                        force_software_gpu=True,
                        append_log=True,
                    )
                    deadline = time.monotonic() + timeout_sec
                    continue

                hint = _friendly_crash_reason(tail)
                raise EmulatorError(
                    f"Android Emulator exited during startup (code {exit_code}).\n"
                    f"{hint}\n\nRecent log output:\n{tail}"
                )

            serial = adb_tools.find_running_emulator_serial(normalized)
            if serial and adb_tools.is_boot_completed(normalized, serial=serial):
                if normalized.low_end_mode:
                    adb_tools.apply_low_end_optimizations(normalized, serial=serial)
                return serial

            time.sleep(1.2)

        raise EmulatorError(
            "Android took too long to boot (timed out).\n"
            "Try enabling 'Phone 720p' or disabling GPU acceleration in Settings, or check Logs."
        )

    def stop(self, cfg: AppConfig, wait_sec: float = 15.0) -> None:
        """Cleanly stop the emulator via `adb emu kill` so quick-boot snapshots save properly."""
        if not self.is_process_running() and not self._find_avd_pids():
            if adb_tools.find_running_emulator_serial(cfg):
                adb_tools.kill_emulator(cfg)
            self._close_log_handle()
            return

        serial = adb_tools.find_running_emulator_serial(cfg)
        adb_tools.kill_emulator(cfg, serial=serial)

        proc = self._proc
        if proc is not None:
            try:
                proc.wait(timeout=wait_sec)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)

        if not self._wait_for_avd_exit(timeout_sec=5.0):
            self._force_kill_avd_processes()

        self._proc = None
        self._close_log_handle()

    def _close_log_handle(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None


def _friendly_crash_reason(log_text: str) -> str:
    error_lines = [
        line.lower()
        for line in log_text.splitlines()
        if any(tag in line.lower() for tag in ("error", "fatal", "failed"))
    ]
    joined = "\n".join(error_lines) if error_lines else log_text.lower()

    if "another emulator instance is running" in joined:
        return "Tip: Another emulator process was still closing. Try clicking Start Android again."
    if "whpx" in joined or "hax" in joined or "hypervisor" in joined or "virtualization" in joined:
        return "Tip: Check that Windows Hypervisor Platform (WHPX) and BIOS virtualization are enabled."
    if "not enough space" in joined or "no space left" in joined:
        return "Tip: Free up disk space on your C: drive."
    if "memory" in joined or "ram" in joined:
        return "Tip: Try lowering RAM to 2 GB in Settings."
    if "opengl" in joined or "vulkan" in joined or "gpu" in joined:
        return "Tip: Try disabling hardware GPU acceleration in Settings."
    return "Tip: Try 'Clean boot' or 'Phone 720p' in Settings, or check the Logs tab."


def read_log_tail(max_lines: int = 200) -> str:
    """Read the last N lines of the emulator log file."""
    if not EMULATOR_LOG_FILE.is_file():
        return "(No emulator logs yet.)"
    try:
        text = EMULATOR_LOG_FILE.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        return "\n".join(lines[-max_lines:]) if lines else "(Log file is empty.)"
    except OSError as exc:
        return f"(Could not read log file: {exc})"
