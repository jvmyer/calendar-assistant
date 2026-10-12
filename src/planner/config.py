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

    @property
    def db_path(self) -> Path:
        return self.data_dir / "planner.db"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "google_token.json"


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    data_dir = Path(os.environ.get("PLANNER_DATA_DIR") or PROJECT_ROOT / "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        data_dir=data_dir,
        google_client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
        google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        raumzeit_ics_url=os.environ.get("RAUMZEIT_ICS_URL", ""),
    )
