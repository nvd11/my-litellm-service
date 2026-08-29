FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH="/app" \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./

RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev --no-install-project \
    && rm -rf /root/.cache

COPY config.yaml ./config.yaml
COPY app ./app

USER 65532:65532

EXPOSE 4000

ENTRYPOINT ["litellm"]
CMD ["--config", "/app/config.yaml", "--host", "0.0.0.0", "--port", "4000"]
