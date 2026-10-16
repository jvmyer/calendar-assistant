import json
from datetime import datetime

import httpx
import pytest

from planner import areas
from planner.bot_logic import handle_callback
from planner.db import connect
from planner.llm import LlmError, OllamaClient, SpokenTask
from planner.rules import TZ
from planner.voice import MAX_SECONDS, handle_voice

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
    brain = Brain(SpokenTask("Bericht schreiben", "Bis Freitag", 120, "hoch", "Arbeit"))
    text, buttons = handle_voice(conn, Ears(SPOKEN), brain, b"ogg", duration=8, message_id=5, now=NOW)
    assert brain.areas == ["Arbeit"]
    assert f"„{SPOKEN}“" in text and "➕ Bericht schreiben" in text
    assert "bis 23.10." in text and "~2 h" in text and "🔴 hoch" in text and "#Arbeit" in text
    assert buttons == [[("✅ Anlegen", "vo:1:ok"), ("❌ Verwerfen", "vo:1:no")]]
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0   # erst nach Bestätigung

    r = handle_callback(conn, "vo:1:ok")
    assert r.changed and "Aufgabe #1 angelegt" in r.text
    t = conn.execute("SELECT * FROM tasks").fetchone()
    assert (t["title"], t["deadline"], t["estimate_min"], t["priority_boost"], t["source"]) == \
        ("Bericht schreiben", "2026-10-23", 120, 1.5, "voice")
    assert areas.path_of(conn, t["area_id"]) == "Arbeit"
    assert conn.execute("SELECT transcript FROM voice_notes").fetchone()[0] is None   # Wortlaut gelöscht
    assert "Schon erledigt" in handle_callback(conn, "vo:1:ok").text   # kein Doppelanlegen


def test_voice_reject(conn):
    handle_voice(conn, Ears("Irgendwas"), Brain(SpokenTask("x", "", None, "mittel", "")), b"", duration=3,
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
            {"title": "Bericht", "deadline_text": "bis Freitag", "effort_min": 120, "priority": "hoch",
             "area": "Arbeit"})}})

    client = OllamaClient("http://127.0.0.1:11434", "m", client=httpx.Client(transport=httpx.MockTransport(handler)))
    t = client.extract_spoken_task(SPOKEN, today=NOW.date(), areas=["Arbeit", "Uni"])
    assert t == SpokenTask("Bericht", "bis Freitag", 120, "hoch", "Arbeit")
    assert seen["format"]["properties"]["area"]["enum"] == ["", "Arbeit", "Uni"]
    assert seen["options"]["num_gpu"] == 0
