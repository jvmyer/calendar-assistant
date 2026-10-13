"""Kommandozeile: `planner login | status | log | calendar-check`."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

from . import changelog
from .calendar_client import CalendarClient
from .config import load_settings
from .db import connect, set_meta
from .google_auth import ReauthNeeded, get_credentials, login


def cmd_login(_args) -> int:
    settings = load_settings()
    conn = connect(settings.db_path)
    login(settings)
    set_meta(conn, "google_login_at", changelog.now_iso())
    set_meta(conn, "reauth_notified", "0")
    set_meta(conn, "expiry_warned", "0")
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


def make_notifier(settings):
    """Telegram, wenn konfiguriert; sonst nur Log-Ausgabe."""
    from .service import LogNotifier

    if settings.telegram_enabled:
        from .telegram_bot import TelegramNotifier

        return TelegramNotifier(settings.telegram_bot_token, settings.telegram_user_id)
    return LogNotifier()


def cmd_run(args) -> int:
    from .service import run_forever, sync_cycle

    settings = load_settings()
    conn = connect(settings.db_path)
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    notifier = make_notifier(settings)
    bot_starter = None
    if settings.telegram_enabled:
        from .telegram_bot import quiet_http_loggers, start_bot

        quiet_http_loggers()   # Token steckt in den API-URLs → nie loggen
        bot_starter = lambda wake, stop: start_bot(settings, wake, stop)
    else:
        logging.getLogger("planner").warning("Telegram nicht konfiguriert (TELEGRAM_BOT_TOKEN/TELEGRAM_USER_ID) – nur Log")
    if args.once:
        sync_cycle(conn, settings, notifier)
        return 0
    run_forever(conn, settings, notifier, interval_min=args.interval, bot_starter=bot_starter)
    return 0


def cmd_bot(args) -> int:
    """Nur der Telegram-Empfang, ohne Google-Sync (zum Testen). Strg+C oder --minutes beendet."""
    import logging
    import threading
    import time

    from .telegram_bot import quiet_http_loggers, start_bot

    settings = load_settings()
    if not settings.telegram_enabled:
        print("TELEGRAM_BOT_TOKEN und/oder TELEGRAM_USER_ID fehlen in .env")
        return 1
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    quiet_http_loggers()
    stop, wake = threading.Event(), threading.Event()
    start_bot(settings, wake, stop)
    end = time.monotonic() + args.minutes * 60 if args.minutes else None
    try:
        while end is None or time.monotonic() < end:
            time.sleep(1)
            wake.clear()   # ohne Sync gibt es nichts zu wecken
    except KeyboardInterrupt:
        pass
    stop.set()
    time.sleep(2)
    return 0


def cmd_ilias_login(_args) -> int:
    """Expliziter Login (genau ein Versuch); hebt die automatische Sperre nach einem Fehlschlag auf."""
    from .connectors.ilias import IliasAuthError, IliasClient

    settings = load_settings()
    conn = connect(settings.db_path)
    client = IliasClient(settings.hka_user, settings.hka_password, settings.ilias_session_path)
    try:
        client.fetch_news_html()
    except IliasAuthError as exc:
        print(f"ILIAS-Login fehlgeschlagen: {exc}\nKein weiterer Versuch – bitte Zugangsdaten in .env prüfen.")
        return 1
    finally:
        client.close()
    set_meta(conn, "ilias_blocked", "0")
    print("ILIAS-Login ok. Der Dienst darf ILIAS wieder abrufen.")
    return 0


def cmd_ilias_sync(_args) -> int:
    """Einmal abrufen und anzeigen, was neu ist (erster Lauf = stiller Erstimport)."""
    from .service import LogNotifier, ilias_step

    settings = load_settings()
    conn = connect(settings.db_path)
    res = ilias_step(conn, settings, LogNotifier(), datetime.now(UTC), force=True)
    if res is None:
        print("Nicht abgerufen (Zugangsdaten fehlen, gesperrt oder Fehler – siehe `planner log`).")
        return 1
    if res.initial:
        print(f"Erstimport: {res.stored} Einträge gespeichert (keine Meldungen).")
        return 0
    print(f"{res.stored} neu gespeichert, davon {len(res.task_like)} aufgaben-artig (→ Vorschläge).")
    for item in res.new:
        mark = "💡" if item in res.task_like else "  "
        print(f"{mark} {item.ts:%d.%m. %H:%M} {item.author[:20]:<20} {item.title[:70]}")
    return 0


def cmd_ilias_news(args) -> int:
    from .bot_logic import ilias_overview

    conn = connect(load_settings().db_path)
    print(ilias_overview(conn, days=args.days, limit=args.n))
    return 0


def cmd_telegram_test(_args) -> int:
    """Schickt eine Testnachricht an den eingetragenen Nutzer (prüft Token, ID und ob /start gedrückt wurde)."""
    settings = load_settings()
    if not settings.telegram_enabled:
        print("TELEGRAM_BOT_TOKEN und/oder TELEGRAM_USER_ID fehlen in .env")
        return 1
    ok = make_notifier(settings).send("✅ Planer-Bot verbunden. Schreib /hilfe für die Befehle.")
    print("Testnachricht gesendet." if ok else
          "Senden fehlgeschlagen. Token falsch? Hast du dem Bot in Telegram einmal „Start“ geschickt?")
    return 0 if ok else 1


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


def cmd_add(args) -> int:
    from . import areas, tasks
    from .parsing import parse_deadline, parse_estimate

    conn = connect(load_settings().db_path)
    try:
        deadline = parse_deadline(args.deadline) if args.deadline else None
        estimate = parse_estimate(args.est) if args.est else None
    except ValueError as exc:
        print(exc)
        return 1
    area_id = areas.ensure_path(conn, args.area, actor="user") if args.area else None
    task_id = tasks.add_task(conn, title=args.title, source="manual", actor="user", area_id=area_id,
                             deadline=deadline, estimate_min=estimate)
    if args.boost != 1.0:
        conn.execute("UPDATE tasks SET priority_boost = ? WHERE id = ?", (args.boost, task_id))
        conn.commit()
    print(f"Aufgabe #{task_id} angelegt. Mit `planner plan` einplanen (der Dienst tut das im Takt selbst).")
    return 0


def cmd_tasks(_args) -> int:
    from . import areas, tasks

    conn = connect(load_settings().db_path)
    rows = tasks.open_tasks(conn)
    if not rows:
        print("Keine offenen Aufgaben.")
    for t in rows:
        est = f"{t['estimate_min']} Min." if t["estimate_min"] else "–"
        print(f"#{t['id']:<4} {t['title']:<40} Deadline: {t['deadline'] or '–':<18} Aufwand: {est:<9} "
              f"Bereich: {areas.path_of(conn, t['area_id']) or '–'}")
    return 0


def cmd_set_status(args) -> int:
    from . import tasks

    conn = connect(load_settings().db_path)
    try:
        tasks.set_status(conn, args.id, args.status, actor="user")
    except KeyError:
        print(f"Aufgabe #{args.id} gibt es nicht.")
        return 1
    print(f"Aufgabe #{args.id}: {args.status}. Die Blocker verschwinden beim nächsten Planungslauf.")
    return 0


def cmd_area(args) -> int:
    from . import areas

    conn = connect(load_settings().db_path)
    if args.area_cmd == "set":
        try:
            area_id = areas.ensure_path(conn, args.path, actor="user")
            areas.set_weight(conn, area_id, args.weight, actor="user")
        except ValueError as exc:
            print(exc)
            return 1
        print(f"{areas.path_of(conn, area_id)}: Gewicht {args.weight}")
        return 0
    nodes = areas.tree(conn)
    if not nodes:
        print("Noch keine Bereiche. Beispiel: planner area set Uni/Modul-X 0.8")
    for depth, _id, name, weight in nodes:
        print(f"{'  ' * depth}{name} ({weight})")
    return 0


class _PrintNotifier:
    def send(self, text: str) -> None:
        print(text)


def cmd_plan(args) -> int:
    from .gtasks import TasksClient
    from .planning import describe_plan, run_plan
    from .rules import load_rules
    from .service import planning_step

    settings = load_settings()
    conn = connect(settings.db_path)
    try:
        rules = load_rules(settings.rules_path)
        creds = get_credentials(settings)
    except ReauthNeeded as exc:
        print(f"Bitte `planner login` ausführen ({exc})")
        return 1
    except ValueError as exc:
        print(f"rules.toml: {exc}")
        return 1
    cal = CalendarClient(creds, conn)
    if args.dry_run:
        report = run_plan(conn, cal, rules, dry_run=True)
        print("[Probelauf, nichts in Kalender oder Tasks geschrieben]")
        print(describe_plan(report) or "Plan unverändert.")
        if report.pinned or report.removed_by_user:
            print(f"Erkannt: {report.pinned} verschoben (gepinnt), {report.removed_by_user} gelöscht.")
        return 0
    planning_step(conn, cal, TasksClient(creds, conn), rules, _PrintNotifier(), datetime.now(UTC))
    print("Planung abgeschlossen (Kalender-Blocker und Google Tasks aktuell).")
    return 0


def cmd_propose(args) -> int:
    from . import areas, proposals
    from .parsing import parse_deadline, parse_estimate

    conn = connect(load_settings().db_path)
    try:
        start = parse_deadline(args.start) if args.start else None
        end = parse_deadline(args.end) if args.end else None
        if args.kind == "event" and not (start and end and "T" in start and "T" in end):
            raise ValueError("Termin braucht --start und --end mit Uhrzeit, z. B. '5.11. 13:30'")
        deadline = parse_deadline(args.deadline) if args.deadline else None
        estimate = parse_estimate(args.est) if args.est else None
        area_id = areas.ensure_path(conn, args.area, actor="user") if args.area else None
        pid = proposals.add_proposal(conn, title=args.title, kind=args.kind, source="manual", actor="user",
                                     start=start, end=end, deadline=deadline, estimate_min=estimate,
                                     area_id=area_id, note=args.note)
    except ValueError as exc:
        print(exc)
        return 1
    print(f"Vorschlag #{pid} angelegt. Er kommt nur mit `planner accept {pid}` in den Kalender.")
    return 0


def cmd_proposals(_args) -> int:
    from . import proposals

    conn = connect(load_settings().db_path)
    rows = proposals.pending(conn)
    if not rows:
        print("Keine offenen Vorschläge.")
    for row in rows:
        print(proposals.describe(row))
    return 0


def cmd_proposal_decision(args) -> int:
    from . import proposals

    conn = connect(load_settings().db_path)
    try:
        if args.accept:
            task_id = proposals.accept(conn, args.id, actor="user")
            print(f"Vorschlag #{args.id} bestätigt."
                  + (f" Aufgabe #{task_id} angelegt." if task_id else " Der Termin wird beim nächsten `planner plan` bzw. Dienst-Lauf eingetragen."))
        else:
            proposals.reject(conn, args.id, actor="user")
            print(f"Vorschlag #{args.id} verworfen – es landet nichts im Kalender.")
    except KeyError:
        print(f"Vorschlag #{args.id} gibt es nicht.")
        return 1
    except ValueError as exc:
        print(exc)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="planner")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add", help="Aufgabe anlegen")
    p_add.add_argument("title")
    p_add.add_argument("--deadline", help="z. B. 2026-10-20, 20.10. oder '20.10. 18:00'")
    p_add.add_argument("--est", help="Aufwand, z. B. 90, 45m, 2h")
    p_add.add_argument("--area", help="Bereichspfad, z. B. Uni/Modul-X")
    p_add.add_argument("--boost", type=float, default=1.0, help="manueller Prioritätsfaktor")
    p_add.set_defaults(fn=cmd_add)
    sub.add_parser("tasks", help="Offene Aufgaben anzeigen").set_defaults(fn=cmd_tasks)
    for name, status, text in (("done", "done", "Aufgabe erledigt"), ("drop", "dropped", "Aufgabe verwerfen")):
        p = sub.add_parser(name, help=text)
        p.add_argument("id", type=int)
        p.set_defaults(fn=cmd_set_status, status=status)
    p_area = sub.add_parser("area", help="Bereiche und Gewichte")
    area_sub = p_area.add_subparsers(dest="area_cmd", required=True)
    area_sub.add_parser("list", help="Bereichsbaum anzeigen")
    p_set = area_sub.add_parser("set", help="Bereich anlegen bzw. Gewicht setzen")
    p_set.add_argument("path")
    p_set.add_argument("weight", type=float)
    p_area.set_defaults(fn=cmd_area)
    p_prop = sub.add_parser("propose", help="Vorschlag anlegen (kommt nur nach Bestätigung in den Kalender)")
    p_prop.add_argument("title")
    p_prop.add_argument("--kind", choices=["event", "task"], default="event")
    p_prop.add_argument("--start", help="Termin: Beginn, z. B. '5.11. 13:30'")
    p_prop.add_argument("--end", help="Termin: Ende, z. B. '5.11. 17:30'")
    p_prop.add_argument("--deadline", help="Aufgabe: Deadline")
    p_prop.add_argument("--est", help="Aufgabe: Aufwand, z. B. 30m")
    p_prop.add_argument("--area")
    p_prop.add_argument("--note")
    p_prop.set_defaults(fn=cmd_propose)
    sub.add_parser("proposals", help="Offene Vorschläge anzeigen").set_defaults(fn=cmd_proposals)
    for name, accept, text in (("accept", True, "Vorschlag bestätigen → Kalender"), ("reject", False, "Vorschlag verwerfen")):
        p = sub.add_parser(name, help=text)
        p.add_argument("id", type=int)
        p.set_defaults(fn=cmd_proposal_decision, accept=accept)
    p_plan = sub.add_parser("plan", help="Aufgaben jetzt einplanen")
    p_plan.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts in den Kalender schreiben")
    p_plan.set_defaults(fn=cmd_plan)
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
    p_run = sub.add_parser("run", help="Hintergrunddienst (Sync-Takt)")
    p_run.add_argument("--once", action="store_true", help="Nur einen Durchlauf, dann beenden")
    p_run.add_argument("--interval", type=int, default=30, help="Minuten zwischen Läufen (min. 30)")
    p_run.set_defaults(fn=cmd_run)
    p_bot = sub.add_parser("bot", help="Nur den Telegram-Bot starten (ohne Google-Sync, zum Testen)")
    p_bot.add_argument("--minutes", type=int, default=0, help="nach n Minuten beenden (0 = bis Strg+C)")
    p_bot.set_defaults(fn=cmd_bot)
    sub.add_parser("ilias-login", help="ILIAS-Login prüfen (1 Versuch) und Sperre aufheben").set_defaults(
        fn=cmd_ilias_login)
    sub.add_parser("ilias-sync", help="ILIAS-Neuigkeiten jetzt abrufen").set_defaults(fn=cmd_ilias_sync)
    p_news = sub.add_parser("ilias-news", help="Gespeicherte ILIAS-Neuigkeiten anzeigen")
    p_news.add_argument("--days", type=int, default=14)
    p_news.add_argument("-n", type=int, default=20)
    p_news.set_defaults(fn=cmd_ilias_news)
    sub.add_parser("telegram-test", help="Testnachricht an Telegram senden").set_defaults(fn=cmd_telegram_test)
    sub.add_parser("calendar-check", help="Kalender-Zugriff testen (nur lesend)").set_defaults(
        fn=cmd_calendar_check
    )
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
