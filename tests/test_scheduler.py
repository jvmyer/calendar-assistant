from datetime import datetime, timedelta

from planner.rules import TZ, Rules
from planner.scheduler import TaskInput, ceil_to, free_windows, merge, schedule, score, subtract

MON_8 = datetime(2026, 10, 12, 8, 0, tzinfo=TZ)   # Montag


def at(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 10, day, hh, mm, tzinfo=TZ)


def test_ceil_to():
    assert ceil_to(at(12, 9, 1), 15) == at(12, 9, 15)
    assert ceil_to(at(12, 9, 15), 15) == at(12, 9, 15)
    assert ceil_to(at(12, 9, 0).replace(second=5), 15) == at(12, 9, 15)


def test_merge_and_subtract():
    assert merge([(at(12, 9), at(12, 10)), (at(12, 10), at(12, 11))]) == [(at(12, 9), at(12, 11))]
    out = subtract([(at(12, 9), at(12, 12))], [(at(12, 10), at(12, 11))])
    assert out == [(at(12, 9), at(12, 10)), (at(12, 11), at(12, 12))]


def test_free_windows_use_buffer_and_skip_sunday():
    rules = Rules(horizon_days=6)
    free = free_windows(MON_8, rules, busy=[(at(12, 10), at(12, 11))])
    assert free[0] == (at(12, 9), at(12, 9, 50))
    assert free[1] == (at(12, 11, 15), at(12, 19))   # 11:00 + 10 Min. Puffer, aufs 15er-Raster gerundet
    assert all(s.weekday() != 6 for s, _ in free)   # Sonntag nicht im Standard


def test_fixed_blocks_have_no_buffer():
    free = free_windows(MON_8, Rules(horizon_days=0), busy=[], fixed=[(at(12, 9), at(12, 10))])
    assert free[0][0] == at(12, 10)


def test_splits_over_days_with_daily_cap():
    rules = Rules(horizon_days=6)
    free = free_windows(MON_8, rules, [])
    res = schedule([TaskInput(1, "Groß", 400)], free, rules, MON_8)
    per_day = [(p.start.date().day, int((p.end - p.start).total_seconds() // 60)) for p in res.placements]
    assert per_day == [(12, 180), (13, 180), (14, 40)]
    assert not res.unplaced


def test_no_tiny_leftover_block():
    rules = Rules(horizon_days=6)
    free = free_windows(MON_8, rules, [])
    res = schedule([TaskInput(1, "X", 190)], free, rules, MON_8)
    sizes = sorted(int((p.end - p.start).total_seconds() // 60) for p in res.placements)
    assert min(sizes) >= rules.min_block_min and sum(sizes) == 190


def test_weight_decides_order():
    rules = Rules(horizon_days=3)
    free = free_windows(MON_8, rules, [])
    res = schedule(
        [TaskInput(1, "Privat", 60, weight=0.5), TaskInput(2, "Arbeit", 60, weight=1.0)], free, rules, MON_8
    )
    first = res.placements[0]
    assert first.task_id == 2 and first.start == at(12, 9)


def test_urgent_deadline_beats_weight():
    rules = Rules(horizon_days=6)
    free = free_windows(MON_8, rules, [])
    urgent = TaskInput(1, "Abgabe", 480, deadline=at(12, 19), weight=0.5)   # braucht fast alles heute
    relaxed = TaskInput(2, "Wichtig, aber Zeit", 60, deadline=at(20, 19), weight=1.0)
    res = schedule([relaxed, urgent], free, rules, MON_8)
    assert res.placements[0].task_id == 1


def test_deadline_is_respected_and_missing_time_reported():
    rules = Rules(horizon_days=3)
    busy = [(at(12, 9), at(12, 18))]
    free = free_windows(MON_8, rules, busy)
    res = schedule([TaskInput(1, "Knapp", 120, deadline=at(12, 19))], free, rules, MON_8)
    assert all(p.end <= at(12, 19) for p in res.placements)
    assert res.unplaced[1] > 0


def test_overdue_is_scheduled_asap():
    rules = Rules(horizon_days=3)
    free = free_windows(MON_8, rules, [])
    res = schedule([TaskInput(1, "Zu spät", 60, deadline=MON_8 - timedelta(days=1))], free, rules, MON_8)
    assert 1 in res.overdue and res.placements[0].start == at(12, 9)


def test_near_equal_scores_prefer_earlier_deadline():
    """Große Aufgabe mit ferner Deadline darf keine Slots einer knappen kleinen wegnehmen."""
    rules = Rules(horizon_days=14)
    busy = [(at(12, 9), at(12, 19)), (at(13, 9), at(13, 19)), (at(14, 9), at(14, 19)), (at(15, 9), at(15, 16, 50))]
    free = free_windows(MON_8, rules, busy)   # bis Donnerstag bleiben nur 120 Min. (17:00–19:00)
    big_far = TaskInput(1, "Groß, fern", 120, deadline=at(20, 23, 59))
    small_near = TaskInput(2, "Klein, bald", 5, deadline=at(15, 23, 59))
    s_big, s_small = score(big_far, free, MON_8), score(small_near, free, MON_8)
    assert s_big > s_small and int(s_big * 4) == int(s_small * 4)   # Voraussetzung: „gleicher“ Score, groß knapp vorn
    res = schedule([big_far, small_near], free, rules, MON_8)
    assert not res.unplaced
    first = min(res.placements, key=lambda p: p.start)
    assert first.task_id == 2   # knappe Aufgabe bekommt den frühesten Slot
