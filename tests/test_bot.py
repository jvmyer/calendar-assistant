import logging
from datetime import date, datetime

import pytest

from planner import changelog, proposals, tasks
from planner.bot_logic import handle_callback, handle_message
from planner.db import connect
from planner.quickadd import parse_quick
from planner.rules import TZ
from planner.service import _notify_proposals
from planner.telegram_bot import TelegramNotifier, quiet_http_loggers

TODAY = date(2026, 10, 13)   # Dienstag
NOW = datetime(2026, 10, 13, 9, 0, tzinfo=TZ)
TOKEN = "123456789:AAEXAMPLEtokenEXAMPLEtokenEXAMPLE12345"


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.db")


# -- Schnellerfassung ---------------------------------------------------------------------
@pytest.mark.parametrize("text,title,deadline,est,area", [
    ("Bericht schreiben bis 20.10. 2h #Uni/Modul-X", "Bericht schreiben", "2026-10-20", 120, "Uni/Modul-X"),
    ("Zahnarzt anrufen bis morgen 16:00 15min", "Zahnarzt anrufen", "2026-10-14T16:00+02:00", 15, None),
    ("Mathe lernen ca. 1,5 h bis freitag", "Mathe lernen", "2026-10-16", 90, None),
    ("Bis dienstag Steuer machen", "Steuer machen", "2026-10-13", None, None),
    ("Einkaufen", "Einkaufen", None, None, None),
    ("Kapitel 3 lesen muss sein", "Kapitel 3 lesen muss sein", None, None, None),   # „3 m…“ ist kein Aufwand
])
def test_parse_quick(text, title, deadline, est, area):
    q = parse_quick(text, today=TODAY)
    assert (q.title, q.deadline, q.estimate_min, q.area) == (title, deadline, est, area)


def test_parse_quick_needs_title():
    with pytest.raises(ValueError):
        parse_quick("bis 20.10. 2h", today=TODAY)


# -- Nachrichten ----------------------------------------------------------------------------
def test_text_creates_task_via_telegram_source(conn):
    r = handle_message(conn, "Bericht schreiben bis 20.10. 2h #Uni", message_id=7, now=NOW)
    row = conn.execute("SELECT * FROM tasks").fetchone()
    assert (row["title"], row["source"], row["deadline"], row["estimate_min"]) == (
        "Bericht schreiben", "telegram", "2026-10-20", 120)
    assert r.changed and "Aufgabe angelegt" in r.text and "bis 20.10." in r.text and "2 h" in r.text
    assert [b[1] for b in r.buttons[0]] == [f"done:{row['id']}", f"drop:{row['id']}"]
    log = conn.execute("SELECT actor FROM changelog WHERE action='task.created'").fetchone()
    assert log["actor"] == "user"


def test_redelivered_message_is_not_duplicated(conn):
    handle_message(conn, "Bericht", message_id=7, now=NOW)
    r = handle_message(conn, "Bericht", message_id=7, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1 and not r.changed


def test_unparseable_text_gives_hint_and_creates_nothing(conn):
    r = handle_message(conn, "bis 20.10. 2h", now=NOW)
    assert "Titel" in r.text and not r.changed
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_commands(conn):
    assert "Schreib mir einfach" in handle_message(conn, "/start").text
    assert "kenne ich nicht" in handle_message(conn, "/gibtsnicht").text
    assert handle_message(conn, "/aufgaben").text.startswith("Keine offenen")
    tid = tasks.add_task(conn, title="A", source="manual", actor="user", estimate_min=30)
    r = handle_message(conn, "/aufgaben")
    assert f"#{tid} A" in r.text and r.buttons
    done = handle_message(conn, f"/erledigt {tid}")
    assert "erledigt" in done.text and done.changed
    assert conn.execute("SELECT status FROM tasks").fetchone()[0] == "done"
    assert "Welche Aufgabe" in handle_message(conn, "/erledigt abc").text
    assert "Welche Aufgabe" in handle_message(conn, "/verwerfen 999").text
    assert "task.status" in handle_message(conn, "/log 3").text


def test_command_with_bot_suffix(conn):
    assert "Schreib mir einfach" in handle_message(conn, "/start@mein_bot").text


def test_today_overview(conn):
    assert handle_message(conn, "/heute", now=NOW).text == "Heute ist nichts geplant."
    conn.execute(
        "INSERT INTO external_events(source, uid, title, start, end, all_day, location, first_seen, last_seen) "
        "VALUES ('raumzeit','1','Vert. Systeme','2026-10-13T13:40:00+00:00','2026-10-13T15:10:00+00:00',0,'R-203','x','x')")
    tid = tasks.add_task(conn, title="Blockiert", source="manual", actor="user", estimate_min=60)
    conn.execute("INSERT INTO blocks(task_id,start,end,gcal_event_id,calendar_id,created_at) "
                 "VALUES (?,?,?,?,?,?)", (tid, "2026-10-13T08:00:00+00:00", "2026-10-13T09:00:00+00:00", "e", "c", "x"))
    tasks.add_task(conn, title="Abgabe", source="manual", actor="user", deadline="2026-10-13")
    tasks.add_task(conn, title="Verpasst", source="manual", actor="user", deadline="2026-10-10")
    tasks.add_task(conn, title="Später", source="manual", actor="user", deadline="2026-10-30")
    conn.commit()
    text = handle_message(conn, "/heute", now=NOW).text
    assert "15:40–17:10 Vert. Systeme (R-203)" in text and "10:00–11:00 Blockiert" in text
    assert "Abgabe" in text and "⏰ überfällig: #" in text and "Verpasst" in text and "Später" not in text


# -- Buttons -----------------------------------------------------------------------------------
def make_event_proposal(conn):
    return proposals.add_proposal(conn, title="KI-Barcamp", kind="event", source="mail", actor="system",
                                  source_ref="m", start="2026-11-05T13:30:00+01:00", end="2026-11-05T17:30:00+01:00")


def test_accept_and_reject_buttons(conn):
    a, b = make_event_proposal(conn), proposals.add_proposal(
        conn, title="Waffeln", kind="task", source="mail", actor="system", source_ref="w")
    r = handle_callback(conn, f"acc:{a}")
    assert "wird in den Kalender eingetragen" in r.text and r.changed
    r2 = handle_callback(conn, f"rej:{b}")
    assert "verworfen" in r2.text and not r2.changed
    assert "schon accepted" in handle_callback(conn, f"acc:{a}").text   # Doppelklick ändert nichts
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0   # abgelehnt → nie eine Aufgabe


def test_accept_task_proposal_creates_task(conn):
    p = proposals.add_proposal(conn, title="Anmeldung", kind="task", source="mail", actor="system", source_ref="a")
    assert "als Aufgabe #1 übernommen" in handle_callback(conn, f"acc:{p}").text


def test_task_buttons_and_garbage(conn):
    tid = tasks.add_task(conn, title="A", source="manual", actor="user")
    assert "verworfen" in handle_callback(conn, f"drop:{tid}").text
    assert handle_callback(conn, "quatsch").text == "Unbekannte Aktion."
    assert handle_callback(conn, "acc:999").text == "Das gibt es nicht mehr."
    assert handle_callback(conn, "boom:1").text == "Unbekannte Aktion."


# -- Notifier ----------------------------------------------------------------------------------
class Resp:
    def __init__(self, code=200):
        self.status_code = code


def test_notifier_sends_proposal_with_buttons():
    calls = []
    n = TelegramNotifier(TOKEN, 42, post=lambda url, json, timeout: calls.append((url, json)) or Resp())
    assert n.send_proposal(3, "💡 Vorschlag") is True
    url, payload = calls[0]
    assert url.endswith("/sendMessage") and payload["chat_id"] == 42
    assert [b["callback_data"] for b in payload["reply_markup"]["inline_keyboard"][0]] == ["acc:3", "rej:3"]


def test_notifier_truncates_long_text():
    calls = []
    n = TelegramNotifier(TOKEN, 42, post=lambda url, json, timeout: calls.append(json) or Resp())
    n.send("x" * 10000)
    assert len(calls[0]["text"]) == 4000


def test_notifier_never_leaks_token_and_never_raises(caplog):
    def boom(url, json, timeout):
        raise RuntimeError(f"Verbindung zu {url} gescheitert")   # Bibliotheken packen die URL gern in den Text

    with caplog.at_level(logging.DEBUG):
        assert TelegramNotifier(TOKEN, 42, post=boom).send("hi") is False
        assert TelegramNotifier(TOKEN, 42, post=lambda url, json, timeout: Resp(401)).send("hi") is False
    assert TOKEN not in caplog.text and "AAEXAMPLE" not in caplog.text
    assert "RuntimeError" in caplog.text and "401" in caplog.text


def test_quiet_http_loggers():
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    quiet_http_loggers()
    assert logging.getLogger("httpx").level == logging.WARNING


# -- Vorschläge über den Kanal melden ----------------------------------------------------------
class ButtonChannel:
    def __init__(self, ok):
        self.ok, self.sent = ok, []

    def send(self, text):
        return True

    def send_proposal(self, pid, text):
        self.sent.append(pid)
        return self.ok


def test_proposal_marked_notified_only_if_delivery_worked(conn):
    pid = make_event_proposal(conn)
    down = ButtonChannel(ok=False)
    _notify_proposals(conn, down)
    _notify_proposals(conn, down)
    assert down.sent == [pid, pid]   # nicht zugestellt → beim nächsten Lauf nochmal
    up = ButtonChannel(ok=True)
    _notify_proposals(conn, up)
    _notify_proposals(conn, up)
    assert up.sent == [pid]          # zugestellt → nur einmal


def test_change_log_records_user_actions(conn):
    handle_message(conn, "Bericht", message_id=1, now=NOW)
    assert changelog.recent(conn, 1)[0]["actor"] == "user"
