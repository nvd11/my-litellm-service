"""Environment-backed application settings."""

from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated settings loaded from environment variables and local ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    mysql_host: str
    mysql_port: int = 3306
    mysql_user: str
    mysql_password: SecretStr
    mysql_db: str

    redis_host: str
    redis_port: int = 6379
    redis_password: SecretStr

    openai_api_key_free_1: SecretStr
    openai_api_key_free_2: SecretStr | None = None

    litellm_master_key: SecretStr
    litellm_port: int = 4000
    fastapi_port: int = 8000
    connect_timeout_seconds: float = 5.0

    @field_validator(
        "mysql_port",
        "redis_port",
        "litellm_port",
        "fastapi_port",
    )
    @classmethod
    def validate_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("port must be between 1 and 65535")
        return value

    @field_validator(
        "mysql_host",
        "mysql_user",
        "mysql_db",
        "redis_host",
    )
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be empty")
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings instance."""

    return Settings()


def redacted_summary(settings: Settings) -> dict[str, object]:
    """Return safe-to-log settings without exposing any secret values."""

    return {
        "mysql_host": settings.mysql_host,
        "mysql_port": settings.mysql_port,
        "mysql_db": settings.mysql_db,
        "redis_host": settings.redis_host,
        "redis_port": settings.redis_port,
        "litellm_port": settings.litellm_port,
        "fastapi_port": settings.fastapi_port,
        "secrets": "***",
    }
