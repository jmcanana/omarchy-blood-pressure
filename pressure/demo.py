"""Synthetic patients and readings for screenshots and trials. Nothing here is a real measurement.

Seeded, relative to today: sparse sessions over four months, then bursts over the last three days,
the shape real home use has. Alex is an adult on cuff user 1, Sam is under 13 on cuff user 2, and
Jordan's latest reading is above the AHA crisis line so the severe guidance can be shown.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from pressure.fake import measurement
from pressure.store import Store

_ADDRESS = "00:00:00:00:00:01"
_IRREGULAR = 0x0004


def _sessions(today: datetime) -> list[datetime]:
    """Session times: one every week or two for four months, then several a day for three days."""
    times = []
    for days_ago in (122, 110, 96, 81, 67, 52, 44, 30, 17):
        day = today - timedelta(days=days_ago)
        times.append(day.replace(hour=random.choice((7, 8, 19, 20)), minute=random.randrange(60), second=0))
    for days_ago in (2, 1, 0):
        day = today - timedelta(days=days_ago)
        for hour in (7, 8, 13, 19, 21) if days_ago else (7, 8, 12):
            times.append(day.replace(hour=hour, minute=random.randrange(60), second=0))
    return [moment for moment in times if moment <= today]


def _adult(moment: datetime) -> tuple[int, int, int, int]:
    morning = 4 <= moment.hour <= 9
    systolic = int(random.gauss(131 if morning else 122, 7))
    diastolic = int(random.gauss(84 if morning else 77, 4))
    pulse = int(random.gauss(72, 6))
    status = _IRREGULAR if random.random() < 0.08 else 0
    return systolic, diastolic, pulse, status


def _child(moment: datetime) -> tuple[int, int, int, int]:
    return int(random.gauss(106, 5)), int(random.gauss(66, 4)), int(random.gauss(90, 6)), 0


def _add(store: Store, user: int, moment: datetime, values: tuple[int, int, int, int]) -> None:
    systolic, diastolic, pulse, status = values
    when = (moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second)
    payload = measurement(systolic, diastolic, when, pulse, user=user, status=status)
    store.add_reading(
        _ADDRESS,
        {
            "raw": payload.hex(),
            "flags": payload[0],
            "units": "mmHg",
            "systolic": systolic,
            "diastolic": diastolic,
            "mean": "+inf",
            "mean_raw": 0x07FE,
            "taken_at": moment.strftime("%Y-%m-%dT%H:%M:%S"),
            "pulse": pulse,
            "user_byte": user,
            "status": status,
            "tail": "",
        },
        "standard",
    )


def seed(store: Store, today: datetime | None = None) -> None:
    random.seed(18)
    now = (today or datetime.now()).replace(microsecond=0)
    alex = store.add_patient("Alex")
    sam = store.add_patient("Sam")
    jordan = store.add_patient("Jordan")
    store.set_under_13(sam, True)
    store.trust(_ADDRESS, "Synthetic monitor", "transfer")
    store.bind(_ADDRESS, 1, alex)
    store.bind(_ADDRESS, 2, sam)
    store.bind(_ADDRESS, 3, jordan)
    store.update_device(_ADDRESS, model="UA-810BLE", manufacturer="Synthetic", firmware="1.01", battery=70, features=47)
    store.put_setting("guideline", "aha")
    store.put_setting("synthetic", "1")
    store.put_setting("clock_checked", "1")
    store.put_setting("active_patient", str(alex))
    for moment in _sessions(now - timedelta(minutes=5)):
        _add(store, 1, moment, _adult(moment))
    for moment in _sessions(now - timedelta(minutes=5))[::3]:
        _add(store, 2, moment, _child(moment))
    for days_ago, values in ((9, (138, 88, 80, 0)), (4, (152, 94, 84, 0)), (0, (186, 124, 96, _IRREGULAR))):
        _add(store, 3, (now - timedelta(days=days_ago, minutes=20)).replace(second=0), values)
    store.record_sync(3)
    store.write_status(state="scanning", beacon="none", link=False, error="")
