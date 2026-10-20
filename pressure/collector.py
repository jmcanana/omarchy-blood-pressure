"""Drain one bonded link.

Subscribe to the record characteristics, then send nothing until the cuff hangs up.
On 17 and 18 October 2026 a feature read between records was followed by no further
record on 5 of 6 links (the 17 October 14:13 drain continued); three of those five also
wrote the clock. Subscriptions between records (6 of 6) and one 11127001 read did not
stop it. A client that only subscribes took the whole backlog on every link (5, 3, 1 and
3 records). Records arrive on 0x2A35 or 11127003, so both are subscribed. Device reads, and
a clock write when the cuff clock is off, wait until the records have stopped.
The link is released by the caller when this function returns.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from pressure.blp import (
    AND_VENDOR_INDICATE,
    BATTERY,
    BP_FEATURE,
    BP_INTERMEDIATE,
    BP_MEASUREMENT,
    CURRENT_TIME,
    measurement_uuids,
    parse_feature,
    parse_intermediate,
    parse_measurement,
)
from pressure.store import Store

# The cuff hangs up 5.2–5.8 s after its last record; waiting longer leaves the end to it.
QUIET_SECONDS = 10.0
FIRST_WAIT_SECONDS = 8.0
RECORD_CHANNELS = {BP_MEASUREMENT, BP_INTERMEDIATE, AND_VENDOR_INDICATE}
# Records came at most 0.8 s apart, so 2 s without one means the backlog is out. The cuff
# hangs up 5.2-5.8 s after its last record and reads take about 0.4 s, so at most seven fit.
TAIL_READ_AFTER_SECONDS = 2.0
CLOCK_TOLERANCE_SECONDS = 120
_DEVICE_INFO = {
    "00002a24-0000-1000-8000-00805f9b34fb": "model",
    "00002a26-0000-1000-8000-00805f9b34fb": "firmware",
}
_DEVICE_EXTRA = {
    "00002a29-0000-1000-8000-00805f9b34fb": "manufacturer",
    "00002a25-0000-1000-8000-00805f9b34fb": "serial",
}



class Link(Protocol):
    def is_connected(self) -> bool: ...

    async def pair(self) -> None: ...

    async def subscribe(self, uuid: str, callback) -> None: ...

    async def read(self, uuid: str) -> bytes: ...

    async def write(self, uuid: str, payload: bytes) -> None: ...

    def notify_uuids(self) -> list[str]: ...


async def drain(
    link: Link,
    store: Store,
    address: str,
    *,
    quiet_seconds: float = QUIET_SECONDS,
    first_wait: float = FIRST_WAIT_SECONDS,
    tail_after: float = TAIL_READ_AFTER_SECONDS,
    clock_source: Callable[[], bytes] | None = None,
) -> dict:
    """clock_source gives this computer's time as a Current Time payload; without it the clock is
    only read. Readings stored while the clock is found off are marked, whether or not it is then set."""
    await link.pair()
    stored = 0
    repeated = 0
    measurement_traffic = 0
    clock_set = False
    clock_off = False
    stored_ids: list[int] = []

    def on_value(uuid: str, data: bytes) -> None:
        nonlocal stored, repeated, measurement_traffic
        key = uuid.lower()
        if key == BP_INTERMEDIATE:
            measurement_traffic += 1
            parsed = parse_intermediate(data)
            if parsed is not None:
                store.add_telemetry(address, "intermediate", {"units": parsed["units"]})
            else:
                store.add_telemetry(address, "intermediate", {"nbytes": len(data), "raw": data.hex()})
            return
        if not measurement_uuids(key):
            store.add_telemetry(address, "notify", {"nbytes": len(data)})
            return
        measurement_traffic += 1
        parsed = parse_measurement(data)
        if parsed is None:
            store.add_telemetry(address, "unparsed", {"nbytes": len(data), "source": key, "raw": data.hex()})
            return
        parsed["raw"] = data.hex()
        source = "standard" if key == BP_MEASUREMENT else "vendor"
        reading_id = store.add_reading(address, parsed, source)
        if reading_id is None:
            repeated += 1
            return
        stored += 1
        stored_ids.append(reading_id)

    for uuid in link.notify_uuids():
        if uuid.lower() in RECORD_CHANNELS:
            await link.subscribe(uuid, on_value)

    started = time.monotonic()
    seen = measurement_traffic
    last_rx: float | None = started if seen else None
    device_read = False
    while link.is_connected():
        await asyncio.sleep(0.05)
        now = time.monotonic()
        current = measurement_traffic
        if current != seen:
            seen = current
            last_rx = now
        if last_rx is None:
            if now - started > first_wait:
                break
            continue
        if not device_read and now - last_rx > tail_after:
            device_read = True
            # The cuff hangs up about 3 s after this point; the clock goes before the identity reads.
            clock_matched = await _read_status(link, store, address)
            if clock_matched is False:
                clock_off = True
                store.mark_clock_off(stored_ids)
                if clock_source is not None and link.is_connected():
                    clock_set = await _set_clock(link, store, address, clock_source())
            await _read_identity(link, store, address)
            continue
        if now - last_rx > quiet_seconds:
            break

    if clock_off:
        # A record after the tail began was stamped by the same clock.
        store.mark_clock_off(stored_ids)
    return {
        "cuff_released": not link.is_connected(),
        "stored": stored,
        "repeated": repeated,
        "clock_set": clock_set,
    }


async def _read_status(link: Link, store: Store, address: str) -> bool | None:
    """Battery and clock, on every sync. Returns whether the cuff clock matched, or None when it could
    not be read."""
    clock_matched: bool | None = None
    try:
        battery = await link.read(BATTERY)
        if battery:
            store.update_device(address, battery=battery[0])
            store.add_telemetry(address, "battery", {"percent": battery[0]})
    except Exception as exc:
        store.add_telemetry(address, "battery-read-failed", {"error": type(exc).__name__})
    try:
        clock_matched = _check_clock(await link.read(CURRENT_TIME), store, address)
    except Exception as exc:
        store.add_telemetry(address, "clock-read-failed", {"error": type(exc).__name__})
    return clock_matched


async def _read_identity(link: Link, store: Store, address: str) -> None:
    """Device information, until model, firmware and features are stored."""
    if store.device_known(address):
        return
    fields: dict[str, object] = {}
    try:
        for uuid, field in {**_DEVICE_INFO, BP_FEATURE: "features", **_DEVICE_EXTRA}.items():
            value = await link.read(uuid)
            if field == "features":
                store.add_telemetry(address, "feature", parse_feature(value))
                if value:
                    fields[field] = int.from_bytes(value, "little")
                continue
            text = value.decode("utf-8", "replace").strip("\x00").strip()
            if text:
                fields[field] = text
    except Exception as exc:
        store.add_telemetry(address, "device-read-stopped", {"error": type(exc).__name__, "fields": len(fields)})
    if fields:
        store.update_device(address, **fields)


async def _set_clock(link: Link, store: Store, address: str, payload: bytes) -> bool:
    """Write this computer's local time to the cuff and read it back; returns whether it now matches."""
    try:
        await link.write(CURRENT_TIME, payload)
    except Exception as exc:
        store.add_telemetry(address, "clock-write-failed", {"error": type(exc).__name__})
        return False
    try:
        readback = await link.read(CURRENT_TIME)
    except Exception as exc:
        store.add_telemetry(address, "clock-readback-failed", {"error": type(exc).__name__})
        return False
    matched = _check_clock(readback, store, address)
    store.add_telemetry(address, "clock-write", {"readback_matched": bool(matched)})
    return bool(matched)


def _check_clock(value: bytes, store: Store, address: str) -> bool | None:
    """Record whether the cuff clock is within two minutes of this computer; None for a short read.
    A date that is not a date (Bluetooth's year 0, month 0 or day 0 mean unknown) counts as off."""
    if len(value) < 7:
        return None
    try:
        cuff = datetime(int.from_bytes(value[0:2], "little"), value[2], value[3], value[4], value[5], value[6])
    except ValueError:
        store.add_telemetry(address, "clock-unknown", {"raw": value[:7].hex()})
        store.put_setting("clock_checked", "0")
        store.put_setting("clock_skew_seconds", "")
        return False
    skew = abs((cuff - datetime.now()).total_seconds())
    matched = skew <= CLOCK_TOLERANCE_SECONDS
    store.add_telemetry(address, "clock-read", {"skew_seconds": round(skew), "matched": matched})
    store.put_setting("clock_checked", "1" if matched else "0")
    store.put_setting("clock_skew_seconds", str(round(skew)))
    return matched
