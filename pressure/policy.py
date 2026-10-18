"""When a beacon is allowed to take the cuff's only connection.

btmon on one A&D UA-810, 18 October 2026: the company-105 payload rides on the
cuff's own packets and the Apple iBeacon on separate non-connectable ones. BlueZ
keeps the last value of each, so a callback can pair a fresh field with a stale one.
- ``01…`` is the START burst, ADV_SCAN_IND: never connectable.
- ``02…`` with the iBeacon tail a1030001c3 is the 60-s window while the Bluetooth
  light flashes, ADV_IND: the only connectable advert. The fourth byte (42 or 64)
  varies and is ignored.
- The tail 32010040c3 is the idle iBeacon, ADV_NONCONN_IND.
"""

from __future__ import annotations

BP_SERVICE_PREFIX = "00001810"
AAND_D_COMPANY = 105
APPLE_COMPANY = 76
CONNECT_TIMEOUT_SECONDS = 6.0
TRANSFER_TAIL = "a1030001c3"
IDLE_TAIL = "32010040c3"


def _hex(mfg: dict, company: int) -> str:
    value = mfg.get(company, mfg.get(str(company), b""))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value or "").lower()


def beacon_class(mfg: dict | None) -> str:
    """Classify one advertisement. Unknown shapes stay ``generic``."""
    payload = _hex(mfg or {}, AAND_D_COMPANY)
    tail = _hex(mfg or {}, APPLE_COMPANY)[-10:]
    if payload.startswith("02") and tail == TRANSFER_TAIL:
        return "transfer"
    if tail == IDLE_TAIL:
        return "idle"
    if payload.startswith("01"):
        return "start"
    if payload or tail == TRANSFER_TAIL:
        return "aandd-other"
    return "generic"


def advertises_blood_pressure(service_uuids: list[str] | None) -> bool:
    for uuid in service_uuids or []:
        compact = uuid.lower().replace("-", "")
        if compact.startswith(BP_SERVICE_PREFIX):
            return True
    return False


def should_connect(service_uuids: list[str] | None, mfg: dict | None) -> bool:
    """True only for the transfer window, or a generic Blood Pressure advert.

    A generic advert has the Blood Pressure service and no company-105 payload.
    """
    kind = beacon_class(mfg)
    if kind == "transfer":
        return True
    if kind != "generic":
        return False
    if _hex(mfg or {}, AAND_D_COMPANY):
        return False
    return advertises_blood_pressure(service_uuids)


def arms_session(kind: str) -> bool:
    """A trusted cuff's START burst or transfer window means stay ready."""
    return kind in {"start", "transfer"}


def should_drain(service_uuids: list[str] | None, mfg: dict | None, trusted_beacon: str) -> bool:
    """True when an already-trusted monitor may take the only link.

    A cuff trusted from the transfer beacon stays on that pair. A later
    advert with no company-105 payload does not stop discovery, even when
    it carries the Blood Pressure service. A monitor trusted as generic
    connects only on a generic advert.
    """
    if not should_connect(service_uuids, mfg):
        return False
    kind = beacon_class(mfg)
    if trusted_beacon == "generic":
        return kind == "generic"
    return kind == "transfer"
