from __future__ import annotations

from typing import Any, Protocol

from ..domain import EquipmentSnapshot


class AqualinkApi(Protocol):
    """Northbound AqualinkD operations required by validation keywords."""

    @property
    def base_url(self) -> str: ...

    async def devices(self) -> EquipmentSnapshot: ...

    async def status(self) -> dict[str, Any]: ...

    async def set_device(self, identifier: str, enabled: bool) -> None: ...

    async def set_setpoint(self, identifier: str, value: int) -> None: ...
