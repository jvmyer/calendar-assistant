import json
from datetime import datetime
from urllib.parse import unquote

import httpx
import pytest

from planner.config import Settings
from planner.connectors.owa import MailHeader, OwaAuthError, OwaClient, sync_inbox
from planner.db import connect, get_meta
from planner.deadlines import resolve_phrase
from planner.llm import LlmError, MailVerdict, OllamaClient, parse_verdict, today_text
from planner.mail_flow import process_mails
from planner.rules import TZ
from planner.service import mail_step
from test_ilias_flow import Channel

NOW = datetime(2026, 10, 16, 9, 0, tzinfo=TZ)   # Freitag


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.db")


# -- Frist aus der Textstelle ---------------------------------------------------------------------
@pytest.mark.parametrize("phrase, expected", [
    ("bis Sonntagabend", "2026-10-18"),
    ("bis Mittwoch", "2026-10-21"),
    ("bis Freitag, 23.10.", "2026-10-23"),
    ("bis zum 29.01.2027", "2027-01-29"),
    ("vom 02.11. bis 13.11.2026", "2026-11-13"),   # Zeitraum: das Ende zählt
])
def test_resolve_phrase(phrase, expected):
    d = resolve_phrase(phrase, posted=NOW, now=NOW)
    assert d is not None and d.when.date().isoformat() == expected


def test_resolve_phrase_empty_or_past():
    assert resolve_phrase("", posted=NOW, now=NOW) is None
    assert resolve_phrase("bis 01.10.2026", posted=NOW, now=NOW) is None


# -- Modell-Antwort ---------------------------------------------------------------------------------
def test_parse_verdict():
    v = parse_verdict('{"kind": "Aufgabe", "title": " Folien ", "deadline_text": "bis Sonntag", "effort_min": 90}')
    assert v == MailVerdict("aufgabe", "Folien", "bis Sonntag", 90)
    assert parse_verdict('{"kind": "nichts", "title": "", "deadline_text": "", "effort_min": 0}').effort_min is None


@pytest.mark.parametrize("raw", ["kein json", '{"title": "x"}', '{"kind": "vielleicht"}'])
def test_parse_verdict_rejects_garbage(raw):
    with pytest.raises(LlmError):
        parse_verdict(raw)


def test_ollama_client_sends_mail_only_to_local_model():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": json.dumps(
            {"kind": "vorschlag", "title": "Spieleabend", "deadline_text": "", "effort_min": 0})}})

    client = OllamaClient("http://127.0.0.1:11434", "qwen3:4b", client=httpx.Client(transport=httpx.MockTransport(handler)))
    v = client.classify_mail("Fachschaft", "Spieleabend", "x" * 10_000, today=NOW.date())
    assert v.kind == "vorschlag"
    assert seen["url"] == "http://127.0.0.1:11434/api/chat"
    assert "Freitag, 16.10.2026" in seen["body"]["messages"][0]["content"]
    assert len(seen["body"]["messages"][1]["content"]) < 3200   # gekürzt
    assert today_text(NOW.date()) == "Freitag, 16.10.2026"


# -- Posteingang ------------------------------------------------------------------------------------
def hdr(i, received=NOW, subject="Betreff"):
    return MailHeader(f"id{i}", received, "Prof. Weber", subject)


def test_sync_inbox_first_run_is_silent_then_new(conn):
    r = sync_inbox(conn, [hdr(1), hdr(2)], now=NOW)
    assert r.initial and r.stored == 2 and r.new == 0
    r = sync_inbox(conn, [hdr(1), hdr(2), hdr(3), hdr(4, received=datetime(2026, 9, 1, tzinfo=TZ))], now=NOW)
    assert not r.initial and r.stored == 2 and r.new == 1   # alte Mail nie als „neu“
    assert [r[0] for r in conn.execute("SELECT item_id FROM mails WHERE state = 'new'")] == ["id3"]


# -- Verarbeitung -------------------------------------------------------------------------------------
class Mailbox:
    def __init__(self, bodies=None, error=None):
        self.bodies, self.error = bodies or {}, error

    def get_body(self, item_id):
        if self.error:
            raise self.error
        return self.bodies.get(item_id, "")


class Model:
    def __init__(self, verdicts=None, error=None):
        self.verdicts, self.error, self.calls = verdicts or {}, error, 0

    def classify_mail(self, sender, subject, body, *, today):
        self.calls += 1
        if self.error:
            raise self.error
        return self.verdicts[subject]


def add_mail(conn, item_id, subject, sender="Lisa"):
    conn.execute("INSERT INTO mails(item_id, received, sender, subject, first_seen, state) VALUES (?,?,?,?, 'x', 'new')",
                 (item_id, NOW.isoformat(), sender, subject))
    conn.commit()


def test_task_proposal_and_nothing(conn):
    add_mail(conn, "a", "Folien bis Sonntag?")
    add_mail(conn, "b", "Spieleabend")
    add_mail(conn, "c", "Newsletter")
    model = Model({
        "Folien bis Sonntag?": MailVerdict("aufgabe", "Folien Marktanalyse", "bis Sonntagabend", 60),
        "Spieleabend": MailVerdict("vorschlag", "Spieleabend", "", None),
        "Newsletter": MailVerdict("nichts", "", "", None),
    })
    ch = Channel()
    assert process_mails(conn, Mailbox(), model, ch, NOW) == 3
    task = conn.execute("SELECT * FROM tasks").fetchone()
    assert (task["title"], task["source"], task["deadline"], task["estimate_min"]) == \
        ("Folien Marktanalyse", "mail", "2026-10-18", 60)
    prop = conn.execute("SELECT * FROM proposals").fetchone()
    assert prop["title"] == "Spieleabend" and prop["state"] == "pending"   # nur nach Bestätigung in den Kalender
    assert len(ch.sent) == 1 and "#1 Folien Marktanalyse – bis 18.10." in ch.sent[0][0]
    assert ch.sent[0][1] == [[("🗑 #1 verwerfen", "drop:1")]]
    assert dict(conn.execute("SELECT item_id, verdict FROM mails").fetchall()) == \
        {"a": "aufgabe", "b": "vorschlag", "c": "nichts"}


def test_task_without_phrase_uses_rules_on_body_and_never_drops_task(conn):
    add_mail(conn, "a", "Abgabe Blatt 3")
    add_mail(conn, "b", "Rückmeldung")
    model = Model({"Abgabe Blatt 3": MailVerdict("aufgabe", "Blatt 3 abgeben", "", None),
                   "Rückmeldung": MailVerdict("aufgabe", "Rückmeldung geben", "", None)})
    process_mails(conn, Mailbox({"a": "Bitte Blatt 3 bis 23.10. abgeben."}), model, Channel(), NOW)
    rows = {r["title"]: r["deadline"] for r in conn.execute("SELECT * FROM tasks")}
    assert rows == {"Blatt 3 abgeben": "2026-10-23", "Rückmeldung geben": None}


def test_model_down_retries_then_reports(conn):
    add_mail(conn, "a", "Wichtig")
    ch, model = Channel(), Model(error=LlmError("ConnectError"))
    for _ in range(2):
        process_mails(conn, Mailbox(), model, ch, NOW)
    assert ch.sent == [] and conn.execute("SELECT state FROM mails").fetchone()[0] == "new"
    process_mails(conn, Mailbox(), model, ch, NOW)
    assert "nicht automatisch gelesen" in ch.sent[0][0] and "Wichtig" in ch.sent[0][0]
    assert conn.execute("SELECT state FROM mails").fetchone()[0] == "seen"


def test_failed_message_is_repeated_without_duplicate_task(conn):
    add_mail(conn, "a", "Abgabe")
    model = Model({"Abgabe": MailVerdict("aufgabe", "Abgabe", "bis Mittwoch", None)})
    process_mails(conn, Mailbox(), model, Channel(ok=False), NOW)
    ch = Channel()
    process_mails(conn, Mailbox(), model, ch, NOW)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert len(ch.sent) == 1


# -- Dienstschritt ------------------------------------------------------------------------------------
def settings(tmp_path, **kw):
    base = {"data_dir": tmp_path, "google_client_id": "", "google_client_secret": "", "hka_user": "u", "hka_password": "p",
                "mail_enabled": True}
    return Settings(**{**base, **kw})


class FakeOwa(Mailbox):
    def __init__(self, headers=(), auth_error=False, **kw):
        super().__init__(**kw)
        self.headers, self.auth_error, self.closed = list(headers), auth_error, False

    def list_inbox(self, limit=25):
        if self.auth_error:
            raise OwaAuthError("Login abgelehnt")
        return self.headers

    def close(self):
        self.closed = True


class FakeModel(Model):
    def close(self):
        pass


def test_mail_step_disabled_without_opt_in(conn, tmp_path):
    assert mail_step(conn, settings(tmp_path, mail_enabled=False), Channel(), NOW) is None


def test_mail_step_blocks_after_auth_error(conn, tmp_path):
    ch, owa = Channel(), FakeOwa(auth_error=True)
    mail_step(conn, settings(tmp_path), ch, NOW, owa_factory=lambda: owa, llm_factory=FakeModel)
    assert get_meta(conn, "mail_blocked") == "1" and "mail-login" in ch.sent[0][0] and owa.closed
    calls = []
    mail_step(conn, settings(tmp_path), ch, NOW, owa_factory=lambda: calls.append(1), llm_factory=FakeModel,
              force=True)
    assert calls == []   # gesperrt: kein weiterer Login-Versuch


def test_mail_step_hourly(conn, tmp_path):
    owa = FakeOwa([hdr(1)])
    assert mail_step(conn, settings(tmp_path), Channel(), NOW, owa_factory=lambda: owa, llm_factory=FakeModel) == 0
    later = NOW.replace(minute=30)
    assert mail_step(conn, settings(tmp_path), Channel(), later, owa_factory=lambda: owa, llm_factory=FakeModel) is None


# -- OWA-Client (gegen nachgebauten Server) -----------------------------------------------------------
FIND = {"Body": {"ResponseMessages": {"Items": [{"ResponseClass": "Success", "RootFolder": {"Items": [
    {"ItemId": {"Id": "AAA="}, "Subject": "Übungsblatt 3", "DateTimeReceived": "2026-10-16T07:00:00Z",
     "From": {"Mailbox": {"Name": "Prof. Weber", "EmailAddress": "weber@example.org"}}}]}}]}}}
GET = {"Body": {"ResponseMessages": {"Items": [{"ResponseClass": "Success", "Items": [
    {"Body": {"BodyType": "Text", "Value": "Bitte bis Freitag abgeben."}}]}]}}}


class FakeOwaServer:
    def __init__(self, accept_login=True, session_valid=False):
        self.accept_login, self.session_valid, self.logins, self.actions = accept_login, session_valid, 0, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/owa/auth.owa":
            self.logins += 1
            if not self.accept_login:
                return httpx.Response(302, headers={"Location": "/owa/auth/logon.aspx?reason=2"})
            self.session_valid = True
            return httpx.Response(302, headers={"Location": "/owa/", "Set-Cookie": "cadata=s3cret; Path=/"})
        if path == "/owa/auth/logon.aspx":
            return httpx.Response(200, text="<form>logon</form>")
        if not self.session_valid:
            return httpx.Response(302, headers={"Location": "/owa/auth/logon.aspx"})
        if path == "/owa/":
            return httpx.Response(200, text="<html>owa</html>", headers={"Set-Cookie": "X-OWA-CANARY=can1; Path=/"})
        if path == "/owa/service.svc":
            assert request.headers["X-OWA-CANARY"] == "can1"
            action = request.headers["Action"]
            payload = json.loads(unquote(request.headers["X-OWA-UrlPostData"]))
            self.actions.append((action, payload["Body"]["__type"]))
            return httpx.Response(200, json=FIND if action == "FindItem" else GET)
        return httpx.Response(404)


def owa_client(tmp_path, server):
    http = httpx.Client(transport=httpx.MockTransport(server), follow_redirects=True)
    return OwaClient("u", "p", tmp_path / "c.pem", tmp_path / "k.pem", tmp_path / "owa.json", client=http)


def test_owa_login_list_and_body(tmp_path):
    server = FakeOwaServer()
    c = owa_client(tmp_path, server)
    [h] = c.list_inbox()
    assert (h.item_id, h.sender, h.subject) == ("AAA=", "Prof. Weber", "Übungsblatt 3")
    assert h.received == datetime(2026, 10, 16, 9, 0, tzinfo=TZ)
    assert c.get_body("AAA=") == "Bitte bis Freitag abgeben."
    assert server.logins == 1
    assert server.actions == [("FindItem", "FindItemRequest:#Exchange"), ("GetItem", "GetItemRequest:#Exchange")]
    assert (tmp_path / "owa.json").stat().st_mode & 0o777 == 0o600


def test_owa_rejected_login_is_tried_once(tmp_path):
    server = FakeOwaServer(accept_login=False)
    c = owa_client(tmp_path, server)
    with pytest.raises(OwaAuthError):
        c.list_inbox()
    with pytest.raises(OwaAuthError):
        c.list_inbox()
    assert server.logins == 1


# -- Probelauf ------------------------------------------------------------------------------------------
def test_review_sorts_by_priority_and_writes_nothing(conn):
    from planner.mail_flow import format_review, priority, review

    assert priority("2026-10-18", NOW) == "🔴 hoch"
    assert priority("2026-10-27", NOW) == "🟠 mittel"
    assert priority("2027-01-29", NOW) == "🟢 niedrig"
    assert priority(None, NOW) == "🟠 mittel"
    headers = [hdr(1, subject="Semesterbeitrag"), hdr(2, subject="Folien"), hdr(3, subject="Spieleabend"),
               hdr(4, subject="News")]
    model = Model({
        "Semesterbeitrag": MailVerdict("aufgabe", "Semesterbeitrag zahlen", "bis zum 29.01.2027", 15),
        "Folien": MailVerdict("aufgabe", "Folien schicken", "bis Sonntagabend", 60),
        "Spieleabend": MailVerdict("vorschlag", "Spieleabend", "", None),
        "News": MailVerdict("nichts", "", "", None),
    })
    todo, maybe, nothing, failed = review(Mailbox(), model, headers, NOW)
    assert [t[2] for t in todo] == ["Folien schicken", "Semesterbeitrag zahlen"]
    assert len(maybe) == 1 and (nothing, failed) == (1, 0)
    text = format_review(todo, maybe, nothing, failed, 4)
    assert "🔴 hoch · Folien schicken – bis 18.10. · ~60 Min." in text and "💡 Spieleabend" in text
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_mail_step_survives_broken_certificate(conn, tmp_path):
    def broken():
        raise FileNotFoundError("hka_chain.pem")
    assert mail_step(conn, settings(tmp_path), Channel(), NOW, owa_factory=broken, llm_factory=FakeModel) is None
    assert get_meta(conn, "mail_blocked") is None   # kein Login-Problem → nicht sperren
