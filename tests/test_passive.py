"""Passive listening, the adapter choice, signal-driven scan loss and the command ledger. Synthetic data only."""

from __future__ import annotations

import asyncio
import enum
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import test_pressure as base
from test_pressure import FakeBus, FakeRegistrar, _store

WatchdogTests = base.WatchdogTests

CUFF = "aa:bb:cc:dd:ee:01"
CUFF_PATH = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_01"
WINDOW = base.QuietScanTests.WINDOW
START = {105: bytes.fromhex("010501640080")}


def device1(manufacturer: dict, address: str = CUFF.upper(), name: str = "UA-810BLE") -> dict:
    return {"Address": address, "Name": name, "ManufacturerData": dict(manufacturer), "UUIDs": []}


class ActivatingRegistrar(FakeRegistrar):
    """A registrar whose monitor BlueZ activates at once; `armed(found, released)` runs at the first register."""

    def __init__(self, log: list[str], refuse: str = "", activate: bool = True, armed=None) -> None:
        super().__init__(log, refuse)
        self.activate = activate
        self.armed = armed
        self.registrations = 0

    async def register(self, label, pattern, found, released, activated):
        handle = await super().register(label, pattern, found, released, activated)
        self.registrations += 1
        if self.activate:
            activated(label)
        if self.armed and self.registrations == 1:
            self.armed(found, released)
        return handle


def run_passive(armed=None, seconds: float = 0.6, seed=None, refuse: str = "", activate: bool = True, **extra):
    """Run the radio loop in passive mode; `armed(found, released, bus, store)` runs once the monitor is up."""
    made: dict = {}

    def registrar(store, log, bus):
        made["registrar"] = ActivatingRegistrar(
            log, refuse=refuse, activate=activate,
            armed=(lambda found, released: armed(found, released, bus, store)) if armed else None,
        )
        return made["registrar"]

    watchdog = WatchdogTests()
    result, log, store = watchdog.run_radio(
        [WatchdogTests.UP], seconds=seconds, mode="passive", registrar=registrar, prepare=seed, **extra
    )
    return result, log, store, made.get("registrar"), watchdog.bus


def kinds(store) -> list[str]:
    return [row["kind"] for row in store.telemetry_public(False)]


def bodies(store, kind: str) -> list[dict]:
    return [row["body"] for row in store.telemetry_public(False) if row["kind"] == kind]


def scan_mode_file(store) -> str:
    return (store.state_dir / "scan-mode").read_text(encoding="utf-8").strip()


class SightingTests(unittest.TestCase):
    def test_both_sources_give_the_same_sighting(self) -> None:
        from pressure.sighting import Sighting, from_bleak, from_device1

        device = type("Device", (), {"address": "AA:BB:CC:DD:EE:01", "name": "UA-810BLE"})()
        advert = type("Advert", (), {"manufacturer_data": WINDOW, "service_uuids": ["00001810-0000-1000-8000-00805f9b34fb"], "local_name": ""})()
        variant = type("Variant", (), {})
        wrapped = {}
        for key, value in WINDOW.items():
            holder = variant()
            holder.value = value
            wrapped[key] = holder
        props = {"Address": "AA:BB:CC:DD:EE:01", "Name": "UA-810BLE", "ManufacturerData": wrapped,
                 "UUIDs": ["00001810-0000-1000-8000-00805f9b34fb"]}
        self.assertEqual(from_bleak(device, advert), from_device1(props))
        self.assertEqual(from_device1(props).manufacturer, WINDOW)
        named = type("Advert", (), {"manufacturer_data": None, "service_uuids": None, "local_name": "Cuff"})()
        self.assertEqual(from_bleak(device, named), Sighting("AA:BB:CC:DD:EE:01", "Cuff", {}, ()))

    def test_a_missing_or_addressless_read_is_no_sighting(self) -> None:
        from pressure.sighting import from_device1

        self.assertIsNone(from_device1(None))
        self.assertIsNone(from_device1({}))
        self.assertIsNone(from_device1({"Name": "x"}))
        self.assertEqual(from_device1({"Address": "a"}).manufacturer, {})


class PatternTests(unittest.TestCase):
    def test_the_passive_patterns_match_the_cuffs_bytes_where_they_sit(self) -> None:
        from pressure.passive import PASSIVE_PATTERNS

        apple_value = bytes.fromhex("4c000215") + bytes(range(16)) + bytes.fromhex("a1030001c3")
        company_value = bytes.fromhex("6900") + bytes.fromhex("020501640080")
        self.assertEqual(len(apple_value) - 2, 23)  # the Apple value as bleak reports it, after the company id
        self.assertEqual(len(PASSIVE_PATTERNS), 2)
        for value in (company_value, apple_value):
            matched = [p for p in PASSIVE_PATTERNS if p[1] == 0xFF and value[p[0]:p[0] + len(p[2])] == p[2]]
            self.assertEqual(len(matched), 1, value.hex())
        idle = bytes.fromhex("4c000215") + bytes(range(16)) + bytes.fromhex("32010040c3")
        self.assertFalse([p for p in PASSIVE_PATTERNS if idle[p[0]:p[0] + len(p[2])] == p[2]])

    def test_start_is_read_from_the_payload_whatever_the_apple_value(self) -> None:
        from pressure.policy import beacon_class, starts_measurement

        stale_idle = {105: bytes.fromhex("010501640080"), 76: bytes.fromhex("001122334455" + "32010040c3")}
        self.assertEqual(beacon_class(stale_idle), "idle")
        self.assertTrue(starts_measurement(stale_idle))
        self.assertFalse(starts_measurement(WINDOW))
        self.assertFalse(starts_measurement(None))


class AdapterTests(unittest.TestCase):
    def test_the_first_powered_central_adapter_in_path_order(self) -> None:
        from pressure.bluez import adapter_name, choose_adapter

        def adapter(powered: bool, roles=("central", "peripheral")) -> dict:
            values = {"Powered": powered}
            if roles is not None:
                values["Roles"] = list(roles)
            return {"org.bluez.Adapter1": values}

        objects = {
            "/org/bluez/hci10": adapter(True),
            "/org/bluez/hci2": adapter(True),
            "/org/bluez/hci0": adapter(True, roles=("peripheral",)),
            "/org/bluez/hci1": adapter(False),
            "/org/bluez/hci2/dev_AA": {"org.bluez.Device1": {}},
            "/org/bluez": {"org.bluez.AgentManager1": {}},
        }
        self.assertEqual(choose_adapter(objects), "/org/bluez/hci2")
        self.assertEqual(adapter_name(choose_adapter(objects)), "hci2")
        self.assertEqual(choose_adapter({"/org/bluez/hci3": adapter(True, roles=None)}), "/org/bluez/hci3")
        self.assertIsNone(choose_adapter({"/org/bluez/hci1": adapter(False)}))
        self.assertIsNone(choose_adapter({}))

    def test_the_chosen_adapter_reaches_scanner_client_and_watchdog(self) -> None:
        def trusted_window(callback, store) -> None:
            store.trust(CUFF, "Synthetic")
            device = type("Device", (), {"address": CUFF, "name": "UA-810BLE"})()
            advert = type("Advert", (), {"manufacturer_data": WINDOW, "service_uuids": [], "local_name": ""})()
            callback(device, advert)

        _result, log, store = WatchdogTests().run_radio([WatchdogTests.UP], seconds=2.0, fail_resume=True, trigger=trusted_window)
        self.assertIn("scanner:hci0", log)
        self.assertIn("client:hci0", log)
        self.assertIn("watch:/org/bluez/hci0", log)
        self.assertEqual(bodies(store, "adapter"), [{"mode": "active", "name": "hci0"}])

    def test_the_registrar_and_shadow_get_the_chosen_path(self) -> None:
        import pressure.radio as radio

        bus = types.SimpleNamespace(bus="the-bus")
        registrar = radio._default_registrar("/org/bluez/hci1", bus)
        self.assertEqual((registrar.adapter_path, registrar.bus, registrar.borrowed), ("/org/bluez/hci1", "the-bus", True))
        store, _root = _store()
        shadow = radio._default_shadow(store)("/org/bluez/hci1", bus)
        self.assertEqual(shadow.registrar.adapter_path, "/org/bluez/hci1")

    def test_with_no_powered_adapter_the_choice_waits_and_reads_again(self) -> None:
        from pressure.bluez import wait_for_adapter

        log: list[str] = []
        bus = FakeBus([None, WatchdogTests.OFF, WatchdogTests.UP], log)
        self.assertEqual(asyncio.run(wait_for_adapter(bus, 0.001)), "/org/bluez/hci0")
        self.assertEqual(log.count("ask"), 3)


def fake_dbus(replies: dict):
    """A dbus_fast stand-in: `replies` maps a member name to a reply body, an exception, or None (an error reply)."""
    module = types.ModuleType("dbus_fast")
    aio = types.ModuleType("dbus_fast.aio")

    class MessageType(enum.Enum):
        METHOD_RETURN = 1
        ERROR = 2
        SIGNAL = 3

    class Message:
        def __init__(self, **fields) -> None:
            self.__dict__.update(fields)

    class MessageBus:
        made: list = []

        def __init__(self, bus_type=None) -> None:
            self.calls: list = []
            self.handlers: list = []
            self.disconnected = False
            MessageBus.made.append(self)

        async def connect(self):
            return self

        async def call(self, message):
            self.calls.append(message)
            reply = replies.get(message.member)
            if isinstance(reply, BaseException):
                raise reply
            kind = MessageType.ERROR if reply is None else MessageType.METHOD_RETURN
            return types.SimpleNamespace(message_type=kind, body=reply)

        def add_message_handler(self, handler) -> None:
            self.handlers.append(handler)

        def disconnect(self) -> None:
            self.disconnected = True

    module.Message = Message
    module.MessageType = MessageType
    module.BusType = types.SimpleNamespace(SYSTEM=1)
    aio.MessageBus = MessageBus
    module.aio = aio
    return {"dbus_fast": module, "dbus_fast.aio": aio}, MessageType, MessageBus


def variant(value):
    return types.SimpleNamespace(value=value)


class SystemBusTests(unittest.TestCase):
    def test_reads_unwrap_and_a_refusal_or_failure_reads_as_none(self) -> None:
        from pressure.bluez import SystemBus

        replies = {
            "GetAll": [{"Powered": variant(True), "Discovering": variant(False)}],
            "GetNameOwner": [":1.7"],
            "GetManagedObjects": [{"/org/bluez/hci0": {"org.bluez.Adapter1": {"Powered": variant(True)}}}],
        }
        modules, _kind, made = fake_dbus(replies)

        async def run():
            bus = SystemBus()
            await bus.open()
            got = (await bus.get_all("/org/bluez/hci0", "org.bluez.Adapter1"), await bus.name_owner(), await bus.managed_objects(),
                   await bus.object_listed("/org/bluez/hci0"), await bus.object_listed("/org/bluez/hci1"))
            replies.update({"GetAll": None, "GetNameOwner": RuntimeError("gone"), "GetManagedObjects": None})
            missing = (await bus.get_all("/x", "y"), await bus.name_owner(), await bus.managed_objects(),
                       await bus.object_listed("/org/bluez/hci0"))
            bus.close()
            bus.close()
            return got, missing, bus

        with mock.patch.dict("sys.modules", modules):
            got, missing, bus = asyncio.run(run())
        self.assertEqual(got[0], {"Powered": True, "Discovering": False})
        self.assertEqual(got[1], ":1.7")
        self.assertEqual(got[2], {"/org/bluez/hci0": {"org.bluez.Adapter1": {"Powered": True}}})
        self.assertEqual(got[3:], (True, False))
        self.assertEqual(missing, (None, None, {}, None))
        self.assertTrue(made.made[-1].disconnected)
        self.assertIsNone(bus.bus)

    def test_watch_subscribes_and_turns_signals_into_changes(self) -> None:
        from pressure.bluez import SystemBus

        modules, kind, made = fake_dbus({"AddMatch": []})
        seen: list = []

        def signal(member, path="/", body=(), interface="org.freedesktop.DBus.Properties", message_type=None):
            return types.SimpleNamespace(message_type=message_type or kind.SIGNAL, member=member, path=path,
                                         body=list(body), interface=interface)

        async def run():
            bus = SystemBus()
            await bus.open()
            await bus.watch("/org/bluez/hci0", lambda what, value: seen.append((what, value)))
            handler = made.made[-1].handlers[0]
            for message in (
                signal("PropertiesChanged", "/org/bluez/hci0", ["org.bluez.Adapter1", {"Powered": variant(False)}, []]),
                signal("PropertiesChanged", "/org/bluez/hci0", ["org.bluez.Adapter1", {"Discovering": variant(True)}, []]),
                signal("PropertiesChanged", "/org/bluez/hci1", ["org.bluez.Adapter1", {"Powered": variant(True)}, []]),
                signal("PropertiesChanged", "/org/bluez/hci0", ["org.bluez.Device1", {"RSSI": variant(-60)}, []]),
                signal("NameOwnerChanged", "/org/freedesktop/DBus", ["org.bluez", ":1.7", ":1.9"], "org.freedesktop.DBus"),
                signal("NameOwnerChanged", "/org/freedesktop/DBus", ["org.other", ":1.2", ""], "org.freedesktop.DBus"),
                signal("PrepareForSleep", "/org/freedesktop/login1", [False], "org.freedesktop.login1.Manager"),
                signal("PrepareForSleep", "/org/freedesktop/login1", [True], "org.freedesktop.Other"),
                signal("GetAll", message_type=kind.METHOD_RETURN),
            ):
                self.assertIsNone(handler(message))
            return made.made[-1].calls

        with mock.patch.dict("sys.modules", modules):
            calls = asyncio.run(run())
        rules = [call.body[0] for call in calls if call.member == "AddMatch"]
        self.assertEqual(len(rules), 3)
        self.assertIn("path='/org/bluez/hci0'", rules[0])
        self.assertIn("arg0='org.bluez'", rules[1])
        self.assertIn("PrepareForSleep", rules[2])
        self.assertEqual(seen, [("powered", False), ("adapter", None), ("owner", ":1.9"), ("sleep", False)])


class StubBus:
    """The few reads the watchdog makes, set directly by a test."""

    def __init__(self) -> None:
        self.owner = ":1.7"
        self.adapter = {"Powered": True, "Discovering": True}
        self.listed: bool | None = True
        self.changed = None

    async def object_listed(self, _path):
        return self.listed

    async def name_owner(self):
        return self.owner

    async def get_all(self, _path, _interface):
        return self.adapter

    async def watch(self, _path, changed) -> None:
        self.changed = changed


class WatchdogRuleTests(unittest.TestCase):
    def make(self):
        from pressure.bluez import Watchdog

        now = {"t": 100.0}
        bus = StubBus()
        wake = asyncio.Event()
        watchdog = Watchdog(bus, "/org/bluez/hci0", wake, clock=lambda: now["t"])
        asyncio.run(watchdog.open())
        return watchdog, bus, wake, now

    def loss(self, watchdog, passive: bool):
        return asyncio.run(watchdog.loss(passive))

    def test_owner_rules(self) -> None:
        watchdog, bus, _wake, _now = self.make()
        self.assertIsNone(self.loss(watchdog, True))
        bus.owner = None
        self.assertIsNone(self.loss(watchdog, True))
        bus.owner = ":1.9"
        self.assertEqual(self.loss(watchdog, False), "bluetoothd-restarted")

    def test_a_power_cycle_seen_by_signal_or_by_poll(self) -> None:
        for by_signal in (True, False):
            watchdog, bus, wake, _now = self.make()
            if by_signal:
                bus.changed("powered", False)
                self.assertTrue(wake.is_set())
            bus.adapter = {"Powered": False, "Discovering": False}
            self.assertIsNone(self.loss(watchdog, True))
            bus.adapter = {"Powered": True, "Discovering": False}
            self.assertEqual(self.loss(watchdog, True), "adapter-power-cycle")

    def test_discovering_counts_only_in_active_mode(self) -> None:
        watchdog, bus, _wake, _now = self.make()
        bus.adapter = {"Powered": True, "Discovering": False}
        self.assertEqual(self.loss(watchdog, False), "not-discovering")
        self.assertIsNone(self.loss(watchdog, True))
        bus.changed("adapter", None)
        self.assertIsNone(self.loss(watchdog, True))

    def test_an_adapter_gone_from_the_bus_is_a_loss_in_both_modes(self) -> None:
        for passive in (True, False):
            watchdog, bus, _wake, _now = self.make()
            bus.adapter = None
            bus.listed = False
            self.assertEqual(self.loss(watchdog, passive), "adapter-gone")

    def test_a_failed_read_of_a_listed_or_unknown_adapter_is_not_a_loss(self) -> None:
        for listed in (True, None):
            watchdog, bus, _wake, _now = self.make()
            bus.adapter = None
            bus.listed = listed
            self.assertIsNone(self.loss(watchdog, True), listed)

    def test_a_resume_from_sleep_is_a_loss(self) -> None:
        watchdog, bus, wake, _now = self.make()
        bus.changed("sleep", True)
        self.assertIsNone(self.loss(watchdog, True))
        bus.changed("sleep", False)
        self.assertTrue(wake.is_set())
        self.assertEqual(self.loss(watchdog, False), "resumed-from-sleep")

    def test_a_release_alone_falls_back_only_after_the_grace(self) -> None:
        from pressure.bluez import RELEASE_GRACE_SECONDS

        watchdog, _bus, wake, now = self.make()
        watchdog.released()
        watchdog.released()
        self.assertTrue(wake.is_set())
        now["t"] += RELEASE_GRACE_SECONDS - 0.1
        self.assertIsNone(self.loss(watchdog, True))
        now["t"] += 0.2
        self.assertEqual(self.loss(watchdog, True), "monitor-released")
        self.assertIsNone(self.loss(watchdog, False))

    def test_a_release_beside_a_power_off_or_owner_change_is_that_loss(self) -> None:
        from pressure.bluez import RELEASE_GRACE_SECONDS

        for cause, offset in (("powered", 9.9), ("powered", -9.9), ("owner", 5.0), ("owner", -5.0)):
            watchdog, bus, _wake, now = self.make()
            if offset < 0:
                bus.changed(cause, False if cause == "powered" else ":1.9")
                now["t"] -= offset
                watchdog.released()
            else:
                watchdog.released()
                now["t"] += offset
                bus.changed(cause, False if cause == "powered" else ":1.9")
            now["t"] += RELEASE_GRACE_SECONDS + 1
            self.assertNotEqual(self.loss(watchdog, True), "monitor-released", (cause, offset))
        watchdog, bus, _wake, now = self.make()
        watchdog.released()
        now["t"] += RELEASE_GRACE_SECONDS + 0.1
        bus.changed("powered", False)
        bus.changed("powered", True)
        bus.adapter = {"Powered": True, "Discovering": False}
        self.assertEqual(self.loss(watchdog, True), "adapter-power-cycle")


class PassiveListenTests(unittest.TestCase):
    def make(self, refuse: str = "", activate: bool = True, devices=None):
        from pressure.passive import PassiveListen

        log: list[str] = []
        heard: list = []
        events: list[str] = []
        registrar = ActivatingRegistrar(log, refuse=refuse, activate=activate)

        async def read(path):
            log.append(f"read:{path}")
            await asyncio.sleep(0)
            return (devices or {}).get(path)

        listen = PassiveListen(registrar, read, heard.append, lambda: events.append("released"), lambda: events.append("active"))
        return listen, registrar, log, heard, events

    def test_start_registers_both_patterns_and_waits_for_activation(self) -> None:
        from pressure.passive import LABEL, PASSIVE_PATTERNS

        listen, registrar, log, _heard, events = self.make()
        asyncio.run(listen.start())
        self.assertTrue(listen.listening)
        self.assertEqual(registrar.patterns[LABEL], PASSIVE_PATTERNS)
        self.assertEqual(events, ["active"])
        registrar.callbacks[LABEL][1](LABEL)
        self.assertEqual(events, ["active", "released"])
        asyncio.run(listen.stop())
        asyncio.run(listen.stop())
        self.assertFalse(listen.listening)
        self.assertEqual(log.count("disarm:passive"), 1)

    def test_a_refusal_or_no_activation_is_a_refusal(self) -> None:
        import pressure.passive as passive

        listen, _registrar, _log, _heard, _events = self.make(refuse="passive")
        with self.assertRaises(passive.ListenRefused) as refused:
            asyncio.run(listen.start())
        self.assertEqual(str(refused.exception), "org.bluez.Error.Failed")
        listen, _registrar, log, _heard, _events = self.make(activate=False)
        with mock.patch.object(passive, "ACTIVATE_SECONDS", 0.05), self.assertRaises(passive.ListenRefused) as silent:
            asyncio.run(listen.start())
        self.assertEqual(str(silent.exception), passive.NOT_ACTIVATED)
        self.assertIn("disarm:passive", log)
        self.assertFalse(listen.listening)

    def test_one_read_per_device_at_a_time_and_a_failed_read_is_a_miss(self) -> None:
        other = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_09"
        listen, _registrar, log, heard, _events = self.make(devices={CUFF_PATH: device1(WINDOW)})

        async def run():
            await listen.start()
            for _ in range(5):
                listen.found("passive", CUFF_PATH)
            listen.found("passive", other)
            await asyncio.sleep(0.05)
            counts = (log.count(f"read:{CUFF_PATH}"), log.count(f"read:{other}"), len(heard))
            listen.found("passive", CUFF_PATH)
            await asyncio.sleep(0.05)
            counts += (log.count(f"read:{CUFF_PATH}"), len(heard))
            listen.found("passive", CUFF_PATH)
            await listen.stop()
            await asyncio.sleep(0.05)
            return counts + (log.count(f"read:{CUFF_PATH}"), len(heard), listen.reading)

        counts = asyncio.run(run())
        # Five matches while a read is in flight make one read; the unknown path's empty read is a miss; a read
        # still pending at stop is cancelled.
        self.assertEqual(counts, (1, 1, 1, 2, 2, 2, 2, {}))
        self.assertEqual(heard[0].manufacturer, WINDOW)


class PassiveRadioTests(unittest.TestCase):
    """The radio loop in passive mode, with a registrar BlueZ activates and Device1 reads from the fake bus."""

    def test_a_window_heard_passively_syncs_through_the_unchanged_connect_path(self) -> None:
        def armed(found, _released, bus, store):
            store.trust(CUFF, "Synthetic")
            bus.devices[CUFF_PATH] = device1(WINDOW)
            found("passive", CUFF_PATH)

        result, log, store, registrar, _bus = run_passive(armed, seconds=0.5)
        self.assertEqual(result, "kept-scanning")
        # The monitor is unregistered before the client, and registered again after the connect failed.
        self.assertLess(log.index("disarm:passive"), log.index("client:hci0"))
        self.assertGreater(registrar.registrations, 1)
        self.assertGreater(len(log) - log[::-1].index("arm:passive"), log.index("client:hci0"))
        self.assertIn("connect-start", kinds(store))
        self.assertEqual(bodies(store, "connect-end")[-1]["outcome"], "failed")
        self.assertIn({"monitor": "passive"}, bodies(store, "monitor-active"))
        self.assertNotIn("scanner:hci0", log)

    def test_a_timed_out_connect_also_registers_the_monitor_again(self) -> None:
        def armed(found, _released, bus, store):
            store.trust(CUFF, "Synthetic")
            bus.devices[CUFF_PATH] = device1(WINDOW)
            found("passive", CUFF_PATH)

        _result, _log, store, registrar, _bus = run_passive(armed, seconds=0.5, connect_error=TimeoutError())
        self.assertEqual(bodies(store, "connect-end")[-1]["outcome"], "timeout")
        self.assertGreater(registrar.registrations, 1)

    def test_the_same_adverts_give_the_same_decisions_on_both_paths(self) -> None:
        cases = {
            "trusted window": (True, WINDOW, "connect-start"),
            "untrusted window": (False, WINDOW, "seen"),
            "trusted start": (True, START, "ready"),
        }
        for name, (trusted, manufacturer, expect) in cases.items():
            def armed(found, _released, bus, store, trusted=trusted, manufacturer=manufacturer):
                if trusted:
                    store.trust(CUFF, "Synthetic")
                bus.devices[CUFF_PATH] = device1(manufacturer)
                found("passive", CUFF_PATH)

            def trigger(callback, store, trusted=trusted, manufacturer=manufacturer):
                if trusted:
                    store.trust(CUFF, "Synthetic")
                device = type("Device", (), {"address": CUFF.upper(), "name": "UA-810BLE"})()
                advert = type("Advert", (), {"manufacturer_data": manufacturer, "service_uuids": [], "local_name": ""})()
                # After the scan has started, as a passive match arrives after the monitor is up.
                asyncio.get_running_loop().call_soon(callback, device, advert)

            outcomes = []
            for passive in (True, False):
                if passive:
                    _result, _log, store, _registrar, _bus = run_passive(armed, seconds=0.3, fail_resume=True)
                else:
                    _result, _log, store = WatchdogTests().run_radio([WatchdogTests.UP], seconds=0.3, fail_resume=True, trigger=trigger)
                status = json.loads(store.status_path.read_text(encoding="utf-8"))
                outcomes.append(("connect-start" in kinds(store), store.pending_public() is not None, status["beacon"]))
                if expect == "connect-start":
                    self.assertTrue(outcomes[-1][0], name)
                if expect == "seen":
                    self.assertTrue(outcomes[-1][1], name)
            self.assertEqual(outcomes[0], outcomes[1], name)

    def test_a_refused_or_silent_monitor_falls_back_to_active(self) -> None:
        import pressure.passive as passive

        result, _log, store, _registrar, _bus = run_passive(refuse="passive")
        self.assertEqual(result, "lost")
        self.assertEqual(scan_mode_file(store), "active")
        self.assertEqual(bodies(store, "scan-mode-fallback"), [{"reason": "monitor-refused"}])
        self.assertEqual(oct((store.state_dir / "scan-mode").stat().st_mode & 0o777), "0o600")
        with mock.patch.object(passive, "ACTIVATE_SECONDS", 0.02):
            result, _log, store, _registrar, _bus = run_passive(activate=False)
        self.assertEqual(result, "lost")
        self.assertEqual(bodies(store, "scan-mode-fallback"), [{"reason": "not-activated"}])

    def test_three_passive_losses_in_ten_minutes_fall_back_and_two_do_not(self) -> None:
        for earlier, falls_back in ((3, True), (2, False)):
            def seed(store, earlier=earlier):
                for _ in range(earlier):
                    store.add_telemetry(None, "scan-lost", {"mode": "passive", "reason": "bluetoothd-restarted"})
                store.add_telemetry(None, "scan-lost", {"mode": "active", "reason": "not-discovering"})

            result, _log, store, _registrar, _bus = run_passive(seed=seed, seconds=0.2)
            self.assertEqual(result, "lost" if falls_back else "kept-scanning", earlier)
            self.assertEqual(bool(bodies(store, "scan-mode-fallback")), falls_back, earlier)

    def test_a_release_with_nothing_beside_it_falls_back(self) -> None:
        import pressure.bluez as bluez

        def armed(_found, released, _bus, _store):
            released("passive")

        with mock.patch.object(bluez, "RELEASE_GRACE_SECONDS", 0.05):
            result, _log, store, _registrar, _bus = run_passive(armed, seconds=1.0)
        self.assertEqual(result, "lost")
        self.assertEqual(bodies(store, "scan-mode-fallback"), [{"reason": "monitor-released"}])

    def test_an_owner_change_is_a_counted_loss_not_a_fallback(self) -> None:
        def armed(_found, _released, bus, _store):
            bus.owner = ":1.99"
            bus.changed("owner", ":1.99")

        result, _log, store, _registrar, _bus = run_passive(armed, seconds=0.5)
        self.assertEqual(result, "lost")
        self.assertEqual(bodies(store, "scan-lost"), [{"mode": "passive", "reason": "bluetoothd-restarted"}])
        self.assertFalse(bodies(store, "scan-mode-fallback"))

    def test_a_monitor_that_stopped_outside_a_connect_is_a_loss(self) -> None:
        import pressure.radio as radio

        original = radio.PassiveListen.start

        async def start_then_vanish(self):
            await original(self)
            self.handle = None

        with mock.patch.object(radio.PassiveListen, "start", start_then_vanish):
            result, _log, store, _registrar, _bus = run_passive(seconds=0.5)
        self.assertEqual(result, "lost")
        self.assertEqual(bodies(store, "scan-lost"), [{"mode": "passive", "reason": "not-listening"}])

    def test_one_missed_window_records_and_a_second_in_a_row_falls_back(self) -> None:
        import pressure.radio as radio

        def armed(found, _released, bus, store):
            store.trust(CUFF, "Synthetic")
            bus.devices[CUFF_PATH] = device1(START)
            found("passive", CUFF_PATH)

        with mock.patch.object(radio, "WINDOW_WAIT_SECONDS", 0.05):
            result, _log, store, _registrar, _bus = run_passive(armed, seconds=0.4)
            self.assertEqual(result, "kept-scanning")
            self.assertEqual(kinds(store).count("window-missed"), 1)
            self.assertFalse(bodies(store, "scan-mode-fallback"))

            def seed(store):
                store.add_telemetry(None, "connect-start", {"beacon": "transfer"})
                store.add_telemetry(None, "window-missed", {"after_seconds": 600})

            result, _log, store, _registrar, _bus = run_passive(armed, seed=seed, seconds=0.4)
            self.assertEqual(result, "lost")
            self.assertEqual(bodies(store, "scan-mode-fallback"), [{"reason": "window-missed"}])

    def test_a_window_after_start_or_an_untrusted_start_arms_no_miss(self) -> None:
        import pressure.radio as radio

        def window_follows(found, _released, bus, store):
            store.trust(CUFF, "Synthetic")
            bus.devices[CUFF_PATH] = device1(START)
            found("passive", CUFF_PATH)

            async def later():
                await asyncio.sleep(0.02)
                bus.devices[CUFF_PATH] = device1(WINDOW)
                found("passive", CUFF_PATH)

            asyncio.get_running_loop().create_task(later())

        def untrusted_start(found, _released, bus, _store):
            bus.devices[CUFF_PATH] = device1(START)
            found("passive", CUFF_PATH)

        with mock.patch.object(radio, "WINDOW_WAIT_SECONDS", 0.15):
            for armed in (window_follows, untrusted_start):
                _result, _log, store, _registrar, _bus = run_passive(armed, seconds=0.5, fail_resume=True)
                self.assertNotIn("window-missed", kinds(store), armed.__name__)

    def test_active_mode_restarts_on_a_signalled_owner_change_at_once(self) -> None:
        def trigger(_callback, store) -> None:
            pass

        class SignallingBus(FakeBus):
            async def watch(self, adapter_path, changed) -> None:
                await super().watch(adapter_path, changed)
                self.owner = ":1.50"
                changed("owner", ":1.50")

        # The poll is 30 s, so only the signal can end this run within 0.5 s.
        result, _log, store = WatchdogTests().run_radio(
            [WatchdogTests.UP], seconds=0.5, trigger=trigger, bus=SignallingBus, check_seconds=30.0
        )
        self.assertEqual(result, "lost")
        self.assertEqual(bodies(store, "scan-lost"), [{"mode": "active", "reason": "bluetoothd-restarted"}])


class ModeChoiceTests(unittest.TestCase):
    def test_passive_needs_a_cuff_and_only_cuffs_trusted_from_the_window(self) -> None:
        from pressure.sidecar import choose_mode

        store, _root = _store()
        self.assertEqual(choose_mode(store), ("active", {"mode": "active"}))
        (store.state_dir / "scan-mode").write_text("passive\n", encoding="utf-8")
        self.assertEqual(choose_mode(store), ("active", {"mode": "active", "wanted": "passive", "why": "no-trusted-cuff"}))
        store.trust("aa:00:00:00:00:01", "Old cuff")
        self.assertEqual(choose_mode(store), ("active", {"mode": "active", "wanted": "passive", "why": "beacon-unknown"}))
        store.trust("aa:00:00:00:00:01", beacon="transfer")
        self.assertEqual(choose_mode(store), ("passive", {"mode": "passive"}))
        store.trust("aa:00:00:00:00:02", "Cuff", beacon="transfer")
        self.assertEqual(choose_mode(store)[0], "passive")
        store.trust("aa:00:00:00:00:03", "Other monitor", beacon="generic")
        self.assertEqual(choose_mode(store), ("active", {"mode": "active", "wanted": "passive", "why": "generic-monitor"}))
        (store.state_dir / "scan-mode").write_text("shadow\n", encoding="utf-8")
        self.assertEqual(choose_mode(store), ("shadow", {"mode": "shadow"}))

    def test_the_status_names_the_mode(self) -> None:
        store, _root = _store()
        store.write_status(state="idle")
        self.assertEqual(json.loads(store.status_path.read_text(encoding="utf-8"))["scan"], "none")
        store.scan_mode = "passive"
        store.write_status(state="scanning")
        self.assertEqual(json.loads(store.status_path.read_text(encoding="utf-8"))["scan"], "passive")


class StoreCountTests(unittest.TestCase):
    def test_counts_by_kind_body_and_age(self) -> None:
        store, _root = _store()
        store.add_telemetry(None, "scan-lost", {"mode": "passive", "reason": "a"})
        store.add_telemetry(None, "scan-lost", {"mode": "active", "reason": "b"})
        store._db.execute("INSERT INTO telemetry(address, at, kind, body) VALUES(NULL, '2020-01-01T00:00:00+00:00', 'scan-lost', ?)",
                          (json.dumps({"mode": "passive"}),))
        self.assertEqual(store.count_telemetry_since("scan-lost", 600, mode="passive"), 1)
        self.assertEqual(store.count_telemetry_since("scan-lost", 600), 2)
        self.assertEqual(store.count_telemetry_since("window-missed", 600), 0)

    def test_misses_in_a_row_stop_at_a_connect(self) -> None:
        store, _root = _store()
        self.assertEqual(store.windows_missed_in_a_row(), 0)
        store.add_telemetry(None, "window-missed", {})
        store.add_telemetry(None, "connect-start", {})
        store.add_telemetry(None, "window-missed", {})
        store.add_telemetry(None, "scan-lost", {})
        store.add_telemetry(None, "window-missed", {})
        self.assertEqual(store.windows_missed_in_a_row(), 2)

    def test_trusted_beacons(self) -> None:
        store, _root = _store()
        self.assertEqual(store.trusted_beacons(), [])
        store.trust("aa:00:00:00:00:02", "B", beacon="generic")
        store.trust("aa:00:00:00:00:01", "A")
        self.assertEqual(store.trusted_beacons(), ["", "generic"])


class LedgerTests(unittest.TestCase):
    def test_a_change_runs_once_and_a_read_runs_again(self) -> None:
        from pressure.sidecar import CommandLedger, _run_command

        store, _root = _store()
        ledger = CommandLedger(store.state_dir / "commands.done")
        first = _run_command(store, ledger, {"op": "add_patient", "alias": "North", "id": "s-1"})
        again = _run_command(store, ledger, {"op": "add_patient", "alias": "North", "id": "s-1"})
        self.assertTrue(first["ok"])
        self.assertEqual(again, {"ok": True, "error": "", "repeated": True})
        self.assertEqual(len(store.patients()), 1)
        # After a restart the record is read back from its file.
        reborn = CommandLedger(store.state_dir / "commands.done")
        self.assertEqual(_run_command(store, reborn, {"op": "add_patient", "alias": "North", "id": "s-1"})["repeated"], True)
        self.assertEqual(len(store.patients()), 1)
        self.assertEqual(oct((store.state_dir / "commands.done").stat().st_mode & 0o777), "0o600")
        self.assertIn("patients", _run_command(store, reborn, {"op": "patients", "id": "s-2"}))
        self.assertIn("patients", _run_command(store, reborn, {"op": "patients", "id": "s-2"}))

    def test_a_failed_change_replays_its_failure(self) -> None:
        from pressure.sidecar import CommandLedger, _run_command

        store, _root = _store()
        ledger = CommandLedger(store.state_dir / "commands.done")
        first = _run_command(store, ledger, {"op": "rename_patient", "patient": 99, "alias": "x", "id": "s-3"})
        self.assertFalse(first["ok"])
        replay = _run_command(store, CommandLedger(store.state_dir / "commands.done"), {"op": "rename_patient", "id": "s-3"})
        self.assertEqual(replay, {"ok": False, "error": first["error"], "repeated": True})
        broken = _run_command(store, ledger, {"op": "nonsense", "id": "s-4"})
        self.assertFalse(broken["ok"])
        crashed = _run_command(store, ledger, {"op": "set_under_13", "id": "s-5"})
        self.assertEqual(crashed, {"ok": False, "error": "command failed"})

    def test_the_ledger_keeps_the_last_64_and_survives_a_bad_file(self) -> None:
        from pressure.sidecar import LEDGER_KEEP, CommandLedger

        path = Path(tempfile.mkdtemp()) / "commands.done"
        path.write_text("not json", encoding="utf-8")
        ledger = CommandLedger(path)
        self.assertEqual(ledger.entries, [])
        for number in range(LEDGER_KEEP + 3):
            ledger.record(f"s-{number}", {"ok": True})
        reborn = CommandLedger(path)
        self.assertEqual(len(reborn.entries), LEDGER_KEEP)
        self.assertIsNone(reborn.outcome("s-0"))
        self.assertEqual(reborn.outcome(f"s-{LEDGER_KEEP + 2}"), {"ok": True, "error": ""})
        self.assertIsNone(CommandLedger(path).outcome("missing"))

    def test_a_command_file_that_is_not_json_gets_an_error(self) -> None:
        from pressure import sidecar

        store, _root = _store()
        state = store.state_dir
        (state / "command.json").write_text("{", encoding="utf-8")

        async def run():
            task = asyncio.ensure_future(sidecar._commands_clean(store, state, {}))
            for _ in range(50):
                await asyncio.sleep(0.02)
                if (state / "response.json").exists():
                    break
            task.cancel()

        asyncio.run(run())
        response = json.loads((state / "response.json").read_text(encoding="utf-8"))
        self.assertEqual(response, {"ok": False, "error": "command was not JSON", "id": ""})
        (state / "command.json").write_text("[1, 2]", encoding="utf-8")
        asyncio.run(run())
        response = json.loads((state / "response.json").read_text(encoding="utf-8"))
        self.assertEqual(response["id"], "")
        self.assertFalse(response["ok"])


class PanelChannelTests(unittest.TestCase):
    PANEL = Path(__file__).resolve().parent.parent / "Panel.qml"

    def test_the_guard_resends_the_same_command_with_a_growing_wait(self) -> None:
        text = self.PANEL.read_text(encoding="utf-8")
        self.assertIn("      if (root.inflightBody) {\n        root.queue = [root.inflightBody].concat(root.queue)\n", text)
        self.assertIn("        root.guardDelay = Math.min(root.guardDelay * 2, 32000)\n", text)
        self.assertIn("    if (!body.id) {\n", text)
        self.assertIn("      root.guardDelay = 4000\n", text)
        self.assertIn("    flushGuard.interval = root.guardDelay\n", text)
        self.assertIn("    root.inflightBody = null\n", text)

    def test_the_device_strip_names_the_mode(self) -> None:
        text = self.PANEL.read_text(encoding="utf-8")
        self.assertIn('({ active: "Full scan", passive: "Quiet listening", shadow: "Shadow trial" })[root.scanMode]', text)
        self.assertIn('root.scanMode = doc.scan || ""', text)
        self.assertIn('(root.scanLabel ? " · " + root.scanLabel : "")', text)


class RegistrarTests(unittest.TestCase):
    def test_a_borrowed_bus_is_neither_opened_nor_closed(self) -> None:
        from pressure.shadow import BluezRegistrar

        bus = types.SimpleNamespace(disconnect=lambda: self.fail("closed a borrowed bus"))
        registrar = BluezRegistrar("/org/bluez/hci0", bus)
        asyncio.run(registrar.open())
        asyncio.run(registrar.close())
        self.assertIs(registrar.bus, bus)

    def test_a_list_of_patterns_becomes_one_monitor(self) -> None:
        from pressure.shadow import BluezRegistrar

        modules, _kind, _made = fake_dbus({"RegisterMonitor": []})
        service = types.ModuleType("dbus_fast.service")
        service.method = lambda: (lambda function: function)
        made: list = []

        class AdvertisementMonitor:
            def __init__(self, patterns) -> None:
                made.append(patterns)

        monitor_module = types.ModuleType("bleak.backends.bluezdbus.advertisement_monitor")
        monitor_module.AdvertisementMonitor = AdvertisementMonitor
        for parent in ("bleak", "bleak.backends", "bleak.backends.bluezdbus"):
            modules[parent] = types.ModuleType(parent)
        exported: list = []
        bus = types.SimpleNamespace(
            call=modules["dbus_fast.aio"].MessageBus().call, export=lambda path, monitor: exported.append(path),
        )
        modules.update({"dbus_fast.service": service, "bleak.backends.bluezdbus.advertisement_monitor": monitor_module})
        noop = lambda *_args: None  # noqa: E731
        with mock.patch.dict("sys.modules", modules):
            registrar = BluezRegistrar("/org/bluez/hci0", bus)
            asyncio.run(registrar.register("passive", [(0, 255, b"\x69\x00"), (20, 255, b"\xa1")], noop, noop, noop))
            asyncio.run(registrar.register("tail", (20, 255, b"\xa1"), noop, noop, noop))
        self.assertEqual(made, [[(0, 255, b"\x69\x00"), (20, 255, b"\xa1")], [(20, 255, b"\xa1")]])
        self.assertEqual(len(exported), 2)
        self.assertTrue(all(path.startswith(f"/org/jmcanana/bloodpressure/{os.getpid()}/") for path in exported))


class ConnectPathTests(unittest.TestCase):
    """The connect path the modes share, now that it is its own function."""

    def window(self, store=None):
        def trigger(callback, store_) -> None:
            store_.trust(CUFF, "Synthetic")
            device = type("Device", (), {"address": CUFF, "name": "UA-810BLE"})()
            advert = type("Advert", (), {"manufacturer_data": WINDOW, "service_uuids": [], "local_name": ""})()
            if "start" in getattr(store_, "_starts", ""):
                return
            store_._starts = "start"
            callback(device, advert)
            callback(device, advert)  # a second advert while the first is pending is ignored

        return trigger

    def client(self, log, pair=None):
        class Client:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            async def __aenter__(self):
                log.append("connect")
                return types.SimpleNamespace(is_connected=True, mtu_size=185, pair=pair or self.paired)

            async def paired(self) -> None:
                return None

            async def __aexit__(self, *_exc) -> None:
                return None

        return Client

    def test_a_good_sync_drains_records_and_resumes(self) -> None:
        import pressure.radio as radio

        log: list[str] = []

        async def drained(_link, _store, _address, clock_source=None):
            log.append(f"drain:{clock_source is None}")
            return {"cuff_released": True, "stored": 1}

        with mock.patch.object(radio, "drain", drained):
            result, run_log, store = WatchdogTests().run_radio(
                [WatchdogTests.UP], seconds=0.3, trigger=self.window(), client=self.client(log)
            )
            WatchdogTests().run_radio(
                [WatchdogTests.UP], seconds=0.3, trigger=self.window(), client=self.client(log), synced=True
            )
        self.assertEqual(result, "kept-scanning")
        # Unsynchronized, the clock is never written (no source); synchronized, it is offered.
        self.assertEqual(log, ["connect", "drain:True", "connect", "drain:False"])
        # The window sync records the beacon of a cuff trusted without one, so passive can use it.
        self.assertEqual(store.trusted_beacon(CUFF), "transfer")
        self.assertEqual(bodies(store, "connect-end")[-1]["outcome"], "ok")
        self.assertIn("disconnect", kinds(store))
        self.assertGreaterEqual(run_log.count("start"), 2)

    def test_a_pair_that_hangs_times_out_and_resumes(self) -> None:
        import pressure.radio as radio

        log: list[str] = []

        async def hang() -> None:
            await asyncio.sleep(10)

        with mock.patch.object(radio, "CONNECT_TIMEOUT_SECONDS", 0.05):
            _result, _log, store = WatchdogTests().run_radio(
                [WatchdogTests.UP], seconds=0.4, trigger=self.window(), client=self.client(log, pair=hang)
            )
        self.assertEqual(bodies(store, "connect-end")[-1]["outcome"], "pair-timeout")

    def test_a_scanner_that_will_not_stop_is_recorded_and_the_sync_goes_on(self) -> None:
        result, log, store = WatchdogTests().run_radio(
            [WatchdogTests.UP], seconds=0.3, trigger=self.window(), fail_stop=True
        )
        self.assertEqual(result, "kept-scanning")
        self.assertEqual(bodies(store, "scan-stop-failed"), [{"error": "RuntimeError"}])
        self.assertIn("connect", log)

    def test_adverts_that_may_not_take_the_link(self) -> None:
        generic_advert = {}

        def trigger(callback, store) -> None:
            store.trust(CUFF, "Synthetic", beacon="generic")
            device = type("Device", (), {"address": CUFF, "name": "UA-810BLE"})()
            for manufacturer in (WINDOW, generic_advert):
                advert = type("Advert", (), {"manufacturer_data": manufacturer, "service_uuids": [], "local_name": ""})()
                callback(device, advert)

        _result, log, store = WatchdogTests().run_radio([WatchdogTests.UP], seconds=0.2, trigger=trigger)
        self.assertNotIn("connect", log)
        self.assertNotIn("connect-start", kinds(store))

    def test_the_shadow_ticks_while_waiting_and_arms_again_after_a_sync(self) -> None:
        from pressure.shadow import Shadow

        ticks: list[int] = []

        class CountingShadow(Shadow):
            async def tick(self) -> None:
                ticks.append(1)
                await super().tick()

        _result, log, _store = WatchdogTests().run_radio(
            [WatchdogTests.UP], seconds=0.4, trigger=self.window(),
            shadow=lambda store, log: CountingShadow(store, FakeRegistrar(log)),
        )
        self.assertTrue(ticks)
        self.assertGreaterEqual(log.count("arm:company"), 2)


class SyncErrorTests(unittest.TestCase):
    def test_an_error_around_the_connect_is_a_counted_loss(self) -> None:
        def armed(found, _released, bus, store):
            store.trust(CUFF, "Synthetic")
            bus.devices[CUFF_PATH] = device1(WINDOW)
            found("passive", CUFF_PATH)

        store_holder: list = []

        def keep(store):
            store_holder.append(store)

        with self.assertRaises(OSError):
            run_passive(armed, seed=keep, seconds=0.5, clock_error=OSError("timedate1 went away"))
        self.assertEqual(bodies(store_holder[0], "scan-lost"), [{"mode": "passive", "reason": "sync-error"}])
        self.assertNotIn("connect-start", kinds(store_holder[0]))


class OwnStopTests(unittest.TestCase):
    def test_the_scans_own_stop_around_a_connect_is_not_a_loss(self) -> None:
        """While the sidecar connects its own scan is stopped, so Discovering reads false and a signal says so;
        just after the scan starts again it may still read false for a moment. Neither may end the run."""
        import time as clock

        import pressure.radio as radio

        made: dict = {}

        class LagBus(FakeBus):
            def __init__(self, states, log) -> None:
                super().__init__(states, log)
                self.connecting = False
                self.ended = -1.0
                made["bus"] = self

            def next_state(self):
                self.log.append("ask")
                lagging = self.connecting or clock.monotonic() - self.ended < 0.15
                return WatchdogTests.LOST if lagging else WatchdogTests.UP

        class Client:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            async def __aenter__(self):
                made["bus"].connecting = True
                made["bus"].changed("adapter", None)
                return types.SimpleNamespace(is_connected=True, mtu_size=185, pair=self.paired)

            async def paired(self) -> None:
                return None

            async def __aexit__(self, *_exc) -> None:
                made["bus"].connecting = False
                made["bus"].ended = clock.monotonic()

        async def drained(_link, _store, _address, clock_source=None):
            return {"cuff_released": True, "stored": 1}

        trigger = ConnectPathTests().window()
        with mock.patch.object(radio, "drain", drained):
            result, _log, store = WatchdogTests().run_radio(
                [WatchdogTests.UP], seconds=0.8, trigger=trigger, client=Client, bus=LagBus, check_seconds=0.3
            )
        self.assertEqual(result, "kept-scanning")
        self.assertEqual(bodies(store, "connect-end")[-1]["outcome"], "ok")
        self.assertNotIn("scan-lost", kinds(store))


class StopTests(unittest.TestCase):
    def test_an_unregister_that_fails_still_stops_listening(self) -> None:
        from pressure.passive import PassiveListen

        class Failing(ActivatingRegistrar):
            async def unregister(self, handle) -> None:
                raise RuntimeError("org.bluez.Error.DoesNotExist")

        async def read(_path):
            return None

        listen = PassiveListen(Failing([]), read, lambda _s: None, lambda: None, lambda: None)
        asyncio.run(listen.start())
        asyncio.run(listen.stop())
        self.assertFalse(listen.listening)

    def test_a_listener_that_fails_to_stop_at_the_end_is_ignored(self) -> None:
        result, log, _store = WatchdogTests().run_radio(
            [WatchdogTests.UP, WatchdogTests.LOST], seconds=0.3, fail_stop=True
        )
        self.assertEqual(result, "lost")
        self.assertEqual(log[-2:], ["stop", "bus-close"])


if __name__ == "__main__":
    unittest.main()
