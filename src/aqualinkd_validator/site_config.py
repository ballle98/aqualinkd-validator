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
class SiteConfig:
    source: Path | None = None
    spa: SpaSiteConfig = SpaSiteConfig()


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
    _only_keys(raw, {"schema", "spa"}, str(path))
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
    return SiteConfig(source=path, spa=SpaSiteConfig(fill_seconds=fill_seconds))


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
