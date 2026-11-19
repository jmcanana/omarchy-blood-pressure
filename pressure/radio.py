"""BlueZ scan or passive listening, and the one-link drain. bleak is imported only when the radio runs."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from datetime import datetime
from typing import NoReturn

from pressure.bluez import DEVICE_INTERFACE, SystemBus, Watchdog, adapter_name, wait_for_adapter
from pressure.collector import drain
from pressure.passive import NOT_ACTIVATED, ListenRefused, PassiveListen
from pressure.policy import (
    APPLE_COMPANY,
    CONNECT_TIMEOUT_SECONDS,
    arms_session,
    beacon_class,
    should_connect,
    should_drain,
    starts_measurement,
)
from pressure.shadow import BluezRegistrar, Shadow, set_scan_mode
from pressure.sighting import Sighting, from_bleak
from pressure.store import Store


def clock_payload(moment: datetime) -> bytes:
    local = moment.astimezone()
    return (
        int(local.year).to_bytes(2, "little")
        + bytes(
            [
                local.month,
                local.day,
                local.hour,
                local.minute,
                local.second,
                local.isoweekday(),
                0,
                0x01,
            ]
        )
    )


NTP_CHECK_SECONDS = 1.0
SCAN_CHECK_SECONDS = 10.0
# Passive listening falls back to the active scan after this many losses in this many seconds, counted
# from telemetry so restarts do not reset it.
LOSSES_BEFORE_FALLBACK = 3
LOSS_WINDOW_SECONDS = 600.0
# A trusted cuff's START burst with no window this long after it means passive listening missed the window.
# One miss can be a measurement abandoned after START; a second in a row falls back.
WINDOW_WAIT_SECONDS = 600.0
MISSES_BEFORE_FALLBACK = 2


class ScanLost(RuntimeError):
    """The scan or the listening ended underneath the sidecar. Raised so the sidecar exits and Service.qml
    starts a fresh one: bleak 3.0.2 does not notice a bluetoothd restart or an adapter power cycle."""


def fall_back(store: Store, reason: str) -> NoReturn:
    """Give up on passive listening: write active to scan-mode and end the run, so the next sidecar scans."""
    set_scan_mode(store.state_dir, "active")
    store.add_telemetry(None, "scan-mode-fallback", {"reason": reason})
    raise ScanLost(f"passive listening fell back to the active scan: {reason}")


async def system_properties(destination: str, path: str, interface: str, timeout: float) -> dict | None:
    """All properties of one system-bus object, or None when it cannot be read in time."""
    try:
        from dbus_fast import BusType, Message, MessageType
        from dbus_fast.aio import MessageBus

        async def ask() -> dict | None:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            try:
                reply = await bus.call(
                    Message(
                        destination=destination,
                        path=path,
                        interface="org.freedesktop.DBus.Properties",
                        member="GetAll",
                        signature="s",
                        body=[interface],
                    )
                )
            finally:
                bus.disconnect()
            if reply.message_type != MessageType.METHOD_RETURN:
                return None
            return {name: variant.value for name, variant in reply.body[0].items()}

        return await asyncio.wait_for(ask(), timeout)
    except Exception:
        return None


async def computer_clock_synced() -> bool:
    """Whether systemd says this computer's clock is NTP-synchronized. Unknown counts as no, so an
    unsynchronized computer never overwrites the cuff."""
    properties = await system_properties(
        "org.freedesktop.timedate1", "/org/freedesktop/timedate1", "org.freedesktop.timedate1", NTP_CHECK_SECONDS
    )
    return (properties or {}).get("NTPSynchronized") is True


async def woken_within(wake: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(wake.wait(), seconds)
    except TimeoutError:
        return False
    return True


class _BleakLink:
    def __init__(self, client) -> None:
        self.client = client

    def is_connected(self) -> bool:
        return bool(self.client.is_connected)

    async def pair(self) -> None:
        try:
            await self.client.pair()
        except NotImplementedError:
            return None
        except Exception as exc:
            if "already" in str(exc).lower():
                return None
            raise

    def notify_uuids(self) -> list[str]:
        found = []
        for service in self.client.services:
            for characteristic in service.characteristics:
                properties = {item.lower() for item in characteristic.properties}
                if "notify" in properties or "indicate" in properties:
                    found.append(str(characteristic.uuid))
        return found

    async def subscribe(self, uuid: str, callback) -> None:
        def wrapped(_characteristic, data: bytes) -> None:
            callback(uuid, bytes(data))

        await self.client.start_notify(uuid, wrapped)

    async def read(self, uuid: str) -> bytes:
        return bytes(await self.client.read_gatt_char(uuid))

    async def write(self, uuid: str, payload: bytes) -> None:
        await self.client.write_gatt_char(uuid, payload, response=True)


class ActiveScan:
    """bleak's active scan on the chosen adapter, handing each advert on as a Sighting."""

    def __init__(self, adapter: str, heard: Callable[[Sighting], None]) -> None:
        from bleak import BleakScanner

        self.scanner = BleakScanner(
            detection_callback=lambda device, advertisement: heard(from_bleak(device, advertisement)),
            bluez={"adapter": adapter},
        )

    async def start(self) -> None:
        await self.scanner.start()

    async def stop(self) -> None:
        await self.scanner.stop()


async def radio_loop(
    store: Store,
    live: dict,
    mode: str = "active",
    bus: SystemBus | None = None,
    shadow_for: Callable[[str, SystemBus], Shadow] | None = None,
    registrar_for: Callable[[str, SystemBus], BluezRegistrar] | None = None,
) -> None:
    """Scan (active, shadow) or listen (passive), and drain a trusted cuff in its window. In shadow mode the
    shadow listeners run beside the scan; they are disarmed before every connect, so the connect path is the
    same in every mode."""
    from bleak import BleakClient

    passive = mode == "passive"
    bus = bus if bus is not None else SystemBus()
    target: dict = {}
    shown_kind = ""
    start_heard_at: float | None = None
    shadow: Shadow | None = None
    wake = asyncio.Event()

    def heard(seen: Sighting) -> None:
        nonlocal shown_kind, start_heard_at
        manufacturer = seen.manufacturer
        uuids = list(seen.uuids)
        kind = beacon_class(manufacturer)
        address = seen.address
        trusted = store.is_trusted(address)
        if trusted and shadow is not None and APPLE_COMPANY in manufacturer:
            shadow.note_apple_shape(address, manufacturer[APPLE_COMPANY])
        if trusted and kind == "transfer":
            start_heard_at = None
        elif trusted and start_heard_at is None and starts_measurement(manufacturer):
            start_heard_at = time.monotonic()
        if trusted and arms_session(kind):
            if kind != shown_kind and not target:
                shown_kind = kind
                live.update(state="ready", beacon=kind)
                store.write_status(state="ready", beacon=kind, link=False, error="")
        if target:
            return
        if kind in {"idle", "start"}:
            return
        if not should_connect(uuids, manufacturer):
            return
        if not trusted:
            replaced = store.remember_pending(secrets.token_hex(8), address, seen.name, kind)
            live["beacon"] = kind
            # Once per new sighting, or again after another write moved the status off "seen":
            # an untrusted cuff advertises about twenty times a second.
            if replaced or store.last_status_state != "seen":
                store.write_status(state="seen", beacon=kind, link=False, error="")
            return
        if not should_drain(uuids, manufacturer, store.trusted_beacon(address)):
            return
        target["sighting"] = seen
        target["kind"] = kind
        live["beacon"] = kind
        wake.set()

    await bus.open()
    try:
        adapter_path = await wait_for_adapter(bus, SCAN_CHECK_SECONDS)
        adapter = adapter_name(adapter_path)
        watchdog = Watchdog(bus, adapter_path, wake)
        await watchdog.open()
        store.add_telemetry(None, "adapter", {"name": adapter, "mode": mode})
        if passive:
            if store.count_telemetry_since("scan-lost", LOSS_WINDOW_SECONDS, mode="passive") >= LOSSES_BEFORE_FALLBACK:
                fall_back(store, "repeated-loss")
            listener = PassiveListen(
                (registrar_for or _default_registrar)(adapter_path, bus),
                lambda path: bus.get_all(path, DEVICE_INTERFACE),
                heard,
                watchdog.released,
                lambda: store.add_telemetry(None, "monitor-active", {"monitor": "passive"}),
            )
        else:
            listener = ActiveScan(adapter, heard)
        if mode == "shadow":
            shadow = (shadow_for or _default_shadow(store))(adapter_path, bus)
        await _start_listening(store, listener, passive)
        live["state"] = "scanning"
        store.write_status(state="scanning", beacon="none", link=False, error="")
        try:
            if shadow is not None:
                await shadow.arm()
            while True:
                await woken_within(wake, SCAN_CHECK_SECONDS)
                wake.clear()
                if "sighting" not in target:
                    if await _check_listening(store, watchdog, mode, start_heard_at, listener):
                        start_heard_at = None
                    if shadow is not None:
                        await shadow.tick()
                    continue
                seen = target.pop("sighting")
                kind = target.pop("kind", "transfer")
                await _sync(store, live, seen, kind, adapter, listener, shadow, BleakClient)
                start_heard_at = None
                await _resume_listening(store, live, listener, passive)
                if shadow is not None:
                    await shadow.arm()
                # Our own stop and start of the scan changed the adapter; the next real change wakes us again.
                wake.clear()
        finally:
            if shadow is not None:
                await shadow.close()
            try:
                await listener.stop()
            except Exception:
                pass
    finally:
        bus.close()


def _default_registrar(adapter_path: str, bus: SystemBus) -> BluezRegistrar:
    return BluezRegistrar(adapter_path, bus.bus)


def _default_shadow(store: Store) -> Callable[[str, SystemBus], Shadow]:
    return lambda adapter_path, bus: Shadow(store, _default_registrar(adapter_path, bus))


async def _start_listening(store: Store, listener, passive: bool) -> None:
    try:
        await listener.start()
    except ListenRefused as refused:
        store.add_telemetry(None, "monitor-refused", {"monitor": "passive", "name": str(refused)})
        fall_back(store, NOT_ACTIVATED if str(refused) == NOT_ACTIVATED else "monitor-refused")


async def _check_listening(store: Store, watchdog: Watchdog, mode: str, start_heard_at: float | None, listener) -> bool:
    """End the run when the scan or listening is lost; in passive mode, fall back to active when BlueZ released
    the monitor on its own or a second window in a row was missed. True when a miss was recorded."""
    passive = mode == "passive"
    reason = await watchdog.loss(passive)
    if not reason and passive and not listener.listening:
        reason = "not-listening"
    if reason == "monitor-released":
        fall_back(store, reason)
    if reason:
        store.add_telemetry(None, "scan-lost", {"mode": mode, "reason": reason})
        raise ScanLost(reason)
    if passive and start_heard_at is not None and time.monotonic() - start_heard_at >= WINDOW_WAIT_SECONDS:
        store.add_telemetry(None, "window-missed", {"after_seconds": round(time.monotonic() - start_heard_at)})
        if store.windows_missed_in_a_row() >= MISSES_BEFORE_FALLBACK:
            fall_back(store, "window-missed")
        return True
    return False


async def _resume_listening(store: Store, live: dict, listener, passive: bool) -> None:
    """Start the scan or listening again after a sync; one retry, then end the run so a fresh sidecar starts."""
    if passive:
        await _start_listening(store, listener, passive)
        return
    try:
        await listener.start()
    except Exception:
        await asyncio.sleep(1)
        try:
            await listener.start()
        except Exception:
            live["state"] = "error"
            store.write_status(state="error", beacon="none", link=False, error="scan did not resume")
            store.add_telemetry(None, "scan-lost", {"mode": "active", "reason": "resume-failed"})
            raise ScanLost("scan did not resume after a sync") from None


async def _sync(store: Store, live: dict, seen: Sighting, kind: str, adapter: str, listener, shadow, client_class) -> None:
    """Stop listening, connect by address and drain. The connect path is the same in every mode."""
    address = seen.address
    live.update(state="connecting", beacon=kind)
    store.write_status(state="connecting", beacon=kind, link=False, error="")
    clock_synced = await computer_clock_synced()
    if not clock_synced:
        store.add_telemetry(address, "computer-clock-unsynced", {})
    try:
        await listener.stop()
    except Exception as exc:
        store.add_telemetry(address, "scan-stop-failed", {"error": type(exc).__name__})
    if shadow is not None:
        await shadow.disarm()
    outcome = "ok"
    failure = ""
    started = time.monotonic()
    store.add_telemetry(address, "connect-start", {"beacon": kind})
    try:
        # An address string makes bleak rediscover the cuff before Connect, as the
        # script that drained on 17 October 2026 did. A BLEDevice connected 0 of 3.
        async with client_class(address, timeout=CONNECT_TIMEOUT_SECONDS, bluez={"adapter": adapter}) as client:
            store.add_telemetry(address, "connected", {"seconds": round(time.monotonic() - started, 3)})
            link = _BleakLink(client)
            live.update(state="draining", link=True, error="")
            store.write_status(state="draining", beacon=kind, link=True, error="")
            try:
                await asyncio.wait_for(link.pair(), CONNECT_TIMEOUT_SECONDS)
            except TimeoutError:
                outcome = "pair-timeout"
                live.update(state="scanning", link=False, error="pairing timed out")
                store.write_status(state="scanning", beacon=kind, link=False, error="pairing timed out")
            else:
                result = await drain(
                    link,
                    store,
                    address,
                    clock_source=(lambda: clock_payload(datetime.now().astimezone())) if clock_synced else None,
                )
                store.add_telemetry(address, "drain", {"cuff_released": result["cuff_released"]})
                store.record_sync(result["stored"])
                store.update_device(address, name=seen.name, mtu=getattr(client, "mtu_size", None))
    except TimeoutError:
        outcome = "timeout"
        live.update(state="scanning", link=False, error="timed out")
        store.write_status(state="scanning", beacon="none", link=False, error="timed out")
    except Exception as exc:
        outcome = "failed"
        # The type only: a bleak message can carry a device path.
        failure = type(exc).__name__
        live.update(state="scanning", link=False, error="connection failed")
        store.write_status(state="scanning", beacon="none", link=False, error="connection failed")
    finally:
        ended = {"outcome": outcome, "seconds": round(time.monotonic() - started, 3)}
        if failure:
            ended["error"] = failure
        store.add_telemetry(address, "connect-end", ended)
    if outcome == "ok":
        live.update(state="scanning", link=False, error="")
        store.add_telemetry(address, "disconnect", {"beacon": kind})
        store.write_status(state="scanning", beacon="none", link=False, error="")
