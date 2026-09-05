-- ==============================================================================
-- MySQL Dynamic Hyperlink View for LiteLLM Payload Exploration (Phase 5)
-- ==============================================================================
-- Database: litellm_db (OCI MySQL HeatWave / Neon PG)
-- Base Table: llm_request_logs
-- Purpose: Dynamically construct one-click direct HTTPS hyperlinks for Prompt and
--          Response payloads offloaded to NUC MinIO S3 object storage.
-- ==============================================================================

USE litellm_db;

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
    -- 动态拼接 NUC MinIO S3 Prompt 输入报文直达超链接
    CONCAT(
        'https://payloads.jppwl.asia/payloads/',
        DATE_FORMAT(l.created_at, '%Y-%m-%d'), '/',
        l.request_id, '/prompt.json'
    ) AS prompt_url,
    -- 动态拼接 NUC MinIO S3 Response 输出报文直达超链接
    CONCAT(
        'https://payloads.jppwl.asia/payloads/',
        DATE_FORMAT(l.created_at, '%Y-%m-%d'), '/',
        l.request_id, '/response.json'
    ) AS response_url
FROM llm_request_logs l;
