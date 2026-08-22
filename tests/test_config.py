from pydantic import ValidationError

from app.core.config import Settings, redacted_summary


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
    assert redacted_summary(settings)["secrets"] == "***"


def test_settings_rejects_invalid_port(monkeypatch):
    values = _env()
    values["MYSQL_PORT"] = "70000"
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    try:
        Settings()
    except ValidationError:
        pass
    else:
        raise AssertionError("invalid port should be rejected")


def test_redacted_summary_does_not_expose_secrets(monkeypatch):
    for key, value in _env().items():
        monkeypatch.setenv(key, value)

    summary = str(redacted_summary(Settings()))
    assert "test-mysql-password" not in summary
    assert "test-redis-password" not in summary
    assert "test-gemini-key" not in summary
