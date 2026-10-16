"""Konfiguration aus Umgebungsvariablen bzw. .env im Projektroot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    google_client_id: str
    google_client_secret: str
    raumzeit_ics_url: str = ""
    hka_user: str = ""
    hka_password: str = ""
    telegram_bot_token: str = ""
    telegram_user_id: int | None = None   # nur dieser Nutzer darf den Bot bedienen
    mail_enabled: bool = False            # HKA-Mail abrufen (MAIL_ENABLED=1)
    mail_feedback: bool = True            # unter Mail-Aufgaben nach Einschätzung fragen (MAIL_FEEDBACK=0: aus)
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:4b"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_user_id)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "planner.db"

    @property
    def ilias_session_path(self) -> Path:
        return self.data_dir / "ilias_session.json"

    @property
    def owa_session_path(self) -> Path:
        return self.data_dir / "owa_session.json"

    @property
    def owa_cert_path(self) -> Path:
        return self.data_dir / "secrets" / "hka_chain.pem"   # Leaf + Zwischenzertifikat (nur Leaf scheitert)

    @property
    def owa_key_path(self) -> Path:
        return self.data_dir / "secrets" / "hka_key.pem"

    @property
    def rules_path(self) -> Path:
        return self.data_dir / "rules.toml"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "google_token.json"


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value.strip()) if value else None
    except ValueError:
        return None


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    data_dir = Path(os.environ.get("PLANNER_DATA_DIR") or PROJECT_ROOT / "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        data_dir=data_dir,
        google_client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
        google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        raumzeit_ics_url=os.environ.get("RAUMZEIT_ICS_URL", ""),
        hka_user=os.environ.get("HKA_USER", "").strip(),
        hka_password=os.environ.get("HKA_PASSWORD", ""),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_user_id=_int_or_none(os.environ.get("TELEGRAM_USER_ID")),
        mail_enabled=os.environ.get("MAIL_ENABLED", "").strip() == "1",
        mail_feedback=os.environ.get("MAIL_FEEDBACK", "1").strip() != "0",
        ollama_url=os.environ.get("OLLAMA_URL", "").strip() or "http://127.0.0.1:11434",
        ollama_model=os.environ.get("OLLAMA_MODEL", "").strip() or "qwen3:4b",
    )
