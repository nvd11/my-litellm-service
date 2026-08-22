import pytest

from app.core import connectivity
from app.core.config import Settings


def _settings() -> Settings:
    return Settings(
        mysql_host="mysql.example.internal",
        mysql_user="test-user",
        mysql_password="mysql-password",
        mysql_db="litellm_test",
        redis_host="redis.example.internal",
        redis_password="redis-password",
        openai_api_key_free_1="gemini-key",
        litellm_master_key="master-key",
    )


@pytest.mark.asyncio
async def test_check_mysql_success(monkeypatch):
    class Cursor:
        async def execute(self, query):
            assert query == "SELECT 1"

        async def fetchone(self):
            return (1,)

        def close(self):
            self.closed = True

    class Connection:
        def __init__(self):
            self.cursor_instance = Cursor()
            self.closed = False

        async def cursor(self):
            return self.cursor_instance

        def close(self):
            self.closed = True

    connection = Connection()

    async def connect(**kwargs):
        assert kwargs["password"] == "mysql-password"
        return connection

    monkeypatch.setattr(connectivity.aiomysql, "connect", connect)
    result = await connectivity.check_mysql(_settings())

    assert result.ok is True
    assert result.detail == "connected"
    assert connection.closed is True


@pytest.mark.asyncio
async def test_check_mysql_auth_failure(monkeypatch):
    class AccessDeniedError(Exception):
        pass

    async def connect(**kwargs):
        raise AccessDeniedError("password=must-not-leak")

    monkeypatch.setattr(connectivity.aiomysql, "connect", connect)
    result = await connectivity.check_mysql(_settings())

    assert result.ok is False
    assert result.detail == "authentication_failed"
    assert "password" not in result.detail


@pytest.mark.asyncio
async def test_check_redis_success(monkeypatch):
    class Client:
        def __init__(self):
            self.closed = False

        async def ping(self):
            return True

        async def aclose(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(connectivity.redis_asyncio, "Redis", lambda **kwargs: client)

    result = await connectivity.check_redis(_settings())

    assert result.ok is True
    assert result.detail == "connected"
    assert client.closed is True


@pytest.mark.asyncio
async def test_check_all_has_stable_order(monkeypatch):
    async def mysql(_settings):
        return connectivity.CheckResult("mysql", True, 1.0, "connected")

    async def redis(_settings):
        return connectivity.CheckResult("redis", True, 2.0, "connected")

    monkeypatch.setattr(connectivity, "check_mysql", mysql)
    monkeypatch.setattr(connectivity, "check_redis", redis)

    results = await connectivity.check_all(_settings())

    assert [result.name for result in results] == ["mysql", "redis"]
