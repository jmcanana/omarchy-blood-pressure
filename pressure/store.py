"""Local store for patients, readings, and device telemetry.

Files are created user-only. Readings are not written to the status document.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path

from pressure.blp import mmhg, status_bits
from pressure import view
from pressure.categories import CHILD, reading_concern
from pressure.trends import CLOCK_OFF, notices, period_contrast

STATUS_KEYS = (
    "state",
    "concern",
    "records",
    "unassigned",
    "patients",
    "beacon",
    "link",
    "error",
    "trusted",
)


def _public_monitor_name(name: str) -> str:
    """Drop an address-shaped suffix. A&D names end in an underscore and six hex digits."""
    cleaned = " ".join((name or "").split())
    compact = cleaned.replace(":", "").replace("-", "")
    if compact and len(compact) >= 12 and len(compact) % 2 == 0 and all(character in "0123456789abcdefABCDEF" for character in compact):
        return "Blood pressure monitor"
    if len(cleaned) > 7 and cleaned[-7] == "_" and all(character in "0123456789abcdefABCDEF" for character in cleaned[-6:]):
        cleaned = cleaned[:-7]
    return cleaned or "Blood pressure monitor"


def _public_contrast(readings: list[dict], guideline: str, clock_known: bool) -> dict:
    """Category names stay out until a sync has set the cuff clock."""
    contrast = period_contrast(readings, guideline)
    if clock_known:
        return contrast
    return {
        "ready": False,
        "morning_n": contrast["morning_n"],
        "evening_n": contrast["evening_n"],
    }


def _clean_alias(alias: str) -> str:
    cleaned = " ".join(alias.split())
    if not cleaned or len(cleaned) > 24 or "\n" in alias:
        raise ValueError("alias must be 1 to 24 characters")
    return cleaned


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


class Store:
    def __init__(self, data_dir: Path, state_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.state_dir = Path(state_dir)
        _private_dir(self.data_dir)
        _private_dir(self.state_dir)
        self.db_path = self.data_dir / "pressure.sqlite"
        self.status_path = self.state_dir / "status.json"
        self._db = sqlite3.connect(self.db_path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._migrate()
        os.chmod(self.db_path, stat.S_IRUSR | stat.S_IWUSR)

    def close(self) -> None:
        self._db.close()

    def _migrate(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS patient (
                id INTEGER PRIMARY KEY,
                alias TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS device (
                address TEXT PRIMARY KEY,
                trusted INTEGER NOT NULL DEFAULT 0,
                name TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                manufacturer TEXT NOT NULL DEFAULT '',
                serial TEXT NOT NULL DEFAULT '',
                firmware TEXT NOT NULL DEFAULT '',
                hardware TEXT NOT NULL DEFAULT '',
                software TEXT NOT NULL DEFAULT '',
                battery INTEGER,
                features INTEGER,
                mtu INTEGER,
                last_seen TEXT,
                trusted_beacon TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS binding (
                address TEXT NOT NULL,
                user_byte INTEGER NOT NULL,
                patient_id INTEGER NOT NULL,
                PRIMARY KEY (address, user_byte)
            );
            CREATE TABLE IF NOT EXISTS reading (
                id INTEGER PRIMARY KEY,
                patient_id INTEGER,
                address TEXT NOT NULL,
                user_byte INTEGER,
                taken_at TEXT,
                time_source TEXT NOT NULL,
                received_at TEXT NOT NULL,
                systolic INTEGER,
                diastolic INTEGER,
                mean_raw INTEGER,
                pulse INTEGER,
                status INTEGER,
                flags INTEGER NOT NULL,
                source TEXT NOT NULL,
                tail TEXT NOT NULL DEFAULT '',
                raw TEXT NOT NULL,
                UNIQUE (address, raw)
            );
            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY,
                address TEXT,
                at TEXT NOT NULL,
                kind TEXT NOT NULL,
                body TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS setting (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending (
                token TEXT PRIMARY KEY,
                address TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                beacon TEXT NOT NULL,
                seen_at TEXT NOT NULL
            );
            """
        )
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(device)")}
        if "trusted_beacon" not in columns:
            self._db.execute("ALTER TABLE device ADD COLUMN trusted_beacon TEXT NOT NULL DEFAULT ''")
        patient_columns = {row[1] for row in self._db.execute("PRAGMA table_info(patient)")}
        if "under_13" not in patient_columns:
            self._db.execute("ALTER TABLE patient ADD COLUMN under_13 INTEGER NOT NULL DEFAULT 0")
        self._db.commit()

    def setting(self, key: str, default: str = "") -> str:
        row = self._db.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def put_setting(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO setting(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._db.commit()

    def add_patient(self, alias: str) -> int:
        cleaned = _clean_alias(alias)
        cursor = self._db.execute(
            "INSERT INTO patient(alias, created_at) VALUES(?, ?)",
            (cleaned, _now()),
        )
        self._db.commit()
        patient_id = int(cursor.lastrowid)
        if not self.setting("active_patient"):
            self.put_setting("active_patient", str(patient_id))
        return patient_id

    def patients(self) -> list[dict]:
        rows = self._db.execute("SELECT id, alias, under_13 FROM patient ORDER BY id").fetchall()
        users: dict[int, list[int]] = {}
        for row in self._db.execute("SELECT patient_id, user_byte FROM binding ORDER BY user_byte"):
            users.setdefault(int(row["patient_id"]), []).append(int(row["user_byte"]))
        return [
            {
                "id": row["id"],
                "alias": row["alias"],
                "label": f"Patient {index}",
                "under_13": bool(row["under_13"]),
                "cuff_users": users.get(int(row["id"]), []),
            }
            for index, row in enumerate(rows, start=1)
        ]

    def rename_patient(self, patient_id: int, alias: str) -> None:
        self._require_patient(patient_id)
        cleaned = _clean_alias(alias)
        self._db.execute("UPDATE patient SET alias = ? WHERE id = ?", (cleaned, patient_id))
        self._db.commit()

    def set_under_13(self, patient_id: int, under_13: bool) -> None:
        """Adult bands are not applied to a patient under 13."""
        self._require_patient(patient_id)
        self._db.execute("UPDATE patient SET under_13 = ? WHERE id = ?", (int(under_13), patient_id))
        self._db.commit()

    def guideline_for(self, patient_id: int | None, guideline: str) -> str:
        if patient_id is None:
            return guideline
        row = self._db.execute("SELECT under_13 FROM patient WHERE id = ?", (patient_id,)).fetchone()
        return CHILD if row and row["under_13"] else guideline

    def bind(self, address: str, user_byte: int | None, patient_id: int) -> None:
        """Point this cuff user at a patient, including readings already stored."""
        self._require_patient(patient_id)
        lowered = address.lower()
        if user_byte is None:
            self.put_setting(f"null-user:{lowered}", str(patient_id))
            self._db.execute(
                "UPDATE reading SET patient_id = ? WHERE address = ? AND user_byte IS NULL",
                (patient_id, lowered),
            )
        else:
            self._db.execute(
                """
                INSERT INTO binding(address, user_byte, patient_id) VALUES(?, ?, ?)
                ON CONFLICT(address, user_byte) DO UPDATE SET patient_id = excluded.patient_id
                """,
                (lowered, int(user_byte), patient_id),
            )
            self._db.execute(
                "UPDATE reading SET patient_id = ? WHERE address = ? AND user_byte = ?",
                (patient_id, lowered, int(user_byte)),
            )
        self._db.commit()

    def _require_patient(self, patient_id: int) -> None:
        row = self._db.execute("SELECT id FROM patient WHERE id = ?", (patient_id,)).fetchone()
        if row is None:
            raise ValueError("unknown patient")

    def address_for_tail(self, tail: str) -> str:
        wanted = tail.lower().replace(":", "")
        if len(wanted) != 4:
            return ""
        rows = self._db.execute("SELECT address FROM device WHERE trusted = 1").fetchall()
        matches = [row["address"] for row in rows if row["address"].replace(":", "").endswith(wanted)]
        if len(matches) != 1:
            return ""
        return matches[0]

    def trusted_count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) AS n FROM device WHERE trusted = 1").fetchone()["n"])

    def patient_for(self, address: str, user_byte: int | None) -> int | None:
        if user_byte is None:
            raw = self.setting(f"null-user:{address.lower()}")
            return int(raw) if raw else None
        row = self._db.execute(
            "SELECT patient_id FROM binding WHERE address = ? AND user_byte = ?",
            (address.lower(), int(user_byte)),
        ).fetchone()
        return int(row["patient_id"]) if row else None

    def trust(self, address: str, name: str = "", beacon: str = "") -> None:
        self._db.execute(
            """
            INSERT INTO device(address, trusted, name, last_seen, trusted_beacon) VALUES(?, 1, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
                trusted = 1,
                name = CASE WHEN excluded.name = '' THEN device.name ELSE excluded.name END,
                trusted_beacon = CASE WHEN excluded.trusted_beacon = '' THEN device.trusted_beacon ELSE excluded.trusted_beacon END
            """,
            (address.lower(), name, _now(), beacon),
        )
        self._db.commit()
        self.put_setting("last_address", address.lower())

    def trusted_beacon(self, address: str) -> str:
        row = self._db.execute(
            "SELECT trusted_beacon FROM device WHERE address = ?",
            (address.lower(),),
        ).fetchone()
        return str(row["trusted_beacon"] or "") if row else ""

    def is_trusted(self, address: str) -> bool:
        row = self._db.execute("SELECT trusted FROM device WHERE address = ?", (address.lower(),)).fetchone()
        return bool(row and row["trusted"])

    def remember_pending(self, token: str, address: str, name: str, beacon: str) -> None:
        """Keep one sighting. The same device repeating the same advert keeps its token, so a Trust tap
        is not outrun by the next advert and the database is not rewritten twenty times a second;
        any change of address, name or beacon replaces the token."""
        row = self._db.execute("SELECT address, name, beacon FROM pending LIMIT 1").fetchone()
        if row is not None and (row["address"], row["name"], row["beacon"]) == (address.lower(), name, beacon):
            return
        self._db.execute("DELETE FROM pending")
        self._db.execute(
            "INSERT INTO pending(token, address, name, beacon, seen_at) VALUES(?, ?, ?, ?, ?)",
            (token, address.lower(), name, beacon, _now()),
        )
        self._db.commit()

    def peek_pending(self, token: str) -> dict | None:
        row = self._db.execute(
            "SELECT token, address, name, beacon FROM pending WHERE token = ?",
            (token,),
        ).fetchone()
        if row is None:
            return None
        return {"token": row["token"], "address": row["address"], "name": row["name"], "beacon": row["beacon"]}

    def take_pending(self, token: str) -> dict | None:
        row = self._db.execute("SELECT token, address, name, beacon FROM pending WHERE token = ?", (token,)).fetchone()
        if row is None:
            return None
        self._db.execute("DELETE FROM pending WHERE token = ?", (token,))
        self._db.commit()
        return {"token": row["token"], "address": row["address"], "name": row["name"], "beacon": row["beacon"]}

    def pending_public(self) -> dict | None:
        row = self._db.execute("SELECT token, name, beacon, address FROM pending LIMIT 1").fetchone()
        if row is None:
            return None
        return {
            "token": row["token"],
            "name": _public_monitor_name(row["name"]),
            "beacon": row["beacon"],
            "tail": row["address"].replace(":", "")[-4:],
        }

    def devices_public(self, reveal_device: bool) -> list[dict]:
        """Trusted monitors for the panel. The full address never leaves the database."""
        rows = self._db.execute(
            "SELECT * FROM device WHERE trusted = 1 ORDER BY last_seen DESC"
        ).fetchall()
        public = []
        for row in rows:
            item = {
                "tail": row["address"].replace(":", "")[-4:],
                "model": row["model"],
                "manufacturer": row["manufacturer"],
                "firmware": row["firmware"],
                "hardware": row["hardware"],
                "software": row["software"],
                "battery": row["battery"],
                "features": row["features"],
                "mtu": row["mtu"],
            }
            if reveal_device:
                item["serial"] = row["serial"]
                item["name"] = row["name"]
            public.append(item)
        return public

    def add_reading(self, address: str, parsed: dict, source: str, received_at: str | None = None) -> int | None:
        """Returns the new row id, or None for a reading already stored."""
        raw = parsed["raw"]
        systolic, diastolic = mmhg(parsed)
        bits = status_bits(parsed.get("status"))
        user_byte = parsed.get("user_byte")
        patient_id = self.patient_for(address, user_byte)
        try:
            cursor = self._db.execute(
                """
                INSERT INTO reading(
                    patient_id, address, user_byte, taken_at, time_source, received_at,
                    systolic, diastolic, mean_raw, pulse, status, flags, source, tail, raw
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    patient_id,
                    address.lower(),
                    user_byte,
                    parsed.get("taken_at"),
                    "cuff" if parsed.get("taken_at") else "host",
                    received_at or _now(),
                    systolic,
                    diastolic,
                    parsed.get("mean_raw"),
                    parsed.get("pulse") if isinstance(parsed.get("pulse"), int) else None,
                    parsed.get("status"),
                    parsed.get("flags") or 0,
                    source,
                    parsed.get("tail") or "",
                    raw,
                ),
            )
        except sqlite3.IntegrityError:
            self.add_telemetry(address, "repeat", {"source": source})
            self._db.commit()
            return None
        self._db.commit()
        return cursor.lastrowid

    def mark_clock_off(self, reading_ids: list[int]) -> None:
        """These readings were stamped by a cuff clock found more than two minutes off."""
        self._db.executemany(
            "UPDATE reading SET time_source = ? WHERE id = ? AND time_source = 'cuff'",
            [(CLOCK_OFF, reading_id) for reading_id in reading_ids],
        )
        self._db.commit()

    def add_telemetry(self, address: str | None, kind: str, body: dict) -> None:
        self._db.execute(
            "INSERT INTO telemetry(address, at, kind, body) VALUES(?, ?, ?, ?)",
            (address.lower() if address else None, _now(), kind, json.dumps(body, sort_keys=True)),
        )
        self._db.commit()

    def update_device(self, address: str, **fields: object) -> None:
        self.trust(address)
        allowed = {
            "name",
            "model",
            "manufacturer",
            "serial",
            "firmware",
            "hardware",
            "software",
            "battery",
            "features",
            "mtu",
        }
        assignments = []
        values: list[object] = []
        for key, value in fields.items():
            if key not in allowed or value is None:
                continue
            assignments.append(f"{key} = ?")
            values.append(value)
        assignments.append("last_seen = ?")
        values.append(_now())
        values.append(address.lower())
        self._db.execute(f"UPDATE device SET {', '.join(assignments)} WHERE address = ?", values)
        self._db.commit()

    def _clock_skew_minutes(self) -> int | None:
        """Minutes the cuff clock was off at the last check, or None when it matched or was never read."""
        if self.setting("clock_checked") != "0" or not self.setting("clock_skew_seconds"):
            return None
        return round(int(self.setting("clock_skew_seconds")) / 60)

    def _clock_unset(self) -> bool:
        """The cuff reported a date that is not a date, so there is no offset to show."""
        return self.setting("clock_checked") == "0" and not self.setting("clock_skew_seconds")

    def device_known(self, address: str) -> bool:
        """True once a model, a firmware string and the feature bits are stored for this monitor."""
        row = self._db.execute(
            "SELECT model, firmware, features FROM device WHERE address = ?",
            (address.lower(),),
        ).fetchone()
        return bool(row and row["model"] and row["firmware"] and row["features"] is not None)

    def readings_for(self, patient_id: int | None) -> list[dict]:
        if patient_id is None:
            rows = self._db.execute(
                "SELECT * FROM reading WHERE patient_id IS NULL ORDER BY taken_at, id"
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM reading WHERE patient_id = ? ORDER BY taken_at, id",
                (patient_id,),
            ).fetchall()
        return [self._reading(row) for row in rows]

    def _reading(self, row: sqlite3.Row) -> dict:
        bits = status_bits(row["status"])
        return {
            "id": row["id"],
            "patient_id": row["patient_id"],
            "taken_at": row["taken_at"],
            "time_source": row["time_source"],
            "systolic": row["systolic"],
            "diastolic": row["diastolic"],
            "pulse": row["pulse"],
            "irregular": bits["irregular_pulse"],
            "body_movement": bits["body_movement"],
            "cuff_loose": bits["cuff_loose"],
            "source": row["source"],
            "tail": row["tail"],
        }

    def count_readings(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) AS n FROM reading").fetchone()["n"])

    def count_unassigned(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) AS n FROM reading WHERE patient_id IS NULL").fetchone()["n"])

    def active_patient(self) -> int | None:
        raw = self.setting("active_patient")
        if not raw:
            return None
        return int(raw)

    def summary(self, patient_id: int | None, guideline: str) -> dict:
        """The panel's whole view for one patient. It goes only to the 0600 response file."""
        table = self.guideline_for(patient_id, guideline)
        readings = self.readings_for(patient_id)
        clock_known = self.setting("clock_checked") == "1"
        alias = next((person["alias"] for person in self.patients() if person["id"] == patient_id), "Unassigned")
        return {
            "patient": alias,
            "patient_id": patient_id,
            "guideline": guideline,
            "table": table,
            "child": table == CHILD,
            "n": len(readings),
            "hero": view.hero(readings, table, datetime.now().date()),
            "map": view.map_view(readings, table),
            "trend": view.trend(readings, table),
            "hours": view.hours(readings, table),
            "contrast": _public_contrast(readings, table, clock_known),
            "notices": notices(readings, table, clock_known=clock_known),
            "unassigned": self.count_unassigned(),
            "clock_checked": clock_known,
            "clock_skew_minutes": self._clock_skew_minutes(),
            "clock_unset": self._clock_unset(),
            "last_sync": self.last_sync(),
        }

    def record_sync(self, stored: int) -> None:
        self.put_setting("last_sync", json.dumps({"at": datetime.now().strftime("%H:%M"), "stored": stored}))

    def last_sync(self) -> dict | None:
        raw = self.setting("last_sync")
        return json.loads(raw) if raw else None

    def telemetry_public(self, reveal_device: bool) -> list[dict]:
        rows = self._db.execute(
            "SELECT address, at, kind, body FROM telemetry ORDER BY id DESC LIMIT 40"
        ).fetchall()
        public = []
        for row in rows:
            body = json.loads(row["body"])
            if not reveal_device:
                body = {key: value for key, value in body.items() if key not in {"serial", "address", "raw", "tail"}}
            public.append({"at": row["at"], "kind": row["kind"], "body": body, "tail": (row["address"] or "").replace(":", "")[-4:]})
        return public

    def write_status(self, **fields: object) -> None:
        latest_concern = "none"
        guideline = self.setting("guideline", "aha")
        ids = [int(row["id"]) for row in self._db.execute("SELECT id FROM patient")]
        pools = [(self.readings_for(pid), self.guideline_for(pid, guideline)) for pid in ids]
        pools.append((self.readings_for(None), guideline))
        for readings, table in pools:
            if not readings:
                continue
            latest = readings[-1]
            level = reading_concern(latest.get("systolic"), latest.get("diastolic"), table)
            if latest.get("irregular") and level == "none":
                level = "watch"
            if level == "severe" or (level == "watch" and latest_concern != "severe"):
                latest_concern = level
        document = {
            "state": str(fields.get("state", "idle")),
            "concern": latest_concern,
            "records": self.count_readings(),
            "unassigned": self.count_unassigned(),
            "patients": len(self.patients()),
            "beacon": str(fields.get("beacon", "none")),
            "link": bool(fields.get("link", False)),
            "error": str(fields.get("error", ""))[:160],
            "trusted": int(self._db.execute("SELECT COUNT(*) AS n FROM device WHERE trusted = 1").fetchone()["n"]),
        }
        if set(document) != set(STATUS_KEYS):
            raise RuntimeError("status document gained a field")
        temporary = self.status_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        temporary.replace(self.status_path)
