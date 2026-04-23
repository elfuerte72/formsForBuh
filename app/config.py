"""Application settings loaded from environment / .env."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings for the Stage 1 (webhook + vision) slice.

    Google Sheets / Drive fields are intentionally absent — they will be added
    when those services are wired in a later plan.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Core ----------------------------------------------------------------
    anthropic_api_key: str = Field(..., description="Anthropic API key used by VisionService")
    webhook_secret: str = Field(..., description="Shared secret expected in X-Webhook-Secret")

    # --- Tunables ------------------------------------------------------------
    log_level: str = Field("INFO", description="Log level (DEBUG/INFO/WARNING/ERROR)")
    log_format: str = Field("json", description="'json' for prod, 'pretty' for dev")
    anthropic_model: str = Field("claude-sonnet-4-6", description="Model used by VisionService")
    max_image_dpi: int = Field(200, description="DPI when rasterising PDF page to PNG")
    max_download_bytes: int = Field(25 * 1024 * 1024, description="Hard cap on downloaded file size")
    http_timeout_seconds: float = Field(30.0, description="httpx timeout for file download")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor — safe to call from any DI factory."""
    return Settings()  # type: ignore[call-arg]


def redact_settings(settings: Settings) -> dict[str, object]:
    """Return a loggable dict with secrets masked."""
    data = settings.model_dump()
    for key in ("anthropic_api_key", "webhook_secret"):
        value = data.get(key) or ""
        data[key] = f"***{value[-4:]}" if len(value) >= 4 else "***"
    return data
