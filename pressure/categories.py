"""Home blood-pressure categories, held as data so the panel draws the same bands it classifies with.

Default table: American Heart Association, "Understanding Blood Pressure
Readings", page updated August 2025. Severe is "higher than 180 and/or higher
than 120". The alternate switch is ESC 2024: non-elevated, elevated (120–139 or
70–89) and hypertension (140/90 or higher). The category is the higher of the
systolic and diastolic bands. A single home reading is not a diagnosis.
"""

from __future__ import annotations

# Each side lists its bands low to high: key, lowest value in the band, and whether the band starts
# strictly above that value (AHA "higher than 180"). The first band's lower edge and the last band's
# upper edge are display limits for the panel, not thresholds.
TABLES: dict[str, dict] = {
    "aha": {
        "labels": {"normal": "Normal", "elevated": "Elevated", "stage1": "Stage 1", "stage2": "Stage 2", "severe": "Severe"},
        "order": ["normal", "elevated", "stage1", "stage2", "severe"],
        "systolic": [("normal", 90, False), ("elevated", 120, False), ("stage1", 130, False), ("stage2", 140, False), ("severe", 180, True)],
        "diastolic": [("normal", 50, False), ("stage1", 80, False), ("stage2", 90, False), ("severe", 120, True)],
        "systolic_top": 210,
        "diastolic_top": 140,
    },
    "esc": {
        "labels": {"nonelevated": "Non-elevated", "elevated": "Elevated", "hypertension": "Hypertension"},
        "order": ["nonelevated", "elevated", "hypertension"],
        "systolic": [("nonelevated", 90, False), ("elevated", 120, False), ("hypertension", 140, False)],
        "diastolic": [("nonelevated", 50, False), ("elevated", 70, False), ("hypertension", 90, False)],
        "systolic_top": 210,
        "diastolic_top": 140,
    },
}
URGENT = {"stage2", "severe", "hypertension"}
CRISIS_SYSTOLIC = 180
CRISIS_DIASTOLIC = 120


def side_band(value: int | None, table: str, side: str) -> str:
    """The band one side's value falls in, or unknown."""
    if value is None:
        return "unknown"
    found = "unknown"
    for key, edge, strict in TABLES[table][side]:
        if value > edge if strict else value >= edge:
            found = key
    return found if found != "unknown" else TABLES[table][side][0][0]


def _rank(table: str, key: str) -> int:
    return TABLES[table]["order"].index(key)


def aha_category(systolic: int | None, diastolic: int | None) -> str:
    return _category(systolic, diastolic, "aha")


def esc_category(systolic: int | None, diastolic: int | None) -> str:
    return _category(systolic, diastolic, "esc")


def _category(systolic: int | None, diastolic: int | None, table: str) -> str:
    if systolic is None or diastolic is None:
        return "unknown"
    return max(side_band(systolic, table, "systolic"), side_band(diastolic, table, "diastolic"), key=lambda key: _rank(table, key))


def crisis(systolic: int | None, diastolic: int | None) -> bool:
    """AHA crisis line, at any age: higher than 180 and/or higher than 120."""
    return (systolic or 0) > CRISIS_SYSTOLIC or (diastolic or 0) > CRISIS_DIASTOLIC


# The AHA/ESC tables are for adults. The AAP 2017 guideline uses the same fixed cut-offs from
# age 13; under 13 it uses percentiles for age, sex and height, which this plugin does not hold.
CHILD = "child"
CHILD_LABEL = {"unknown": "No reading", "unclassified": "Not classified under 13"}


def categorise(systolic: int | None, diastolic: int | None, guideline: str) -> str:
    if guideline == CHILD:
        return "unclassified" if systolic is not None else "unknown"
    if guideline == "esc":
        return esc_category(systolic, diastolic)
    return aha_category(systolic, diastolic)


def label(category: str, guideline: str) -> str:
    if guideline == CHILD:
        return CHILD_LABEL.get(category, category)
    if category == "unknown":
        return "No reading"
    return TABLES["esc" if guideline == "esc" else "aha"]["labels"].get(category, category)


def concern(category: str) -> str:
    """Bar signal from a category: AHA stage 2 and ESC hypertension (both from 140/90) watch, severe is severe.

    AHA stage 1 and ESC elevated stay as words inside the panel.
    """
    if category == "severe":
        return "severe"
    if category in URGENT:
        return "watch"
    return "none"


def reading_concern(systolic: int | None, diastolic: int | None, guideline: str) -> str:
    """The crisis line raises severe under every table and at every age, including under 13."""
    if crisis(systolic, diastolic):
        return "severe"
    return concern(categorise(systolic, diastolic, guideline))


def pulse_band(pulse: int | None) -> str:
    """Usual adult resting range quoted by the AHA is 60 to 100 beats/min.

    A cuff pulse is taken during a blood-pressure measurement, so it is not a
    resting pulse. Callers must say that beside the band.
    """
    if pulse is None:
        return "unknown"
    if pulse < 60:
        return "below"
    if pulse > 100:
        return "above"
    return "usual"
