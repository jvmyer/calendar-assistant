"""HKA-Mail über den OWA-Webzugang (owa-cert.h-ka.de) – nur lesend.

Kein IMAP an der HKA, EWS scheitert; der OWA-Weg ist inoffiziell (Details: docs/KONZEPT.md §4):
Client-Zertifikat (HARICA, Leaf + Zwischenzertifikat) + Formular-Login → Cookie `cadata`, danach die
JSON-Schnittstelle `service.svc` mit `X-OWA-CANARY`. Jede Anfrage auf frischer Verbindung
(`Connection: close`), weil der Server wiederverwendete Verbindungen kappt.

Login-Schutz wie bei ILIAS: höchstens EIN Login-Versuch pro Client; schlägt er fehl, wird nie
automatisch neu versucht, bis der Nutzer `planner mail-login` ausführt.
"""

from __future__ import annotations

import json
import os
import ssl
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from ..rules import TZ
from .mailbox import MailHeader

BASE = "https://owa-cert.h-ka.de"
HOST = "owa-cert.h-ka.de"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"


class OwaAuthError(Exception):
    """Login fehlgeschlagen oder nicht möglich – nicht automatisch wiederholen."""


def _is_logon(resp: httpx.Response) -> bool:
    return "logon.aspx" in str(resp.url).lower() or resp.status_code in (401, 440)


def _ssl_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.load_cert_chain(str(cert_path), str(key_path))
    return ctx


class OwaClient:
    def __init__(self, user: str, password: str, cert_path: Path, key_path: Path, session_path: Path, *,
                 client: httpx.Client | None = None):
        self._user, self._password, self._path = user, password, session_path
        self._http = client or httpx.Client(
            verify=_ssl_context(cert_path, key_path), timeout=60, follow_redirects=True,
            headers={"User-Agent": UA, "Connection": "close"})
        self._login_tried = False
        self._canary: str | None = None
        self._load_cookies()

    # -- Sitzung -----------------------------------------------------------------------------
    def _load_cookies(self) -> None:
        if self._path.exists():
            for name, value in json.loads(self._path.read_text()).items():
                self._http.cookies.set(name, value, domain=HOST)

    def _save_cookies(self) -> None:
        self._path.write_text(json.dumps({c.name: c.value for c in self._http.cookies.jar}))
        os.chmod(self._path, 0o600)

    def login(self) -> None:
        """Genau ein Versuch pro Client-Instanz; Fehler → OwaAuthError."""
        if self._login_tried:
            raise OwaAuthError("Login schon versucht – kein weiterer Versuch")
        self._login_tried = True
        if not (self._user and self._password):
            raise OwaAuthError("HKA_USER/HKA_PASSWORD fehlen in .env")
        self._http.cookies.clear()
        resp = self._http.post(f"{BASE}/owa/auth.owa", data={
            "destination": f"{BASE}/owa/", "flags": "4", "forcedownlevel": "0",
            "username": self._user, "password": self._password, "passwordText": "", "isUtf8": "1",
        })
        if "cadata" not in {c.name for c in self._http.cookies.jar} or _is_logon(resp):
            raise OwaAuthError("Login abgelehnt (Zugangsdaten falsch?)")
        self._canary = None
        self._save_cookies()

    def _ensure_canary(self) -> str:
        if self._canary:
            return self._canary
        resp = self._http.get(f"{BASE}/owa/")
        if _is_logon(resp):
            self.login()
            resp = self._http.get(f"{BASE}/owa/")
            if _is_logon(resp):
                raise OwaAuthError("Nach dem Login weiter Anmeldeseite")
        canary = resp.headers.get("X-OWA-CANARY") or self._http.cookies.get("X-OWA-CANARY", domain=HOST)
        if not canary:
            raise OwaAuthError("Kein X-OWA-CANARY (OWA geändert?)")
        self._canary = canary
        self._save_cookies()
        return canary

    def _call(self, action: str, body: dict) -> dict:
        """service.svc-Aufruf; ist die Sitzung abgelaufen, einmal einloggen und wiederholen."""
        payload = {"__type": f"{action}JsonRequest:#Exchange",
                   "Header": {"__type": "JsonRequestHeaders:#Exchange", "RequestServerVersion": "Exchange2013"},
                   "Body": body}
        for attempt in (0, 1):
            resp = self._http.post(
                f"{BASE}/owa/service.svc?action={action}",
                headers={"Action": action, "X-OWA-CANARY": self._ensure_canary(),
                         "X-OWA-UrlPostData": quote(json.dumps(payload)),
                         "Content-Type": "application/json; charset=utf-8"})
            if _is_logon(resp) and attempt == 0:
                self._canary = None
                self.login()
                continue
            if _is_logon(resp):
                raise OwaAuthError("Sitzung nach dem Login ungültig")
            resp.raise_for_status()
            msg = resp.json()["Body"]["ResponseMessages"]["Items"][0]
            if msg.get("ResponseClass") != "Success":
                raise ValueError(f"OWA {action}: {msg.get('ResponseCode', '?')}")
            return msg
        raise OwaAuthError("OWA nicht abrufbar")

    # -- Abruf -------------------------------------------------------------------------------
    def list_inbox(self, limit: int = 25) -> list[MailHeader]:
        """Neueste Mails des Posteingangs (nur Kopfdaten)."""
        prop = lambda f: {"__type": "PropertyUri:#Exchange", "FieldURI": f}
        msg = self._call("FindItem", {
            "__type": "FindItemRequest:#Exchange",
            "ItemShape": {"__type": "ItemResponseShape:#Exchange", "BaseShape": "IdOnly",
                          "AdditionalProperties": [prop("Subject"), prop("DateTimeReceived"), prop("From"),
                                                   prop("ConversationId")]},
            "ParentFolderIds": [{"__type": "DistinguishedFolderId:#Exchange", "Id": "inbox"}],
            "Traversal": "Shallow",
            "Paging": {"__type": "IndexedPageView:#Exchange", "BasePoint": "Beginning", "Offset": 0,
                       "MaxEntriesReturned": limit},
            "SortOrder": [{"__type": "SortResults:#Exchange", "Order": "Descending",
                           "Path": prop("DateTimeReceived")}],
        })
        return [parse_header(i) for i in msg["RootFolder"]["Items"]]

    def get_body(self, item_id: str) -> str:
        """Text der Mail (OWA wandelt HTML in Text um)."""
        msg = self._call("GetItem", {
            "__type": "GetItemRequest:#Exchange",
            "ItemShape": {"__type": "ItemResponseShape:#Exchange", "BaseShape": "IdOnly", "BodyType": "Text",
                          "AdditionalProperties": [{"__type": "PropertyUri:#Exchange", "FieldURI": "Body"}]},
            "ItemIds": [{"__type": "ItemId:#Exchange", "Id": item_id}],
        })
        items = msg.get("Items") or []
        return ((items[0].get("Body") or {}).get("Value") or "") if items else ""

    def close(self) -> None:
        self._http.close()


def parse_header(item: dict) -> MailHeader:
    mailbox = (item.get("From") or {}).get("Mailbox") or {}
    sender = mailbox.get("Name") or mailbox.get("EmailAddress") or ""
    return MailHeader(
        item_id=item["ItemId"]["Id"],
        received=datetime.fromisoformat(item["DateTimeReceived"]).astimezone(TZ),
        sender=sender,
        subject=item.get("Subject") or "",
        thread_id=(item.get("ConversationId") or {}).get("Id"),
    )
