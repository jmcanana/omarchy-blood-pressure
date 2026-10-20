"""Panel commands. Responses go to a 0600 file read only by the panel."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from pressure.store import Store

GUIDELINES = {"aha", "esc"}


def handle(store: Store, command: dict) -> dict:
    op = command.get("op")
    if op == "patients":
        return {"ok": True, "patients": _public_patients(store)}
    if op == "add_patient":
        patient_id = store.add_patient(str(command.get("alias") or ""))
        return {"ok": True, "id": patient_id, "patients": _public_patients(store)}
    if op == "rename_patient":
        store.rename_patient(int(command["patient"]), str(command.get("alias") or ""))
        return {"ok": True, "patients": _public_patients(store)}
    if op == "dismiss_seen":
        store.take_pending(str(command.get("token") or ""))
        return {"ok": True}
    if op == "set_under_13":
        store.set_under_13(int(command["patient"]), bool(command.get("under_13")))
        return {"ok": True, "patients": _public_patients(store)}
    if op == "set_active":
        store._require_patient(int(command["patient"]))
        store.put_setting("active_patient", str(int(command["patient"])))
        return {"ok": True}
    if op == "bind":
        address = ""
        tail = str(command.get("tail") or "")
        if tail:
            address = store.address_for_tail(tail)
        elif store.trusted_count() == 1:
            address = store.setting("last_address")
        if not address:
            return {"ok": False, "error": "name the monitor"}
        raw_user = command.get("user_byte")
        user_byte = None if raw_user is None else int(raw_user)
        store.bind(address, user_byte, int(command["patient"]))
        return {"ok": True, "tail": address.replace(":", "")[-4:]}
    if op == "trust":
        token = str(command.get("token") or "")
        pending = store.peek_pending(token)
        if pending is None:
            return {"ok": False, "error": "nothing waiting"}
        if pending["beacon"] == "generic" and not command.get("confirm_generic"):
            return {"ok": False, "error": "confirm the unknown advert"}
        pending = store.take_pending(token)
        if pending is None:
            return {"ok": False, "error": "nothing waiting"}
        store.trust(pending["address"], pending["name"], pending["beacon"])
        return {"ok": True, "tail": pending["address"].replace(":", "")[-4:], "beacon": pending["beacon"]}
    if op == "seen":
        return {"ok": True, "seen": store.pending_public()}
    if op == "guideline":
        name = str(command.get("guideline") or "aha")
        if name not in GUIDELINES:
            return {"ok": False, "error": "unknown guideline"}
        store.put_setting("guideline", name)
        return {"ok": True, "guideline": name}
    if op == "summary":
        patient = command.get("patient")
        patient_id = None if patient in (None, "", 0, "0") else int(patient)
        if patient_id is None and store.active_patient() is not None and command.get("unassigned") is not True:
            patient_id = store.active_patient()
        guideline = str(command.get("guideline") or store.setting("guideline", "aha"))
        if guideline not in GUIDELINES:
            guideline = "aha"
        body = store.summary(patient_id, guideline)
        body["ok"] = True
        body["seen"] = store.pending_public()
        body["patients"] = _public_patients(store)
        body["active"] = store.active_patient()
        body["monitors"] = store.devices_public(True)
        return body
    if op == "telemetry":
        reveal_device = bool(command.get("reveal_device"))
        return {
            "ok": True,
            "events": store.telemetry_public(reveal_device),
            "devices": store.devices_public(reveal_device),
        }
    return {"ok": False, "error": "unknown command"}


def _public_patients(store: Store) -> list[dict]:
    return [
        {"id": person["id"], "alias": person["alias"], "under_13": person["under_13"], "cuff_users": person["cuff_users"]}
        for person in store.patients()
    ]


def write_response(path: Path, command_id: str, body: dict) -> None:
    body = dict(body)
    body["id"] = command_id
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(body), encoding="utf-8")
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    temporary.replace(path)
