from datetime import UTC, datetime, timedelta

import httpx
import pytest

from planner import proposals
from planner.bot_logic import handle_callback, handle_message, ilias_overview
from planner.config import Settings
from planner.connectors.ilias import (
    BASE,
    TZ,
    IliasAuthError,
    IliasClient,
    looks_like_task,
    parse_news,
    sync_news,
)
from planner.db import connect, get_meta
from planner.service import ilias_step

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=TZ)


def panel(news_id, stamp, title, *, uid="abcd0001", name="Muster, Erika", headline="Die Datei wurde hinzugefügt.",
          typ="file", ref="100", body=""):
    """Eintrag im Aufbau der echten Neuigkeiten-Seite (synthetisch, keine echten Daten)."""
    return f"""<li><div class="ilTimelinePanel"><div>
<div class="ilNewsTimelineContentSection"><div class="light small ilNewsTimelineEditInfo">
 {name} [{uid}] - {stamp}<br />Bearbeitet: {stamp} </div>
<h4 class="ilNewsTimelineTruncatedText">{headline}</h4>
<p class="ilNewsTimelineObjHead ilNewsTimelineTruncatedText"><a href="https://ilias.h-ka.de/goto.php/{typ}/{ref}"><img src="x" /> {title}</a></p>
<div class="js-dynamic-height dynamic-max-height"><div class="dynamic-height-wrap"><p>{body}</p></div>
<button class="js-dynamic-show-hide">Show more</button></div></div></div>
<a href='ilias.php?cmd=updateWidget&news_id={news_id}&hash=1'>x</a></div></li>"""


def page(*panels):
    return '<form class="il-standard-form" data-cmd-apply="ilias.php?cmd=view&amp;cmdFilter=apply"></form>' \
           '<ul class="ilTimeline">' + "".join(panels) + "</ul>"


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.db")


# -- Parser ------------------------------------------------------------------------------------
def test_parse_news_fields():
    items = parse_news(page(
        panel(1, "8. Okt 2026, 13:56", "Plan.pdf", uid="mmus0001", name="Mustermann, Max"),
        panel(2, "1. Mär 2026, 09:05", "Frage", typ="frm", ref="55", headline="Neue Aufgabe", body="Abgabe bis Freitag"),
    ))
    a, b = sorted(items, key=lambda i: i.news_id)
    assert (a.author, a.uid, a.title, a.obj_type, a.ref_id) == ("Mustermann, Max", "mmus0001", "Plan.pdf", "file", "100")
    assert a.ts == datetime(2026, 10, 8, 13, 56, tzinfo=TZ) and a.url == BASE + "goto.php/file/100"
    assert b.ts == datetime(2026, 3, 1, 9, 5, tzinfo=TZ) and b.body == "Abgabe bis Freitag" and b.headline == "Neue Aufgabe"


def test_parse_skips_unknown_and_dedupes():
    broken = '<div class="ilTimelinePanel"><div>kein Zeitstempel news_id=9</div></div>'
    items = parse_news(page(panel(1, "8. Okt 2026, 13:56", "A.pdf"), panel(1, "8. Okt 2026, 13:56", "A.pdf"), broken))
    assert [i.news_id for i in items] == ["1"]


def test_author_falls_back_to_uid():
    [item] = parse_news(page(panel(1, "8. Okt 2026, 13:56", "A.pdf", uid="abcd0002", name="")))
    assert item.author == "abcd0002"


@pytest.mark.parametrize("title,body,expected", [
    ("Projektaufgabe Teil 2.pdf", "", True),
    ("Zeitplan_Abgabe.xlsx", "", True),
    ("Abgabe Übungsblatt 3", "", True),
    ("Vorlesung 2 Folien.pdf", "", False),
    ("Introduction Slides.pdf", "", False),
    ("Klausuranmeldung WS 26.pdf", "", False),
    ("Prüfungsinfos.pdf", "", False),
    ("Praktikumsbörse.pdf", "", False),
    ("Exercise 3 - sorting", "", True),
    ("Worksheet Statistik.pdf", "", True),
    ("Frage", "Bitte bis zum 20.10. einreichen", True),
])
def test_looks_like_task(title, body, expected):
    [item] = parse_news(page(panel(1, "8. Okt 2026, 13:56", title, body=body)))
    assert looks_like_task(item) is expected


# -- Neue Einträge erkennen ------------------------------------------------------------------------
def test_first_sync_is_silent_baseline(conn):
    res = sync_news(conn, page(panel(1, "8. Okt 2026, 13:56", "Projektaufgabe.pdf")), now=NOW)
    assert res.initial and res.stored == 1 and res.new == []
    assert conn.execute("SELECT state FROM ilias_news").fetchone()[0] == "seen"


def test_second_sync_reports_only_new(conn):
    sync_news(conn, page(panel(1, "8. Okt 2026, 13:56", "Alt.pdf")), now=NOW)
    html = page(panel(1, "8. Okt 2026, 13:56", "Alt.pdf"), panel(2, "13. Okt 2026, 10:00", "Abgabe Blatt 1.pdf"))
    res = sync_news(conn, html, now=NOW)
    assert not res.initial and [i.news_id for i in res.new] == ["2"] and len(res.task_like) == 1
    assert sync_news(conn, html, now=NOW).new == []   # nicht doppelt melden


def test_very_old_entries_are_never_new(conn):
    sync_news(conn, page(panel(1, "8. Okt 2026, 13:56", "A.pdf")), now=NOW)
    res = sync_news(conn, page(panel(2, "19. Okt 2016, 10:14", "Plan2016.pdf")), now=NOW)
    assert res.stored == 1 and res.new == []


def test_unrecognised_page_raises(conn):
    with pytest.raises(ValueError):
        sync_news(conn, "<html>ganz anderes Layout</html>", now=NOW)


# -- Dienst-Schritt ----------------------------------------------------------------------------------
class Collect:
    def __init__(self):
        self.msgs = []

    def send(self, text):
        self.msgs.append(text)
        return True


class FakeIlias:
    def __init__(self, html=None, error=None, files=None, course="Test Kurs"):
        self.html, self.error, self.calls, self.closed = html, error, 0, False
        self.files, self.course = files or {}, course

    def fetch_news_html(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.html

    def download_file(self, ref_id, **kw):
        return self.files.get(ref_id, (b"Hier steht keine Frist.", "text/plain", "x.txt"))

    def course_of(self, obj_type, ref_id):
        return self.course

    def close(self):
        self.closed = True


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, google_client_id="x", google_client_secret="y")


NOW_UTC = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


class ButtonChannel(Collect):
    def __init__(self, ok=True):
        super().__init__()
        self.ok, self.buttons = ok, []

    def send_with_buttons(self, text, buttons):
        self.msgs.append(text)
        self.buttons.append(buttons)
        return self.ok


def _first_then(conn, settings, channel, html):
    ilias_step(conn, settings, Collect(), NOW_UTC, ilias_factory=lambda: FakeIlias(page(panel(1, "8. Okt 2026, 13:56", "A.pdf"))))
    return ilias_step(conn, settings, channel, NOW_UTC + timedelta(hours=1), ilias_factory=lambda: FakeIlias(html))


def test_assignment_is_announced_but_nothing_is_created(conn, settings):
    ch = ButtonChannel()
    html = page(panel(1, "8. Okt 2026, 13:56", "A.pdf"), panel(2, "13. Okt 2026, 10:00", "Projektaufgabe.pdf"),
                panel(3, "13. Okt 2026, 10:05", "Vorlesung 3 Folien.pdf"))
    _first_then(conn, settings, ch, html)
    assert len(ch.msgs) == 1 and "Projektaufgabe.pdf" in ch.msgs[0] and "goto.php/file/100" in ch.msgs[0]
    assert ch.buttons[0] == [[("➕ Als Aufgabe anlegen", "ilt:2")]]
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0           # nur Information
    assert proposals.pending(conn) == []                                          # keine Warteliste
    assert conn.execute("SELECT COUNT(*) FROM ilias_news").fetchone()[0] == 3     # alles gespeichert
    assert conn.execute("SELECT COUNT(*) FROM ilias_news WHERE state='new'").fetchone()[0] == 0


def test_announced_only_once(conn, settings):
    ch = ButtonChannel()
    html = page(panel(1, "8. Okt 2026, 13:56", "A.pdf"), panel(2, "13. Okt 2026, 10:00", "Projektaufgabe.pdf"))
    _first_then(conn, settings, ch, html)
    ilias_step(conn, settings, ch, NOW_UTC + timedelta(hours=3), ilias_factory=lambda: FakeIlias(html))
    assert len(ch.msgs) == 1


def test_undelivered_announcement_is_retried(conn, settings):
    down = ButtonChannel(ok=False)
    html = page(panel(1, "8. Okt 2026, 13:56", "A.pdf"), panel(2, "13. Okt 2026, 10:00", "Projektaufgabe.pdf"))
    _first_then(conn, settings, down, html)
    up = ButtonChannel(ok=True)
    ilias_step(conn, settings, up, NOW_UTC + timedelta(hours=3), ilias_factory=lambda: FakeIlias(html))
    ilias_step(conn, settings, up, NOW_UTC + timedelta(hours=6), ilias_factory=lambda: FakeIlias(html))
    assert len(down.msgs) == 1 and len(up.msgs) == 1   # beim zweiten Versuch zugestellt, danach Ruhe


def test_works_with_plain_notifier_without_buttons(conn, settings):
    plain = Collect()
    html = page(panel(1, "8. Okt 2026, 13:56", "A.pdf"), panel(2, "13. Okt 2026, 10:00", "Hausaufgabe 4.pdf"))
    _first_then(conn, settings, plain, html)
    assert len(plain.msgs) == 1 and "Hausaufgabe 4.pdf" in plain.msgs[0]


def test_news_are_processed_in_batches(conn, settings):
    ch = ButtonChannel()
    html = page(panel(1, "8. Okt 2026, 13:56", "A.pdf"),
                *[panel(10 + i, "13. Okt 2026, 10:00", f"Aufgabe {i}.pdf", ref=str(200 + i)) for i in range(12)])
    _first_then(conn, settings, ch, html)
    assert len(ch.buttons) == 10                                   # pro Lauf höchstens 10 Einträge
    assert conn.execute("SELECT COUNT(*) FROM ilias_news WHERE state='new'").fetchone()[0] == 2
    ilias_step(conn, settings, ch, NOW_UTC + timedelta(hours=3), ilias_factory=lambda: FakeIlias(html))
    assert len(ch.buttons) == 12 and conn.execute("SELECT COUNT(*) FROM ilias_news WHERE state='new'").fetchone()[0] == 0


def test_irrelevant_news_is_silent(conn, settings):
    ch = ButtonChannel()
    html = page(panel(1, "8. Okt 2026, 13:56", "A.pdf"), panel(2, "13. Okt 2026, 10:00", "Klausuranmeldung.pdf"),
                panel(3, "13. Okt 2026, 10:05", "Vorlesung 3 Folien.pdf"))
    _first_then(conn, settings, ch, html)
    assert ch.msgs == []


def test_button_creates_task_only_on_click_and_only_once(conn, settings):
    ch = ButtonChannel()
    html = page(panel(1, "8. Okt 2026, 13:56", "A.pdf"), panel(2, "13. Okt 2026, 10:00", "Projektaufgabe.pdf"))
    _first_then(conn, settings, ch, html)
    r = handle_callback(conn, "ilt:2")
    row = conn.execute("SELECT * FROM tasks").fetchone()
    assert r.changed and row["title"] == "ILIAS: Projektaufgabe.pdf" and row["source"] == "ilias" and row["deadline"] is None
    assert "/frist 1" in r.text
    assert "schon eine Aufgabe" in handle_callback(conn, "ilt:2").text
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert "gibt es nicht mehr" in handle_callback(conn, "ilt:999").text


def test_frist_and_aufwand_commands(conn):
    handle_message(conn, "Bericht", message_id=1, now=NOW)
    assert "20.10." in handle_message(conn, "/frist 1 20.10.", now=NOW).text
    assert conn.execute("SELECT deadline FROM tasks").fetchone()[0].endswith("-10-20")
    assert "2 h" in handle_message(conn, "/aufwand 1 2h").text
    assert conn.execute("SELECT estimate_min FROM tasks").fetchone()[0] == 120
    assert "entfernt" in handle_message(conn, "/frist 1 keine").text
    assert conn.execute("SELECT deadline FROM tasks").fetchone()[0] is None
    assert "So geht" in handle_message(conn, "/frist").text
    assert "nicht lesbar" in handle_message(conn, "/frist 1 irgendwann").text
    assert "gibt es nicht" in handle_message(conn, "/aufwand 99 1h").text
    log = conn.execute("SELECT actor, detail FROM changelog WHERE action='task.updated'").fetchall()
    assert len(log) == 3 and all(r["actor"] == "user" for r in log)


def test_ilias_polled_at_most_every_30_minutes(conn, settings):
    f = FakeIlias(page(panel(1, "8. Okt 2026, 13:56", "A.pdf")))
    ilias_step(conn, settings, Collect(), NOW_UTC, ilias_factory=lambda: f)
    ilias_step(conn, settings, Collect(), NOW_UTC + timedelta(minutes=10), ilias_factory=lambda: f)
    assert f.calls == 1
    ilias_step(conn, settings, Collect(), NOW_UTC + timedelta(minutes=31), ilias_factory=lambda: f)
    assert f.calls == 2 and f.closed


def test_auth_failure_blocks_further_attempts(conn, settings):
    n = Collect()
    f = FakeIlias(error=IliasAuthError("abgelehnt"))
    for i in range(3):
        ilias_step(conn, settings, n, NOW_UTC + timedelta(hours=i), ilias_factory=lambda: f)
    assert f.calls == 1                       # kein automatischer Retry → keine Kontosperre
    assert get_meta(conn, "ilias_blocked") == "1"
    assert sum("ILIAS-Login fehlgeschlagen" in m for m in n.msgs) == 1


def test_network_error_does_not_block_but_is_logged(conn, settings):
    f = FakeIlias(error=OSError("kein Netz"))
    ilias_step(conn, settings, Collect(), NOW_UTC, ilias_factory=lambda: f)
    assert get_meta(conn, "ilias_blocked") != "1"
    assert conn.execute("SELECT 1 FROM changelog WHERE action='sync.error' AND entity_id='ilias'").fetchone()


def test_no_credentials_means_no_call(conn, settings):
    assert ilias_step(conn, settings, Collect(), NOW_UTC) is None


# -- Client mit Mock-Server -----------------------------------------------------------------------------
LOGIN_PAGE = ('<form role="form" name="login_form" action="ilias.php?baseClass=ilstartupgui&amp;cmd=post">'
              '<input type="text" name="login_form/input_3/input_4" />'
              '<input type="password" name="login_form/input_3/input_5" />'
              '<input type="hidden" name="login_form/csrf" value="tok" /></form>')


def mock_ilias(password="pw"):
    log = {"posts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        logged_in = request.headers.get("cookie", "").find("sess=ok") >= 0
        if request.method == "POST":
            log["posts"] += 1
            body = request.content.decode()
            if f"input_5={password}" in body and "csrf=tok" in body.replace("%2F", "/"):
                return httpx.Response(200, text="<html>Dashboard</html>", headers={"set-cookie": "sess=ok; Path=/"})
            return httpx.Response(200, text=LOGIN_PAGE)
        if not logged_in:
            return httpx.Response(200, text=LOGIN_PAGE)
        if "cmdFilter=apply" in str(request.url):
            return httpx.Response(200, text=page(panel(1, "13. Okt 2026, 10:00", "Gefiltert.pdf")))
        return httpx.Response(200, text=page(panel(9, "19. Okt 2016, 10:14", "Alt.pdf")))

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True), log


def test_client_logs_in_once_and_applies_filter(tmp_path):
    http, log = mock_ilias()
    c = IliasClient("user", "pw", tmp_path / "s.json", client=http)
    html = c.fetch_news_html()
    assert "Gefiltert.pdf" in html and "Alt.pdf" not in html   # Zeitfilter wird explizit gesetzt
    assert log["posts"] == 1 and (tmp_path / "s.json").exists()
    assert oct((tmp_path / "s.json").stat().st_mode)[-3:] == "600"
    c.fetch_news_html()
    assert log["posts"] == 1                                      # Sitzung wiederverwendet


def test_client_reuses_saved_session(tmp_path):
    http, _ = mock_ilias()
    IliasClient("user", "pw", tmp_path / "s.json", client=http).fetch_news_html()
    http2, log2 = mock_ilias()
    IliasClient("user", "pw", tmp_path / "s.json", client=http2).fetch_news_html()
    assert log2["posts"] == 0


def test_wrong_password_raises_and_never_retries(tmp_path):
    http, log = mock_ilias(password="richtig")
    c = IliasClient("user", "falsch", tmp_path / "s.json", client=http)
    with pytest.raises(IliasAuthError):
        c.fetch_news_html()
    with pytest.raises(IliasAuthError):
        c.fetch_news_html()
    assert log["posts"] == 1                                      # genau ein Versuch
    assert not (tmp_path / "s.json").exists()


def test_missing_credentials_raise_without_request(tmp_path):
    http, log = mock_ilias()
    with pytest.raises(IliasAuthError):
        IliasClient("", "", tmp_path / "s.json", client=http).fetch_news_html()
    assert log["posts"] == 0


# -- Bot ---------------------------------------------------------------------------------------------
def test_bot_ilias_command(conn):
    assert "Keine ILIAS-Neuigkeiten" in handle_message(conn, "/ilias").text
    recent = datetime.now(TZ).strftime("%-d. %b %Y, 10:00").replace("Oct", "Okt").replace("Mar", "Mär")
    sync_news(conn, page(panel(1, recent, "Plan.pdf")), now=datetime.now(TZ))
    text = handle_message(conn, "/ilias").text
    assert "Plan.pdf" in text and "Muster" in text
    assert "Plan.pdf" in ilias_overview(conn, days=3)


# -- Datei-Download ----------------------------------------------------------------------------------
def download_client(tmp_path, handler):
    return IliasClient("user", "pw", tmp_path / "s.json", client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True))


def test_download_returns_content_and_decodes_filename(tmp_path):
    def handler(request):
        return httpx.Response(200, content=b"%PDF-1.4 hallo", headers={
            "content-type": "application/pdf", "content-disposition": "attachment; filename*=UTF-8''%C3%9Cbung%203.pdf"})

    data, ctype, name = download_client(tmp_path, handler).download_file("5")
    assert data == b"%PDF-1.4 hallo" and ctype == "application/pdf" and name == "Übung 3.pdf"


def test_accept_filter_gets_filename_first_as_documents_is_supported_expects(tmp_path):
    """Regression: Die Argumente waren vertauscht → im Echtbetrieb wurde jede Datei übersprungen."""
    from planner.connectors.ilias import SkippedFile
    from planner.documents import is_supported

    def pdf(request):
        return httpx.Response(200, content=b"x", headers={"content-type": "application/pdf",
                                                           "content-disposition": 'attachment; filename="a.pdf"'})

    def video(request):
        return httpx.Response(200, content=b"x", headers={"content-type": "video/mp4",
                                                           "content-disposition": 'attachment; filename="film.mp4"'})

    assert download_client(tmp_path, pdf).download_file("1", accept=is_supported)[0] == b"x"
    with pytest.raises(SkippedFile):
        download_client(tmp_path, video).download_file("1", accept=is_supported)


def test_download_rejects_oversized(tmp_path):
    def handler(request):
        return httpx.Response(200, content=b"x" * 50, headers={"content-type": "application/pdf"})

    with pytest.raises(ValueError, match="zu groß"):
        download_client(tmp_path, handler).download_file("1", max_bytes=10)


def test_html_page_instead_of_file_skips_only_that_file_without_auth_error(tmp_path):
    """Keine Berechtigung/kein Dateiobjekt darf nie als Login-Fehler gelten (sperrt sonst die ganze Anbindung)."""
    from planner.connectors.ilias import SkippedFile

    def handler(request):
        return httpx.Response(200, text="<html>Sie haben keine Berechtigung</html>", headers={"content-type": "text/html"})

    with pytest.raises(SkippedFile):
        download_client(tmp_path, handler).download_file("1")


def test_download_logs_in_once_when_session_expired(tmp_path):
    state = {"logged_in": False, "posts": 0}

    def handler(request):
        if request.method == "POST":
            state["posts"] += 1
            state["logged_in"] = True
            return httpx.Response(200, text="<html>Dashboard</html>")
        if "cmd=sendfile" in str(request.url):
            if not state["logged_in"]:
                return httpx.Response(200, text=LOGIN_PAGE, headers={"content-type": "text/html"})
            return httpx.Response(200, content=b"daten", headers={"content-type": "application/pdf"})
        return httpx.Response(200, text=LOGIN_PAGE)

    data, _, _ = download_client(tmp_path, handler).download_file("1")
    assert data == b"daten" and state["posts"] == 1


def test_download_never_loops_on_login(tmp_path):
    def handler(request):
        return httpx.Response(200, text=LOGIN_PAGE, headers={"content-type": "text/html"})

    with pytest.raises(IliasAuthError):
        download_client(tmp_path, handler).download_file("1")
