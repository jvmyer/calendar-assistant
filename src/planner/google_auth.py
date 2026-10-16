"""Google-OAuth (Desktop-Client, Loopback-Flow).

Die App läuft im Teststatus → der Refresh-Token läuft nach 7 Tagen ab.
In diesem Fall wird `ReauthNeeded` geworfen; der Aufrufer (Bot/CLI) fordert zum Re-Login auf.
"""

from __future__ import annotations

import json

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import Settings

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
]
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"   # nur lesen
LOGIN_SCOPES = [*SCOPES, GMAIL_SCOPE]


class ReauthNeeded(Exception):
    """Kein gültiger Token vorhanden bzw. abgelaufen – `planner login` ausführen."""


def _client_config(settings: Settings) -> dict:
    if not settings.google_client_id or not settings.google_client_secret:
        raise RuntimeError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET fehlen in .env")
    return {
        "installed": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def login(settings: Settings) -> Credentials:
    """Interaktiver Login (öffnet den Browser) und speichert den Token."""
    flow = InstalledAppFlow.from_client_config(_client_config(settings), LOGIN_SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    _save(settings, creds)
    return creds


def get_credentials(settings: Settings) -> Credentials:
    """Gültige Credentials oder `ReauthNeeded`."""
    path = settings.token_path
    if not path.exists():
        raise ReauthNeeded("Noch nicht eingeloggt.")
    # Mit den beim Login gewährten Rechten laden (sonst gingen sie beim Speichern nach dem Refresh verloren).
    # Ältere Tokens haben noch kein Gmail-Recht → Kalender/Tasks laufen weiter, Gmail meldet sich.
    granted = json.loads(path.read_text()).get("scopes") or SCOPES
    if not set(SCOPES) <= set(granted):
        raise ReauthNeeded("Token ohne Kalender-/Tasks-Recht.")
    creds = Credentials.from_authorized_user_file(str(path), granted)
    if creds.valid:
        return creds
    if creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise ReauthNeeded(f"Refresh fehlgeschlagen (Token abgelaufen?): {exc}") from exc
        _save(settings, creds)
        return creds
    raise ReauthNeeded("Kein Refresh-Token vorhanden.")


def _save(settings: Settings, creds: Credentials) -> None:
    path = settings.token_path
    path.write_text(creds.to_json())
    path.chmod(0o600)


def has_gmail(creds: Credentials) -> bool:
    return GMAIL_SCOPE in (creds.scopes or [])
