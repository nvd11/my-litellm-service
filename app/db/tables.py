"""SQLAlchemy Core 数据库表结构定义模块 (Table Schemas).

业务背景与设计说明:
1. 采用纯 SQLAlchemy 2.0 Core 的 Table 声明模式，不引入 ORM 实体映射与 Session 状态开销，
   保持轻量、极速与微秒级响应性能。
2. 表 `llm_request_logs`:
   - 用于全量记录 LiteLLM 接收到的每一次普通与流式大模型请求;
   - 精准存储美金费用 (cost_usd DECIMAL(10, 6))、结算汇率 (fx_rate DECIMAL(8, 4))
     以及折合人民币费用 (cost_cny DECIMAL(10, 6));
   - 记录请求别名 (model_requested) 与实际命中上游模型 (model_used)，追踪梯队路由与降级轨迹;
   - 建立 `created_at`、`model_used`、`status_code` 三大核心查询索引，优化后续监控看板与报表性能。
"""

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    func,
)

# ==============================================================================
# 元数据集合定义 (MetaData Collection)
# ==============================================================================
metadata = MetaData()

# ==============================================================================
# 数据表定义: llm_request_logs (大模型请求审计与计费日志表)
# ==============================================================================
llm_request_logs = Table(
    "llm_request_logs",
    metadata,
    # 记录唯一主键 ID (UUID4 字符串)
    Column("id", String(36), primary_key=True, comment="记录唯一标识符 (UUID4)"),
    # LiteLLM 生成或透传的请求 ID
    Column("request_id", String(128), nullable=False, comment="LiteLLM API 请求 ID"),
    # 客户端 API Key 别名或团队身份标识
    Column(
        "api_key_alias",
        String(64),
        nullable=False,
        server_default="default",
        comment="客户端 Key 别名 / 团队标识",
    ),
    # 客户端请求的模型别名 (例如 gemini-3.7-flash)
    Column(
        "model_requested",
        String(64),
        nullable=False,
        comment="客户端请求的模型别名",
    ),
    # 实际命中并执行的上游模型 ID (例如 gemini-3.7-backup)
    Column(
        "model_used",
        String(64),
        nullable=False,
        comment="实际命中的上游模型 ID (追踪降级轨迹)",
    ),
    # 上游供应商与上游 API Key 标识
    Column(
        "provider",
        String(64),
        nullable=False,
        server_default="unknown",
        comment="上游真实供应商标识 (如 google-gemini, a6api.com)",
    ),
    Column(
        "provider_key_alias",
        String(64),
        nullable=False,
        server_default="unknown",
        comment="调用上游使用的 API Key 别名",
    ),
    # Token 计量字段 (提示、补全与总 Token 数)
    Column(
        "prompt_tokens",
        Integer,
        nullable=False,
        server_default="0",
        comment="输入/提示 Token 数",
    ),
    Column(
        "completion_tokens",
        Integer,
        nullable=False,
        server_default="0",
        comment="输出/补全 Token 数",
    ),
    Column(
        "total_tokens",
        Integer,
        nullable=False,
        server_default="0",
        comment="总 Token 消耗数",
    ),
    # 财务费用字段 (美金、人民币与结算汇率，统一采用 DECIMAL 避免浮点精度丢失)
    Column(
        "cost_usd",
        Numeric(10, 6),
        nullable=False,
        server_default="0.000000",
        comment="美金开销 (USD)",
    ),
    Column(
        "cost_cny",
        Numeric(10, 6),
        nullable=False,
        server_default="0.000000",
        comment="折合人民币开销 (RMB)",
    ),
    Column(
        "fx_rate",
        Numeric(8, 4),
        nullable=False,
        server_default="7.2300",
        comment="结算时采用的当日 USD/CNY 汇率",
    ),
    # 性能与状态字段
    Column(
        "latency_ms",
        Integer,
        nullable=False,
        server_default="0",
        comment="请求响应耗时 (毫秒)",
    ),
    Column(
        "status_code",
        Integer,
        nullable=False,
        server_default="200",
        comment="HTTP 响应状态码",
    ),
    # 记录创建时间戳 (默认数据库当前时间)
    Column(
        "created_at",
        DateTime,
        server_default=func.now(),
        nullable=False,
        comment="记录落库时间",
    ),
    # 索引优化
    Index("idx_logs_created_at", "created_at"),
    Index("idx_logs_model_used", "model_used"),
    Index("idx_logs_provider", "provider"),
    Index("idx_logs_provider_key", "provider_key_alias"),
    Index("idx_logs_status_code", "status_code"),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
    mysql_collate="utf8mb4_unicode_ci",
)
