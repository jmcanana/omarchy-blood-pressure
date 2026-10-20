"""BlueZ scan and one-link drain. Imported only when the sidecar really runs."""

from __future__ import annotations

import asyncio
import secrets
import time
from datetime import datetime

from pressure.collector import drain
from pressure.policy import (
    CONNECT_TIMEOUT_SECONDS,
    arms_session,
    beacon_class,
    should_connect,
    should_drain,
)
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


async def computer_clock_synced() -> bool:
    """Whether systemd says this computer's clock is NTP-synchronized. Unknown counts as no, so an
    unsynchronized computer never overwrites the cuff."""
    try:
        from dbus_fast import BusType, Message, MessageType
        from dbus_fast.aio import MessageBus

        async def ask() -> bool:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            try:
                reply = await bus.call(
                    Message(
                        destination="org.freedesktop.timedate1",
                        path="/org/freedesktop/timedate1",
                        interface="org.freedesktop.DBus.Properties",
                        member="Get",
                        signature="ss",
                        body=["org.freedesktop.timedate1", "NTPSynchronized"],
                    )
                )
            finally:
                bus.disconnect()
            return reply.message_type == MessageType.METHOD_RETURN and reply.body[0].value is True

        return await asyncio.wait_for(ask(), NTP_CHECK_SECONDS)
    except Exception:
        return False


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


async def radio_loop(store: Store, live: dict) -> None:
    from bleak import BleakClient, BleakScanner

    target: dict = {}
    shown_kind = ""
    wake = asyncio.Event()

    def on_detect(device, advertisement) -> None:
        nonlocal shown_kind
        manufacturer = {int(key): value for key, value in (advertisement.manufacturer_data or {}).items()}
        uuids = [str(item) for item in (advertisement.service_uuids or [])]
        kind = beacon_class(manufacturer)
        address = str(device.address)
        if store.is_trusted(address) and arms_session(kind):
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
        address = str(device.address)
        name = advertisement.local_name or device.name or ""
        if not store.is_trusted(address):
            store.remember_pending(secrets.token_hex(8), address, name, kind)
            live["beacon"] = kind
            store.write_status(state="seen", beacon=kind, link=False, error="")
            return
        if not should_drain(uuids, manufacturer, store.trusted_beacon(address)):
            return
        target["device"] = device
        target["kind"] = kind
        live["beacon"] = kind
        wake.set()

    scanner = BleakScanner(detection_callback=on_detect)
    await scanner.start()
    live["state"] = "scanning"
    store.write_status(state="scanning", beacon="none", link=False, error="")
    try:
        while True:
            await wake.wait()
            wake.clear()
            if "device" not in target:
                continue
            device = target.pop("device")
            kind = target.pop("kind", "transfer")
            live.update(state="connecting", beacon=kind)
            store.write_status(state="connecting", beacon=kind, link=False, error="")
            clock_synced = await computer_clock_synced()
            if not clock_synced:
                store.add_telemetry(str(device.address), "computer-clock-unsynced", {})
            try:
                await scanner.stop()
            except Exception as exc:
                store.add_telemetry(str(device.address), "scan-stop-failed", {"error": type(exc).__name__})
            outcome = "ok"
            started = time.monotonic()
            store.add_telemetry(str(device.address), "connect-start", {"beacon": kind})
            try:
                # An address string makes bleak rediscover the cuff before Connect, as the
                # script that drained on 17 October 2026 did. A BLEDevice connected 0 of 3.
                async with BleakClient(str(device.address), timeout=CONNECT_TIMEOUT_SECONDS) as client:
                    store.add_telemetry(
                        str(device.address), "connected", {"seconds": round(time.monotonic() - started, 3)}
                    )
                    link = _BleakLink(client)
                    live.update(state="draining", link=True, error="")
                    store.write_status(state="draining", beacon=kind, link=True, error="")
                    try:
                        await asyncio.wait_for(link.pair(), CONNECT_TIMEOUT_SECONDS)
                    except TimeoutError:
                        outcome = "pair-timeout"
                        live.update(state="scanning", link=False, error="pairing timed out")
                        store.write_status(
                            state="scanning", beacon=kind, link=False, error="pairing timed out"
                        )
                    else:
                        result = await drain(
                            link,
                            store,
                            str(device.address),
                            clock_source=(lambda: clock_payload(datetime.now().astimezone())) if clock_synced else None,
                        )
                        store.add_telemetry(
                            str(device.address), "drain", {"cuff_released": result["cuff_released"]}
                        )
                        store.record_sync(result["stored"])
                        store.update_device(
                            str(device.address),
                            name=device.name or "",
                            mtu=getattr(client, "mtu_size", None),
                        )
            except TimeoutError:
                outcome = "timeout"
                live.update(state="scanning", link=False, error="timed out")
                store.write_status(state="scanning", beacon="none", link=False, error="timed out")
            except Exception:
                outcome = "failed"
                live.update(state="scanning", link=False, error="connection failed")
                store.write_status(state="scanning", beacon="none", link=False, error="connection failed")
            finally:
                store.add_telemetry(
                    str(device.address),
                    "connect-end",
                    {"outcome": outcome, "seconds": round(time.monotonic() - started, 3)},
                )
            if outcome == "ok":
                live.update(state="scanning", link=False, error="")
                store.add_telemetry(str(device.address), "disconnect", {"beacon": kind})
                store.write_status(state="scanning", beacon="none", link=False, error="")
            try:
                await scanner.start()
            except Exception:
                await asyncio.sleep(1)
                try:
                    await scanner.start()
                except Exception:
                    live["state"] = "error"
                    store.write_status(state="error", beacon="none", link=False, error="scan did not resume")
                    return
    finally:
        try:
            await scanner.stop()
        except Exception:
            pass
