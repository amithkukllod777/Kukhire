import os
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


class Settings(BaseSettings):
    app_name: str = "KukHire API"
    api_version: str = "0.3.0"
    environment: str = "development"

    # Persistent storage. SQLite is a local-development convenience only; production uses
    # PostgreSQL (docs/MVP_ARCHITECTURE.md). There is deliberately no in-memory fallback.
    # Precedence: an explicit argument > KUKHIRE_DATABASE_URL > DATABASE_URL (the name the previous
    # Settings read, kept working) > the local SQLite default.
    database_url: str = Field(default_factory=lambda: os.getenv("DATABASE_URL") or "sqlite:///./kukhire.db")

    max_resume_size_mb: int = 10
    allowed_resume_extensions: tuple[str, ...] = ("pdf", "docx")

    # --- Identity (the shared Kuklabs Account; see KUKLABS_IDENTITY.md) ------------------
    # Base URL of the AuthKit REST server, e.g. "https://<auth host>". There is no default on
    # purpose: pointing at a host nobody has verified would be worse than refusing to start
    # authenticated routes. When unset, every authenticated route answers 503.
    auth_base_url: str = ""
    # Sent as X-Kuklabs-Product so the identity server attributes the membership correctly.
    auth_product_id: str = "kukhire"
    auth_timeout_seconds: float = 5.0
    # Verified principals are cached briefly so one page load is not N identity round-trips.
    auth_cache_seconds: int = 30

    # --- HRMS handoff -------------------------------------------------------------------
    # Hosts KukHire may send a hire handoff to (comma separated, exact host[:port] match).
    # Empty means NO handoff target is allowed. An org admin supplies the URL and an API key;
    # the allowlist stops that URL being pointed at an internal service (SSRF).
    hrms_allowed_hosts_csv: str = ""
    # Plain-http targets are refused unless this is set (local development / tests only).
    hrms_allow_insecure_http: bool = False
    hrms_timeout_seconds: float = 10.0

    # Fernet key for secrets at rest (org HRMS API keys, mailbox tokens).
    secret_encryption_key: str = ""

    # --- AI evaluation ------------------------------------------------------------------
    # Off by default. Scores are advisory and never change an application's stage.
    evaluation_enabled: bool = False
    evaluation_provider: str = "ollama"
    evaluation_model: str = "gemma3:4b"

    # --- Web ----------------------------------------------------------------------------
    # Origins allowed to call the API from a browser on another host. Empty = same-origin only.
    cors_allowed_origins_csv: str = ""
    serve_ui: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_prefix="KUKHIRE_",
    )

    @field_validator("auth_base_url")
    @classmethod
    def _strip_auth_base(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @property
    def hrms_allowed_hosts(self) -> tuple[str, ...]:
        return tuple(h.lower() for h in _split_csv(self.hrms_allowed_hosts_csv))

    @property
    def cors_allowed_origins(self) -> tuple[str, ...]:
        return _split_csv(self.cors_allowed_origins_csv)


@lru_cache
def get_settings() -> Settings:
    return Settings()
