from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml  # type: ignore[import-untyped]

_DURATION = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>ms|s|m)$")
_DURATION_FACTORS = {"ms": 0.001, "s": 1.0, "m": 60.0}
DEFAULT_SITE_CONFIG_NAME = "aqualinkd-validator.yaml"


class SiteConfigError(ValueError):
    """Raised when installation-specific validator configuration is invalid."""


@dataclass(frozen=True)
class SpaSiteConfig:
    fill_seconds: float | None = None


@dataclass(frozen=True)
class PowerCenterSiteConfig:
    helper: Path
    wine_prefix: Path
    model: str
    port: str
    wine: str = "wine"
    command_timeout_seconds: float = 10.0
    power_timeout_seconds: float = 8.0
    observation_seconds: float = 0.75


@dataclass(frozen=True)
class SiteConfig:
    source: Path | None = None
    spa: SpaSiteConfig = SpaSiteConfig()
    power_center: PowerCenterSiteConfig | None = None


def load_site_config(
    aqualinkd_config: Path,
    requested_path: Path | None = None,
) -> SiteConfig:
    """Load an explicit or adjacent installation-specific validator profile.

    An explicit path must exist. Without one, the profile is optional and is
    discovered beside ``aqualinkd.conf`` as ``aqualinkd-validator.yaml``.
    """

    path = (
        requested_path.expanduser().resolve(strict=True)
        if requested_path is not None
        else aqualinkd_config.parent / DEFAULT_SITE_CONFIG_NAME
    )
    if requested_path is None and not path.is_file():
        return SiteConfig()

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise SiteConfigError(f"Unable to read site config {path}: {error}") from error
    if not isinstance(raw, dict):
        raise SiteConfigError(f"{path}: root must be a mapping")
    _only_keys(raw, {"schema", "spa", "power_center"}, str(path))
    if raw.get("schema") != 1:
        raise SiteConfigError(f"{path}: schema must be 1")

    spa_raw = raw.get("spa", {})
    if not isinstance(spa_raw, dict):
        raise SiteConfigError(f"{path}: spa must be a mapping")
    _only_keys(spa_raw, {"fill_time"}, f"{path}: spa")
    fill_seconds = (
        _duration_seconds(spa_raw["fill_time"], f"{path}: spa.fill_time")
        if "fill_time" in spa_raw
        else None
    )
    power_center = _power_center_config(raw.get("power_center"), path)
    return SiteConfig(
        source=path,
        spa=SpaSiteConfig(fill_seconds=fill_seconds),
        power_center=power_center,
    )


def _power_center_config(value: object, source: Path) -> PowerCenterSiteConfig | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SiteConfigError(f"{source}: power_center must be a mapping")
    location = f"{source}: power_center"
    _only_keys(
        value,
        {
            "helper",
            "wine_prefix",
            "model",
            "port",
            "wine",
            "command_timeout",
            "power_timeout",
            "observation_time",
        },
        location,
    )
    missing = [
        key for key in ("helper", "wine_prefix", "model", "port") if key not in value
    ]
    if missing:
        raise SiteConfigError(f"{location}: missing {', '.join(missing)}")
    helper = _config_path(value["helper"], source, f"{location}.helper", file=True)
    wine_prefix = _config_path(
        value["wine_prefix"], source, f"{location}.wine_prefix", file=False
    )
    model = _nonempty_string(value["model"], f"{location}.model")
    port = _nonempty_string(value["port"], f"{location}.port")
    wine = _nonempty_string(value.get("wine", "wine"), f"{location}.wine")
    return PowerCenterSiteConfig(
        helper=helper,
        wine_prefix=wine_prefix,
        model=model,
        port=port,
        wine=wine,
        command_timeout_seconds=_optional_duration(
            value, "command_timeout", 10.0, location
        ),
        power_timeout_seconds=_optional_duration(value, "power_timeout", 8.0, location),
        observation_seconds=_optional_duration(
            value, "observation_time", 0.75, location
        ),
    )


def _only_keys(value: dict[object, object], allowed: set[str], path: str) -> None:
    unexpected = sorted(str(key) for key in value if key not in allowed)
    if unexpected:
        raise SiteConfigError(f"{path}: unexpected key(s): {', '.join(unexpected)}")


def _duration_seconds(value: object, path: str) -> float:
    if not isinstance(value, str):
        raise SiteConfigError(f"{path} must be a duration such as 8m")
    match = _DURATION.fullmatch(value)
    if match is None:
        raise SiteConfigError(f"{path} must be a duration such as 8m")
    seconds = float(match.group("value")) * _DURATION_FACTORS[match.group("unit")]
    if seconds <= 0:
        raise SiteConfigError(f"{path} must be greater than zero")
    return seconds


def _optional_duration(
    value: dict[object, object], key: str, default: float, path: str
) -> float:
    return _duration_seconds(value[key], f"{path}.{key}") if key in value else default


def _nonempty_string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SiteConfigError(f"{path} must be a non-empty string")
    return value.strip()


def _config_path(value: object, source: Path, path: str, *, file: bool) -> Path:
    raw = _nonempty_string(value, path)
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = source.parent / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise SiteConfigError(f"{path} does not exist: {candidate}") from error
    if file and not resolved.is_file():
        raise SiteConfigError(f"{path} must be a file: {resolved}")
    if not file and not resolved.is_dir():
        raise SiteConfigError(f"{path} must be a directory: {resolved}")
    return resolved
