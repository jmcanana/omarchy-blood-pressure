"""In-memory link for tests and the offline self-check."""

from __future__ import annotations

import asyncio

from pressure.blp import (
    AND_VENDOR_INDICATE,
    BATTERY,
    BP_FEATURE,
    BP_MEASUREMENT,
)


class FakeLink:
    def __init__(
        self,
        packets: list[tuple[str, bytes]],
        *,
        fail_reads: bool = False,
        uuids: list[str] | None = None,
        spacing: float = 0.02,
        responses: dict[str, bytes] | None = None,
    ) -> None:
        self.packets = packets
        self.fail_reads = fail_reads
        self.connected = True
        self.writes: list[tuple[str, bytes]] = []
        self.reads: list[str] = []
        self.events: list[str] = []
        self.spacing = spacing
        self.responses = {key.lower(): value for key, value in (responses or {}).items()}
        self.subscribed: list[str] = []
        self._uuids = uuids or [BP_MEASUREMENT, AND_VENDOR_INDICATE]
        self._callbacks: list[tuple[str, object]] = []
        self._pumped = False

    def is_connected(self) -> bool:
        return self.connected

    async def pair(self) -> None:
        return None

    def notify_uuids(self) -> list[str]:
        return list(self._uuids)

    async def subscribe(self, uuid: str, callback) -> None:
        self.subscribed.append(uuid.lower())
        self._callbacks.append((uuid.lower(), callback))
        if not self._pumped:
            self._pumped = True
            asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        await asyncio.sleep(0.02)
        for uuid, payload in self.packets:
            for subscribed, callback in list(self._callbacks):
                if subscribed == uuid.lower():
                    self.events.append("record")
                    callback(uuid, payload)
            await asyncio.sleep(self.spacing)

    async def read(self, uuid: str) -> bytes:
        self.reads.append(uuid.lower())
        self.events.append("read")
        if self.fail_reads:
            raise RuntimeError("unread")
        key = uuid.lower()
        if key in self.responses:
            return self.responses[key]
        if key[4:8] == "2a49":
            return bytes.fromhex("2f00")
        if key == BATTERY:
            return bytes([80])
        if key[4:8] == "2a29":
            return b"Example"
        return b""

    async def write(self, uuid: str, payload: bytes) -> None:
        self.writes.append((uuid.lower(), payload))


def measurement(
    systolic: int,
    diastolic: int,
    when: tuple[int, int, int, int, int, int],
    pulse: int,
    *,
    user: int = 1,
    status: int = 0,
    tail: bytes = b"",
    kpa: bool = False,
) -> bytes:
    """Build a flags-0x1e measurement. The mean is the SFLOAT +infinity special."""
    flags = 0x1F if kpa else 0x1E
    year, month, day, hour, minute, second = when
    body = bytes([flags])
    body += int(systolic).to_bytes(2, "little")
    body += int(diastolic).to_bytes(2, "little")
    body += (0x07FE).to_bytes(2, "little")
    body += int(year).to_bytes(2, "little")
    body += bytes([month, day, hour, minute, second, pulse & 0xFF, (pulse >> 8) & 0xFF, user])
    body += int(status).to_bytes(2, "little")
    return body + tail
