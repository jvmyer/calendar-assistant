"""Planer v1 (Greedy): reine Funktionen ohne Kalender- oder Datenbankzugriff.

Ablauf: freie Zeitfenster berechnen → Aufgaben nach Score sortieren → jede Aufgabe in die
frühesten passenden Fenster legen (in Blöcke gesplittet, pro Tag begrenzt).

    score = Bereichsgewicht × Dringlichkeit × manueller Faktor
    Dringlichkeit = 1 + 4 × min(1, Restaufwand / freie Zeit bis zur Deadline)
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .rules import TZ, Rules

Interval = tuple[datetime, datetime]
_FAR_FUTURE = datetime.max.replace(tzinfo=TZ)


@dataclass(frozen=True)
class TaskInput:
    id: int
    title: str
    remaining_min: int
    deadline: datetime | None = None
    weight: float = 1.0
    boost: float = 1.0


@dataclass(frozen=True)
class Placement:
    task_id: int
    start: datetime
    end: datetime


@dataclass
class ScheduleResult:
    placements: list[Placement] = field(default_factory=list)
    unplaced: dict[int, int] = field(default_factory=dict)   # task_id -> fehlende Minuten
    overdue: set[int] = field(default_factory=set)           # Deadline schon vorbei


def minutes(a: datetime, b: datetime) -> int:
    return int((b - a).total_seconds() // 60)


def ceil_to(dt: datetime, step_min: int) -> datetime:
    """Rundet auf das nächste Raster (z. B. 15 Min.) auf."""
    dt = dt.astimezone(TZ)
    base = dt.replace(second=0, microsecond=0)
    if dt != base:
        base += timedelta(minutes=1)
    rem = base.minute % step_min
    return base + timedelta(minutes=step_min - rem) if rem else base


def merge(intervals: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for s, e in sorted(intervals):
        if e <= s:
            continue
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def subtract(windows: list[Interval], busy: list[Interval]) -> list[Interval]:
    result: list[Interval] = []
    blocked = merge(busy)
    for ws, we in windows:
        cur = ws
        for bs, be in blocked:
            if be <= cur:
                continue
            if bs >= we:
                break
            if bs > cur:
                result.append((cur, bs))
            cur = max(cur, be)
            if cur >= we:
                break
        if cur < we:
            result.append((cur, we))
    return result


def free_windows(
    now: datetime, rules: Rules, busy: list[Interval], fixed: list[Interval] | None = None
) -> list[Interval]:
    """Freie Fenster im Arbeitszeitrahmen.

    `busy` (fremde Termine) wird mit Puffer abgezogen, `fixed` (eigene, bereits stehende
    Blocker) ohne Puffer.
    """
    start = ceil_to(now, rules.granularity_min)
    first_day = start.date()
    windows: list[Interval] = []
    for i in range(rules.horizon_days + 1):
        day = first_day + timedelta(days=i)
        if day.weekday() not in rules.weekdays:
            continue
        ws = max(datetime.combine(day, rules.day_start, TZ), start)
        we = datetime.combine(day, rules.day_end, TZ)
        if we > ws:
            windows.append((ws, we))
    pad = timedelta(minutes=rules.buffer_min)
    free = subtract(windows, [(s - pad, e + pad) for s, e in busy])
    free = subtract(free, fixed or [])
    out: list[Interval] = []
    for s, e in free:
        s = ceil_to(s, rules.granularity_min)
        if minutes(s, e) >= rules.min_block_min:
            out.append((s, e))
    return out


def _available_before(free: list[Interval], now: datetime, deadline: datetime) -> int:
    total = 0
    for s, e in free:
        lo, hi = max(s, now), min(e, deadline)
        if hi > lo:
            total += minutes(lo, hi)
    return total


def urgency(task: TaskInput, free: list[Interval], now: datetime) -> float:
    if task.deadline is None:
        return 1.0
    avail = _available_before(free, now, task.deadline)
    if avail <= 0:
        return 5.0
    return 1.0 + 4.0 * min(1.0, task.remaining_min / avail)


def score(task: TaskInput, free: list[Interval], now: datetime) -> float:
    return task.weight * urgency(task, free, now) * task.boost


def schedule(tasks: list[TaskInput], free: list[Interval], rules: Rules, now: datetime) -> ScheduleResult:
    result = ScheduleResult()
    scores = {t.id: score(t, free, now) for t in tasks}
    windows = [list(w) for w in free]
    per_day: dict[tuple[int, object], int] = defaultdict(int)
    # Scores innerhalb von 0,25 gelten als gleich; dann entscheidet die frühere Deadline.
    # Sonst belegt eine große Aufgabe mit ferner Deadline die frühen Slots einer knappen kleinen.
    ordered = sorted(
        tasks,
        key=lambda t: (-math.floor(scores[t.id] * 4), t.deadline or _FAR_FUTURE, -scores[t.id], t.id),
    )

    for task in ordered:
        need = task.remaining_min
        if need <= 0:
            continue
        limit = task.deadline
        if limit is not None and limit <= now:
            result.overdue.add(task.id)   # überfällig: so früh wie möglich, ohne Obergrenze
            limit = None
        for w in windows:
            if need <= 0:
                break
            if limit is not None and w[0] >= limit:
                break
            cap_end = min(w[1], limit) if limit is not None else w[1]
            length = minutes(w[0], cap_end)
            day = w[0].astimezone(TZ).date()
            room = rules.max_task_per_day_min - per_day[(task.id, day)]
            chunk = min(need, length, room)
            if chunk <= 0 or chunk < min(rules.min_block_min, need):
                continue
            leftover = need - chunk
            if 0 < leftover < rules.min_block_min:   # keinen winzigen Rest übrig lassen
                shrink = rules.min_block_min - leftover
                if chunk - shrink >= rules.min_block_min:
                    chunk -= shrink
            start = w[0]
            end = start + timedelta(minutes=chunk)
            result.placements.append(Placement(task.id, start, end))
            w[0] = ceil_to(end, rules.granularity_min)
            per_day[(task.id, day)] += chunk
            need -= chunk
        if need > 0:
            result.unplaced[task.id] = need

    result.placements = _merge_adjacent(sorted(result.placements, key=lambda p: (p.start, p.task_id)))
    return result


def _merge_adjacent(placements: list[Placement]) -> list[Placement]:
    out: list[Placement] = []
    for p in placements:
        prev = next((q for q in reversed(out) if q.task_id == p.task_id), None)
        if prev is not None and prev.end == p.start:
            out[out.index(prev)] = Placement(p.task_id, prev.start, p.end)
        else:
            out.append(p)
    return out
