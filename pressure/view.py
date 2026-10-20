"""The panel's view model: everything the QML draws, computed here so it can be tested.

Positions are fractions in [0, 1]. Strips and the map use equal-height band lanes: each band gets
the same room, and inside a lane a value's position is linear. The time axis gives each day with
readings a slot sized by its count and breaks the strip where more than a day passed.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from statistics import median

from pressure.categories import CHILD, TABLES, URGENT, categorise, crisis, label, pulse_band, side_band
from pressure.trends import clock_trusted

PULSE_LANES = {
    "lanes": [("below", 40, False), ("usual", 60, False), ("above", 100, True)],
    "labels": {"below": "Below 60", "usual": "60–100", "above": "Above 100"},
    "top": 140,
}
SENTENCE_WINDOW = 10
MAP_RECENT = 11
TREND_WINDOW = 40
DAY_SLOT_BASE = 14
DAY_SLOT_PER_READING = 6
BREAK_WIDTH = 6
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def lanes(guideline: str, side: str) -> list[dict]:
    """One side's bands as drawable lanes: key, label, low value, high value, urgent."""
    if side == "pulse":
        spec, labels, top = PULSE_LANES["lanes"], PULSE_LANES["labels"], PULSE_LANES["top"]
    else:
        table = TABLES["esc" if guideline == "esc" else "aha"]
        spec, labels, top = table[side], table["labels"], table[f"{side}_top"]
    result = []
    for index, (key, low, _strict) in enumerate(spec):
        high = spec[index + 1][1] if index + 1 < len(spec) else top
        result.append({"key": key, "label": labels[key], "low": low, "high": high, "urgent": key in URGENT})
    return result


def lane_fraction(value: float | None, side_lanes: list[dict]) -> float | None:
    """0 at the bottom of the lowest lane, 1 at the top of the highest."""
    if value is None or not side_lanes:
        return None
    count = len(side_lanes)
    for index, lane in enumerate(side_lanes):
        if value < lane["high"] or index == count - 1:
            span = lane["high"] - lane["low"]
            within = (value - lane["low"]) / span if span else 0.0
            return max(0.0, min(1.0, (index + max(0.0, min(1.0, within))) / count))
    return 1.0


def map_regions(guideline: str) -> list[dict]:
    """Nested L regions, largest first: band k covers readings whose category is at most k."""
    table = TABLES["esc" if guideline == "esc" else "aha"]
    order = table["order"]
    systolic, diastolic = lanes(guideline, "systolic"), lanes(guideline, "diastolic")
    regions = []
    for rank in range(len(order) - 1, -1, -1):
        key = order[rank]
        regions.append(
            {
                "key": key,
                "label": table["labels"][key],
                "urgent": key in URGENT,
                "x": _region_edge(diastolic, order, rank),
                "y": _region_edge(systolic, order, rank),
            }
        )
    return regions


def _region_edge(side_lanes: list[dict], order: list[str], rank: int) -> float:
    covered = [index for index, lane in enumerate(side_lanes) if order.index(lane["key"]) <= rank]
    return (max(covered) + 1) / len(side_lanes) if covered else 0.0


def kicker(taken_at: str | None, today: date) -> str:
    moment = _parse(taken_at)
    if moment is None:
        return "Latest"
    days = (today - moment.date()).days
    clock = moment.strftime("%H:%M")
    if days == 0:
        return f"Latest · today {clock}"
    if days == 1:
        return f"Latest · yesterday {clock}"
    return f"Latest · {MONTHS[moment.month - 1]} {moment.day} {clock}"


def explain(systolic: int | None, diastolic: int | None, guideline: str) -> str:
    if systolic is None or diastolic is None or guideline == CHILD:
        return ""
    table = "esc" if guideline == "esc" else "aha"
    sys_band = TABLES[table]["labels"][side_band(systolic, table, "systolic")].lower()
    dia_band = TABLES[table]["labels"][side_band(diastolic, table, "diastolic")].lower()
    return f"Systolic {systolic} is {sys_band}; diastolic {diastolic} is {dia_band}."


def sentence(readings: list[dict], measure: str, guideline: str) -> str:
    """The trend comparison, verbatim from the design spec. No judgement words."""
    values = [item[measure] for item in readings[-SENTENCE_WINDOW:] if isinstance(item.get(measure), (int, float))]
    if not values:
        return ""
    count, middle = len(values), int(median(values))
    if guideline == CHILD:
        return f"Median {middle} over the last {count} readings."
    if measure == "pulse":
        within = sum(1 for value in values if 60 <= value <= 100)
        return (
            f"{within} of the last {count} pulse readings were within 60–100 bpm, median {middle}. "
            "A cuff pulse is taken during the measurement."
        )
    table = "esc" if guideline == "esc" else "aha"
    bands = Counter(side_band(value, table, measure) for value in values)
    order = TABLES[table]["order"]
    ranked = sorted(bands.items(), key=lambda pair: (-pair[1], order.index(pair[0])))
    band, within = ranked[0]
    labels = TABLES[table]["labels"]
    if len(ranked) > 1 and ranked[1][1] == within:
        present = sorted(bands, key=order.index)
        low, high = labels[present[0]].lower(), labels[present[-1]].lower()
        return f"The last {count} {measure} readings span the {low} to {high} bands, median {middle}."
    return f"{within} of the last {count} {measure} readings were in the {labels[band].lower()} band, median {middle}."


def time_axis(readings: list[dict]) -> dict:
    """Day slots, breaks and each reading's x, all as fractions of the strip width."""
    dated = [(item, _parse(item.get("taken_at"))) for item in readings[-TREND_WINDOW:]]
    dated = [(item, moment) for item, moment in dated if moment is not None]
    days: list[dict] = []
    for item, moment in dated:
        if not days or days[-1]["date"] != moment.date():
            days.append({"date": moment.date(), "items": []})
        days[-1]["items"].append(item)
    cursor = 0.0
    breaks: list[float] = []
    previous: date | None = None
    for day in days:
        if previous is not None and (day["date"] - previous).days > 1:
            breaks.append(cursor + BREAK_WIDTH / 2)
            cursor += BREAK_WIDTH
        day["x0"] = cursor
        cursor += DAY_SLOT_BASE + DAY_SLOT_PER_READING * len(day["items"])
        day["x1"] = cursor
        previous = day["date"]
    total = cursor or 1.0
    out_days, points, month = [], [], None
    for day in days:
        width = day["x1"] - day["x0"]
        label_month = MONTHS[day["date"].month - 1] if day["date"].month != month else ""
        month = day["date"].month
        out_days.append({"x0": day["x0"] / total, "x1": day["x1"] / total, "day": day["date"].day, "month": label_month})
        for index, item in enumerate(day["items"]):
            x = (day["x0"] + width * (index + 0.5) / len(day["items"])) / total
            points.append({"x": x, "day": len(out_days) - 1, "item": item})
    return {"days": out_days, "breaks": [value / total for value in breaks], "points": points}


def trend(readings: list[dict], guideline: str) -> dict:
    axis = time_axis(readings)
    child = guideline == CHILD
    strips = {}
    for measure, side in (("systolic", "systolic"), ("diastolic", "diastolic"), ("pulse", "pulse")):
        side_lanes = [] if child else lanes(guideline, side)
        values = [point["item"].get(measure) for point in axis["points"] if point["item"].get(measure) is not None]
        low, high = (min(values), max(values)) if values else (0, 1)
        plain = [{"key": "range", "label": "", "low": low - 2, "high": high + 2, "urgent": False}]
        latest_band = ""
        if side_lanes and values:
            band = side_band(values[-1], "esc" if guideline == "esc" else "aha", side) if side != "pulse" else pulse_band(values[-1])
            latest_band = band
        strips[measure] = {
            "lanes": side_lanes,
            "latest_lane": latest_band,
            "range": [low, high],
            "sentence": sentence(readings, measure, guideline),
            "points": [
                {
                    "x": point["x"],
                    "y": lane_fraction(point["item"].get(measure), side_lanes or plain),
                    "day": point["day"],
                    "value": point["item"].get(measure),
                }
                for point in axis["points"]
                if point["item"].get(measure) is not None
            ],
        }
    readings_out = [
        {
            "taken_at": point["item"].get("taken_at"),
            "systolic": point["item"].get("systolic"),
            "diastolic": point["item"].get("diastolic"),
            "pulse": point["item"].get("pulse"),
            "irregular": bool(point["item"].get("irregular")),
        }
        for point in axis["points"]
    ]
    first = axis["points"][0]["item"].get("taken_at") if axis["points"] else None
    last = axis["points"][-1]["item"].get("taken_at") if axis["points"] else None
    return {
        "days": axis["days"],
        "breaks": axis["breaks"],
        "strips": strips,
        "readings": readings_out,
        "span": _span(first, last, len(readings)),
    }


def map_view(readings: list[dict], guideline: str) -> dict:
    if guideline == CHILD:
        return {"regions": [], "points": [], "systolic": [], "diastolic": []}
    systolic, diastolic = lanes(guideline, "systolic"), lanes(guideline, "diastolic")
    recent = readings[-MAP_RECENT:]
    points = [
        {
            "x": lane_fraction(item.get("diastolic"), diastolic),
            "y": lane_fraction(item.get("systolic"), systolic),
            "latest": index == len(recent) - 1,
        }
        for index, item in enumerate(recent)
        if item.get("systolic") is not None and item.get("diastolic") is not None
    ]
    latest = recent[-1] if recent else {}
    return {
        "regions": map_regions(guideline),
        "current": categorise(latest.get("systolic"), latest.get("diastolic"), guideline),
        "points": points,
        "systolic": [{"value": lane["low"], "y": lane_fraction(lane["low"], systolic)} for lane in systolic[1:]],
        "diastolic": [{"value": lane["low"], "x": lane_fraction(lane["low"], diastolic)} for lane in diastolic[1:]],
    }


def hero(readings: list[dict], guideline: str, today: date) -> dict:
    if not readings:
        return {"present": False}
    latest = readings[-1]
    systolic, diastolic = latest.get("systolic"), latest.get("diastolic")
    category = categorise(systolic, diastolic, guideline)
    return {
        "present": True,
        "systolic": systolic,
        "diastolic": diastolic,
        "pulse": latest.get("pulse"),
        "taken_at": latest.get("taken_at"),
        "kicker": kicker(latest.get("taken_at"), today),
        "category": category,
        "category_label": label(category, guideline),
        "urgent": category in URGENT or crisis(systolic, diastolic),
        "crisis": crisis(systolic, diastolic),
        "explain": explain(systolic, diastolic, guideline),
        "irregular": bool(latest.get("irregular")),
        "body_movement": bool(latest.get("body_movement")),
        "cuff_loose": bool(latest.get("cuff_loose")),
    }


def hours(readings: list[dict], guideline: str) -> list[dict]:
    """The 24-hour dial: per hour, the lowest and highest systolic and diastolic, the count, and
    whether any reading in that hour was stage 2 or above. Readings stamped by an off clock are left out."""
    buckets: list[dict] = [{"hour": hour, "n": 0, "urgent": False} for hour in range(24)]
    for item in filter(clock_trusted, readings):
        moment = _parse(item.get("taken_at"))
        systolic, diastolic = item.get("systolic"), item.get("diastolic")
        if moment is None or systolic is None or diastolic is None:
            continue
        bucket = buckets[moment.hour]
        bucket["n"] += 1
        bucket["sys_min"] = min(bucket.get("sys_min", systolic), systolic)
        bucket["sys_max"] = max(bucket.get("sys_max", systolic), systolic)
        bucket["dia_min"] = min(bucket.get("dia_min", diastolic), diastolic)
        bucket["dia_max"] = max(bucket.get("dia_max", diastolic), diastolic)
        category = categorise(systolic, diastolic, guideline)
        bucket["urgent"] = bucket["urgent"] or category in URGENT or crisis(systolic, diastolic)
    return buckets


def _span(first: str | None, last: str | None, count: int) -> str:
    start, end = _parse(first), _parse(last)
    if start is None or end is None:
        return ""
    def short(moment: datetime) -> str:
        return f"{MONTHS[moment.month - 1]} {moment.day}"
    noun = "reading" if count == 1 else "readings"
    if start.date() == end.date():
        return f"{short(end)} · {count} {noun}"
    return f"{short(start)} – {short(end)} · {count} {noun}"


def _parse(taken_at: str | None) -> datetime | None:
    if not taken_at:
        return None
    try:
        return datetime.fromisoformat(taken_at)
    except ValueError:
        return None
