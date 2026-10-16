"""Hintergrunddienst: periodischer Sync der Quellen und Spiegelung nach Google.

Ein Durchlauf (`sync_cycle`) ist bewusst robust: Fehler einer Quelle stoppen den Dienst nie,
sie landen im Change-Log. Meldungen an den Nutzer laufen über einen `Notifier`
(später Telegram; bis dahin nur Log-Ausgabe).
"""

from __future__ import annotations

import logging
import signal
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from . import changelog, gtasks, proposals
from .calendar_client import CalendarClient
from .config import Settings
from .connectors.ical_sync import Event, SyncResult, sync_url
from .connectors.ilias import (
    IliasAuthError,
    IliasClient,
    NewsSyncResult,
    sync_news,
)
from .db import get_meta, set_meta
from .google_auth import ReauthNeeded, get_credentials
from .gtasks import TasksClient
from .ilias_flow import process_news
from .mail_flow import process_mails
from .mirror import mirror_source
from .planning import describe_plan, run_plan
from .rules import Rules, load_rules

log = logging.getLogger("planner")
TZ = ZoneInfo("Europe/Berlin")
MIN_INTERVAL_MIN = 30
TOKEN_WARN_AFTER = timedelta(days=6)  # Testmodus: Refresh-Token läuft nach 7 Tagen ab
MAX_LINES = 8


class Notifier(Protocol):
    """Meldungskanal. Optional: `send_proposal(id, text) -> bool` für Kanäle mit Buttons (Telegram)."""

    def send(self, text: str) -> object: ...


class LogNotifier:
    def send(self, text: str) -> bool:
        log.info("MELDUNG: %s", text.replace("\n", " | "))
        return True

    def send_with_buttons(self, text: str, buttons) -> bool:
        return self.send(text)


def _fmt(ev: Event) -> str:
    if ev.all_day:
        return ev.start
    return datetime.fromisoformat(ev.start).astimezone(TZ).strftime("%a %d.%m. %H:%M")


def describe_changes(res: SyncResult) -> str | None:
    """Kurze, deutsche Zusammenfassung der Stundenplan-Änderungen (None = nichts zu melden)."""
    lines: list[str] = []
    for old, new in res.changed:
        what = f"{_fmt(old)} → {_fmt(new)}" if old.start != new.start else f"{_fmt(new)}"
        extra = f" (Raum {old.location or '–'} → {new.location or '–'})" if old.location != new.location else ""
        lines.append(f"🔁 {new.title}: {what}{extra}")
    lines += [f"❌ Entfällt: {e.title} ({_fmt(e)})" for e in res.removed]
    lines += [f"➕ Neu: {e.title} ({_fmt(e)})" for e in res.added]
    if not lines:
        return None
    more = len(lines) - MAX_LINES
    out = lines[:MAX_LINES] + ([f"… und {more} weitere"] if more > 0 else [])
    return "📅 Stundenplan geändert:\n" + "\n".join(out)


def _check_token_age(conn: sqlite3.Connection, notifier: Notifier, now: datetime) -> None:
    login_at = get_meta(conn, "google_login_at")
    if not login_at or get_meta(conn, "expiry_warned") == "1":
        return
    if now - datetime.fromisoformat(login_at) >= TOKEN_WARN_AFTER:
        notifier.send("🔑 Der Google-Login läuft bald ab. Bitte `planner login` ausführen.")
        set_meta(conn, "expiry_warned", "1")


def _guarded(conn: sqlite3.Connection, entity: str, fn):
    """Führt einen Teilschritt aus; Fehler landen im Change-Log, der Rest läuft weiter."""
    try:
        return fn()
    except ReauthNeeded:
        raise
    except Exception as exc:
        log.exception("%s fehlgeschlagen", entity)
        changelog.log(conn, actor="system", action="sync.error", entity=entity,
                      detail={"error": f"{type(exc).__name__}: {exc}"[:300]})
        return None


def planning_step(
    conn: sqlite3.Connection, cal, tc, rules: Rules, notifier: Notifier, now: datetime
) -> None:
    """Ein Planungsdurchlauf in dieser Reihenfolge (jeder Schritt einzeln abgesichert):

    1. Änderungen des Nutzers in Google Tasks lesen (abgehakt / gelöscht – der Mensch gewinnt)
    2. bestätigte Termine in den Kalender eintragen
    3. planen: Blocker setzen, wo Zeit ist (verstrichene Aufgaben gelten als erledigt)
    4. Google Tasks nachziehen: Aufgaben OHNE Zeitfenster stehen dort (Auffangnetz), mit Blocker nicht
    5. neue Vorschläge melden (nichts davon kommt ohne Bestätigung in den Kalender)
    """
    if tc is not None:
        _guarded(conn, "gtasks.pull", lambda: gtasks.pull_changes(conn, tc))
    _guarded(conn, "proposals", lambda: proposals.materialize_events(conn, cal))
    report = _guarded(conn, "planner", lambda: run_plan(conn, cal, rules, now=now))
    if report is not None:
        msg = describe_plan(report)
        if msg:
            notifier.send(msg)
    if tc is not None:
        _guarded(conn, "gtasks.push", lambda: gtasks.push(
            conn, tc, now=now, unplaced=report.unplaced if report else None))
    _notify_proposals(conn, notifier)


def _notify_proposals(conn: sqlite3.Connection, notifier: Notifier) -> None:
    """Neue Vorschläge melden – mit Buttons, wenn der Kanal es kann. Nur als gemeldet markieren, wenn es klappte."""
    items = proposals.unnotified_items(conn)
    if not items:
        return
    if hasattr(notifier, "send_proposal"):
        sent = [pid for pid, text in items if notifier.send_proposal(pid, text)]
    else:
        text, ids = proposals.take_unnotified(conn)
        notifier.send(text)
        sent = ids
    proposals.mark_notified(conn, sent)


ILIAS_MIN_INTERVAL = timedelta(minutes=30)


def ilias_step(
    conn: sqlite3.Connection, settings: Settings, notifier: Notifier, now: datetime, *, ilias_factory=None,
    force: bool = False,
) -> NewsSyncResult | None:
    """ILIAS-Neuigkeiten abrufen; Aufgaben mit erkennbarer Frist werden automatisch eingetragen (`ilias_flow`).

    Höchstens ein Login-Versuch pro Lauf; nach einem Fehlschlag kein automatischer Retry
    (Kontosperre!), bis der Nutzer `planner ilias-login` ausführt.
    """
    if ilias_factory is None and not (settings.hka_user and settings.hka_password):
        return None
    if get_meta(conn, "ilias_blocked") == "1":
        return None
    last = get_meta(conn, "ilias_last_sync")
    if not force and last and now - datetime.fromisoformat(last) < ILIAS_MIN_INTERVAL:
        return None
    client = ilias_factory() if ilias_factory else IliasClient(
        settings.hka_user, settings.hka_password, settings.ilias_session_path)
    try:
        result = sync_news(conn, client.fetch_news_html(), now=now)
        set_meta(conn, "ilias_last_sync", now.isoformat())
        process_news(conn, client, notifier, now)
        return result
    except IliasAuthError as exc:
        set_meta(conn, "ilias_blocked", "1")
        changelog.log(conn, actor="system", action="auth.expired", entity="ilias", detail={"reason": str(exc)[:200]})
        notifier.send("🔑 ILIAS-Login fehlgeschlagen – ich versuche es nicht automatisch erneut (Kontosperre). "
                      "Bitte Zugangsdaten prüfen und `planner ilias-login` ausführen.")
    except Exception as exc:
        log.exception("ILIAS-Sync fehlgeschlagen")
        changelog.log(conn, actor="system", action="sync.error", entity="source", entity_id="ilias",
                      detail={"error": f"{type(exc).__name__}: {exc}"[:300]})
    finally:
        client.close()
    return None


MAIL_MIN_INTERVAL = timedelta(minutes=60)


def mail_step(
    conn: sqlite3.Connection, settings: Settings, notifier: Notifier, now: datetime, *,
    owa_factory=None, llm_factory=None, force: bool = False,
) -> int | None:
    """HKA-Mails abrufen und vom lokalen Modell lesen lassen (stündlich; nur mit MAIL_ENABLED=1).

    Login-Schutz wie bei ILIAS: höchstens ein Versuch, nach Fehlschlag gesperrt bis `planner mail-login`.
    Rückgabe: Anzahl verarbeiteter Mails (None = nicht gelaufen).
    """
    if owa_factory is None and not (settings.mail_enabled and settings.hka_user and settings.hka_password):
        return None
    if get_meta(conn, "mail_blocked") == "1":
        return None
    last = get_meta(conn, "mail_last_sync")
    if not force and last and now - datetime.fromisoformat(last) < MAIL_MIN_INTERVAL:
        return None
    from .connectors.mailbox import sync_inbox
    from .connectors.owa import OwaAuthError, OwaClient
    from .llm import OllamaClient

    try:   # Zertifikat fehlt/defekt o. Ä.: darf den Dienst nicht stoppen
        client = owa_factory() if owa_factory else OwaClient(
            settings.hka_user, settings.hka_password, settings.owa_cert_path, settings.owa_key_path,
            settings.owa_session_path)
    except Exception as exc:
        log.exception("Mail-Client nicht startbar")
        changelog.log(conn, actor="system", action="sync.error", entity="source", entity_id="mail",
                      detail={"error": f"{type(exc).__name__}: {exc}"[:300]})
        return None
    llm = llm_factory() if llm_factory else OllamaClient(settings.ollama_url, settings.ollama_model)
    try:
        sync_inbox(conn, client.list_inbox(), now=now)
        set_meta(conn, "mail_last_sync", now.isoformat())
        return process_mails(conn, client, llm, notifier, now, ask_feedback=settings.mail_feedback)
    except OwaAuthError as exc:
        set_meta(conn, "mail_blocked", "1")
        changelog.log(conn, actor="system", action="auth.expired", entity="mail", detail={"reason": str(exc)[:200]})
        notifier.send("🔑 HKA-Mail-Login fehlgeschlagen – ich versuche es nicht automatisch erneut (Kontosperre). "
                      "Bitte Zugangsdaten/Zertifikat prüfen und `planner mail-login` ausführen.")
    except Exception as exc:
        log.exception("Mail-Abruf fehlgeschlagen")
        changelog.log(conn, actor="system", action="sync.error", entity="source", entity_id="mail",
                      detail={"error": f"{type(exc).__name__}: {exc}"[:300]})
    finally:
        client.close()
        llm.close()
    return None


def gmail_step(
    conn: sqlite3.Connection, settings: Settings, notifier: Notifier, now: datetime, *,
    gmail_factory=None, llm_factory=None, force: bool = False,
) -> int | None:
    """Gmail wie HKA-Mail: stündlich, nur mit GMAIL_ENABLED=1. Fehlt das Gmail-Recht im Google-Login,
    wird einmal gemeldet (Kalender und Tasks laufen davon unberührt weiter)."""
    if gmail_factory is None and not settings.gmail_enabled:
        return None
    last = get_meta(conn, "gmail_last_sync")
    if not force and last and now - datetime.fromisoformat(last) < MAIL_MIN_INTERVAL:
        return None
    from .connectors.gmail import GmailClient
    from .connectors.mailbox import sync_inbox
    from .google_auth import has_gmail
    from .llm import OllamaClient

    try:
        if gmail_factory is not None:
            client = gmail_factory()
        else:
            creds = get_credentials(settings)
            if not has_gmail(creds):
                if get_meta(conn, "gmail_scope_notified") != "1":
                    notifier.send("✉️ Für Gmail fehlt noch das Leserecht im Google-Login. Bitte einmal "
                                  "`planner login` ausführen und den Token aufspielen.")
                    set_meta(conn, "gmail_scope_notified", "1")
                return None
            client = GmailClient(creds, query=settings.gmail_query)
    except ReauthNeeded:
        return None   # meldet schon der Kalender-Schritt
    set_meta(conn, "gmail_scope_notified", "0")
    llm = llm_factory() if llm_factory else OllamaClient(settings.ollama_url, settings.ollama_model)
    try:
        sync_inbox(conn, client.list_inbox(), now=now, source="gmail")
        set_meta(conn, "gmail_last_sync", now.isoformat())
        return process_mails(conn, client, llm, notifier, now, ask_feedback=settings.mail_feedback, source="gmail")
    except Exception as exc:
        log.exception("Gmail-Abruf fehlgeschlagen")
        changelog.log(conn, actor="system", action="sync.error", entity="source", entity_id="gmail",
                      detail={"error": f"{type(exc).__name__}: {exc}"[:300]})
    finally:
        client.close()
        llm.close()
    return None


def _plan_step(conn: sqlite3.Connection, cal, tc, settings: Settings, notifier: Notifier, now: datetime) -> None:
    """Nur laufen lassen, wenn es etwas zu tun gibt (Aufgaben oder Vorschläge vorhanden)."""
    has_work = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM tasks) OR EXISTS(SELECT 1 FROM proposals)"
    ).fetchone()[0]
    if has_work:
        planning_step(conn, cal, tc, load_rules(settings.rules_path), notifier, now)


def sync_cycle(
    conn: sqlite3.Connection,
    settings: Settings,
    notifier: Notifier,
    *,
    now: datetime | None = None,
    fetcher=None,
    calendar_factory=None,
    tasks_factory=None,
    ilias_factory=None,
) -> None:
    now = now or datetime.now(UTC)
    _check_token_age(conn, notifier, now)

    # 1) Stundenplan abrufen
    if settings.raumzeit_ics_url:
        try:
            res = sync_url(conn, "raumzeit", settings.raumzeit_ics_url, fetcher=fetcher)
            msg = describe_changes(res)
            if msg:
                notifier.send(msg)
        except Exception as exc:  # Netzwerk, Parsing, … – Dienst läuft weiter
            log.exception("Raumzeit-Sync fehlgeschlagen")
            changelog.log(conn, actor="system", action="sync.error", entity="source",
                          entity_id="raumzeit", detail={"error": f"{type(exc).__name__}: {exc}"[:300]})

    # 1b) ILIAS-Neuigkeiten (erzeugt höchstens Vorschläge)
    ilias_step(conn, settings, notifier, now, ilias_factory=ilias_factory)

    # 1c) HKA-Mail (lokales Modell; Aufgaben automatisch, Vorschläge nur nach Bestätigung)
    mail_step(conn, settings, notifier, now)
    gmail_step(conn, settings, notifier, now)

    # 2) Nach Google spiegeln (nur Differenz)
    try:
        if calendar_factory is not None:
            cal = calendar_factory()
            tc = tasks_factory() if tasks_factory is not None else None
        else:
            creds = get_credentials(settings)
            cal, tc = CalendarClient(creds, conn), TasksClient(creds, conn)
        cal_id = cal.ensure_calendar("Stundenplan", "calendar_id:stundenplan", "Aus raumzeit gespiegelt")
        mirror_source(conn, cal, source="raumzeit", calendar_id=cal_id, now=now)
        set_meta(conn, "reauth_notified", "0")
        _plan_step(conn, cal, tc, settings, notifier, now)
    except ReauthNeeded as exc:
        if get_meta(conn, "reauth_notified") != "1":  # nur einmal melden, nicht alle 30 Min.
            notifier.send("🔑 Google-Login abgelaufen. Bitte `planner login` ausführen.")
            set_meta(conn, "reauth_notified", "1")
            changelog.log(conn, actor="system", action="auth.expired", entity="google",
                          detail={"reason": str(exc)[:200]})
    except Exception as exc:
        log.exception("Spiegelung fehlgeschlagen")
        changelog.log(conn, actor="system", action="sync.error", entity="mirror",
                      detail={"error": f"{type(exc).__name__}: {exc}"[:300]})


def run_forever(
    conn: sqlite3.Connection,
    settings: Settings,
    notifier: Notifier,
    *,
    interval_min: int = 30,
    bot_starter=None,
) -> None:
    """Dauerlauf. `bot_starter(wake, stop)` startet optional den Telegram-Empfang; setzt der Bot
    `wake` (neue Aufgabe, Bestätigung …), läuft der Planungsdurchlauf sofort statt erst im Takt."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    interval = max(interval_min, MIN_INTERVAL_MIN) * 60
    stop, wake = threading.Event(), threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    if bot_starter is not None:
        bot_starter(wake, stop)
    log.info("Dienst gestartet (Takt: %d Min.)", interval // 60)
    while not stop.is_set():
        wake.clear()
        sync_cycle(conn, settings, notifier)
        for _ in range(interval // 2):   # kurzer Takt, damit Bot-Eingaben sofort wirken
            if stop.wait(2) or wake.is_set():
                break
    log.info("Dienst beendet")
