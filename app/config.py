"""Configuration globale de l'application (variables d'environnement / fichier .env)."""
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Application ---
    app_name: str = "FlotteBox"
    secret_key: str = "change-me-in-production"
    base_url: str = "http://localhost:8000"  # URL publique (utilisée dans les SMS)
    data_dir: Path = Path("data")
    database_url: str = ""  # par défaut : sqlite dans data_dir
    timezone: str = "Europe/Paris"
    allow_signup: bool = True  # inscription libre d'une nouvelle société (mode SaaS)
    enable_scheduler: bool = True
    secure_cookies: bool = False  # True derrière HTTPS

    # --- Location directe ---
    access_code_lead_minutes: int = 30  # envoi du code X minutes avant le début
    access_code_grace_hours: int = 6  # validité du code après la fin de location

    # --- SMS ---
    sms_provider: str = "console"  # console | twilio | brevo
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from: str = ""
    brevo_api_key: str = ""
    brevo_sender: str = "FlotteBox"

    # --- Claude (assistant + lecture des contraventions) ---
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "low"

    # --- Getaround ---
    getaround_base_url: str = "https://api-eu.getaround.com/owner/v1"
    getaround_signature_header: str = "X-Getaround-Signature"

    # --- Pennylane ---
    pennylane_base_url: str = "https://app.pennylane.com/api/external/v2"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'app.db').as_posix()}"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def fines_inbox_dir(self) -> Path:
        """Dossier surveillé : déposez-y les avis de contravention (PDF/JPG/PNG)."""
        return self.data_dir / "contraventions_a_traiter"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.uploads_dir.mkdir(parents=True, exist_ok=True)
    s.fines_inbox_dir.mkdir(parents=True, exist_ok=True)
    return s
