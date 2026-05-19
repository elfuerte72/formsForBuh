"""Application settings loaded from environment / .env."""

from functools import cached_property, lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings for the custom UPD upload form + Google Sheets archival slice."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Core ----------------------------------------------------------------
    anthropic_api_key: str = Field(..., description="Anthropic API key used by VisionService")

    # --- Google Sheets -------------------------------------------------------
    sheet_id: str = Field(..., description="Google Sheet ID where UPD rows are appended")
    google_credentials_json: str = Field(
        ...,
        description=(
            "Service Account credentials as a single-line JSON string "
            "(escape \\n inside private_key)."
        ),
    )

    # --- Tunables ------------------------------------------------------------
    log_level: str = Field("INFO", description="Log level (DEBUG/INFO/WARNING/ERROR)")
    log_format: str = Field("json", description="'json' for prod, 'pretty' for dev")
    anthropic_model: str = Field("claude-sonnet-4-6", description="Model used by VisionService")
    max_image_dpi: int = Field(200, description="DPI when rasterising PDF page to PNG")
    max_upload_bytes: int = Field(25 * 1024 * 1024, description="Hard cap on uploaded file size")
    max_batch_files: int = Field(10, description="Maximum number of files per /api/upload request")
    http_timeout_seconds: float = Field(30.0, description="httpx timeout for outbound calls")

    @cached_property
    def sheet_url(self) -> str:
        """Public-facing URL to the spreadsheet (used in success responses)."""
        return f"https://docs.google.com/spreadsheets/d/{self.sheet_id}/edit"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor — safe to call from any DI factory."""
    return Settings()  # type: ignore[call-arg]


def redact_settings(settings: Settings) -> dict[str, object]:
    """Return a loggable dict with secrets masked."""
    data = settings.model_dump()
    for key in ("anthropic_api_key", "google_credentials_json"):
        value = data.get(key) or ""
        data[key] = f"***{value[-4:]}" if len(value) >= 4 else "***"
    return data
