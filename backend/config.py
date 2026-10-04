"""Small, environment-based configuration (backend/.env is optional)."""

from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BACKEND_DIR.parent / ".env", BACKEND_DIR / ".env"),
        extra="ignore",
        populate_by_name=True,
    )

    ollama_model: str = "qwen3:8b"
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_api_key: SecretStr | None = None
    e2b_api_key: SecretStr | None = None
    e2b_template: str = "pydantic-ai-chat-python312"
    e2b_base_image: str = Field(default="python:3.12-slim", min_length=1)
    database_path: Path = BACKEND_DIR / "data" / "chat.sqlite3"
    sandbox_idle_timeout_seconds: int = Field(default=600, ge=60, le=86400)
    sandbox_max_lifetime_seconds: int = Field(default=3600, ge=60, le=86400)
    chat_timeout_seconds: int = Field(default=180, ge=10, le=600)

    @model_validator(mode="after")
    def validate_timeouts(self):
        # A chat turn must finish before the server-side sandbox deadline.
        if self.chat_timeout_seconds >= self.sandbox_idle_timeout_seconds:
            raise ValueError("CHAT_TIMEOUT_SECONDS must be less than SANDBOX_IDLE_TIMEOUT_SECONDS")
        if self.sandbox_max_lifetime_seconds < self.sandbox_idle_timeout_seconds:
            raise ValueError("SANDBOX_MAX_LIFETIME_SECONDS must be >= SANDBOX_IDLE_TIMEOUT_SECONDS")
        return self
