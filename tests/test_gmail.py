import base64
import json
import sqlite3
from datetime import datetime

import pytest

from planner.config import Settings
from planner.connectors.gmail import GmailClient, body_text, html_to_text, parse_header, sender_name
from planner.connectors.mailbox import MailHeader, sync_inbox
from planner.db import connect, get_meta
from planner.google_auth import GMAIL_SCOPE, SCOPES, ReauthNeeded, get_credentials, has_gmail
from planner.llm import MailVerdict
from planner.rules import TZ
from planner.service import gmail_step
from test_ilias_flow import Channel
from test_mail import Mailbox, Model

NOW = datetime(2026, 10, 16, 9, 0, tzinfo=TZ)


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.db")


# -- Nachricht lesen ------------------------------------------------------------------------------------
def test_body_prefers_plain_and_skips_attachments():
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/plain", "body": {"data": b64("Bitte bis Freitag zurückmelden.")}},
            {"mimeType": "text/html", "body": {"data": b64("<p>Bitte</p>")}}]},
        {"mimeType": "text/plain", "filename": "anhang.txt", "body": {"data": b64("GEHEIM")}}]}
    assert body_text(payload) == "Bitte bis Freitag zurückmelden."


def test_body_falls_back_to_html():
    payload = {"mimeType": "text/html", "body": {"data": b64(
        "<html><head><style>p{}</style></head><body><p>Hallo&nbsp;Jonah,</p><p>Abgabe bis 23.10.</p>"
        "<script>x()</script></body></html>")}}
    assert body_text(payload) == "Hallo\xa0Jonah,\nAbgabe bis 23.10."
    assert html_to_text("a<br>b") == "a\nb"


def test_header_and_sender():
    msg = {"id": "18f", "internalDate": "1792134000000", "payload": {"headers": [
        {"name": "From", "value": "Lisa Müller <lisa@example.org>"}, {"name": "Subject", "value": "Folien"}]}}
    h = parse_header(msg)
    assert (h.item_id, h.sender, h.subject) == ("18f", "Lisa Müller", "Folien")
    assert h.received.tzinfo is not None
    assert sender_name("lisa@example.org") == "lisa@example.org"


class FakeService:
    """Minimaler Nachbau von googleapiclient: users().messages().list/get(...).execute()."""

    def __init__(self, messages):
        self.messages, self.queries = messages, []

    def list(self, userId, q, maxResults):
        self.queries.append(q)
        return _Exec({"messages": [{"id": m["id"]} for m in self.messages][:maxResults]})

    def get(self, userId, id, format, metadataHeaders=None):
        return _Exec(next(m for m in self.messages if m["id"] == id))



class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


def fake_msg(i, subject, text):
    return {"id": f"m{i}", "internalDate": "1792134000000", "payload": {
        "mimeType": "text/plain", "body": {"data": b64(text)},
        "headers": [{"name": "From", "value": "Lisa <l@x.de>"}, {"name": "Subject", "value": subject}]}}


def test_gmail_client_lists_primary_inbox_and_reads_body():
    msgs = [fake_msg(1, "Folien", "Kannst du die Folien bis Sonntag schicken?")]
    svc = FakeService(msgs)
    svc_messages = svc

    class Users:
        def messages(self):
            return svc_messages

    class Root:
        def users(self):
            return Users()

    client = GmailClient(None, service=Root())
    [h] = client.list_inbox()
    assert h.subject == "Folien" and svc.queries == ["in:inbox category:primary"]
    assert client.get_body("m1") == "Kannst du die Folien bis Sonntag schicken?"


# -- Postfächer getrennt ---------------------------------------------------------------------------------------
def test_inboxes_are_separate_and_baselined_independently(conn):
    h = MailHeader("x1", NOW, "A", "S")
    sync_inbox(conn, [h], now=NOW, source="hka")
    r = sync_inbox(conn, [MailHeader("g1", NOW, "B", "T")], now=NOW, source="gmail")
    assert r.initial and r.new == 0    # Gmail hat seinen eigenen stillen Erstimport
    r = sync_inbox(conn, [MailHeader("g1", NOW, "B", "T"), MailHeader("g2", NOW, "B", "U")], now=NOW, source="gmail")
    assert r.new == 1
    assert dict(conn.execute("SELECT item_id, source FROM mails").fetchall()) == {"x1": "hka", "g1": "gmail", "g2": "gmail"}


def test_old_database_gets_source_column(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE mails (item_id TEXT PRIMARY KEY, received TEXT NOT NULL, sender TEXT, subject TEXT, "
                "first_seen TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'new', verdict TEXT, attempts INTEGER NOT NULL DEFAULT 0)")
    old.execute("INSERT INTO mails(item_id, received, first_seen) VALUES ('a', 'x', 'y')")
    old.commit()
    old.close()
    conn = connect(path)
    assert conn.execute("SELECT source FROM mails").fetchone()[0] == "hka"


# -- Dienstschritt -------------------------------------------------------------------------------------------
def settings(tmp_path, **kw):
    base = {"data_dir": tmp_path, "google_client_id": "", "google_client_secret": "", "gmail_enabled": True}
    return Settings(**{**base, **kw})


class FakeGmail(Mailbox):
    def __init__(self, headers, bodies=None):
        super().__init__(bodies)
        self.headers = headers

    def list_inbox(self, limit=25):
        return self.headers

    def close(self):
        pass


class FakeModel(Model):
    def close(self):
        pass


def test_gmail_step_baseline_then_task_with_gmail_label(conn, tmp_path):
    first = FakeGmail([MailHeader("g1", NOW, "Lisa", "Alt")])
    assert gmail_step(conn, settings(tmp_path), Channel(), NOW, gmail_factory=lambda: first, llm_factory=FakeModel) == 0
    second = FakeGmail([MailHeader("g1", NOW, "Lisa", "Alt"), MailHeader("g2", NOW, "Lisa", "Folien")])
    model = FakeModel({"Folien": MailVerdict("aufgabe", "Folien schicken", "bis Sonntagabend", 60, "hoch")})
    ch = Channel()
    n = gmail_step(conn, settings(tmp_path), ch, NOW.replace(hour=11), gmail_factory=lambda: second,
                   llm_factory=lambda: model)
    assert n == 1 and ch.sent[0][0].startswith("✉️ Aus Gmail➕")
    assert conn.execute("SELECT title, deadline FROM tasks").fetchone()[:] == ("Folien schicken", "2026-10-18")


def test_gmail_step_off_by_default(conn, tmp_path):
    assert gmail_step(conn, settings(tmp_path, gmail_enabled=False), Channel(), NOW) is None


def test_gmail_step_reports_missing_scope_once(conn, tmp_path):
    token = tmp_path / "google_token.json"
    token.write_text(json.dumps({"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s",
                                 "scopes": SCOPES, "expiry": "2099-01-01T00:00:00Z"}))
    ch = Channel()
    for hour in (9, 11):
        assert gmail_step(conn, settings(tmp_path), ch, NOW.replace(hour=hour)) is None
    assert len(ch.sent) == 1 and "Leserecht" in ch.sent[0][0]
    assert get_meta(conn, "gmail_scope_notified") == "1"


# -- Google-Login ------------------------------------------------------------------------------------------------
def write_token(tmp_path, scopes):
    (tmp_path / "google_token.json").write_text(json.dumps({
        "token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s", "scopes": scopes,
        "expiry": "2099-01-01T00:00:00Z"}))


def test_old_token_still_works_for_calendar_without_gmail(tmp_path):
    write_token(tmp_path, SCOPES)
    creds = get_credentials(settings(tmp_path))
    assert creds.valid and not has_gmail(creds)


def test_new_token_keeps_gmail_scope(tmp_path):
    write_token(tmp_path, [*SCOPES, GMAIL_SCOPE])
    assert has_gmail(get_credentials(settings(tmp_path)))


def test_token_without_calendar_scope_needs_login(tmp_path):
    write_token(tmp_path, [GMAIL_SCOPE])
    with pytest.raises(ReauthNeeded):
        get_credentials(settings(tmp_path))
