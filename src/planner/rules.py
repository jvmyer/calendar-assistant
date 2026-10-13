"""Planungsregeln (Constraints), konfigurierbar über `rules.toml` im Datenordner."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields, replace
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
DAYS = {"mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6}


@dataclass(frozen=True)
class Rules:
    day_start: time = time(9, 0)          # frühester Beginn eines Blockers
    day_end: time = time(19, 0)           # spätestes Ende
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4, 5)   # Mo–Sa
    buffer_min: int = 10                  # Abstand zu festen Terminen
    min_block_min: int = 30               # kleinster sinnvoller Blocker
    max_task_per_day_min: int = 180       # pro Aufgabe und Tag
    default_estimate_min: int = 60        # wenn keine Schätzung vorliegt
    horizon_days: int = 14                # so weit wird vorausgeplant
    freeze_days: int = 2                  # heute + morgen bleiben stabil
    granularity_min: int = 15             # Blockanfänge liegen auf diesem Raster
    ignore_calendars: tuple[str, ...] = ()  # Kalendernamen, die nicht als „belegt“ zählen


def _parse_time(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def load_rules(path: Path) -> Rules:
    """Liest Regeln aus einer TOML-Datei; fehlt sie, gelten die Standardwerte."""
    rules = Rules()
    if not path.exists():
        return rules
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in fields(Rules)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Unbekannte Schlüssel in {path.name}: {', '.join(sorted(unknown))}")
    if "day_start" in data:
        data["day_start"] = _parse_time(data["day_start"])
    if "day_end" in data:
        data["day_end"] = _parse_time(data["day_end"])
    if "weekdays" in data:
        try:
            data["weekdays"] = tuple(DAYS[d.lower()[:2]] for d in data["weekdays"])
        except KeyError as exc:
            raise ValueError(f"Unbekannter Wochentag in weekdays: {exc}") from None
    if "ignore_calendars" in data:
        data["ignore_calendars"] = tuple(data["ignore_calendars"])
    rules = replace(rules, **data)
    if rules.day_end <= rules.day_start:
        raise ValueError("day_end muss nach day_start liegen")
    return rules
