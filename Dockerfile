FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH="/app" \
    PATH="/app/.venv/bin:$PATH" \
    PRISMA_HOME_DIR="/tmp/prisma-cache" \
    PRISMA_USE_GLOBAL_NODE="true"

WORKDIR /app

# 安装 OpenSSL 与 CA 证书，满足 Neon SSL 连接与 Prisma Engine 运行依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    openssl \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./

# 安装依赖并提前预生成 Prisma 客户端代码
RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev --no-install-project \
    && mkdir -p /tmp/prisma-cache \
    && PRISMA_HOME_DIR=/tmp/prisma-cache uv run prisma generate --schema=/app/.venv/lib/python3.12/site-packages/litellm/proxy/schema.prisma \
    && chmod -R 777 /tmp/prisma-cache \
    && rm -rf /root/.cache

COPY config.yaml ./config.yaml
COPY app ./app

USER 65532:65532

EXPOSE 4000

ENTRYPOINT ["litellm"]
CMD ["--config", "/app/config.yaml", "--host", "0.0.0.0", "--port", "4000"]
