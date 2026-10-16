"""Gmail lesen (nur lesend, Gmail-API mit dem bestehenden Google-Login).

Standardmäßig nur der Posteingang, Kategorie „Allgemein“ (keine Werbung/Soziales/Benachrichtigungen) –
das hält Newsletter fern und spart Rechenzeit des lokalen Modells. Inhalte gehen nur ans lokale Modell.
"""

from __future__ import annotations

import base64
import re
from datetime import UTC, datetime
from email.utils import parseaddr
from html import unescape

from googleapiclient.discovery import build

from ..rules import TZ
from .mailbox import MailHeader

DEFAULT_QUERY = "in:inbox category:primary"


def _header(headers: list[dict], name: str) -> str:
    return next((h.get("value", "") for h in headers if h.get("name", "").lower() == name.lower()), "")


def sender_name(raw: str) -> str:
    name, addr = parseaddr(raw)
    return name or addr or raw


def parse_header(msg: dict) -> MailHeader:
    headers = (msg.get("payload") or {}).get("headers") or []
    received = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=UTC).astimezone(TZ)
    return MailHeader(item_id=msg["id"], received=received, sender=sender_name(_header(headers, "From")),
                      subject=_header(headers, "Subject"))


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", html)
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r" *\n *", "\n", text)).strip()


def body_text(payload: dict) -> str:
    """Text einer Mail: text/plain bevorzugt, sonst text/html in Text umgewandelt (Anhänge ignoriert)."""
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if part.get("filename"):
            return   # Anhang
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            html.append(_decode(data))
        for sub in part.get("parts") or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    return html_to_text("\n".join(html))


class GmailClient:
    def __init__(self, creds, *, query: str = DEFAULT_QUERY, service=None):
        self._svc = service or build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._query = query

    def list_inbox(self, limit: int = 25) -> list[MailHeader]:
        res = self._svc.users().messages().list(userId="me", q=self._query, maxResults=limit).execute()
        out = []
        for ref in res.get("messages") or []:
            msg = self._svc.users().messages().get(
                userId="me", id=ref["id"], format="metadata", metadataHeaders=["From", "Subject"]).execute()
            out.append(parse_header(msg))
        return out

    def get_body(self, item_id: str) -> str:
        msg = self._svc.users().messages().get(userId="me", id=item_id, format="full").execute()
        return body_text(msg.get("payload") or {})

    def close(self) -> None:
        pass
