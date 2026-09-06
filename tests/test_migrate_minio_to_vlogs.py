"""MinIO → VictoriaLogs 迁移脚本测试."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from scripts.migrate_minio_to_vlogs import MinioToVlogsMigrator


class TestMinioToVlogsMigrator:
    """MinioToVlogsMigrator 测试."""

    @pytest.fixture
    def settings(self) -> Settings:
        """测试用 Settings."""
        return Settings(
            mysql_host="localhost",
            mysql_port=3306,
            mysql_user="test",
            mysql_password=SecretStr("test"),
            mysql_db="test",
            redis_host="localhost",
            redis_password=SecretStr("test"),
            litellm_master_key=SecretStr("test"),
            payload_s3_endpoint="http://localhost:9000",
            payload_s3_access_key="test",
            payload_s3_secret_key=SecretStr("test"),
            payload_bucket_name="payloads",
            victorialogs_url="http://localhost:9428",
        )

    @pytest.fixture
    def migrator(self, settings: Settings) -> MinioToVlogsMigrator:
        """测试用迁移器."""
        return MinioToVlogsMigrator(settings, dry_run=True)

    def test_init(self, migrator: MinioToVlogsMigrator):
        """初始化测试."""
        assert migrator.settings is not None
        assert migrator.dry_run is True
        assert migrator.stats["total_objects"] == 0
        assert migrator.stats["migrated_requests"] == 0
        assert migrator.stats["failed_requests"] == 0
        assert migrator.stats["skipped_requests"] == 0

    @pytest.mark.asyncio
    async def test_list_minio_requests(self, migrator: MinioToVlogsMigrator):
        """列出所有 request_id 测试."""
        xml_output = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Contents><Key>2026-09-01/req-001/prompt.json</Key></Contents>
  <Contents><Key>2026-09-01/req-001/response.json</Key></Contents>
  <Contents><Key>2026-09-01/req-002/prompt.json</Key></Contents>
  <Contents><Key>2026-09-02/req-003/prompt.json</Key></Contents>
  <Contents><Key>2026-09-02/req-003/response.json</Key></Contents>
</ListBucketResult>"""

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(xml_output.encode("utf-8"), b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await migrator.list_minio_requests()

            assert len(result) == 3
            assert {"date": "2026-09-01", "request_id": "req-001"} in result
            assert {"date": "2026-09-01", "request_id": "req-002"} in result
            assert {"date": "2026-09-02", "request_id": "req-003"} in result

    @pytest.mark.asyncio
    async def test_read_minio_payload(self, migrator: MinioToVlogsMigrator):
        """从 MinIO 读取 payload 测试."""
        mock_proc_prompt = MagicMock()
        mock_proc_prompt.communicate = AsyncMock(return_value=(b'{"user_prompt": "test"}', b""))
        mock_proc_prompt.returncode = 0

        mock_proc_resp = MagicMock()
        mock_proc_resp.communicate = AsyncMock(return_value=(b'{"reply": "test"}', b""))
        mock_proc_resp.returncode = 0

        with patch(
            "asyncio.create_subprocess_exec", side_effect=[mock_proc_prompt, mock_proc_resp]
        ):
            prompt, response = await migrator.read_minio_payload("req-001", "2026-09-01")

            assert prompt == {"user_prompt": "test"}
            assert response == {"reply": "test"}

    @pytest.mark.asyncio
    async def test_fetch_metadata_from_mysql(self, migrator: MinioToVlogsMigrator):
        """从 MySQL 读取元数据测试."""
        mock_cursor = MagicMock()
        mock_cursor.execute = AsyncMock()
        mock_cursor.fetchone = AsyncMock(
            return_value={
                "request_id": "req-001",
                "api_key_alias": "test-key",
                "model_used": "test-model",
                "provider": "google-gemini",
                "provider_key_alias": "test-provider-key",
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
                "cost_usd": 0.001,
                "cost_cny": 0.007,
                "latency_ms": 100,
                "status_code": 200,
                "created_at": None,
            }
        )

        mock_cursor_ctx = AsyncMock()
        mock_cursor_ctx.__aenter__.return_value = mock_cursor
        mock_cursor_ctx.__aexit__.return_value = None

        mock_conn = MagicMock()
        mock_conn.cursor = MagicMock(return_value=mock_cursor_ctx)
        mock_conn.close = MagicMock()

        with patch("aiomysql.connect", AsyncMock(return_value=mock_conn)):
            metadata = await migrator.fetch_metadata_from_mysql("req-001")

            assert metadata["request_id"] == "req-001"
            assert metadata["model"] == "test-model"
            assert metadata["key_alias"] == "test-key"
            assert metadata["prompt_tokens"] == 10
            assert metadata["completion_tokens"] == 20
            assert metadata["total_tokens"] == 30
            assert metadata["spend"] == 0.001
            assert metadata["latency_ms"] == 100
            assert metadata["status_code"] == 200

    @pytest.mark.asyncio
    async def test_write_to_victorialogs_dry_run(self, migrator: MinioToVlogsMigrator):
        """Dry-run 模式测试."""
        result = await migrator.write_to_victorialogs(
            request_id="req-001",
            prompt={"user_prompt": "test"},
            response={"reply": "test"},
            metadata={"model": "test-model"},
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_write_to_victorialogs_success(self, settings: Settings):
        """实际写入成功测试."""
        migrator = MinioToVlogsMigrator(settings, dry_run=False)

        mock_subprocess_res = MagicMock()
        mock_subprocess_res.stdout = "200\n"

        with patch("subprocess.run", return_value=mock_subprocess_res) as mock_run:
            result = await migrator.write_to_victorialogs(
                request_id="req-001",
                prompt={"user_prompt": "test"},
                response={"reply": "test"},
                metadata={
                    "timestamp": "2026-09-01T00:00:00+00:00",
                    "model": "test-model",
                    "key_alias": "test-key",
                    "status_code": 200,
                    "latency_ms": 100,
                },
            )

            assert result is True
            assert mock_run.call_count >= 1

    @pytest.mark.asyncio
    async def test_migrate_single(self, migrator: MinioToVlogsMigrator):
        """单条迁移测试."""
        with (
            patch.object(migrator, "read_minio_payload") as mock_read,
            patch.object(migrator, "fetch_metadata_from_mysql") as mock_meta,
            patch.object(migrator, "write_to_victorialogs") as mock_write,
        ):
            mock_read.return_value = ({"user_prompt": "test"}, {"reply": "test"})
            mock_meta.return_value = {"model": "test-model"}
            mock_write.return_value = True

            result = await migrator.migrate_single("req-001", "2026-09-01")

            assert result is True
            assert migrator.stats["migrated_requests"] == 1
            mock_read.assert_called_once_with("req-001", "2026-09-01")
            mock_meta.assert_called_once_with("req-001")
            mock_write.assert_called_once()

    @pytest.mark.asyncio
    async def test_migrate_single_no_payload(self, migrator: MinioToVlogsMigrator):
        """无 payload 跳过测试."""
        with patch.object(migrator, "read_minio_payload") as mock_read:
            mock_read.return_value = ({}, {})

            result = await migrator.migrate_single("req-001", "2026-09-01")

            assert result is False
            assert migrator.stats["skipped_requests"] == 1

    @pytest.mark.asyncio
    async def test_migrate_batch(self, migrator: MinioToVlogsMigrator):
        """批量迁移测试."""
        request_ids = [
            {"date": "2026-09-01", "request_id": "req-001"},
            {"date": "2026-09-01", "request_id": "req-002"},
            {"date": "2026-09-02", "request_id": "req-003"},
        ]

        with patch.object(migrator, "migrate_single") as mock_migrate:
            mock_migrate.side_effect = [True, True, False]

            await migrator.migrate_batch(request_ids, batch_size=2)
            assert mock_migrate.call_count == 3

    @pytest.mark.asyncio
    async def test_run_with_date_filter(self, migrator: MinioToVlogsMigrator):
        """带日期过滤的运行测试."""
        with (
            patch.object(migrator, "list_minio_requests") as mock_list,
            patch.object(migrator, "migrate_batch") as mock_batch,
        ):
            mock_list.return_value = [
                {"date": "2026-09-01", "request_id": "req-001"},
            ]

            await migrator.run(start_date="2026-09-01", end_date="2026-09-02")

            mock_list.assert_called_once_with("2026-09-01", "2026-09-02")
            mock_batch.assert_called_once()
            assert migrator.stats["end_time"] is not None
