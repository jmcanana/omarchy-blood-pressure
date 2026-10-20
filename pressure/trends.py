"""Time-of-day patterns over stored home readings.

Morning is 4:00–9:59 and evening is 18:00–1:59 on the cuff clock. Those are
the windows this monitor uses for its own AM and PM averages. A contrast is
reported only when each side has at least four readings. Ten mmHg is the
display threshold for "wider", not a diagnosis.
"""

from __future__ import annotations

from statistics import median

from pressure.categories import URGENT, categorise, crisis

MINIMUM_SIDE = 4
# time_source of a reading stamped while the cuff clock was more than two minutes off.
CLOCK_OFF = "cuff-off"
WIDER_THAN_MMHG = 10


def _hour(taken_at: str | None) -> int | None:
    if not taken_at or "T" not in taken_at:
        return None
    try:
        return int(taken_at.split("T", 1)[1][:2])
    except ValueError:
        return None


def in_morning(hour: int | None) -> bool:
    """4:00–9:59, the monitor's own AM average."""
    return hour is not None and 4 <= hour <= 9


def in_evening(hour: int | None) -> bool:
    """18:00–1:59, the monitor's own PM average. The window crosses midnight."""
    return hour is not None and (hour >= 18 or hour <= 1)


def clock_trusted(reading: dict) -> bool:
    """False for a reading stamped by a cuff clock found off in the same sync: its hour is unreliable."""
    return reading.get("time_source") != CLOCK_OFF


def period_contrast(readings: list[dict], guideline: str) -> dict:
    timed = [item for item in readings if clock_trusted(item)]
    morning = [item for item in timed if in_morning(_hour(item.get("taken_at")))]
    evening = [item for item in timed if in_evening(_hour(item.get("taken_at")))]
    ready = len(morning) >= MINIMUM_SIDE and len(evening) >= MINIMUM_SIDE
    result = {
        "ready": ready,
        "morning_n": len(morning),
        "evening_n": len(evening),
        "morning_category": "unknown",
        "evening_category": "unknown",
        "wider": False,
    }
    if not ready:
        return result
    morning_sys = int(median(item["systolic"] for item in morning))
    morning_dia = int(median(item["diastolic"] for item in morning))
    evening_sys = int(median(item["systolic"] for item in evening))
    evening_dia = int(median(item["diastolic"] for item in evening))
    result["morning_category"] = categorise(morning_sys, morning_dia, guideline)
    result["evening_category"] = categorise(evening_sys, evening_dia, guideline)
    result["wider"] = abs(morning_sys - evening_sys) >= WIDER_THAN_MMHG
    result["morning_higher"] = morning_sys - evening_sys >= WIDER_THAN_MMHG
    return result


def notices(readings: list[dict], guideline: str, *, clock_known: bool = False) -> list[str]:
    """Short labels for the panel. Each one names its rule."""
    found: list[str] = []
    if not readings:
        return found
    latest = sorted(readings, key=lambda item: item.get("taken_at") or "")[-1]
    if crisis(latest.get("systolic"), latest.get("diastolic")):
        found.append("severe-latest")
    if latest.get("irregular"):
        found.append("irregular-latest")
    recent = sorted(readings, key=lambda item: item.get("taken_at") or "")[-14:]
    irregular = sum(1 for item in recent if item.get("irregular"))
    if irregular >= 2:
        found.append("irregular-repeat")
    high = [
        item
        for item in recent
        if categorise(item.get("systolic"), item.get("diastolic"), guideline) in URGENT
    ]
    if len(recent) >= 6 and len(high) * 2 > len(recent):
        found.append("often-high")
    contrast = period_contrast(readings, guideline)
    if clock_known and contrast["ready"] and contrast["wider"]:
        found.append("morning-evening")
    return found
