"""Drain one bonded link.

Subscribe to the record characteristics, then send nothing until the cuff hangs up.
On 17 and 18 October 2026 a feature read between records was followed by no further
record on 5 of 6 links (the 17 October 14:13 drain continued); three of those five also
wrote the clock. Subscriptions between records (6 of 6) and one 11127001 read did not
stop it. A client that only subscribes took the whole backlog on every link (5, 3, 1 and
3 records). Records arrive on 0x2A35 or 11127003, so both are subscribed. The clock is not
written unless the caller asks, and then only after the burst has gone quiet.
The link is released by the caller when this function returns.
"""

from __future__ import annotations

import asyncio
import time
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
    write_clock: bool = False,
    quiet_seconds: float = QUIET_SECONDS,
    first_wait: float = FIRST_WAIT_SECONDS,
    clock_payload: bytes | None = None,
    tail_after: float = TAIL_READ_AFTER_SECONDS,
) -> dict:
    await link.pair()
    stored = 0
    repeated = 0
    measurement_traffic = 0
    clock_written = False
    same_link = False

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
        if store.add_reading(address, parsed, source):
            stored += 1
        else:
            repeated += 1

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
            await _read_device(link, store, address)
            continue
        if now - last_rx > quiet_seconds:
            break

    if write_clock and link.is_connected() and clock_payload:
        await link.write(CURRENT_TIME, clock_payload)
        clock_written = True
        readback = b""
        try:
            readback = await link.read(CURRENT_TIME)
        except Exception:
            readback = b""
        same_link = readback[:7] == clock_payload[:7] if len(readback) >= 7 and len(clock_payload) >= 7 else False
        store.add_telemetry(
            address,
            "clock-write",
            {"nbytes": len(clock_payload), "readback_nbytes": len(readback), "same_link_time_match": same_link},
        )

    return {
        "cuff_released": not link.is_connected(),
        "stored": stored,
        "repeated": repeated,
        "clock_written": clock_written,
        "clock_matched": same_link,
    }


async def _read_device(link: Link, store: Store, address: str) -> None:
    """Battery and clock on every sync; device information until model, firmware and features are stored."""
    fields: dict[str, object] = {}
    try:
        battery = await link.read(BATTERY)
        if battery:
            fields["battery"] = battery[0]
            store.add_telemetry(address, "battery", {"percent": battery[0]})
        try:
            _check_clock(await link.read(CURRENT_TIME), store, address)
        except Exception as exc:
            store.add_telemetry(address, "clock-read-failed", {"error": type(exc).__name__})
        if not store.device_known(address):
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


def _check_clock(value: bytes, store: Store, address: str) -> None:
    """Record whether the cuff clock is within two minutes of this computer. Nothing is written."""
    if len(value) < 7:
        return
    try:
        cuff = datetime(int.from_bytes(value[0:2], "little"), value[2], value[3], value[4], value[5], value[6])
    except ValueError:
        return
    skew = abs((cuff - datetime.now()).total_seconds())
    matched = skew <= CLOCK_TOLERANCE_SECONDS
    store.add_telemetry(address, "clock-read", {"skew_seconds": round(skew), "matched": matched})
    store.put_setting("clock_checked", "1" if matched else "0")
    store.put_setting("clock_skew_seconds", str(round(skew)))
