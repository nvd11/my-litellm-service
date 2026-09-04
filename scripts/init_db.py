"""Idempotent initialization script for OCI MySQL litellm_db and llm_request_logs."""

import asyncio
import sys

import aiomysql

from app.core.config import get_settings

CREATE_DATABASE_SQL = """
CREATE DATABASE IF NOT EXISTS litellm_db
DEFAULT CHARACTER SET utf8mb4
COLLATE utf8mb4_unicode_ci;
"""

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS llm_request_logs (
    id VARCHAR(36) PRIMARY KEY,
    request_id VARCHAR(128) NOT NULL,
    api_key_alias VARCHAR(64) DEFAULT 'default',
    model_requested VARCHAR(64) NOT NULL,
    model_used VARCHAR(64) NOT NULL,
    provider VARCHAR(64) NOT NULL DEFAULT 'unknown',
    provider_key_alias VARCHAR(64) NOT NULL DEFAULT 'unknown',
    prompt_tokens INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    total_tokens INT NOT NULL DEFAULT 0,
    cost_usd DECIMAL(10, 6) NOT NULL DEFAULT 0.000000,
    cost_cny DECIMAL(10, 6) NOT NULL DEFAULT 0.000000,
    fx_rate DECIMAL(8, 4) NOT NULL DEFAULT 7.2300,
    latency_ms INT NOT NULL DEFAULT 0,
    status_code INT NOT NULL DEFAULT 200,
    error_msg TEXT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_logs_created_at (created_at),
    INDEX idx_logs_model_used (model_used),
    INDEX idx_logs_provider (provider),
    INDEX idx_logs_provider_key (provider_key_alias),
    INDEX idx_logs_status_code (status_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

CREATE_VIEW_SQL = """
CREATE OR REPLACE VIEW v_llm_request_details AS
SELECT 
    l.id,
    l.request_id,
    l.api_key_alias,
    l.model_requested,
    l.model_used,
    l.provider,
    l.provider_key_alias,
    l.prompt_tokens,
    l.completion_tokens,
    l.total_tokens,
    l.cost_usd,
    l.cost_cny,
    l.fx_rate,
    l.latency_ms,
    l.status_code,
    l.error_msg,
    l.created_at,
    CONCAT(
        'https://minio.jppwl.asia/litellm-payloads/',
        DATE_FORMAT(l.created_at, '%Y-%m-%d'), '/',
        l.request_id, '/prompt.json'
    ) AS prompt_url,
    CONCAT(
        'https://minio.jppwl.asia/litellm-payloads/',
        DATE_FORMAT(l.created_at, '%Y-%m-%d'), '/',
        l.request_id, '/response.json'
    ) AS response_url
FROM llm_request_logs l;
"""


async def init_database() -> bool:
    settings = get_settings()
    print(f"Connecting to MySQL server at {settings.mysql_host}:{settings.mysql_port}...")

    # 1. Connect without selecting database to create database if not exists
    try:
        conn = await aiomysql.connect(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            autocommit=True,
        )
        cursor = await conn.cursor()
        print("Creating database 'litellm_db' if not exists...")
        await cursor.execute(CREATE_DATABASE_SQL)
        await cursor.close()
        conn.close()
    except Exception as error:
        print(f"Failed to create database: {type(error).__name__}: {error}")
        return False

    # 2. Connect to litellm_db to create table if not exists
    try:
        conn = await aiomysql.connect(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password.get_secret_value(),
            db=settings.mysql_db,
            connect_timeout=settings.connect_timeout_seconds,
            autocommit=True,
        )
        cursor = await conn.cursor()
        print("Creating table 'llm_request_logs' if not exists...")
        await cursor.execute(CREATE_TABLE_SQL)

        print("Creating view 'v_llm_request_details' if not exists...")
        await cursor.execute(CREATE_VIEW_SQL)

        await cursor.execute("SHOW TABLES LIKE 'llm_request_logs';")
        tables = await cursor.fetchall()
        await cursor.close()
        conn.close()

        if tables:
            print("Database 'litellm_db' and table 'llm_request_logs' initialized successfully.")
            return True
        print("Table 'llm_request_logs' verification failed.")
        return False
    except Exception as error:
        print(f"Failed to create table: {type(error).__name__}: {error}")
        return False


def main() -> int:
    success = asyncio.run(init_database())
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
