from __future__ import annotations

import hashlib
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

_ASSIGNMENT = re.compile(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$")


class ConfigurationError(ValueError):
    """Raised when an AqualinkD configuration is unsafe or invalid."""


def read_config_value(path: Path, key: str) -> str | None:
    """Return the last active assignment for *key* in an AqualinkD config."""
    result: str | None = None
    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            match = _ASSIGNMENT.match(raw_line)
            if match is None or match.group(1) != key:
                continue
            result = _unquote(match.group(2).strip())
    return result


def validate_live_serial_device(
    config_path: Path,
    requested_device: Path | None = None,
) -> Path:
    """Resolve the configured serial device and validate any explicit override."""
    configured = read_config_value(config_path, "serial_port")
    if configured is None:
        raise ConfigurationError(
            f"{config_path} does not contain an active serial_port assignment"
        )

    configured_path = Path(configured).expanduser().resolve(strict=False)
    requested_path = (
        requested_device.expanduser().resolve(strict=False)
        if requested_device is not None
        else configured_path
    )
    if requested_device is not None and configured_path != requested_path:
        raise ConfigurationError(
            "Explicit serial device does not match the configuration: "
            f"requested {requested_path}, configured {configured_path}"
        )

    try:
        mode = requested_path.stat().st_mode
    except FileNotFoundError as error:
        raise ConfigurationError(
            f"Serial device does not exist: {requested_path}"
        ) from error
    if not stat.S_ISCHR(mode):
        raise ConfigurationError(
            f"Live serial endpoint is not a character device: {requested_path}"
        )
    return requested_path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_api_base_url(
    config_path: Path,
    requested_url: str | None,
) -> str:
    value = requested_url or read_config_value(config_path, "listen_address")
    if value is None:
        raise ConfigurationError(
            "No API URL was supplied and the configuration has no active "
            "listen_address"
        )
    return normalize_api_base_url(value)


def normalize_api_base_url(value: str) -> str:
    """Normalize an AqualinkD listener URL for local API access."""
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname is None:
        raise ConfigurationError(f"Invalid AqualinkD API URL: {value}")
    hostname = (
        "127.0.0.1"
        if parsed.hostname in {"0.0.0.0", "::", "[::]"}
        else parsed.hostname
    )
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{hostname}:{parsed.port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
