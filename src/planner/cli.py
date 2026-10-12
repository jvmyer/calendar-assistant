"""Kommandozeile: `planner login | status | log | calendar-check`."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

from . import changelog
from .calendar_client import CalendarClient
from .config import load_settings
from .db import connect
from .google_auth import ReauthNeeded, get_credentials, login


def cmd_login(_args) -> int:
    settings = load_settings()
    conn = connect(settings.db_path)
    login(settings)
    changelog.log(conn, actor="user", action="auth.login", entity="google")
    print("Login erfolgreich. Token gespeichert (gültig bis Re-Login in ~7 Tagen).")
    return 0


def cmd_status(_args) -> int:
    settings = load_settings()
    conn = connect(settings.db_path)
    print(f"Datenbank: {settings.db_path}")
    n_open = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='open'").fetchone()[0]
    print(f"Offene Aufgaben: {n_open}")
    try:
        get_credentials(settings)
        print("Google: eingeloggt")
    except ReauthNeeded as exc:
        print(f"Google: NICHT eingeloggt – bitte `planner login` ausführen ({exc})")
        return 1
    return 0


def cmd_sync_raumzeit(args) -> int:
    from .connectors.ical_sync import sync_url

    settings = load_settings()
    if not settings.raumzeit_ics_url:
        print("RAUMZEIT_ICS_URL fehlt in .env")
        return 1
    conn = connect(settings.db_path)
    res = sync_url(conn, "raumzeit", settings.raumzeit_ics_url, force=args.force)
    if res.skipped:
        print("Übersprungen: letzter Abruf < 30 Minuten her (--force zum Erzwingen).")
        return 0
    if res.initial:
        n = conn.execute("SELECT COUNT(*) FROM external_events WHERE source='raumzeit'").fetchone()[0]
        print(f"Erstimport: {n} Termine gespeichert (keine Meldungen).")
        return 0
    for ev in res.added:
        print(f"+ NEU        {ev.start}  {ev.title}  {ev.location}")
    for old, new in res.changed:
        print(f"~ GEÄNDERT   {old.title}: {old.start} {old.location} -> {new.start} {new.location}")
    for ev in res.removed:
        print(f"- ENTFÄLLT   {ev.start}  {ev.title}")
    if not (res.added or res.changed or res.removed):
        print("Keine Änderungen.")
    return 0


def cmd_mirror_raumzeit(_args) -> int:
    """Spiegelt den Stundenplan in den Google-Kalender „Stundenplan“."""
    from .mirror import mirror_source

    settings = load_settings()
    conn = connect(settings.db_path)
    try:
        creds = get_credentials(settings)
    except ReauthNeeded as exc:
        print(f"Bitte `planner login` ausführen ({exc})")
        return 1
    cal = CalendarClient(creds, conn)
    cal_id = cal.ensure_calendar("Stundenplan", "calendar_id:stundenplan", "Aus raumzeit gespiegelt")
    res = mirror_source(conn, cal, source="raumzeit", calendar_id=cal_id)
    print(f"Stundenplan-Kalender: +{res.created} neu, ~{res.updated} geändert, "
          f"-{res.deleted} gelöscht, ={res.unchanged} unverändert")
    return 0


def cmd_log(args) -> int:
    conn = connect(load_settings().db_path)
    for row in reversed(changelog.recent(conn, args.n)):
        print(f"{row['ts']}  {row['actor']:<8} {row['action']:<16} {row['entity']}:{row['entity_id']}  {row['detail'] or ''}")
    return 0


def cmd_calendar_check(_args) -> int:
    """Liest nur: legt höchstens den Nebenkalender „Planer“ an und zählt Termine der nächsten 7 Tage."""
    settings = load_settings()
    conn = connect(settings.db_path)
    try:
        creds = get_credentials(settings)
    except ReauthNeeded as exc:
        print(f"Bitte `planner login` ausführen ({exc})")
        return 1
    cal = CalendarClient(creds, conn)
    planner_id = cal.ensure_planner_calendar()
    changelog.log(conn, actor="system", action="calendar.ensure", entity="calendar", entity_id=planner_id)
    now = datetime.now(UTC)
    for c in cal.list_calendars():
        n = len(cal.list_events(c["id"], now, now + timedelta(days=7)))
        mark = "  <- Planer" if c["id"] == planner_id else ""
        print(f"{c.get('summary', '?'):<40} {n:>3} Termine in 7 Tagen{mark}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="planner")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="Google-Login (öffnet den Browser)").set_defaults(fn=cmd_login)
    sub.add_parser("status", help="Zustand anzeigen").set_defaults(fn=cmd_status)
    p_log = sub.add_parser("log", help="Change-Log anzeigen")
    p_log.add_argument("-n", type=int, default=20)
    p_log.set_defaults(fn=cmd_log)
    p_rz = sub.add_parser("sync-raumzeit", help="Stundenplan von raumzeit abrufen und Änderungen zeigen")
    p_rz.add_argument("--force", action="store_true", help="30-Minuten-Sperre ignorieren")
    p_rz.set_defaults(fn=cmd_sync_raumzeit)
    sub.add_parser("mirror-raumzeit", help="Stundenplan in Google-Kalender „Stundenplan“ spiegeln").set_defaults(
        fn=cmd_mirror_raumzeit
    )
    sub.add_parser("calendar-check", help="Kalender-Zugriff testen (nur lesend)").set_defaults(
        fn=cmd_calendar_check
    )
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
