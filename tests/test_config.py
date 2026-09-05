import pytest
from pydantic import ValidationError

from app.core.config import Settings, parse_csv_origins, redacted_summary


def _env() -> dict[str, str]:
    return {
        "MYSQL_HOST": "mysql.example.internal",
        "MYSQL_PORT": "3306",
        "MYSQL_USER": "test-user",
        "MYSQL_PASSWORD": "test-mysql-password",
        "MYSQL_DB": "litellm_test",
        "REDIS_HOST": "redis.example.internal",
        "REDIS_PORT": "6379",
        "REDIS_PASSWORD": "test-redis-password",
        "OPENAI_API_KEY_FREE_1": "test-gemini-key",
        "LITELLM_MASTER_KEY": "test-master-key",
    }


def test_settings_load_from_environment(monkeypatch):
    for key, value in _env().items():
        monkeypatch.setenv(key, value)

    settings = Settings()

    assert settings.mysql_host == "mysql.example.internal"
    assert settings.mysql_port == 3306
    assert settings.litellm_port == 4000
    assert settings.default_usd_to_cny_rate == 7.23
    assert redacted_summary(settings)["secrets"] == "***"


def test_settings_rejects_invalid_port(monkeypatch):
    values = _env()
    values["MYSQL_PORT"] = "70000"
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError):
        Settings()


def test_settings_rejects_invalid_fx_rate(monkeypatch):
    values = _env()
    values["DEFAULT_USD_TO_CNY_RATE"] = "-1.0"
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError):
        Settings()


def test_redacted_summary_does_not_expose_secrets(monkeypatch):
    for key, value in _env().items():
        monkeypatch.setenv(key, value)

    summary = str(redacted_summary(Settings()))
    assert "test-mysql-password" not in summary
    assert "test-redis-password" not in summary
    assert "test-gemini-key" not in summary


def test_parse_csv_origins():
    default = ["http://localhost:5173"]

    assert parse_csv_origins(None, default) == default
    assert parse_csv_origins("", default) == default
    assert parse_csv_origins("   ", default) == default
    assert parse_csv_origins("https://a.example.com, https://b.example.com ", default) == [
        "https://a.example.com",
        "https://b.example.com",
    ]
    assert parse_csv_origins("*", default) == ["*"]
