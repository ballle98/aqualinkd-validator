from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import pwd
import secrets
import select
import shutil
import socket
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import IO, Literal

from ..site_config import PowerCenterSiteConfig
from .power_center import PowerCenterAutomationError

PowerCenterDisplayMode = Literal["headless", "desktop"]


class ManagedWinePowerCenterSession:
    """Own an isolated display and the emulator processes for one run."""

    def __init__(
        self,
        config: PowerCenterSiteConfig,
        *,
        display_mode: PowerCenterDisplayMode = "headless",
    ) -> None:
        self._config = config
        self._display_mode = display_mode
        self._environment = {**os.environ, "WINEPREFIX": str(config.wine_prefix)}
        self._lock_handle: IO[str] | None = None
        self._display_lock_handle: IO[str] | None = None
        self._xauthority: Path | None = None
        self._xvfb: subprocess.Popen[str] | None = None
        self._applications: list[subprocess.Popen[str]] = []
        self._started = False

    @property
    def environment(self) -> dict[str, str]:
        return self._environment.copy()

    @property
    def metadata(self) -> dict[str, object]:
        return {
            "managed": True,
            "display_mode": self._display_mode,
            "display": self._environment.get("DISPLAY"),
            "power_center_executable": str(self._power_center_executable()),
            "simio_executable": str(self._simio_executable()),
        }

    def start(self) -> None:
        if self._started:
            return
        try:
            self._acquire_prefix_lock()
            if self._display_mode == "headless":
                self._start_xvfb()
            elif not self._environment.get("DISPLAY"):
                raise PowerCenterAutomationError(
                    "desktop Power Center display requires DISPLAY to be set"
                )
            self._launch(self._simio_executable())
            self._launch(self._power_center_executable())
            self._wait_until_ready()
            self._started = True
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._applications:
            self._stop_wine_prefix()
        for process in reversed(self._applications):
            self._stop_process(process)
        self._applications.clear()
        if self._xvfb is not None:
            self._stop_process(self._xvfb)
            self._xvfb = None
        if self._xauthority is not None:
            self._xauthority.unlink(missing_ok=True)
            self._xauthority = None
        if self._display_lock_handle is not None:
            fcntl.flock(self._display_lock_handle, fcntl.LOCK_UN)
            self._display_lock_handle.close()
            self._display_lock_handle = None
        if self._lock_handle is not None:
            fcntl.flock(self._lock_handle, fcntl.LOCK_UN)
            self._lock_handle.close()
            self._lock_handle = None
        self._started = False

    def _power_center_executable(self) -> Path:
        configured = self._config.power_center_executable
        executable = configured or self._installation() / "Pwrcntr.exe"
        return self._require_executable(executable)

    def _simio_executable(self) -> Path:
        configured = self._config.simio_executable
        executable = configured or self._installation() / "Simio.exe"
        return self._require_executable(executable)

    def _installation(self) -> Path:
        return self._config.wine_prefix / "drive_c" / "Program Files" / "Alwin32"

    @staticmethod
    def _require_executable(path: Path) -> Path:
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise PowerCenterAutomationError(
                f"Power Center executable does not exist: {path}"
            ) from error
        if not resolved.is_file():
            raise PowerCenterAutomationError(
                f"Power Center executable is not a file: {resolved}"
            )
        return resolved

    def _acquire_prefix_lock(self) -> None:
        digest = hashlib.sha256(
            str(self._config.wine_prefix).encode("utf-8")
        ).hexdigest()[:16]
        path = Path("/tmp") / f"aqualinkd-validator-wine-{digest}.lock"
        handle = self._open_shared_lock(path)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.close()
            raise PowerCenterAutomationError(
                "Power Center Wine prefix is already in use by another validator "
                f"run: {self._config.wine_prefix}; parallel runs require separate "
                "Wine prefixes and serial links"
            ) from error
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        self._lock_handle = handle

    def _open_shared_lock(self, path: Path) -> IO[str]:
        flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            try:
                descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                descriptor = os.open(path, flags)
        try:
            if os.geteuid() == 0:
                prefix = self._config.wine_prefix.stat()
                os.fchown(descriptor, prefix.st_uid, prefix.st_gid)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise PowerCenterAutomationError(
                    f"Power Center lock path is not a regular file: {path}"
                )
            return os.fdopen(descriptor, "r+", encoding="utf-8")
        except Exception:
            os.close(descriptor)
            raise

    def _start_xvfb(self) -> None:
        executable = shutil.which("Xvfb")
        if executable is None:
            raise PowerCenterAutomationError(
                "headless Power Center display requires Xvfb"
            )
        if self._x11_socket_directory_is_usable():
            self._start_unix_xvfb(executable)
        else:
            self._start_tcp_xvfb(executable)

    def _start_unix_xvfb(self, executable: str) -> None:
        arguments = [
            executable,
            "-displayfd",
            "1",
            "-screen",
            "0",
            "1280x800x24",
            "-nolisten",
            "tcp",
            "-ac",
        ]
        process = subprocess.Popen(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self._xvfb = process
        assert process.stdout is not None
        deadline = time.monotonic() + self._config.startup_timeout_seconds
        display_number = ""
        while time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], 0.1)
            if ready:
                display_number = process.stdout.readline().strip()
                break
            if process.poll() is not None:
                break
        if not display_number.isdecimal():
            detail = ""
            self._stop_process(process)
            if process.stderr is not None:
                detail = process.stderr.read().strip()[-2000:]
            raise PowerCenterAutomationError(
                f"Xvfb failed to allocate a display: {detail or display_number}"
            )
        self._environment["DISPLAY"] = f":{display_number}"

    @staticmethod
    def _x11_socket_directory_is_usable() -> bool:
        socket_directory = Path("/tmp/.X11-unix")
        if not socket_directory.exists():
            return True
        mode = stat.S_IMODE(socket_directory.stat().st_mode)
        return bool(mode & stat.S_ISVTX)

    def _start_tcp_xvfb(self, executable: str) -> None:
        display_number = self._allocate_tcp_display()
        descriptor, raw_path = tempfile.mkstemp(
            prefix="aqualinkd-validator-xauth-",
            dir="/tmp",
        )
        os.close(descriptor)
        xauthority = Path(raw_path)
        self._xauthority = xauthority
        os.chmod(xauthority, 0o600)
        cookie = secrets.token_hex(16)
        xauth = shutil.which("xauth")
        if xauth is None:
            raise PowerCenterAutomationError(
                "authenticated Xvfb TCP fallback requires xauth"
            )
        authorization = subprocess.run(
            [
                xauth,
                "-f",
                str(xauthority),
                "add",
                f"127.0.0.1:{display_number}",
                "MIT-MAGIC-COOKIE-1",
                cookie,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if authorization.returncode != 0:
            raise PowerCenterAutomationError(
                "could not create Xvfb authorization: "
                f"{authorization.stderr.strip()}"
            )
        prefix = self._config.wine_prefix.stat()
        if os.geteuid() == 0 and prefix.st_uid != 0:
            os.chown(xauthority, prefix.st_uid, prefix.st_gid)
        self._environment["DISPLAY"] = f"127.0.0.1:{display_number}"
        self._environment["XAUTHORITY"] = str(xauthority)
        process = subprocess.Popen(
            [
                executable,
                f":{display_number}",
                "-screen",
                "0",
                "1280x800x24",
                "-nolisten",
                "unix",
                "-listen",
                "tcp",
                "-auth",
                str(xauthority),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self._xvfb = process
        deadline = time.monotonic() + self._config.startup_timeout_seconds
        port = 6000 + display_number
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.05)
        self._stop_process(process)
        detail = ""
        if process.stderr is not None:
            detail = process.stderr.read().strip()[-2000:]
        raise PowerCenterAutomationError(
            f"Xvfb TCP fallback failed to start: {detail or 'no diagnostic'}"
        )

    def _allocate_tcp_display(self) -> int:
        for display_number in range(100, 1000):
            lock_path = Path("/tmp") / (
                f"aqualinkd-validator-xdisplay-{display_number}.lock"
            )
            handle = self._open_shared_lock(lock_path)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                    probe.bind(("127.0.0.1", 6000 + display_number))
            except OSError:
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()
                continue
            self._display_lock_handle = handle
            return display_number
        raise PowerCenterAutomationError(
            "could not allocate an isolated Xvfb TCP display"
        )

    def _launch(self, executable: Path) -> None:
        command = self._wine_command(self._config.wine, str(executable))
        try:
            process = subprocess.Popen(
                command,
                cwd=executable.parent,
                env=self._environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
        except OSError as error:
            raise PowerCenterAutomationError(
                f"could not launch {executable.name}: {error}"
            ) from error
        self._applications.append(process)

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + self._config.startup_timeout_seconds
        diagnostics = "windows not found"
        while time.monotonic() < deadline:
            failed = [
                process.returncode
                for process in self._applications
                if process.poll() not in (None, 0)
            ]
            if failed:
                raise PowerCenterAutomationError(
                    f"Power Center emulator exited during startup: {failed}"
                )
            status = self._run_helper("status")
            io_status = self._run_helper("io-status")
            if status.returncode == 0 and io_status.returncode == 0:
                return
            diagnostics = "; ".join(
                detail
                for detail in (
                    status.stderr.strip(),
                    io_status.stderr.strip(),
                )
                if detail
            ) or diagnostics
            time.sleep(0.25)
        raise PowerCenterAutomationError(
            "Power Center emulator windows were not ready within "
            f"{self._config.startup_timeout_seconds:g}s: {diagnostics}"
        )

    def _run_helper(self, argument: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                self._wine_command(
                    self._config.wine,
                    str(self._config.helper),
                    argument,
                ),
                check=False,
                capture_output=True,
                text=True,
                env=self._environment,
                timeout=self._config.command_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return subprocess.CompletedProcess([], 1, "", str(error))

    def _stop_wine_prefix(self) -> None:
        wineserver = shutil.which("wineserver")
        if wineserver is None:
            return
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                self._wine_command(wineserver, "-k"),
                check=False,
                capture_output=True,
                text=True,
                env=self._environment,
                timeout=5,
            )

    @staticmethod
    def _stop_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _wine_command(self, *arguments: str) -> list[str]:
        prefix = self._config.wine_prefix.stat()
        owner = prefix.st_uid
        if os.geteuid() != 0 or owner == 0:
            return list(arguments)
        try:
            user = pwd.getpwuid(owner).pw_name
        except KeyError as error:
            raise PowerCenterAutomationError(
                f"Wine prefix owner UID {owner} has no local account"
            ) from error
        environment = [
            f"WINEPREFIX={self._config.wine_prefix}",
            f"DISPLAY={self._environment.get('DISPLAY', '')}",
        ]
        if "XAUTHORITY" in self._environment:
            environment.append(f"XAUTHORITY={self._environment['XAUTHORITY']}")
        return [
            "runuser",
            "--user",
            user,
            "--",
            "env",
            *environment,
            *arguments,
        ]
