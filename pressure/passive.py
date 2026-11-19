"""Passive listening: one BlueZ advertisement monitor for the cuff's packets, and a Device1 read on each
match, so nothing but the cuff stirs BlueZ. This is the path the quiet-only run of November 19, 2026
proved: the monitors heard the START burst and the window with no discovery running."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from pressure.shadow import BLUEZ_CALL_SECONDS, PATTERNS
from pressure.sighting import Sighting, from_device1

# Company 105 (the START burst, the window, any other A&D device) and the transfer iBeacon's tail at its
# measured offset; the shadow trials and the quiet-only run matched both.
PASSIVE_PATTERNS = [PATTERNS["company"], PATTERNS["tail"]]
LABEL = "passive"
# BlueZ called Activate within 0.1 s in the quiet-only run; a monitor not active after this never will be.
ACTIVATE_SECONDS = 5.0


NOT_ACTIVATED = "not-activated"


class ListenRefused(RuntimeError):
    """The monitor could not be registered or never became active; the reason is the message."""


class PassiveListen:
    """Start and stop like a scanner. `heard` gets a Sighting for each match; `released` is called when
    BlueZ releases the monitor."""

    def __init__(
        self,
        registrar,
        read_device: Callable[[str], Awaitable[dict | None]],
        heard: Callable[[Sighting], None],
        released: Callable[[], None],
        activated: Callable[[], None],
    ) -> None:
        self.registrar = registrar
        self.read_device = read_device
        self.heard = heard
        self.on_released = released
        self.on_activated = activated
        self.handle = None
        self.active = asyncio.Event()
        self.reading: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        self.active.clear()
        try:
            await asyncio.wait_for(self.registrar.open(), BLUEZ_CALL_SECONDS)
            self.handle = await asyncio.wait_for(
                self.registrar.register(LABEL, PASSIVE_PATTERNS, self.found, self.released, self.activated),
                BLUEZ_CALL_SECONDS,
            )
        except Exception as exc:
            raise ListenRefused(getattr(exc, "name", type(exc).__name__)) from None
        try:
            await asyncio.wait_for(self.active.wait(), ACTIVATE_SECONDS)
        except TimeoutError:
            await self.stop()
            raise ListenRefused(NOT_ACTIVATED) from None

    @property
    def listening(self) -> bool:
        """Registered and not since stopped."""
        return self.handle is not None

    def activated(self, _label: str) -> None:
        self.active.set()
        self.on_activated()

    def released(self, _label: str) -> None:
        self.on_released()

    def found(self, _label: str, device_path: str) -> None:
        """Called on every matching advert. One read per device at a time: the read returns the latest
        values, and the next match after it starts another."""
        if device_path in self.reading:
            return
        self.reading[device_path] = asyncio.get_running_loop().create_task(self._read(device_path))

    async def _read(self, device_path: str) -> None:
        try:
            sighting = from_device1(await self.read_device(device_path))
        finally:
            self.reading.pop(device_path, None)
        if sighting is not None:
            self.heard(sighting)

    async def stop(self) -> None:
        for task in list(self.reading.values()):
            task.cancel()
        self.reading.clear()
        handle, self.handle = self.handle, None
        if handle is not None:
            try:
                await asyncio.wait_for(self.registrar.unregister(handle), BLUEZ_CALL_SECONDS)
            except Exception:
                pass
