"""BlueZ over one system-bus connection kept for the sidecar's life: which adapter to use, and whether
the scan or the listening has been lost underneath us. dbus_fast is imported only when the bus opens."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

BLUEZ = "org.bluez"
ADAPTER_INTERFACE = "org.bluez.Adapter1"
DEVICE_INTERFACE = "org.bluez.Device1"
PROPERTIES = "org.freedesktop.DBus.Properties"
BUS_NAME = "org.freedesktop.DBus"
LOGIN_MANAGER = "org.freedesktop.login1.Manager"
BUS_CALL_SECONDS = 2.0
# A Release that comes this close to an owner change or a power cycle is part of that loss, not a reason
# to give up on listening.
RELEASE_GRACE_SECONDS = 10.0


def _path_order(path: str) -> tuple[int, str]:
    """hci2 before hci10."""
    return len(path), path


def choose_adapter(objects: dict) -> str | None:
    """The first adapter path, in path order, that can be a central and is powered; None while none is.
    bleak alone takes the first powered central adapter of an unordered set, so the choice is made here
    and passed to everything that uses the radio."""
    for path in sorted(objects, key=_path_order):
        adapter = objects[path].get(ADAPTER_INTERFACE)
        if adapter is None:
            continue
        roles = adapter.get("Roles")
        if roles is not None and "central" not in roles:
            continue
        if adapter.get("Powered") is True:
            return path
    return None


def adapter_name(path: str) -> str:
    """/org/bluez/hci0 -> hci0, the form bleak takes."""
    return path.rsplit("/", 1)[-1]


def _plain(properties: dict) -> dict:
    return {name: getattr(value, "value", value) for name, value in properties.items()}


class SystemBus:
    """One system-bus connection for the sidecar's life. Every call is bounded; a failed or refused call
    reads as None, so a caller decides what a missing answer means."""

    def __init__(self) -> None:
        self.bus = None

    async def open(self) -> None:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus

        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    def close(self) -> None:
        if self.bus is not None:
            self.bus.disconnect()
            self.bus = None

    async def _call(self, destination: str, path: str, interface: str, member: str, signature: str = "", body=None):
        from dbus_fast import Message, MessageType

        message = Message(
            destination=destination, path=path, interface=interface, member=member, signature=signature, body=body or []
        )
        try:
            reply = await asyncio.wait_for(self.bus.call(message), BUS_CALL_SECONDS)
        except Exception:
            return None
        if reply.message_type != MessageType.METHOD_RETURN:
            return None
        return reply.body

    async def get_all(self, path: str, interface: str) -> dict | None:
        body = await self._call(BLUEZ, path, PROPERTIES, "GetAll", "s", [interface])
        return None if body is None else _plain(body[0])

    async def name_owner(self) -> str | None:
        """bluetoothd's unique bus name, or None while it is not running."""
        body = await self._call(BUS_NAME, "/org/freedesktop/DBus", BUS_NAME, "GetNameOwner", "s", [BLUEZ])
        return None if body is None else str(body[0])

    async def managed_objects(self) -> dict:
        body = await self._call(BLUEZ, "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
        if body is None:
            return {}
        return {str(path): {name: _plain(values) for name, values in interfaces.items()} for path, interfaces in body[0].items()}

    async def watch(self, adapter_path: str, changed: Callable[[str, object], None]) -> None:
        """Call changed("powered", value) when the adapter's Powered changes, changed("adapter", None) for any
        other adapter property, changed("owner", new_owner) when bluetoothd's bus name changes hands, and
        changed("sleep", going_to_sleep) around a suspend."""
        from dbus_fast import MessageType

        rules = (
            f"type='signal',interface='{PROPERTIES}',member='PropertiesChanged',path='{adapter_path}',arg0='{ADAPTER_INTERFACE}'",
            f"type='signal',sender='{BUS_NAME}',interface='{BUS_NAME}',member='NameOwnerChanged',arg0='{BLUEZ}'",
            f"type='signal',interface='{LOGIN_MANAGER}',member='PrepareForSleep'",
        )
        for rule in rules:
            await self._call(BUS_NAME, "/org/freedesktop/DBus", BUS_NAME, "AddMatch", "s", [rule])

        def handler(message) -> None:
            if message.message_type != MessageType.SIGNAL:
                return None
            if message.member == "PropertiesChanged" and message.path == adapter_path and message.body[0] == ADAPTER_INTERFACE:
                values = _plain(message.body[1])
                if "Powered" in values:
                    changed("powered", values["Powered"])
                else:
                    changed("adapter", None)
            elif message.member == "NameOwnerChanged" and message.body[0] == BLUEZ:
                changed("owner", message.body[2])
            elif message.member == "PrepareForSleep" and message.interface == LOGIN_MANAGER:
                changed("sleep", bool(message.body[0]))
            return None

        self.bus.add_message_handler(handler)


async def wait_for_adapter(bus: SystemBus, seconds: float) -> str:
    """The adapter to use, once bluetoothd answers and one is powered; reads again every `seconds` until then."""
    while (path := choose_adapter(await bus.managed_objects())) is None:
        await asyncio.sleep(seconds)
    return path


def scan_lost(adapter: dict | None) -> bool:
    """Active mode only: the adapter is up but nobody is discovering, so our scan has ended. While
    bluetoothd is down or the adapter is off, a fresh sidecar could not scan either, so that is not a loss
    yet. Another client's discovery (the Bluetooth panel, say) hides a loss until it stops."""
    return (adapter or {}).get("Powered") is True and (adapter or {}).get("Discovering") is False


class Watchdog:
    """Decides whether the scan (active) or the listening (passive) has been lost. Signals only wake the
    radio loop; every verdict comes from a fresh read, so the sidecar's own scanner stop around a connect is
    never taken for a loss."""

    def __init__(self, bus: SystemBus, adapter_path: str, wake: asyncio.Event, clock: Callable[[], float] = time.monotonic) -> None:
        self.bus = bus
        self.adapter_path = adapter_path
        self.wake = wake
        self.clock = clock
        self.owner: str | None = None
        self.power_was_off = False
        self.disturbed_at: float | None = None
        self.released_at: float | None = None
        self.resumed = False

    async def open(self) -> None:
        self.owner = await self.bus.name_owner()
        await self.bus.watch(self.adapter_path, self.changed)

    def changed(self, kind: str, value: object) -> None:
        if kind == "owner" or (kind == "powered" and value is False):
            self.disturbed_at = self.clock()
        if kind == "sleep" and value is False:
            # Whether a controller reset on resume keeps the monitors in the kernel is not known, and it may
            # leave Powered true and call no Release: start afresh after every resume.
            self.resumed = True
        if kind == "powered" and value is False:
            self.power_was_off = True
        self.wake.set()

    def released(self) -> None:
        """BlueZ called the passive monitor's Release."""
        if self.released_at is None:
            self.released_at = self.clock()
        self.wake.set()

    def _release_is_its_own(self) -> bool | None:
        """True once a Release has stood alone for the grace period; None while it might still be part of
        an owner change or power cycle; False when it was."""
        if self.released_at is None:
            return False
        if self.disturbed_at is not None and abs(self.released_at - self.disturbed_at) <= RELEASE_GRACE_SECONDS:
            return False
        if self.clock() - self.released_at < RELEASE_GRACE_SECONDS:
            return None
        return True

    async def loss(self, passive: bool) -> str | None:
        """The reason the scan or listening is lost, or None. A bluetoothd that is down or an adapter that is
        off is not a loss yet: a fresh sidecar could do no better until they are back."""
        owner = await self.bus.name_owner()
        if owner is None:
            return None
        if owner != self.owner:
            return "bluetoothd-restarted"
        if self.resumed:
            return "resumed-from-sleep"
        adapter = await self.bus.get_all(self.adapter_path, ADAPTER_INTERFACE)
        powered = (adapter or {}).get("Powered")
        if powered is False:
            if not self.power_was_off:
                self.disturbed_at = self.clock()
            self.power_was_off = True
            return None
        if powered is True and self.power_was_off:
            return "adapter-power-cycle"
        if not passive:
            return "not-discovering" if scan_lost(adapter) else None
        return "monitor-released" if self._release_is_its_own() else None
