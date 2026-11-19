"""One advert as the decisions see it, whether the active scan or passive listening heard it."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Sighting:
    address: str
    name: str = ""
    manufacturer: dict = field(default_factory=dict)
    uuids: tuple = ()


def from_bleak(device, advertisement) -> Sighting:
    """From bleak's detection callback."""
    return Sighting(
        address=str(device.address),
        name=advertisement.local_name or device.name or "",
        manufacturer={int(key): bytes(value) for key, value in (advertisement.manufacturer_data or {}).items()},
        uuids=tuple(str(item) for item in (advertisement.service_uuids or [])),
    )


def from_device1(properties: dict | None) -> Sighting | None:
    """From BlueZ's Device1 properties, read after a monitor match. BlueZ keeps the last value per company,
    so a read during the window holds the company-105 payload and the Apple tail together."""
    if not properties or not properties.get("Address"):
        return None
    manufacturer = {
        int(key): bytes(getattr(value, "value", value)) for key, value in (properties.get("ManufacturerData") or {}).items()
    }
    return Sighting(
        address=str(properties["Address"]),
        name=str(properties.get("Name") or ""),
        manufacturer=manufacturer,
        uuids=tuple(str(item) for item in (properties.get("UUIDs") or [])),
    )
