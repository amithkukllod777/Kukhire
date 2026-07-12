from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "KukHire API"
    api_version: str = "0.2.0"
    environment: str = "development"
    database_url: str = "sqlite:///./kukhire.db"
    max_resume_size_mb: int = 10
    allowed_resume_extensions: tuple[str, ...] = ("pdf", "docx")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
