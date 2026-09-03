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

    default_usd_to_cny_rate: float = 7.23

    openai_api_key_free_1: SecretStr
    openai_api_key_free_2: SecretStr | None = None
    openai_api_key_free_3: SecretStr | None = None
    openai_api_key_pro_plan: SecretStr | None = None
    a6_api_key: SecretStr | None = None

    litellm_master_key: SecretStr
    litellm_port: int = 4000
    fastapi_port: int = 8000
    connect_timeout_seconds: float = 5.0

    # === Payload Offloading (S3 / MinIO) Settings ===
    enable_payload_offload: bool = True
    payload_s3_endpoint: str = "http://minio.minio.svc.cluster.local:9000"
    payload_s3_access_key: str = "litellm_admin"
    payload_s3_secret_key: SecretStr = SecretStr("CHANGE_ME")
    payload_bucket_name: str = "litellm-payloads"
    payload_public_base_url: str = "https://payloads.jppwl.asia/litellm-payloads"
    payload_upload_timeout_seconds: float = 2.0

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

    @field_validator("default_usd_to_cny_rate")
    @classmethod
    def validate_fx_rate(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("default_usd_to_cny_rate must be positive")
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
        "default_usd_to_cny_rate": settings.default_usd_to_cny_rate,
        "litellm_port": settings.litellm_port,
        "fastapi_port": settings.fastapi_port,
        "enable_payload_offload": settings.enable_payload_offload,
        "payload_s3_endpoint": settings.payload_s3_endpoint,
        "payload_bucket_name": settings.payload_bucket_name,
        "secrets": "***",
    }
