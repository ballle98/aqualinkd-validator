from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..interfaces import ArtifactStore


@dataclass(frozen=True)
class FileState:
    exists: bool
    inode: int | None = None
    size: int | None = None
    mtime_ns: int | None = None


@dataclass(frozen=True)
class SupplementalLogSpec:
    name: str
    source: Path
    artifact: str
    fidelity: str
    limitations: tuple[str, ...]


class SupplementalSerialLogTracker:
    """Retain enabled fixed-path protocol logs only when this run changed them."""

    def __init__(
        self,
        specs: tuple[SupplementalLogSpec, ...],
        *,
        artifacts: ArtifactStore,
    ) -> None:
        self._specs = specs
        self._artifacts = artifacts
        self._before = {spec.name: file_state(spec.source) for spec in specs}

    def snapshot(self) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        for spec in self._specs:
            before = self._before[spec.name]
            after = file_state(spec.source)
            record: dict[str, Any] = {
                "name": spec.name,
                "source": str(spec.source),
                "artifact": None,
                "fidelity": spec.fidelity,
                "limitations": list(spec.limitations),
                "before": asdict(before),
                "after": asdict(after),
            }
            if not after.exists:
                record["status"] = "missing"
            elif after == before:
                record["status"] = "unchanged_not_captured"
            else:
                digest, byte_count = self._copy_and_hash(spec)
                record.update(
                    {
                        "status": "captured",
                        "artifact": spec.artifact,
                        "sha256": digest,
                        "byte_count": byte_count,
                    }
                )
            records.append(record)
        return {
            "requested": bool(self._specs),
            "files": records,
        }

    def _copy_and_hash(self, spec: SupplementalLogSpec) -> tuple[str, int]:
        digest = hashlib.sha256()
        byte_count = 0
        with spec.source.open("rb") as source, self._artifacts.open_binary(
            spec.artifact
        ) as destination:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                destination.write(chunk)
                digest.update(chunk)
                byte_count += len(chunk)
        return digest.hexdigest(), byte_count


def file_state(path: Path) -> FileState:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return FileState(False)
    return FileState(
        True,
        inode=stat.st_ino,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


PACKET_LOG_SPEC = SupplementalLogSpec(
    name="aqualinkd_packet_log",
    source=Path("/tmp/RS485.log"),
    artifact="RS485.log",
    fidelity="aqualinkd_logged_logical_packets",
    limitations=(
        "no packet timestamps",
        "buffered file output is not the canonical timing source",
        "may omit noise, discarded bytes, and transmit padding",
    ),
)

RAW_READ_LOG_SPEC = SupplementalLogSpec(
    name="aqualinkd_raw_read_log",
    source=Path("/tmp/RS485raw.log"),
    artifact="RS485raw.log",
    fidelity="aqualinkd_received_raw_bytes",
    limitations=(
        "no timestamps",
        "receive direction only",
        "does not contain AqualinkD transmissions",
    ),
)
