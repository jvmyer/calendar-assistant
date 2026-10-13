"""ILIAS (HKA): Neuigkeiten abrufen, neue Einträge erkennen.

ILIAS 9 hat keine Übungsobjekte mit Fristen für uns; Aufgaben stehen in Dateien, Ordnern und
Foren. Die Seite „Neuigkeiten“ bündelt alles Neue aus allen Kursen und dient als Auslöser.

Login-Schutz: Es gibt pro Dienstlauf höchstens EINEN Login-Versuch. Schlägt er fehl, wird nie
automatisch neu versucht (Kontosperre!), bis der Nutzer `planner ilias-login` ausführt.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx

BASE = "https://ilias.h-ka.de/"
NEWS_PATH = "ilias.php?baseClass=ilDashboardGUI&cmd=jumpToNews"
LOGIN_FORM = re.compile(r'<form[^>]*name="login_form"[^>]*>(.*?)</form>', re.DOTALL)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"


TZ = ZoneInfo("Europe/Berlin")
MONTHS = {"jan": 1, "feb": 2, "mär": 3, "mrz": 3, "apr": 4, "mai": 5, "jun": 6, "jul": 7, "aug": 8,
          "sep": 9, "okt": 10, "nov": 11, "dez": 12}
_PANEL = re.compile(r'<div class="ilTimelinePanel">')
_EDIT = re.compile(
    r'ilNewsTimelineEditInfo">(.*?)</div>', re.DOTALL)
_STAMP = re.compile(r"\[(?P<uid>[\w.\-]+)\]\s*-\s*(?P<d>\d{1,2})\.\s*(?P<m>[A-Za-zÄÖÜäöü]+)\.?\s+(?P<y>\d{4}),\s*(?P<h>\d{1,2}):(?P<min>\d{2})")
_HEAD = re.compile(r"<h4[^>]*>(.*?)</h4>", re.DOTALL)
_OBJ = re.compile(r'ilNewsTimelineObjHead[^>]*>.*?<a[^>]*href="[^"]*goto\.php/(?P<type>\w+)/(?P<ref>\d+)[^"]*"[^>]*>(?P<title>.*?)</a>', re.DOTALL)
_BODY = re.compile(r'dynamic-height-wrap">(.*?)</div>\s*<button', re.DOTALL)
_NEWS_ID = re.compile(r"news_id=(\d+)")


@dataclass(frozen=True)
class NewsItem:
    news_id: str
    author: str          # „Nachname, Vorname“ bzw. Kürzel
    uid: str             # Kürzel, z. B. abcd0001
    ts: datetime         # Europe/Berlin
    headline: str        # z. B. „Die Datei wurde hinzugefügt.“ oder Titel des Beitrags
    obj_type: str        # file | frm | crs | …
    ref_id: str
    title: str           # Dateiname bzw. Objekttitel
    body: str            # Text (z. B. Forenbeitrag), kann leer sein

    @property
    def url(self) -> str:
        return f"{BASE}goto.php/{self.obj_type}/{self.ref_id}"


def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_news(html: str) -> list[NewsItem]:
    """Liest alle Einträge der Neuigkeiten-Seite (unsortiert; ILIAS liefert auch Jahre alte)."""
    items: dict[str, NewsItem] = {}
    parts = _PANEL.split(html)[1:]
    for part in parts:
        nid = _NEWS_ID.search(part)
        edit = _EDIT.search(part)
        stamp = _STAMP.search(_clean(edit.group(1))) if edit else None
        obj = _OBJ.search(part)
        if not (nid and stamp and obj):
            continue   # unbekanntes Format: lieber auslassen als falsch raten
        month = MONTHS.get(stamp["m"].lower()[:3])
        if month is None:
            continue
        ts = datetime(int(stamp["y"]), month, int(stamp["d"]), int(stamp["h"]), int(stamp["min"]), tzinfo=TZ)
        head = _HEAD.search(part)
        body = _BODY.search(part)
        edit_text = _clean(edit.group(1))
        author = edit_text.split("[")[0].strip() or stamp["uid"]
        item = NewsItem(
            news_id=nid.group(1), author=author, uid=stamp["uid"], ts=ts,
            headline=_clean(head.group(1)) if head else "", obj_type=obj["type"], ref_id=obj["ref"],
            title=_clean(obj["title"]), body=_clean(body.group(1)) if body else "",
        )
        items.setdefault(item.news_id, item)
    return list(items.values())


class IliasAuthError(Exception):
    """Login fehlgeschlagen oder nicht möglich – nicht automatisch wiederholen."""


class IliasClient:
    def __init__(self, user: str, password: str, session_path: Path, *, client: httpx.Client | None = None):
        self._user, self._password, self._path = user, password, session_path
        self._http = client or httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
        self._login_tried = False
        self._load_cookies()

    # -- Sitzung ---------------------------------------------------------------------------
    def _load_cookies(self) -> None:
        if self._path.exists():
            for name, value in json.loads(self._path.read_text()).items():
                self._http.cookies.set(name, value, domain="ilias.h-ka.de")

    def _save_cookies(self) -> None:
        jar = {c.name: c.value for c in self._http.cookies.jar}
        self._path.write_text(json.dumps(jar))
        os.chmod(self._path, 0o600)

    @staticmethod
    def is_login_page(html: str) -> bool:
        return 'name="login_form"' in html

    def login(self) -> None:
        """Genau ein Versuch pro Client-Instanz; Fehler → IliasAuthError."""
        if self._login_tried:
            raise IliasAuthError("Login schon versucht – kein weiterer Versuch")
        self._login_tried = True
        if not (self._user and self._password):
            raise IliasAuthError("HKA_USER/HKA_PASSWORD fehlen in .env")
        page = self._http.get(BASE).text
        m = LOGIN_FORM.search(page)
        if not m:
            return   # schon eingeloggt (Sitzung noch gültig)
        open_tag = re.search(r'<form[^>]*name="login_form"[^>]*>', page).group(0)   # type: ignore[union-attr]
        action = unescape(re.search(r'action="([^"]+)"', open_tag).group(1))   # type: ignore[union-attr]
        data: dict[str, str] = {}
        user_key = pass_key = None
        for tag in re.findall(r"<input[^>]*>", m.group(1)):
            name = re.search(r'name="([^"]+)"', tag)
            kind = re.search(r'type="([^"]+)"', tag)
            if not name:
                continue
            value = re.search(r'value="([^"]*)"', tag)
            t = kind.group(1) if kind else "text"
            if t in ("submit", "button"):
                continue
            if t == "password":
                pass_key = name.group(1)
            elif t in ("text", "email") and user_key is None:
                user_key = name.group(1)
            data[name.group(1)] = unescape(value.group(1)) if value else ""
        if not (user_key and pass_key):
            raise IliasAuthError("Login-Formular nicht erkannt (ILIAS geändert?)")
        data[user_key], data[pass_key] = self._user, self._password
        resp = self._http.post(urljoin(BASE, action), data=data)
        if self.is_login_page(resp.text):
            raise IliasAuthError("Login abgelehnt (Zugangsdaten falsch?)")
        self._save_cookies()

    def get_page(self, path: str) -> str:
        """Seite holen; ist die Sitzung abgelaufen, einmal einloggen."""
        html = self._http.get(urljoin(BASE, path)).text
        if self.is_login_page(html):
            self.login()
            html = self._http.get(urljoin(BASE, path)).text
            if self.is_login_page(html):
                raise IliasAuthError("Nach dem Login weiter Anmeldeseite")
        return html

    def fetch_news_html(self, days: int = 30) -> str:
        """Neuigkeiten mit explizit gesetztem Zeitfilter.

        Der Standardabruf ist nicht stabil (liefert je nach Sitzungszustand Jahre alte Einträge);
        mit angewendetem Filter kommt immer dasselbe, neueste zuerst.
        """
        html = self.get_page(NEWS_PATH)
        m = re.search(r'data-cmd-apply="([^"]+)"', html)
        if not m:
            return html
        url = (urljoin(BASE, unescape(m.group(1)))
               + f"&filter_input_0/filter_input_1={days}&filter_input_0/filter_input_2=0"
               + "&__filter_status_news_per=1&__filter_status_news_ref_id=1")
        filtered = self._http.get(url).text
        return filtered if "ilTimeline" in filtered else html

    def close(self) -> None:
        self._http.close()


# -- Erkennung neuer Einträge ------------------------------------------------------------------
TASK_HINTS = re.compile(
    r"aufgabe|übung|uebung|abgabe|abgeben|einreichen|assignment|hausaufgabe|homework|exercise|deadline|"
    r"frist|anmeld|klausur|prüfung|praktikum|bis zum|spätestens|zu bearbeiten|bearbeiten sie",
    re.IGNORECASE,
)
MAX_AGE_DAYS = 45   # ältere Einträge nie als „neu“ melden


@dataclass
class NewsSyncResult:
    initial: bool = False
    stored: int = 0
    new: list[NewsItem] = field(default_factory=list)

    @property
    def task_like(self) -> list[NewsItem]:
        return [i for i in self.new if looks_like_task(i)]


def looks_like_task(item: NewsItem) -> bool:
    """Grobe Stichwortprüfung – Auswahl für Vorschläge, kein Urteil. Die Extraktion kommt später per LLM."""
    return bool(TASK_HINTS.search(f"{item.title} {item.headline} {item.body}"))


def sync_news(conn: sqlite3.Connection, html: str, *, now: datetime | None = None) -> NewsSyncResult:
    """Speichert Einträge; der erste Lauf ist ein stiller Erstimport (alles „seen“)."""
    from .. import changelog
    from ..db import get_meta, set_meta

    now = now or datetime.now(TZ)
    items = parse_news(html)
    if not items and "ilTimeline" not in html:
        raise ValueError("Neuigkeiten-Seite nicht erkannt (ILIAS geändert?)")
    result = NewsSyncResult(initial=get_meta(conn, "ilias_baselined") != "1")
    known = {r[0] for r in conn.execute("SELECT news_id FROM ilias_news")}
    stamp = changelog.now_iso()
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    for item in sorted(items, key=lambda i: i.ts):
        if item.news_id in known:
            continue
        fresh = not result.initial and item.ts >= cutoff
        conn.execute(
            "INSERT INTO ilias_news(news_id, ts, author, uid, headline, obj_type, ref_id, title, body, first_seen, state) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (item.news_id, item.ts.isoformat(), item.author, item.uid, item.headline, item.obj_type,
             item.ref_id, item.title, item.body, stamp, "new" if fresh else "seen"),
        )
        result.stored += 1
        if fresh:
            result.new.append(item)
    conn.commit()
    if result.initial:
        set_meta(conn, "ilias_baselined", "1")
    if result.new:
        changelog.log(conn, actor="ilias", action="news.new", entity="ilias", detail={
            "count": len(result.new), "titles": [i.title for i in result.new][:10]})
    return result
