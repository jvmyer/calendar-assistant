from datetime import datetime

import httpx
import pytest

from planner import tasks
from planner.connectors.ilias import SkippedFile
from planner.courses import clean_course_name, match_score, next_session
from planner.db import connect
from planner.ilias_flow import process_news, task_title
from planner.rules import TZ, Rules
from planner.service import planning_step
from test_gtasks import FakeTasks
from test_planning import FakeCal

NOW = datetime(2026, 10, 13, 10, 30, tzinfo=TZ)   # Dienstag


class Channel:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    def send(self, text):
        self.sent.append((text, []))
        return self.ok

    def send_with_buttons(self, text, buttons):
        self.sent.append((text, buttons))
        return self.ok


class Client:
    def __init__(self, files=None, course="Verteilte Systeme WS 2026_27", error=None):
        self.files, self.course, self.error = files or {}, course, error

    def download_file(self, ref_id, accept=None, **kw):
        if self.error:
            raise self.error
        data, ctype, name = self.files.get(ref_id, (b"", "text/plain", "leer.txt"))
        if accept is not None and not accept(name, ctype):   # wie der echte Client
            raise SkippedFile("Typ nicht gelesen")
        return data, ctype, name

    def course_of(self, obj_type, ref_id):
        return self.course


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.db")


def add_news(conn, news_id, title, *, ref="100", obj_type="file", body="", headline="Die Datei wurde hinzugefügt.",
             ts="2026-10-13T10:00:00+02:00", author="Muster, Erika"):
    conn.execute(
        "INSERT INTO ilias_news(news_id, ts, author, uid, headline, obj_type, ref_id, title, body, first_seen, state) "
        "VALUES (?,?,?,?,?,?,?,?,?,?, 'new')", (news_id, ts, author, "abcd0001", headline, obj_type, ref, title, body, "x"))
    conn.commit()


def add_lecture(conn, title, start_iso, uid="1"):
    conn.execute(
        "INSERT INTO external_events(source, uid, title, start, end, all_day, first_seen, last_seen) "
        "VALUES ('raumzeit', ?, ?, ?, ?, 0, 'x', 'x')", (uid, title, start_iso, start_iso))
    conn.commit()


def txt(text):
    return (text.encode(), "text/plain", "blatt.txt")


# -- Beispiel 1: „Abgabe bis spätestens … 19 Uhr“ ------------------------------------------------------------------------
def test_explicit_deadline_becomes_task_automatically(conn):
    add_news(conn, "1", "Blatt 3.txt", ref="100")
    client = Client({"100": txt("Aufgabe 3: Lösung als PDF.\nAbgabe bis spätestens 20.10. 19 Uhr in ILIAS.")})
    ch = Channel()
    assert process_news(conn, client, ch, NOW) == 1
    t = conn.execute("SELECT * FROM tasks").fetchone()
    assert t["title"] == "Abgabe: Blatt 3 (Verteilte Systeme)" and t["source"] == "ilias"
    assert t["deadline"] == "2026-10-20T19:00+02:00"
    assert conn.execute("SELECT name FROM areas").fetchall()[-1]["name"] == "Verteilte Systeme"
    text, buttons = ch.sent[0]
    assert text.startswith("📚 ILIAS") and "bis 20.10. 19:00" in text
    assert buttons == [[(f"🗑 #{t['id']} verwerfen", f"drop:{t['id']}")]]
    assert conn.execute("SELECT state FROM ilias_news").fetchone()[0] == "seen"


# -- Beispiel 2: „bis zur nächsten Stunde“ ------------------------------------------------------------------------------
def test_next_lesson_deadline_comes_from_timetable(conn):
    add_lecture(conn, "Verteilte Systeme", "2026-10-13T08:40:00+00:00", "a")   # 10:40 heute, nach dem Posting 10:00
    add_lecture(conn, "Verteilte Systeme", "2026-10-20T08:40:00+00:00", "b")
    add_lecture(conn, "Computergrafik Grundlagen", "2026-10-13T12:00:00+00:00", "c")
    add_news(conn, "1", "Fragebogen.txt", ref="101")
    client = Client({"101": txt("Bitte füllen Sie das neue Dokument bis zur nächsten Stunde aus.")})
    ch = Channel()
    process_news(conn, client, ch, NOW)
    t = conn.execute("SELECT * FROM tasks").fetchone()
    assert datetime.fromisoformat(t["deadline"]) == datetime(2026, 10, 13, 10, 40, tzinfo=TZ)   # Beginn der nächsten Stunde
    assert "Fragebogen" in t["title"]


def test_next_lesson_after_the_posting_not_before(conn):
    add_lecture(conn, "Verteilte Systeme", "2026-10-13T06:00:00+00:00", "a")   # 08:00 – vor dem Posting
    add_lecture(conn, "Verteilte Systeme", "2026-10-20T08:40:00+00:00", "b")
    add_news(conn, "1", "Formular.txt", ref="101", ts="2026-10-13T10:00:00+02:00")
    process_news(conn, Client({"101": txt("Bitte bis zur nächsten Vorlesung ausfüllen.")}), Channel(), NOW)
    assert tasks.open_tasks(conn)[0]["deadline"].startswith("2026-10-20T10:40")


def test_unresolvable_next_lesson_still_creates_task_without_deadline(conn):
    add_news(conn, "1", "Fragebogen.txt", ref="101")
    ch = Channel()
    process_news(conn, Client({"101": txt("Bitte bis zur nächsten Stunde ausfüllen.")}, course="Unbekannter Kurs"), ch, NOW)
    t = conn.execute("SELECT * FROM tasks").fetchone()
    assert t["deadline"] is None                                     # nichts darf verloren gehen
    assert "kein passender Stundenplan-Termin" in ch.sent[0][0] and "/frist" in ch.sent[0][0]


# -- Altes Semester, keine Frist, Fließtext -----------------------------------------------------------------------------------
OLD_PLAN = "2 06.10.25 Abgabe1: Team (8.10.)\n3 13.10.25 Abgabe2: Thema\n19.01.26 Abgabe 4: Businessplan am 19.1.2026"


def test_last_semesters_plan_is_never_entered(conn):
    add_news(conn, "1", "Zeitplan_Projektaufgabe.xlsx.pdf", ref="102")
    ch = Channel()
    process_news(conn, Client({"102": txt(OLD_PLAN)}), ch, NOW)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    text, buttons = ch.sent[0]
    assert "Vergangenheit" in text and "Vorsemester" in text and "Nichts eingetragen" in text
    assert buttons == [[("➕ Als Aufgabe anlegen", "ilt:1")]]


def test_mixed_future_and_past_creates_only_future(conn):
    add_news(conn, "1", "Plan.txt", ref="102")
    text = "Abgabe 1: Team (8.10.2026)\nAbgabe 2: Thema (3.11.2026)\nAbgabe 3: Plan (24.11.2026)"
    ch = Channel()
    process_news(conn, Client({"102": txt(text)}), ch, NOW)
    titles = [t["title"] for t in tasks.open_tasks(conn)]
    assert len(titles) == 2 and all("Abgabe 1" not in t for t in titles)
    assert "1 Termin(e) in der Vergangenheit ignoriert" in ch.sent[0][0]


def test_assignment_without_deadline_is_announced_with_button(conn):
    add_news(conn, "1", "Projektaufgabe Teil 2.pdf", ref="103")
    ch = Channel()
    process_news(conn, Client({"103": txt("Praktische Arbeit: Die Bearbeitung erfolgt in Gruppen.")}), ch, NOW)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert "keine Frist erkannt" in ch.sent[0][0] and ch.sent[0][1] == [[("➕ Als Aufgabe anlegen", "ilt:1")]]


def test_lecture_slides_are_silent(conn):
    add_news(conn, "1", "Vorlesung 3 Folien.pdf", ref="104")
    ch = Channel()
    process_news(conn, Client({"104": txt("Folie 1: Einführung. Vorlesung von 9 bis 11 Uhr.")}), ch, NOW)
    assert ch.sent == [] and conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert conn.execute("SELECT state FROM ilias_news").fetchone()[0] == "seen"


def test_forum_post_text_is_used(conn):
    add_news(conn, "1", "Hausaufgabe", obj_type="frm", ref="55", headline="Neue Hausaufgabe",
             body="Bitte Kapitel 3 lesen und die Fragen beantworten, Abgabe bis 22.10. 12:00 Uhr.")
    process_news(conn, Client(), Channel(), NOW)
    assert tasks.open_tasks(conn)[0]["deadline"] == "2026-10-22T12:00+02:00"


# -- Robustheit ---------------------------------------------------------------------------------------------------------------------
def test_failed_message_is_retried_without_duplicate_tasks(conn):
    add_news(conn, "1", "Blatt.txt", ref="100")
    client = Client({"100": txt("Abgabe bis 20.10.2026 19 Uhr")})
    down = Channel(ok=False)
    process_news(conn, client, down, NOW)
    assert conn.execute("SELECT state, attempts FROM ilias_news").fetchone()[:] == ("new", 1)
    up = Channel(ok=True)
    process_news(conn, client, up, NOW)
    process_news(conn, client, up, NOW)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1          # nicht doppelt
    assert len(down.sent) == 1 and len(up.sent) == 1                                # danach Ruhe
    assert f"#{tasks.open_tasks(conn)[0]['id']}" in up.sent[0][0]                    # Meldung nennt die vorhandene Aufgabe


def test_network_error_is_retried_then_given_up(conn):
    add_news(conn, "1", "Blatt.txt", ref="100")
    flaky = Client(error=httpx.ConnectError("kein Netz"))
    for expected in (1, 2):
        process_news(conn, flaky, Channel(), NOW)
        row = conn.execute("SELECT state, attempts FROM ilias_news").fetchone()
        assert (row["state"], row["attempts"]) == ("new", expected)
    process_news(conn, flaky, Channel(), NOW)                                     # dritter Fehlschlag → aufgeben
    assert conn.execute("SELECT state FROM ilias_news").fetchone()[0] == "seen"


def test_unreadable_or_oversized_file_falls_back_to_title(conn):
    add_news(conn, "1", "Aufgabe Video.mp4", ref="100")
    ch = Channel()
    process_news(conn, Client({"100": (b"\x00\x01", "video/mp4", "v.mp4")}), ch, NOW)
    assert "keine Frist erkannt" in ch.sent[0][0]                                   # Titel klingt nach Aufgabe
    add_news(conn, "2", "Kaputt.pdf", ref="101")
    process_news(conn, Client({"101": (b"%PDF-kaputt", "application/pdf", "k.pdf")}), Channel(), NOW)   # wirft nicht
    assert conn.execute("SELECT state FROM ilias_news WHERE news_id='2'").fetchone()[0] == "seen"


def test_task_cap_per_item(conn):
    add_news(conn, "1", "Plan.txt", ref="100")
    text = "\n".join(f"Abgabe {i}: Teil {i} am {i}.12.2026" for i in range(1, 12))
    process_news(conn, Client({"100": txt(text)}), Channel(), NOW)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 8


def test_same_news_reprocessed_does_not_duplicate(conn):
    add_news(conn, "1", "Blatt.txt", ref="100")
    client = Client({"100": txt("Abgabe bis 20.10.2026 19 Uhr")})
    process_news(conn, client, Channel(), NOW)
    conn.execute("UPDATE ilias_news SET state='new'")
    conn.commit()
    process_news(conn, client, Channel(), NOW)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


# -- Ende-zu-Ende: ILIAS → Aufgabe → Kalender + Google Tasks ---------------------------------------------------------------------
def test_end_to_end_into_calendar(conn):
    add_news(conn, "1", "Blatt 3.txt", ref="100")
    process_news(conn, Client({"100": txt("Abgabe bis spätestens 20.10. 19 Uhr")}), Channel(), NOW)
    cal, tc, out = FakeCal(), FakeTasks(), Channel()
    planning_step(conn, cal, tc, Rules(horizon_days=14), out, NOW)
    blocks = [e for e in cal.events["planner"].values() if e["summary"].startswith("Abgabe")]
    assert len(blocks) == 1 and datetime.fromisoformat(blocks[0]["end"]["dateTime"]) <= datetime(2026, 10, 20, 19, 0, tzinfo=TZ)
    assert "Frist: 20.10.2026 19:00" in blocks[0]["description"]
    assert tc.only()["title"].startswith("Abgabe")         # Zeitfenster gefunden → Blocker + Begleit-Task zum Abhaken


def test_end_to_end_without_time_falls_back_to_google_task(conn):
    add_news(conn, "1", "Blatt 3.txt", ref="100")
    process_news(conn, Client({"100": txt("Abgabe bis spätestens 20.10. 19 Uhr")}), Channel(), NOW)
    cal, tc = FakeCal(), FakeTasks()
    for d in range(13, 21):                                # alles bis zur Frist belegt
        cal.busy(datetime(2026, 10, d, 9, tzinfo=TZ), datetime(2026, 10, d, 19, tzinfo=TZ))
    planning_step(conn, cal, tc, Rules(horizon_days=14), Channel(), NOW)
    google = tc.only()
    assert google["title"].startswith("Abgabe: Blatt 3") and google["due"].startswith("2026-10-20")
    assert not cal.events["planner"]


# -- Kurs ↔ Stundenplan -----------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("event,course,expected", [
    ("Verteilte Systeme", "Verteilte Systeme", 1.0),
    ("Projektmanagement & Methodik", "Projektmanagement und Methodik WS 2026_27", 1.0),
    ("Mensch & Maschine Interaktion", "Mensch & Maschine Interaktion 2026/27", 1.0),
    ("Computergrafik Grundlagen", "Verteilte Systeme", 0.0),
    ("Vert. Systeme", "Verteilte Systeme", 1.0),
])
def test_match_score(event, course, expected):
    assert match_score(event, course) == pytest.approx(expected)


def test_next_session_ambiguity_and_unknown(conn):
    add_lecture(conn, "Verteilte Systeme", "2026-10-14T08:00:00+00:00", "a")
    assert next_session(conn, "Verteilte Systeme WS 26", NOW)["uid"] == "a"
    assert next_session(conn, "Ganz anderer Kurs", NOW) is None
    assert next_session(conn, "", NOW) is None
    add_lecture(conn, "Eingebettete Systeme", "2026-10-15T08:00:00+00:00", "b")
    assert next_session(conn, "Systeme", NOW) is None                  # zwei verschiedene Veranstaltungen passen gleich gut


def test_helpers():
    assert clean_course_name("Projektmanagement und Methodik WS 2026_27") == "Projektmanagement und Methodik"
    assert clean_course_name("Mensch & Maschine Interaktion 2026/27") == "Mensch & Maschine Interaktion"
    assert task_title("Abgabe 2: Thema", "Plan.pdf", "Kurs") == "Abgabe 2: Thema (Kurs)"
    assert task_title("Abgabe", "Blatt 3.pdf", "Kurs") == "Abgabe: Blatt 3 (Kurs)"
    assert task_title("", "Blatt 3.pdf", "") == "Aufgabe: Blatt 3"


# -- Fehltreffer aus dem Durchlauf durch echte Kurse (Regression) --------------------------------------------------
@pytest.mark.parametrize("title", ["Klausur_Risiko_WS14.pdf", "Lösung_Risiko_WS16.pdf", "Musterlösung Blatt 2.pdf", "Altklausur 2019.pdf"])
def test_exams_and_solutions_are_never_assignments(conn, title):
    add_news(conn, "1", title, ref="100")
    ch = Channel()
    process_news(conn, Client({"100": txt("Aufgabe 1 ... Abgabe bis 20.10.2026 19 Uhr")}), ch, NOW)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0 and ch.sent == []


def test_far_future_date_without_year_asks_instead_of_entering(conn):
    """„Assignment until April 9“ (Datum ohne Jahr, ~6 Monate weg) ist vermutlich aus einem alten Semester."""
    add_news(conn, "1", "Assignment 1.txt", ref="100")
    ch = Channel()
    process_news(conn, Client({"100": txt("Assignment until April 9\nAssignment until April 16")}), ch, NOW)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    text, buttons = ch.sent[0]
    assert "Jahr unklar" in text and "nichts eingetragen" in text
    assert buttons == [[("➕ Als Aufgabe anlegen", "ilt:1")]]


def test_ambiguous_semester_end_rule_is_not_entered(conn):
    add_news(conn, "1", "Organisation.txt", ref="100")
    ch = Channel()
    process_news(conn, Client({"100": txt("Abgabe Ende des Semesters (jeweils 31.08. SS / 15. März WS)")}), ch, NOW)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0 and "Jahr unklar" in ch.sent[0][0]


def test_near_date_without_year_is_still_entered(conn):
    add_news(conn, "1", "Blatt.txt", ref="100")
    process_news(conn, Client({"100": txt("Abgabe bis spätestens 5.11. 12 Uhr")}), Channel(), NOW)
    assert tasks.open_tasks(conn)[0]["deadline"] == "2026-11-05T12:00+01:00"      # 23 Tage entfernt → plausibel


def test_explicit_year_is_trusted_even_far_away(conn):
    add_news(conn, "1", "Plan.txt", ref="100")
    process_news(conn, Client({"100": txt("Abgabetermin: 31.03.2027")}), Channel(), NOW)
    assert tasks.open_tasks(conn)[0]["deadline"] == "2027-03-31"
