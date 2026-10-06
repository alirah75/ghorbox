"""Validated settings; errors exposed to users never contain input values."""

from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.utils.token import TokenValidationError, validate_token
from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError


class ConfigurationError(Exception):
    """Safe configuration failure with field names only."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8-sig",
        extra="forbid",
        hide_input_in_errors=True,
        frozen=True,
    )

    bot_token: SecretStr = Field(repr=False)
    bootstrap_admin_ids: tuple[int, ...] = Field(repr=False)
    app_timezone: str = "Asia/Tehran"
    data_dir: Path = Path("data")
    secrets_dir: Path = Path(".secrets")
    retention_enabled: bool = False

    @field_validator("bot_token")
    @classmethod
    def check_token(cls, value: SecretStr) -> SecretStr:
        try:
            validate_token(value.get_secret_value())
        except TokenValidationError:
            raise ValueError("invalid bot token") from None
        return value

    @field_validator("bootstrap_admin_ids", mode="before")
    @classmethod
    def check_admins(cls, value: object) -> tuple[int, ...]:
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("at least one administrator is required")
        if any(type(item) is not int or not 0 < item < 2**52 for item in value):
            raise ValueError("administrator IDs must be positive integers")
        if len(set(value)) != len(value):
            raise ValueError("duplicate administrator IDs")
        return tuple(value)

    @field_validator("app_timezone")
    @classmethod
    def check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown timezone") from None
        return value


def load_settings(env_file: Path = Path(".env")) -> Settings:
    try:
        return Settings(_env_file=env_file)
    except ValidationError as error:
        # Do not expose arbitrary unknown keys or Pydantic's input/context payloads.
        fields = sorted(
            {
                str(item["loc"][0]).upper()
                for item in error.errors(include_input=False, include_context=False)
                if item["loc"] and item["loc"][0] in Settings.model_fields
            }
        )
        suffix = ", ".join(fields) if fields else "ENV_FILE"
        raise ConfigurationError(f"تنظیمات معتبر نیست: {suffix}") from None
    except (SettingsError, OSError, UnicodeError):
        raise ConfigurationError("فایل تنظیمات قابل خواندن یا پردازش نیست.") from None
