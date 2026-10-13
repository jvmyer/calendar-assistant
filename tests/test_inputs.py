from datetime import date, time

import pytest

from planner import areas
from planner.db import connect
from planner.parsing import deadline_to_dt, parse_deadline, parse_estimate
from planner.rules import TZ, Rules, load_rules


@pytest.mark.parametrize("text,expected", [("90", 90), ("45m", 45), ("2h", 120), ("1,5h", 90), (" 2 Std", 120)])
def test_parse_estimate(text, expected):
    assert parse_estimate(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "0", "2x"])
def test_parse_estimate_rejects(bad):
    with pytest.raises(ValueError):
        parse_estimate(bad)


def test_parse_deadline_variants():
    today = date(2026, 10, 12)
    assert parse_deadline("2026-10-20") == "2026-10-20"
    assert parse_deadline("20.10.", today=today) == "2026-10-20"
    assert parse_deadline("5.10.", today=today) == "2027-10-05"   # ohne Jahr: nächstes Vorkommen
    assert parse_deadline("20.10.2026 18:00", today=today) == "2026-10-20T18:00+02:00"
    assert parse_deadline("2026-10-20 18:00") == "2026-10-20T18:00+02:00"
    with pytest.raises(ValueError):
        parse_deadline("irgendwann")


def test_deadline_to_dt():
    assert deadline_to_dt("2026-10-20").hour == 23
    assert deadline_to_dt("2026-10-20T18:00+02:00").astimezone(TZ).hour == 18


def test_load_rules(tmp_path):
    p = tmp_path / "rules.toml"
    assert load_rules(p) == Rules()   # Datei fehlt → Standard
    p.write_text('day_start = "08:30"\nweekdays = ["mo", "di"]\nignore_calendars = ["Reminder"]\nbuffer_min = 5\n')
    r = load_rules(p)
    assert r.day_start == time(8, 30) and r.weekdays == (0, 1) and r.ignore_calendars == ("Reminder",)
    p.write_text("buffr_min = 5\n")
    with pytest.raises(ValueError, match="Unbekannte"):
        load_rules(p)
    p.write_text('day_start = "20:00"\nday_end = "09:00"\n')
    with pytest.raises(ValueError):
        load_rules(p)


def test_area_tree(tmp_path):
    conn = connect(tmp_path / "t.db")
    leaf = areas.ensure_path(conn, "Uni/Modul X", actor="user")
    assert areas.ensure_path(conn, "uni/modul x", actor="user") == leaf   # groß/klein egal, keine Duplikate
    areas.set_weight(conn, leaf, 1.5, actor="user")
    assert areas.weight_of(conn, leaf) == 1.5 and areas.weight_of(conn, None) == 1.0
    assert areas.path_of(conn, leaf) == "Uni/Modul X"
    assert [(d, n) for d, _, n, _ in areas.tree(conn)] == [(0, "Uni"), (1, "Modul X")]
    with pytest.raises(ValueError):
        areas.set_weight(conn, leaf, 0, actor="user")
