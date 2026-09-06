"""MinIO S3 Payload 后端实现"""

import json
import logging
from typing import Any

import aioboto3
from botocore.config import Config

from app.core.config import Settings
from app.core.payload_backend import PayloadBackend

logger = logging.getLogger(__name__)


class MinIOBackend(PayloadBackend):
    """MinIO S3 存储后端"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = aioboto3.Session()
        self.boto_config = Config(
            connect_timeout=settings.payload_upload_timeout_seconds,
            read_timeout=settings.payload_upload_timeout_seconds,
            retries={"max_attempts": 2},
        )

    async def write_payload(
        self,
        request_id: str,
        prompt: dict[str, Any],
        response: dict[str, Any],
        metadata: dict[str, Any],
    ) -> bool:
        """写入 MinIO S3（保持现有行为）

        Args:
            request_id: 请求唯一标识
            prompt: 用户输入报文
            response: 模型输出报文
            metadata: 调用元数据（需包含 date 字段，格式 YYYY-MM-DD）

        Returns:
            bool: 写入是否成功
        """
        date_str = metadata.get("date", "")
        if not date_str:
            logger.warning("MinIO write failed for %s: missing date in metadata", request_id)
            return False

        prefix = f"{date_str}/{request_id}"

        try:
            prompt_bytes = json.dumps(prompt, ensure_ascii=False, indent=2).encode("utf-8")
            response_bytes = json.dumps(response, ensure_ascii=False, indent=2).encode("utf-8")

            async with self.session.client(
                "s3",
                endpoint_url=self.settings.payload_s3_endpoint,
                aws_access_key_id=self.settings.payload_s3_access_key,
                aws_secret_access_key=self.settings.payload_s3_secret_key.get_secret_value(),
                config=self.boto_config,
            ) as s3_client:
                # 写入 prompt.json
                await s3_client.put_object(
                    Bucket=self.settings.payload_bucket_name,
                    Key=f"{prefix}/prompt.json",
                    Body=prompt_bytes,
                    ContentType="application/json; charset=utf-8",
                )
                # 写入 response.json
                await s3_client.put_object(
                    Bucket=self.settings.payload_bucket_name,
                    Key=f"{prefix}/response.json",
                    Body=response_bytes,
                    ContentType="application/json; charset=utf-8",
                )
            return True
        except Exception as e:
            logger.warning("MinIO write failed for %s: %s", request_id, e)
            return False

    async def read_payload(
        self,
        request_id: str,
        date: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """从 MinIO S3 读取（保持现有行为）

        Args:
            request_id: 请求唯一标识
            date: 日期分区（YYYY-MM-DD），必须提供

        Returns:
            tuple: (prompt_data, response_data)，不存在时返回空 dict
        """
        if not date:
            logger.warning("MinIO read failed for %s: date is required", request_id)
            return {}, {}

        prefix = f"{date}/{request_id}"
        prompt_data: dict[str, Any] = {}
        response_data: dict[str, Any] = {}

        try:
            async with self.session.client(
                "s3",
                endpoint_url=self.settings.payload_s3_endpoint,
                aws_access_key_id=self.settings.payload_s3_access_key,
                aws_secret_access_key=self.settings.payload_s3_secret_key.get_secret_value(),
                config=self.boto_config,
            ) as s3_client:
                # 读取 prompt.json
                try:
                    prompt_obj = await s3_client.get_object(
                        Bucket=self.settings.payload_bucket_name,
                        Key=f"{prefix}/prompt.json",
                    )
                    prompt_bytes = await prompt_obj["Body"].read()
                    prompt_data = json.loads(prompt_bytes.decode("utf-8"))
                except Exception as e:
                    logger.debug("Prompt payload not found in S3 for %s: %s", request_id, e)
                    prompt_data = {"user_prompt": "（此历史调用的原始输入报文未在 MinIO 归档）"}

                # 读取 response.json
                try:
                    resp_obj = await s3_client.get_object(
                        Bucket=self.settings.payload_bucket_name,
                        Key=f"{prefix}/response.json",
                    )
                    resp_bytes = await resp_obj["Body"].read()
                    response_data = json.loads(resp_bytes.decode("utf-8"))
                except Exception as e:
                    logger.debug("Response payload not found in S3 for %s: %s", request_id, e)
                    response_data = {"reply": "（此历史调用的原始模型回复未在 MinIO 归档）"}

            return prompt_data, response_data
        except Exception as exc:
            logger.warning("Failed to connect to S3 to read payload for %s: %s", request_id, exc)
            prompt_data = {"user_prompt": f"（S3 存储节点响应超时或暂时离线: {exc}）"}
            response_data = {"reply": "（无法从 NUC MinIO 读取回复报文）"}
            return prompt_data, response_data

    async def search_payloads(
        self,
        keyword: str,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 500,
    ) -> list[str]:
        """MinIO 不原生支持全文倒排检索，返回空列表."""
        logger.debug("search_payloads is not natively supported by MinIOBackend")
        return []

    async def health_check(self) -> bool:
        """MinIO 健康检查

        Returns:
            bool: 后端是否可用
        """
        try:
            async with self.session.client(
                "s3",
                endpoint_url=self.settings.payload_s3_endpoint,
                aws_access_key_id=self.settings.payload_s3_access_key,
                aws_secret_access_key=self.settings.payload_s3_secret_key.get_secret_value(),
                config=self.boto_config,
            ) as s3_client:
                await s3_client.list_buckets()
            return True
        except Exception as e:
            logger.warning("MinIO health check failed: %s", e)
            return False
