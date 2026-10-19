"""Offline tests. Fixtures are synthetic. No captured readings."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from pressure.blp import mmhg, parse_feature, parse_intermediate, parse_measurement, sfloat
from pressure.categories import aha_category, concern, esc_category, pulse_band
from pressure.collector import drain
from pressure.commands import handle
from pressure.fake import FakeLink, measurement
from pressure.policy import CONNECT_TIMEOUT_SECONDS, arms_session, beacon_class, should_connect, should_drain
from pressure.store import STATUS_KEYS, Store
from pressure.trends import in_evening, in_morning, notices, period_contrast


def _store() -> tuple[Store, Path]:
    root = Path(tempfile.mkdtemp())
    return Store(root / "data", root / "state"), root


class ParseTests(unittest.TestCase):
    def test_specials_and_round_trip(self) -> None:
        self.assertEqual(sfloat(0x07FE), "+inf")
        self.assertEqual(sfloat(0x07FF), "NaN")
        payload = measurement(122, 78, (2024, 8, 15, 18, 5, 55), 84, status=0x0004)
        parsed = parse_measurement(payload)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["taken_at"], "2024-08-15T18:05:55")
        self.assertEqual(mmhg(parsed), (122, 78))
        self.assertEqual(parsed["pulse"], 84)
        self.assertEqual(parsed["user_byte"], 1)
        self.assertEqual(parsed["status"], 0x0004)
        self.assertEqual(parsed["mean"], "+inf")
        self.assertEqual(parsed["tail"], "")

    def test_vendor_tail_and_short_packet(self) -> None:
        payload = measurement(118, 76, (2024, 1, 2, 3, 4, 5), 60, tail=bytes.fromhex("090000000004"))
        parsed = parse_measurement(payload)
        assert parsed is not None
        self.assertEqual(parsed["tail"], "090000000004")
        self.assertIsNone(parse_measurement(b"\x1e\x00\x00"))
        self.assertIsNone(parse_measurement(bytes.fromhex("03005410")))

    def test_kpa_is_not_treated_as_mmhg(self) -> None:
        payload = measurement(16, 10, (2024, 1, 2, 3, 4, 5), 60, kpa=True)
        parsed = parse_measurement(payload)
        assert parsed is not None
        systolic, diastolic = mmhg(parsed)
        self.assertEqual(systolic, 120)
        self.assertEqual(diastolic, 75)

    def test_intermediate_is_not_a_stored_layout(self) -> None:
        self.assertEqual(parse_intermediate(bytes.fromhex("007800"))["units"], "mmHg")
        self.assertEqual(parse_feature(bytes.fromhex("2f00"))["multiple_bonds"], True)
        self.assertFalse(parse_feature(bytes.fromhex("2f00"))["position"])


class PolicyTests(unittest.TestCase):
    def test_transfer_connects_and_the_lookalikes_do_not(self) -> None:
        service = ["00001810-0000-1000-8000-00805f9b34fb"]
        tail_transfer = bytes.fromhex("001122334455a1030001c3")
        tail_idle = bytes.fromhex("00112233445532010040c3")
        window_64 = {105: bytes.fromhex("020501640080"), 76: tail_transfer}
        window_42 = {105: bytes.fromhex("020501420080"), 76: tail_transfer}
        start_stale_tail = {105: bytes.fromhex("010501640080"), 76: tail_transfer}
        start_idle_tail = {105: bytes.fromhex("010501420080"), 76: tail_idle}
        idle_stale_payload = {105: bytes.fromhex("020501420080"), 76: tail_idle}
        prefix_03 = {105: bytes.fromhex("0306fe640080"), 76: tail_transfer}
        generic = {76: b"\x00"}
        for window in (window_64, window_42):
            self.assertEqual(beacon_class(window), "transfer")
            self.assertTrue(should_connect(service, window))
            self.assertTrue(should_drain(service, window, "transfer"))
        for start in (start_stale_tail, start_idle_tail):
            self.assertEqual(beacon_class(start), "start" if start is start_stale_tail else "idle")
            self.assertFalse(should_connect(service, start))
        self.assertEqual(beacon_class(idle_stale_payload), "idle")
        self.assertFalse(should_connect(service, idle_stale_payload))
        self.assertEqual(beacon_class({76: tail_idle}), "idle")
        no_tail_window = {105: bytes.fromhex("020501420080")}
        self.assertEqual(beacon_class(no_tail_window), "aandd-other")
        self.assertFalse(should_connect(service, no_tail_window))
        self.assertEqual(beacon_class(prefix_03), "aandd-other")
        self.assertFalse(should_connect(service, prefix_03))
        self.assertEqual(beacon_class({76: tail_transfer}), "aandd-other")
        self.assertFalse(should_connect(service, {76: tail_transfer}))
        self.assertEqual(beacon_class(generic), "generic")
        self.assertTrue(should_connect(service, generic))
        self.assertFalse(should_connect(["0000180f-0000-1000-8000-00805f9b34fb"], {}))
        self.assertFalse(should_drain(service, generic, "transfer"))
        self.assertTrue(should_drain(service, generic, "generic"))
        as_bytes = {105: bytearray.fromhex("020501420080"), 76: bytearray.fromhex("001122334455a1030001c3")}
        self.assertEqual(beacon_class(as_bytes), "transfer")
        self.assertTrue(arms_session("start") and arms_session("transfer"))
        self.assertFalse(arms_session("idle") or arms_session("aandd-other"))
        self.assertEqual(CONNECT_TIMEOUT_SECONDS, 6.0)


class CategoryTests(unittest.TestCase):
    def test_aha_boundaries(self) -> None:
        self.assertEqual(aha_category(119, 79), "normal")
        self.assertEqual(aha_category(120, 79), "elevated")
        self.assertEqual(aha_category(129, 79), "elevated")
        self.assertEqual(aha_category(129, 80), "stage1")
        self.assertEqual(aha_category(130, 79), "stage1")
        self.assertEqual(aha_category(140, 90), "stage2")
        self.assertEqual(aha_category(180, 90), "stage2")
        self.assertEqual(aha_category(181, 80), "severe")
        self.assertEqual(aha_category(120, 121), "severe")
        self.assertEqual(aha_category(None, 80), "unknown")

    def test_esc_takes_the_higher_side(self) -> None:
        self.assertEqual(esc_category(119, 69), "nonelevated")
        self.assertEqual(esc_category(119, 70), "elevated")
        self.assertEqual(esc_category(120, 60), "elevated")
        self.assertEqual(esc_category(139, 89), "elevated")
        self.assertEqual(esc_category(140, 60), "hypertension")
        self.assertEqual(esc_category(118, 90), "hypertension")
        self.assertEqual(esc_category(181, 70), "hypertension")

    def test_stage_one_stays_off_the_bar(self) -> None:
        self.assertEqual(concern("stage1"), "none")
        self.assertEqual(concern("elevated"), "none")
        self.assertEqual(concern("stage2"), "watch")
        self.assertEqual(concern("hypertension"), "watch")
        self.assertEqual(concern("severe"), "severe")

    def test_pulse_band_is_the_resting_range(self) -> None:
        self.assertEqual(pulse_band(59), "below")
        self.assertEqual(pulse_band(60), "usual")
        self.assertEqual(pulse_band(100), "usual")
        self.assertEqual(pulse_band(101), "above")


class StoreTests(unittest.TestCase):
    def test_duplicate_binding_and_the_panel_summary(self) -> None:
        store, root = _store()
        patient = store.add_patient("North")
        address = "aa:bb:cc:dd:ee:ff"
        store.trust(address, "Synthetic")
        store.bind(address, 1, patient)
        payload = measurement(128, 82, (2024, 6, 1, 7, 30, 0), 70)
        parsed = parse_measurement(payload)
        assert parsed is not None
        parsed["raw"] = payload.hex()
        self.assertTrue(store.add_reading(address, parsed, "standard"))
        self.assertFalse(store.add_reading(address, parsed, "standard"))
        shown = store.summary(patient, "aha")
        self.assertNotIn("morning_category", shown["contrast"])
        self.assertEqual(shown["hero"]["category"], "stage1")
        self.assertEqual(shown["hero"]["systolic"], 128)
        self.assertEqual(shown["hero"]["explain"], "Systolic 128 is elevated; diastolic 82 is stage 1.")
        self.assertEqual(shown["patient"], "North")
        store.write_status(state="idle", beacon="none", link=False, error="")
        document = json.loads((root / "state" / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(set(document), set(STATUS_KEYS))
        self.assertEqual(document["concern"], "none")
        mode = (root / "data" / "pressure.sqlite").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_unassigned_severe_raises_the_bar_without_a_number(self) -> None:
        store, root = _store()
        payload = measurement(182, 100, (2024, 6, 1, 8, 0, 0), 90, user=2)
        parsed = parse_measurement(payload)
        assert parsed is not None
        parsed["raw"] = payload.hex()
        store.add_reading("aa:bb:cc:dd:ee:01", parsed, "vendor")
        store.write_status(state="idle")
        document = json.loads((root / "state" / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(document["concern"], "severe")
        self.assertEqual(document["unassigned"], 1)
        self.assertNotIn("182", json.dumps(document))

    def test_trust_command_returns_only_the_tail(self) -> None:
        store, _root = _store()
        store.remember_pending("seen", "00:11:22:33:44:55", "Cuff", "transfer")
        body = handle(store, {"op": "trust", "token": "seen"})
        self.assertTrue(body["ok"])
        self.assertEqual(body["tail"], "4455")
        self.assertNotIn("address", body)
        self.assertTrue(store.is_trusted("00:11:22:33:44:55"))
        self.assertEqual(store.setting("last_address"), "00:11:22:33:44:55")

    def test_bind_uses_the_last_trusted_address(self) -> None:
        store, _root = _store()
        patient = store.add_patient("North")
        store.trust("aa:bb:cc:dd:ee:10", "Synthetic")
        body = handle(store, {"op": "bind", "user_byte": 2, "patient": patient})
        self.assertTrue(body["ok"])
        self.assertEqual(store.patient_for("aa:bb:cc:dd:ee:10", 2), patient)

    def test_device_detail_hides_serial_and_the_address_suffix(self) -> None:
        store, _root = _store()
        store.remember_pending("seen", "aa:bb:cc:dd:ee:11", "UA-810BLE_EE0011", "transfer")
        seen = handle(store, {"op": "seen"})["seen"]
        self.assertEqual(seen["name"], "UA-810BLE")
        self.assertNotIn("ee0011", json.dumps(seen).lower())
        store.remember_pending("mac", "00:11:22:33:44:85", "00:11:22:33:44:55", "transfer")
        mac_name = handle(store, {"op": "seen"})["seen"]["name"]
        self.assertEqual(mac_name, "Blood pressure monitor")
        self.assertNotIn("44:55", mac_name)
        store.trust("aa:bb:cc:dd:ee:11", "UA-810BLE_EE0011")
        store.update_device("aa:bb:cc:dd:ee:11", model="UA-810BLE", serial="SECRET", battery=80, firmware="1.01")
        hidden = store.devices_public(False)[0]
        self.assertNotIn("serial", hidden)
        self.assertNotIn("name", hidden)
        self.assertEqual(hidden["tail"], "ee11")
        self.assertEqual(hidden["model"], "UA-810BLE")
        shown = store.devices_public(True)[0]
        self.assertEqual(shown["serial"], "SECRET")
        self.assertNotIn("address", shown)


class ClockTests(unittest.TestCase):
    def test_october_vector(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from pressure.radio import clock_payload

        moment = datetime(2026, 10, 17, 13, 46, 52, tzinfo=ZoneInfo("America/Toronto"))
        self.assertEqual(clock_payload(moment).hex(), "ea070a110d2e34060001")

    def test_pair_ignores_an_unimplemented_or_existing_bond(self) -> None:
        import asyncio

        from pressure.radio import _BleakLink

        class Client:
            def __init__(self, error: Exception | None) -> None:
                self.error = error

            async def pair(self) -> None:
                if self.error:
                    raise self.error

        async def run() -> None:
            await _BleakLink(Client(NotImplementedError())).pair()
            await _BleakLink(Client(RuntimeError("Already Exists"))).pair()

        asyncio.run(run())
        with self.assertRaises(RuntimeError):
            asyncio.run(_BleakLink(Client(RuntimeError("Authentication Failed"))).pair())


class TrendTests(unittest.TestCase):
    def test_contrast_waits_for_four_on_each_side(self) -> None:
        few = [{"taken_at": "2024-01-01T07:00:00", "systolic": 150, "diastolic": 95, "irregular": False}]
        self.assertFalse(period_contrast(few, "aha")["ready"])
        readings = []
        for day, morning, evening in (
            (1, 150, 118),
            (2, 148, 116),
            (3, 152, 120),
            (4, 146, 119),
        ):
            readings.append({"taken_at": f"2024-02-{day:02d}T07:00:00", "systolic": morning, "diastolic": 92, "irregular": False})
            readings.append({"taken_at": f"2024-02-{day:02d}T19:00:00", "systolic": evening, "diastolic": 76, "irregular": False})
        contrast = period_contrast(readings, "aha")
        self.assertTrue(contrast["ready"])
        self.assertTrue(contrast["wider"])
        self.assertTrue(in_morning(4) and in_morning(9) and not in_morning(10))
        self.assertTrue(in_evening(18) and in_evening(1) and not in_evening(17) and not in_evening(2))
        self.assertNotIn("morning-evening", notices(readings, "aha"))
        self.assertIn("morning-evening", notices(readings, "aha", clock_known=True))


class AgeTests(unittest.TestCase):
    def test_under_13_is_not_classified_and_does_not_light_the_bar(self) -> None:
        from pressure.categories import CHILD, categorise, label

        self.assertEqual(categorise(150, 95, CHILD), "unclassified")
        self.assertEqual(label("unclassified", CHILD), "Not classified under 13")
        self.assertEqual(categorise(None, None, CHILD), "unknown")
        store, _root = _store()
        address = "00:00:00:00:00:0b"
        patient = store.add_patient("Child")
        store.bind(address, 1, patient)
        parsed = parse_measurement(measurement(150, 95, (2024, 7, 1, 9, 0, 0), 80))
        parsed["raw"] = "aa"
        store.add_reading(address, parsed, "standard")
        self.assertEqual(store.summary(patient, "aha")["hero"]["category"], "stage2")
        response = handle(store, {"op": "set_under_13", "patient": patient, "under_13": True})
        self.assertTrue(response["patients"][0]["under_13"])
        summary = store.summary(patient, "aha")
        self.assertEqual(summary["hero"]["category"], "unclassified")
        self.assertEqual(summary["hero"]["category_label"], "Not classified under 13")
        self.assertEqual(summary["map"]["regions"], [])
        self.assertEqual(summary["trend"]["strips"]["systolic"]["lanes"], [])
        store.write_status(state="idle", beacon="none", link=False, error="")
        self.assertEqual(json.loads(store.status_path.read_text(encoding="utf-8"))["concern"], "none")


class ViewTests(unittest.TestCase):
    def test_lanes_are_equal_height_and_linear_inside(self) -> None:
        from pressure.view import lane_fraction, lanes

        systolic = lanes("aha", "systolic")
        self.assertEqual([lane["key"] for lane in systolic], ["normal", "elevated", "stage1", "stage2", "severe"])
        self.assertEqual(lane_fraction(120, systolic), 0.2)
        self.assertEqual(lane_fraction(125, systolic), 0.3)
        self.assertEqual(lane_fraction(400, systolic), 1.0)
        self.assertEqual(lane_fraction(10, systolic), 0.0)
        self.assertEqual([lane["key"] for lane in lanes("aha", "diastolic")], ["normal", "stage1", "stage2", "severe"])
        self.assertEqual([lane["low"] for lane in lanes("esc", "diastolic")], [50, 70, 90])
        self.assertEqual([lane["label"] for lane in lanes("aha", "pulse")], ["Below 60", "60–100", "Above 100"])

    def test_map_regions_nest_and_skip_the_missing_diastolic_band(self) -> None:
        from pressure.view import map_regions

        regions = {region["key"]: region for region in map_regions("aha")}
        self.assertEqual((regions["severe"]["x"], regions["severe"]["y"]), (1.0, 1.0))
        self.assertEqual(regions["elevated"]["x"], regions["normal"]["x"])
        self.assertGreater(regions["elevated"]["y"], regions["normal"]["y"])
        self.assertEqual([region["key"] for region in map_regions("esc")], ["hypertension", "elevated", "nonelevated"])

    def test_sentences_follow_the_spec(self) -> None:
        from pressure.view import sentence

        readings = [{"systolic": value, "diastolic": 85, "pulse": 72} for value in (118, 132, 134, 136, 128)]
        self.assertEqual(sentence(readings, "systolic", "aha"), "3 of the last 5 systolic readings were in the stage 1 band, median 132.")
        self.assertEqual(sentence(readings, "diastolic", "aha"), "5 of the last 5 diastolic readings were in the stage 1 band, median 85.")
        self.assertTrue(sentence(readings, "pulse", "aha").startswith("5 of the last 5 pulse readings were within 60–100 bpm, median 72."))
        self.assertEqual(sentence(readings, "systolic", "child"), "Median 132 over the last 5 readings.")
        spread = [{"systolic": value} for value in (138, 152, 186)]
        self.assertEqual(sentence(spread, "systolic", "aha"), "The last 3 systolic readings span the stage 1 to severe bands, median 152.")
        self.assertEqual(sentence([], "systolic", "aha"), "")

    def test_time_axis_slots_days_and_breaks_long_gaps(self) -> None:
        from pressure.view import time_axis

        readings = [
            {"taken_at": "2026-06-21T21:11:00"},
            {"taken_at": "2026-10-17T13:06:55"},
            {"taken_at": "2026-10-17T13:16:34"},
            {"taken_at": "2026-10-18T08:05:22"},
        ]
        axis = time_axis(readings)
        self.assertEqual([day["day"] for day in axis["days"]], [21, 17, 18])
        self.assertEqual([day["month"] for day in axis["days"]], ["Jun", "Oct", ""])
        self.assertEqual(len(axis["breaks"]), 1)
        self.assertEqual(axis["days"][-1]["x1"], 1.0)
        xs = [point["x"] for point in axis["points"]]
        self.assertEqual(xs, sorted(xs))
        self.assertTrue(axis["days"][0]["x1"] < axis["breaks"][0] < axis["days"][1]["x0"])

    def test_the_crisis_line_lights_the_bar_under_13(self) -> None:
        store, _root = _store()
        address = "00:00:00:00:00:0c"
        patient = store.add_patient("Child")
        store.set_under_13(patient, True)
        store.bind(address, 1, patient)
        parsed = parse_measurement(measurement(185, 95, (2024, 7, 1, 9, 0, 0), 80))
        parsed["raw"] = "bb"
        store.add_reading(address, parsed, "standard")
        summary = store.summary(patient, "aha")
        self.assertTrue(summary["hero"]["crisis"])
        self.assertIn("severe-latest", summary["notices"])
        store.write_status(state="idle", beacon="none", link=False, error="")
        self.assertEqual(json.loads(store.status_path.read_text(encoding="utf-8"))["concern"], "severe")

    def test_hours_carry_the_range_per_hour(self) -> None:
        from pressure.view import hours

        readings = [
            {"taken_at": "2026-10-18T08:05:00", "systolic": 128, "diastolic": 80},
            {"taken_at": "2026-10-18T08:40:00", "systolic": 141, "diastolic": 76},
            {"taken_at": "2026-10-18T20:00:00", "systolic": 118, "diastolic": 72},
        ]
        profile = hours(readings, "aha")
        self.assertEqual((profile[8]["sys_min"], profile[8]["sys_max"], profile[8]["dia_min"], profile[8]["dia_max"]), (128, 141, 76, 80))
        self.assertTrue(profile[8]["urgent"])
        self.assertFalse(profile[20]["urgent"])
        self.assertNotIn("sys_min", profile[3])

    def test_kicker_and_sync_record(self) -> None:
        from datetime import date

        from pressure.view import kicker

        self.assertEqual(kicker("2026-10-18T08:14:00", date(2026, 10, 18)), "Latest · today 08:14")
        self.assertEqual(kicker("2026-10-17T20:34:16", date(2026, 10, 18)), "Latest · yesterday 20:34")
        self.assertEqual(kicker("2026-06-21T21:11:00", date(2026, 10, 18)), "Latest · Jun 21 21:11")
        store, _root = _store()
        self.assertIsNone(store.last_sync())
        store.record_sync(3)
        self.assertEqual(store.last_sync()["stored"], 3)


class DemoTests(unittest.TestCase):
    def test_demo_seed_covers_the_screenshot_states(self) -> None:
        from datetime import datetime

        from pressure.demo import seed

        store, _root = _store()
        seed(store, datetime.now().replace(hour=16, minute=0, second=0))
        people = {person["alias"]: person for person in store.patients()}
        self.assertEqual(sorted(people), ["Alex", "Jordan", "Sam"])
        self.assertTrue(people["Sam"]["under_13"])
        self.assertTrue(store.summary(people["Jordan"]["id"], "aha")["hero"]["crisis"])
        self.assertTrue(store.summary(people["Alex"]["id"], "aha")["hero"]["kicker"].startswith("Latest · today"))
        self.assertEqual(store.setting("synthetic"), "1")


class SetupTests(unittest.TestCase):
    def test_missing_bleak_leaves_the_panel_working_and_says_setup(self) -> None:
        import asyncio
        import sys

        from pressure import sidecar

        store, _root = _store()
        live: dict = {}
        saved = sys.modules.get("bleak", "absent")
        sys.modules["bleak"] = None  # an import of bleak now raises ImportError
        try:
            asyncio.run(asyncio.wait_for(sidecar._radio(store, live), 2))
        finally:
            if saved == "absent":
                del sys.modules["bleak"]
            else:
                sys.modules["bleak"] = saved
        self.assertEqual(live["state"], "setup")
        status = json.loads(store.status_path.read_text(encoding="utf-8"))
        self.assertEqual((status["state"], status["error"]), ("setup", sidecar.BLEAK_MISSING))
        self.assertTrue(handle(store, {"op": "summary"})["ok"])


class IdentityTests(unittest.TestCase):
    def test_every_path_uses_the_manifest_id(self) -> None:
        import argparse

        from pressure import sidecar

        root = Path(__file__).resolve().parent.parent
        plugin_id = json.loads((root / "manifest.json").read_text(encoding="utf-8"))["id"]
        data, state = sidecar._dirs(argparse.Namespace(data=None, state=None))
        self.assertEqual((data.name, state.name), (plugin_id, plugin_id))
        for name in ("Service.qml", "Panel.qml", "BarWidget.qml", "pressure/run"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertIn(plugin_id, text, name)
            self.assertNotIn("jmcanana.pressure", text, name)


class PanelPrivacyTests(unittest.TestCase):
    # Every process the QML may start, by its exact literal argument list. Commands and personal
    # data go through files in the private state directory, never through a command line.
    ALLOWED_COMMANDS = {
        '["sh", "-c", "wl-copy --type image/png < \\"$1\\"", "sh", root.sharePath]',
        '["wl-copy", "--", root.setupText]',
        '[root.pluginDir + "pressure/run", "--state", root.stateDir, "--data", root.dataDir]',
    }
    PROCESS_BLOCKS = 2

    def violations(self, text: str) -> list[str]:
        """Launch forms that are not one of the allowlisted literal command lines."""
        found = []
        for match in re.finditer(r"(?:\.command\s*=|\bcommand\s*:)\s*(\S)", text):
            if match.group(1) != "[":
                found.append("non-literal command: " + text[match.start():match.start() + 60])
                continue
            # The literal must end its statement, so it cannot be extended (".concat(...)").
            literal = re.match(r"(\[[^\]]*\])[ \t]*(?:\n|;|\}|$)", text[match.start(1):])
            if literal is None or literal.group(1) not in self.ALLOWED_COMMANDS:
                found.append("unlisted command: " + text[match.start():match.start() + 60])
        for launcher in (r"\.\s*exec\s*\(", r"execDetached", r"startDetached", r"property:\s*\"command\"", r"openUrlExternally"):
            if re.search(launcher, text):
                found.append("launcher: " + launcher)
        return found

    def test_only_allowlisted_processes_exist(self) -> None:
        root = Path(__file__).resolve().parent.parent
        sources = {qml: qml.read_text(encoding="utf-8") for qml in sorted(root.rglob("*.qml"))}
        problems = [f"{qml.name}: {issue}" for qml, text in sources.items() for issue in self.violations(text)]
        self.assertEqual(problems, [])
        blocks = sum(len(re.findall(r"\bProcess\s*\{", text)) for text in sources.values())
        self.assertEqual(blocks, self.PROCESS_BLOCKS)
        panel = sources[root / "Panel.qml"]
        self.assertIn("commandFile.setText(JSON.stringify(body))", panel)
        self.assertIn('    body.id = root.sessionTag + "-" + root.commandId\n', panel)
        # The two definitions the allowlisted command lines use, pinned exactly.
        self.assertIn('  readonly property string sharePath: root.stateDir + "/pressure-share.png"\n', panel)
        self.assertIn(
            '  readonly property string setupText: "python3 -m venv ~/.local/share/jmcanana.blood-pressure/venv'
            ' && ~/.local/share/jmcanana.blood-pressure/venv/bin/pip install \\"bleak==3.0.2\\""\n',
            panel,
        )
        self.assertEqual(len(re.findall(r"property string (?:sharePath|setupText)\b", panel)), 2)

    def test_the_scan_catches_every_form(self) -> None:
        planted = [
            'enqueue.command = ["python3", "e.py", JSON.stringify(body)]',
            'Process { command: ["python3", "e.py", JSON.stringify(body)] }',
            'Quickshell.execDetached(["python3", "e.py", JSON.stringify(body)])',
            'clipboard.exec(["python3", "e.py", JSON.stringify(body)])',
            'Process { command: root.writerArgs }',
            'var argv = ["python3", JSON.stringify(body)]; clipboard.command = argv',
            'Process { startDetached() }',
            'clipboard.exec (["python3", JSON.stringify(body)])',
            'clipboard.command = ["wl-copy", "--", root.setupText].concat([JSON.stringify(root.queue)])',
            'Binding { target: clipboard; property: "command"; value: ["python3", JSON.stringify(body)] }',
            'Qt.openUrlExternally("mailto:?body=" + root.summary.patient)',
        ]
        for text in planted:
            self.assertTrue(self.violations(text), text)

    def test_the_sidecar_claims_and_answers_a_command(self) -> None:
        import asyncio

        from pressure import sidecar

        store, root = _store()
        state = root / "state"
        (state / "command.json").write_text(json.dumps({"op": "patients", "id": "7"}), encoding="utf-8")

        async def run():
            task = asyncio.create_task(sidecar._commands_clean(store, state, {}))
            for _ in range(50):
                await asyncio.sleep(0.05)
                if (state / "response.json").exists():
                    break
            task.cancel()

        asyncio.run(run())
        self.assertEqual(json.loads((state / "response.json").read_text(encoding="utf-8"))["id"], "7")
        self.assertFalse((state / "command.json").exists())
        self.assertFalse((state / "command.claimed").exists())

    def test_a_claim_left_by_a_crash_is_handled_first(self) -> None:
        import asyncio

        from pressure import sidecar

        store, root = _store()
        state = root / "state"
        (state / "command.claimed").write_text(json.dumps({"op": "patients", "id": "left"}), encoding="utf-8")
        state.chmod(0o755)

        async def run():
            task = asyncio.create_task(sidecar._commands_clean(store, state, {}))
            for _ in range(50):
                await asyncio.sleep(0.05)
                if (state / "response.json").exists():
                    break
            task.cancel()

        asyncio.run(run())
        self.assertTrue((state / "response.json").exists())
        self.assertEqual(json.loads((state / "response.json").read_text(encoding="utf-8"))["id"], "left")
        self.assertFalse((state / "command.claimed").exists())
        self.assertEqual(state.stat().st_mode & 0o777, 0o700)

    def test_a_command_written_while_one_is_handled_is_not_lost(self) -> None:
        import asyncio
        from unittest import mock

        from pressure import sidecar

        store, root = _store()
        state = root / "state"
        (state / "command.json").write_text(json.dumps({"op": "patients", "id": "1"}), encoding="utf-8")
        handled: list[str] = []
        real_handle = sidecar.handle
        real_unlink = Path.unlink
        planted = {"done": False}

        def recording_handle(store_arg, command):
            handled.append(str(command.get("id")))
            return real_handle(store_arg, command)

        def unlink_after_a_new_command(path, *args, **kwargs):
            if not planted["done"]:
                planted["done"] = True
                (state / "command.json").write_text(json.dumps({"op": "patients", "id": "2"}), encoding="utf-8")
            return real_unlink(path, *args, **kwargs)

        async def run():
            task = asyncio.create_task(sidecar._commands_clean(store, state, {}))
            for _ in range(60):
                await asyncio.sleep(0.05)
                if len(handled) >= 2:
                    break
            task.cancel()

        with mock.patch.object(sidecar, "handle", recording_handle), mock.patch.object(Path, "unlink", unlink_after_a_new_command):
            asyncio.run(run())
        self.assertEqual(handled, ["1", "2"])


class DrainTests(unittest.TestCase):
    def test_burst_is_stored_and_the_clock_stays_unwritten(self) -> None:
        store, _root = _store()
        store.add_patient("North")
        address = "00:00:00:00:00:02"
        store.bind(address, 1, 1)
        standard = measurement(118, 76, (2024, 7, 1, 9, 0, 0), 60)
        vendor = measurement(140, 90, (2024, 7, 2, 9, 0, 0), 70, tail=bytes.fromhex("0100000000"))

        async def run():
            link = FakeLink(
                [
                    ("00002a35-0000-1000-8000-00805f9b34fb", standard),
                    ("11127003-b364-11e4-ab27-0800200c9a66", vendor),
                ],
                uuids=[
                    "00002a35-0000-1000-8000-00805f9b34fb",
                    "00002a2b-0000-1000-8000-00805f9b34fb",
                    "11127003-b364-11e4-ab27-0800200c9a66",
                ],
            )
            result = await drain(link, store, address, quiet_seconds=0.12, first_wait=1.0)
            return link, result

        import asyncio

        link, result = asyncio.run(run())
        self.assertEqual(result["stored"], 2)
        self.assertFalse(result["clock_written"])
        self.assertEqual(link.writes, [])
        self.assertIn("00002a35-0000-1000-8000-00805f9b34fb", link.subscribed)
        self.assertIn("11127003-b364-11e4-ab27-0800200c9a66", link.subscribed)
        self.assertNotIn("00002a2b-0000-1000-8000-00805f9b34fb", link.subscribed)
        self.assertFalse(result["cuff_released"])
        readings = store.readings_for(1)
        self.assertEqual([item["source"] for item in readings], ["standard", "vendor"])
        self.assertEqual(readings[1]["tail"], "0100000000")

    def test_clock_write_is_explicit(self) -> None:
        store, _root = _store()
        address = "00:00:00:00:00:03"
        payload = measurement(118, 76, (2024, 7, 1, 9, 0, 0), 60)

        async def run():
            link = FakeLink([("00002a35-0000-1000-8000-00805f9b34fb", payload)])
            result = await drain(
                link,
                store,
                address,
                write_clock=True,
                clock_payload=bytes(10),
                quiet_seconds=0.12,
                first_wait=1.0,
            )
            return link, result

        import asyncio

        link, result = asyncio.run(run())
        self.assertTrue(result["clock_written"])
        self.assertEqual(len(link.writes), 1)
        self.assertIn("2a2b", link.writes[0][0])
        self.assertTrue(all("11127001" not in uuid for uuid, _payload in link.writes))

    def test_drain_stays_quiet_after_subscribing(self) -> None:
        from pressure.blp import AND_VENDOR_INDICATE, AND_VENDOR_NOTIFY, AND_VENDOR_READ, BP_FEATURE, BP_MEASUREMENT

        store, _root = _store()
        record = measurement(118, 76, (2024, 7, 1, 9, 0, 0), 60)

        async def run():
            link = FakeLink(
                [(BP_MEASUREMENT, record)],
                uuids=[BP_MEASUREMENT, BP_FEATURE, AND_VENDOR_INDICATE, AND_VENDOR_NOTIFY, AND_VENDOR_READ],
            )
            result = await drain(link, store, "00:00:00:00:00:04", quiet_seconds=0.12, first_wait=0.4)
            return link, result

        import asyncio

        link, result = asyncio.run(run())
        self.assertEqual(result["stored"], 1)
        self.assertEqual(link.reads, [])
        self.assertEqual(link.writes, [])
        self.assertEqual(sorted(link.subscribed), sorted([BP_MEASUREMENT, AND_VENDOR_INDICATE]))

    def test_device_reads_wait_for_the_backlog_and_repeat_only_the_battery(self) -> None:
        from pressure.blp import BATTERY, BP_MEASUREMENT

        store, _root = _store()
        address = "00:00:00:00:00:06"
        records = [(BP_MEASUREMENT, measurement(118, 76, (2024, 7, day, 9, 0, 0), 60)) for day in (1, 2, 3)]

        async def run():
            link = FakeLink(
                list(records),
                uuids=[BP_MEASUREMENT],
                responses={
                    "00002a24-0000-1000-8000-00805f9b34fb": b"UA-810BLE",
                    "00002a26-0000-1000-8000-00805f9b34fb": b"PUA0810_1.01",
                },
            )
            result = await drain(link, store, address, quiet_seconds=0.6, first_wait=0.4, tail_after=0.2)
            return link, result

        import asyncio

        link, result = asyncio.run(run())
        self.assertEqual(result["stored"], 3)
        self.assertEqual(link.reads[0], BATTERY)
        self.assertIn("00002a24-0000-1000-8000-00805f9b34fb", link.reads)
        self.assertTrue(store.device_known(address))
        store.update_device("00:00:00:00:00:09", model="UA-810BLE", firmware="PUA0810_1.01")
        self.assertFalse(store.device_known("00:00:00:00:00:09"))
        link, _result = asyncio.run(run())
        self.assertEqual(link.reads, [BATTERY, "00002a2b-0000-1000-8000-00805f9b34fb"])

    def test_device_reads_never_land_between_records(self) -> None:
        from pressure.blp import BP_MEASUREMENT

        store, _root = _store()
        records = [(BP_MEASUREMENT, measurement(118, 76, (2024, 7, day, 9, 0, 0), 60)) for day in (1, 2, 3)]

        async def run():
            link = FakeLink(list(records), uuids=[BP_MEASUREMENT], spacing=0.3)
            await drain(link, store, "00:00:00:00:00:07", quiet_seconds=1.0, first_wait=0.4, tail_after=0.6)
            return link

        import asyncio

        link = asyncio.run(run())
        self.assertEqual(link.events[:3], ["record", "record", "record"])
        self.assertIn("read", link.events)

    def test_a_failed_clock_read_still_reads_the_device(self) -> None:
        from pressure.blp import BP_MEASUREMENT, CURRENT_TIME

        class ClockFails(FakeLink):
            async def read(self, uuid: str) -> bytes:
                if uuid.lower() == CURRENT_TIME:
                    raise RuntimeError("clock")
                return await super().read(uuid)

        store, _root = _store()
        record = measurement(118, 76, (2024, 7, 1, 9, 0, 0), 60)

        async def run():
            link = ClockFails([(BP_MEASUREMENT, record)], uuids=[BP_MEASUREMENT])
            await drain(link, store, "00:00:00:00:00:0a", quiet_seconds=0.6, first_wait=0.4, tail_after=0.2)
            return link

        import asyncio

        link = asyncio.run(run())
        for uuid in ("00002a24", "00002a26", "00002a49"):
            self.assertTrue(any(read.startswith(uuid) for read in link.reads), uuid)

    def test_clock_read_decides_the_check_and_writes_nothing(self) -> None:
        from datetime import datetime, timedelta

        from pressure.blp import BP_MEASUREMENT, CURRENT_TIME
        from pressure.radio import clock_payload

        record = measurement(118, 76, (2024, 7, 1, 9, 0, 0), 60)
        for offset, expected in ((timedelta(0), "1"), (timedelta(hours=1), "0"), (timedelta(hours=-1), "0")):
            store, _root = _store()

            async def run():
                link = FakeLink(
                    [(BP_MEASUREMENT, record)],
                    uuids=[BP_MEASUREMENT],
                    responses={CURRENT_TIME: clock_payload(datetime.now().astimezone() + offset)},
                )
                await drain(link, store, "00:00:00:00:00:08", quiet_seconds=0.6, first_wait=0.4, tail_after=0.2)
                return link

            import asyncio

            link = asyncio.run(run())
            self.assertEqual(store.setting("clock_checked"), expected)
            self.assertEqual(store.summary(None, "aha")["clock_skew_minutes"], None if expected == "1" else 60)
            self.assertEqual(link.writes, [])

    def test_unparsed_vendor_keeps_the_bytes_and_intermediate_is_not_a_row(self) -> None:
        from pressure.blp import AND_VENDOR_INDICATE, BP_INTERMEDIATE

        store, _root = _store()
        raw = bytes.fromhex("1e0100")

        async def run():
            link = FakeLink(
                [
                    (AND_VENDOR_INDICATE, raw),
                    (BP_INTERMEDIATE, bytes.fromhex("00")),
                ],
                uuids=[AND_VENDOR_INDICATE, BP_INTERMEDIATE],
            )
            return await drain(link, store, "00:00:00:00:00:05", quiet_seconds=0.12, first_wait=0.4)

        import asyncio

        result = asyncio.run(run())
        self.assertEqual(result["stored"], 0)
        self.assertEqual(store.count_readings(), 0)
        revealed = store.telemetry_public(True)
        hidden = store.telemetry_public(False)
        unparsed = [item for item in revealed if item["kind"] == "unparsed"]
        self.assertEqual(unparsed[0]["body"]["raw"], raw.hex())
        self.assertTrue(all("raw" not in item["body"] for item in hidden))


class ReviewTests(unittest.TestCase):
    def test_a_replaced_sighting_cannot_be_trusted(self) -> None:
        store, _root = _store()
        store.remember_pending("old", "00:11:22:33:44:01", "First", "transfer")
        store.remember_pending("new", "00:11:22:33:44:02", "Second", "transfer")
        stale = handle(store, {"op": "trust", "token": "old"})
        self.assertFalse(stale["ok"])
        fresh = handle(store, {"op": "trust", "token": "new"})
        self.assertTrue(fresh["ok"])
        self.assertEqual(fresh["tail"], "4402")
        self.assertFalse(store.is_trusted("00:11:22:33:44:01"))

    def test_a_repeated_sighting_keeps_its_token_and_a_changed_one_does_not(self) -> None:
        store, _root = _store()
        store.remember_pending("first", "00:11:22:33:44:03", "Cuff", "transfer")
        store.remember_pending("second", "00:11:22:33:44:03", "Cuff", "transfer")
        self.assertEqual(store.pending_public()["token"], "first")
        store.remember_pending("third", "00:11:22:33:44:03", "Cuff renamed", "transfer")
        self.assertEqual(store.pending_public()["token"], "third")

    def test_a_generic_advert_needs_its_own_confirm(self) -> None:
        store, _root = _store()
        store.remember_pending("tok", "00:11:22:33:44:66", "Other", "generic")
        refused = handle(store, {"op": "trust", "token": "tok"})
        self.assertFalse(refused["ok"])
        self.assertFalse(store.is_trusted("00:11:22:33:44:66"))
        accepted = handle(store, {"op": "trust", "token": "tok", "confirm_generic": True})
        self.assertTrue(accepted["ok"])
        self.assertEqual(accepted["beacon"], "generic")

    def test_bind_names_the_monitor_and_the_bar_ignores_who_is_on_screen(self) -> None:
        store, root = _store()
        first = store.add_patient("North")
        second = store.add_patient("South")
        address = "aa:bb:cc:dd:ee:21"
        other = "aa:bb:cc:dd:ee:22"
        store.trust(address, "Synthetic")
        store.trust(other, "Other")
        unnamed = handle(store, {"op": "bind", "user_byte": 1, "patient": first})
        self.assertFalse(unnamed["ok"])
        mild = measurement(118, 76, (2024, 7, 1, 9, 0, 0), 60, user=1)
        severe = measurement(182, 100, (2024, 7, 2, 9, 0, 0), 90, user=2)
        for payload in (mild, severe):
            parsed = parse_measurement(payload)
            assert parsed is not None
            parsed["raw"] = payload.hex()
            store.add_reading(address, parsed, "standard")
        moved = handle(store, {"op": "bind", "tail": "ee21", "user_byte": 2, "patient": second})
        self.assertTrue(moved["ok"])
        self.assertEqual(store.readings_for(second)[0]["systolic"], 182)
        handle(store, {"op": "bind", "tail": "ee21", "user_byte": 1, "patient": first})
        self.assertEqual(store.count_unassigned(), 0)
        store.put_setting("active_patient", str(first))
        store.write_status(state="idle")
        looking_at_first = json.loads((root / "state" / "status.json").read_text(encoding="utf-8"))["concern"]
        store.put_setting("active_patient", str(second))
        store.write_status(state="idle")
        looking_at_second = json.loads((root / "state" / "status.json").read_text(encoding="utf-8"))["concern"]
        self.assertEqual(looking_at_first, "severe")
        self.assertEqual(looking_at_second, "severe")


if __name__ == "__main__":
    unittest.main()
