import json
from datetime import datetime

import httpx
import pytest

from planner import areas
from planner.bot_logic import handle_callback, handle_message
from planner.db import connect
from planner.llm import LlmError, OllamaClient, SpokenTask
from planner.rules import TZ
from planner.voice import (
    MAX_SECONDS,
    clean_title,
    handle_voice,
    spoken_clock,
    spoken_minutes,
    spoken_priority,
)

NOW = datetime(2026, 10, 16, 9, 0, tzinfo=TZ)   # Freitag


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.db")


class Ears:
    def __init__(self, text="", error=None):
        self.text, self.error, self.calls = text, error, 0

    def transcribe(self, audio):
        self.calls += 1
        if self.error:
            raise self.error
        return self.text


class Brain:
    def __init__(self, task=None, error=None):
        self.task, self.error, self.areas = task, error, None

    def extract_spoken_task(self, text, *, today, areas):
        self.areas = areas
        if self.error:
            raise self.error
        return self.task


SPOKEN = "Bis Freitag muss ich den Bericht für die Arbeit schreiben, dauert so zwei Stunden, ist wichtig"


def test_voice_preview_then_accept(conn):
    areas.ensure_path(conn, "Arbeit", actor="user")
    brain = Brain(SpokenTask("Bericht schreiben", "Bis Freitag", 120, "Arbeit"))
    text, buttons = handle_voice(conn, Ears(SPOKEN), brain, b"ogg", duration=8, message_id=5, now=NOW)
    assert brain.areas == ["Arbeit"]
    assert f"„{SPOKEN}“" in text and "➕ Bericht schreiben" in text
    assert "bis 23.10." in text and "~2 h" in text and "🔴 hoch" in text and "#Arbeit" in text
    assert buttons == [[("✅ Anlegen", "vo:1:ok"), ("✏️ Titel", "vo:1:edit"), ("❌ Verwerfen", "vo:1:no")]]
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0   # erst nach Bestätigung

    r = handle_callback(conn, "vo:1:ok")
    assert r.changed and "Aufgabe #1 angelegt" in r.text
    t = conn.execute("SELECT * FROM tasks").fetchone()
    assert (t["title"], t["deadline"], t["estimate_min"], t["priority_boost"], t["source"]) == \
        ("Bericht schreiben", "2026-10-23", 120, 1.5, "voice")
    assert areas.path_of(conn, t["area_id"]) == "Arbeit"
    assert conn.execute("SELECT transcript FROM voice_notes").fetchone()[0] is None   # Wortlaut gelöscht
    assert "Schon erledigt" in handle_callback(conn, "vo:1:ok").text   # kein Doppelanlegen


@pytest.mark.parametrize("copied", ["morgen", ""])   # Modell kopiert „morgen“ – oder lässt es weg
def test_voice_morgen_means_tomorrow(conn, copied):
    spoken = "Morgen muss ich den Bericht schreiben"
    handle_voice(conn, Ears(spoken), Brain(SpokenTask("Bericht schreiben", copied, 60, "")), b"ogg",
                 duration=4, message_id=7, now=NOW)
    r = handle_callback(conn, "vo:1:ok")
    assert "am 17.10." in r.text
    t = conn.execute("SELECT deadline, not_before FROM tasks").fetchone()
    assert (t["deadline"], t["not_before"]) == ("2026-10-17", "2026-10-17")


def test_voice_time_range_is_fixed_slot(conn):
    spoken = "Termin morgen von 16:30 bis 17:30 beim Zahnarzt"
    text, _ = handle_voice(conn, Ears(spoken), Brain(SpokenTask("Zahnarzt", "morgen", 0, "")), b"ogg",
                           duration=4, message_id=7, now=NOW)
    assert "Zeit: fest Sa 17.10. 16:30–17:30" in text
    handle_callback(conn, "vo:1:ok")
    t = conn.execute("SELECT deadline, fixed_start, fixed_end, estimate_min FROM tasks").fetchone()
    assert tuple(t) == (None, "2026-10-17T16:30+02:00", "2026-10-17T17:30+02:00", 60)


@pytest.mark.parametrize("spoken,guess,area", [
    ("Kaffee mit Familie am Freitag", "Uni", None),                       # Modell rät – zählt nicht
    ("Für die Uni Statistik wiederholen", "Uni/Statistik", "Uni/Statistik"),
    ("Privat: Geschenk für Mama kaufen", "", "Privat"),
    ("Hausarbeit gliedern", "", None),                                    # „Arbeit“ nur als eigenes Wort
])
def test_voice_area_only_when_named(conn, spoken, guess, area):
    areas.ensure_path(conn, "Uni/Statistik", actor="user")
    handle_voice(conn, Ears(spoken), Brain(SpokenTask("X", "", 30, guess)), b"ogg",
                 duration=4, message_id=7, now=NOW)
    assert conn.execute("SELECT area FROM voice_notes").fetchone()[0] == area


@pytest.mark.parametrize("spoken,clock", [
    ("morgen von halb fünf bis halb sechs", "morgen von 16:30 bis 17:30"),
    ("um viertel nach 3", "um 15:15"),
    ("Freitag viertel vor neun", "Freitag 08:45"),
    ("morgen früh um 6", "morgen früh um 06:00"),
    ("Samstag um 5 Uhr 30", "Samstag um 17:30 Uhr"),
    ("von 4 bis 6", "von 16:00 bis 18:00"),
    ("um 10 Uhr", "um 10:00 Uhr"),
    ("am 20.10. um 14:30", "am 20.10. um 14:30"),          # schon Ziffern: bleibt
    ("bis 3 Tage vorher", "bis 3 Tage vorher"),             # keine Uhrzeit
])
def test_spoken_clock(spoken, clock):
    assert spoken_clock(spoken) == clock


@pytest.mark.parametrize("spoken,minutes", [
    ("dauert zehn Minuten", 10), ("so zwei Stunden", 120), ("eine halbe Stunde", 30),
    ("anderthalb Stunden", 90), ("1,5 h", 90), ("zwei einhalb Stunden", 150), ("ohne Dauer", None),
])
def test_spoken_minutes(spoken, minutes):
    assert spoken_minutes(spoken) == minutes


@pytest.mark.parametrize("spoken,prio", [
    ("ist wichtig", "hoch"), ("das ist dringend", "hoch"), ("ist aber nicht so dringend", "niedrig"),
    ("irgendwann mal", "niedrig"), ("Bericht schreiben", "mittel"),
])
def test_spoken_priority(spoken, prio):
    assert spoken_priority(spoken) == prio


@pytest.mark.parametrize("title,clean", [
    ("Kaffee mit Familie anlegen", "Kaffee mit Familie"),
    ("Café mit Familie am Samstag", "Café mit Familie"),
    ("Präsentation für die Arbeit vorbereiten", "Präsentation vorbereiten"),
    ("Zahnarzt morgen von halb 5 bis halb 6 eintragen", "Zahnarzt"),
    ("Termin: Steuerberater", "Steuerberater"),
    ("ich muss noch Mama anrufen", "Mama anrufen"),
    ("anlegen", "anlegen"),                               # bleibt nichts übrig: nicht leer machen
    # ohne Modell ist der Titel der ganze Satz
    ("Trag mal ein, dass ich bis Freitag die Übungsblätter abgeben muss, dauert so zwei Stunden",
     "Übungsblätter abgeben"),
    ("Erinnere mich daran, dass ich nächste Woche Dienstag die Präsentation vorbereiten muss, ist wichtig",
     "Präsentation vorbereiten"),
    ("Ich sollte irgendwann mal die Steuererklärung machen, ist aber nicht so dringend", "Steuererklärung machen"),
    ("Ich hab vergessen, die Bücher zu verlängern, das muss bis Montag passieren", "Bücher verlängern"),
    ("Vokabeln lernen", "Vokabeln lernen"),
])
def test_clean_title(title, clean):
    assert clean_title(title, today=NOW.date()) == clean


def test_voice_spoken_details_beat_the_model(conn):
    spoken = "Ähm, Mama zurückrufen, heute noch, dauert zehn Minuten, ist wichtig"
    brain = Brain(SpokenTask("Mama zurückrufen anlegen", "bis Freitag", 600, ""))   # Frist erfunden, 600 min
    handle_voice(conn, Ears(spoken), brain, b"ogg", duration=4, message_id=7, now=NOW)
    n = conn.execute("SELECT title, deadline, estimate_min, priority FROM voice_notes").fetchone()
    assert tuple(n) == ("Mama zurückrufen", "2026-10-16", 10, "hoch")


def test_voice_spoken_half_past_is_fixed_afternoon_slot(conn):
    spoken = "Ich muss morgen von halb fünf bis halb sechs noch zum Zahnarzt"
    handle_voice(conn, Ears(spoken), Brain(SpokenTask("Zahnarzt", "morgen", 30, "")), b"ogg",
                 duration=4, message_id=7, now=NOW)
    n = conn.execute("SELECT fixed_start, fixed_end FROM voice_notes").fetchone()
    assert tuple(n) == ("2026-10-17T16:30+02:00", "2026-10-17T17:30+02:00")


def test_voice_edit_title_then_accept(conn):
    handle_voice(conn, Ears("Kaffe mit Familie"), Brain(SpokenTask("Kaffe", "", 60, "")), b"ogg",
                 duration=4, message_id=7, now=NOW)
    r = handle_callback(conn, "vo:1:edit")
    assert "richtigen Titel" in r.text and r.buttons
    r = handle_message(conn, "Kaffee mit Familie", now=NOW)
    assert "➕ Kaffee mit Familie" in r.text and r.buttons[0][0] == ("✅ Anlegen", "vo:1:ok")
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0   # Titel geändert, nichts angelegt
    handle_callback(conn, "vo:1:ok")
    assert conn.execute("SELECT title FROM tasks").fetchone()[0] == "Kaffee mit Familie"
    assert "#2 Wäsche waschen" in handle_message(conn, "Wäsche waschen", now=NOW).text   # danach wieder normal


def test_voice_reject(conn):
    handle_voice(conn, Ears("Irgendwas"), Brain(SpokenTask("x", "", None, "")), b"", duration=3,
                 message_id=1, now=NOW)
    r = handle_callback(conn, "vo:1:no")
    assert "Verworfen" in r.text and not r.changed
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_model_down_falls_back_to_text_parser(conn):
    text, _ = handle_voice(conn, Ears("Wäsche waschen bis morgen 1h"), Brain(error=LlmError("down")), b"",
                           duration=3, message_id=1, now=NOW)
    assert "➕ Wäsche waschen" in text and "bis 17.10." in text and "~1 h" in text


def test_too_long_empty_and_broken_audio(conn):
    ears = Ears("x")
    text, buttons = handle_voice(conn, ears, Brain(), b"", duration=MAX_SECONDS + 1, message_id=1, now=NOW)
    assert "zu lang" in text and buttons == [] and ears.calls == 0
    assert "nichts verstanden" in handle_voice(conn, Ears(""), Brain(), b"", duration=2, message_id=1, now=NOW)[0]
    assert "technischer Fehler" in handle_voice(conn, Ears(error=RuntimeError()), Brain(), b"", duration=2,
                                                message_id=1, now=NOW)[0]


def test_ollama_spoken_task_schema_limits_areas():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": json.dumps(
            {"title": "Bericht", "deadline_text": "bis Freitag", "effort_min": 120, "area": "Arbeit"})}})

    client = OllamaClient("http://127.0.0.1:11434", "m", client=httpx.Client(transport=httpx.MockTransport(handler)))
    t = client.extract_spoken_task(SPOKEN, today=NOW.date(), areas=["Arbeit", "Uni"])
    assert t == SpokenTask("Bericht", "bis Freitag", 120, "Arbeit")
    assert seen["format"]["properties"]["area"]["enum"] == ["", "Arbeit", "Uni"]
    assert seen["options"]["num_gpu"] == 0
