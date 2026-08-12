from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from typing import Any

from .config import sha256_file


def collect_host_metadata() -> dict[str, Any]:
    return {
        "architecture": platform.machine(),
        "clock_ticks_per_second": os.sysconf("SC_CLK_TCK"),
        "container": _container_kind(),
        "cpu_count": os.cpu_count(),
        "cpu_governor": _read_optional(
            Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
        ),
        "cpu_model": _cpu_model(),
        "kernel": platform.release(),
        "load_average": list(os.getloadavg()),
        "os": _os_release(),
        "page_size_bytes": os.sysconf("SC_PAGE_SIZE"),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "temperature_celsius": _temperature_celsius(),
    }


def collect_binary_metadata(binary: Path) -> dict[str, Any]:
    resolved = binary.expanduser().resolve(strict=True)
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def collect_source_metadata(
    source_tree: Path | None,
    *,
    source_commit: str | None = None,
    source_branch: str | None = None,
) -> dict[str, Any] | None:
    if source_tree is None and source_commit is None and source_branch is None:
        return None

    resolved = (
        source_tree.expanduser().resolve(strict=True)
        if source_tree is not None
        else None
    )
    metadata: dict[str, Any] = {
        "path": str(resolved) if resolved is not None else None,
        "commit": source_commit,
        "branch": source_branch,
        "dirty": None,
    }
    if resolved is None or (source_commit is not None and source_branch is not None):
        return metadata
    try:
        metadata["commit"] = source_commit or _git(resolved, "rev-parse", "HEAD")
        metadata["branch"] = source_branch or _git(resolved, "branch", "--show-current")
        metadata["dirty"] = bool(_git(resolved, "status", "--porcelain"))
    except subprocess.CalledProcessError as error:
        metadata["git_error"] = error.stderr.strip() or "git metadata unavailable"
    return metadata


def _git(worktree: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout.strip()


def _container_kind() -> str | None:
    if Path("/.dockerenv").exists():
        return "docker"
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    for name in ("podman", "docker", "containerd", "lxc"):
        if name in cgroup:
            return name
    return None


def _os_release() -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = Path("/etc/os-release").read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.lower()] = value.strip().strip('"')
    return result


def _cpu_model() -> str | None:
    try:
        lines = Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for key_name in ("model name", "Model", "Hardware"):
        prefix = f"{key_name}\t:"
        for line in lines:
            if line.startswith(prefix):
                return line.split(":", 1)[1].strip()
    return None


def _temperature_celsius() -> float | None:
    raw = _read_optional(Path("/sys/class/thermal/thermal_zone0/temp"))
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value / 1000.0 if value > 1000 else value


def _read_optional(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
