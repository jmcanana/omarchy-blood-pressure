"""Shadow listeners: BlueZ advertisement monitors beside the active scan, recording what a passive scan
would have heard, so passive scanning can be proved on real measurements before it replaces the scan.

An opt-in trial ("shadow" in the state directory's scan-mode file; the default is "active"). The connect
path is unchanged: the monitors are removed before each connect and registered again after it, every
BlueZ call is bounded, and any failure turns the shadow off for this run and is recorded. The scan is
not unchanged: while any monitor exists the kernel turns off the controller's duplicate filter for
active discovery (Linux 7.2 hci_sync.c, hci_active_scan_sync), so bluetoothd sees every advert of every
device in range, and the shell log may grow faster during the trial. Watch it.

BlueZ 5.87 calls DeviceFound on every matching advert for a monitor without RSSI thresholds, so a
trusted cuff's match is stored once per episode (EPISODE_SECONDS) per monitor, with the reports counted
and written when the monitors are removed. Other devices are counted as distinct devices, never named.

Patterns (an or-pattern matches its bytes at a fixed offset inside one AD structure's data):
- company: the A&D company id 105 (0x0069, little-endian) at the start of manufacturer data. The START
  burst and the window carry it.
- tail: the transfer iBeacon's major, minor and power, a1030001c3, at offset 20 of Apple's manufacturer
  data (2 company bytes, 02 15, a 16-byte UUID). The `apple-shape` row records the length the cuff's
  Apple value really has, which proves or disproves that offset.
- ibeacon: any iBeacon (4c 00 02 15 at offset 0), to show whether the cuff's Apple packet is heard at all
  when the tail pattern is not.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from pressure.store import Store

MANUFACTURER_DATA = 0xFF
PATTERNS: dict[str, tuple[int, int, bytes]] = {
    "company": (0, MANUFACTURER_DATA, bytes.fromhex("6900")),
    "tail": (20, MANUFACTURER_DATA, bytes.fromhex("a1030001c3")),
    "ibeacon": (0, MANUFACTURER_DATA, bytes.fromhex("4c000215")),
}
REARM_SECONDS = 120.0
EPISODE_SECONDS = 300.0
BLUEZ_CALL_SECONDS = 1.0
OTHERS_REPORT_SECONDS = 3600.0
MODE_FILE = "scan-mode"
MODES = ("active", "shadow")


def scan_mode(state_dir: Path) -> str:
    """The scan mode from the state directory's scan-mode file: active unless it says shadow."""
    try:
        chosen = (Path(state_dir) / MODE_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return "active"
    return chosen if chosen in MODES else "active"


def address_from_path(path: str) -> str:
    """/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF -> aa:bb:cc:dd:ee:ff, or "" for any other path."""
    leaf = str(path).rsplit("/", 1)[-1]
    if not leaf.startswith("dev_"):
        return ""
    return leaf[4:].replace("_", ":").lower()


class BluezRegistrar:
    """Registers monitors with org.bluez.AdvertisementMonitorManager1 on its own system-bus connection,
    the way bleak 3.0.2 registers its passive scanner's monitor (register, then export)."""

    def __init__(self, adapter_path: str) -> None:
        self.adapter_path = adapter_path
        self.bus = None

    async def open(self) -> None:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus

        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    async def register(self, label: str, pattern: tuple[int, int, bytes], found: Callable, released: Callable):
        from dbus_fast import Message, MessageType
        from dbus_fast.service import method

        from bleak.backends.bluezdbus.advertisement_monitor import AdvertisementMonitor

        class Monitor(AdvertisementMonitor):
            @method()
            def Release(self):  # noqa: N802 (BlueZ method name)
                released(label)

            @method()
            def DeviceFound(self, device: "o"):  # noqa: F821, N802
                found(label, device)

            @method()
            def DeviceLost(self, device: "o"):  # noqa: F821, N802
                return None

        monitor = Monitor([pattern])
        path = f"/org/jmcanana/bloodpressure/{os.getpid()}/{label}"
        reply = await self.bus.call(
            Message(
                destination="org.bluez",
                path=self.adapter_path,
                interface="org.bluez.AdvertisementMonitorManager1",
                member="RegisterMonitor",
                signature="o",
                body=[path],
            )
        )
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(reply.error_name)
        self.bus.export(path, monitor)
        return path, monitor

    async def unregister(self, handle) -> None:
        from dbus_fast import Message

        path, monitor = handle
        self.bus.unexport(path, monitor)
        await self.bus.call(
            Message(
                destination="org.bluez",
                path=self.adapter_path,
                interface="org.bluez.AdvertisementMonitorManager1",
                member="UnregisterMonitor",
                signature="o",
                body=[path],
            )
        )

    async def close(self) -> None:
        if self.bus is not None:
            self.bus.disconnect()
            self.bus = None


class Shadow:
    """The shadow listeners for one sidecar run. Every method is safe to call in any state."""

    def __init__(self, store: Store, registrar, clock: Callable[[], float] = time.monotonic) -> None:
        self.store = store
        self.registrar = registrar
        self.clock = clock
        self.enabled = True
        self.opened = False
        self.handles: dict[str, object] = {}
        self.armed_at = 0.0
        self.others: dict[str, set[str]] = {}
        self.others_since = clock()
        self.shape_seen: set[str] = set()
        self.last_stored: dict[tuple[str, str], float] = {}
        self.reports: Counter = Counter()

    async def _bounded(self, call) -> None:
        await asyncio.wait_for(call, BLUEZ_CALL_SECONDS)

    def _off(self, kind: str, exc: BaseException) -> None:
        self.enabled = False
        self.handles.clear()
        self.store.add_telemetry(None, kind, {"error": type(exc).__name__})

    def found(self, label: str, device_path: str) -> None:
        """Called for every matching advert. A trusted cuff's first match in an episode is stored; the rest
        are counted. Other devices are kept only as a set of paths, reported as a count."""
        address = address_from_path(device_path)
        if not (address and self.store.is_trusted(address)):
            self.others.setdefault(label, set()).add(device_path)
            return
        self.reports[(label, address)] += 1
        now = self.clock()
        last = self.last_stored.get((label, address))
        if last is None or now - last >= EPISODE_SECONDS:
            self.last_stored[(label, address)] = now
            self.store.add_telemetry(address, "monitor-seen", {"monitor": label})

    def released(self, label: str) -> None:
        self.handles.pop(label, None)
        self.store.add_telemetry(None, "monitor-released", {"monitor": label})

    def note_apple_shape(self, address: str, apple_value: bytes | None) -> None:
        """Once per run per trusted cuff: the length of its Apple manufacturer value, which fixes the tail offset."""
        if not apple_value or address in self.shape_seen or not self.store.is_trusted(address):
            return
        self.shape_seen.add(address)
        self.store.add_telemetry(address, "apple-shape", {"length": len(apple_value)})

    async def arm(self) -> None:
        if not self.enabled or self.handles:
            return
        try:
            if not self.opened:
                await self._bounded(self.registrar.open())
                self.opened = True
            for label, pattern in PATTERNS.items():
                self.handles[label] = await asyncio.wait_for(
                    self.registrar.register(label, pattern, self.found, self.released), BLUEZ_CALL_SECONDS
                )
        except Exception as exc:
            await self.disarm()
            self._off("monitor-refused", exc)
            return
        self.armed_at = self.clock()

    async def disarm(self) -> None:
        if self.reports:
            for (label, address), count in sorted(self.reports.items()):
                self.store.add_telemetry(address, "monitor-reports", {"monitor": label, "reports": count})
            self.reports.clear()
        handles, self.handles = self.handles, {}
        for handle in handles.values():
            try:
                await self._bounded(self.registrar.unregister(handle))
            except Exception as exc:
                self.store.add_telemetry(None, "monitor-unregister-failed", {"error": type(exc).__name__})

    async def tick(self) -> None:
        """Re-arm, at most every REARM_SECONDS, once BlueZ has released a monitor; report how many other
        devices each monitor matched, once an hour."""
        now = self.clock()
        if self.enabled and len(self.handles) < len(PATTERNS) and now - self.armed_at >= REARM_SECONDS:
            await self.disarm()
            await self.arm()
            self.armed_at = now
        if now - self.others_since >= OTHERS_REPORT_SECONDS:
            if self.others:
                self.store.add_telemetry(None, "monitor-others", {label: len(paths) for label, paths in sorted(self.others.items())})
            self.others.clear()
            self.others_since = now

    async def close(self) -> None:
        await self.disarm()
        try:
            await self.registrar.close()
        except Exception:
            pass
