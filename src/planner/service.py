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

from . import changelog
from .calendar_client import CalendarClient
from .config import Settings
from .connectors.ical_sync import Event, SyncResult, sync_url
from .db import get_meta, set_meta
from .google_auth import ReauthNeeded, get_credentials
from .mirror import mirror_source

log = logging.getLogger("planner")
TZ = ZoneInfo("Europe/Berlin")
MIN_INTERVAL_MIN = 30
TOKEN_WARN_AFTER = timedelta(days=6)  # Testmodus: Refresh-Token läuft nach 7 Tagen ab
MAX_LINES = 8


class Notifier(Protocol):
    def send(self, text: str) -> None: ...


class LogNotifier:
    def send(self, text: str) -> None:
        log.info("MELDUNG: %s", text.replace("\n", " | "))


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


def sync_cycle(
    conn: sqlite3.Connection,
    settings: Settings,
    notifier: Notifier,
    *,
    now: datetime | None = None,
    fetcher=None,
    calendar_factory=None,
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

    # 2) Nach Google spiegeln (nur Differenz)
    try:
        if calendar_factory is not None:
            cal = calendar_factory()
        else:
            cal = CalendarClient(get_credentials(settings), conn)
        cal_id = cal.ensure_calendar("Stundenplan", "calendar_id:stundenplan", "Aus raumzeit gespiegelt")
        mirror_source(conn, cal, source="raumzeit", calendar_id=cal_id, now=now)
        set_meta(conn, "reauth_notified", "0")
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
    conn: sqlite3.Connection, settings: Settings, notifier: Notifier, *, interval_min: int = 30
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    interval = max(interval_min, MIN_INTERVAL_MIN) * 60
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log.info("Dienst gestartet (Takt: %d Min.)", interval // 60)
    while not stop.is_set():
        sync_cycle(conn, settings, notifier)
        stop.wait(interval)
    log.info("Dienst beendet")
