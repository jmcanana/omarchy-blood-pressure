"""Blood Pressure Profile parsers.

The layout is the Bluetooth SIG Blood Pressure Measurement characteristic.
A vendor indication is accepted only when it carries that same layout, with an
optional trailing tail that is stored and not decoded.
"""

from __future__ import annotations

SFLOAT_NAN = 0x07FF
SFLOAT_NRES = 0x0800
SFLOAT_PINF = 0x07FE
SFLOAT_NINF = 0x0802
SFLOAT_RESERVED = 0x0801

_SPECIAL = {
    SFLOAT_NAN: "NaN",
    SFLOAT_NRES: "NRes",
    SFLOAT_PINF: "+inf",
    SFLOAT_NINF: "-inf",
    SFLOAT_RESERVED: "reserved",
}

KPA_TO_MMHG = 7.50062

BODY_MOVEMENT = 0x0001
CUFF_LOOSE = 0x0002
IRREGULAR_PULSE = 0x0004
PULSE_RANGE_MASK = 0x0018
POSITION = 0x0020

BP_MEASUREMENT = "00002a35-0000-1000-8000-00805f9b34fb"
BP_INTERMEDIATE = "00002a36-0000-1000-8000-00805f9b34fb"
BP_FEATURE = "00002a49-0000-1000-8000-00805f9b34fb"
CURRENT_TIME = "00002a2b-0000-1000-8000-00805f9b34fb"
BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"
AND_VENDOR = "11127000-b364-11e4-ab27-0800200c9a66"
AND_VENDOR_READ = "11127001-b364-11e4-ab27-0800200c9a66"
AND_VENDOR_NOTIFY = "11127002-b364-11e4-ab27-0800200c9a66"
AND_VENDOR_INDICATE = "11127003-b364-11e4-ab27-0800200c9a66"


def sfloat(raw: int) -> int | float | str:
    """Decode an IEEE-11073 16-bit SFLOAT."""
    if raw in _SPECIAL:
        return _SPECIAL[raw]
    mantissa = raw & 0x0FFF
    if mantissa >= 0x0800:
        mantissa -= 0x1000
    exponent = (raw >> 12) & 0x0F
    if exponent >= 0x0008:
        exponent -= 0x0010
    value = mantissa * (10 ** exponent)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _u16(data: bytes, index: int) -> int:
    return int.from_bytes(data[index : index + 2], "little")


def _take_sfloat(data: bytes, index: int) -> tuple[int | float | str, int, int]:
    raw = _u16(data, index)
    return sfloat(raw), raw, index + 2


def parse_measurement(data: bytes) -> dict | None:
    """Parse a Blood Pressure Measurement, or None if the buffer is too short."""
    if len(data) < 7:
        return None
    flags = data[0]
    index = 1
    systolic, systolic_raw, index = _take_sfloat(data, index)
    if index + 4 > len(data):
        return None
    diastolic, _, index = _take_sfloat(data, index)
    mean, mean_raw, index = _take_sfloat(data, index)
    parsed = {
        "flags": flags,
        "units": "kPa" if flags & 0x01 else "mmHg",
        "systolic": systolic,
        "diastolic": diastolic,
        "mean": mean,
        "mean_raw": mean_raw,
        "systolic_raw": systolic_raw,
        "taken_at": None,
        "pulse": None,
        "user_byte": None,
        "status": None,
        "tail": "",
    }
    if flags & 0x02:
        if index + 7 > len(data):
            return None
        year = _u16(data, index)
        month, day = data[index + 2], data[index + 3]
        hour, minute, second = data[index + 4], data[index + 5], data[index + 6]
        if not _plausible_clock(year, month, day, hour, minute, second):
            return None
        parsed["taken_at"] = f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"
        index += 7
    if flags & 0x04:
        if index + 2 > len(data):
            return None
        pulse, _, index = _take_sfloat(data, index)
        parsed["pulse"] = pulse
    if flags & 0x08:
        if index >= len(data):
            return None
        parsed["user_byte"] = data[index]
        index += 1
    if flags & 0x10:
        if index + 2 > len(data):
            return None
        parsed["status"] = _u16(data, index)
        index += 2
    parsed["tail"] = data[index:].hex()
    return parsed


def _plausible_clock(year: int, month: int, day: int, hour: int, minute: int, second: int) -> bool:
    return (
        2000 <= year <= 2099
        and 1 <= month <= 12
        and 1 <= day <= 31
        and hour <= 23
        and minute <= 59
        and second <= 59
    )


def mmhg(parsed: dict) -> tuple[int | None, int | None]:
    """Return systolic and diastolic in mmHg. kPa values are converted."""
    systolic = _mmhg_number(parsed["systolic"], parsed["units"])
    diastolic = _mmhg_number(parsed["diastolic"], parsed["units"])
    return systolic, diastolic


def _mmhg_number(value: int | float | str, units: str) -> int | None:
    if not isinstance(value, (int, float)):
        return None
    if units == "kPa":
        value = float(value) * KPA_TO_MMHG
    return int(round(value))


def status_bits(status: int | None) -> dict:
    if status is None:
        return {
            "body_movement": False,
            "cuff_loose": False,
            "irregular_pulse": False,
            "pulse_range": None,
            "position": False,
        }
    return {
        "body_movement": bool(status & BODY_MOVEMENT),
        "cuff_loose": bool(status & CUFF_LOOSE),
        "irregular_pulse": bool(status & IRREGULAR_PULSE),
        "pulse_range": (status & PULSE_RANGE_MASK) >> 3,
        "position": bool(status & POSITION),
    }


def parse_feature(data: bytes) -> dict:
    flags = int.from_bytes(data, "little") if data else 0
    return {
        "raw": flags,
        "body_movement": bool(flags & 0x01),
        "cuff_fit": bool(flags & 0x02),
        "irregular_pulse": bool(flags & 0x04),
        "pulse_range": bool(flags & 0x08),
        "position": bool(flags & 0x10),
        "multiple_bonds": bool(flags & 0x20),
    }


def parse_intermediate(data: bytes) -> dict | None:
    """Intermediate cuff pressure. This is live telemetry, not a stored reading."""
    if len(data) < 3:
        return None
    flags = data[0]
    pressure, raw, _index = _take_sfloat(data, 1)
    units = "kPa" if flags & 0x01 else "mmHg"
    return {"flags": flags, "pressure": pressure, "raw": raw, "units": units}


def measurement_uuids(uuid: str) -> bool:
    """True for the standard measurement and the A&D indicate that has carried it."""
    key = uuid.lower()
    return key in {BP_MEASUREMENT, AND_VENDOR_INDICATE}
